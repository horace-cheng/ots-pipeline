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
2. Preserve proper nouns (names, places) with romanization when first mentioned
3. Cultural terms without English equivalents: keep the original with a brief parenthetical explanation
4. Tailo romanization markers may be kept in parentheses where helpful (e.g., tshit-thô)
5. Output only the translation, no explanations or preamble
{term_injection}

Source (Taiwanese):
{source_text}

English translation:"""

PROMPT_JA = """あなたは台湾語（台湾閩南語）→日本語翻訳の専門家です。

以下の台湾語テキストを自然で読みやすい日本語に翻訳してください。

規則：
1. 段落構造を完全に保持すること（段落の統合・分割は不可）
2. 固有名詞（人名・地名）は初出時に原文をカタカナ読みで併記
3. 日本語に対応する語がない文化的な言葉は原文を残し、括弧で簡単な説明を加える
4. 台羅ローマ字表記は必要に応じて括弧内に残す（例：tshit-thô）
5. 翻訳結果のみを出力し、説明や前置きは不要
{term_injection}

原文（台湾語）：
{source_text}

日本語翻訳："""

PROMPT_KO = """당신은 대만어（대만 민남어）→ 한국어 번역 전문가입니다。

다음 대만어 텍스트를 자연스럽고 읽기 쉬운 한국어로 번역하세요。

규칙：
1. 단락 구조를 완전히 유지할 것（단락 병합 또는 분리 금지）
2. 고유명사（인명、지명）는 첫 등장 시 원문을 병기
3. 한국어로 대응하는 표현이 없는 문화적 어휘는 원문을 유지하고 괄호로 간단한 설명 추가
4. 대만어 로마자 표기는 필요 시 괄호 안에 유지（예：tshit-thô）
5. 번역 결과만 출력、설명이나 서문 불필요
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

        # LLM often ignores markers and returns paragraphs separated by \n\n
        if len(parts) < len(batch):
            fallback = [p.strip() for p in re.split(r"\n{2,}", response) if p.strip()]
            if len(fallback) >= len(parts):
                parts = fallback
                logger.info(f"Batch {i//batch_size + 1}: used \\n\\n fallback split, got {len(parts)} parts")

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
