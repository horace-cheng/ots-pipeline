"""
Unit tests for gt_deliver/main.py (v2 — segment-based, multiple side-by-side HTMLs)
"""
import os
os.environ.setdefault("ORDER_ID", "test-order-gutenberg")
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")

import pytest
import importlib.util

_gt_dir = os.path.join(os.path.dirname(__file__), '..', 'gt_deliver')
_spec = importlib.util.spec_from_file_location("gt_deliver_main", os.path.join(_gt_dir, "main.py"))
_gt_deliver = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gt_deliver)

format_source_vs_chinese = _gt_deliver.format_source_vs_chinese
format_simplified_vs_tailo = _gt_deliver.format_simplified_vs_tailo
format_simplified_reader    = _gt_deliver.format_simplified_reader
format_full_vs_simplified   = _gt_deliver.format_full_vs_simplified
read_chapters            = _gt_deliver.read_chapters
BASE_CSS                 = _gt_deliver.BASE_CSS
FONT_IMPORT              = _gt_deliver.FONT_IMPORT


# ── Fixtures ──────────────────────────────────────────────────────────────

SAMPLE_METADATA = {
    "title":    "Pride and Prejudice",
    "authors":  ["Jane Austen"],
    "language": "en",
    "book_id":  1342,
}

SAMPLE_SEGMENTS = [
    {"index": 0, "text": "It is a truth universally acknowledged.", "chapter_index": 0, "chapter_title": "Chapter I."},
    {"index": 1, "text": "However little known the feelings or views of such a man may be.", "chapter_index": 0, "chapter_title": "Chapter I."},
    {"index": 2, "text": "Chapter two opening line.", "chapter_index": 1, "chapter_title": "Chapter II."},
    {"index": 3, "text": "Another sentence in chapter two.", "chapter_index": 1, "chapter_title": "Chapter II."},
]

SAMPLE_CHAPTERS = [
    {"index": 0, "title": "Chapter I.",  "char_start": 0, "char_end": 100,
     "char_count": 100, "segment_start": 0, "segment_end": 2},
    {"index": 1, "title": "Chapter II.", "char_start": 102, "char_end": 200,
     "char_count": 98, "segment_start": 2, "segment_end": 4},
]

SAMPLE_TRANSLATED = [
    {"index": 0, "source": SAMPLE_SEGMENTS[0]["text"], "translated": "凡是有錢的單身漢，總想娶位太太。", "chapter_index": 0, "chapter_title": "Chapter I."},
    {"index": 1, "source": SAMPLE_SEGMENTS[1]["text"], "translated": "這是一句大家公認的真理。",        "chapter_index": 0, "chapter_title": "Chapter I."},
    {"index": 2, "source": SAMPLE_SEGMENTS[2]["text"], "translated": "第二章開頭。",                  "chapter_index": 1, "chapter_title": "Chapter II."},
    {"index": 3, "source": SAMPLE_SEGMENTS[3]["text"], "translated": "第二章另一句。",                "chapter_index": 1, "chapter_title": "Chapter II."},
]

SAMPLE_SIMPLIFIED = [
    {"index": 0, "source": SAMPLE_SEGMENTS[0]["text"], "translated": "有錢的單身漢都想結婚。", "chapter_index": 0, "chapter_title": "Chapter I."},
    {"index": 1, "source": SAMPLE_SEGMENTS[1]["text"], "translated": "大家都知道這是真的。",   "chapter_index": 0, "chapter_title": "Chapter I."},
    {"index": 2, "source": SAMPLE_SEGMENTS[2]["text"], "translated": "第二章開始。",         "chapter_index": 1, "chapter_title": "Chapter II."},
    {"index": 3, "source": SAMPLE_SEGMENTS[3]["text"], "translated": "第二章的另一句話。",   "chapter_index": 1, "chapter_title": "Chapter II."},
]

SAMPLE_TAILO = [
    {"index": 0, "source": SAMPLE_SEGMENTS[0]["text"], "translated": "有錢的單身漢 (Ū-chîⁿ ê toan-seng-han)", "chapter_index": 0, "chapter_title": "Chapter I."},
    {"index": 1, "source": SAMPLE_SEGMENTS[1]["text"], "translated": "大家都知道 (Ta̍k-ke lóng chai-iáⁿ)",      "chapter_index": 0, "chapter_title": "Chapter I."},
    {"index": 2, "source": SAMPLE_SEGMENTS[2]["text"], "translated": "第二章 (Tē-jī tsiu)",                   "chapter_index": 1, "chapter_title": "Chapter II."},
    {"index": 3, "source": SAMPLE_SEGMENTS[3]["text"], "translated": "另一句 (Līng tsi̍t kù)",                  "chapter_index": 1, "chapter_title": "Chapter II."},
]

SAMPLE_SIMPLIFIED_CHAPTERS = [
    {"chapter_index": 0, "title": "Chapter I.",  "text": "有錢的單身漢都想結婚。\n\n大家都知道這是真的。"},
    {"chapter_index": 1, "title": "Chapter II.", "text": "第二章開始了。\n\n這是第二章的另一句話。"},
]


@pytest.fixture(autouse=True)
def stub_chapters(monkeypatch):
    """All tests need read_chapters() to return the sample chapters."""
    monkeypatch.setattr(_gt_deliver, "read_chapters", lambda: SAMPLE_CHAPTERS)


# ── format_source_vs_chinese ─────────────────────────────────────────────

def test_source_vs_chinese_basic_structure():
    html = format_source_vs_chinese(SAMPLE_SEGMENTS, SAMPLE_TRANSLATED, SAMPLE_METADATA)
    assert "<!DOCTYPE html>" in html
    assert "Pride and Prejudice" in html
    assert "Jane Austen" in html
    assert "bilingual-table" in html
    assert "</html>" in html


def test_source_vs_chinese_has_chapter_headings():
    html = format_source_vs_chinese(SAMPLE_SEGMENTS, SAMPLE_TRANSLATED, SAMPLE_METADATA)
    assert "Chapter I." in html
    assert "Chapter II." in html
    assert html.count("<h2>") == 2


def test_source_vs_chinese_contains_source_and_translation_pairs():
    html = format_source_vs_chinese(SAMPLE_SEGMENTS, SAMPLE_TRANSLATED, SAMPLE_METADATA)
    assert "It is a truth universally acknowledged." in html
    assert "凡是有錢的單身漢" in html


def test_source_vs_chinese_columns_have_chinese_headers():
    html = format_source_vs_chinese(SAMPLE_SEGMENTS, SAMPLE_TRANSLATED, SAMPLE_METADATA)
    assert "原文" in html
    assert "標準翻譯" in html


def test_source_vs_chinese_escapes_metadata_title():
    html = format_source_vs_chinese(SAMPLE_SEGMENTS, SAMPLE_TRANSLATED,
                                     {"title": "<script>alert(1)</script>",
                                      "authors": []})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_source_vs_chinese_handles_missing_translation():
    segments = [{"index": 0, "text": "src"}]
    translated = [{"index": 0, "translated": ""}]
    html = format_source_vs_chinese(segments, translated, {"title": "X", "authors": []})
    assert "src" in html
    assert "<!DOCTYPE html>" in html


def test_source_vs_chinese_works_without_chapters(monkeypatch):
    """If source/chapters.json is missing, render each segment as its own block."""
    monkeypatch.setattr(_gt_deliver, "read_chapters", lambda: [])
    html = format_source_vs_chinese(SAMPLE_SEGMENTS, SAMPLE_TRANSLATED, SAMPLE_METADATA)
    assert html.count("<h2>") == 4
    assert "凡是有錢的單身漢" in html


# ── format_simplified_vs_tailo ──────────────────────────────────────────

def test_simplified_vs_tailo_basic_structure():
    html = format_simplified_vs_tailo(SAMPLE_SIMPLIFIED, SAMPLE_TAILO, SAMPLE_METADATA)
    assert "<!DOCTYPE html>" in html
    assert "Pride and Prejudice" in html
    assert "comparison-chapter" in html


def test_simplified_vs_tailo_has_chapter_headings():
    html = format_simplified_vs_tailo(SAMPLE_SIMPLIFIED, SAMPLE_TAILO, SAMPLE_METADATA)
    assert html.count("<h2>") == 2


def test_simplified_vs_tailo_contains_both_versions():
    html = format_simplified_vs_tailo(SAMPLE_SIMPLIFIED, SAMPLE_TAILO, SAMPLE_METADATA)
    assert "有錢的單身漢都想結婚" in html  # simplified
    assert "Ū-chîⁿ ê toan-seng-han" in html  # tailo


def test_simplified_vs_tailo_columns_have_chinese_headers():
    html = format_simplified_vs_tailo(SAMPLE_SIMPLIFIED, SAMPLE_TAILO, SAMPLE_METADATA)
    assert "青少年版" in html
    assert "台羅版" in html


def test_simplified_vs_tailo_works_without_chapters(monkeypatch):
    monkeypatch.setattr(_gt_deliver, "read_chapters", lambda: [])
    html = format_simplified_vs_tailo(SAMPLE_SIMPLIFIED, SAMPLE_TAILO, SAMPLE_METADATA)
    assert html.count("<h2>") == 4
    assert "Ū-chîⁿ" in html


def test_simplified_vs_tailo_handles_mismatched_lengths():
    """If tailo is shorter, missing segments render as empty cells."""
    short_tailo = [SAMPLE_TAILO[0]]
    html = format_simplified_vs_tailo(SAMPLE_SIMPLIFIED, short_tailo, SAMPLE_METADATA)
    assert "<!DOCTYPE html>" in html
    assert "Ū-chîⁿ" in html


# ── format_simplified_reader ─────────────────────────────────────────────

def test_simplified_reader_basic_structure():
    html = format_simplified_reader(SAMPLE_SIMPLIFIED_CHAPTERS, SAMPLE_METADATA)
    assert "<!DOCTYPE html>" in html
    assert "Pride and Prejudice" in html
    assert "reader-section" in html
    assert html.count("<h2>") == 2


def test_simplified_reader_contains_chapter_text():
    html = format_simplified_reader(SAMPLE_SIMPLIFIED_CHAPTERS, SAMPLE_METADATA)
    assert "有錢的單身漢都想結婚" in html
    assert "第二章開始了" in html


def test_simplified_reader_handles_empty_text():
    chapters = [{"chapter_index": 0, "title": "Ch1", "text": ""}]
    html = format_simplified_reader(chapters, {"title": "X", "authors": []})
    assert "<!DOCTYPE html>" in html
    assert "Ch1" in html


# ── format_full_vs_simplified ────────────────────────────────────────────

def test_full_vs_simplified_basic_structure():
    html = format_full_vs_simplified(
        SAMPLE_TRANSLATED, SAMPLE_SIMPLIFIED_CHAPTERS, SAMPLE_METADATA,
    )
    assert "<!DOCTYPE html>" in html
    assert "Pride and Prejudice" in html
    assert "comparison-chapter" in html


def test_full_vs_simplified_contains_both_versions():
    html = format_full_vs_simplified(
        SAMPLE_TRANSLATED, SAMPLE_SIMPLIFIED_CHAPTERS, SAMPLE_METADATA,
    )
    assert "凡是有錢的單身漢" in html  # full translation
    assert "有錢的單身漢都想結婚" in html  # simplified


def test_full_vs_simplified_has_column_labels():
    html = format_full_vs_simplified(
        SAMPLE_TRANSLATED, SAMPLE_SIMPLIFIED_CHAPTERS, SAMPLE_METADATA,
    )
    assert "標準翻譯" in html
    assert "青少年版" in html


def test_full_vs_simplified_handles_empty_chapters():
    chapters = [{"chapter_index": 0, "title": "Ch1", "text": "", "segment_start": 0, "segment_end": 0}]
    html = format_full_vs_simplified(
        [{"index": 0, "translated": ""}], chapters, {"title": "X", "authors": []},
    )
    assert "<!DOCTYPE html>" in html


# ── read_chapters ────────────────────────────────────────────────────────

def test_read_chapters_returns_empty_on_missing(monkeypatch):
    def fake_read(name):
        raise FileNotFoundError(f"missing {name}")
    monkeypatch.setattr(_gt_deliver, "read_temp_json", fake_read)
    assert read_chapters() == []


def test_read_chapters_returns_empty_on_malformed(monkeypatch):
    monkeypatch.setattr(_gt_deliver, "read_temp_json", lambda name: {"not": "a list"})
    assert read_chapters() == []
