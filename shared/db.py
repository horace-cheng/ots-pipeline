"""
shared/db.py

Cloud Run Jobs 用同步 SQLAlchemy（非 async）。
Jobs 是單次執行，不需要 async 連線池。
"""

from sqlalchemy import create_engine, text
from functools import lru_cache
from sqlalchemy.orm import sessionmaker, Session
from contextlib import contextmanager
from shared.config import cfg
import logging

logger = logging.getLogger(__name__)

engine = create_engine(
    cfg.DB_URL.replace("postgresql+asyncpg://", "postgresql+psycopg2://"),
    pool_size=2,
    pool_pre_ping=True,
    pool_recycle=300,
)

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@contextmanager
def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def update_job_status(job_type: str, status: str,
                      qa_result: dict | None = None,
                      error_message: str | None = None):
    """更新或建立 pipeline_jobs 記錄"""
    with get_db() as db:
        existing = db.execute(text("""
            SELECT id FROM pipeline_jobs
            WHERE order_id = :order_id AND job_type = :job_type
        """), {"order_id": cfg.ORDER_ID, "job_type": job_type}).fetchone()

        if existing:
            db.execute(text("""
                UPDATE pipeline_jobs
                SET status        = :status,
                    qa_result     = cast(:qa_result as jsonb),
                    error_message = :error_message,
                    finished_at   = CASE WHEN :status IN ('success','failed','skipped')
                                         THEN NOW() ELSE finished_at END,
                    started_at    = CASE WHEN :status = 'running' AND started_at IS NULL
                                         THEN NOW() ELSE started_at END
                WHERE order_id = :order_id AND job_type = :job_type
            """), {
                "status":        status,
                "qa_result":     __import__("json").dumps(qa_result) if qa_result else None,
                "error_message": error_message,
                "order_id":      cfg.ORDER_ID,
                "job_type":      job_type,
            })
        else:
            import json
            db.execute(text("""
                INSERT INTO pipeline_jobs
                    (order_id, job_type, status, qa_result, error_message, started_at)
                VALUES
                    (:order_id, :job_type, :status,
                     cast(:qa_result as jsonb), :error_message,
                     CASE WHEN :status = 'running' THEN NOW() ELSE NULL END)
            """), {
                "order_id":      cfg.ORDER_ID,
                "job_type":      job_type,
                "status":        status,
                "qa_result":     json.dumps(qa_result) if qa_result else None,
                "error_message": error_message,
            })

        logger.info(f"Job status updated: {job_type} → {status}")


def get_order_info() -> dict:
    """取得訂單基本資訊"""
    with get_db() as db:
        row = db.execute(text("""
            SELECT o.id, o.track_type, o.source_lang, o.target_lang,
                   o.word_count, o.gcs_upload_path, o.term_dict_id
            FROM orders o
            WHERE o.id = :order_id
        """), {"order_id": cfg.ORDER_ID}).fetchone()

        if not row:
            raise ValueError(f"Order not found: {cfg.ORDER_ID}")
        return dict(row._mapping)


def update_order_field(field: str, value: str):
    """更新訂單單一欄位"""
    allowed = {"title", "gcs_upload_path", "gcs_output_path", "gcs_bilingual_output_path", "gcs_plain_text_output_path", "term_dict_id", "status"}
    if field not in allowed:
        raise ValueError(f"Field not allowed: {field}")
    with get_db() as db:
        db.execute(
            text(f"UPDATE orders SET {field} = :value WHERE id = :order_id"),
            {"value": value, "order_id": cfg.ORDER_ID}
        )


def log_token_usage(job_type: str, model: str,
                    prompt_tokens: int, candidates_tokens: int,
                    total_tokens: int):
    """Log a single Gemini API call's token usage to the token_usage table.
    
    Cost is calculated from the model pricing in PipelineConfig.
    The unit rates used are also stored for transparent cost display.
    """
    rate = cfg.MODEL_PRICING.get(model, {"input": 0, "output": 0})
    input_rate  = rate["input"]
    output_rate = rate["output"]
    cost_usd = (prompt_tokens / 1_000_000 * input_rate) + \
               (candidates_tokens / 1_000_000 * output_rate)
    with get_db() as db:
        db.execute(text("""
            INSERT INTO token_usage
                (order_id, job_type, model, prompt_tokens, candidates_tokens,
                 total_tokens, cost_usd, input_rate, output_rate)
            VALUES
                (:order_id, :job_type, :model, :prompt_tokens, :candidates_tokens,
                 :total_tokens, :cost_usd, :input_rate, :output_rate)
        """), {
            "order_id":          cfg.ORDER_ID,
            "job_type":          job_type,
            "model":             model,
            "prompt_tokens":     prompt_tokens,
            "candidates_tokens": candidates_tokens,
            "total_tokens":      total_tokens,
            "cost_usd":          round(cost_usd, 6),
            "input_rate":        input_rate,
            "output_rate":       output_rate,
        })
    logger.info(f"Logged token usage: {model} {prompt_tokens}+{candidates_tokens}={total_tokens} ${cost_usd:.6f} "
                f"(rate: {input_rate}/{output_rate} per 1M)")


def get_sample_package() -> dict | None:
    """取得訂單的試譯提案包（若存在）"""
    with get_db() as db:
        row = db.execute(text("""
            SELECT translator_bio, book_fact_sheet, synopsis, market_analysis
            FROM order_sample_packages
            WHERE order_id = :order_id AND status = 'generated'
        """), {"order_id": cfg.ORDER_ID}).fetchone()
        if not row:
            return None
        result = dict(row._mapping)
        if isinstance(result.get("book_fact_sheet"), str):
            result["book_fact_sheet"] = __import__("json").loads(result["book_fact_sheet"])
        return result


def write_qa_flags(flags: list[dict], job_type: str = "qa_auto"):
    """批次寫入 QA flags。job_type 預設 'qa_auto'（Fast Track），LT 可傳 'lt_qa_checklist'。

    For same order + same paragraph_index + same flag_type, only the latest
    record is kept — any existing flag for that combination is deleted before
    the new one is inserted.
    """
    if not flags:
        return
    with get_db() as db:
        job_row = db.execute(text("""
            SELECT id FROM pipeline_jobs
            WHERE order_id = :order_id AND job_type = :job_type
        """), {"order_id": cfg.ORDER_ID, "job_type": job_type}).fetchone()

        if not job_row:
            raise ValueError(f"{job_type} job not found, cannot write flags")

        job_id = str(job_row.id)
        for flag in flags:
            db.execute(text("""
                DELETE FROM qa_flags
                WHERE paragraph_index = :para_idx
                  AND flag_type = CAST(:flag_type AS flag_type)
                  AND job_id IN (
                      SELECT id FROM pipeline_jobs WHERE order_id = :order_id
                  )
            """), {
                "para_idx":  flag["paragraph_index"],
                "flag_type": flag["flag_type"],
                "order_id":  cfg.ORDER_ID,
            })
            db.execute(text("""
                INSERT INTO qa_flags
                    (job_id, paragraph_index, flag_level, flag_type,
                     source_segment, translated_segment)
                VALUES
                    (:job_id, :para_idx, :flag_level, :flag_type,
                     :source, :translated)
            """), {
                "job_id":     job_id,
                "para_idx":   flag["paragraph_index"],
                "flag_level": flag["flag_level"],
                "flag_type":  flag["flag_type"],
                "source":     flag.get("source_segment"),
                "translated": flag.get("translated_segment"),
            })
    logger.info(f"Wrote {len(flags)} QA flags for order {cfg.ORDER_ID}")


# ── Language labels (read from DB, fallback for codes not yet seeded) ──

_LANG_LABEL_FALLBACK: dict[str, dict[str, str]] = {
    "tai-lo":     {"en": "Taiwanese Hokkien",       "zh": "台語（台羅拼音）"},
    "hakka":      {"en": "Hakka",                   "zh": "客語"},
    "indigenous": {"en": "Taiwanese Indigenous",    "zh": "原住民族語"},
    "zh-tw":      {"en": "Traditional Chinese",      "zh": "繁體中文"},
    "zh-cn":      {"en": "Simplified Chinese",       "zh": "简体中文"},
    "en":         {"en": "English",                  "zh": "英語"},
    "ja":         {"en": "Japanese",                 "zh": "日語"},
    "ko":         {"en": "Korean",                   "zh": "韩語"},
    "fr":         {"en": "French",                   "zh": "法語"},
    "de":         {"en": "German",                   "zh": "德語"},
    "es":         {"en": "Spanish",                  "zh": "西班牙語"},
    "vi":         {"en": "Vietnamese",               "zh": "越南語"},
    "th":         {"en": "Thai",                     "zh": "泰語"},
    "cs":         {"en": "Czech",                    "zh": "捷克語"},
}


@lru_cache(maxsize=2)
def get_lang_labels(locale: str = "en") -> dict[str, str]:
    """Return {code: label} dict for all active languages.

    DB entries override the hardcoded fallback, so admins can
    customise labels via the language config panel without a deploy.
    """
    merged = {code: labels.copy() for code, labels in _LANG_LABEL_FALLBACK.items()}
    try:
        with get_db() as db:
            rows = db.execute(
                text("SELECT code, label_en, label_zh FROM language_configs WHERE is_active = true")
            ).fetchall()
        for row in rows:
            merged.setdefault(row.code, {})
            merged[row.code]["en"] = row.label_en
            merged[row.code]["zh"] = row.label_zh
    except Exception as e:
        logger.warning(f"Failed to read language_configs from DB, using fallback: {e}")
    return {code: labels.get(locale, code) for code, labels in merged.items()}
