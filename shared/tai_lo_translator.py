"""shared/tai_lo_translator.py — thin wrapper around ots-common for the pipeline.

Provides the same public API (translate_to_tai_lo(chinese_text) -> str)
as before, but delegates the prompt + logic to ots_common.translation.tai_lo.
"""

import logging
import sys
from pathlib import Path

try:
    from ots_common.translation.tai_lo import translate_to_tai_lo as _translate
except ImportError:
    _candidates = [
        Path(__file__).resolve().parent.parent / "ots-common",          # submodule: ots-pipeline/ots-common/
        Path(__file__).resolve().parent.parent.parent.parent / "ots-common",  # dev: repo root
    ]
    for _root in _candidates:
        if _root.exists():
            sys.path.insert(0, str(_root))
            try:
                from ots_common.translation.tai_lo import translate_to_tai_lo as _translate
                break
            except ImportError:
                sys.path.pop(0)
    else:
        raise

from shared.gemini import translate

logger = logging.getLogger("tai_lo_translator")


def translate_to_tai_lo(chinese_text: str) -> str:
    """Convert Traditional Chinese narration text to Hanzi-only Taiwanese Hokkien.

    Args:
        chinese_text: Traditional Chinese text to convert.

    Returns:
        Taiwanese Hokkien text using Hanzi characters only.
    """
    if not chinese_text.strip():
        return chinese_text

    def _call_gemini(prompt: str) -> str:
        result = translate(prompt, max_tokens=4096, job_type="gt_video_prep")
        return result.strip()

    try:
        result = _translate(chinese_text, _call_gemini)
        logger.info(
            f"Tai-lo translation OK — {len(chinese_text)} chars → {len(result)} chars"
        )
        return result
    except Exception as e:
        logger.error(
            f"Tai-lo translation failed for text starting with '{chinese_text[:40]}': {e}"
        )
        raise
