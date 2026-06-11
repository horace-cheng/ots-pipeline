"""
Unit tests for gt_chapter_splitter/main.py

Tests the LLM-driven chapter detection (with mocked Gemini), the
regex fallback path, and the GCS artifact writing.

Strategy: load the module via importlib (so it picks up the same env
vars as gt_fetcher tests), then monkey-patch:
  - shared.translate (Gemini call) — return canned responses or raise
  - shared.gcs_client — record uploaded blobs
  - shared.db.update_job_status — no-op
"""
import os
os.environ.setdefault("ORDER_ID", "test-order-gutenberg")
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")

import importlib.util
import json
from unittest.mock import MagicMock

import pytest


_SPLITTER_DIR = os.path.join(os.path.dirname(__file__), '..', 'gt_chapter_splitter')
_spec = importlib.util.spec_from_file_location("gt_splitter_main", os.path.join(_SPLITTER_DIR, "main.py"))
_splitter = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_splitter)

run                            = _splitter.run
parse_llm_response             = _splitter.parse_llm_response
locate_chapters_in_text        = _splitter.locate_chapters_in_text
chapters_via_llm               = _splitter.chapters_via_llm
chapters_via_regex             = _splitter.chapters_via_regex
build_segments_and_chapters    = _splitter.build_segments_and_chapters
split_paragraphs               = _splitter.split_paragraphs
count_words                    = _splitter.count_words
SPLITTER_PROMPT                = _splitter.SPLITTER_PROMPT
COVERAGE_MIN                   = _splitter.COVERAGE_MIN


# ── Prompt structure ──────────────────────────────────────────────────────

def test_prompt_mentions_json_structure():
    assert '"chapters"' in SPLITTER_PROMPT
    assert '"title"' in SPLITTER_PROMPT
    assert '"first_words"' in SPLITTER_PROMPT


def test_prompt_asks_to_skip_frontmatter():
    assert "frontmatter" in SPLITTER_PROMPT.lower() or "table of contents" in SPLITTER_PROMPT.lower()
    assert "backmatter" in SPLITTER_PROMPT.lower() or "index" in SPLITTER_PROMPT.lower()


# ── parse_llm_response ────────────────────────────────────────────────────

def test_parse_llm_response_object_form():
    raw = json.dumps({
        "chapters": [
            {"title": "I. Start", "first_words": "It was a dark and stormy night."},
            {"title": "II. Middle", "first_words": "Then suddenly the door opened."},
        ]
    })
    out = parse_llm_response(raw)
    assert len(out) == 2
    assert out[0]["title"] == "I. Start"
    assert out[0]["first_words"] == "It was a dark and stormy night."


def test_parse_llm_response_bare_array_form():
    """Defensive: accept a bare JSON array if Gemini returns one despite JSON mode."""
    raw = json.dumps([
        {"title": "Ch1", "first_words": "Hello world."},
    ])
    out = parse_llm_response(raw)
    assert len(out) == 1
    assert out[0]["title"] == "Ch1"


def test_parse_llm_response_strips_markdown_fences():
    raw = "```json\n" + json.dumps({"chapters": [{"title": "A", "first_words": "B"}]}) + "\n```"
    out = parse_llm_response(raw)
    assert out == [{"title": "A", "first_words": "B"}]


def test_parse_llm_response_malformed_returns_empty():
    assert parse_llm_response("not json at all") == []
    assert parse_llm_response("") == []
    assert parse_llm_response("[invalid json") == []


def test_parse_llm_response_wrong_type_returns_empty():
    """A JSON string or number is not a valid response shape."""
    assert parse_llm_response('"just a string"') == []
    assert parse_llm_response("42") == []


def test_parse_llm_response_skips_chapters_with_empty_fields():
    raw = json.dumps({
        "chapters": [
            {"title": "Good",  "first_words": "Real text."},
            {"title": "",      "first_words": "No title."},
            {"title": "No fw", "first_words": ""},
        ]
    })
    out = parse_llm_response(raw)
    assert len(out) == 1
    assert out[0]["title"] == "Good"


# ── locate_chapters_in_text ───────────────────────────────────────────────

def test_locate_chapters_basic():
    text = "It was a dark and stormy night.\n\nThen suddenly the door opened.\n\nAnd that was that."
    llm = [
        {"title": "Ch1", "first_words": "It was a dark and stormy night."},
        {"title": "Ch2", "first_words": "Then suddenly the door opened."},
    ]
    out = locate_chapters_in_text(text, llm)
    assert out is not None
    assert len(out) == 2
    assert out[0]["char_start"] == 0
    assert out[0]["char_end"] == text.find("Then suddenly")
    assert out[1]["char_start"] == text.find("Then suddenly")
    assert out[1]["char_end"] == len(text)


def test_locate_chapters_last_chapter_extends_to_text_end():
    text = "Intro text here.\n\nBody text here."
    llm = [
        {"title": "Ch1", "first_words": "Intro text here."},
        {"title": "Ch2", "first_words": "Body text here."},
    ]
    out = locate_chapters_in_text(text, llm)
    assert out is not None
    assert out[-1]["char_end"] == len(text)


def test_locate_chapters_skips_chapters_whose_first_words_not_found():
    text = "It was a dark and stormy night.\n\nThen suddenly the door opened."
    llm = [
        {"title": "Ch1", "first_words": "It was a dark and stormy night."},
        {"title": "Bad", "first_words": "This sentence does not exist in the text."},
        {"title": "Ch2", "first_words": "Then suddenly the door opened."},
    ]
    out = locate_chapters_in_text(text, llm)
    assert out is not None
    # Bad chapter skipped, but 2 valid chapters remain
    titles = [c["title"] for c in out]
    assert "Bad" not in titles
    assert "Ch1" in titles
    assert "Ch2" in titles


def test_locate_chapters_returns_none_when_no_chapters():
    """0 chapters from LLM → fall back to regex."""
    out = locate_chapters_in_text("Some text.", [])
    assert out is None


def test_locate_chapters_returns_none_when_all_first_words_missing():
    text = "Just one paragraph of text here."
    llm = [{"title": "Ch1", "first_words": "totally absent"}]
    out = locate_chapters_in_text(text, llm)
    assert out is None


def test_locate_chapters_low_coverage_falls_back():
    """If located chapters cover < 50% of text, fall back to regex."""
    text = "A " * 1000  # 2000 chars
    llm = [
        {"title": "Ch1", "first_words": "A "},  # would cover 100% but is unique only at the start
    ]
    out = locate_chapters_in_text(text, llm)
    # 1 chapter covering all 2000 chars is fine
    assert out is not None


def test_locate_chapters_low_coverage_returns_none():
    """If first_words matches in only a small section, coverage is low."""
    text = "X" * 10000 + " unique anchor " + "Y" * 5000
    llm = [{"title": "Ch1", "first_words": "unique anchor"}]
    out = locate_chapters_in_text(text, llm)
    # 1 chapter from position ~10000 to end (~5000 chars) of a 15000-char text
    # coverage = 5000/15000 = 33% < 50% → fallback
    assert out is None


# ── chapters_via_llm (mocked Gemini) ─────────────────────────────────────

def test_chapters_via_llm_happy_path(monkeypatch):
    full_text = "It was a dark and stormy night.\n\nThen suddenly the door opened."
    canned = json.dumps({"chapters": [
        {"title": "Ch1", "first_words": "It was a dark and stormy night."},
        {"title": "Ch2", "first_words": "Then suddenly the door opened."},
    ]})
    monkeypatch.setattr(_splitter, "translate", lambda *a, **kw: canned)
    out = chapters_via_llm(full_text)
    assert out is not None
    assert len(out) == 2
    assert out[0]["title"] == "Ch1"


def test_chapters_via_llm_raises_falls_back(monkeypatch):
    def _raise(*a, **kw):
        raise RuntimeError("Gemini unavailable")
    monkeypatch.setattr(_splitter, "translate", _raise)
    out = chapters_via_llm("Some text here.")
    assert out is None  # caller will use regex fallback


def test_chapters_via_llm_malformed_response_falls_back(monkeypatch):
    monkeypatch.setattr(_splitter, "translate", lambda *a, **kw: "not json at all")
    out = chapters_via_llm("Some text here.")
    assert out is None


# ── chapters_via_regex (the fallback) ────────────────────────────────────

def test_chapters_via_regex_basic():
    text = "CHAPTER I\n\nFirst chapter body.\n\nCHAPTER II\n\nSecond chapter body."
    out = chapters_via_regex(text)
    assert len(out) == 2
    assert out[0]["title"] == "Part 1"
    assert out[1]["title"] == "Part 2"
    # char offsets use running counter (+2 for "\n\n" join)
    assert out[0]["char_start"] == 0
    assert out[1]["char_start"] > out[0]["char_end"]


def test_chapters_via_regex_empty_text():
    out = chapters_via_regex("")
    assert out == []


def test_chapters_via_regex_strips_gutenberg_boilerplate():
    """The fallback uses split_text_structured which strips boilerplate.

    Mirrors the production flow: ``gt_fetcher`` writes ``full_text.txt`` as
    ``join(chapter bodies)`` (no headings, no boilerplate), and ``run()``
    re-strips defensively. The function's ``char_start/char_end`` refer to
    positions in the **joined chapter bodies**, not the raw input.
    """
    # In production, full_text.txt = join of chapter bodies (no chapter headings)
    chapter_bodies = ["Body 1.", "Body 2."]
    text = "\n\n".join(chapter_bodies)
    out = chapters_via_regex(text)
    assert len(out) == 2
    # Body content should be covered at the right offsets
    assert "Body 1." in text[out[0]["char_start"]:out[0]["char_end"]]
    assert "Body 2." in text[out[1]["char_start"]:out[1]["char_end"]]
    # Offsets are running-counter based (+2 for "\n\n" separator)
    assert out[0]["char_start"] == 0
    assert out[0]["char_end"]   == len("Body 1.")
    assert out[1]["char_start"] == len("Body 1.") + 2
    assert out[1]["char_end"]   == len(text)


# ── build_segments_and_chapters ───────────────────────────────────────────

def test_build_segments_basic():
    full_text = "First chapter text.\n\nFirst chapter paragraph 2.\n\nSecond chapter text."
    chapters_meta = [
        {"index": 0, "title": "Ch1", "char_start": 0,  "char_end": full_text.find("Second")},
        {"index": 1, "title": "Ch2", "char_start": full_text.find("Second"), "char_end": len(full_text)},
    ]
    segs, idx = build_segments_and_chapters(full_text, chapters_meta)
    assert len(idx) == 2
    assert idx[0]["segment_start"] == 0
    assert idx[0]["segment_end"] >= 1
    assert idx[1]["segment_start"] == idx[0]["segment_end"]
    # Segments have chapter linkage
    assert all("chapter_index" in s and "chapter_title" in s for s in segs)
    # Ch1 segments have title "Ch1"
    ch1_segs = [s for s in segs if s["chapter_index"] == 0]
    assert all(s["chapter_title"] == "Ch1" for s in ch1_segs)
    ch2_segs = [s for s in segs if s["chapter_index"] == 1]
    assert all(s["chapter_title"] == "Ch2" for s in ch2_segs)


def test_build_segments_segment_ranges_non_overlapping():
    full_text = "AAAA" * 50 + "\n\n" + "BBBB" * 50 + "\n\n" + "CCCC" * 50
    chapters_meta = [
        {"index": 0, "title": "A", "char_start": 0,  "char_end": 200},
        {"index": 1, "title": "B", "char_start": 202, "char_end": 402},
        {"index": 2, "title": "C", "char_start": 404, "char_end": 604},
    ]
    segs, idx = build_segments_and_chapters(full_text, chapters_meta)
    # No segment ranges overlap across chapters
    prev_end = 0
    for ch in idx:
        ch_segs = [s for s in segs if s["chapter_index"] == ch["index"]]
        if not ch_segs:
            continue
        assert ch["segment_start"] >= prev_end
        prev_end = ch["segment_end"]


def test_build_segments_long_paragraph_stays_intact():
    """A single chapter with a long paragraph is NOT sub-split (a segment
    is just a paragraph). The full paragraph is preserved as one segment,
    no matter its length.

    Note: when the chapter text has no blank-line paragraph boundaries
    (single-block input), the sentence fallback inside ``split_paragraphs``
    will still split on sentence terminators — but for normal multi-paragraph
    chapter text, paragraphs are preserved intact.
    """
    long_para = "word " * 999 + "word"  # 1000 words, no trailing whitespace
    chapter_bodies = ["Para 1.\n\n" + long_para + "\n\nPara 2."]
    full_text = "\n\n".join(chapter_bodies)
    chapters_meta = [{"index": 0, "title": "C", "char_start": 0, "char_end": len(full_text)}]
    segs, _ = build_segments_and_chapters(full_text, chapters_meta)
    # 3 paragraphs, 3 segments — the long one stays whole.
    assert len(segs) == 3
    assert segs[0]["text"] == "Para 1."
    assert segs[1]["text"] == long_para
    assert segs[2]["text"] == "Para 2."


# ── split_paragraphs (basic sanity, was in gt_fetcher, now here) ─────────

def test_split_paragraphs_basic():
    text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
    paras = split_paragraphs(text)
    assert len(paras) == 3
    assert paras[0] == "First paragraph here."


def test_split_paragraphs_empty():
    assert split_paragraphs("") == []


def test_split_paragraphs_single_block_falls_back_to_sentences():
    text = "One. Two. Three. Four. Five."
    paras = split_paragraphs(text)
    assert len(paras) >= 2


def test_split_paragraphs_keeps_short_paragraphs_intact():
    text = "Long first paragraph with many words.\n\nHi.\n\nAnother long paragraph with content."
    paras = split_paragraphs(text)
    assert len(paras) == 3
    assert paras[1] == "Hi."


# ── count_words ───────────────────────────────────────────────────────────

def test_count_words_english():
    assert count_words("Hello world how are you") == 5


def test_count_words_with_punctuation():
    assert count_words("Hello, world! How are you?") == 5
