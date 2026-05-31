"""
shared/storage.py

GCS 讀寫工具，供所有 Pipeline Jobs 使用。
Pipeline 資料存放於 BUCKET_TEMP，按 order_id 隔離。
"""

import json, re
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


# ── 預處理產物（segments / batches / metadata）───────────────────────────────
# 寫入一次，後續重啟可跳過預處理，直接從 NMT 階段開始。

PREPROCESS_SEGMENTS  = "segments.json"
PREPROCESS_BATCHES   = "batches.json"
PREPROCESS_METADATA  = "metadata.json"


def save_preprocess_artifacts(
    segments: list[dict],
    batches: list[dict],
    metadata: dict,
):
    """Write preprocess artifacts to GCS temp.

    Once written, subsequent runs can load these to skip re-preprocessing.
    """
    write_temp_json(PREPROCESS_SEGMENTS, segments)
    write_temp_json(PREPROCESS_BATCHES,  batches)
    write_temp_json(PREPROCESS_METADATA, metadata)
    logger.info(
        f"Preprocess artifacts saved: {len(segments)} segments, "
        f"{len(batches)} batches"
    )


def load_preprocess_artifacts() -> tuple[list[dict], list[dict], dict] | None:
    """Load preprocess artifacts from GCS temp.

    Returns (segments, batches, metadata) or None if any file is missing.
    """
    try:
        segments = read_temp_json(PREPROCESS_SEGMENTS)
        batches  = read_temp_json(PREPROCESS_BATCHES)
        metadata = read_temp_json(PREPROCESS_METADATA)
        if isinstance(segments, list) and isinstance(batches, list) and isinstance(metadata, dict):
            logger.info(
                f"Preprocess artifacts loaded: {len(segments)} segments, "
                f"{len(batches)} batches"
            )
            return segments, batches, metadata
    except Exception:
        pass
    return None


# ── 每批 NMT Checkpoint（單一檔案／無 Lock）───────────────────────────────────
# 每個 batch 寫入一獨立檔案，無需 lock、可無損恢復。

CHECKPOINT_BATCH_PREFIX = "checkpoint_batch_"


def _checkpoint_filename(batch_id: int) -> str:
    return f"{CHECKPOINT_BATCH_PREFIX}{batch_id}.json"


def save_batch_checkpoint(batch_id: int, data: dict):
    """Save one batch's translation result as a per-batch checkpoint file."""
    write_temp_json(_checkpoint_filename(batch_id), data)


def load_batch_checkpoint(batch_id: int) -> dict | None:
    """Load a per-batch checkpoint file, or None if missing/corrupted."""
    try:
        return read_temp_json(_checkpoint_filename(batch_id))
    except Exception:
        return None


def list_batch_checkpoints() -> list[int]:
    """Return sorted list of batch_ids that have checkpoint files on GCS."""
    client = get_client()
    bucket = client.bucket(cfg.BUCKET_TEMP)
    prefix = _temp_path(CHECKPOINT_BATCH_PREFIX)
    blobs  = list(bucket.list_blobs(prefix=prefix))
    ids: list[int] = []
    for b in blobs:
        m = re.search(rf"{CHECKPOINT_BATCH_PREFIX}(\d+)\.json$", b.name)
        if m:
            ids.append(int(m.group(1)))
    return sorted(ids)


def aggregate_checkpoints(
    batches: list[dict],
    total_segments: int,
) -> list[str]:
    """Aggregate all per-batch checkpoint files into an ordered translation list.

    Reads every checkpoint_batch_{id}.json, validates that all batch files
    exist and no segment is empty, then returns a flat list[str] ordered by
    segment index.

    Raises RuntimeError if any segment is empty (zero-empty-segment policy).
    """
    translations = [""] * total_segments

    for batch in batches:
        batch_id = batch["batch_id"]
        start    = batch["start"]
        count    = batch["count"]

        ckpt = load_batch_checkpoint(batch_id)
        if ckpt is None:
            raise RuntimeError(
                f"Missing checkpoint for batch {batch_id} — "
                f"cannot aggregate incomplete results"
            )

        parts = ckpt.get("translations", [])
        if len(parts) != count:
            raise RuntimeError(
                f"Checkpoint batch {batch_id}: expected {count} translations, "
                f"got {len(parts)}"
            )

        for offset, t in enumerate(parts):
            idx = start + offset
            if not t:
                raise RuntimeError(
                    f"Empty translation in batch {batch_id}, segment {idx} "
                    f"— zero-empty-segment policy enforced"
                )
            translations[idx] = t

    done = sum(1 for t in translations if t)
    logger.info(
        f"Aggregated {done}/{total_segments} translations from "
        f"{len(batches)} batch checkpoint(s)"
    )

    return translations


# ── 支援材料 ──────────────────────────────────────────────────────────────────
def list_support_files() -> list[dict]:
    """列出支援材料檔案（GCS uploads bucket 中的 orders/{order_id}/support/ 目錄）"""
    client = get_client()
    bucket = client.bucket(cfg.BUCKET_UPLOADS)
    prefix = f"orders/{cfg.ORDER_ID}/support/"

    blobs = list(bucket.list_blobs(prefix=prefix))
    return [
        {
            "name":         b.name,
            "size":         b.size,
            "content_type": b.content_type,
        }
        for b in blobs if not b.name.endswith("/")
    ]
