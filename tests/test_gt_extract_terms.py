"""
Unit tests for gt_extract_terms/main.py
"""
import os
os.environ.setdefault("ORDER_ID", "test-order-gutenberg")
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")

import pytest
import importlib.util

_gt_dir = os.path.join(os.path.dirname(__file__), '..', 'gt_extract_terms')
_spec = importlib.util.spec_from_file_location("gt_extract_terms_main", os.path.join(_gt_dir, "main.py"))
_gt_extract = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gt_extract)

EXTRACT_PROMPT = _gt_extract.EXTRACT_PROMPT
parse_terminology_response = _gt_extract.parse_terminology_response
read_source_sample_v2 = _gt_extract.read_source_sample_v2
read_chunks_concat = _gt_extract.read_chunks_concat
list_source_chunks = _gt_extract.list_source_chunks
run = _gt_extract.run


# ── EXTRACT_PROMPT ─────────────────────────────────────────────────────────

def test_extract_prompt_asks_for_50_entities():
    assert "50" in EXTRACT_PROMPT
    assert "JSON" in EXTRACT_PROMPT
    assert "Traditional Chinese" in EXTRACT_PROMPT


def test_extract_prompt_includes_source_text_placeholder():
    assert "{source_text}" in EXTRACT_PROMPT


def test_extract_prompt_includes_examples():
    """Prompt should include example JSON entries to guide the model."""
    assert "Mr. Darcy" in EXTRACT_PROMPT
    assert "Elizabeth Bennet" in EXTRACT_PROMPT


# ── parse_terminology_response ────────────────────────────────────────────

def test_parse_clean_json():
    """Clean JSON object should parse directly."""
    raw = '{"Mr. Darcy": "達西先生", "Elizabeth": "伊麗莎白"}'
    result = parse_terminology_response(raw)
    assert result == {"Mr. Darcy": "達西先生", "Elizabeth": "伊麗莎白"}


def test_parse_json_with_markdown_fences():
    """Should strip ```json ... ``` fences."""
    raw = '```json\n{"A": "甲", "B": "乙"}\n```'
    result = parse_terminology_response(raw)
    assert result == {"A": "甲", "B": "乙"}


def test_parse_json_with_bare_fences():
    """Should strip bare ``` fences (no language)."""
    raw = '```\n{"X": "十"}\n```'
    result = parse_terminology_response(raw)
    assert result == {"X": "十"}


def test_parse_invalid_json_returns_empty():
    """Garbage response should return empty dict (don't crash the pipeline)."""
    result = parse_terminology_response("This is not JSON at all")
    assert result == {}


def test_parse_array_instead_of_object_returns_empty():
    """A JSON array (not object) should return empty dict."""
    result = parse_terminology_response('["A", "B"]')
    assert result == {}


def test_parse_empty_string():
    result = parse_terminology_response("")
    assert result == {}


def test_parse_trims_whitespace():
    raw = '   \n  {"K": "鍵"}  \n  '
    result = parse_terminology_response(raw)
    assert result == {"K": "鍵"}


# ── v2 source reader (read_source_sample_v2) ──────────────────────────────
# v2 fetcher writes ``source/full_text.txt`` (canonical) and ``segments.json``
# (fallback). The old v1 reader used ``source/chunk_*.txt``. The v2 job must
# work with the new layout AND fall back to v1 for legacy orders.

from unittest.mock import patch, MagicMock


def test_v2_reads_full_text_txt():
    """v2 layout: source/full_text.txt is preferred — read first 60K chars."""
    with patch.object(_gt_extract, "temp_blob_exists", return_value=True), \
         patch.object(_gt_extract, "read_temp_text", return_value="A" * 200_000) as mock_read:
        result = read_source_sample_v2(max_chars=60_000)
    assert len(result) == 60_000
    mock_read.assert_called_once_with("source/full_text.txt")


def test_v2_falls_back_to_segments_json():
    """If full_text.txt is missing, fall back to segments.json sample."""
    def exists(path):
        return path == "segments.json"
    segs = [
        {"index": 0, "text": "First segment " * 100},
        {"index": 1, "text": "Second segment " * 100},
        {"index": 2, "text": "Third segment " * 100},
    ]
    with patch.object(_gt_extract, "temp_blob_exists", side_effect=exists), \
         patch.object(_gt_extract, "read_temp_json", return_value=segs):
        result = read_source_sample_v2(max_chars=2000)
    assert "First segment" in result
    assert "Second segment" in result
    # Stops once total >= max_chars
    assert result.count("Third segment") == 0


def test_v2_returns_empty_when_no_source():
    """If neither v2 nor v1 artifacts exist, return empty string."""
    with patch.object(_gt_extract, "temp_blob_exists", return_value=False):
        result = read_source_sample_v2()
    assert result == ""


def test_v2_segments_json_not_a_list_returns_empty():
    """Defensive: malformed segments.json should not crash."""
    def exists(path):
        return path == "segments.json"
    with patch.object(_gt_extract, "temp_blob_exists", side_effect=exists), \
         patch.object(_gt_extract, "read_temp_json", return_value={"not": "a list"}):
        result = read_source_sample_v2()
    assert result == ""


def test_v2_handles_segment_without_text_field():
    """If a segment dict is missing the 'text' key, treat it as empty string."""
    def exists(path):
        return path == "segments.json"
    segs = [{"index": 0}, {"index": 1, "text": "hello"}]
    with patch.object(_gt_extract, "temp_blob_exists", side_effect=exists), \
         patch.object(_gt_extract, "read_temp_json", return_value=segs):
        result = read_source_sample_v2()
    assert "hello" in result


# ── run() integration — v2 happy path ─────────────────────────────────────

def test_run_v2_happy_path_saves_terminology():
    """Full v2 run: read full_text, call Gemini, write terminology.json."""
    fake_translate = MagicMock(return_value='{"Mr. Darcy": "達西先生", "Elizabeth": "伊麗莎白"}')
    with patch.object(_gt_extract, "read_source_sample_v2", return_value="Excerpt of Pride and Prejudice..."), \
         patch("shared.gemini.translate", fake_translate), \
         patch.object(_gt_extract, "write_temp_json") as mock_write, \
         patch.object(_gt_extract, "update_job_status") as mock_status:
        run()
    mock_write.assert_called_once()
    filename, data = mock_write.call_args[0]
    assert filename == "terminology.json"
    assert data == {"Mr. Darcy": "達西先生", "Elizabeth": "伊麗莎白"}
    statuses = [c.args[1] for c in mock_status.call_args_list]
    assert "running" in statuses
    assert "success" in statuses


def test_run_falls_back_to_v1_chunks_when_no_v2_artifacts():
    """If read_source_sample_v2 returns empty, the runner tries read_chunks_concat."""
    fake_translate = MagicMock(return_value='{"X": "Y"}')
    with patch.object(_gt_extract, "read_source_sample_v2", return_value=""), \
         patch.object(_gt_extract, "read_chunks_concat", return_value="Legacy chunk text") as mock_legacy, \
         patch("shared.gemini.translate", fake_translate), \
         patch.object(_gt_extract, "write_temp_json") as mock_write, \
         patch.object(_gt_extract, "update_job_status") as mock_status:
        run()
    mock_legacy.assert_called_once()
    mock_write.assert_called_once()
    statuses = [c.args[1] for c in mock_status.call_args_list]
    assert "success" in statuses


def test_run_raises_when_no_source_anywhere():
    """If both v2 and v1 are missing, the job fails with a clear error."""
    with patch.object(_gt_extract, "read_source_sample_v2", return_value=""), \
         patch.object(_gt_extract, "read_chunks_concat", return_value=""), \
         patch.object(_gt_extract, "update_job_status") as mock_status:
        with pytest.raises(ValueError, match="No source content found"):
            run()
    statuses = [c.args[1] for c in mock_status.call_args_list]
    assert "failed" in statuses


def test_run_empty_terminology_response_still_succeeds():
    """Garbage Gemini response → empty dict → job still marked success."""
    fake_translate = MagicMock(return_value="not valid json")
    with patch.object(_gt_extract, "read_source_sample_v2", return_value="Some text"), \
         patch("shared.gemini.translate", fake_translate), \
         patch.object(_gt_extract, "write_temp_json") as mock_write, \
         patch.object(_gt_extract, "update_job_status") as mock_status:
        run()
    filename, data = mock_write.call_args[0]
    assert filename == "terminology.json"
    assert data == {}
    statuses = [c.args[1] for c in mock_status.call_args_list]
    assert "success" in statuses
    # qa_result includes source_layout=v2
    success_call = next(c for c in mock_status.call_args_list if c.args[1] == "success")
    assert success_call.kwargs.get("qa_result", {}).get("source_layout") == "v2"
