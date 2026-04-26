"""
shared/storage.py

GCS 讀寫工具，供所有 Pipeline Jobs 使用。
Pipeline 資料存放於 BUCKET_TEMP，按 order_id 隔離。
"""

import json
from google.cloud import storage
from shared.config import cfg
import logging

logger = logging.getLogger(__name__)
_client: storage.Client | None = None


def get_client() -> storage.Client:
    global _client
    if _client is None:
        _client = storage.Client(project=cfg.PROJECT_ID)
    return _client


def _temp_path(filename: str) -> str:
    return f"pipeline/{cfg.ORDER_ID}/{filename}"


# ── 讀取 ──────────────────────────────────────────────────────────────────────
def read_upload(gcs_path: str) -> bytes:
    """讀取客戶上傳的原始檔案"""
    client  = get_client()
    bucket  = client.bucket(cfg.BUCKET_UPLOADS)
    # gcs_path 格式：orders/{order_id}/{filename}
    blob_path = gcs_path.replace(f"gs://{cfg.BUCKET_UPLOADS}/", "")
    return bucket.blob(blob_path).download_as_bytes()


def read_temp_json(filename: str) -> dict | list:
    """從 temp bucket 讀取 JSON 中間產物"""
    client  = get_client()
    bucket  = client.bucket(cfg.BUCKET_TEMP)
    data    = bucket.blob(_temp_path(filename)).download_as_text(encoding="utf-8")
    return json.loads(data)


def read_temp_text(filename: str) -> str:
    """從 temp bucket 讀取純文字中間產物"""
    client = get_client()
    bucket = client.bucket(cfg.BUCKET_TEMP)
    return bucket.blob(_temp_path(filename)).download_as_text(encoding="utf-8")


# ── 寫入 ──────────────────────────────────────────────────────────────────────
def write_temp_json(filename: str, data: dict | list):
    """寫入 JSON 中間產物到 temp bucket"""
    client  = get_client()
    bucket  = client.bucket(cfg.BUCKET_TEMP)
    blob    = bucket.blob(_temp_path(filename))
    blob.upload_from_string(
        json.dumps(data, ensure_ascii=False, indent=2),
        content_type="application/json",
    )
    logger.info(f"Written temp: {_temp_path(filename)}")


def write_output(filename: str, content: str, content_type: str = "text/plain") -> str:
    """寫入最終交付檔案到 outputs bucket，回傳 GCS path"""
    client    = get_client()
    bucket    = client.bucket(cfg.BUCKET_OUTPUTS)
    gcs_path  = f"orders/{cfg.ORDER_ID}/{filename}"
    blob      = bucket.blob(gcs_path)
    blob.upload_from_string(content.encode("utf-8"), content_type=content_type)
    full_path = f"gs://{cfg.BUCKET_OUTPUTS}/{gcs_path}"
    logger.info(f"Written output: {full_path}")
    return full_path
