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

    # Frontend URL (for serving static assets like fonts in deliverable HTML)
    WEB_PORTAL_URL:  str = os.environ.get("WEB_PORTAL_URL", "https://ots-frontend-dev-miptn5nxpa-de.a.run.app")

    # API base URL (for Cloud Tasks notification callbacks)
    # Override via API_BASE_URL env var for each environment.
    API_BASE_URL:    str = os.environ.get(
        "API_BASE_URL",
        f"https://ots-api-backend-{os.environ.get('ENV', 'dev')}-miptn5nxpa-de.a.run.app"
    )

    # Vertex AI / Gemini
    GEMINI_PRO_MODEL:   str = "gemini-3.5-flash"
    GEMINI_FLASH_MODEL: str = "gemini-3.5-flash"

    # Token pricing (USD per 1M tokens)
    MODEL_PRICING: dict[str, dict[str, float]] = {
        "gemini-2.5-pro": {
            "input":  float(os.environ.get("GEMINI_PRO_INPUT_COST", "1.25")),
            "output": float(os.environ.get("GEMINI_PRO_OUTPUT_COST", "10.00")),
        },
        "gemini-2.5-flash": {
            "input":  float(os.environ.get("GEMINI_FLASH_INPUT_COST", "0.30")),
            "output": float(os.environ.get("GEMINI_FLASH_OUTPUT_COST", "2.50")),
        },
        "gemini-3.5-flash": {
            "input":  float(os.environ.get("GEMINI_35_FLASH_INPUT_COST", "0.50")),
            "output": float(os.environ.get("GEMINI_35_FLASH_OUTPUT_COST", "3.00")),
        },
    }

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
