"""
gt_simplify/main.py — Cloud Run Job (Gutenberg Track, simplify stage)

Reads `translated.json` (the standard Chinese translation from gt_translate)
and `source/chapters.json` (chapter boundaries from gt_chapter_splitter),
then simplifies each chapter as a whole via Gemini.

Produces two outputs:
  - simplified_chapters.json — whole-chapter simplified stories
  - simplified.json — per-segment split (by paragraph) for gt_tailo

Batching: whole-chapter. One Gemini call per chapter (no sub-chunking),
with the full chapter text as context for a coherent story output.

Output: simplified_chapters.json (narrative) + simplified.json (per-segment)
consumed by gt_tailo (downstream) and gt_deliver (final packaging).

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
logger = logging.getLogger("gt_simplify")


def run():
    run_stage("simplify")


if __name__ == "__main__":
    run()
