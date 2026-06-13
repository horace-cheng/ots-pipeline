"""
Local integration tests for gt_video_prep.

Tests the storyboard generation logic with mock simplified chapter data.
These tests use the actual Gemini API (requires valid credentials in the
test environment) or can be run with --skip-gemini to test only the
data-wrangling logic.

Usage:
    ORDER_ID=test DB_URL=sqlite:///tmp/test.db python3.10 -m pytest tests/test_gt_video_prep.py -v
"""
import os
os.environ.setdefault("ORDER_ID", "test-video-order")
os.environ.setdefault("DB_URL", "sqlite:///tmp/test.db")
os.environ.setdefault("ENV", "test")

import json
import pytest
import importlib.util
from pathlib import Path

_video_prep_dir = os.path.join(os.path.dirname(__file__), '..', 'gt_video_prep')
_spec = importlib.util.spec_from_file_location(
    "gt_video_prep_main",
    os.path.join(_video_prep_dir, "main.py"),
)
_gt_video_prep = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gt_video_prep)

CHARACTER_SHEET_PROMPT = _gt_video_prep.CHARACTER_SHEET_PROMPT
SCENE_BREAKDOWN_PROMPT = _gt_video_prep.SCENE_BREAKDOWN_PROMPT
_build_character_sheet = _gt_video_prep._build_character_sheet
_build_scenes = _gt_video_prep._build_scenes
_validate_scene_data = _gt_video_prep._validate_scene_data

# ── Sample Data ───────────────────────────────────────────────────────────────

SAMPLE_SIMPLIFIED_CHAPTERS = [
    {
        "chapter_index": 0,
        "title": "Chapter I. Into the Primitive",
        "text": (
            "巴克不讀報紙，否則他就會知道，一場風暴正在醞釀。"
            "他住在陽光普照的聖克拉拉谷的一座大宅邸裡。"
            "巴克統治著這片廣袤的領地。"
            "但有一天，一個園丁的助手把巴克帶走了。"
            "巴克被賣掉，開始了他命運的轉變。"
        ),
    },
    {
        "chapter_index": 1,
        "title": "Chapter II. The Law of Club and Fang",
        "text": (
            "巴克被帶到一個陌生的地方，那裡有許多狗。"
            "他學會了棍棒和牙齒的法律。"
            "斯匹茲，一隻雪橇犬，成了巴克的敵人。"
            "巴克知道，他必須學會生存。"
        ),
    },
]


# ── Prompt Structure ─────────────────────────────────────────────────────────

def test_character_sheet_prompt_has_placeholders():
    assert "__BOOK_TEXT_PLACEHOLDER__" in CHARACTER_SHEET_PROMPT
    assert "characters" in CHARACTER_SHEET_PROMPT
    assert "environment" in CHARACTER_SHEET_PROMPT


def test_scene_breakdown_prompt_has_placeholders():
    assert "__CHARACTER_SHEET__" in SCENE_BREAKDOWN_PROMPT
    assert "__CHAPTER_TEXT_PLACEHOLDER__" in SCENE_BREAKDOWN_PROMPT
    assert "narration_text" in SCENE_BREAKDOWN_PROMPT
    assert "visual_prompt" in SCENE_BREAKDOWN_PROMPT


# ── Validation ───────────────────────────────────────────────────────────────

def test_validate_scene_data_valid():
    data = {
        "scene_index": 0,
        "narration_text": "巴克的故事開始了。",
        "visual_prompt": "A large dog in a sunny California valley.",
    }
    result = _validate_scene_data(data)
    assert result is not None
    assert result["scene_index"] == 0
    assert result["narration_text"] == "巴克的故事開始了。"


def test_validate_scene_data_missing_fields():
    data = {"scene_index": 0, "narration_text": "test"}
    result = _validate_scene_data(data)
    assert result is None


def test_validate_scene_data_empty_narration():
    data = {
        "scene_index": 0,
        "narration_text": "  ",
        "visual_prompt": "A dog.",
    }
    result = _validate_scene_data(data)
    assert result is not None
    assert result["narration_text"] == ""


# ── Gemini Integration (optional) ────────────────────────────────────────────

@pytest.mark.skipif(
    not os.environ.get("TEST_GEMINI"),
    reason="Set TEST_GEMINI=1 to run Gemini integration tests"
)
def test_build_character_sheet_with_gemini():
    full_text = "\n\n".join(ch["text"] for ch in SAMPLE_SIMPLIFIED_CHAPTERS)
    result = _build_character_sheet(full_text)
    assert isinstance(result, dict)
    assert "characters" in result
    assert "environment" in result


@pytest.mark.skipif(
    not os.environ.get("TEST_GEMINI"),
    reason="Set TEST_GEMINI=1 to run Gemini integration tests"
)
def test_build_scenes_with_gemini():
    character_sheet = {
        "characters": {
            "巴克": "A large St. Bernard dog with a white patch on his left ear, thick brown fur, strong build."
        },
        "environment": "A sunny California estate with a big house, stables, and gardens.",
    }
    ch_text = SAMPLE_SIMPLIFIED_CHAPTERS[0]["text"]
    scenes = _build_scenes(ch_text, "Chapter I", 0, character_sheet)
    assert len(scenes) > 0
    for scene in scenes:
        assert "narration_text" in scene
        assert "visual_prompt" in scene
        assert len(scene["narration_text"]) > 0
