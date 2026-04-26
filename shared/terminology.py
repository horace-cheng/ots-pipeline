"""
shared/terminology.py

Firestore 術語詞庫操作。
每案一個 document，key = order_id。
"""

from google.cloud import firestore
from shared.config import cfg
import logging

logger = logging.getLogger(__name__)
_db: firestore.Client | None = None


def get_db() -> firestore.Client:
    global _db
    if _db is None:
        _db = firestore.Client(project=cfg.PROJECT_ID)
    return _db


def get_terms(order_id: str) -> dict:
    """取得術語詞庫（source_text → target_text）"""
    db  = get_db()
    doc = db.collection("terminology").document(order_id).get()
    if not doc.exists:
        return {}
    data = doc.to_dict()
    return data.get("terms", {})


def init_terms(order_id: str, source_lang: str, target_lang: str) -> str:
    """
    初始化空術語詞庫，回傳 document ID。
    若已存在則不覆蓋。
    """
    db  = get_db()
    ref = db.collection("terminology").document(order_id)
    doc = ref.get()
    if not doc.exists:
        ref.set({
            "source_lang": source_lang,
            "target_lang": target_lang,
            "terms":       {},
            "created_at":  firestore.SERVER_TIMESTAMP,
            "updated_at":  firestore.SERVER_TIMESTAMP,
        })
        logger.info(f"Terminology dict initialized: {order_id}")
    return order_id


def add_term(order_id: str, source: str, target: str):
    """新增或更新單一術語"""
    db  = get_db()
    ref = db.collection("terminology").document(order_id)
    ref.update({
        f"terms.{source}": target,
        "updated_at": firestore.SERVER_TIMESTAMP,
    })


def scan_terminology_inconsistencies(
    paragraphs: list[dict],
    terms: dict,
) -> list[dict]:
    """
    Layer 3 術語一致性掃描。
    paragraphs: [{"source": str, "translated": str, "index": int}, ...]
    回傳 flag list。
    """
    flags = []
    # 建立 target_text → [expected source term] 反向索引
    target_to_source = {}
    for src, tgt in terms.items():
        target_to_source.setdefault(tgt, []).append(src)

    for para in paragraphs:
        src_text   = para.get("source", "")
        trans_text = para.get("translated", "")
        idx        = para.get("index", 0)

        for src_term, tgt_term in terms.items():
            # 原文包含此術語，但譯文用了不同翻譯
            if src_term in src_text and tgt_term not in trans_text:
                # 確認是否用了其他已知的版本
                other_versions = [
                    t for t in terms.values()
                    if t != tgt_term and t in trans_text
                ]
                if other_versions or src_term in trans_text:
                    flags.append({
                        "paragraph_index": idx,
                        "flag_level":      "must_fix",
                        "flag_type":       "terminology_mismatch",
                        "source_segment":  src_text[:200],
                        "translated_segment": trans_text[:200],
                    })
                    break  # 每段只標記一次

    return flags
