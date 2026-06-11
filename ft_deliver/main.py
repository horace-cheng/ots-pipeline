"""
ft_deliver/main.py — Cloud Run Job

Fast Track Step 4: 格式化交付
- 讀取 translations.json 和 qa_result.json
- 產生結構化譯文（TXT + HTML 兩種格式）
- 上傳到 GCS outputs bucket
- 更新 orders.status → delivered
- 觸發 email 通知（透過 Cloud Tasks）
- 寫入 BigQuery corpus（若 consent_given = true）

HTML uses the shared ``shared.deliver_html`` template for a clean,
book-style reading experience.
"""

import sys, json, re, logging, os
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import text as sqla_text

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config  import cfg
from shared.db      import update_job_status, get_order_info, update_order_field, get_db, get_lang_labels
from shared.deliver_html import html_text, render_doc, table_open, table_close
from shared.storage import read_temp_json, write_output
from shared.gemini  import judge

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ft_deliver")


LANG_LABELS = get_lang_labels("zh")


def format_plain_text(translations: list[dict]) -> str:
    """產生純文字譯文（無 header/footer/metadata，僅譯文段落）。"""
    paras = [trans["translated"] for trans in sorted(translations, key=lambda x: x["index"])]
    return "\n\n".join(paras)


def format_txt(translations: list[dict], metadata: dict) -> str:
    """產生純文字格式譯文"""
    order    = metadata.get("order_id", "")
    src_lang = LANG_LABELS.get(metadata.get("source_lang", ""), metadata.get("source_lang", ""))
    tgt_lang = LANG_LABELS.get(metadata.get("target_lang", ""), metadata.get("target_lang", ""))
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        "=" * 60,
        f"OTS 翻譯服務 — Fast Track",
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
        "本譯文由 OTS 翻譯服務（AI 輔助翻譯）提供。",
        "如有任何疑問，請聯繫 service@ots.tw",
        "=" * 60,
    ]

    return "\n".join(lines)


def format_html(translations: list[dict], metadata: dict,
                qa_result: dict | None = None) -> str:
    """產生 HTML 格式譯文（帶基本樣式）"""
    order    = metadata.get("order_id", "")
    src_lang = LANG_LABELS.get(metadata.get("source_lang", ""), metadata.get("source_lang", ""))
    tgt_lang = LANG_LABELS.get(metadata.get("target_lang", ""), metadata.get("target_lang", ""))

    para_html = "\n".join(
        f'<p class="para">{html_text(trans["translated"])}</p>'
        for trans in sorted(translations, key=lambda x: x["index"])
    )

    extra_meta = ""
    if qa_result and qa_result.get("layer4_llm_judge"):
        score = qa_result["layer4_llm_judge"].get("score", "")
        if score:
            extra_meta = f'<span class="qa-score">QA 評分：{score}/100</span>'

    return render_doc(
        title=f"OTS 翻譯 — {order or 'Fast Track'}",
        body_html=f'<div class="para-flow">{para_html}</div>',
        eyebrow="OTS 翻譯服務 — Fast Track",
        source_lang=src_lang,
        target_lang=tgt_lang,
        page_subtitle=f"{src_lang} → {tgt_lang} 譯文",
        page_description=(
            f"<b>訂單編號：</b>{order or '—'}　·　"
            f"<b>語言方向：</b>{src_lang} → {tgt_lang}"
        ) if order else None,
        extra_meta=extra_meta,
        order_id=order,
    )


def format_bilingual_html(translations: list[dict], metadata: dict) -> str:
    """產生原文＋譯文對照 HTML（左右雙欄）"""
    order    = metadata.get("order_id", "")
    src_lang = LANG_LABELS.get(metadata.get("source_lang", ""), metadata.get("source_lang", ""))
    tgt_lang = LANG_LABELS.get(metadata.get("target_lang", ""), metadata.get("target_lang", ""))

    rows_html = []
    for trans in sorted(translations, key=lambda x: x["index"]):
        seg_num = f'<span class="seg-num">{trans["index"] + 1}</span>'
        rows_html.append(
            f"<tr>"
            f"<td class='src'>{seg_num}{html_text(trans.get('source', ''))}</td>"
            f"<td class='trans'>{html_text(trans.get('translated', ''))}</td>"
            f"</tr>"
        )

    body = (
        table_open(2, [f"原文（{src_lang}）", f"譯文（{tgt_lang}）"])
        + "\n".join(rows_html)
        + table_close()
    )

    return render_doc(
        title=f"OTS 翻譯對照 — {order or 'Fast Track'}",
        body_html=body,
        eyebrow="OTS 翻譯服務 — 原文／譯文對照",
        source_lang=src_lang,
        target_lang=tgt_lang,
        page_subtitle="原文 / 譯文 逐段對照",
        page_description=(
            f"<b>訂單編號：</b>{order or '—'}　·　"
            f"<b>語言方向：</b>{src_lang} → {tgt_lang}"
        ) if order else None,
        order_id=order,
    )


def write_corpus(translations: list[dict], metadata: dict, qa_result: dict | None):
    """若客戶同意，寫入 BigQuery 語料"""
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
        qa_score = None
        if qa_result and qa_result.get("layer4_llm_judge"):
            qa_score = qa_result["layer4_llm_judge"].get("score")

        rows = [
            {
                "order_id":        cfg.ORDER_ID,
                "source_lang":     metadata["source_lang"],
                "target_lang":     metadata["target_lang"],
                "source_text":     t["source"],
                "translated_text": t["translated"],
                "qa_score":        float(qa_score) if qa_score else None,
                "track_type":      metadata.get("track_type", "fast"),
                "consent_given":   True,
                "created_at":      now,
            }
            for t in translations
        ]

        errors = client.insert_rows_json(table, rows)
        if errors:
            logger.error(f"BigQuery insert errors: {errors}")
        else:
            logger.info(f"Corpus written: {len(rows)} rows to BigQuery")

            # 更新 corpus_log.bq_row_id
            with get_db() as db:
                from sqlalchemy import text as sqla_text
                db.execute(sqla_text("""
                    UPDATE corpus_log
                    SET bq_row_id = :bq_id
                    WHERE order_id = :order_id
                """), {"bq_id": f"bq-{cfg.ORDER_ID}", "order_id": cfg.ORDER_ID})

    except Exception as e:
        logger.warning(f"BigQuery write failed (non-critical): {e}")


def notify_delivery():
    """透過 Cloud Tasks 發送交付通知 email（非同步，不阻塞交付流程）"""
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
                "url": f"{cfg.API_BASE_URL}/internal/notify",
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
    logger.info(f"=== ft_deliver START — order: {cfg.ORDER_ID} ===")
    update_job_status("format_deliver", "running")
    update_order_field("status", "processing")

    try:
        translations = read_temp_json("translations.json")
        metadata     = read_temp_json("metadata.json")

        qa_result = None
        try:
            qa_result = read_temp_json("qa_result.json")
        except Exception:
            logger.warning("qa_result.json not found, proceeding without QA summary")

        order = get_order_info()
        logger.info(f"Formatting output: {len(translations)} segments")

        # ── 產生三種格式 ──────────────────────────────────────────────────
        txt_content  = format_txt(translations, metadata)
        html_content = format_html(translations, metadata, qa_result)
        bilingual_content = format_bilingual_html(translations, metadata)
        plain_content = format_plain_text(translations)

        # ── 上傳到 GCS outputs ────────────────────────────────────────────
        order_short  = cfg.ORDER_ID[:8]
        tgt_lang     = metadata.get("target_lang", "en")
        now_str      = datetime.now(timezone.utc).strftime("%Y%m%d")

        txt_path  = write_output(f"translation_{tgt_lang}_{now_str}.txt",  txt_content,  "text/plain")
        html_path = write_output(f"translation_{tgt_lang}_{now_str}.html", html_content, "text/html")
        bilingual_path = write_output(f"translation_{tgt_lang}_{now_str}_bilingual.html", bilingual_content, "text/html")
        plain_path = write_output(f"translation_{tgt_lang}_{now_str}_plain.txt", plain_content, "text/plain")

        # 主要交付使用 HTML 格式
        update_order_field("gcs_output_path", html_path)
        update_order_field("gcs_bilingual_output_path", bilingual_path)
        update_order_field("gcs_plain_text_output_path", plain_path)

        # ── 判斷 QA 分數是否達標 ──────────────────────────────────────────
        redeliver = os.environ.get("REDELIVER", "").lower() == "true"

        if redeliver:
            logger.info("Redeliver mode — setting status to delivered, skipping QA gate / corpus / notification")
            with get_db() as db:
                db.execute(sqla_text("""
                    UPDATE orders
                    SET status = 'delivered', delivered_at = NOW()
                    WHERE id = :order_id
                """), {"order_id": cfg.ORDER_ID})
        else:
            llm_score = None
            if qa_result and qa_result.get("layer4_llm_judge"):
                llm_score = qa_result["layer4_llm_judge"].get("score")

            must_fix_count = qa_result.get("must_fix_count", 0) if qa_result else 0
            qa_passed = (llm_score is None or llm_score >= cfg.LLM_JUDGE_MIN_SCORE) and must_fix_count == 0

            if qa_passed:
                final_status = "delivered"
            else:
                final_status = "qa_review"
                reason = f"score {llm_score:.1f} < {cfg.LLM_JUDGE_MIN_SCORE}" if (llm_score and llm_score < cfg.LLM_JUDGE_MIN_SCORE) else f"{must_fix_count} must_fix flags"
                logger.warning(
                    f"QA failed ({reason}) — setting order to qa_review instead of delivered"
                )

            logger.info(
                f"QA passed (score={llm_score}, must_fix={must_fix_count}) — delivering"
            )
            with get_db() as db:
                if qa_passed:
                    db.execute(sqla_text("""
                        UPDATE orders
                        SET status       = 'delivered',
                            delivered_at = NOW()
                        WHERE id = :order_id
                    """), {"order_id": cfg.ORDER_ID})
                else:
                    db.execute(sqla_text("""
                        UPDATE orders
                        SET status = 'qa_review'
                        WHERE id = :order_id
                    """), {"order_id": cfg.ORDER_ID})

            # ── 語料寫入 BigQuery ─────────────────────────────────────────────
            write_corpus(translations, metadata, qa_result)

            # ── 通知客戶（僅 QA 通過時）──────────────────────────────────────
            if qa_passed:
                notify_delivery()

        update_job_status("format_deliver", "success")
        logger.info(f"=== ft_deliver DONE — output: {html_path} ===")

    except Exception as e:
        logger.exception(f"ft_deliver FAILED: {e}")
        update_job_status("format_deliver", "failed", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
