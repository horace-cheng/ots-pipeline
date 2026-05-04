"""Tests for docx text extraction in ft_preprocess."""

import zipfile
import io
from xml.etree import ElementTree

import pytest


def _extract_text_from_docx(raw: bytes) -> str:
    """Extract text from a .docx file (ZIP archive of XMLs)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            doc_xml = zf.read("word/document.xml")
            root = ElementTree.fromstring(doc_xml)
    except (zipfile.BadZipFile, KeyError) as e:
        return None

    paragraphs = []
    current_text = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "p":
            text = "".join(current_text)
            if text.strip():
                paragraphs.append(text.strip())
            current_text = []
        elif tag == "t" and elem.text:
            current_text.append(elem.text)
    text = "".join(current_text)
    if text.strip():
        paragraphs.append(text.strip())

    return "\n\n".join(paragraphs)


def _make_docx(paragraphs: list[str]) -> bytes:
    """Create a minimal .docx file in memory for testing."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        body_parts = []
        for p in paragraphs:
            body_parts.append(f'<w:p xmlns:w="{ns}"><w:r><w:t>{p}</w:t></w:r></w:p>')
        body = "".join(body_parts)
        xml = f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?><w:document xmlns:w="{ns}"><w:body>{body}</w:body></w:document>'
        zf.writestr("word/document.xml", xml)
    buf.seek(0)
    return buf.read()


class TestDocxExtraction:
    def test_extract_text_from_docx(self):
        docx = _make_docx(["第一段測試文字。", "第二段測試文字。"])
        result = _extract_text_from_docx(docx)
        assert "第一段測試文字" in result
        assert "第二段測試文字" in result

    def test_empty_paragraphs_skipped(self):
        docx = _make_docx(["   ", "實際內容"])
        result = _extract_text_from_docx(docx)
        assert "   " not in result
        assert "實際內容" in result

    def test_multiple_runs_in_paragraph(self):
        """Paragraphs with multiple <w:r> elements should be joined."""
        # Create a docx where one paragraph has two <w:r> runs
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            ns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
            xml = (f'<?xml version="1.0" encoding="UTF-8"?>'
                   f'<w:document xmlns:w="{ns}">'
                   f'<w:body>'
                   f'<w:p><w:r><w:t>這是一個</w:t></w:r><w:r><w:t>合併的段落。</w:t></w:r></w:p>'
                   f'</w:body></w:document>')
            zf.writestr("word/document.xml", xml)
        buf.seek(0)
        docx = buf.read()

        result = _extract_text_from_docx(docx)
        assert "這是一個合併的段落。" in result

    def test_not_a_docx_returns_none(self):
        assert _extract_text_from_docx(b"this is not a docx file") is None

    def test_zip_but_no_document_xml_returns_none(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("other.xml", "not a docx")
        buf.seek(0)
        assert _extract_text_from_docx(buf.read()) is None

    def test_extract_preserves_chinese_characters(self):
        text = "月光下的相思雨"
        docx = _make_docx([text])
        result = _extract_text_from_docx(docx)
        assert text in result

    def test_extract_preserves_tailo_romanization(self):
        text = "Goā-thâu ê soan-hong"
        docx = _make_docx([text])
        result = _extract_text_from_docx(docx)
        assert text in result
