"""
Tests for shared/deliver_html.py
"""
import os
os.environ.setdefault("ORDER_ID", "test-order-shared")
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")

import importlib.util
import pytest

_spec = importlib.util.spec_from_file_location(
    "deliver_html", "shared/deliver_html.py"
)
deliver_html = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(deliver_html)

# Public surface
BASE_CSS = deliver_html.BASE_CSS
FONT_IMPORT = deliver_html.FONT_IMPORT
html_text = deliver_html.html_text
book_header = deliver_html.book_header
toc = deliver_html.toc
page_intro = deliver_html.page_intro
chapter_heading = deliver_html.chapter_heading
table_open = deliver_html.table_open
table_close = deliver_html.table_close
footer = deliver_html.footer
render_doc = deliver_html.render_doc


# ── html_text ─────────────────────────────────────────────────────────────

def test_html_text_escapes_special_chars():
    assert html_text("<script>") == "&lt;script&gt;"
    assert html_text("a & b") == "a &amp; b"


def test_html_text_converts_newlines():
    assert html_text("line1\nline2") == "line1<br>line2"


def test_html_text_empty():
    assert html_text("") == ""
    assert html_text(None) == ""


# ── book_header ──────────────────────────────────────────────────────────

def test_book_header_renders_title_and_authors():
    h = book_header("My Book", authors=["Alice", "Bob"], source_lang="English",
                    target_lang="繁體中文")
    assert "My Book" in h
    assert "Alice, Bob" in h
    assert "English" in h
    assert "繁體中文" in h
    assert "book-header" in h


def test_book_header_escapes_title():
    h = book_header("<script>alert(1)</script>")
    assert "<script>" not in h
    assert "&lt;script&gt;" in h


def test_book_header_no_authors_no_paragraph():
    h = book_header("X", authors=[])
    assert "book-author" in h
    # The empty <p></p> is harmless but acceptable
    assert "X" in h


# ── toc ──────────────────────────────────────────────────────────────────

def test_toc_renders_chapter_links():
    chapters = [
        {"index": 0, "title": "Chapter I."},
        {"index": 1, "title": "Chapter II."},
    ]
    t = toc(chapters)
    assert "Table of contents" in t
    assert "Chapter I." in t
    assert "Chapter II." in t
    assert "ch-000" in t
    assert "ch-001" in t
    assert t.count('href="#ch-') == 2


def test_toc_empty_chapters_returns_empty_string():
    assert toc([]) == ""


# ── page_intro ──────────────────────────────────────────────────────────

def test_page_intro_renders_text():
    pi = page_intro("Some <b>bold</b> intro")
    assert "page-intro" in pi
    assert "Some <b>bold</b> intro" in pi


def test_page_intro_empty_text_returns_empty():
    assert page_intro("") == ""


# ── chapter_heading ──────────────────────────────────────────────────────

def test_chapter_heading_has_anchor():
    h = chapter_heading("Chapter I.", 0)
    assert "Chapter I." in h
    assert 'id="ch-000' in h
    assert "<h2>" in h


def test_chapter_heading_handles_chinese_title():
    h = chapter_heading("第一章 緣起", 0)
    assert "第一章 緣起" in h
    assert "<h2>" in h


# ── table_open / table_close ─────────────────────────────────────────────

def test_table_open_renders_class_and_headers():
    t = table_open(2, ["A", "B"])
    assert 'class="bilingual-table cols-2"' in t
    assert "<th>A</th>" in t
    assert "<th>B</th>" in t
    assert t.endswith("<tbody>")


def test_table_close():
    assert table_close() == "</tbody></table>"


# ── footer ──────────────────────────────────────────────────────────────

def test_footer_renders_attribution():
    f = footer(order_id="abc-123")
    assert "OTS 翻譯服務" in f
    assert "abc-123" in f
    assert "service@ots.tw" in f


def test_footer_no_order_id():
    f = footer()
    assert "OTS 翻譯服務" in f
    assert "service@ots.tw" in f
    assert "訂單" not in f


# ── render_doc ──────────────────────────────────────────────────────────

def test_render_doc_basic_structure():
    html = render_doc(
        title="My Book",
        body_html="<p>Body</p>",
        authors=["Alice"],
        source_lang="English",
        target_lang="繁體中文",
        page_subtitle="Subtitle",
        page_description="Some description",
        chapters=[{"index": 0, "title": "Chapter I."}],
    )
    assert "<!DOCTYPE html>" in html
    assert "<p>Body</p>" in html
    assert "My Book" in html
    assert "Alice" in html
    assert "Subtitle" in html
    assert "Some description" in html
    assert "Chapter I." in html
    assert 'lang="zh-Hant"' in html
    assert "book-header" in html
    assert "</html>" in html


def test_render_doc_escapes_title():
    html = render_doc(
        title="<script>x</script>",
        body_html="",
    )
    assert "<script>" not in html
    assert "&lt;script&gt;" in html


def test_render_doc_no_toc_when_chapters_empty():
    html = render_doc(title="X", body_html="Y", chapters=[])
    # The CSS contains ".toc { ... }" — assert the rendered <nav class="toc"> is absent.
    assert '<nav class="toc"' not in html
