"""Unit tests for lt_preprocess_nmt split_paragraphs + parser hardening.

Loads the module via importlib (same pattern as test_gt_chapter_splitter)
and exercises the real split_paragraphs implementation against the
document-layout scenarios that broke order 839206f1:

- title keeps its own segment
- epigraph quote lines group into one segment with line breaks
- attribution and <claim> subtitle keep their own segments
- every chapter heading is its own segment and never merges with
  adjacent short content lines
"""
import os
os.environ.setdefault("ORDER_ID", "test-order-lt")
os.environ.setdefault("DB_URL", "postgresql+asyncpg://test:test@localhost:5432/test")
os.environ.setdefault("ENV", "test")

import importlib.util

import pytest


_LT_DIR = os.path.join(os.path.dirname(__file__), "..", "lt_preprocess_nmt")
_spec = importlib.util.spec_from_file_location("lt_preprocess_main", os.path.join(_LT_DIR, "main.py"))
_lt = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_lt)

split_paragraphs = _lt.split_paragraphs


TITLE      = "柴山少年安魂曲"
QUOTE_LINES = [
    "兄弟們",
    "你們不要懼怕人們的罪孽",
    "要愛在罪孽裡的人",
    "因為這是神的表示",
    "這是世上愛的高峰",
]
ATTRIBUTION = "──杜思妥也夫斯基《卡拉馬助夫兄弟們》"
SUBTITLE   = "〈聲明與感謝〉"
LONG_PARA  = "這個故事，是我眞實的回憶，之所以一開始就這麼強調，實在是因為我的生活裡，有太多荒誕不經的事。"


def _make_text(*paras: str) -> str:
    return "\n\n".join(paras)


class TestSplitParagraphs:
    def test_title_is_own_segment(self):
        text = _make_text(TITLE, LONG_PARA)
        result = split_paragraphs(text)
        assert result[0] == TITLE

    def test_epigraph_groups_with_linebreaks(self):
        text = _make_text(TITLE, *QUOTE_LINES, ATTRIBUTION)
        result = split_paragraphs(text)
        assert result[0] == TITLE
        assert result[1] == "\n".join(QUOTE_LINES)

    def test_attribution_own_segment(self):
        text = _make_text(TITLE, *QUOTE_LINES, ATTRIBUTION, SUBTITLE, LONG_PARA)
        result = split_paragraphs(text)
        assert result[2] == ATTRIBUTION
        assert result[3] == SUBTITLE

    def test_subtitle_own_segment(self):
        text = _make_text(TITLE, LONG_PARA, SUBTITLE, LONG_PARA)
        result = split_paragraphs(text)
        assert SUBTITLE in result
        idx = result.index(SUBTITLE)
        assert "\n" not in result[idx]

    def test_chapter_heading_own_segment(self):
        text = _make_text(TITLE, LONG_PARA, "第一章", LONG_PARA, "第二章", LONG_PARA)
        result = split_paragraphs(text)
        assert result == [TITLE, LONG_PARA, "第一章", LONG_PARA, "第二章", LONG_PARA]

    def test_heading_never_merges_with_short_content_before(self):
        short_line = "而那柴枝……"
        text = _make_text(TITLE, LONG_PARA, short_line, "第三章", LONG_PARA)
        result = split_paragraphs(text)
        assert "第三章" in result
        idx = result.index("第三章")
        assert "\n" not in result[idx]
        assert result[idx - 1] == short_line

    def test_heading_never_merges_with_short_content_after(self):
        dialogue = "「柳仔比你想的還不好惹……」"
        text = _make_text(TITLE, LONG_PARA, "第十一章", dialogue, LONG_PARA)
        result = split_paragraphs(text)
        assert "第十一章" in result
        idx = result.index("第十一章")
        assert "\n" not in result[idx]
        assert result[idx + 1] == dialogue

    def test_long_paragraph_own_segment(self):
        text = _make_text(TITLE, LONG_PARA, LONG_PARA)
        result = split_paragraphs(text)
        assert result[1] == LONG_PARA
        assert result[2] == LONG_PARA

    def test_consecutive_shorts_group_into_block(self):
        short_a = "這是短的甲"
        short_b = "這是短的乙"
        text = _make_text(TITLE, LONG_PARA, short_a, short_b, LONG_PARA)
        result = split_paragraphs(text)
        assert "\n".join([short_a, short_b]) in result

    def test_no_backward_merge_into_long_paragraph(self):
        short_line = "他接著說："
        text = _make_text(TITLE, LONG_PARA + "。", short_line, LONG_PARA)
        result = split_paragraphs(text)
        assert short_line in result
        idx = result.index(short_line)
        assert "\n" not in result[idx]

    def test_oversized_segment_split_by_sentence(self):
        big = "這是第一句。" * 500
        result = split_paragraphs(big)
        assert all(len(s) <= 4000 for s in result)
        assert len(result) > 1

    def test_single_paragraph_falls_back_to_sentence_split(self):
        result = split_paragraphs("這是一句話。這是一句話。")
        assert len(result) >= 2
