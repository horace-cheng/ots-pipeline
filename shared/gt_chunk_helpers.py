"""
shared/gt_chunk_helpers.py

Shared helpers for the Gutenberg Track's three LLM stages:

  - gt_translate : English source segments → Traditional Chinese
  - gt_simplify  : Standard Chinese       → youth-friendly Chinese (whole-chapter stories)
  - gt_tailo     : Simplified Chinese     → Hanzi + Tai-lo annotation (segment-based)

Translate and tailo share the segment-based batching + [N]-marker pipeline.
Simplify concatenates each chapter's translated segments into a single
Gemini call and outputs whole-chapter stories. The simplified output is
split back into segments (by paragraph) for tailo's per-segment input.

Each stage ships as a separate Cloud Run Job; this module is the shared
contract between them — prompts, input/output paths, parsing, checkpoint
helpers, and the consolidated output writer.
"""
import json
import logging
import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.db import update_job_status, update_order_field
from shared.gemini import translate
from shared.storage import (
    read_temp_json, read_temp_text, write_temp_json,
)
from shared.notify import notify_stage

logger = logging.getLogger("gt_chunk_helpers")


# ── Prompts ──────────────────────────────────────────────────────────────────

TRANSLATE_PROMPT = """You are a professional literary translator. Translate the following English text segments into fluent, literary Traditional Chinese (繁體中文).

Maintain the original tone, style, and period feel. Preserve paragraph structure exactly — do not merge or split paragraphs.

Translation rules for specific terms:
{glossary}

CRITICAL: Output ONLY the translations — one per line prefixed with [1], [2], etc. Never output the glossary rules themselves, never explain your choices, never add commentary. The user cannot see anything except the translated text.

The input contains {batch_size} segments, each marked [1] through [{batch_size}].
Output each translation prefixed with the matching [N] marker, in order, with no commentary between them.
Do not output anything before [1] or after [{batch_size}].
"""


TAILO_PROMPT = """Convert the following Traditional Chinese text into a Hanzi-dominant version with selective Tai-lo romanization.

Use the MOE Tâi-lô orthography standard with tone marks (e.g. 你好 (Lí hó)).

Rules:
- Keep all Hanzi characters as-is; romanization is only added for words or particles that have no Hanzi representation (e.g. 的 (ê), 了 (liáu), 是 (sī))
- For words that already have a standard Hanzi form, do NOT append romanization
- Do NOT output pure romanization — always keep the Hanzi first
- Keep paragraph structure exactly
- Output ONLY the converted text — no explanations, no commentary, no extra formatting

The input contains {batch_size} segments, each marked [1] through [{batch_size}].
Output each converted version prefixed with the matching [N] marker, in order, with no commentary between them.
Do not output anything before [1] or after [{batch_size}].
"""

TRANSLATE_RETRY_PROMPT = """Translate the following {batch_size} English segment(s) into fluent, literary Traditional Chinese (繁體中文).

These are segments that were not properly translated in a previous attempt.
Use the same terminology mapping:
{glossary}

Segments:
{segments}
"""


TAILO_RETRY_PROMPT = """Convert the following {batch_size} Traditional Chinese segment(s) into Hanzi-dominant version with selective Tai-lo romanization.

Rules:
- Keep all Hanzi as-is; romanization is only added for words that have no Hanzi representation
- For words with a standard Hanzi form, do NOT append romanization
- Use MOE Tâi-lô orthography standard with tone marks

Segments:
{segments}
"""


SIMPLIFY_CHAPTER_PROMPT = """You are simplifying a chapter from a translated Chinese book so it is enjoyable and understandable for children aged 8-12.

Below is the full chapter text. Rewrite it as a coherent, flowing story that young readers can easily follow.

Rules:
- Use simple, common vocabulary (HSK 3-4 level)
- Write short sentences (ideally under 20 characters each)
- Keep the narrative flow natural — this should read like a story, not a list of disconnected sentences
- Preserve all key plot points, characters, and settings
- Do NOT add new characters, events, or interpretations
- Keep character and place names exactly as they appear in the original
- Maintain paragraph structure where it makes sense, but feel free to break long paragraphs into shorter ones for readability
- The output should be the full rewritten chapter — do NOT output segment markers like [1], [2], etc.
- Output ONLY the rewritten text — no explanations, no commentary

Chapter text:
{chapter_text}
"""


SIMPLIFIED_CHAPTERS_OUTPUT_PATH = "simplified_chapters.json"


# Per-stage job_type label for Gemini call (tracked in token_usage).
JOB_TYPE = {
    "translate": "gt_translate",
    "simplify":  "gt_simplify",
    "tailo":     "gt_tailo",
}

# Per-stage input/output filenames in the temp bucket.
INPUT_PATHS = {
    "translate": "segments.json",
    "simplify":  "translated.json",
    "tailo":     "simplified.json",
}

OUTPUT_PATHS = {
    "translate": "translated.json",
    "simplify":  "simplified.json",
    "tailo":     "tailo.json",
}

# Segment batch size for translate / tailo (10 mirrors the FT/LT design).
BATCH_SIZE = 10

# Max source words per batch for translate / tailo. Prevents output token
# exhaustion when multiple very long segments land in the same batch. A single
# segment exceeding this limit gets its own batch. Word count = split by
# whitespace (works naturally for English source; for Chinese source each
# segment is ~1 word so the batch_size count is the effective limit).
MAX_BATCH_WORDS = 500

# Plan artifact: per-stage batch boundaries, written to GCS at the start of
# the run for operator observability. Mirrors lt_preprocess_nmt's batches.json.
BATCHES_PLAN_PATH = "batches.json"

# Per-stage checkpoint prefix for segment-based batches (translate / tailo).
# Each stage uses its own prefix to prevent collisions — otherwise the
# second stage (tailo) loads checkpoints written by the first (translate)
# and skips all processing, producing the wrong output.
CHECKPOINT_STAGE_PREFIX = {
    "translate": "checkpoint_translate_batch_",
    "tailo":     "checkpoint_tailo_batch_",
}


def _stage_ckpt_filename(stage: str, batch_id: int) -> str:
    return f"{CHECKPOINT_STAGE_PREFIX[stage]}{batch_id}.json"


def list_stage_checkpoints(stage: str) -> List[int]:
    """Return sorted batch_ids for a specific stage's checkpoints."""
    from shared.storage import get_client
    from shared.config import cfg
    client = get_client()
    bucket = client.bucket(cfg.BUCKET_TEMP)
    prefix = f"pipeline/{cfg.ORDER_ID}/{CHECKPOINT_STAGE_PREFIX[stage]}"
    blobs = list(bucket.list_blobs(prefix=prefix))
    ids: List[int] = []
    for b in blobs:
        m = re.search(rf"{CHECKPOINT_STAGE_PREFIX[stage]}(\d+)\.json$", b.name)
        if m:
            ids.append(int(m.group(1)))
    return sorted(ids)


def save_stage_checkpoint(stage: str, batch_id: int, data: dict) -> None:
    write_temp_json(_stage_ckpt_filename(stage, batch_id), data)


def load_stage_checkpoint(stage: str, batch_id: int) -> Optional[dict]:
    try:
        return read_temp_json(_stage_ckpt_filename(stage, batch_id))
    except Exception:
        return None


# ── Terminology ──────────────────────────────────────────────────────────────

def load_terminology() -> Dict[str, str]:
    """Load terminology.json from GCS. Returns empty dict if missing."""
    try:
        data = read_temp_text("terminology.json")
        if not data:
            return {}
        if isinstance(data, dict):
            return {str(k): str(v) for k, v in data.items()}
        if isinstance(data, str):
            return json.loads(data)
    except Exception as e:
        logger.warning(f"terminology.json not available: {e}")
    return {}


def format_glossary_for_prompt(terminology: Dict[str, str]) -> str:
    if not terminology:
        return "No specific terminology provided."
    return "\n".join([f'- \"{k}\" must be translated as \"{v}\".' for k, v in terminology.items()])


# ── Inputs / outputs ────────────────────────────────────────────────────────

def load_segments() -> List[dict]:
    """Read `segments.json` (paragraph-level list)."""
    data = read_temp_json("segments.json")
    if not isinstance(data, list):
        raise ValueError("segments.json must be a list of segment dicts")
    return data


def load_input_segments(stage: str) -> List[dict]:
    """Load the segment list to feed into this stage's Gemini calls.

    For ``translate``: read ``segments.json`` directly. The ``text`` field
    already contains the English source.

    For ``simplify`` / ``tailo``: read the previous stage's consolidated
    output JSON and re-shape into a segments-shaped list. The ``text``
    field is set to the previous stage's ``translated`` so ``build_prompt``'s
    ``seg['text']`` works uniformly for all three stages.
    """
    if stage == "translate":
        return load_segments()

    path = INPUT_PATHS[stage]
    try:
        data = read_temp_json(path)
    except Exception as e:
        raise FileNotFoundError(
            f"{path} not found — {stage} stage requires the previous stage's "
            f"output. Run translate (and simplify) first. ({e})"
        ) from e
    if not isinstance(data, list):
        raise ValueError(f"{path} must be a list of segment dicts")
    for e in data:
        e.setdefault("text", e.get("translated", ""))
    return data


# ── Segment-based batching (translate / tailo) ──────────────────────────────

def build_batches(
    total_segments: int,
    batch_size: int = BATCH_SIZE,
    segments: Optional[List[dict]] = None,
) -> List[dict]:
    """Build batch boundaries, optionally size-aware.

    When ``segments`` is provided, each batch is limited to ``MAX_BATCH_WORDS``
    words of source text (whitespace-delimited; for Chinese source each segment
    is ~1 word). A single segment exceeding ``MAX_BATCH_WORDS`` gets its own
    batch. When ``segments`` is None, falls back to pure count-based batching.
    """
    if segments is None or len(segments) != total_segments:
        return [
            {
                "batch_id": i,
                "start":    i * batch_size,
                "count":    min(batch_size, total_segments - i * batch_size),
            }
            for i in range((total_segments + batch_size - 1) // batch_size)
        ]

    batches: List[dict] = []
    i = 0
    batch_id = 0
    while i < total_segments:
        start = i
        words = 0
        count = 0
        while i < total_segments and count < batch_size:
            seg_words = len(segments[i].get("text", "").split())
            if words + seg_words > MAX_BATCH_WORDS and count > 0:
                break
            words += seg_words
            count += 1
            i += 1
        batches.append({
            "batch_id": batch_id,
            "start":    start,
            "count":    max(count, 1),
        })
        batch_id += 1
    return batches


def build_prompt(prompt_template: str, batch: List[dict], glossary: str) -> str:
    """Build the Gemini prompt for a batch of segments.

    The template describes the format and rules; this function appends the
    numbered input blocks ``[1]\\ntext1\\n\\n[2]\\ntext2\\n...`` after the
    template body, mirroring FT's numbered-marker pattern.
    """
    blocks = [f"[{j+1}]\n{seg['text']}" for j, seg in enumerate(batch)]
    text_block = "\n\n".join(blocks)
    return prompt_template.format(
        glossary=glossary,
        batch_size=len(batch),
    ) + "\n\n" + text_block


def _strip_marker_prefix(text: str) -> str:
    """Remove a leading [N] marker that may have leaked into a fallback piece."""
    return re.sub(r"^\s*\[\d+\]\s*", "", text).strip()


PREAMBLE_RE = re.compile(
    r'^(here are|following|below|sure|certainly|of course|'
    r'here is|here\'s|below is|the following|'
    r'以下是|好的|當然|翻譯如下|這是)',
    re.IGNORECASE
)


CJK_RE = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uff00-\uffef]")


def is_valid_translation(text: Optional[str], stage: str) -> bool:
    """Reject translations that are empty, non-CJK garbage, or prompt/glossary bleed."""
    if not text:
        return False
    # Must contain at least one CJK character for translate/simplify/tailo stages
    if CJK_RE.search(text):
        return True
    return False


def parse_numbered_response(response: str, expected: int) -> List[Optional[str]]:
    """Parse a Gemini response that should contain [1] ... [2] ... markers.

    Returns a list of length ``expected``. Empty / missing slots are ``None``
    so the caller can flag them as must_fix rather than silently dropping
    a translation. If no numbered markers are detected at all the response
    is treated as unparseable and ``[None, None, ...]`` is returned — the
    caller should retry the Gemini call rather than accept a garbled
    fallback as a valid translation.
    """
    parts: List[Optional[str]] = [None] * expected

    numbered = re.findall(r"\[(\d+)\]\s*(.*?)(?=\[\d+\]|$)", response, re.DOTALL)
    if numbered and len(numbered) == expected:
        for marker, body in numbered:
            idx = int(marker) - 1
            if 0 <= idx < expected:
                cleaned = body.strip()
                if cleaned:
                    parts[idx] = cleaned
        return parts

    if not numbered:
        return parts

    first_marker = re.search(r"\n?\[1\]", response)
    clean_response = response[first_marker.end():] if first_marker else response

    fallback = [_strip_marker_prefix(p) for p in re.split(r"\n{2,}", clean_response)]
    fallback = [p for p in fallback if p]
    while fallback and PREAMBLE_RE.match(fallback[0]):
        fallback.pop(0)

    for marker, body in numbered:
        idx = int(marker) - 1
        if 0 <= idx < expected and not parts[idx]:
            cleaned = body.strip()
            if cleaned:
                parts[idx] = cleaned

    if len(fallback) >= expected:
        for k in range(expected):
            if not parts[k] and fallback[k]:
                parts[k] = fallback[k]
    elif len(fallback) > 0 and not any(parts):
        if len(fallback) == 1:
            parts[0] = fallback[0]
        else:
            for k, f in enumerate(fallback[:expected]):
                if not parts[k]:
                    parts[k] = f

    return parts


def process_batch(
    batch: List[dict],
    stage: str,
    glossary: str,
    max_retries: int = 3,
) -> List[Optional[str]]:
    """Translate one batch of segments via Gemini.

    First attempt uses the full prompt with all segments. On retry, only the
    untranslated segments are sent with a focused retry prompt — this avoids
    re-sending already-translated segments and lets the model focus on gaps.

    Returns a list of length len(batch). Empty slots are ``None``.
    """
    prompt_template = {
        "translate": TRANSLATE_PROMPT,
        "tailo":     TAILO_PROMPT,
    }[stage]
    retry_template = {
        "translate": TRANSLATE_RETRY_PROMPT,
        "tailo":     TAILO_RETRY_PROMPT,
    }[stage]
    prompt = build_prompt(prompt_template, batch, glossary)

    parts: List[Optional[str]] = [None] * len(batch)
    missing_idx: List[int] = list(range(len(batch)))  # indices into batch

    for attempt in range(max_retries + 1):
        expected = len(missing_idx) if attempt > 0 else len(batch)
        try:
            response = translate(prompt, job_type=JOB_TYPE[stage])
            new_parts = parse_numbered_response(response, expected=expected)
        except Exception as e:
            logger.warning(
                f"  batch {batch[0].get('index', '?')}: Gemini attempt "
                f"{attempt+1}/{max_retries+1} failed: {e}"
            )
            if attempt < max_retries:
                continue
            return parts

        if attempt == 0:
            # First attempt: map full-length response directly
            for k, p in enumerate(new_parts):
                if is_valid_translation(p, stage) and not parts[k]:
                    parts[k] = p
        else:
            # Retry: response corresponds to missing_idx order
            for offset, p in enumerate(new_parts):
                if is_valid_translation(p, stage):
                    orig_k = missing_idx[offset]
                    if not parts[orig_k]:
                        parts[orig_k] = p

        non_empty = sum(1 for p in parts if p)
        if non_empty == len(batch) or attempt == max_retries:
            if non_empty < len(batch):
                logger.warning(
                    f"  batch starting at {batch[0].get('index', '?')}: "
                    f"only {non_empty}/{len(batch)} parsed after "
                    f"{attempt+1} attempts"
                )
            return parts

        logger.info(
            f"  batch starting at {batch[0].get('index', '?')}: retrying, "
            f"got {non_empty}/{len(batch)}"
        )

        # Build a retry prompt with only the untranslated segments
        missing_idx = [k for k, p in enumerate(parts) if not p]
        missing_segs = [batch[k] for k in missing_idx]
        blocks = [f"[{j+1}]\n{seg['text']}" for j, seg in enumerate(missing_segs)]
        prompt = retry_template.format(
            glossary=glossary,
            batch_size=len(missing_segs),
            segments="\n\n".join(blocks),
        )

    return parts


def run_segment_pipeline(
    segments: List[dict],
    stage: str,
    terminology: Optional[Dict[str, str]] = None,
) -> Tuple[List[Optional[str]], dict]:
    """Process every segment in 10-segment batches, with per-batch checkpoint
    resume. Used by translate and tailo stages.

    On startup, lists all existing checkpoint files for this stage (from
    the GCS temp prefix) and loads them. Any missing / incomplete batches
    are re-translated.

    Returns:
        translations:  list of length len(segments); None means "missing"
        stats:          {n_batches, n_skipped, n_translated, n_partial}
    """
    if terminology is None:
        terminology = load_terminology()
    glossary = format_glossary_for_prompt(terminology)
    batches  = build_batches(len(segments), segments=segments)
    total    = len(segments)
    total_batches = len(batches)
    existing_ids = set(list_stage_checkpoints(stage))

    write_temp_json(BATCHES_PLAN_PATH, {
        "stage":          stage,
        "total_segments": total,
        "batch_size":     BATCH_SIZE,
        "total_batches":  total_batches,
        "batches":        batches,
    })
    logger.info(
        f"run_segment_pipeline: stage={stage}, segments={total}, "
        f"batches={total_batches}, existing_checkpoints={len(existing_ids)}, "
        f"terms={len(terminology)}"
    )

    translations: List[Optional[str]] = [None] * total
    n_skipped = 0
    n_translated = 0
    n_partial = 0

    for batch in batches:
        batch_id = batch["batch_id"]
        start    = batch["start"]
        count    = batch["count"]
        current_batch = batch_id + 1

        if batch_id in existing_ids:
            ckpt = load_stage_checkpoint(stage, batch_id)
            if ckpt is not None:
                ckpt_start = ckpt.get("start")
                ckpt_count = ckpt.get("count")
                if ckpt_start == start and ckpt_count == count:
                    ckpt_parts = ckpt.get("translations", [])
                    empty_count = sum(1 for t in ckpt_parts if not t)
                    if empty_count == 0:
                        for offset, t in enumerate(ckpt_parts):
                            translations[start + offset] = t
                        n_skipped += 1
                        logger.info(
                            f"  batch {current_batch}/{total_batches}: "
                            f"checkpoint exists ({count}/{count}) — skipped"
                        )
                        continue
                    logger.info(
                        f"  batch {current_batch}/{total_batches}: "
                        f"checkpoint has {empty_count}/{count} empty — re-running"
                    )
                else:
                    logger.warning(
                        f"  batch {current_batch}/{total_batches}: stale checkpoint "
                        f"(expected {count}@{start}, got {ckpt_count}@{ckpt_start}) — ignoring"
                    )

        logger.info(
            f"  batch {current_batch}/{total_batches}: "
            f"translating segments {start}–{start+count-1} "
            f"({total} total)"
        )
        batch_segs = segments[start:start + count]
        parts = process_batch(batch_segs, stage, glossary)

        save_stage_checkpoint(stage, batch_id, {
            "batch_id":     batch_id,
            "start":        start,
            "count":        count,
            "translations": parts,
        })

        non_empty = sum(1 for p in parts if p)
        if non_empty == count:
            n_translated += 1
        elif non_empty > 0:
            n_translated += 1
            n_partial += 1
        for offset, p in enumerate(parts):
            translations[start + offset] = p

    return translations, {
        "n_batches":     len(batches),
        "n_skipped":     n_skipped,
        "n_translated":  n_translated,
        "n_partial":     n_partial,
    }


# ── Simplify (whole-chapter pipeline) ────────────────────────────────────────

def build_chapter_text(translated_entries: List[dict], seg_start: int, seg_end: int) -> str:
    """Concatenate translated segments for a chapter into a coherent text."""
    paras = []
    for i in range(seg_start, seg_end):
        text = translated_entries[i].get("translated", "") if i < len(translated_entries) else ""
        text = text.strip()
        if text:
            paras.append(text)
    return "\n\n".join(paras)


def process_simplify_chapter(chapter_text: str, chapter_index: int, title: str) -> str:
    """Send one chapter's text to Gemini for whole-chapter simplification.
    Retries once if the result is empty or non-CJK.
    """
    prompt = SIMPLIFY_CHAPTER_PROMPT.format(chapter_text=chapter_text)

    for attempt in range(2):
        try:
            result = translate(prompt, job_type=JOB_TYPE["simplify"], max_tokens=65536)
            if result and CJK_RE.search(result):
                return result
            logger.warning(
                f"  chapter {chapter_index} ({title!r}): attempt {attempt + 1} "
                f"returned non-CJK or empty — retrying"
            )
        except Exception as e:
            logger.warning(
                f"  chapter {chapter_index} ({title!r}): Gemini attempt "
                f"{attempt + 1} failed: {e}"
            )
    return ""


def run_simplify_pipeline(
    translated_entries: List[dict],
    chapters: List[dict],
) -> Tuple[List[dict], dict]:
    """Simplify every chapter as a whole via Gemini.

    Loads translated.json (per-segment) + chapters.json, groups by chapter,
    calls Gemini once per chapter, writes simplify_chapters.json, then splits
    into per-segment simplify.json.

    Returns:
        chapter_entries: list of ``{chapter_index, title, text}``
        stats: ``{n_chapters, n_simplified}``
    """
    chapter_entries: List[dict] = []
    n_chapters = len(chapters)
    n_simplified = 0

    for ch in chapters:
        ci = ch.get("index", 0)
        title = ch.get("title", f"Chapter {ci + 1}")
        seg_start = ch.get("segment_start", 0)
        seg_end = ch.get("segment_end", seg_start)

        chapter_text = build_chapter_text(translated_entries, seg_start, seg_end)
        char_count = len(chapter_text)
        logger.info(
            f"  chapter {ci} ({title!r}): {seg_end - seg_start} segments, "
            f"{char_count:,} chars"
        )

        if not chapter_text.strip():
            logger.warning(f"  chapter {ci} ({title!r}): empty — skipping")
            chapter_entries.append({
                "chapter_index": ci,
                "title": title,
                "text": "",
            })
            continue

        result = process_simplify_chapter(chapter_text, ci, title)
        chapter_entries.append({
            "chapter_index": ci,
            "title": title,
            "text": result,
        })
        if result:
            n_simplified += 1

    write_temp_json(SIMPLIFIED_CHAPTERS_OUTPUT_PATH, chapter_entries)
    logger.info(
        f"Wrote {SIMPLIFIED_CHAPTERS_OUTPUT_PATH} ({len(chapter_entries)} chapters, "
        f"{n_simplified}/{n_chapters} simplified)"
    )

    return chapter_entries, {
        "n_chapters":   n_chapters,
        "n_simplified": n_simplified,
    }


def split_simplified_chapters(chapter_entries: List[dict]) -> List[dict]:
    """Split whole-chapter simplify output into per-segment entries.

    Each chapter is split on double-newlines (``\\n\\n``). The resulting
    segments are assigned sequential indices independent of the original
    segmentation. This output is consumed by gt_tailo and gt_deliver.
    """
    out: List[dict] = []
    seg_index = 0
    for ch in chapter_entries:
        ci = ch.get("chapter_index", 0)
        title = ch.get("title", f"Chapter {ci + 1}")
        text = ch.get("text", "")
        if not text.strip():
            out.append({
                "index":          seg_index,
                "source":         "",
                "translated":     "",
                "chapter_index":  ci,
                "chapter_title":  title,
            })
            seg_index += 1
            continue

        # Split on double newlines first, then single newlines if needed
        paras = re.split(r"\n\n+", text.strip())
        if len(paras) <= 1:
            paras = text.strip().split("\n")
        for p in paras:
            p = p.strip()
            if not p:
                continue
            out.append({
                "index":          seg_index,
                "source":         "",
                "translated":     p,
                "chapter_index":  ci,
                "chapter_title":  title,
            })
            seg_index += 1

    return out


# ── Consolidated output writer ──────────────────────────────────────────────

def write_consolidated_output(
    segments: List[dict],
    translations: List[Optional[str]],
    stage: str,
) -> List[dict]:
    """Write translated.json / simplified.json / tailo.json — the consolidated
    per-segment output that gt_deliver consumes.

    Each entry is ``{index, source, translated, chapter_index, chapter_title}``.
    ``source`` is the English original (always the segment's text for
    translate stage; from the previous stage's ``source`` field for
    simplify / tailo).
    """
    if stage == "translate":
        source_texts = [s.get("text", "") for s in segments]
    else:
        source_texts = [s.get("source", "") for s in segments]

    out: List[dict] = []
    for i, seg in enumerate(segments):
        out.append({
            "index":         i,
            "source":        source_texts[i],
            "translated":    translations[i] or "",
            "chapter_index": seg.get("chapter_index", 0),
            "chapter_title": seg.get("chapter_title", ""),
        })

    write_temp_json(OUTPUT_PATHS[stage], out)
    logger.info(f"Wrote {OUTPUT_PATHS[stage]} ({len(out)} segments)")
    return out


# ── Stage entry-point helper ────────────────────────────────────────────────

def run_stage(stage: str) -> None:
    """Common entry point for the three gt_* jobs.

    Loads the right input for the stage, runs the appropriate pipeline
    (segment-based for translate/tailo, chapter-based for simplify), and
    writes the consolidated output. Updates pipeline_jobs with status.
    """
    from shared.config import cfg

    if stage not in {"translate", "simplify", "tailo"}:
        raise ValueError(
            f"stage must be one of translate|simplify|tailo, got: {stage!r}"
        )

    job_type = JOB_TYPE[stage]
    logger.info(f"=== gt_{stage} START — order: {cfg.ORDER_ID} ===")
    update_job_status(job_type, "running")

    try:
        if stage == "simplify":
            translated_entries = load_input_segments("simplify")
            if not translated_entries:
                raise ValueError("No translated entries found for simplify")
            logger.info(
                f"Loaded {len(translated_entries)} translated segments"
            )
            chapters = read_temp_json("source/chapters.json")
            if not isinstance(chapters, list):
                raise ValueError("chapters.json must be a list of chapter dicts")
            logger.info(f"Loaded {len(chapters)} chapters from source/chapters.json")

            chapter_entries, stats = run_simplify_pipeline(
                translated_entries, chapters,
            )

            per_seg = split_simplified_chapters(chapter_entries)
            write_temp_json(OUTPUT_PATHS["simplify"], per_seg)
            logger.info(
                f"Wrote {OUTPUT_PATHS['simplify']} "
                f"({len(per_seg)} per-segment entries from "
                f"{stats['n_chapters']} chapters, "
                f"{sum(1 for e in per_seg if e.get('translated'))} non-empty)"
            )

            update_order_field("status", "processing")
            update_job_status(job_type, "success", qa_result={
                "stage":          stage,
                "n_chapters":     stats["n_chapters"],
                "n_simplified":   stats["n_simplified"],
                "n_segments_out": len(per_seg),
                **stats,
            })
            notify_stage(job_type)
            logger.info(
                f"=== gt_{stage} DONE — "
                f"{stats['n_simplified']}/{stats['n_chapters']} "
                f"chapters simplified, "
                f"{len(per_seg)} output segments ==="
            )
        else:
            segments = load_input_segments(stage)
            if not segments:
                raise ValueError(f"No input segments found for stage={stage}")
            logger.info(f"Loaded {len(segments)} input segments for stage={stage}")

            translations, stats = run_segment_pipeline(segments, stage)
            write_consolidated_output(segments, translations, stage)

            n_done = sum(1 for t in translations if t)
            n_missing = sum(1 for t in translations if not t)

            update_order_field("status", "processing")
            update_job_status(job_type, "success", qa_result={
                "stage":          stage,
                "num_segments":   len(segments),
                "num_translated": n_done,
                "num_missing":    n_missing,
                **stats,
            })
            notify_stage(job_type)
            logger.info(
                f"=== gt_{stage} DONE — {n_done}/{len(segments)} translated, "
                f"{n_missing} missing, {stats.get('n_skipped', 0)} resumed ==="
            )

    except Exception as e:
        logger.error(f"gt_{stage} failed: {e}", exc_info=True)
        update_job_status(job_type, "failed", error_message=str(e)[:500])
        raise
