"""
shared/db.py

Cloud Run Jobs 用同步 SQLAlchemy（非 async）。
Jobs 是單次執行，不需要 async 連線池。
"""

from sqlalchemy import create_engine, text
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
    allowed = {"gcs_upload_path", "gcs_output_path", "term_dict_id", "status"}
    if field not in allowed:
        raise ValueError(f"Field not allowed: {field}")
    with get_db() as db:
        db.execute(
            text(f"UPDATE orders SET {field} = :value WHERE id = :order_id"),
            {"value": value, "order_id": cfg.ORDER_ID}
        )


def write_qa_flags(flags: list[dict], job_type: str = "qa_auto"):
    """批次寫入 QA flags。job_type 預設 'qa_auto'（Fast Track），LT 可傳 'lt_qa_checklist'。"""
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
                INSERT INTO qa_flags
                    (job_id, paragraph_index, flag_level, flag_type,
                     source_segment, translated_segment)
                VALUES
                    (:job_id, :para_idx, :flag_level, :flag_type,
                     :source, :translated)
                ON CONFLICT DO NOTHING
            """), {
                "job_id":     job_id,
                "para_idx":   flag["paragraph_index"],
                "flag_level": flag["flag_level"],
                "flag_type":  flag["flag_type"],
                "source":     flag.get("source_segment"),
                "translated": flag.get("translated_segment"),
            })
    logger.info(f"Wrote {len(flags)} QA flags for order {cfg.ORDER_ID}")
