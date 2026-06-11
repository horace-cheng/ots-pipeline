"""
shared/notify.py

透過 Cloud Tasks 非同步發送 pipeline stage 通知到 API，
由 API 內部端點發布至 Pub/Sub 並寄送 email。
"""

import json
import logging

from shared.config import cfg

logger = logging.getLogger(__name__)

GT_STAGE_LABELS = {
    "gt_translate": "翻譯（Translate）",
    "gt_simplify": "簡化（Simplify）",
    "gt_tailo": "台語化（Tâi-lô）",
    "gt_deliver": "遞送完成（Deliver）",
}


def notify_stage(job_type: str):
    """發送 Gutenberg pipeline 階段完成通知至 Cloud Tasks。

    job_type: 'gt_translate', 'gt_simplify', 'gt_tailo', 'gt_deliver'
    """
    try:
        from google.cloud import tasks_v2

        stage_label = GT_STAGE_LABELS.get(job_type, job_type)

        client = tasks_v2.CloudTasksClient()
        queue_path = client.queue_path(cfg.PROJECT_ID, cfg.REGION, f"ots-notify-{cfg.ENV}")

        payload = json.dumps({
            "type": "gt_stage_complete",
            "order_id": cfg.ORDER_ID,
            "stage": job_type,
            "stage_label": stage_label,
        })

        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"{cfg.API_BASE_URL}/internal/notify",
                "headers": {"Content-Type": "application/json"},
                "body": payload.encode(),
                "oidc_token": {
                    "service_account_email": f"ots-pipeline-{cfg.ENV}@{cfg.PROJECT_ID}.iam.gserviceaccount.com",
                },
            }
        }

        client.create_task(parent=queue_path, task=task)
        logger.info(f"Stage notification sent: {job_type} for order {cfg.ORDER_ID}")
    except Exception as e:
        logger.warning(f"Failed to send stage notification (non-critical): {e}")
