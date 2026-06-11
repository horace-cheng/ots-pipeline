"""
test_chapter_simplify.py — Experimental prototype: simplify a whole chapter
as a single Gemini call (no segment-by-segment batching).

Usage:
  python gt_simplify/test_chapter_simplify.py <order_id> [chapter_index]

Downloads translated.json + source/chapters.json from GCS, combines the
segments for the given chapter into one coherent text, sends it to Gemini
with a story-oriented simplify prompt, and prints the result.

Example:
  python gt_simplify/test_chapter_simplify.py d6546a94-0fe2-41b3-9d71-19050985b55a 0
"""
import json, logging, os, sys, tempfile, textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

from shared.gemini import translate
from shared.storage import get_client as get_gcs_client, cfg

GCS_TEMP_BUCKET = "ots-translation-pipeline-temp-dev"


def load_order_data(order_id: str) -> tuple[list, list[dict]]:
    """Download translated.json + chapters.json from GCS."""
    client = get_gcs_client()
    bucket = client.bucket(GCS_TEMP_BUCKET)

    def _read_json(blob_name: str) -> list:
        blob = bucket.blob(f"pipeline/{order_id}/{blob_name}")
        return json.loads(blob.download_as_bytes())

    chapters = _read_json("source/chapters.json")
    translated = _read_json("translated.json")
    return chapters, translated


def build_chapter_text(translated: list, seg_start: int, seg_end: int) -> str:
    """Concatenate translated segments for a chapter into a coherent text."""
    paras = []
    for i in range(seg_start, seg_end):
        text = translated[i].get("translated", "") if i < len(translated) else ""
        text = text.strip()
        if text:
            paras.append(text)
    return "\n\n".join(paras)


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


def main():
    order_id = sys.argv[1] if len(sys.argv) > 1 else "d6546a94-0fe2-41b3-9d71-19050985b55a"
    ch_index = int(sys.argv[2]) if len(sys.argv) > 2 else 0

    print(f"Loading order {order_id}, chapter {ch_index}...")
    chapters, translated = load_order_data(order_id)

    if ch_index >= len(chapters):
        print(f"Error: chapter index {ch_index} out of range (max {len(chapters)-1})")
        return

    ch = chapters[ch_index]
    ch_title = ch.get("title", f"Chapter {ch_index + 1}")
    seg_start = ch.get("segment_start", 0)
    seg_end = ch.get("segment_end", seg_start)
    print(f"\nChapter {ch_index}: {ch_title}")
    print(f"  Segments: {seg_start} to {seg_end} ({seg_end - seg_start} segments)")

    chapter_text = build_chapter_text(translated, seg_start, seg_end)
    char_count = len(chapter_text)
    print(f"  Character count: {char_count:,}")

    # Truncate if absurdly long for Gemini context
    MAX_CHARS = 32_000
    if char_count > MAX_CHARS:
        print(f"  Truncating to {MAX_CHARS:,} chars for testing...")
        chapter_text = chapter_text[:MAX_CHARS] + "\n\n[...truncated]"

    prompt = SIMPLIFY_CHAPTER_PROMPT.format(chapter_text=chapter_text)

    print(f"\n=== Sending to Gemini (chapter: {ch_title}) ===")
    print(f"  Prompt length: {len(prompt):,} chars")
    print()

    result = translate(prompt, max_tokens=65536)

    print(f"\n{'='*60}")
    print(f"SIMPLIFIED OUTPUT ({ch_title})")
    print(f"{'='*60}")
    print(result)
    print(f"\n{'='*60}")
    print(f"Output chars: {len(result):,}")


if __name__ == "__main__":
    main()
