"""
ft_nmt/main.py — Cloud Run Job

Fast Track Step 2: NMT（兩段式翻譯）
- Stage 1: 台語（台羅/漢字）→ 繁體中文
- Stage 2: 繁體中文 → 目標語（英文/日文/韓文）
- 套用術語詞庫（pre-injection 方式）
- 寫入 translations.json 到 GCS temp
"""

import sys, json, re, logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config     import cfg
from shared.db         import update_job_status, get_order_info
from shared.storage    import read_temp_json, write_temp_json
from shared.gemini     import translate
from shared.terminology import get_terms

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ft_nmt")


# ── Prompt 模板 ───────────────────────────────────────────────────────────────
STAGE1_PROMPT = """你是一位專業的台語（臺灣閩南語）到繁體中文翻譯專家。

請將以下台語文本翻譯成自然流暢的繁體中文。

規則：
1. 保留原文的段落結構，不合併或拆分段落
2. 人名、地名保留原文標注，並加上中文對應（如：阿輝（阿輝）→ 直接保留）
3. 文化詞彙若無對應中文，保留原詞並加括號說明
4. 台羅拼音標記保留在括號內（如：tshit-thô (七逃)）
5. 只輸出翻譯結果，不添加任何說明或前言
{term_injection}

原文（台語）：
{source_text}

繁體中文翻譯："""

STAGE2_PROMPT_EN = """You are a professional translator specializing in translating Traditional Chinese literary text to English.

Translate the following Traditional Chinese text to natural, readable English.

Rules:
1. Preserve paragraph structure exactly — do not merge or split paragraphs
2. Preserve proper nouns (names, places) with romanization when first mentioned
3. Cultural terms without English equivalents: keep the original with a brief parenthetical explanation
4. Output only the translation, no explanations or preamble
{term_injection}

Source (Traditional Chinese):
{source_text}

English translation:"""

STAGE2_PROMPT_JA = """あなたは台湾文学の中国語→日本語翻訳の専門家です。

以下の繁体字中国語テキストを自然で読みやすい日本語に翻訳してください。

規則：
1. 段落構造を完全に保持すること（段落の統合・分割は不可）
2. 固有名詞（人名・地名）は初出時に原文をカタカナ読みで併記
3. 日本語に対応する語がない文化的な言葉は原文を残し、括弧で簡単な説明を加える
4. 翻訳結果のみを出力し、説明や前置きは不要
{term_injection}

原文（繁体字中国語）：
{source_text}

日本語翻訳："""

STAGE2_PROMPT_KO = """당신은 대만 문학의 중국어 → 한국어 번역 전문가입니다。

다음 번체자 중국어 텍스트를 자연스럽고 읽기 쉬운 한국어로 번역하세요。

규칙：
1. 단락 구조를 완전히 유지할 것（단락 병합 또는 분리 금지）
2. 고유명사（인명、지명）는 첫 등장 시 원문을 병기
3. 한국어로 대응하는 표현이 없는 문화적 어휘는 원문을 유지하고 괄호로 간단한 설명 추가
4. 번역 결과만 출력、설명이나 서문 불필요
{term_injection}

원문（번체자 중국어）：
{source_text}

한국어 번역："""

STAGE2_PROMPTS = {
    "en": STAGE2_PROMPT_EN,
    "ja": STAGE2_PROMPT_JA,
    "ko": STAGE2_PROMPT_KO,
}


def build_term_injection(terms: dict, target_lang: str) -> str:
    """將術語詞庫注入 Prompt"""
    if not terms:
        return ""
    lines = [f"術語對照表 / Terminology reference:"]
    for src, tgt in list(terms.items())[:30]:  # 最多注入 30 條術語
        lines.append(f"  {src} → {tgt}")
    return "\n".join(lines)


def translate_batch(segments: list[dict], prompt_template: str,
                    terms: dict, target_lang: str,
                    batch_size: int = 5) -> list[str]:
    """
    批次翻譯，每批最多 batch_size 段。
    把多段合併成一個 prompt，降低 API 呼叫次數。
    """
    results = [""] * len(segments)
    term_inj = build_term_injection(terms, target_lang)

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]

        # 合併段落，用分隔符標記
        combined = "\n\n[PARA_SEP]\n\n".join(
            f"[{j+1}] {seg['text']}" for j, seg in enumerate(batch)
        )

        prompt = prompt_template.format(
            source_text    = combined,
            term_injection = term_inj,
        )

        response = translate(prompt)

        # 解析分隔符，恢復對應段落
        parts = re.split(r"\[PARA_SEP\]|\[\d+\]", response)
        parts = [p.strip() for p in parts if p.strip()]

        for k, part in enumerate(parts[:len(batch)]):
            results[i + k] = part

        # 補齊空缺
        for k in range(len(parts), len(batch)):
            results[i + k] = batch[k]["text"]   # fallback: 原文
            logger.warning(f"Missing translation for segment {i+k}, using source")

        logger.info(f"Translated batch {i//batch_size + 1}: segments {i}–{i+len(batch)-1}")

    return results


def run():
    logger.info(f"=== ft_nmt START — order: {cfg.ORDER_ID} ===")
    update_job_status("nmt_stage1", "running")

    try:
        # ── 1. 讀取前置資料 ──────────────────────────────────────────────
        segments = read_temp_json("segments.json")
        metadata = read_temp_json("metadata.json")
        terms    = get_terms(cfg.ORDER_ID)

        source_lang = metadata["source_lang"]
        target_lang = metadata["target_lang"]

        logger.info(f"NMT: {source_lang} → {target_lang}, {len(segments)} segments")

        # ── 2. Stage 1：台語 → 繁體中文 ──────────────────────────────────
        logger.info("Stage 1: Taiwanese → Traditional Chinese")
        zh_translations = translate_batch(
            segments       = segments,
            prompt_template = STAGE1_PROMPT,
            terms          = terms,
            target_lang    = "zh-tw",
        )

        # 儲存 Stage 1 中間結果
        stage1_data = [
            {
                "index":   seg["index"],
                "source":  seg["text"],
                "zh_text": zh_trans,
            }
            for seg, zh_trans in zip(segments, zh_translations)
        ]
        write_temp_json("stage1_zh.json", stage1_data)
        update_job_status("nmt_stage1", "success")

        # ── 3. Stage 2：繁體中文 → 目標語 ────────────────────────────────
        update_job_status("nmt_stage2", "running")
        logger.info(f"Stage 2: Traditional Chinese → {target_lang}")

        if target_lang not in STAGE2_PROMPTS:
            raise ValueError(f"Unsupported target language: {target_lang}")

        # 用 zh_text 作為 Stage 2 輸入
        zh_segments = [
            {"index": d["index"], "text": d["zh_text"]}
            for d in stage1_data
        ]

        target_translations = translate_batch(
            segments        = zh_segments,
            prompt_template = STAGE2_PROMPTS[target_lang],
            terms           = terms,
            target_lang     = target_lang,
        )

        # ── 4. 合併結果寫入 translations.json ────────────────────────────
        translations = [
            {
                "index":       seg["index"],
                "source":      seg["text"],
                "bridge_text": zh_trans,
                "translated":  tgt_trans,
            }
            for seg, zh_trans, tgt_trans in zip(
                segments, zh_translations, target_translations
            )
        ]
        write_temp_json("translations.json", translations)

        update_job_status("nmt_stage2", "success")
        logger.info(f"=== ft_nmt DONE — {len(translations)} segments translated ===")

    except Exception as e:
        logger.exception(f"ft_nmt FAILED: {e}")
        update_job_status("nmt_stage2", "failed", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
