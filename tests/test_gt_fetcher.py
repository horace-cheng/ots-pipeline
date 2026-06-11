"""
Unit tests for gt_fetcher/main.py
Tests the book fetching (EPUB primary, text fallback), header parsing,
``split_text_structured`` (used by the splitter's regex fallback), and
GCS download-artifact writing logic with mocks.

Chapter detection and paragraph segmentation are tested in
``test_gt_chapter_splitter.py`` — ``gt_fetcher`` is download-only.
"""
import os
os.environ.setdefault("ORDER_ID", "test-order-gutenberg")
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")

import io
import json
import zipfile
from unittest.mock import AsyncMock, MagicMock, patch
import importlib.util
import pytest

_gt_dir = os.path.join(os.path.dirname(__file__), '..', 'gt_fetcher')
_spec = importlib.util.spec_from_file_location("gt_fetcher_main", os.path.join(_gt_dir, "main.py"))
_gt_fetcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_gt_fetcher)

fetch_book         = _gt_fetcher.fetch_book
fetch_book_text    = _gt_fetcher.fetch_book_text
fetch_book_metadata = _gt_fetcher.fetch_book_metadata
parse_header_metadata = _gt_fetcher.parse_header_metadata
split_text_structured = _gt_fetcher.split_text_structured
save_downloaded_text_to_gcs = _gt_fetcher.save_downloaded_text_to_gcs
TEXT_URL_PATTERNS    = _gt_fetcher.TEXT_URL_PATTERNS
_is_chapter_label    = _gt_fetcher._is_chapter_label
SOURCE_FULL_TEXT     = _gt_fetcher.SOURCE_FULL_TEXT


# ── split_text_structured (text fallback) ──────────────────────────────────

def test_split_by_chapters():
    text = "CHAPTER I. The Beginning\n\nIt was the best of times.\n\nCHAPTER II. The Middle\n\nIt was the worst of times."
    chunks = split_text_structured(text)
    assert len(chunks) >= 2
    assert any("best of times" in c for c in chunks)
    assert any("worst of times" in c for c in chunks)


def test_split_by_paragraphs_when_no_chapters():
    text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
    chunks = split_text_structured(text)
    assert len(chunks) == 3


def test_split_by_sentences_when_no_structure():
    text = "One. Two. Three. Four."
    chunks = split_text_structured(text)
    assert len(chunks) >= 3


def test_split_empty_text():
    chunks = split_text_structured("")
    assert chunks == []


def test_split_handles_indented_chapters():
    text = "                          CHAPTER I\n\nbody\n\n                          CHAPTER II\n\nbody"
    chunks = split_text_structured(text)
    assert len(chunks) == 2


def test_split_handles_crlf_line_endings():
    text = "CHAPTER I\r\n\r\nbody\r\n\r\nCHAPTER II\r\n\r\nbody\r\n"
    chunks = split_text_structured(text)
    assert len(chunks) == 2


def test_split_ignores_toc_entries():
    text = (
        "CONTENTS\n\nI. Heroisms\nII. Try Your Luck\n\n"
        "                          CHAPTER I\n\nBody.\n\n"
        "                          CHAPTER II\n\nMore body.\n"
    )
    chunks = split_text_structured(text)
    assert any("Body." in c for c in chunks)
    assert any("More body." in c for c in chunks)


def test_split_bare_roman_numerals_time_machine_style():
    """The Time Machine by H. G. Wells (Gutenberg #35) uses bare Roman
    numerals (I., II., …, XVI.) without the word "CHAPTER" — the old parser
    produced 387 "chapters" because it fell through to paragraph splitting
    and counted the Gutenberg license header."""
    text = (
        "*** START OF THE PROJECT GUTENBERG EBOOK THE TIME MACHINE ***\n"
        "Title: The Time Machine\nAuthor: H. G. Wells\n\n"
        "                          EBOOK THE TIME MACHINE ***\n"
        "                          The Time Machine\n"
        "                          An Invention\n"
        "                          by H. G. Wells\n"
        "\n"
        "                          CONTENTS\n"
        "\n"
        " I. Introduction\n II. The Machine\n III. The Time Traveller Returns\n"
        " IV. Time Travelling\n V. In the Golden Age\n"
        "\n"
        " I.\n"
        "\n"
        "Introduction\n\n"
        "The Time Traveller was expounding.\n"
        "\n"
        " II.\n"
        "\n"
        "The Machine\n\n"
        "He held a glittering metallic framework.\n"
        "\n"
        " III.\n"
        "\n"
        "The Time Traveller Returns\n\n"
        "He returned, pale and dirty.\n"
        "\n"
        " Epilogue\n"
        "\n"
        "One cannot choose but wonder.\n"
        "\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK THE TIME MACHINE ***\n"
    )
    chunks = split_text_structured(text)
    # Should be: title-page/TOC + 3 chapters + epilogue = 5
    # (NOT 387 paragraphs, NOT 30+ TOC+chapter splits).
    assert len(chunks) == 5, f"expected 5 chunks, got {len(chunks)}: {chunks[:3]}"
    # First chunk is the title page / TOC.
    assert "The Time Machine" in chunks[0]
    # Body of chapter I is in chunk 1.
    assert "The Time Traveller was expounding" in chunks[1]
    # Body of chapter III is in chunk 3.
    assert "He returned, pale and dirty" in chunks[3]
    # Epilogue is the last chunk.
    assert "One cannot choose but wonder" in chunks[4]


def test_split_does_not_split_on_few_roman_matches():
    """A handful of stray "I." or "II." lines in body text should NOT be
    treated as chapter markers — require at least 3 matches.

    This test checks the threshold: with 2 Roman-looking lines, we fall
    through to paragraph splitting (so "I." might appear in a chunk), but
    the chunk count should be small (< 5), not the number of matches + 1."""
    text = (
        "*** START OF THE PROJECT GUTENBERG EBOOK FOO ***\n"
        "Some prose.\n\n"
        "I.\n\n"
        "More prose, this one not Roman.\n\n"
        "Some dialogue:\n"
        "II.\n"
        "\"Really?\" said I.\n\n"
        "And then this happened.\n"
        "*** END OF THE PROJECT GUTENBERG EBOOK FOO ***\n"
    )
    chunks = split_text_structured(text)
    # With only 2 Roman-looking lines, fall through to paragraph split.
    # The chunk count should be the number of paragraphs (~5), not the
    # number of Roman matches (which would give 3).
    assert 3 <= len(chunks) <= 5
    # Body content should still be present.
    assert any("More prose" in c for c in chunks)
    assert any("And then this happened" in c for c in chunks)


def test_split_strips_gutenberg_boilerplate():
    """The license header and footer (before *** START / after *** END)
    should be excluded from the chunk count."""
    text = (
        "Long Project Gutenberg license preamble.\n" * 50
        + "*** START OF THE PROJECT GUTENBERG EBOOK FOO ***\n"
        + "CHAPTER I\n\nBody of chapter one.\n\n"
        + "CHAPTER II\n\nBody of chapter two.\n"
        + "*** END OF THE PROJECT GUTENBERG EBOOK FOO ***\n"
        + "Long Project Gutenberg license postamble.\n" * 50
    )
    chunks = split_text_structured(text)
    # Should be exactly 2 (the two chapters) — no license text leaks in.
    assert len(chunks) == 2
    assert "Body of chapter one" in chunks[0]
    assert "Body of chapter two" in chunks[1]


# ── count_words ────────────────────────────────────────────────────────────
# count_words tests moved to test_gt_chapter_splitter.py — the splitter is
# the only consumer of count_words now.

# ── parse_header_metadata ───────────────────────────────────────────────────

PG_HEADER_SAMPLE = """The Project Gutenberg eBook of Pride and Prejudice

Title: Pride and Prejudice

Author: Jane Austen

Release Date: June, 1998 [EBook #1342]
Language: English

*** START OF THE PROJECT GUTENBERG EBOOK PRIDE AND PREJUDICE ***

CHAPTER I.
"""


def test_parse_header_metadata_full():
    meta = parse_header_metadata(PG_HEADER_SAMPLE, fallback_book_id=1342)
    assert meta["title"] == "Pride and Prejudice"
    assert meta["authors"] == ["Jane Austen"]
    assert meta["language"] == "en"


def test_parse_header_metadata_multiple_authors():
    text = "Title: Test\nAuthor: A. Author, B. Author\nLanguage: French\n"
    meta = parse_header_metadata(text, fallback_book_id=1)
    assert meta["authors"] == ["A. Author", "B. Author"]
    assert meta["language"] == "fr"


def test_parse_header_metadata_author_with_translator():
    text = "Title: Les Misérables\nAuthor: Victor Hugo, Isabel F. Hapgood (Translator)\nLanguage: French\n"
    meta = parse_header_metadata(text, fallback_book_id=1)
    assert meta["authors"] == ["Victor Hugo", "Isabel F. Hapgood (Translator)"]


def test_parse_header_metadata_missing_uses_fallback():
    meta = parse_header_metadata("Just some body, no header.", fallback_book_id=42)
    assert meta["title"] == "Gutenberg Book 42"
    assert meta["language"] == "en"


# ── fetch_book_text (plain text fallback) ──────────────────────────────────

def _ok_response(text: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.text = text
    resp.raise_for_status = MagicMock()
    return resp


def _not_found_response() -> MagicMock:
    resp = MagicMock()
    resp.status_code = 404
    resp.raise_for_status = MagicMock(side_effect=Exception("404"))
    return resp


@pytest.mark.asyncio
async def test_fetch_book_text_first_pattern_succeeds():
    with patch('httpx.AsyncClient') as MockClient:
        mock_instance = AsyncMock()
        MockClient.return_value.__aenter__.return_value = mock_instance
        mock_instance.get = AsyncMock(return_value=_ok_response("body"))
        result = await fetch_book_text(1342)
        assert result == "body"
        assert mock_instance.get.call_count == 1


@pytest.mark.asyncio
async def test_fetch_book_text_falls_back_to_next_pattern():
    with patch('httpx.AsyncClient') as MockClient:
        mock_instance = AsyncMock()
        MockClient.return_value.__aenter__.return_value = mock_instance
        mock_instance.get = AsyncMock(side_effect=[_not_found_response(), _ok_response("body2")])
        result = await fetch_book_text(1342)
        assert result == "body2"
        assert mock_instance.get.call_count == 2


@pytest.mark.asyncio
async def test_fetch_book_text_all_patterns_404_raises():
    with patch('httpx.AsyncClient') as MockClient:
        mock_instance = AsyncMock()
        MockClient.return_value.__aenter__.return_value = mock_instance
        mock_instance.get = AsyncMock(return_value=_not_found_response())
        with pytest.raises(ValueError, match="No text file found"):
            await fetch_book_text(9999)
        assert mock_instance.get.call_count == len(TEXT_URL_PATTERNS)


@pytest.mark.asyncio
async def test_fetch_book_text_uses_follow_redirects():
    captured_kwargs: dict = {}
    with patch('httpx.AsyncClient') as MockClient:
        mock_instance = AsyncMock()
        mock_instance.get = AsyncMock(return_value=_ok_response("body"))
        MockClient.return_value.__aenter__.return_value = mock_instance
        original_init = MockClient.side_effect
        def capturing(*a, **kw):
            captured_kwargs.update(kw)
            return MagicMock(
                __aenter__=AsyncMock(return_value=mock_instance),
                __aexit__=AsyncMock(return_value=False),
            )
        MockClient.side_effect = capturing
        await fetch_book_text(1342)
    assert captured_kwargs.get("follow_redirects") is True


# ── Chapter label classification ───────────────────────────────────────────

def test_is_chapter_label_chapter_with_title():
    assert _is_chapter_label("CHAPTER I. The Beginning") is True
    assert _is_chapter_label("Chapter 1. The Start") is True


def test_is_chapter_label_bare_roman():
    assert _is_chapter_label("CHAPTER I") is True
    assert _is_chapter_label("Chapter V") is True


def test_is_chapter_label_letter_prefix():
    assert _is_chapter_label("Letter 1") is True
    assert _is_chapter_label("LETTER 4") is True


def test_is_chapter_label_bare_roman_with_title():
    # Treasure Island style
    assert _is_chapter_label("I The Old Sea-dog") is True
    assert _is_chapter_label("V The Last of the Blind Man") is True


def test_is_chapter_label_chapter_in_middle_of_label():
    # Corrupt NCX: previous chapter's content + new heading
    assert _is_chapter_label("I hope Mr. Bingley will like it. CHAPTER II.") is True


def test_is_chapter_label_part_is_not_chapter():
    assert _is_chapter_label("PART ONE—The Old Buccaneer") is False
    assert _is_chapter_label("Part Two") is False
    assert _is_chapter_label("PART 1") is False


def test_is_chapter_label_frontmatter_is_not_chapter():
    assert _is_chapter_label("Contents") is False
    assert _is_chapter_label("Title page") is False
    assert _is_chapter_label("PREFACE") is False
    assert _is_chapter_label("Illustrations") is False


# ── EPUB parsing (in-memory) ────────────────────────────────────────────────

def _make_minimal_epub(opf_xml: str, ncx_xml: str, chapter_files: dict) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("OEBPS/content.opf", opf_xml)
        zf.writestr("OEBPS/toc.ncx", ncx_xml)
        for name, content in chapter_files.items():
            zf.writestr(f"OEBPS/{name}", content)
    return buf.getvalue()


OPF_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<package xmlns="http://www.idpf.org/2007/opf" unique-identifier="bookid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>{title}</dc:title>
    <dc:creator>{author}</dc:creator>
    <dc:language>{language}</dc:language>
  </metadata>
  <manifest>
    <item id="ncx" href="toc.ncx" media-type="application/x-dtbncx+xml"/>
  </manifest>
  <spine toc="ncx"/>
</package>"""

NCX_TEMPLATE = """<?xml version='1.0' encoding='utf-8'?>
<ncx xmlns="http://www.daisy.org/z3986/2005/ncx/" version="2005-1">
  <head><meta name="dtb:uid" content="1"/></head>
  <docTitle><text>Test</text></docTitle>
  <navMap>
{nav_points}
  </navMap>
</ncx>"""


def _nav_point(label: str, src: str) -> str:
    return f"""    <navPoint id="{label}" playOrder="1">
      <navLabel><text>{label}</text></navLabel>
      <content src="{src}"/>
    </navPoint>"""


def test_parse_epub_minimal():
    opf = OPF_TEMPLATE.format(title="Test Book", author="Test Author", language="en")
    ncx = NCX_TEMPLATE.format(nav_points=(
        _nav_point("CHAPTER I. The Start", "ch1.html") +
        _nav_point("CHAPTER II. The End",  "ch2.html")
    ))
    chapters = {
        "ch1.html": "<html><body><h1>Start</h1><p>First chapter text.</p></body></html>",
        "ch2.html": "<html><body><h1>End</h1><p>Second chapter text.</p></body></html>",
    }
    epub_bytes = _make_minimal_epub(opf, ncx, chapters)
    book = _gt_fetcher._parse_epub(epub_bytes, fallback_book_id=1)
    assert book["title"] == "Test Book"
    assert book["authors"] == ["Test Author"]
    assert len(book["chapters"]) == 2
    assert "First chapter" in book["chapters"][0]["text"]
    assert "Second chapter" in book["chapters"][1]["text"]


def test_parse_epub_filters_frontmatter():
    opf = OPF_TEMPLATE.format(title="X", author="Y", language="en")
    ncx = NCX_TEMPLATE.format(nav_points=(
        _nav_point("Title page",   "title.html") +
        _nav_point("Contents",     "contents.html") +
        _nav_point("Preface",      "preface.html") +
        _nav_point("CHAPTER I.",   "ch1.html") +
        _nav_point("CHAPTER II.",  "ch2.html")
    ))
    chapters = {
        "title.html":   "<html><body>Title</body></html>",
        "contents.html": "<html><body>Contents</body></html>",
        "preface.html": "<html><body>Preface</body></html>",
        "ch1.html":     "<html><body>Chapter 1</body></html>",
        "ch2.html":     "<html><body>Chapter 2</body></html>",
    }
    epub_bytes = _make_minimal_epub(opf, ncx, chapters)
    book = _gt_fetcher._parse_epub(epub_bytes, fallback_book_id=1)
    assert len(book["chapters"]) == 2


def test_parse_epub_no_chapters_raises():
    opf = OPF_TEMPLATE.format(title="Essay", author="X", language="en")
    ncx = NCX_TEMPLATE.format(nav_points=_nav_point("Contents", "contents.html"))
    chapters = {"contents.html": "<html><body>Contents</body></html>"}
    epub_bytes = _make_minimal_epub(opf, ncx, chapters)
    with pytest.raises(ValueError, match="No chapters"):
        _gt_fetcher._parse_epub(epub_bytes, fallback_book_id=1)


# ── fetch_book integration (EPUB primary, text fallback) ──────────────────

@pytest.mark.asyncio
async def test_fetch_book_uses_epub_when_available():
    opf = OPF_TEMPLATE.format(title="Ebook Title", author="Ebook Author", language="en")
    ncx = NCX_TEMPLATE.format(nav_points=(
        _nav_point("CHAPTER I. Start", "ch1.html") +
        _nav_point("CHAPTER II. End",  "ch2.html")
    ))
    chapters = {
        "ch1.html": "<html><body>First</body></html>",
        "ch2.html": "<html><body>Second</body></html>",
    }
    epub_bytes = _make_minimal_epub(opf, ncx, chapters)

    with patch.object(_gt_fetcher, "_fetch_epub_bytes", new_callable=AsyncMock) as mock_epub:
        mock_epub.return_value = epub_bytes
        book = await fetch_book(1)
    assert book["title"] == "Ebook Title"
    assert len(book["chapters"]) == 2


@pytest.mark.asyncio
async def test_fetch_book_falls_back_to_text():
    text = (
        "Title: Fallback Book\n"
        "Author: Some Author\n"
        "Language: English\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK FALLBACK ***\n"
        "CHAPTER I.\n\nFirst body.\n\n"
        "CHAPTER II.\n\nSecond body.\n"
    )
    with patch.object(_gt_fetcher, "_fetch_epub_bytes", new_callable=AsyncMock) as mock_epub:
        mock_epub.return_value = None
        with patch.object(_gt_fetcher, "fetch_book_text", new_callable=AsyncMock) as mock_text:
            mock_text.return_value = text
            book = await fetch_book(1)
    assert book["title"] == "Fallback Book"
    # 2 chunks: license header is stripped, leaving 2 chapters.
    assert len(book["chapters"]) == 2
    assert "First body" in book["chapters"][0]["text"]
    assert "Second body" in book["chapters"][1]["text"]


@pytest.mark.asyncio
async def test_fetch_book_falls_back_when_epub_invalid():
    text = (
        "Title: Corrupted\n"
        "Author: X\n"
        "Language: English\n"
        "*** START OF THE PROJECT GUTENBERG EBOOK X ***\n"
        "CHAPTER I.\n\nOnly chapter.\n"
    )
    with patch.object(_gt_fetcher, "_fetch_epub_bytes", new_callable=AsyncMock) as mock_epub:
        mock_epub.return_value = b"not a real epub"
        with patch.object(_gt_fetcher, "fetch_book_text", new_callable=AsyncMock) as mock_text:
            mock_text.return_value = text
            book = await fetch_book(1)
    assert book["title"] == "Corrupted"
    # 1 chapter: license header is stripped, only the body remains.
    assert len(book["chapters"]) == 1
    assert "Only chapter" in book["chapters"][0]["text"]


# ── save_downloaded_text_to_gcs (download-only writer) ───────────────────

def test_save_downloaded_text_to_gcs_writes_full_text_and_minimal_metadata(monkeypatch):
    """Verifies the download-only writer produces only two artifacts:
    ``source/full_text.txt`` and a minimal ``metadata.json`` (no chapter
    or segment counts — those are filled in by ``gt_chapter_splitter``)."""
    uploads: dict[str, str] = {}

    fake_bucket = MagicMock()
    def fake_blob(name):
        b = MagicMock()
        b.name = name
        b.upload_from_string = lambda data, **kw: uploads.update({name: data if isinstance(data, str) else data.decode("utf-8")})
        return b
    fake_bucket.blob.side_effect = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket
    monkeypatch.setattr(_gt_fetcher, "get_gcs_client", lambda: fake_client)

    chapters = [
        {"index": 0, "title": "Chapter I.",  "text": "First chapter body."},
        {"index": 1, "title": "Chapter II.", "text": "Second chapter body."},
    ]
    meta_in = {"title": "Test", "authors": ["A"], "language": "en", "book_id": 1}
    returned = save_downloaded_text_to_gcs(chapters, meta_in)

    full_text_path = _gt_fetcher._source_path(SOURCE_FULL_TEXT)
    metadata_path  = _gt_fetcher._source_path("metadata.json")

    assert full_text_path in uploads
    assert "First chapter body." in uploads[full_text_path]
    assert "Second chapter body." in uploads[full_text_path]
    assert "\n\n" in uploads[full_text_path]  # joined with double newline

    parsed_meta = json.loads(uploads[metadata_path])
    # Minimal metadata — no num_chapters / num_segments / word_count
    assert parsed_meta["book_id"] == 1
    assert parsed_meta["title"] == "Test"
    assert parsed_meta["authors"] == ["A"]
    assert parsed_meta["language"] == "en"
    assert "num_chapters" not in parsed_meta
    assert "num_segments" not in parsed_meta
    assert "word_count" not in parsed_meta
    assert returned == parsed_meta


def test_save_downloaded_text_to_gcs_strips_chapter_whitespace(monkeypatch):
    """Each chapter's text is stripped before joining so a leading/trailing
    blank line in a chapter doesn't produce triple-newline seams in the
    joined ``full_text.txt`` (which would break the splitter's
    ``chapters_via_regex`` running-counter offsets)."""
    uploads: dict[str, str] = {}
    fake_bucket = MagicMock()
    def fake_blob(name):
        b = MagicMock()
        b.name = name
        b.upload_from_string = lambda data, **kw: uploads.update({name: data if isinstance(data, str) else data.decode("utf-8")})
        return b
    fake_bucket.blob.side_effect = fake_blob
    fake_client = MagicMock()
    fake_client.bucket.return_value = fake_bucket
    monkeypatch.setattr(_gt_fetcher, "get_gcs_client", lambda: fake_client)

    chapters = [
        {"index": 0, "title": "I", "text": "  body1 with edge whitespace  \n"},
        {"index": 1, "title": "II", "text": "\n\nbody2\n\n"},
    ]
    save_downloaded_text_to_gcs(chapters, {"title": "X", "authors": [], "language": "en", "book_id": 1})
    full_text = uploads[_gt_fetcher._source_path(SOURCE_FULL_TEXT)]
    # Each chapter is stripped; the join separator is exactly "\n\n".
    assert full_text == "body1 with edge whitespace\n\nbody2"
    # No triple-newline seams that would break the splitter's running counter.
    assert "\n\n\n" not in full_text
