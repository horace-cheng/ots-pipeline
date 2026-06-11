"""
gt_tailo/main.py — Cloud Run Job (Gutenberg Track, tailo stage)

Reads `simplified.json` (the youth-friendly Chinese from gt_simplify) and
produces `tailo.json` — the Hanzi-dominant version with Tai-lo (Tâi-lô)
romanization in parentheses after each phrase.

Batching: 10 segments per Gemini call (segment-based, mirrors the FT design
and matches translate's batching strategy). The input to each Gemini call
is the simplified version, so the romanization reads naturally over the
youth-friendly text.

Output: per-segment rows consumed by gt_deliver (final packaging).

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
logger = logging.getLogger("gt_tailo")


def run():
    run_stage("tailo")


if __name__ == "__main__":
    run()
