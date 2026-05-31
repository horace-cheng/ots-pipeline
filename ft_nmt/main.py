"""
ft_nmt/main.py — Cloud Run Job

Fast Track Step 2: NMT（直接翻譯）
- 台語（台羅/漢字）→ 目標語（英文/日文/韓文）
- 套用術語詞庫（pre-injection 方式）
- 寫入 translations.json 到 GCS temp
"""

import sys, json, re, logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config     import cfg
from shared.db         import update_job_status, get_order_info, update_order_field
from shared.storage    import read_temp_json, write_temp_json
from shared.gemini     import translate
from shared.terminology import get_terms

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ft_nmt")


# ── Prompt 模板 ───────────────────────────────────────────────────────────────
PROMPT = """You are a professional translator specializing in Taiwanese (Taiwanese Hokkien) to {target_lang} translation.

Translate the following Taiwanese text to natural, readable {target_lang}.

Rules:
1. Preserve paragraph structure exactly — do not merge or split paragraphs
2. Translate ALL content to {target_lang}. Do NOT leave original Chinese characters in the output (except in brief parenthetical romanization for proper nouns).
3. Preserve proper nouns (names, places) with romanization when first mentioned
4. Cultural terms without {target_lang} equivalents: keep the original with a brief parenthetical explanation
5. Tailo romanization markers may be kept in parentheses where helpful (e.g., tshit-thô)
6. Output only the translation, no explanations or preamble
{hanzi_instruction}{term_injection}

Source (Taiwanese):
{source_text}

{target_lang} translation:"""


def _get_hanzi_instruction(target_lang: str) -> str:
    """Return extra instruction for Hanzi output when target is tai-lo."""
    if target_lang != "tai-lo":
        return ""
    return (
        "7. CRITICAL — Taiwanese Hokkien output MUST be written in Han characters (台語漢字), "
        "NOT in Pe̍h-ōe-jī romanization.\n"
        "   Correct examples: 我 (not góa), 的 (not ê), 是 (not sī), 有 (not ū), 人 (not lâng), "
        "愛 (not ài), 講 (not kóng), 看 (not khòaⁿ), 這 (not che), 佇 (not tī).\n"
        "   Use Tailo romanization ONLY in parentheses after the Han form for terms without "
        "standard Han characters (e.g., 泅水 (siû-chúi)).\n"
        "   IMPORTANT: Pure romanization output will be rejected. You must produce Han-dominant text "
        "readable by native Taiwanese speakers.\n"
    )


def build_term_injection(terms: dict, target_lang: str) -> str:
    if not terms:
        return ""
    lines = ["術語對照表 / Terminology reference:"]
    for src, tgt in list(terms.items())[:30]:
        lines.append(f"  {src} → {tgt}")
    return "\n".join(lines)


def _cjk_overlap(src: str, tgt: str) -> float:
    """Return the ratio of source CJK chars that also appear in target."""
    cjk = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
    src_cjk = cjk.findall(src)
    if not src_cjk:
        return 0.0
    tgt_set = set(cjk.findall(tgt))
    return sum(1 for c in src_cjk if c in tgt_set) / len(src_cjk)


def _has_sufficient_hanzi(text: str, threshold: float = 0.15) -> bool:
    """Check if a tai-lo output has enough Han characters vs pure romanization.
    
    Returns True if at least `threshold` proportion of content chars are CJK.
    """
    if not text.strip():
        return True
    cjk = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
    # Count only meaningful characters (exclude spaces, punctuation, digits)
    content_chars = re.sub(r"[\s\d\W_]", "", text)
    if not content_chars:
        return False
    hanzi_count = len(cjk.findall(content_chars))
    return hanzi_count / len(content_chars) >= threshold


RETRY_PROMPT = """CRITICAL: The previous attempt failed to translate the text properly — it still contains Chinese characters instead of {target_lang}.

Please translate the following Taiwanese text to natural, readable {target_lang} again.
You MUST translate EVERYTHING to {target_lang}. Do NOT leave any Chinese characters in the output.

Rules:
1. Output ONLY the translation, no explanations
2. Do NOT keep original Chinese characters (romanization in parentheses is OK)
3. Preserve paragraph structure exactly
{hanzi_instruction}
Source (Taiwanese):
{source_text}

{target_lang} translation:"""


def translate_single(
    source_text: str,
    prompt_template: str,
    term_inj: str,
    target_lang: str,
    max_retries: int = 2,
) -> str:
    """Translate one segment with retry if partial_untranslated detected."""
    hanzi_instr = _get_hanzi_instruction(target_lang)
    prompt = prompt_template.format(
        source_text=source_text, term_injection=term_inj,
        target_lang=target_lang, hanzi_instruction=hanzi_instr,
    )

    for attempt in range(max_retries + 1):
        response = translate(prompt, job_type="nmt")

        parts = re.split(r"\[\d+\]", response)
        parts = [p.strip() for p in parts if p.strip()]
        result = parts[0] if parts else response.strip()

        if attempt < max_retries and len(source_text) > 50:
            should_retry = False
            overlap = _cjk_overlap(source_text, result)
            if overlap > 0.5:
                logger.warning(
                    f"Segment partial untranslated (overlap={overlap:.0%}), retrying {attempt + 1}/{max_retries}"
                )
                should_retry = True
            if target_lang == "tai-lo" and not _has_sufficient_hanzi(result):
                logger.warning(
                    f"Tai-lo output lacks Han characters (hanzi ratio too low), retrying {attempt + 1}/{max_retries}"
                )
                should_retry = True
            if should_retry:
                prompt = RETRY_PROMPT.format(
                    source_text=source_text, target_lang=target_lang,
                    hanzi_instruction=_get_hanzi_instruction(target_lang),
                )
                continue

        return result

    return result


def translate_batch(segments: list[dict], prompt_template: str,
                    terms: dict, target_lang: str,
                    batch_size: int = 5) -> list[str]:
    results = [""] * len(segments)
    term_inj = build_term_injection(terms, target_lang)
    hanzi_instr = _get_hanzi_instruction(target_lang)

    # Patterns that indicate LLM preamble text (not actual translation)
    PREAMBLE_RE = re.compile(
        r'^(here are|following|below|sure|certainly|of course|'
        r'here is|here\'s|below is|the following|'
        r'以下是|好的|當然|翻譯如下)',
        re.IGNORECASE
    )

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]

        combined = "\n\n[PARA_SEP]\n\n".join(
            f"[{j+1}] {seg['text']}" for j, seg in enumerate(batch)
        )

        prompt = prompt_template.format(
            source_text       = combined,
            term_injection    = term_inj,
            target_lang       = target_lang,
            hanzi_instruction = hanzi_instr,
        )

        response = translate(prompt, job_type="nmt")

        # ── Primary parse: numbered markers [1] [2] ... ──
        # Extract translations by numbered markers — most reliable
        numbered_parts = re.findall(r"\[(\d+)\]\s*(.*?)(?=\[\d+\]|$)", response, re.DOTALL)
        if numbered_parts and len(numbered_parts) == len(batch):
            parts = [p.strip() for _, p in numbered_parts]
        else:
            # Strip preamble before [1] if present
            first_marker = re.search(r"\n?\[1\]", response)
            clean_response = response[first_marker.end():] if first_marker else response

            # Split on [PARA_SEP] or numbered markers
            parts = re.split(r"\[PARA_SEP\]|\[\d+\]", clean_response)
            parts = [p.strip() for p in parts if p.strip()]

        # ── Handle part count mismatch ──
        if len(parts) > len(batch):
            # LLM produced extra paragraphs (e.g. split title from body)
            # Merge short first part into second (title+body case)
            if len(parts[0]) < len(parts[1]) * 0.3 and not parts[0].rstrip().endswith("."):
                parts[0] = parts[0] + " " + parts[1]
                parts.pop(1)
                logger.info(f"Batch {i//batch_size + 1}: merged title-like first part")

            # If still too many, merge extras into the last part
            while len(parts) > len(batch):
                parts[-2] = parts[-2] + " " + parts.pop(-1)
                logger.info(f"Batch {i//batch_size + 1}: merged excess part into last segment")

        elif len(parts) < len(batch):
            # Fallback: split on double newlines
            first_marker = re.search(r"\n?\[1\]", response)
            clean_response = response[first_marker.end():] if first_marker else response
            fallback = [p.strip() for p in re.split(r"\n{2,}", clean_response) if p.strip()]

            # Drop preamble lines
            while fallback and PREAMBLE_RE.match(fallback[0]):
                fallback.pop(0)

            # Handle extra paragraphs in fallback too
            if len(fallback) > len(batch):
                if len(fallback[0]) < len(fallback[1]) * 0.3 and not fallback[0].rstrip().endswith("."):
                    fallback[0] = fallback[0] + " " + fallback[1]
                    fallback.pop(1)
                while len(fallback) > len(batch):
                    fallback[-2] = fallback[-2] + " " + fallback.pop(-1)

            if len(fallback) >= len(batch):
                parts = fallback[:len(batch)]
                logger.info(f"Batch {i//batch_size + 1}: used \\n\\n fallback split, got {len(parts)} parts")
            else:
                logger.warning(f"Batch {i//batch_size + 1}: LLM response parsing issue. Got {len(parts)}/{len(batch)} parts. Response: {response[:200]}")

        # ── Assign to results ──
        for k, part in enumerate(parts[:len(batch)]):
            results[i + k] = part
            if target_lang == "tai-lo" and not _has_sufficient_hanzi(part):
                logger.warning(
                    f"Segment {i+k} in batch {i//batch_size + 1} has insufficient Han characters "
                    f"(tai-lo target) — prompt may need strengthening"
                )

        for k in range(len(parts), len(batch)):
            results[i + k] = ""
            logger.warning(f"Missing translation for segment {i+k}, leaving blank")

        logger.info(f"Translated batch {i//batch_size + 1}: segments {i}–{i+len(batch)-1}")

    return results


def run():
    logger.info(f"=== ft_nmt START — order: {cfg.ORDER_ID} ===")
    update_job_status("nmt", "running")
    update_order_field("status", "processing")

    try:
        segments = read_temp_json("segments.json")
        metadata = read_temp_json("metadata.json")
        terms    = get_terms(cfg.ORDER_ID)

        source_lang = metadata["source_lang"]
        target_lang = metadata["target_lang"]

        logger.info(f"NMT: {source_lang} → {target_lang}, {len(segments)} segments")

        target_translations = translate_batch(
            segments        = segments,
            prompt_template = PROMPT,
            terms           = terms,
            target_lang     = target_lang,
        )

        translations = [
            {
                "index":      seg["index"],
                "source":     seg["text"],
                "translated": tgt_trans,
                "comments":   "", # Initialize empty comments
            }
            for seg, tgt_trans in zip(segments, target_translations)
        ]
        write_temp_json("translations_raw.json", translations)
        write_temp_json("translations.json",     translations)

        update_job_status("nmt", "success")
        logger.info(f"=== ft_nmt DONE — {len(translations)} segments translated ===")

    except Exception as e:
        logger.exception(f"ft_nmt FAILED: {e}")
        update_job_status("nmt", "failed", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
