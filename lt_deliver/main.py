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
from shared.db import update_job_status, get_order_info, update_order_field, get_lang_labels, get_db, get_sample_package
from shared.deliver_html import html_text, render_doc, table_open, table_close
from shared.storage import read_temp_json, write_output

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("lt_deliver")

LANG_LABELS = get_lang_labels("zh")

_LABEL_TRANSLATIONS: dict[str, dict[str, str]] = {
    "zh-tw": {
        "title":             "OTS 翻譯服務 — Literary Track（文學精譯）",
        "order":             "訂單編號",
        "lang_dir":          "語言方向",
        "delivery_date":     "交付日期",
        "book_fact_sheet":   "書目資料",
        "field":             "欄位",
        "original":          "原文",
        "translation":       "譯文",
        "word_count":        "字數",
        "synopsis":          "故事大綱",
        "translator_bio":    "譯者簡介",
        "market_analysis":   "市場分析",
        "translated_text":   "譯文",
        "footer":            "本譯文由 OTS 翻譯服務提供（AI 初稿 + 編輯審閱 + 校對審閱）。",
        "footer_contact":    "如有任何疑問，請聯繫 service@ots.tw",
        "title_label":       "書名",
        "author":            "作者",
        "publisher":         "出版社",
        "pub_date":          "出版日期",
        "category":          "類別",
        "sales":             "銷售資訊",
        "qa_score":          "QA 評分",
    },
    "en": {
        "title":             "OTS Translation — Literary Track (Premium)",
        "order":             "Order",
        "lang_dir":          "Language Direction",
        "delivery_date":     "Delivery Date",
        "book_fact_sheet":   "Book Fact Sheet",
        "field":             "Field",
        "original":          "Original",
        "translation":       "Translation",
        "word_count":        "Word Count",
        "synopsis":          "Synopsis",
        "translator_bio":    "Translator Bio",
        "market_analysis":   "Market Analysis",
        "translated_text":   "Translation",
        "footer":            "This translation is provided by OTS Translation Service (AI draft + editor review + proofreader review).",
        "footer_contact":    "For inquiries, contact service@ots.tw",
        "title_label":       "Title",
        "author":            "Author",
        "publisher":         "Publisher",
        "pub_date":          "Publication Date",
        "category":          "Category",
        "sales":             "Sales Info",
        "qa_score":          "QA Score",
    },
    "ja": {
        "title":             "OTS翻訳 — Literary Track（文学精訳）",
        "order":             "注文番号",
        "lang_dir":          "言語方向",
        "delivery_date":     "納品日",
        "book_fact_sheet":   "書籍情報",
        "field":             "項目",
        "original":          "原文",
        "translation":       "訳文",
        "word_count":        "文字数",
        "synopsis":          "あらすじ",
        "translator_bio":    "翻訳者紹介",
        "market_analysis":   "市場分析",
        "translated_text":   "翻訳文",
        "footer":            "本翻訳はOTS翻訳サービスによって提供されています（AI初稿＋編集者レビュー＋校正者レビュー）。",
        "footer_contact":    "お問い合わせ：service@ots.tw",
        "title_label":       "タイトル",
        "author":            "著者",
        "publisher":         "出版社",
        "pub_date":          "出版日",
        "category":          "カテゴリ",
        "sales":             "販売情報",
        "qa_score":          "QAスコア",
    },
    "ko": {
        "title":             "OTS 번역 — Literary Track (문학 정역)",
        "order":             "주문 번호",
        "lang_dir":          "언어 방향",
        "delivery_date":     "납품일",
        "book_fact_sheet":   "도서 정보",
        "field":             "항목",
        "original":          "원문",
        "translation":       "번역문",
        "word_count":        "단어 수",
        "synopsis":          "시놉시스",
        "translator_bio":    "번역가 소개",
        "market_analysis":   "시장 분석",
        "translated_text":   "번역문",
        "footer":            "본 번역은 OTS 번역 서비스가 제공합니다 (AI 초안 + 편집자 검토 + 교정자 검토).",
        "footer_contact":    "문의: service@ots.tw",
        "title_label":       "제목",
        "author":            "저자",
        "publisher":         "출판사",
        "pub_date":          "출판일",
        "category":          "카테고리",
        "sales":             "판매 정보",
        "qa_score":          "QA 점수",
    },
}

_FIELD_LABELS: list[tuple[str, str, str]] = [
    ("title_original", "title_target", "title_label"),
    ("author_original", "author_target", "author"),
    ("publisher_original", "publisher_target", "publisher"),
    ("pub_date_original", "pub_date_target", "pub_date"),
    ("category_original", "category_target", "category"),
    ("sales_original", "sales_target", "sales"),
]


def _l10n(tgt_lang: str, key: str) -> str:
    """Get localized label for target language, fallback to zh-tw."""
    lang_code = tgt_lang if tgt_lang in _LABEL_TRANSLATIONS else "zh-tw"
    return _LABEL_TRANSLATIONS[lang_code].get(key, key)


def _render_lit_package(sample_pkg: dict | None, tgt_code: str) -> str:
    """Render the literary package (book fact sheet, synopsis, bio, market) as
    a styled ``.lit-package`` card. Returns empty string if ``sample_pkg`` is
    empty or only has empty fields.
    """
    if not sample_pkg:
        return ""

    parts: list[str] = []

    # ── Book fact sheet ────────────────────────────────────────────────
    bfs = sample_pkg.get("book_fact_sheet") or {}
    if isinstance(bfs, str):
        try:
            bfs = json.loads(bfs)
        except Exception:
            bfs = {}

    field_rows: list[str] = []
    for orig_k, tgt_k, label_key in _FIELD_LABELS:
        orig_v = bfs.get(orig_k, "")
        tgt_v  = bfs.get(tgt_k, "")
        if orig_v or tgt_v:
            field_rows.append(
                f'<div class="field-row">'
                f'<span class="field">{_l10n(tgt_code, label_key)}</span>'
                f'<strong>{_escape(orig_v)}</strong>　·　'
                f'{_escape(tgt_v)}'
                f'</div>'
            )
    wc = bfs.get("word_count", "")
    if wc:
        field_rows.append(
            f'<div class="field-row">'
            f'<span class="field">{_l10n(tgt_code, "word_count")}</span>'
            f'{_escape(wc)}'
            f'</div>'
        )

    if field_rows:
        parts.append(
            f'<h2>{_l10n(tgt_code, "book_fact_sheet")}</h2>'
            + "".join(field_rows)
        )

    # ── Synopsis ───────────────────────────────────────────────────────
    synopsis = sample_pkg.get("synopsis", "")
    if synopsis:
        parts.append(
            f'<h2>{_l10n(tgt_code, "synopsis")}</h2>'
            f'<div class="synopsis">{_md_to_html(synopsis)}</div>'
        )

    # ── Translator bio ─────────────────────────────────────────────────
    bio = sample_pkg.get("translator_bio", "")
    if bio:
        parts.append(
            f'<h2>{_l10n(tgt_code, "translator_bio")}</h2>'
            f'<div class="bio">{_md_to_html(bio)}</div>'
        )

    # ── Market analysis ────────────────────────────────────────────────
    market = sample_pkg.get("market_analysis", "")
    if market:
        parts.append(
            f'<h2>{_l10n(tgt_code, "market_analysis")}</h2>'
            f'<div class="market">{_md_to_html(market)}</div>'
        )

    if not parts:
        return ""

    return f'<section class="lit-package">{"".join(parts)}</section>'


def _escape(text: str) -> str:
    """Lightweight HTML escape for trusted, server-generated strings."""
    if text is None:
        return ""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )


def _md_to_html(text: str) -> str:
    """Convert basic markdown to HTML for delivery output."""
    import re
    text = re.sub(r'^### (.+)$', r'<h3>\1</h3>', text, flags=re.MULTILINE)
    text = re.sub(r'^## (.+)$', r'<h2>\1</h2>', text, flags=re.MULTILINE)
    text = re.sub(r'^# (.+)$', r'<h1>\1</h1>', text, flags=re.MULTILINE)
    text = re.sub(r'\*\*(.+?)\*\*', r'<strong>\1</strong>', text)
    text = re.sub(r'\*(.+?)\*', r'<em>\1</em>', text)
    text = re.sub(r'`(.+?)`', r'<code>\1</code>', text)
    paragraphs = []
    for block in text.split('\n\n'):
        block = block.strip()
        if not block:
            continue
        if block.startswith('<h'):
            paragraphs.append(block)
        elif block.startswith('- ') or block.startswith('* '):
            items = []
            for line in block.split('\n'):
                line = line.strip()
                if line.startswith('- ') or line.startswith('* '):
                    items.append(f'<li>{line[2:]}</li>')
            if items:
                paragraphs.append(f'<ul>{"".join(items)}</ul>')
        else:
            paragraphs.append(f'<p>{block}</p>')
    return '\n'.join(paragraphs)


def format_plain_text(translations: list[dict]) -> str:
    """產生純文字譯文（無 header/footer/metadata，僅譯文段落）。"""
    paras = [trans["translated"] for trans in sorted(translations, key=lambda x: x["index"])]
    return "\n\n".join(paras)


def format_txt(translations: list[dict], metadata: dict,
               sample_pkg: dict | None = None) -> str:
    order    = metadata.get("order_id", "")
    src_lang = LANG_LABELS.get(metadata.get("source_lang", ""), metadata.get("source_lang", ""))
    tgt_lang = LANG_LABELS.get(metadata.get("target_lang", ""), metadata.get("target_lang", ""))
    tgt_code = metadata.get("target_lang", "zh-tw")
    now      = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    lines = [
        "=" * 60,
        _l10n(tgt_code, "title"),
        f"{_l10n(tgt_code, 'order')}：{order}",
        f"{_l10n(tgt_code, 'lang_dir')}：{src_lang} → {tgt_lang}",
        f"{_l10n(tgt_code, 'delivery_date')}：{now}",
        "=" * 60,
    ]

    if sample_pkg:
        bfs = sample_pkg.get("book_fact_sheet") or {}
        if isinstance(bfs, str):
            bfs = __import__("json").loads(bfs)
        for orig_k, tgt_k, label_key in _FIELD_LABELS:
            orig_v = bfs.get(orig_k, "")
            tgt_v  = bfs.get(tgt_k, "")
            if orig_v or tgt_v:
                label = _l10n(tgt_code, label_key)
                lines.append(f"  {label}  |  {orig_v}  |  {tgt_v}")
        wc = bfs.get("word_count", "")
        if wc:
            lines.append(f"  {_l10n(tgt_code, 'word_count')}  |  {wc}")
        lines.append("")
        synopsis = sample_pkg.get("synopsis", "")
        if synopsis:
            lines.append(f"【{_l10n(tgt_code, 'synopsis')}】")
            lines.append(synopsis)
            lines.append("")
        bio = sample_pkg.get("translator_bio", "")
        if bio:
            lines.append(f"【{_l10n(tgt_code, 'translator_bio')}】")
            lines.append(bio)
            lines.append("")
        market = sample_pkg.get("market_analysis", "")
        if market:
            lines.append(f"【{_l10n(tgt_code, 'market_analysis')}】")
            lines.append(market)
            lines.append("")
        lines.append("=" * 60)

    lines.append("")
    lines.append(f"【{_l10n(tgt_code, 'translated_text')}】")
    lines.append("")

    for trans in sorted(translations, key=lambda x: x["index"]):
        lines.append(trans["translated"])
        lines.append("")

    lines += [
        "=" * 60,
        _l10n(tgt_code, "footer"),
        _l10n(tgt_code, "footer_contact"),
        "=" * 60,
    ]

    return "\n".join(lines)


def format_html(translations: list[dict], metadata: dict,
                qa_result: dict | None = None,
                sample_pkg: dict | None = None) -> str:
    order    = metadata.get("order_id", "")
    src_lang = LANG_LABELS.get(metadata.get("source_lang", ""), "")
    tgt_lang = LANG_LABELS.get(metadata.get("target_lang", ""), "")
    tgt_code = metadata.get("target_lang", "zh-tw")

    pkg_html = _render_lit_package(sample_pkg, tgt_code)

    para_html = "\n".join(
        f'<p class="para">{html_text(trans["translated"])}</p>'
        for trans in sorted(translations, key=lambda x: x["index"])
    )

    extra_meta = ""
    if qa_result and qa_result.get("layer4_llm_judge"):
        score = qa_result["layer4_llm_judge"].get("score", "")
        if score:
            extra_meta = (
                f'<span class="qa-score">'
                f'{_l10n(tgt_code, "qa_score")}：{score}/100</span>'
            )

    body = pkg_html + (f'<div class="para-flow">{para_html}</div>' if para_html else "")

    return render_doc(
        title=f"{_l10n(tgt_code, 'title')} — {order or 'Literary Track'}",
        body_html=body,
        eyebrow=_l10n(tgt_code, "title"),
        source_lang=src_lang,
        target_lang=tgt_lang,
        page_subtitle=f"{src_lang} → {tgt_lang} 譯文",
        page_description=(
            f"<b>{_l10n(tgt_code, 'order')}：</b>{order or '—'}　·　"
            f"<b>{_l10n(tgt_code, 'lang_dir')}：</b>{src_lang} → {tgt_lang}"
        ) if order else None,
        extra_meta=extra_meta,
        order_id=order,
        html_lang=tgt_code or "zh-Hant",
    )


def format_bilingual_html(translations: list[dict], metadata: dict,
                          sample_pkg: dict | None = None) -> str:
    """產生原文＋譯文對照 HTML（左右雙欄）"""
    order    = metadata.get("order_id", "")
    src_lang = LANG_LABELS.get(metadata.get("source_lang", ""), metadata.get("source_lang", ""))
    tgt_lang = LANG_LABELS.get(metadata.get("target_lang", ""), metadata.get("target_lang", ""))
    tgt_code = metadata.get("target_lang", "zh-tw")

    pkg_html = _render_lit_package(sample_pkg, tgt_code)

    rows_html: list[str] = []
    for trans in sorted(translations, key=lambda x: x["index"]):
        seg_num = f'<span class="seg-num">{trans["index"] + 1}</span>'
        rows_html.append(
            f"<tr>"
            f"<td class='src'>{seg_num}{html_text(trans.get('source', ''))}</td>"
            f"<td class='trans'>{html_text(trans.get('translated', ''))}</td>"
            f"</tr>"
        )

    body = (
        pkg_html
        + table_open(2, [
            f"{_l10n(tgt_code, 'original')}（{src_lang}）",
            f"{_l10n(tgt_code, 'translation')}（{tgt_lang}）",
        ])
        + "\n".join(rows_html)
        + table_close()
    )

    return render_doc(
        title=f"{_l10n(tgt_code, 'title')}（對照版） — {order or 'Literary Track'}",
        body_html=body,
        eyebrow=f"{_l10n(tgt_code, 'title')}（原文／譯文對照）",
        source_lang=src_lang,
        target_lang=tgt_lang,
        page_subtitle="原文 / 譯文 逐段對照",
        page_description=(
            f"<b>{_l10n(tgt_code, 'order')}：</b>{order or '—'}　·　"
            f"<b>{_l10n(tgt_code, 'lang_dir')}：</b>{src_lang} → {tgt_lang}"
        ) if order else None,
        order_id=order,
        html_lang=tgt_code or "zh-Hant",
    )


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

        sample_pkg = get_sample_package()

        txt_content  = format_txt(translations, metadata, sample_pkg)
        html_content = format_html(translations, metadata, qa_result, sample_pkg)
        bilingual_content = format_bilingual_html(translations, metadata, sample_pkg)
        plain_content = format_plain_text(translations)

        order_short  = cfg.ORDER_ID[:8]
        tgt_lang     = metadata.get("target_lang", "en")
        now_str      = datetime.now(timezone.utc).strftime("%Y%m%d")

        txt_path  = write_output(f"translation_{tgt_lang}_{now_str}.txt",  txt_content,  "text/plain")
        html_path = write_output(f"translation_{tgt_lang}_{now_str}.html", html_content, "text/html")
        bilingual_path = write_output(f"translation_{tgt_lang}_{now_str}_bilingual.html", bilingual_content, "text/html")
        plain_path = write_output(f"translation_{tgt_lang}_{now_str}_plain.txt", plain_content, "text/plain")

        update_order_field("gcs_output_path", html_path)
        update_order_field("gcs_bilingual_output_path", bilingual_path)
        update_order_field("gcs_plain_text_output_path", plain_path)

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
