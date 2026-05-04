"""
ft_qa_auto/main.py — Cloud Run Job

Fast Track Step 3: 自動化 QA（4 層）
Layer 1: 結構驗證（段落數、長度比例、標點）
Layer 2: 語意保留（back-translation + 相似度估算）
Layer 3: 術語一致性掃描
Layer 4: LLM-as-Judge（Gemini Flash 可讀性評分）
"""

import sys, re, json, logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config     import cfg
from shared.db         import update_job_status, write_qa_flags, update_order_field
from shared.storage    import read_temp_json, write_temp_json
from shared.gemini     import judge
from shared.terminology import get_terms, scan_terminology_inconsistencies

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ft_qa_auto")


# ── Layer 1：結構驗證 ─────────────────────────────────────────────────────────
def layer1_structure(segments: list[dict], translations: list[dict],
                     source_lang: str, target_lang: str) -> tuple[dict, list[dict]]:
    """
    驗證：
    1. 段落數一致
    2. 長度比例在正常區間
    3. 非空段落（漏譯偵測）
    """
    flags = []

    # 定義語言對的長度比例區間
    ratio_ranges = {
        # Taiwanese (Han/Tailo) -> ZH_TW
        ("tai-lo", "zh-tw"):   (cfg.LENGTH_RATIO_MIN_TAI_ZH, cfg.LENGTH_RATIO_MAX_TAI_ZH),
        ("hakka",  "zh-tw"):   (cfg.LENGTH_RATIO_MIN_TAI_ZH, cfg.LENGTH_RATIO_MAX_TAI_ZH),
        # Taiwanese -> Foreign
        ("tai-lo", "en"):      (cfg.LENGTH_RATIO_MIN_TAI_EN, cfg.LENGTH_RATIO_MAX_TAI_EN),
        ("tai-lo", "ja"):      (cfg.LENGTH_RATIO_MIN_TAI_JA, cfg.LENGTH_RATIO_MAX_TAI_JA),
        ("tai-lo", "ko"):      (0.5, 1.0),
        # ZH_TW -> Foreign (some pipelines might use ZH_TW as intermediate)
        ("zh-tw",  "en"):      (cfg.LENGTH_RATIO_MIN_TAI_EN, cfg.LENGTH_RATIO_MAX_TAI_EN),
        ("zh-tw",  "ja"):      (cfg.LENGTH_RATIO_MIN_TAI_JA, cfg.LENGTH_RATIO_MAX_TAI_JA),
        ("zh-tw",  "ko"):      (0.5, 1.0),
    }

    # 段落數檢查
    if len(segments) != len(translations):
        flags.append({
            "paragraph_index": 0,
            "flag_level":      "must_fix",
            "flag_type":       "missing_segment",
            "source_segment":  f"Expected {len(segments)} segments, got {len(translations)}",
            "translated_segment": "",
        })

    total_src_len  = 0
    total_tgt_len  = 0
    flag_count     = 0
    pass_count     = 0

    for trans in translations:
        idx         = trans["index"]
        src_text    = trans["source"]
        tgt_text    = trans["translated"]
        src_len     = len(src_text)
        tgt_len     = len(tgt_text)
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
        # 當來源是台語/客語漢字而目標是英文/日文/韓文時，譯文中不應該出現大量原文漢字
        if src_len > 50 and target_lang in ("en", "ja", "ko"):
            cjk_pattern = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
            src_cjk = cjk_pattern.findall(src_text)
            tgt_cjk = cjk_pattern.findall(tgt_text)
            if src_cjk:
                tgt_cjk_set = set(tgt_cjk)
                # 計算有多少原始漢字仍然出現在譯文中
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
    logger.info(f"Layer 1: pass={result['pass']}, flags={flag_count}, ratio={overall_ratio:.3f}")
    return result, flags


# ── Layer 2：語意保留（簡化版 back-translation 評估）─────────────────────────
def layer2_semantic(translations: list[dict]) -> tuple[dict, list[dict]]:
    """
    簡化的語意保留檢查：
    對每段做 sampling（不是全部），用 LLM 快速評估是否有明顯語意漂移。
    完整 COMET-QE 在獨立的 Cloud Run service 執行（此處用 LLM 替代）。
    只對長段落（> 100 字）做 sampling。
    """
    flags = []
    pass_count = 0
    flag_count = 0
    total_scored = 0
    scores = []

    # 只對長段落做語意檢查（控制 API 成本）
    long_paras = [t for t in translations if len(t["source"]) > 100]
    sample_size = min(len(long_paras), 10)  # 最多抽樣 10 段

    import random
    sample = random.sample(long_paras, sample_size) if len(long_paras) > sample_size else long_paras

    for trans in sample:
        idx      = trans["index"]
        src_text = trans["source"]
        tgt_text = trans["translated"]

        prompt = f"""Rate the semantic preservation of this translation on a scale of 0-100.
Focus only on: does the translation preserve the core meaning and key information?
Output ONLY a JSON object: {{"score": <number>, "issue": "<brief description or empty>"}}

Source: {src_text[:300]}

Translation: {tgt_text[:300]}"""

        try:
            response = judge(prompt)
            # 解析 JSON 回應
            clean = re.sub(r"```json|```", "", response).strip()
            data  = json.loads(clean)
            score = float(data.get("score", 70))
            issue = data.get("issue", "")
            scores.append(score)
            total_scored += 1

            if score < 50:
                flags.append({
                    "paragraph_index": idx,
                    "flag_level":      "must_fix",
                    "flag_type":       "semantic_drift",
                    "source_segment":  src_text,
                    "translated_segment": tgt_text,
                })
                flag_count += 1
            elif score < 65:
                flags.append({
                    "paragraph_index": idx,
                    "flag_level":      "review",
                    "flag_type":       "semantic_drift",
                    "source_segment":  src_text,
                    "translated_segment": tgt_text,
                })
                flag_count += 1
            else:
                pass_count += 1

        except Exception as e:
            logger.warning(f"Layer 2 scoring failed for segment {idx}: {e}")

    avg_score = sum(scores) / len(scores) if scores else 75.0
    result = {
        "pass":      flag_count == 0,
        "flags":     flag_count,
        "avg_score": round(avg_score, 1),
        "sampled":   total_scored,
    }
    logger.info(f"Layer 2: pass={result['pass']}, avg_score={avg_score:.1f}, sampled={total_scored}")
    return result, flags


# ── Layer 3：術語一致性 ───────────────────────────────────────────────────────
def layer3_terminology(translations: list[dict]) -> tuple[dict, list[dict]]:
    terms = get_terms(cfg.ORDER_ID)

    if not terms:
        logger.info("Layer 3: no terminology dict, skipping")
        return {"pass": True, "flags": 0, "terms_checked": 0}, []

    para_list = [
        {"index": t["index"], "source": t["source"], "translated": t["translated"]}
        for t in translations
    ]
    flags = scan_terminology_inconsistencies(para_list, terms)
    result = {
        "pass":          len(flags) == 0,
        "flags":         len(flags),
        "terms_checked": len(terms),
    }
    logger.info(f"Layer 3: pass={result['pass']}, flags={len(flags)}, terms={len(terms)}")
    return result, flags


# ── Layer 4：LLM-as-Judge 可讀性評分 ─────────────────────────────────────────
def layer4_llm_judge(translations: list[dict], target_lang: str) -> tuple[dict, list[dict]]:
    """
    Gemini Flash 對譯文做整體可讀性評分。
    以 5 段為一批，降低 API 呼叫次數。
    """
    flags      = []
    all_scores = []

    lang_labels = {"en": "English", "ja": "Japanese", "ko": "Korean", "zh-tw": "Traditional Chinese"}
    lang_label  = lang_labels.get(target_lang, target_lang)

    batch_size = 5
    for i in range(0, len(translations), batch_size):
        batch = translations[i:i + batch_size]
        combined = "\n\n---\n\n".join(
            f"[Segment {t['index']+1}]\n{t['translated']}"
            for t in batch
        )

        prompt = f"""You are evaluating {lang_label} translation quality for readability only.
For each numbered segment, rate readability 0-100.
A score of 60+ means: grammatically acceptable and conveys meaning clearly.
Do NOT penalize for literary style or naturalness — only penalize serious grammar errors or incomprehensibility.

Output ONLY valid JSON array: [{{"index": 1, "score": 85, "note": "brief note or empty"}}, ...]

Segments to evaluate:
{combined}"""

        try:
            response = judge(prompt)
            clean    = re.sub(r"```json|```", "", response).strip()
            results  = json.loads(clean)

            for item, trans in zip(results, batch):
                score = float(item.get("score", 70))
                note  = item.get("note", "")
                all_scores.append(score)

                if score < cfg.LLM_JUDGE_MIN_SCORE:
                    flags.append({
                        "paragraph_index": trans["index"],
                        "flag_level":      "must_fix" if score < 40 else "review",
                        "flag_type":       "readability_low",
                        "source_segment":  trans["source"],
                        "translated_segment": trans["translated"],
                    })

        except Exception as e:
            logger.warning(f"Layer 4 batch {i//batch_size} failed: {e}")

    avg_score = sum(all_scores) / len(all_scores) if all_scores else 70.0
    result = {
        "pass":      len(flags) == 0,
        "score":     round(avg_score, 1),
        "flags":     len(flags),
        "evaluated": len(all_scores),
    }
    logger.info(f"Layer 4: pass={result['pass']}, avg_score={avg_score:.1f}, flags={len(flags)}")
    return result, flags


def run():
    logger.info(f"=== ft_qa_auto START — order: {cfg.ORDER_ID} ===")
    update_job_status("qa_auto", "running")
    update_order_field("status", "processing")

    try:
        translations = read_temp_json("translations.json")
        segments     = read_temp_json("segments.json")
        metadata     = read_temp_json("metadata.json")
        source_lang  = metadata["source_lang"]
        target_lang  = metadata["target_lang"]

        all_flags = []

        # ── Layer 1 ───────────────────────────────────────────────────────
        l1_result, l1_flags = layer1_structure(
            segments, translations, source_lang, target_lang
        )
        all_flags.extend(l1_flags)

        # ── Layer 2 ───────────────────────────────────────────────────────
        l2_result, l2_flags = layer2_semantic(translations)
        all_flags.extend(l2_flags)

        # ── Layer 3 ───────────────────────────────────────────────────────
        l3_result, l3_flags = layer3_terminology(translations)
        all_flags.extend(l3_flags)

        # ── Layer 4 ───────────────────────────────────────────────────────
        l4_result, l4_flags = layer4_llm_judge(translations, target_lang)
        all_flags.extend(l4_flags)

        # ── 寫入 DB ───────────────────────────────────────────────────────
        write_qa_flags(all_flags)

        must_fix_count = sum(1 for f in all_flags if f["flag_level"] == "must_fix")
        qa_result = {
            "layer1_structure":   l1_result,
            "layer2_semantic":    l2_result,
            "layer3_terminology": l3_result,
            "layer4_llm_judge":   l4_result,
            "must_fix_count":     must_fix_count,
        }

        # QA 結果寫入 temp，供後續 Job 讀取
        write_temp_json("qa_result.json", qa_result)

        if must_fix_count > 0:
            update_order_field("status", "qa_review")

        update_job_status("qa_auto", "success", qa_result=qa_result)
        logger.info(f"=== ft_qa_auto DONE — total_flags={len(all_flags)}, must_fix={must_fix_count} ===")

    except Exception as e:
        logger.exception(f"ft_qa_auto FAILED: {e}")
        update_job_status("qa_auto", "failed", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
