"""
shared/config.py

所有 Pipeline Jobs 共用的環境設定。
每個 Job 由 Cloud Run 注入 ORDER_ID 環境變數。
"""

import os

class PipelineConfig:
    ORDER_ID:    str = os.environ.get("ORDER_ID", "")
    PROJECT_ID:  str = os.environ.get("PROJECT_ID", "ots-translation")
    ENV:         str = os.environ.get("ENV", "dev")
    REGION:      str = os.environ.get("REGION", "asia-east1")

    # Cloud SQL（Unix socket via Auth Proxy）
    DB_URL:      str = os.environ.get("DB_URL", "")

    # GCS
    BUCKET_UPLOADS:  str = os.environ.get("GCS_UPLOADS_BUCKET",  "ots-translation-uploads-dev")
    BUCKET_OUTPUTS:  str = os.environ.get("GCS_OUTPUTS_BUCKET",  "ots-translation-outputs-dev")
    BUCKET_TEMP:     str = os.environ.get("GCS_TEMP_BUCKET",     "ots-translation-pipeline-temp-dev")

    # Vertex AI / Gemini
    GEMINI_PRO_MODEL:   str = "gemini-2.5-flash"  # 2.5-pro 需 Preview access，暫用 flash
    GEMINI_FLASH_MODEL: str = "gemini-2.5-flash"

    # QA 閾值
    LENGTH_RATIO_MIN_TAI_ZH: float = 0.7
    LENGTH_RATIO_MAX_TAI_ZH: float = 1.1
    LENGTH_RATIO_MIN_TAI_EN: float = 0.4
    LENGTH_RATIO_MAX_TAI_EN: float = 0.85
    LENGTH_RATIO_MIN_TAI_JA: float = 0.5
    LENGTH_RATIO_MAX_TAI_JA: float = 0.9

    # LLM-as-Judge 最低可讀性分數（0–100）
    LLM_JUDGE_MIN_SCORE: float = 60.0

    # Semantic 距離閾值（越低越相似）
    SEMANTIC_DRIFT_THRESHOLD: float = 0.35

    def __init__(self):
        if not self.ORDER_ID:
            raise ValueError("ORDER_ID environment variable is required")
        if not self.DB_URL:
            raise ValueError("DB_URL environment variable is required")


cfg = PipelineConfig()
