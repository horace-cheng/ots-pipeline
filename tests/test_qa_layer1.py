"""Tests for QA Layer 1 structure validation — partial_untranslated detection.

Tests the layer1_structure function directly without requiring full pipeline environment.
"""

import re
import pytest


def layer1_structure_minimal(segments, translations, source_lang, target_lang):
    """Minimal copy of layer1_structure for testing, without DB/GCS dependencies."""
    flags = []

    ratio_ranges = {
        ("tai-lo", "zh-tw"):   (0.7, 1.1),
        ("hakka",  "zh-tw"):   (0.7, 1.1),
        ("tai-lo", "en"):      (0.4, 0.85),
        ("tai-lo", "ja"):      (0.5, 0.9),
        ("tai-lo", "ko"):      (0.5, 1.0),
        ("zh-tw",  "en"):      (0.4, 0.85),
        ("zh-tw",  "ja"):      (0.5, 0.9),
        ("zh-tw",  "ko"):      (0.5, 1.0),
    }

    if len(segments) != len(translations):
        flags.append({
            "paragraph_index": 0,
            "flag_level":      "must_fix",
            "flag_type":       "missing_segment",
            "source_segment":  f"Expected {len(segments)} segments, got {len(translations)}",
            "translated_segment": "",
        })

    total_src_len = 0
    total_tgt_len = 0
    flag_count = 0
    pass_count = 0

    for trans in translations:
        idx = trans["index"]
        src_text = trans["source"]
        tgt_text = trans["translated"]
        src_len = len(src_text)
        tgt_len = len(tgt_text)
        total_src_len += src_len
        total_tgt_len += tgt_len

        # 漏譯偵測（空翻譯）
        if src_len > 20 and tgt_len < 5:
            flags.append({
                "paragraph_index": idx,
                "flag_level":      "must_fix",
                "flag_type":       "missing_segment",
                "source_segment":  src_text,
                "translated_segment": tgt_text,
            })
            flag_count += 1
            continue

        # 偵測未翻譯（原文與譯文相同，且長度足夠）
        if src_len > 20 and src_text.strip() == tgt_text.strip():
            flags.append({
                "paragraph_index": idx,
                "flag_level":      "must_fix",
                "flag_type":       "untranslated",
                "source_segment":  src_text,
                "translated_segment": tgt_text,
            })
            flag_count += 1
            continue

        # 偵測部分未翻譯（原文中的漢字在譯文中大量殘留）
        if src_len > 50 and target_lang in ("en", "ja", "ko"):
            cjk_pattern = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
            src_cjk = cjk_pattern.findall(src_text)
            tgt_cjk = cjk_pattern.findall(tgt_text)
            if src_cjk:
                tgt_cjk_set = set(tgt_cjk)
                remaining_count = sum(1 for c in src_cjk if c in tgt_cjk_set)
                overlap_ratio = remaining_count / len(src_cjk)
                if overlap_ratio > 0.5:
                    flags.append({
                        "paragraph_index": idx,
                        "flag_level":      "must_fix",
                        "flag_type":       "partial_untranslated",
                        "source_segment":  src_text,
                        "translated_segment": tgt_text,
                    })
                    flag_count += 1
                    continue

        # 長度比例檢查（只在段落 > 50 字時才檢查）
        if src_len >= 50:
            ratio = tgt_len / src_len if src_len > 0 else 0
            range_key = (source_lang, target_lang)
            if range_key in ratio_ranges:
                min_r, max_r = ratio_ranges[range_key]
                if not (min_r <= ratio <= max_r):
                    level = "must_fix" if ratio < min_r * 0.6 or ratio > max_r * 1.5 else "review"
                    flags.append({
                        "paragraph_index": idx,
                        "flag_level":      level,
                        "flag_type":       "length_ratio",
                        "source_segment":  src_text,
                        "translated_segment": tgt_text,
                    })
                    flag_count += 1
                    continue

        pass_count += 1

    overall_ratio = total_tgt_len / total_src_len if total_src_len > 0 else 0
    result = {
        "pass":          flag_count == 0,
        "flags":         flag_count,
        "pass_count":    pass_count,
        "overall_ratio": round(overall_ratio, 3),
    }
    return result, flags


class TestLayer1PartialUntranslated:
    """Test detection of partial translations where source Han characters remain in target."""

    def _make_segments(self, texts):
        return [{"index": i, "text": t} for i, t in enumerate(texts)]

    def _make_translations(self, sources, targets):
        return [{"index": i, "source": s, "translated": t} for i, (s, t) in enumerate(zip(sources, targets))]

    def test_proper_translation_passes(self):
        """A proper translation should not trigger partial_untranslated."""
        src = "《月光下的相思雨》"
        tgt = "Acacia Rain Under the Moonlight"
        segments = self._make_segments([src])
        translations = self._make_translations([src], [tgt])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "en")
        assert result["pass"] is True
        assert len(flags) == 0

    def test_exact_untranslated_still_caught(self):
        """Exact match should still be caught as 'untranslated'."""
        src = "這是一段很長的台語文字，超過二十個字，用來測試未翻譯的偵測功能是否正常運作。"
        segments = self._make_segments([src])
        translations = self._make_translations([src], [src])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "en")
        assert result["pass"] is False
        assert any(f["flag_type"] == "untranslated" for f in flags)

    def test_partial_untranslated_caught(self):
        """Source text with only romanization added should be caught."""
        src = "然而，愛情的道路，總有「酸鹹苦澀」。我想起有一擺咱的「相爭吵」。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」。彼幾工，天也落雨，心也落雨。窗外的「芭蕉葉」，被雨水打到「落落長」，我的心情，也像這芭蕉葉，破破爛脆。我企在鏡前，看著自己憔悴的容顏，想起你的好，眼淚就「袂守門」，一粒一粒掉下來。"
        tgt = '然而，愛情的道路，總有「酸鹹苦澀」 (sng-kiâm-khóo-siap) 的滋味。我想起有一次咱的「相爭吵」 (sio-tsing-tsháu)。為著一件「雞毛蒜皮」 (ke-mn̂g-sǹg-phuê) 的小事，你講我「番仔性」 (hoan-á-sìng)，我講你「無情無義」 (bô-tsîng-bô-gī)。那幾天，天也落雨，心也落雨。窗外的芭蕉葉，被雨水打到「落落長」 (lak-lak-tn̂g)，我的心情，也像這芭蕉葉，破破爛脆。我企在鏡前，看著自己憔悴的容顏，想起你的好，眼淚就「袂守門」 (bē-siú-mn̂g)，一粒一粒掉下來。'

        segments = self._make_segments([src])
        translations = self._make_translations([src], [tgt])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "en")
        assert result["pass"] is False
        partial_flags = [f for f in flags if f["flag_type"] == "partial_untranslated"]
        assert len(partial_flags) == 1
        assert partial_flags[0]["flag_level"] == "must_fix"

    def test_mixed_segments_some_pass_some_fail(self):
        """Batch with both good and bad translations."""
        src_good = "這是一段正常的台語文字。"
        tgt_good = "This is a normal Taiwanese text."
        src_bad = "然而，愛情的道路，總有「酸鹹苦澀」。我想起有一擺咱的「相爭吵」。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」。彼幾工，天也落雨，心也落雨。"
        tgt_bad = "然而，愛情的道路，總有「酸鹹苦澀」 (sng-kiâm-khóo-siap)。我想起有一次咱的「相爭吵」 (sio-tsing-tsháu)。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」 (bô-tsîng-bô-gī)。"

        segments = self._make_segments([src_good, src_bad])
        translations = self._make_translations([src_good, src_bad], [tgt_good, tgt_bad])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "en")
        assert result["pass"] is False
        assert len(flags) == 1
        assert flags[0]["flag_type"] == "partial_untranslated"

    def test_target_lang_zh_tw_not_flagged(self):
        """When target is zh-tw, Han character overlap is expected."""
        src = "這是一段台語文字，有很多漢字在裡面。"
        tgt = "這是一段台語文字，有很多漢字在裡面（台羅：tsit tīnn tuàn tâi-gí bûn-jī）。"

        segments = self._make_segments([src])
        translations = self._make_translations([src], [tgt])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "zh-tw")
        # Should pass because zh-tw target allows Han characters
        assert result["pass"] is True

    def test_target_lang_ja_proper_translation_passes(self):
        """Japanese proper translation should pass even with some Kanji overlap."""
        src = "這是一段台語文字。"
        tgt = "これは台湾語のテキストです。"

        segments = self._make_segments([src])
        translations = self._make_translations([src], [tgt])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "ja")
        # Should pass since src_len < 50, partial check is skipped
        assert result["pass"] is True

    def test_short_segment_not_flagged(self):
        """Segments under 50 chars should not trigger partial_untranslated."""
        src = "你好世界"
        tgt = "你好 (lí-hó) 世界 (sè-kài)"

        segments = self._make_segments([src])
        translations = self._make_translations([src], [tgt])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "en")
        # Too short to flag
        assert result["pass"] is True

    def test_half_overlap_flagged(self):
        """When >50% of source Han characters remain, should be flagged."""
        src = "我站在月光下看著遠方的山，心裡想著你。風輕輕吹過，帶來了你的氣息。這是一段很長的測試文字，用來確認部分未翻譯的偵測功能是否正常工作。希望這個測試能夠成功通過。"
        tgt = "I stood under the moonlight looking at the distant mountains, thinking of you. 風輕輕吹過，帶來了你的氣息。這是一段很長的測試文字，用來確認部分未翻譯的偵測功能是否正常工作。希望這個測試能夠成功通過。"

        segments = self._make_segments([src])
        translations = self._make_translations([src], [tgt])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "en")
        assert result["pass"] is False
        assert any(f["flag_type"] == "partial_untranslated" for f in flags)

    def test_proper_translation_with_few_retained_chars_passes(self):
        """A proper translation with a few retained cultural terms should pass."""
        src = "台文的浪漫，在於毋免講白，在於「心有靈犀」。我記得有一工，咱坐在「榕樹下」歇睏。"
        tgt = "The romance of Taiwanese lies in not needing to speak plainly; it lies in sim-ū-lêng-hi (telepathic understanding). I remember one day, we rested under a banyan tree (iông-chhiū)."

        segments = self._make_segments([src])
        translations = self._make_translations([src], [tgt])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "en")
        assert result["pass"] is True

    def test_real_data_from_pipeline(self):
        """Test with actual problematic segments from the pipeline data."""
        # Index 5 from the real pipeline data
        src_5 = "然而，愛情的道路，總有「酸鹹苦澀」。我想起有一擺咱的「相爭吵」。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」。彼幾工，天也落雨，心也落雨。窗外的「芭蕉葉」，被雨水打到「落落長」，我的心情，也像這芭蕉葉，破破爛脆。我企在鏡前，看著自己憔悴的容顏，想起你的好，眼淚就「袂守門」，一粒一粒掉下來。"
        tgt_5 = '然而，愛情的道路，總有「酸鹹苦澀」 (sng-kiâm-khóo-siap) 的滋味。我想起有一次咱的「相爭吵」 (sio-tsing-tsháu)。為著一件「雞毛蒜皮」 (ke-mn̂g-sǹg-phuê) 的小事，你講我「番仔性」 (hoan-á-sìng)，我講你「無情無義」 (bô-tsîng-bô-gī)。那幾天，天也落雨，心也落雨。窗外的芭蕉葉，被雨水打到「落落長」 (lak-lak-tn̂g)，我的心情，也像這芭蕉葉，破破爛脆。我企在鏡前，看著自己憔悴的容顏，想起你的好，眼淚就「袂守門」 (bē-siú-mn̂g)，一粒一粒掉下來。'

        # Index 6 from the real pipeline data
        src_6 = "原來，愛一個人，是會為伊「牽腸掛肚」。"
        tgt_6 = "原來，愛一個人，是會為伊「牽腸掛肚」 (khian-tn̂g-kuà-tōo)。"

        # Index 0 - properly translated (should pass)
        src_0 = "《月光下的相思雨》"
        tgt_0 = "Acacia Rain Under the Moonlight"

        segments = self._make_segments([src_0, src_5, src_6])
        translations = self._make_translations([src_0, src_5, src_6], [tgt_0, tgt_5, tgt_6])

        result, flags = layer1_structure_minimal(segments, translations, "tai-lo", "en")
        assert result["pass"] is False
        # src_5 should be flagged (long enough)
        # src_6 should NOT be flagged (too short, <50 chars)
        partial_flags = [f for f in flags if f["flag_type"] == "partial_untranslated"]
        assert len(partial_flags) == 1
        assert partial_flags[0]["paragraph_index"] == 1  # Index 5 = segment 1 in this batch
