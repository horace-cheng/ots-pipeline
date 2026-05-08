"""
lt_deliver/main.py — Cloud Run Job

Literary Track Step 7: Deliver
- 讀取最終譯文（proofreader 完成後）
- 產生交付格式（TXT + HTML）
- 上傳到 GCS outputs bucket
- 更新 orders.gcs_output_path → HTML path
- 更新 orders.status → delivered
- 寫入 BigQuery corpus（track_type = "literary"）
- 觸發 email 通知
"""

import sys, json, re, logging
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config  import cfg
from shared.db      import update_job_status, get_order_info, update_order_field, get_db
from shared.storage import read_temp_json, write_output

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("lt_deliver")

LANG_LABELS = {
    "tai-lo":     "台語（台羅拼音）",
    "hakka":      "客語",
    "indigenous": "原住民族語",
    "zh-tw":      "繁體中文",
    "en":         "English",
    "ja":         "日本語",
    "ko":         "한국어",
}


def format_txt(translations: list[dict], metadata: dict) -> str:
    order    = metadata.get("order_id", "")
    src_lang = LANG_LABELS.get(metadata.get("source_lang", ""), metadata.get("source_lang", ""))
    tgt_lang = LANG_LABELS.get(metadata.get("target_lang", ""), metadata.get("target_lang", ""))
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        "=" * 60,
        f"OTS 翻譯服務 — Literary Track（文學精譯）",
        f"訂單編號：{order}",
        f"語言方向：{src_lang} → {tgt_lang}",
        f"交付日期：{now}",
        "=" * 60,
        "",
        "【譯文】",
        "",
    ]

    for trans in sorted(translations, key=lambda x: x["index"]):
        lines.append(trans["translated"])
        lines.append("")

    lines += [
        "=" * 60,
        "本譯文由 OTS 翻譯服務提供（AI 初稿 + 編輯審閱 + 校對審閱）。",
        "如有任何疑問，請聯繫 service@ots.tw",
        "=" * 60,
    ]

    return "\n".join(lines)


def format_html(translations: list[dict], metadata: dict,
                qa_result: dict | None = None) -> str:
    order    = metadata.get("order_id", "")
    src_lang = LANG_LABELS.get(metadata.get("source_lang", ""), "")
    tgt_lang = LANG_LABELS.get(metadata.get("target_lang", ""), "")
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    para_html = "\n".join(
        f"<p class='para'>{trans['translated']}</p>"
        for trans in sorted(translations, key=lambda x: x["index"])
    )

    qa_score = ""
    if qa_result and qa_result.get("layer4_llm_judge"):
        score = qa_result["layer4_llm_judge"].get("score", "")
        qa_score = f"<span class='qa-score'>QA 評分：{score}/100</span>"

    return f"""<!DOCTYPE html>
<html lang="{metadata.get('target_lang', 'en')}">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>OTS 翻譯 — {order}</title>
<style>
  body {{ font-family: Georgia, serif; max-width: 800px; margin: 0 auto; padding: 2rem; color: #333; }}
  header {{ border-bottom: 2px solid #8B5CF6; padding-bottom: 1rem; margin-bottom: 2rem; }}
  h1 {{ color: #8B5CF6; font-size: 1.4rem; margin-bottom: 0.5rem; }}
  .meta {{ color: #666; font-size: 0.9rem; }}
  .para {{ line-height: 1.8; margin-bottom: 1.2em; text-align: justify; }}
  .qa-score {{ display: inline-block; background: #F3E8FF; color: #7C3AED;
               padding: 0.2rem 0.6rem; border-radius: 4px; font-size: 0.85rem; }}
  footer {{ margin-top: 3rem; border-top: 1px solid #ccc; padding-top: 1rem;
            color: #999; font-size: 0.8rem; }}
</style>
</head>
<body>
<header>
  <h1>OTS 翻譯服務 — Literary Track（文學精譯）</h1>
  <div class="meta">
    <span>訂單：{order}</span> &nbsp;|&nbsp;
    <span>{src_lang} → {tgt_lang}</span> &nbsp;|&nbsp;
    <span>{now}</span>
    {f'&nbsp;|&nbsp; {qa_score}' if qa_score else ''}
  </div>
</header>
<main>
{para_html}
</main>
<footer>
  本譯文由 OTS 翻譯服務提供（AI 初稿 + 編輯審閱 + 校對審閱）。
  如有疑問請聯繫 service@ots.tw
</footer>
</body>
</html>"""


def write_corpus(translations: list[dict], metadata: dict):
    """寫入 BigQuery 語料（track_type = "literary"）。"""
    from sqlalchemy import text as sqla_text
    with get_db() as db:
        row = db.execute(sqla_text("""
            SELECT consent_given FROM corpus_log WHERE order_id = :order_id
        """), {"order_id": cfg.ORDER_ID}).fetchone()

        if not row or not row.consent_given:
            logger.info("Corpus consent not given, skipping BigQuery write")
            return

    try:
        from google.cloud import bigquery
        client  = bigquery.Client(project=cfg.PROJECT_ID)
        dataset = f"ots_corpus_{cfg.ENV}"
        table   = f"{cfg.PROJECT_ID}.{dataset}.corpus_pairs"
        now     = datetime.now(timezone.utc).isoformat()

        rows = [
            {
                "order_id":        cfg.ORDER_ID,
                "source_lang":     metadata["source_lang"],
                "target_lang":     metadata["target_lang"],
                "source_text":     t["source"],
                "translated_text": t["translated"],
                "track_type":      "literary",
                "consent_given":   True,
                "created_at":      now,
            }
            for t in translations
        ]

        errors = client.insert_rows_json(table, rows)
        if errors:
            logger.error(f"BigQuery insert errors: {errors}")
        else:
            logger.info(f"Corpus written: {len(rows)} rows to BigQuery (literary track)")

            with get_db() as db:
                db.execute(sqla_text("""
                    UPDATE corpus_log
                    SET bq_row_id = :bq_id
                    WHERE order_id = :order_id
                """), {"bq_id": f"bq-{cfg.ORDER_ID}", "order_id": cfg.ORDER_ID})

    except Exception as e:
        logger.warning(f"BigQuery write failed (non-critical): {e}")


def notify_delivery():
    """透過 Cloud Tasks 發送交付通知 email。"""
    import os
    try:
        from google.cloud import tasks_v2
        client    = tasks_v2.CloudTasksClient()
        queue_path = client.queue_path(
            cfg.PROJECT_ID, cfg.REGION, f"ots-notify-{cfg.ENV}"
        )
        payload = json.dumps({"type": "delivery_complete", "order_id": cfg.ORDER_ID})
        task = {
            "http_request": {
                "http_method": tasks_v2.HttpMethod.POST,
                "url": f"https://ots-api-backend-{cfg.ENV}-{cfg.PROJECT_ID}.asia-east1.run.app/internal/notify",
                "headers": {"Content-Type": "application/json"},
                "body": payload.encode(),
                "oidc_token": {
                    "service_account_email": f"ots-pipeline-{cfg.ENV}@{cfg.PROJECT_ID}.iam.gserviceaccount.com"
                },
            }
        }
        client.create_task(parent=queue_path, task=task)
        logger.info(f"Delivery notification task created for order {cfg.ORDER_ID}")
    except Exception as e:
        logger.warning(f"Failed to create notification task (non-critical): {e}")


def run():
    logger.info(f"=== lt_deliver START — order: {cfg.ORDER_ID} ===")
    update_job_status("lt_deliver", "running")

    try:
        translations = read_temp_json("translations.json")
        metadata     = read_temp_json("metadata.json")

        qa_result = None
        try:
            qa_result = read_temp_json("qa_result.json")
        except Exception:
            logger.warning("qa_result.json not found, proceeding without QA summary")

        logger.info(f"Formatting output: {len(translations)} segments")

        txt_content  = format_txt(translations, metadata)
        html_content = format_html(translations, metadata, qa_result)

        order_short  = cfg.ORDER_ID[:8]
        tgt_lang     = metadata.get("target_lang", "en")
        now_str      = datetime.now(timezone.utc).strftime("%Y%m%d")

        txt_path  = write_output(f"translation_{tgt_lang}_{now_str}.txt",  txt_content,  "text/plain")
        html_path = write_output(f"translation_{tgt_lang}_{now_str}.html", html_content, "text/html")

        update_order_field("gcs_output_path", html_path)

        from sqlalchemy import text as sqla_text
        with get_db() as db:
            db.execute(sqla_text("""
                UPDATE orders
                SET status       = 'delivered',
                    delivered_at = NOW()
                WHERE id = :order_id
            """), {"order_id": cfg.ORDER_ID})

        write_corpus(translations, metadata)

        notify_delivery()

        update_job_status("lt_deliver", "success")
        logger.info(f"=== lt_deliver DONE — output: {html_path} ===")

    except Exception as e:
        logger.exception(f"lt_deliver FAILED: {e}")
        update_job_status("lt_deliver", "failed", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
