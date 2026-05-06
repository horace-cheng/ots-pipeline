"""
lt_qa_checklist/main.py — Cloud Run Job

Literary Track Step 6: Auto QA Checklist
- 讀取最終譯文（proofreader 完成後）
- 輕量 QA 檢查（不對文學風格扣分）：
  - 段落數一致性
  - 漏譯偵測（空段落）
  - 數字/日期一致性（基本檢查）
- 寫入 qa_result.json 到 GCS temp
- 更新訂單狀態 → qa_review
"""

import sys, re, json, logging
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config  import cfg
from shared.db      import update_job_status, write_qa_flags, update_order_field, get_order_info
from shared.storage import read_temp_json, write_temp_json

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("lt_qa_checklist")


def check_structure(segments: list[dict], translations: list[dict]) -> tuple[dict, list[dict]]:
    """檢查段落數一致性和漏譯。"""
    flags = []
    summary = {"pass": True, "flags": 0, "details": []}

    if len(segments) != len(translations):
        summary["pass"] = False
        summary["flags"] += 1
        summary["details"].append(
            f"Segment count mismatch: {len(segments)} source vs {len(translations)} translated"
        )
        flags.append({
            "paragraph_index": 0,
            "flag_level": "must_fix",
            "flag_type": "segment_count_mismatch",
            "source_segment": f"{len(segments)} segments",
            "translated_segment": f"{len(translations)} segments",
        })

    for trans in translations:
        if not trans.get("translated", "").strip():
            idx = trans["index"]
            flags.append({
                "paragraph_index": idx,
                "flag_level": "must_fix",
                "flag_type": "missing_translation",
                "source_segment": segments[idx]["text"] if idx < len(segments) else "",
                "translated_segment": "",
            })
            summary["pass"] = False
            summary["flags"] += 1
            summary["details"].append(f"Segment {idx}: missing translation")

    return summary, flags


def check_numbers(segments: list[dict], translations: list[dict]) -> tuple[dict, list[dict]]:
    """檢查數字一致性（基本檢查：原文中的數字是否在譯文中保留）。"""
    flags = []
    summary = {"pass": True, "flags": 0, "details": []}

    number_re = re.compile(r"\d+")

    for seg, trans in zip(segments, translations):
        src_numbers = set(number_re.findall(seg.get("source", "")))
        tgt_numbers = set(number_re.findall(trans.get("translated", "")))

        # Filter out single-digit numbers (often part of grammar)
        src_significant = {n for n in src_numbers if len(n) >= 2}
        tgt_significant = {n for n in tgt_numbers if len(n) >= 2}

        missing = src_significant - tgt_significant
        if missing:
            idx = trans["index"]
            flags.append({
                "paragraph_index": idx,
                "flag_level": "review",
                "flag_type": "number_inconsistency",
                "source_segment": seg["source"],
                "translated_segment": trans["translated"],
            })
            summary["flags"] += 1
            summary["details"].append(f"Segment {idx}: numbers {missing} not in translation")

    return summary, flags


def run():
    logger.info(f"=== lt_qa_checklist START — order: {cfg.ORDER_ID} ===")
    update_job_status("lt_qa_checklist", "running")

    try:
        segments    = read_temp_json("segments.json")
        translations = read_temp_json("translations.json")
        metadata    = read_temp_json("metadata.json")

        logger.info(f"QA: {len(segments)} segments, {len(translations)} translations")

        # Layer 1: Structure
        structure_summary, structure_flags = check_structure(segments, translations)
        logger.info(f"Structure check: {'PASS' if structure_summary['pass'] else 'FAIL'} ({structure_summary['flags']} flags)")

        # Layer 2: Numbers
        number_summary, number_flags = check_numbers(segments, translations)
        logger.info(f"Number check: {number_summary['flags']} flags")

        all_flags = structure_flags + number_flags

        # QA 結果
        qa_result = {
            "layer1_structure":  structure_summary,
            "layer2_numbers":    number_summary,
            "must_fix_count":    sum(1 for f in all_flags if f["flag_level"] == "must_fix"),
            "review_count":      sum(1 for f in all_flags if f["flag_level"] == "review"),
            "total_flags":       len(all_flags),
        }

        write_temp_json("qa_result.json", qa_result)

        # 寫入 QA flags
        for flag in all_flags:
            flag["order_id"] = cfg.ORDER_ID

        write_qa_flags(all_flags)

        # 更新訂單狀態
        update_order_field("status", "qa_review")

        update_job_status("lt_qa_checklist", "success")
        logger.info(f"=== lt_qa_checklist DONE — {len(all_flags)} flags, {qa_result['must_fix_count']} must_fix ===")

    except Exception as e:
        logger.exception(f"lt_qa_checklist FAILED: {e}")
        update_job_status("lt_qa_checklist", "failed", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
