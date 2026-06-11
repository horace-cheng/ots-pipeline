"""
gt_translate/main.py — Cloud Run Job (Gutenberg Track, translate stage)

Reads `segments.json` (English source paragraphs produced by gt_fetcher) and
generates `translated.json` — Traditional Chinese translation per segment.

Batching: 10 segments per Gemini call (segment-based, mirrors the FT design).
Output: per-segment rows consumed by gt_simplify (downstream) and
gt_deliver (final packaging).

Required env vars:
  ORDER_ID
"""
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.gt_chunk_helpers import run_stage

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gt_translate")


def run():
    run_stage("translate")


if __name__ == "__main__":
    run()
