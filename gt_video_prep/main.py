"""
gt_video_prep/main.py — Cloud Run Job (Gutenberg Track, video prep stage)

Reads `simplified_chapters.json` and generates a storyboard for video production:

  1. Global Character Sheet — Gemini analyzes the full text to create detailed
     visual descriptions of main characters and settings (ensures visual
     consistency across all scenes).
  2. Scene Breakdown — For each chapter, Gemini splits the simplified text
     into logical scenes and generates:
       - narration_text: refined script for TTS (BRONCI API)
       - visual_prompt: high-detail prompt for image/video generation (Luma AI)

Output: video_materials.json consumed by the frontend storyboard page.

Required env vars:
  ORDER_ID
"""
import json
import logging
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from ots_common.video.ltx_prompt_rules import LTX_VISUAL_PROMPT_RULES
from shared.config import cfg
from shared.db import update_job_status
from shared.gemini import call_gemini
from shared.notify import notify_stage
from shared.storage import read_temp_json, write_temp_json
from shared.tai_lo_translator import translate_to_tai_lo

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gt_video_prep")


# ── Prompts ──────────────────────────────────────────────────────────────────

CHARACTER_SHEET_PROMPT = """You are a professional book-to-video storyboard artist. Analyze the following simplified Chinese book text and create a detailed "Visual Style Guide" for consistent video generation.

Output a JSON object with two keys:
1. "characters": an object where each key is a character's name (in Chinese), and the value is a detailed English description of their appearance (height, build, hair/eye color, distinctive features, typical clothing).
2. "environment": a detailed English description of the primary setting/location.

Focus only on visually distinct elements. Output the JSON object with no commentary.

Book text:
__BOOK_TEXT_PLACEHOLDER__
"""

SCENE_BREAKDOWN_PROMPT = """You are a professional book-to-video director and storyboard artist. Given a chapter of a simplified Chinese book and a Global Character Sheet, break the chapter into distinct video scenes.

Each scene should be a single continuous camera shot lasting approximately 10-20 seconds when narrated. Split the text at natural story beats (scene changes, new actions, different locations).

Visual style: __STYLE_DESC__.

__LTX_VISUAL_PROMPT_RULES__

For each scene, output a JSON object with:
1. "scene_index": sequential number starting from 0
2. "narration_text": the text to be spoken (in Traditional Chinese, 繁體中文), adapted from the simplified text for natural spoken narration. Keep it concise — aim for 30-80 words per scene.
3. "visual_prompt": a detailed English video-generation prompt optimized for LTX built using the rules above.
4. "duration_est": estimated duration in seconds as a string (e.g., "15s")

Output a JSON object with key "scenes" containing the array of scenes. No commentary.

Global Character Sheet:
__CHARACTER_SHEET__

Chapter text:
__CHAPTER_TEXT_PLACEHOLDER__
"""


_STYLE_PROMPTS = {
    "photorealistic": None,
    "cinematic": "Cinematic, film grain, dramatic lighting, anamorphic, deep contrast, rich shadows",
    "anime": "Anime style, cel-shaded, bold lines, vibrant flat colors, Studio Ghibli inspired, painterly backgrounds",
    "3d_render": "3D render, Pixar style, bright colors, soft global illumination, subsurface scattering, playful",
    "comic": "Comic book style, halftone dots, bold outlines, Ben-Day dots, pop art colors, speech bubble aesthetic",
    "watercolor": "Watercolor painting, soft washes, paper texture, wet-on-wet, delicate translucent layers, hand-painted",
    "oil_painting": "Oil painting, impasto, visible brush strokes, canvas texture, Van Gogh or Rembrandt palette, thick paint",
}


def _get_style_prompt(visual_style: str) -> str:
    desc = _STYLE_PROMPTS.get(visual_style)
    if desc is None:
        return "Photorealistic, natural lighting, true-to-life, sharp details"
    return desc


# ── Helpers ──────────────────────────────────────────────────────────────────

def _validate_scene_data(data: dict) -> Optional[dict]:
    """Validate and normalize a single scene dict."""
    required = {"scene_index", "narration_text", "visual_prompt"}
    if not all(k in data for k in required):
        return None
    return {
        "scene_index": int(data["scene_index"]),
        "visual_prompt": str(data["visual_prompt"]).strip(),
        "duration_est": str(data.get("duration_est", "15s")),
        "narration_text": str(data["narration_text"]).strip(),
    }


def _extract_first_json(text: str) -> str:
    """Extract the first complete JSON object from text using raw_decode."""
    decoder = json.JSONDecoder()
    idx = text.find("{")
    if idx < 0:
        return text
    try:
        obj, end = decoder.raw_decode(text, idx)
        return json.dumps(obj)
    except (json.JSONDecodeError, ValueError):
        return text


def _build_character_sheet(full_text: str) -> dict:
    """Call Gemini to generate the Global Character Sheet."""
    prompt = CHARACTER_SHEET_PROMPT.replace("__BOOK_TEXT_PLACEHOLDER__", full_text)
    raw, usage = call_gemini(
        prompt,
        max_tokens=16384,
        response_mime_type="application/json",
    )
    logger.info(f"Character sheet Gemini usage: {usage}")
    cleaned = _extract_first_json(raw)
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict) and "characters" in data:
            return {
                "characters": data.get("characters", {}),
                "environment": data.get("environment", ""),
            }
    except (json.JSONDecodeError, ValueError):
        pass
    logger.warning("Failed to parse character sheet JSON — using fallback")
    return {"characters": {}, "environment": ""}


def _build_scenes(chapter_text: str, chapter_title: str,
                   chapter_index: int, character_sheet: dict,
                   visual_style: str = "photorealistic") -> List[dict]:
    """Call Gemini to break a chapter into scenes."""
    sheet_text = json.dumps(character_sheet, ensure_ascii=False, indent=2)
    prompt = SCENE_BREAKDOWN_PROMPT \
        .replace("__STYLE_DESC__", _get_style_prompt(visual_style)) \
        .replace("__LTX_VISUAL_PROMPT_RULES__", LTX_VISUAL_PROMPT_RULES) \
        .replace("__CHARACTER_SHEET__", sheet_text) \
        .replace("__CHAPTER_TEXT_PLACEHOLDER__", chapter_text)
    raw, usage = call_gemini(
        prompt,
        max_tokens=16384,
        response_mime_type="application/json",
    )
    logger.info(f"Chapter {chapter_index} scene breakdown Gemini usage: {usage}")
    try:
        data = json.loads(raw)
        scenes_raw = data.get("scenes", [])
        validated = []
        for s in scenes_raw:
            if isinstance(s, dict):
                v = _validate_scene_data(s)
                if v:
                    validated.append(v)
        return validated
    except (json.JSONDecodeError, ValueError) as e:
        logger.warning(
            f"Failed to parse scene JSON for chapter {chapter_index}: {e}"
        )
        return []


# ── Main ─────────────────────────────────────────────────────────────────────

def run():
    logger.info(f"=== gt_video_prep START — order: {cfg.ORDER_ID} ===")
    update_job_status("gt_video_prep", "running")

    try:
        simplified_chapters = read_temp_json("simplified_chapters.json")
        if not simplified_chapters:
            raise ValueError("simplified_chapters.json is empty — gt_simplify must run first")

        logger.info(f"Loaded {len(simplified_chapters)} simplified chapters")

        # Build full text for character analysis
        full_text = "\n\n".join(
            ch.get("text", "") for ch in simplified_chapters if ch.get("text")
        )

        # Step 1: Global Character Sheet
        logger.info("Generating Global Character Sheet...")
        character_sheet = _build_character_sheet(full_text)
        logger.info(
            f"Character sheet: {len(character_sheet.get('characters', {}))} characters"
        )

        # Step 2: Scene breakdown per chapter
        chapters_out = []
        total_scenes = 0
        for ch in simplified_chapters:
            ch_text = ch.get("text", "").strip()
            ch_title = ch.get("title", f"Chapter {ch.get('chapter_index', 0) + 1}")
            ch_index = ch.get("chapter_index", 0)
            if not ch_text:
                logger.warning(f"Skipping empty chapter {ch_index}")
                continue

            logger.info(f"Breaking down chapter {ch_index}: {ch_title}...")
            visual_style = "photorealistic"
            scenes = _build_scenes(ch_text, ch_title, ch_index, character_sheet, visual_style)

            # Step 2a: Translate to Tâi-lô
            logger.info(f"  Translating {len(scenes)} scenes to Tâi-lô...")
            for s in scenes:
                zh_text = s.get("narration_text", "")
                if zh_text:
                    try:
                        s["narration_tai_lo"] = translate_to_tai_lo(zh_text)
                    except Exception as e:
                        logger.warning(f"  Tai-lo translation failed for scene {s.get('scene_index')}: {e}")
                        s["narration_tai_lo"] = zh_text
                else:
                    s["narration_tai_lo"] = ""

            # Convert to dual-track structure
            dual_track_scenes = []
            for s in scenes:
                zh_text = s.pop("narration_text", "")
                tai_lo_text = s.pop("narration_tai_lo", "")
                s["tracks"] = {
                    "zh": {"narration_text": zh_text},
                    "tai-lo": {"narration_text": tai_lo_text},
                }
                dual_track_scenes.append(s)

            chapters_out.append({
                "chapter_index": ch_index,
                "title": ch_title,
                "scenes": dual_track_scenes,
            })
            total_scenes += len(scenes)
            logger.info(f"  → {len(scenes)} scenes")

        video_materials = {
            "global_style": {
                "characters": character_sheet.get("characters", {}),
                "environment": character_sheet.get("environment", ""),
            },
            "chapters": chapters_out,
            "settings": {
                "voice_id_zh": "cmn-TW-vs2-F04",
                "voice_id_tai_lo": "cmn-TW-vs2-F04",
                "speaking_rate": 1.0,
                "short_pause_duration": 150,
                "long_pause_duration": 450,
                "visual_style": "photorealistic",
            },
        }

        write_temp_json("video_materials.json", video_materials)

        update_job_status("gt_video_prep", "success", qa_result={
            "num_chapters": len(chapters_out),
            "num_scenes": total_scenes,
        })
        notify_stage("gt_video_prep")
        logger.info(
            f"=== gt_video_prep DONE — {len(chapters_out)} chapters, "
            f"{total_scenes} scenes ==="
        )

    except Exception as e:
        logger.error(f"gt_video_prep failed: {e}", exc_info=True)
        update_job_status("gt_video_prep", "failed", error_message=str(e)[:500])
        raise


if __name__ == "__main__":
    run()
