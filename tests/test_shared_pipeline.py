"""
Unit tests for shared/gt_chunk_helpers.py — the shared module behind the
three Gutenberg jobs (gt_translate, gt_simplify, gt_tailo).

Strategy: use the real `gt_chunk_helpers` module (importable once ORDER_ID
and DB_URL are set) and patch the dependencies it uses (translate, storage
helpers, DB helpers) with `monkeypatch.setattr`. monkeypatch undoes each
patch after the test, so sibling test files (test_shared_db, etc.) are
not affected.
"""
import os
os.environ.setdefault("ORDER_ID", "test-order-gutenberg")
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Make pipeline root importable for `from shared import gt_chunk_helpers`.
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared import gt_chunk_helpers as h
from shared import storage as real_storage
from shared import config as real_config


# ── Test-local fake state ─────────────────────────────────────────────────

_fake_storage_blobs: dict = {}   # full GCS path -> raw text/json string
_translate_calls: list[dict] = []   # all translate() invocations
_response_queue: list[str] = []     # pre-loaded Gemini responses (FIFO)


def _write_temp_text(fn: str, content: str) -> str:
    full = f"pipeline/test-order-gutenberg/{fn}"
    _fake_storage_blobs[full] = content
    return full


def _read_temp_text(fn: str) -> str:
    full = f"pipeline/test-order-gutenberg/{fn}"
    if full not in _fake_storage_blobs:
        raise FileNotFoundError(f"fake: {full} not found")
    return _fake_storage_blobs[full]


def _write_temp_json(fn: str, data) -> str:
    full = f"pipeline/test-order-gutenberg/{fn}"
    _fake_storage_blobs[full] = json.dumps(data, ensure_ascii=False, indent=2)
    return full


def _read_temp_json(fn: str):
    full = f"pipeline/test-order-gutenberg/{fn}"
    if full not in _fake_storage_blobs:
        raise FileNotFoundError(f"fake: {full} not found")
    return json.loads(_fake_storage_blobs[full])


def _save_stage_checkpoint(stage: str, batch_id: int, data) -> str:
    prefix = h.CHECKPOINT_STAGE_PREFIX[stage]
    return _write_temp_json(f"{prefix}{batch_id}.json", data)


def _load_stage_checkpoint(stage: str, batch_id: int):
    prefix = h.CHECKPOINT_STAGE_PREFIX[stage]
    full = f"pipeline/test-order-gutenberg/{prefix}{batch_id}.json"
    if full not in _fake_storage_blobs:
        return None
    return json.loads(_fake_storage_blobs[full])


def _list_stage_checkpoints(stage: str):
    prefix = h.CHECKPOINT_STAGE_PREFIX[stage]
    out = []
    for p in _fake_storage_blobs:
        start = f"pipeline/test-order-gutenberg/{prefix}"
        if p.startswith(start) and p.endswith(".json"):
            try:
                bid = int(p.split(prefix)[1].split(".json")[0])
                out.append(bid)
            except ValueError:
                pass
    return sorted(out)


def _fake_get_client():
    client = MagicMock()
    bucket = MagicMock()
    def _list_blobs(prefix=""):
        prefix = prefix or ""
        matched = [n for n in _fake_storage_blobs if n.startswith(prefix)]
        blobs = []
        for nm in matched:
            b = MagicMock()
            b.name = nm
            blobs.append(b)
        return blobs
    bucket.list_blobs.side_effect = _list_blobs
    client.bucket.return_value = bucket
    return client


def _fake_translate(prompt, **kwargs):
    response = _response_queue.pop(0) if _response_queue else ""
    _translate_calls.append({"prompt": prompt, "kwargs": kwargs, "response": response})
    return response


class _Cfg:
    ORDER_ID = "test-order-gutenberg"
    PROJECT_ID = "test-project"
    ENV = "test"
    BUCKET_TEMP = "test-temp"


# Install all fakes via monkeypatch. monkeypatch undoes each patch after
# the test, so sibling test files are not affected.
@pytest.fixture(autouse=True)
def _install_fakes(monkeypatch):
    _fake_storage_blobs.clear()
    _translate_calls.clear()
    _response_queue.clear()

    # Patch the names imported into gt_chunk_helpers' namespace.
    monkeypatch.setattr("shared.gt_chunk_helpers.translate", _fake_translate)
    monkeypatch.setattr("shared.gt_chunk_helpers.read_temp_text", _read_temp_text)
    monkeypatch.setattr("shared.gt_chunk_helpers.write_temp_json", _write_temp_json)
    monkeypatch.setattr("shared.gt_chunk_helpers.read_temp_json", _read_temp_json)
    monkeypatch.setattr("shared.gt_chunk_helpers.save_stage_checkpoint", _save_stage_checkpoint)
    monkeypatch.setattr("shared.gt_chunk_helpers.load_stage_checkpoint", _load_stage_checkpoint)
    monkeypatch.setattr("shared.gt_chunk_helpers.list_stage_checkpoints", _list_stage_checkpoints)
    monkeypatch.setattr("shared.gt_chunk_helpers.update_job_status", lambda *a, **k: None)
    monkeypatch.setattr("shared.gt_chunk_helpers.update_order_field", lambda *a, **k: None)

    # Patch the real modules for the `from shared.storage import get_client`
    # and `from shared.config import cfg` that happens inside
    # list_chapter_checkpoints.
    monkeypatch.setattr(real_storage, "get_client", _fake_get_client)
    monkeypatch.setattr(real_config, "cfg", _Cfg())
    yield


# ── Prompt structure ──────────────────────────────────────────────────────

def test_translate_prompt_has_glossary_and_batch_size_placeholders():
    assert "{glossary}" in h.TRANSLATE_PROMPT
    assert "{batch_size}" in h.TRANSLATE_PROMPT
    assert "Traditional Chinese" in h.TRANSLATE_PROMPT
    assert "[1]" in h.TRANSLATE_PROMPT


def test_simplify_chapter_prompt_targets_age_group():
    assert "8-12" in h.SIMPLIFY_CHAPTER_PROMPT
    assert "narrative flow" in h.SIMPLIFY_CHAPTER_PROMPT
    assert "coherent" in h.SIMPLIFY_CHAPTER_PROMPT


def test_tailo_prompt_uses_taiwanese_specifically():
    assert "Taiwanese" in h.TAILO_PROMPT or "Tâi-lô" in h.TAILO_PROMPT or "Tāi-lô" in h.TAILO_PROMPT


# ── Path / job-type constants ─────────────────────────────────────────────

def test_input_paths_match_per_stage_source():
    assert h.INPUT_PATHS["translate"] == "segments.json"
    assert h.INPUT_PATHS["simplify"] == "translated.json"
    assert h.INPUT_PATHS["tailo"] == "simplified.json"  # tailo reads simplified, not translated


def test_output_paths_match_per_stage_sink():
    assert h.OUTPUT_PATHS["translate"] == "translated.json"
    assert h.OUTPUT_PATHS["simplify"] == "simplified.json"
    assert h.OUTPUT_PATHS["tailo"] == "tailo.json"


def test_job_type_labels_match_old_pipeline():
    """The labels in pipeline_jobs are unchanged from the old gt_process_chunk."""
    assert h.JOB_TYPE["translate"] == "gt_translate"
    assert h.JOB_TYPE["simplify"] == "gt_simplify"
    assert h.JOB_TYPE["tailo"] == "gt_tailo"


# ── Glossary formatting ───────────────────────────────────────────────────

def test_format_glossary_for_prompt_includes_each_entry():
    g = {"Mr. Darcy": "達西先生", "Elizabeth": "伊麗莎白"}
    out = h.format_glossary_for_prompt(g)
    assert "Mr. Darcy" in out
    assert "達西先生" in out
    assert "Elizabeth" in out
    assert "伊麗莎白" in out


def test_format_glossary_for_prompt_handles_empty():
    out = h.format_glossary_for_prompt({})
    # Should not crash, should return a non-empty placeholder
    assert isinstance(out, str)
    assert "terminology" in out.lower() or out == ""


# ── load_input_segments reshape fix (the original bug) ───────────────────

def test_load_input_segments_reshapes_to_text_field():
    """Segments loaded from consolidated JSON have `translated` but no `text`.
    load_input_segments must reshape them so build_prompt can use `text`."""
    blob = [{"index": 0, "translated": "你好"}, {"index": 1, "translated": "再見"}]
    _write_temp_json(h.INPUT_PATHS["simplify"], blob)
    out = h.load_input_segments("simplify")
    assert out[0]["text"] == "你好"
    assert out[1]["text"] == "再見"
    # Original field preserved
    assert out[0]["translated"] == "你好"


def test_load_input_segments_does_not_clobber_existing_text():
    """If a segment already has `text`, leave it alone."""
    blob = [{"index": 0, "text": "preserved"}]
    _write_temp_json(h.INPUT_PATHS["simplify"], blob)
    out = h.load_input_segments("simplify")
    assert out[0]["text"] == "preserved"


# ── build_prompt + parse_numbered_response + process_batch ───────────────

def test_build_prompt_includes_glossary_and_numbered_placeholders():
    p = h.build_prompt(h.TRANSLATE_PROMPT, [{"text": "A"}, {"text": "B"}], glossary="x: y")
    assert "[1]" in p
    assert "[2]" in p
    assert "x: y" in p
    assert "Traditional Chinese" in p


def test_parse_numbered_response_basic():
    raw = "[1] 你好\n[2] 再見"
    out = h.parse_numbered_response(raw, expected=2)
    assert out[0] == "你好"
    assert out[1] == "再見"


def test_parse_numbered_response_no_markers_returns_all_none():
    """If Gemini output has no [N] markers at all, return all None (no silent fallback)."""
    raw = "just some text"
    out = h.parse_numbered_response(raw, expected=2)
    assert out == [None, None]


def test_process_batch_calls_translate_and_returns_translations():
    segs = [{"index": 0, "text": "A"}, {"index": 1, "text": "B"}]
    _response_queue.append("[1] 甲\n[2] 乙")
    out = h.process_batch(segs, stage="translate", glossary="")
    assert out == ["甲", "乙"]


def test_process_batch_retries_with_focused_prompt_on_partial():
    """When the model returns only some segments, the retry should send
    only the missing segments via TRANSLATE_RETRY_PROMPT."""
    segs = [{"index": 0, "text": "A"}, {"index": 1, "text": "B"}, {"index": 2, "text": "C"}]
    # First response: only segment 0 and 2 (missing segment 1)
    _response_queue.append("[1] 第一個\n[3] 第三個")
    # Retry response: segment 1 only (using retry prompt)
    _response_queue.append("[1] 第二個")
    out = h.process_batch(segs, stage="translate", glossary="term: 詞")
    assert out == ["第一個", "第二個", "第三個"]
    # Two translate calls were made
    assert len(_translate_calls) == 2
    # Second call's prompt should use the retry prompt template
    retry_prompt = _translate_calls[1]["prompt"]
    assert "not properly translated" in retry_prompt.lower()
    # Only segment B is in the retry prompt
    assert "[1]\nB" in retry_prompt
    # Retry prompt should only ask for segment 1 (B)
    expected_retry_content = h.TRANSLATE_RETRY_PROMPT.format(
        glossary="term: 詞", batch_size=1, segments="[1]\nB"
    ).strip()
    assert retry_prompt.strip() == expected_retry_content.strip()


def test_process_batch_retry_merges_results():
    """Partial results from multiple attempts should be merged, with later
    attempts filling gaps from earlier ones."""
    segs = [{"index": 0, "text": "X"}, {"index": 1, "text": "Y"}]
    # First call: returns only segment 0
    _response_queue.append("[1] 第一個")
    # Retry: returns segment 1
    _response_queue.append("[1] 第二個")
    out = h.process_batch(segs, stage="tailo", glossary="")
    assert out == ["第一個", "第二個"]


# ── build_batches (segment) ──────────────────────────────────────────────

def test_build_batches_respects_batch_size():
    """build_batches returns batch boundaries for a given total count."""
    batches = h.build_batches(25, batch_size=10)
    assert len(batches) == 3
    assert batches[0]["start"] == 0 and batches[0]["count"] == 10
    assert batches[1]["start"] == 10 and batches[1]["count"] == 10
    assert batches[2]["start"] == 20 and batches[2]["count"] == 5


def test_build_batches_handles_exact_multiple():
    batches = h.build_batches(20, batch_size=10)
    assert len(batches) == 2
    assert all(b["count"] == 10 for b in batches)


# ── run_segment_pipeline ────────────────────────────────────────────────

def test_run_segment_pipeline_processes_all_batches():
    segs = [{"index": i, "text": f"s{i}"} for i in range(7)]
    # Pre-seed 1 response (7 segments, batch_size=10 → 1 batch)
    _response_queue.append("\n".join(f"[{i+1}] 譯{i+1}" for i in range(7)))
    translations, stats = h.run_segment_pipeline(segs, stage="translate")
    # All 7 should be translated
    assert translations == [f"譯{i+1}" for i in range(7)]
    # Consolidated output should be written
    h.write_consolidated_output(segs, translations, stage="translate")
    consolidated = _read_temp_json(h.OUTPUT_PATHS["translate"])
    assert len(consolidated) == 7


def test_run_segment_pipeline_resumes_from_checkpoint():
    segs = [{"index": i, "text": f"s{i}"} for i in range(7)]
    # Pre-seed checkpoint for batch 0 (the only batch)
    _save_stage_checkpoint("translate", 0, {
        "batch_id":     0,
        "start":        0,
        "count":        7,
        "translations": [f"快取{i}" for i in range(7)],
    })
    translations, stats = h.run_segment_pipeline(segs, stage="translate")
    # No new translate call should have been made
    assert _translate_calls == []
    assert translations == [f"快取{i}" for i in range(7)]
    assert stats["n_skipped"] == 1


# ── run_simplify_pipeline ────────────────────────────────────────────────

def test_run_simplify_pipeline_calls_gemini_per_chapter_and_writes_outputs():
    """run_simplify_pipeline should call translate once per chapter and
    write simplified_chapters.json + produce per-segment output."""
    translated_entries = [
        {"index": 0, "source": "src-a", "translated": "這是第一句。", "chapter_index": 0, "chapter_title": "Ch1"},
        {"index": 1, "source": "src-b", "translated": "這是第二句。", "chapter_index": 0, "chapter_title": "Ch1"},
        {"index": 2, "source": "src-c", "translated": "第二章內容。", "chapter_index": 1, "chapter_title": "Ch2"},
    ]
    chapters = [
        {"index": 0, "title": "Ch1", "segment_start": 0, "segment_end": 2},
        {"index": 1, "title": "Ch2", "segment_start": 2, "segment_end": 3},
    ]
    _response_queue.append("簡化第一章全文。")
    _response_queue.append("簡化第二章全文。")
    chapter_entries, stats = h.run_simplify_pipeline(translated_entries, chapters)
    assert len(chapter_entries) == 2
    assert stats["n_chapters"] == 2
    assert stats["n_simplified"] == 2
    assert len(_translate_calls) == 2
    # simplified_chapters.json should have been written
    saved = _read_temp_json(h.SIMPLIFIED_CHAPTERS_OUTPUT_PATH)
    assert len(saved) == 2
    assert saved[0]["text"] == "簡化第一章全文。"
    assert saved[1]["text"] == "簡化第二章全文。"

    # split_simplified_chapters should work on the output
    per_seg = h.split_simplified_chapters(chapter_entries)
    assert len(per_seg) >= 2  # at least one paragraph per chapter


def test_run_simplify_pipeline_handles_empty_chapter():
    """Chapters with no translated text should produce empty output."""
    translated_entries = [
        {"index": 0, "source": "src-a", "translated": "", "chapter_index": 0, "chapter_title": "Ch1"},
    ]
    chapters = [
        {"index": 0, "title": "Ch1", "segment_start": 0, "segment_end": 1},
    ]
    chapter_entries, stats = h.run_simplify_pipeline(translated_entries, chapters)
    assert len(chapter_entries) == 1
    assert chapter_entries[0]["text"] == ""
    assert stats["n_simplified"] == 0


def test_split_simplified_chapters_splits_on_paragraphs():
    entries = [
        {"chapter_index": 0, "title": "Ch1", "text": "第一段。\n\n第二段。\n\n第三段。"},
    ]
    out = h.split_simplified_chapters(entries)
    assert len(out) == 3
    assert all(e["translated"] for e in out)
    assert out[0]["chapter_index"] == 0


def test_split_simplified_chapters_handles_single_paragraph():
    entries = [
        {"chapter_index": 0, "title": "Ch1", "text": "只有一段。"},
    ]
    out = h.split_simplified_chapters(entries)
    assert len(out) == 1
    assert out[0]["translated"] == "只有一段。"


def test_split_simplified_chapters_handles_empty_chapter():
    entries = [
        {"chapter_index": 0, "title": "Ch1", "text": ""},
    ]
    out = h.split_simplified_chapters(entries)
    assert len(out) == 1
    assert out[0]["translated"] == ""


# ── write_consolidated_output ────────────────────────────────────────────

def test_write_consolidated_output_uses_correct_source_field():
    """translate: source=segments[i].text. simplify/tailo: source=segments[i].source."""
    segs = [
        {"index": 0, "text": "english-src", "source": "translated-src"},
    ]
    translations = ["繁中譯"]
    h.write_consolidated_output(segs, translations, stage="translate")
    consolidated = _read_temp_json(h.OUTPUT_PATHS["translate"])
    # For translate stage, source = segment's text (the English source)
    assert consolidated[0]["source"] == "english-src"
    assert consolidated[0]["translated"] == "繁中譯"


def test_write_consolidated_output_for_simplify_uses_source_field():
    segs = [{"index": 0, "text": "原譯", "source": "english-src"}]
    translations = ["簡化"]
    h.write_consolidated_output(segs, translations, stage="simplify")
    consolidated = _read_temp_json(h.OUTPUT_PATHS["simplify"])
    # For simplify stage, source = segment's source (English)
    assert consolidated[0]["source"] == "english-src"
    assert consolidated[0]["translated"] == "簡化"


# ── Tailo invariants ──────────────────────────────────────────────────────

def test_tailo_reads_from_simplified_not_translated():
    """Critical invariant: tailo's input is simplified.json, not translated.json."""
    assert h.INPUT_PATHS["tailo"] == "simplified.json"
    assert h.INPUT_PATHS["tailo"] != h.INPUT_PATHS["translate"]


def test_stage_specific_checkpoints_prevent_cross_stage_contamination():
    """Tailo must not load translate's checkpoints (the root cause of bug #2)."""
    segs_tailo = [{"index": i, "text": f"simplified{i}"} for i in range(7)]
    segs_translate = [{"index": i, "text": f"english{i}"} for i in range(7)]
    # Pre-seed translate's checkpoint with stale data that looks valid
    _save_stage_checkpoint("translate", 0, {
        "batch_id": 0, "start": 0, "count": 7,
        "translations": [f"舊譯{i}" for i in range(7)],
    })
    # Tailo should NOT find translate's checkpoint — it should re-translate
    _response_queue.append("\n".join(f"[{i+1}] 臺羅{i}" for i in range(7)))
    translations, stats = h.run_segment_pipeline(segs_tailo, stage="tailo")
    assert translations == [f"臺羅{i}" for i in range(7)]
    # Verify a translate call actually happened (it wasn't skipped)
    assert len(_translate_calls) == 1
    # Translate's checkpoint should be untouched
    ckpt = _load_stage_checkpoint("translate", 0)
    assert ckpt is not None
    assert ckpt["translations"][0] == "舊譯0"


# ── run_stage dispatcher ────────────────────────────────────────────────

def test_run_stage_translate_calls_segment_pipeline():
    """run_stage('translate') should invoke run_segment_pipeline (not chapter)."""
    segs = [{"index": i, "text": f"s{i}"} for i in range(3)]
    _write_temp_json(h.INPUT_PATHS["translate"], segs)
    _response_queue.append("[1] 甲\n[2] 乙\n[3] 丙")
    h.run_stage("translate")
    # run_stage writes the consolidated output
    consolidated = _read_temp_json(h.OUTPUT_PATHS["translate"])
    assert len(consolidated) == 3
    assert consolidated[0]["translated"] == "甲"


def test_run_stage_simplify_calls_new_pipeline():
    """run_stage('simplify') should invoke run_simplify_pipeline + split_simplified_chapters."""
    # Pre-write translated.json (simplify's input)
    translated = [
        {"index": 0, "source": "src-a", "translated": "這是第一句。", "chapter_index": 0, "chapter_title": "Ch1"},
    ]
    _write_temp_json(h.INPUT_PATHS["simplify"], translated)
    # Pre-write source/chapters.json
    chapters = [
        {"index": 0, "title": "Ch1", "segment_start": 0, "segment_end": 1},
    ]
    _write_temp_json("source/chapters.json", chapters)
    _response_queue.append("簡化全文。")
    h.run_stage("simplify")
    # simplified_chapters.json should be written
    ch_data = _read_temp_json(h.SIMPLIFIED_CHAPTERS_OUTPUT_PATH)
    assert len(ch_data) == 1
    assert ch_data[0]["text"] == "簡化全文。"
    # simplified.json (per-segment) should also be written
    seg_data = _read_temp_json(h.OUTPUT_PATHS["simplify"])
    assert len(seg_data) >= 1


def test_run_stage_tailo_calls_segment_pipeline():
    """run_stage('tailo') should use segment pipeline (each segment independent)."""
    segs = [{"index": i, "text": f"s{i}"} for i in range(3)]
    _write_temp_json(h.INPUT_PATHS["tailo"], segs)
    _response_queue.append("[1] 甲\n[2] 乙\n[3] 丙")
    h.run_stage("tailo")
    consolidated = _read_temp_json(h.OUTPUT_PATHS["tailo"])
    assert len(consolidated) == 3
    assert consolidated[0]["translated"] == "甲"


def test_run_stage_invalid_stage_raises():
    with pytest.raises(ValueError, match="stage must be one of"):
        h.run_stage("bogus")
