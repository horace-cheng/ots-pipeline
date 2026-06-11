"""
gt_chapter_splitter/main.py — Cloud Run Job (Gutenberg Track)

Reads ``source/full_text.txt`` (produced by ``gt_fetcher``), uses Gemini
to identify chapter boundaries, then uses Python ``str.find`` to compute
exact char offsets. Writes the same ``chapters.json`` + ``segments.json``
schema that the downstream stages (``gt_translate``, ``gt_simplify``,
``gt_tailo``, ``gt_deliver``) already consume.

Inputs:
  - GCS: source/full_text.txt  (from gt_fetcher, license already stripped)
  - GCS: metadata.json          (from gt_fetcher, book_id/title/authors/language)

Outputs:
  - GCS: source/chapters.json   — chapter index with char_start/char_end
  - GCS: segments.json          — paragraph-level segments with chapter linkage
  - GCS: metadata.json          — updated with num_chapters/num_segments/word_count

If the LLM call fails or returns unusable output, falls back to the
regex-based ``split_text_structured`` from ``gt_fetcher`` (the previous
behavior). The fallback is silent so the change is strictly additive.
"""
import json
import logging
import re
import sys
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import cfg
from shared.db import update_job_status
from shared.gemini import translate
from shared.storage import (
    read_temp_text, read_temp_json,
    get_client as get_gcs_client,
)
from gt_fetcher.main import (
    _strip_gutenberg_boilerplate,
    split_text_structured,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gt_chapter_splitter")


COVERAGE_MIN = 0.50

SPLITTER_PROMPT = """You are analyzing a book to identify its chapter boundaries.

Read the following book text and identify the chapters. Skip frontmatter (table of contents, preface, dedication, editor's introduction, acknowledgments) and backmatter (index, colophon, appendices, advertisements for other books). Only return the actual story content as chapters.

Return a JSON object with a "chapters" array. For each chapter, provide:
- "title": the chapter heading as it appears in the book (e.g. "CHAPTER I", "I.", "Chapter 1", "Epilogue", "PART TWO", etc.)
- "first_words": the first 8-15 words that appear immediately after the chapter heading, preserving capitalization and punctuation. Include any epigraphs, quotations, or poetry that begin the chapter. Must be an EXACT substring of the chapter's opening.

Output ONLY the JSON object. Example:
{{
  "chapters": [
    {{"title": "I. The Time Traveller Explains", "first_words": "The Time Traveller (for so it will be convenient to speak of him)"}},
    {{"title": "II. The Eloi", "first_words": "We pressed on past the white pyramidal ruins"}}
  ]
}}

Book text:
__BOOK_TEXT_PLACEHOLDER__
"""


# ── Path helpers ───────────────────────────────────────────────────────────

def _source_path(filename: str) -> str:
    return f"pipeline/{cfg.ORDER_ID}/{filename}"


# ── Paragraph segmentation (was in gt_fetcher, moved here) ────────────────

def split_paragraphs(text: str) -> List[str]:
    """Split text into paragraphs. A segment IS a paragraph — no length-based
    sub-splitting.

    Strategy:
      1. Normalize CRLF → LF (so cross-platform text splits correctly)
      2. Primary split on blank lines (`\\n{2,}`)
      3. If only 1 paragraph, fall back to sentence split on
         CJK/Latin sentence boundaries — that's likely a one-block file
         that needs finer splitting
      4. For the primary-split case, merge only truly tiny fragments
         (≤ 2 chars) into the previous paragraph — keeps short stand-alone
         lines like "Hi." or "OK." intact

    Never discards text — paragraph boundaries are preserved exactly.
    """
    if not text or not text.strip():
        return []

    text = text.replace("\r\n", "\n").replace("\r", "\n")

    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text)]
    paragraphs = [p for p in paragraphs if p]

    if len(paragraphs) < 2:
        # One block with no blank lines — split on sentence boundaries so
        # we still get multiple segments out.
        paragraphs = [p.strip() for p in re.split(r"(?<=[。.!?！？\n])\s*", text) if p.strip()]
    else:
        # Merge only truly tiny fragments (≤ 2 chars) into the previous paragraph.
        # Threshold is intentionally tight so legitimate short paragraphs like
        # "Hi." or "OK." stay as their own segment.
        merged: List[str] = []
        for para in paragraphs:
            if merged and len(para) <= 2:
                merged[-1] = merged[-1] + " " + para
            else:
                merged.append(para)
        paragraphs = merged

    return paragraphs


def count_words(text: str) -> int:
    return len(re.findall(r'\b\w+\b', text))


# ── Build segments + chapter index from a chapter-list + full text ───────

def build_segments_and_chapters(
    full_text: str,
    chapters_meta: List[dict],
) -> tuple:
    """Given the full text and a list of chapter metadata
    ``{index, title, char_start, char_end}``, build the
    ``segments.json`` + ``chapters.json`` structures consumed by
    downstream stages.

    The downstream code only uses ``segment_start`` / ``segment_end``
    (indices into the flat ``segments`` list) and ``chapter_index`` /
    ``chapter_title`` on each segment. ``char_start`` / ``char_end`` are
    preserved for human inspection but not load-bearing.
    """
    segments: List[dict] = []
    chapter_index: List[dict] = []
    seg_idx = 0

    for ch_meta in chapters_meta:
        ch_text = full_text[ch_meta["char_start"]:ch_meta["char_end"]].strip()
        ch_seg_start = seg_idx

        ch_segments = split_paragraphs(ch_text)
        for seg_text in ch_segments:
            segments.append({
                "index":         seg_idx,
                "text":          seg_text,
                "char_count":    len(seg_text),
                "chapter_index": ch_meta["index"],
                "chapter_title": ch_meta["title"],
            })
            seg_idx += 1

        chapter_index.append({
            "index":         ch_meta["index"],
            "title":         ch_meta["title"],
            "char_start":    ch_meta["char_start"],
            "char_end":      ch_meta["char_end"],
            "char_count":    ch_meta["char_end"] - ch_meta["char_start"],
            "segment_start": ch_seg_start,
            "segment_end":   seg_idx,
        })

    return segments, chapter_index


# ── LLM chapter detection ─────────────────────────────────────────────────

def call_llm_for_chapters(full_text: str) -> List[dict]:
    """Call Gemini to get chapter metadata. Returns list of
    ``{title, first_words}`` dicts."""
    prompt = SPLITTER_PROMPT.replace("__BOOK_TEXT_PLACEHOLDER__", full_text)
    raw = translate(
        prompt,
        max_tokens=8192,
        response_mime_type="application/json",
        job_type="gt_chapter_splitter",
    )
    return parse_llm_response(raw)


def parse_llm_response(raw: str) -> List[dict]:
    """Parse Gemini's JSON response into a list of ``{title, first_words}`` dicts.

    Accepts both ``{"chapters": [...]}`` (preferred, JSON mode) and bare
    ``[...]`` arrays (defensive, in case JSON mode is bypassed).
    Strips markdown fences if present.
    Returns an empty list on parse failure.
    """
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "chapters" in data:
            chapters = data["chapters"]
        elif isinstance(data, list):
            chapters = data
        else:
            raise ValueError(f"Expected dict with 'chapters' or list, got {type(data)}")
        out = []
        for c in chapters:
            if not isinstance(c, dict):
                continue
            title = str(c.get("title", "")).strip()
            first_words = str(c.get("first_words", "")).strip()
            if title and first_words:
                out.append({"title": title, "first_words": first_words})
        return out
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Failed to parse LLM response as JSON: {e}")
        logger.debug(f"Raw (first 500 chars): {raw[:500]}")
        return []


PG_TITLE_LINE = re.compile(
    r'^The Project Gutenberg eBook of .+, by .+$',
    re.IGNORECASE,
)


def _strip_pg_title_header(text: str) -> str:
    """Strip the "The Project Gutenberg eBook of <title>, by <author>" line
    and any leading blank lines that follow it, when the standard marker-based
    stripper didn't fire (e.g. EPUB-derived text that lacks START/END markers).
    """
    return PG_TITLE_LINE.sub('', text, count=1).lstrip('\n\r')


def locate_chapters_in_text(text: str, llm_chapters: List[dict]) -> Optional[List[dict]]:
    """Locate each LLM-returned chapter in the text via ``str.find``.

    Returns a list of ``{index, title, char_start, char_end}`` dicts, or
    ``None`` if validation fails (0 chapters located, low coverage).
    """
    if not llm_chapters:
        logger.warning("LLM returned 0 chapters — falling back to regex")
        return None

    text_len = len(text)
    located: List[dict] = []

    for i, ch in enumerate(llm_chapters):
        first_words = ch["first_words"]
        char_start = text.find(first_words)
        if char_start == -1:
            logger.warning(
                f"Chapter {i} ({ch['title']!r}): first_words not found in text, "
                f"skipping. first_words={first_words[:60]!r}..."
            )
            continue
        located.append({
            "title":       ch["title"],
            "first_words": first_words,
            "char_start":  char_start,
        })

    if not located:
        logger.warning("No chapters could be located in the text — falling back to regex")
        return None

    final: List[dict] = []
    for i, ch in enumerate(located):
        char_end = located[i + 1]["char_start"] if i + 1 < len(located) else text_len
        if char_end <= ch["char_start"]:
            logger.warning(
                f"Chapter {i} ({ch['title']!r}): char_end <= char_start, skipping"
            )
            continue
        final.append({
            "index":      len(final),
            "title":      ch["title"],
            "char_start": ch["char_start"],
            "char_end":   char_end,
        })

    if not final:
        logger.warning("No valid chapters after char_end assignment — falling back to regex")
        return None

    covered = sum(c["char_end"] - c["char_start"] for c in final)
    coverage = covered / text_len if text_len else 0
    if coverage < COVERAGE_MIN:
        logger.warning(
            f"LLM chapter coverage is only {coverage:.1%} "
            f"({covered}/{text_len} chars) — falling back to regex"
        )
        return None

    logger.info(
        f"LLM chapter detection: {len(final)} chapters, "
        f"coverage {coverage:.1%}"
    )
    return final


def chapters_via_llm(full_text: str) -> Optional[List[dict]]:
    """Try LLM-based chapter detection. Returns list of chapter dicts with
    ``char_start/char_end``, or ``None`` if the LLM path failed.
    """
    try:
        llm_chapters = call_llm_for_chapters(full_text)
    except Exception as e:
        logger.warning(f"LLM call failed: {e} — falling back to regex")
        return None
    return locate_chapters_in_text(full_text, llm_chapters)


def chapters_via_regex(full_text: str) -> List[dict]:
    """Regex-based chapter detection. Mimics the old ``save_book_to_gcs``
    logic exactly so existing orders continue to produce identical output.

    Returns the same ``{index, title, char_start, char_end}`` shape as the
    LLM path.
    """
    chunks = split_text_structured(full_text)
    final: List[dict] = []
    char_pos = 0
    for i, c in enumerate(chunks):
        ch_text = c.strip()
        ch_start = char_pos
        ch_end = char_pos + len(ch_text)
        final.append({
            "index":      i,
            "title":      f"Part {i+1}",
            "char_start": ch_start,
            "char_end":   ch_end,
        })
        char_pos = ch_end + 2  # +2 for the "\n\n" join separator
    return final


def chapters_via_regex_filtered(full_text: str) -> List[dict]:
    """Split text via regex, then filter out title-only chunks (Roman numerals,
    "CHAPTER X") and extract proper headings from content chunks.

    Produces ``{index, title, char_start, char_end}`` dicts compatible with
    ``build_segments_and_chapters``.
    """
    chunks = split_text_structured(full_text)
    TITLE_ONLY = re.compile(r'^(CHAPTER\s+\w+|[IVXLCDM]+)[.\s]*$', re.IGNORECASE)

    # Filter: skip title-only chunks (they come from the regex capturing group)
    # and leading boilerplate
    content: List[str] = []
    for c in chunks:
        text = c.strip()
        if not text:
            continue
        if TITLE_ONLY.match(text):
            continue
        content.append(text)
    if content and 'gutenberg' in content[0].lower():
        content = content[1:]

    if not content:
        return []

    final: List[dict] = []
    for i, ch_text in enumerate(content):
        text = ch_text.strip()
        start = full_text.find(text[:60])
        if start == -1:
            # Fallback: use previous chapter's end + 1
            start = final[-1]["char_end"] + 1 if final else 0
        end = start + len(text)

        first_line = text.split("\n")[0].strip()
        title = re.sub(r"^[IVXLCDM]+\.\s*", "", first_line).strip()
        if not title:
            title = f"Chapter {i + 1}"

        final.append({
            "index":      len(final),
            "title":      title,
            "char_start": start,
            "char_end":   end,
        })

    return final


# ── GCS writer ────────────────────────────────────────────────────────────

def write_chapters_and_segments(
    segments: List[dict],
    chapter_index: List[dict],
    counts: dict,
) -> None:
    """Write ``source/chapters.json`` and ``segments.json``; merge
    ``num_chapters/num_segments/word_count`` into the existing
    ``metadata.json`` written by ``gt_fetcher``."""
    client = get_gcs_client()
    bucket = client.bucket(cfg.BUCKET_TEMP)

    bucket.blob(_source_path("source/chapters.json")).upload_from_string(
        json.dumps(chapter_index, ensure_ascii=False, indent=2),
        content_type="application/json",
    )
    bucket.blob(_source_path("segments.json")).upload_from_string(
        json.dumps(segments, ensure_ascii=False, indent=2),
        content_type="application/json",
    )

    try:
        existing = read_temp_json("metadata.json")
        if not isinstance(existing, dict):
            existing = {}
    except Exception:
        existing = {}

    enriched = {**existing, **counts}
    bucket.blob(_source_path("metadata.json")).upload_from_string(
        json.dumps(enriched, ensure_ascii=False, indent=2),
        content_type="application/json",
    )
    logger.info(
        f"Wrote {len(chapter_index)} chapters + {len(segments)} segments; "
        f"merged counts into metadata.json"
    )


# ── Main entry point ──────────────────────────────────────────────────────

def run():
    logger.info(f"=== gt_chapter_splitter START — order: {cfg.ORDER_ID} ===")
    update_job_status("gt_chapter_splitter", "running")

    try:
        full_text = read_temp_text("source/full_text.txt")
        if not full_text:
            raise ValueError(
                "source/full_text.txt is empty — gt_fetcher must run first"
            )
        logger.info(f"Read {len(full_text)} chars from source/full_text.txt")

        # Defensive re-strip in case the source wasn't already cleaned
        full_text = _strip_gutenberg_boilerplate(full_text)
        # EPUB-derived text lacks START/END markers, so the marker-based
        # stripper above is a no-op.  Strip the PG title header heuristically.
        full_text = _strip_pg_title_header(full_text)

        chapter_meta = chapters_via_llm(full_text)
        regex_filtered = chapters_via_regex_filtered(full_text)
        if chapter_meta is None:
            logger.info("Using regex-based chapter detection (LLM returned None)")
            chapter_meta = regex_filtered or chapters_via_regex(full_text)
            detection_method = "regex"
        elif regex_filtered and len(regex_filtered) > len(chapter_meta):
            logger.info(
                f"Using regex-based chapter detection "
                f"(regex={len(regex_filtered)} > llm={len(chapter_meta)} chapters)"
            )
            chapter_meta = regex_filtered
            detection_method = "regex"
        else:
            detection_method = "llm"

        segments, chapter_index = build_segments_and_chapters(full_text, chapter_meta)

        if not segments:
            raise ValueError(
                "No segments produced — text is empty after chapter splitting"
            )

        num_chapters = len(chapter_index)
        num_segments = len(segments)
        word_count   = sum(count_words(s["text"]) for s in segments)

        write_chapters_and_segments(segments, chapter_index, {
            "num_chapters":  num_chapters,
            "num_segments":  num_segments,
            "word_count":    word_count,
        })

        update_job_status("gt_chapter_splitter", "success", qa_result={
            "num_chapters":     num_chapters,
            "num_segments":     num_segments,
            "word_count":       word_count,
            "detection_method": detection_method,
        })
        logger.info(
            f"=== gt_chapter_splitter DONE — {num_chapters} chapters, "
            f"{num_segments} segments, {word_count} words, "
            f"method={detection_method} ==="
        )

    except Exception as e:
        logger.error(f"gt_chapter_splitter failed: {e}", exc_info=True)
        update_job_status("gt_chapter_splitter", "failed", error_message=str(e)[:500])
        raise


if __name__ == "__main__":
    run()
