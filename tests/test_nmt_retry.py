"""Tests for ft_nmt translation retry logic."""

import re
import pytest


def _cjk_overlap(src: str, tgt: str) -> float:
    """Return the ratio of source CJK chars that also appear in target."""
    cjk = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
    src_cjk = cjk.findall(src)
    if not src_cjk:
        return 0.0
    tgt_set = set(cjk.findall(tgt))
    return sum(1 for c in src_cjk if c in tgt_set) / len(src_cjk)


class TestCjkOverlap:
    def test_no_overlap(self):
        assert _cjk_overlap("你好世界", "Hello World") == 0.0

    def test_full_overlap(self):
        assert _cjk_overlap("你好世界", "你好世界") == 1.0

    def test_partial_overlap(self):
        """Source text with romanization added — most CJK chars still present."""
        src = "然而，愛情的道路，總有「酸鹹苦澀」"
        tgt = "然而，愛情的道路，總有「酸鹹苦澀」 (sng-kiâm-khóo-siap)"
        assert _cjk_overlap(src, tgt) > 0.8

    def test_proper_translation_low_overlap(self):
        """Proper translation keeps very few CJK chars."""
        src = "然而，愛情的道路，總有「酸鹹苦澀」。我想起有一擺咱的「相爭吵」。為著一件「雞毛蒜皮」的小事。"
        tgt = "However, the path of love always has sour, salty, bitter, and astringent tastes."
        assert _cjk_overlap(src, tgt) < 0.1

    def test_no_cjk_in_source(self):
        assert _cjk_overlap("Hello World", "你好") == 0.0

    def test_mixed_proper_translation(self):
        """A proper translation that keeps a couple cultural terms should still be low overlap."""
        src = "台文的浪漫，在於毋免講白，在於「心有靈犀」。我記得有一工，咱坐在「榕樹下」歇睏。"
        tgt = "The romance of Taiwanese lies in sim-ū-lêng-hi (telepathic understanding). I remember one day, we rested under a banyan tree."
        overlap = _cjk_overlap(src, tgt)
        assert overlap < 0.3


RETRY_PROMPT = "CRITICAL: The previous attempt failed. Please translate to {target_lang} again.\n\nSource:\n{source_text}\n\n{target_lang} translation:"


def translate_single_impl(
    source_text: str,
    prompt_template: str,
    term_inj: str,
    target_lang: str,
    max_retries: int,
    translate_fn: callable,
) -> str:
    """Translate one segment with retry if partial_untranslated detected."""
    prompt = prompt_template.format(source_text=source_text, term_injection=term_inj, target_lang=target_lang)

    for attempt in range(max_retries + 1):
        response = translate_fn(prompt)

        parts = re.split(r"\[PARA_SEP\]|\[\d+\]", response)
        parts = [p.strip() for p in parts if p.strip()]
        result = parts[0] if parts else response.strip()

        if attempt < max_retries and len(source_text) > 50:
            overlap = _cjk_overlap(source_text, result)
            if overlap > 0.5:
                prompt = RETRY_PROMPT.format(source_text=source_text, target_lang=target_lang)
                continue

        return result

    return result


class TestTranslateSingle:
    """Test the retry logic."""

    def _make_translate_mock(self, responses: list[str]):
        """Create a mock translate function that returns responses in order."""
        call_count = [0]
        prompts = []

        def mock_translate(prompt: str) -> str:
            prompts.append(prompt)
            resp = responses[call_count[0]]
            call_count[0] += 1
            return resp

        mock_translate.calls = lambda: call_count[0]
        mock_translate.get_prompts = lambda: prompts
        return mock_translate

    def test_success_on_first_try_no_retry(self):
        mock = self._make_translate_mock(["Hello World"])

        result = translate_single_impl("你好世界", "{source_text}\n{term_injection}", "", "en", max_retries=2, translate_fn=mock)

        assert result == "Hello World"
        assert mock.calls() == 1

    def test_retry_on_partial_untranslated(self):
        src = "然而，愛情的道路，總有「酸鹹苦澀」。我想起有一擺咱的「相爭吵」。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」。彼幾工，天也落雨，心也落雨。"
        bad = "然而，愛情的道路，總有「酸鹹苦澀」 (sng-kiâm-khóo-siap) 的滋味。我想起有一次咱的「相爭吵」。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」。彼幾工，天也落雨，心也落雨。"
        good = "However, the path of love always has its bitter and sour moments."
        mock = self._make_translate_mock([bad, good])

        result = translate_single_impl(src, "{source_text}\n{term_injection}", "", "en", max_retries=2, translate_fn=mock)

        assert result == good
        assert mock.calls() == 2

    def test_gives_up_after_max_retries(self):
        src = "然而，愛情的道路，總有「酸鹹苦澀」。我想起有一擺咱的「相爭吵」。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」。彼幾工，天也落雨，心也落雨。"
        bad = "然而，愛情的道路，總有「酸鹹苦澀」 (sng-kiâm-khóo-siap) 的滋味。我想起有一次咱的「相爭吵」。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」。彼幾工，天也落雨，心也落雨。"
        mock = self._make_translate_mock([bad, bad, bad])

        result = translate_single_impl(src, "{source_text}\n{term_injection}", "", "en", max_retries=2, translate_fn=mock)

        assert result == bad
        assert mock.calls() == 3

    def test_no_retry_for_short_segments(self):
        src = "你好"
        bad = "你好 (lí-hó)"
        mock = self._make_translate_mock([bad])

        result = translate_single_impl(src, "{source_text}\n{term_injection}", "", "en", max_retries=2, translate_fn=mock)

        assert result == bad
        assert mock.calls() == 1

    def test_retry_prompt_changes(self):
        """Verify the retry uses a different (stronger) prompt."""
        src = "然而，愛情的道路，總有「酸鹹苦澀」。我想起有一擺咱的「相爭吵」。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」。彼幾工，天也落雨，心也落雨。"
        bad = "然而，愛情的道路，總有「酸鹹苦澀」 (sng-kiâm-khóo-siap) 的滋味。我想起有一次咱的「相爭吵」。為著一件「雞毛蒜皮」的小事，你講我「番仔性」，我講你「無情無義」。彼幾工，天也落雨，心也落雨。"
        good = "However, love's path has bitter moments."
        mock = self._make_translate_mock([bad, good])

        prompt_template = "You are a professional translator. Translate: {source_text}\n{term_injection}"
        translate_single_impl(src, prompt_template, "", "en", max_retries=2, translate_fn=mock)

        prompts = mock.get_prompts()
        assert len(prompts) == 2
        assert prompts[0].startswith("You are a professional")
        assert "CRITICAL" in prompts[1]
