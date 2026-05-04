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
PROMPT_EN = """You are a professional translator specializing in Taiwanese (Taiwanese Hokkien) to English translation.

Translate the following Taiwanese text to natural, readable English.

Rules:
1. Preserve paragraph structure exactly — do not merge or split paragraphs
2. Translate ALL content to English. Do NOT leave original Chinese characters in the output (except in brief parenthetical romanization for proper nouns).
3. Preserve proper nouns (names, places) with romanization when first mentioned
4. Cultural terms without English equivalents: keep the original with a brief parenthetical explanation
5. Tailo romanization markers may be kept in parentheses where helpful (e.g., tshit-thô)
6. Output only the translation, no explanations or preamble
{term_injection}

Source (Taiwanese):
{source_text}

English translation:"""

PROMPT_JA = """あなたは台湾語（台湾閩南語）→日本語翻訳の専門家です。

以下の台湾語テキストを自然で読みやすい日本語に翻訳してください。

規則：
1. 段落構造を完全に保持すること（段落の統合・分割は不可）
2. 全てのコンテンツを日本語に翻訳すること。固有名詞の簡潔な括弧内ローマ字表記を除き、原文の漢字を出力に残さないこと。
3. 固有名詞（人名・地名）は初出時に原文をカタカナ読みで併記
4. 日本語に対応する語がない文化的な言葉は原文を残し、括弧で簡単な説明を加える
5. 台羅ローマ字表記は必要に応じて括弧内に残す（例：tshit-thô）
6. 翻訳結果のみを出力し、説明や前置きは不要
{term_injection}

原文（台湾語）：
{source_text}

日本語翻訳："""

PROMPT_KO = """당신은 대만어（대만 민남어）→ 한국어 번역 전문가입니다。

다음 대만어 텍스트를 자연스럽고 읽기 쉬운 한국어로 번역하세요。

규칙：
1. 단락 구조를 완전히 유지할 것（단락 병합 또는 분리 금지）
2. 모든 콘텐츠를 한국어로 번역할 것。고유명사의 간단한 괄호 안 로마자 표기를 제외하고 원문 한자를 출력에 남기지 말 것。
3. 고유명사（인명、지명）는 첫 등장 시 원문을 병기
4. 한국어로 대응하는 표현이 없는 문화적 어휘는 원문을 유지하고 괄호로 간단한 설명 추가
5. 대만어 로마자 표기는 필요 시 괄호 안에 유지（예：tshit-thô）
6. 번역 결과만 출력、설명이나 서문 불필요
{term_injection}

원문（대만어）：
{source_text}

한국어 번역："""

PROMPTS = {
    "en": PROMPT_EN,
    "ja": PROMPT_JA,
    "ko": PROMPT_KO,
}


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


RETRY_PROMPTS = {
    "en": """CRITICAL: The previous attempt failed to translate the text properly — it still contains Chinese characters instead of English.

Please translate the following Taiwanese text to natural, readable English again.
You MUST translate EVERYTHING to English. Do NOT leave any Chinese characters in the output.

Rules:
1. Output ONLY the translation, no explanations
2. Do NOT keep original Chinese characters (romanization in parentheses is OK)
3. Preserve paragraph structure exactly

Source (Taiwanese):
{source_text}

English translation:""",

    "ja": """重要：前回の翻訳は失敗しました。出力に漢字が残っています。日本語に完全に翻訳してください。

規則：
1. 翻訳結果のみを出力、説明不要
2. 原文の漢字を残さない（括弧内のローマ字は可）
3. 段落構造を完全に保持

原文（台湾語）：
{source_text}

日本語翻訳：""",

    "ko": """중요: 이전 번역이 실패했습니다. 원문 한자가 출력에 남아 있습니다. 한국어로 완전히 번역하세요.

규칙:
1. 번역 결과만 출력, 설명 불필요
2. 원문 한자를 남기지 말 것（괄호 안 로마자 표기는 허용）
3. 단락 구조를 완전히 유지

원문（대만어）：
{source_text}

한국어 번역：""",
}


def translate_single(
    source_text: str,
    prompt_template: str,
    term_inj: str,
    target_lang: str,
    max_retries: int = 2,
) -> str:
    """Translate one segment with retry if partial_untranslated detected."""
    prompt = prompt_template.format(source_text=source_text, term_injection=term_inj)

    for attempt in range(max_retries + 1):
        response = translate(prompt)

        parts = re.split(r"\[PARA_SEP\]|\[\d+\]", response)
        parts = [p.strip() for p in parts if p.strip()]
        result = parts[0] if parts else response.strip()

        if attempt < max_retries and len(source_text) > 50:
            overlap = _cjk_overlap(source_text, result)
            if overlap > 0.5:
                logger.warning(
                    f"Segment partial untranslated (overlap={overlap:.0%}), retrying {attempt + 1}/{max_retries}"
                )
                prompt = RETRY_PROMPTS[target_lang].format(source_text=source_text)
                continue

        return result

    return result


def translate_batch(segments: list[dict], prompt_template: str,
                    terms: dict, target_lang: str,
                    batch_size: int = 5) -> list[str]:
    results = [""] * len(segments)
    term_inj = build_term_injection(terms, target_lang)

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]

        combined = "\n\n[PARA_SEP]\n\n".join(
            f"[{j+1}] {seg['text']}" for j, seg in enumerate(batch)
        )

        prompt = prompt_template.format(
            source_text    = combined,
            term_injection = term_inj,
        )

        response = translate(prompt)

        parts = re.split(r"\[PARA_SEP\]|\[\d+\]", response)
        parts = [p.strip() for p in parts if p.strip()]

        if len(parts) < len(batch):
            fallback = [p.strip() for p in re.split(r"\n{2,}", response) if p.strip()]
            if len(fallback) >= len(parts):
                parts = fallback
                logger.info(f"Batch {i//batch_size + 1}: used \\n\\n fallback split, got {len(parts)} parts")
            else:
                logger.warning(f"Batch {i//batch_size + 1}: LLM response parsing failed. Response: {response}")

        for k, part in enumerate(parts[:len(batch)]):
            results[i + k] = part

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

        if target_lang not in PROMPTS:
            raise ValueError(f"Unsupported target language: {target_lang}")

        target_translations = translate_batch(
            segments        = segments,
            prompt_template = PROMPTS[target_lang],
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
