"""
gt_extract_terms/main.py — Cloud Run Job (Gutenberg Track)

Reads the source produced by gt_fetcher, uses Gemini to identify the
top entities (characters, places, objects) and produce a
{english_term: chinese_term} mapping saved to terminology.json.

v2 storage layout (preferred — see change_logs/2026-06-05_gutenberg_v2_*):
  - GCS: gs://{BUCKET_TEMP}/pipeline/{ORDER_ID}/source/full_text.txt
  - GCS: gs://{BUCKET_TEMP}/pipeline/{ORDER_ID}/segments.json   (fallback)

v1 storage layout (legacy — only kept for un-migrated orders):
  - GCS: gs://{BUCKET_TEMP}/pipeline/{ORDER_ID}/source/chunk_*.txt

Writes:
  - GCS: gs://{BUCKET_TEMP}/pipeline/{ORDER_ID}/terminology.json
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import cfg
from shared.db import update_job_status
from shared.storage import (
    read_temp_text,
    read_temp_json,
    write_temp_json,
    get_client as get_gcs_client,
    temp_blob_exists,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gt_extract_terms")

# Terminology extraction prompt (English-only, all target languages)
EXTRACT_PROMPT = """You are a literary translator preparing to translate an English novel into Traditional Chinese.

Read the following text excerpt and identify the top 50 most important named entities —
characters, places, organizations, and unique objects that recur throughout the book
and must be translated consistently.

Output a JSON object mapping each English term to its Traditional Chinese translation.
Use the conventions below:
- Character names: use widely accepted Chinese transliterations (e.g., "Elizabeth Bennet" → "伊麗莎白·班奈特")
- Place names: translate or transliterate based on the era (e.g., "Longbourn" → "浪博恩")
- Titles (Mr., Mrs., Dr.): use Chinese equivalents (先生, 女士, 醫生)
- Use the same Chinese term for the same English term — DO NOT use different Chinese
  translations for the same character

Output ONLY valid JSON, no preamble, no markdown fences. Example:
{{"Mr. Darcy": "達西先生", "Elizabeth Bennet": "伊麗莎白·班奈特", "Longbourn": "浪博恩"}}

Text excerpt:
{source_text}

JSON terminology mapping:"""


def read_source_sample_v2(max_chars: int = 60_000) -> str:
    """v2 source reader.

    Prefers ``source/full_text.txt`` (the canonical v2 artifact). Falls back
    to a sample of ``segments.json`` if full_text is missing — this can
    happen if gt_fetcher was killed mid-write. The sample is taken from
    the first N segments up to ``max_chars`` to mirror the v1
    read_chunks_concat behaviour.
    """
    if temp_blob_exists("source/full_text.txt"):
        logger.info("Reading source from v2 source/full_text.txt")
        full = read_temp_text("source/full_text.txt")
        return full[:max_chars]

    if temp_blob_exists("segments.json"):
        logger.info("Reading source from v2 segments.json (full_text.txt missing)")
        segs = read_temp_json("segments.json")
        if not isinstance(segs, list):
            return ""
        parts: list[str] = []
        total = 0
        for s in segs:
            text = s.get("text", "") if isinstance(s, dict) else str(s)
            parts.append(text)
            total += len(text)
            if total >= max_chars:
                break
        return "\n\n".join(parts)

    return ""


def list_source_chunks() -> list[str]:
    """Legacy v1 source reader.

    Returns the filenames of all source chunks (``source/chunk_*.txt``)
    in GCS. Kept for backward compatibility with orders created before
    the v2 segment-based rewrite.
    """
    client = get_gcs_client()
    bucket = client.bucket(cfg.BUCKET_TEMP)
    prefix  = f"pipeline/{cfg.ORDER_ID}/source/chunk_"
    blobs   = list(bucket.list_blobs(prefix=prefix))
    files   = [b.name.split("pipeline/" + cfg.ORDER_ID + "/", 1)[-1]
               for b in blobs if b.name.endswith(".txt")]
    files.sort()
    return files


def read_chunks_concat(max_chars: int = 60_000) -> str:
    """Legacy v1 reader: concatenate the first N source chunks."""
    files = list_source_chunks()
    parts: list[str] = []
    total = 0
    for fname in files:
        content = read_temp_text(f"source/{fname.split('/')[-1]}")
        parts.append(content)
        total += len(content)
        if total >= max_chars:
            break
    return "\n\n".join(parts)


def parse_terminology_response(raw: str) -> dict:
    """Parse Gemini's JSON response. Strips markdown fences if present."""
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        lines = [ln for ln in lines if not ln.strip().startswith("```")]
        text = "\n".join(lines)
    try:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("Expected a JSON object")
        return {str(k): str(v) for k, v in data.items()}
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Failed to parse terminology response as JSON: {e}")
        logger.debug(f"Raw response (first 500 chars): {raw[:500]}")
        return {}


def run():
    logger.info(f"=== gt_extract_terms START — order: {cfg.ORDER_ID} ===")
    update_job_status("gt_extract_terms", "running")

    try:
        from shared.gemini import translate

        sample = read_source_sample_v2()
        source_layout = "v2"
        if not sample:
            logger.info("No v2 source artifacts found — falling back to v1 chunk_*.txt")
            sample = read_chunks_concat()
            source_layout = "v1"

        if not sample:
            raise ValueError(
                "No source content found — gt_fetcher must run first. "
                "Expected one of: source/full_text.txt, segments.json, source/chunk_*.txt"
            )

        logger.info(f"Source layout={source_layout}, sample length={len(sample)} chars")

        prompt = EXTRACT_PROMPT.format(source_text=sample)
        raw = translate(prompt, job_type="gt_extract_terms")

        terminology = parse_terminology_response(raw)
        if not terminology:
            logger.warning("Empty terminology returned — translations will be less consistent")
            terminology = {}

        write_temp_json("terminology.json", terminology)
        logger.info(f"Saved terminology.json with {len(terminology)} entries")

        update_job_status("gt_extract_terms", "success", qa_result={
            "num_terms": len(terminology),
            "source_layout": source_layout,
        })
        logger.info(f"=== gt_extract_terms DONE — {len(terminology)} terms ===")

    except Exception as e:
        logger.error(f"gt_extract_terms failed: {e}", exc_info=True)
        update_job_status("gt_extract_terms", "failed", error_message=str(e)[:500])
        raise


if __name__ == "__main__":
    run()
