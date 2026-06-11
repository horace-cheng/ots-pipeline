"""
gt_deliver/main.py — Cloud Run Job (Gutenberg Track) v2

Reads the consolidated per-segment translation files written by
``gt_chapter_splitter`` + ``gt_translate`` + ``gt_simplify`` + ``gt_tailo``
and produces the final delivery artifacts:

  - full_translation.txt       concatenates the standard Traditional Chinese
  - full_simplified.txt        concatenates the youth-friendly version
  - full_tailo.txt             concatenates the Hanzi + Tai-lo version
  - source_vs_chinese.html     2-column side-by-side: 原文 | 標準翻譯
  - youth_vs_tailo.html        2-column side-by-side: 青少年版 | 台羅版
  - book_comparison.html       4-column overview: 原文 | 標準 | 青少年 | 台羅

The HTML uses the shared ``shared.deliver_html`` template (book-cover
header, table of contents, anchored chapter headings, alternating-row
tables, print-friendly stylesheet).
"""
import json
import logging
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config import cfg
from shared.db import update_job_status, update_order_field
from shared.deliver_html import (
    BASE_CSS, FONT_IMPORT, chapter_heading, chapter_text_block,
    comparison_chapter_block, footer, html_text,
    render_doc, table_close, table_open,
)
from shared.notify import notify_stage
from shared.storage import (
    read_temp_text, read_temp_json, write_output, get_client as get_gcs_client,
)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gt_deliver")


# ── Source loading ───────────────────────────────────────────────────────

def _load_json_optional(name: str) -> Optional[list]:
    """Read a per-segment JSON file; return None if missing or malformed."""
    try:
        data = read_temp_json(name)
        if isinstance(data, list):
            return data
    except Exception as e:
        logger.warning(f"{name} not found or unreadable: {e}")
    return None


def read_metadata() -> Dict:
    try:
        raw = read_temp_text("metadata.json")
        if not raw:
            return {}
        if isinstance(raw, dict):
            return raw
        return json.loads(raw)
    except Exception as e:
        logger.warning(f"Could not read metadata.json: {e}")
    return {}


def read_chapters() -> List[dict]:
    """Read source/chapters.json — the chapter index from gt_chapter_splitter."""
    try:
        data = read_temp_json("source/chapters.json")
        if isinstance(data, list):
            return data
    except Exception as e:
        logger.warning(f"source/chapters.json not found: {e}")
    return []


# ── Concatenated text outputs ─────────────────────────────────────────────

def _concat_text(entries: List[dict]) -> str:
    return "\n\n".join(e.get("translated", "") for e in entries if e.get("translated"))


# ── HTML rendering ────────────────────────────────────────────────────────

# Re-export FONT_IMPORT / BASE_CSS so callers that import them still work.
FONT_IMPORT = FONT_IMPORT
BASE_CSS = BASE_CSS

# Legacy alias kept for backward-compat with tests that mock ``_html``.
_html = html_text


def _rows_two_col(entries: List[dict], key_a: str, key_b: str, label_a: str, label_b: str) -> str:
    """Render a flat 2-col table (used by callers that bypass the chapter logic)."""
    rows = []
    for e in entries:
        a = html_text(e.get(key_a, ""))
        b = html_text(e.get(key_b, ""))
        rows.append(f"<tr><td class='src'>{a}</td><td class='trans'>{b}</td></tr>")
    return (
        f"<table class='bilingual-table cols-2'><thead><tr><th>{label_a}</th><th>{label_b}</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def _rows_four_col(entries: List[dict]) -> str:
    """Render a flat 4-col table (used by callers that bypass the chapter logic)."""
    rows = []
    for e in entries:
        src    = html_text(e.get("source", ""))
        trans  = html_text(e.get("translated", ""))
        simp   = html_text(e.get("simplified", ""))
        tailo  = html_text(e.get("tailo", ""))
        rows.append(
            f"<tr><td class='src'>{src}</td><td class='trans'>{trans}</td>"
            f"<td class='simp'>{simp}</td><td class='tailo'>{tailo}</td></tr>"
        )
    return (
        "<table class='bilingual-table cols-4'><thead><tr>"
        "<th>原文</th><th>標準翻譯</th><th>青少年版</th><th>台羅版</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def _render_chapter_blocks(render_rows) -> str:
    """Compatibility shim — older callers passed a ``render_rows`` thunk."""
    return render_rows()


def format_source_vs_chinese(
    source_segments: List[dict],
    translated_entries: List[dict],
    metadata: dict,
) -> str:
    """Side-by-side English source vs Chinese translation, per chapter.

    Source and translated segments are concatenated per chapter and rendered
    as two-column comparison blocks, matching the full_vs_simplified layout.
    """
    raw_title  = metadata.get("title", "Gutenberg Book Translation")
    authors    = metadata.get("authors", [])
    page_title = f"{raw_title} — 原文 ↔ 標準翻譯 對照"

    chapters = read_chapters()
    trans_by_index = {e["index"]: e for e in translated_entries}

    body_parts: List[str] = []
    for ch in (chapters or []):
        ci = ch.get("index", 0)
        title = ch.get("title", f"Chapter {ci + 1}")
        seg_start = ch.get("segment_start", 0)
        seg_end = ch.get("segment_end", seg_start)

        src_paras: List[str] = []
        tgt_paras: List[str] = []
        for j in range(seg_start, seg_end):
            if j < len(source_segments):
                s = source_segments[j].get("text", "")
                if s.strip():
                    src_paras.append(s.strip())
            entry = trans_by_index.get(j, {})
            t = entry.get("translated", "")
            if t.strip():
                tgt_paras.append(t.strip())

        if not src_paras and not tgt_paras:
            continue

        src_text = "\n\n".join(src_paras)
        tgt_text = "\n\n".join(tgt_paras)

        body_parts.append(comparison_chapter_block(
            src_text, tgt_text, title, ci,
            label_full="原文（English）",
            label_simplified="標準翻譯（繁體中文）",
        ))

    if not chapters:
        for i, src_seg in enumerate(source_segments):
            src_text = src_seg.get("text", "")
            tgt_text = translated_entries[i].get("translated", "") if i < len(translated_entries) else ""
            if not src_text.strip() and not tgt_text.strip():
                continue
            body_parts.append(comparison_chapter_block(
                src_text, tgt_text, f"Segment {i + 1}", i,
                label_full="原文（English）",
                label_simplified="標準翻譯（繁體中文）",
            ))

    return render_doc(
        title=page_title,
        body_html="\n".join(body_parts),
        authors=authors,
        eyebrow="OTS 翻譯服務 — Gutenberg Track",
        page_subtitle="原文 ↔ 標準翻譯 對照",
        page_description=(
            "<b>原文</b>（English）與 <b>標準翻譯</b>（繁體中文）逐章對照。"
            "左欄為原文，右欄為標準中文翻譯。"
        ),
        chapters=chapters or None,
    )


def format_simplified_reader(
    simplified_chapters: List[dict],
    metadata: dict,
) -> str:
    """Single-column reader view: each chapter's simplified text as a narrative."""
    raw_title  = metadata.get("title", "Gutenberg Book Translation")
    authors    = metadata.get("authors", [])
    page_title = f"{raw_title} — 青少年讀本"

    body_parts: List[str] = []
    for i, ch in enumerate(simplified_chapters):
        body_parts.append(chapter_text_block(ch, i))

    return render_doc(
        title=page_title,
        body_html="\n".join(body_parts),
        authors=authors,
        eyebrow="OTS 翻譯服務 — Gutenberg Track",
        page_subtitle="青少年讀本（簡化版）",
        page_description=(
            "<b>青少年版</b>以簡化用詞和短句改寫，適合 8-12 歲讀者。"
            "每章完整呈現，保持故事流暢性。"
        ),
        chapters=simplified_chapters,
    )


def format_full_vs_simplified(
    translated_entries: List[dict],
    simplified_chapters: List[dict],
    metadata: dict,
) -> str:
    """Side-by-side comparison: full translation vs simplified, per chapter.

    Each chapter renders the full translation (all segments concatenated) in
    the left column and the simplified version in the right column.
    Chapter boundaries come from source/chapters.json (read_chapters()),
    matched by ``chapter_index``.
    """
    raw_title  = metadata.get("title", "Gutenberg Book Translation")
    authors    = metadata.get("authors", [])
    page_title = f"{raw_title} — 標準翻譯 vs 青少年版"

    chapters = read_chapters()
    simp_by_idx = {ch.get("chapter_index", i): ch for i, ch in enumerate(simplified_chapters)}

    body_parts: List[str] = []
    for ch in (chapters or []):
        ci = ch.get("index", 0)
        title = ch.get("title", f"Chapter {ci + 1}")
        seg_start = ch.get("segment_start", 0)
        seg_end = ch.get("segment_end", seg_start)

        full_paras: List[str] = []
        for j in range(seg_start, seg_end):
            if j < len(translated_entries):
                t = translated_entries[j].get("translated", "")
                if t.strip():
                    full_paras.append(t.strip())
        full_text = "\n\n".join(full_paras)

        simp_ch = simp_by_idx.get(ci, {})
        simplified_text = simp_ch.get("text", "")

        if not full_text.strip() and not simplified_text.strip():
            continue

        body_parts.append(comparison_chapter_block(
            full_text, simplified_text, title, ci,
        ))

    if not chapters:
        # Fallback: entire-translation vs entire-simplified if no chapters
        ch_entries = simplified_chapters
        for i, ch in enumerate(ch_entries):
            ci = ch.get("chapter_index", i)
            title = ch.get("title", f"Chapter {ci + 1}")
            simplified_text = ch.get("text", "")
            body_parts.append(comparison_chapter_block(
                "", simplified_text, title, ci,
            ))

    return render_doc(
        title=page_title,
        body_html="\n".join(body_parts),
        authors=authors,
        eyebrow="OTS 翻譯服務 — Gutenberg Track",
        page_subtitle="標準翻譯 ↔ 青少年版 對照",
        page_description=(
            "左欄為<b>標準翻譯</b>（忠實呈現原文），"
            "右欄為<b>青少年版</b>（簡化用詞，適合 8-12 歲）。"
        ),
        chapters=chapters,
    )


def format_simplified_vs_tailo(
    simplified_entries: List[dict],
    tailo_entries: List[dict],
    metadata: dict,
) -> str:
    """Side-by-side simplified Chinese vs tailo, per chapter.

    Both inputs are per-segment lists with 'translated' key.
    Segments are concatenated per chapter and rendered as two-column
    comparison blocks, matching the full_vs_simplified layout.
    """
    raw_title  = metadata.get("title", "Gutenberg Book Translation")
    authors    = metadata.get("authors", [])
    page_title = f"{raw_title} — 青少年版 ↔ 台羅版"

    chapters = read_chapters()
    simp_by_index = {e["index"]: e for e in simplified_entries}
    tailo_by_index = {e["index"]: e for e in tailo_entries}

    body_parts: List[str] = []
    for ch in (chapters or []):
        ci = ch.get("index", 0)
        title = ch.get("title", f"Chapter {ci + 1}")
        seg_start = ch.get("segment_start", 0)
        seg_end = ch.get("segment_end", seg_start)

        simp_paras: List[str] = []
        tailo_paras: List[str] = []
        for j in range(seg_start, seg_end):
            s = simp_by_index.get(j, {}).get("translated", "")
            if s.strip():
                simp_paras.append(s.strip())
            t = tailo_by_index.get(j, {}).get("translated", "")
            if t.strip():
                tailo_paras.append(t.strip())

        if not simp_paras and not tailo_paras:
            continue

        simp_text = "\n\n".join(simp_paras)
        tailo_text = "\n\n".join(tailo_paras)

        body_parts.append(comparison_chapter_block(
            simp_text, tailo_text, title, ci,
            label_full="青少年版",
            label_simplified="台羅版",
        ))

    if not chapters:
        all_indices = sorted(set(simp_by_index) | set(tailo_by_index))
        for i in all_indices:
            simp_text = simp_by_index.get(i, {}).get("translated", "")
            tailo_text = tailo_by_index.get(i, {}).get("translated", "")
            if not simp_text.strip() and not tailo_text.strip():
                continue
            body_parts.append(comparison_chapter_block(
                simp_text, tailo_text, f"Segment {i + 1}", i,
                label_full="青少年版",
                label_simplified="台羅版",
            ))

    return render_doc(
        title=page_title,
        body_html="\n".join(body_parts),
        authors=authors,
        eyebrow="OTS 翻譯服務 — Gutenberg Track",
        page_subtitle="簡化版 ↔ 台羅版 對照",
        page_description=(
            "<b>青少年版</b>（簡化用詞，適合 8-12 歲）與"
            "<b>台羅版</b>（漢字 + 台羅拼音）逐章對照。"
        ),
        chapters=chapters or None,
    )


# ── Main entry point ──────────────────────────────────────────────────────

def run():
    logger.info(f"=== gt_deliver START — order: {cfg.ORDER_ID} ===")
    update_job_status("gt_deliver", "running")

    try:
        metadata = read_metadata()
        if metadata:
            logger.info(f"Book: {metadata.get('title', '?')}")

        source_segments      = _load_json_optional("segments.json") or []
        translated_entries   = _load_json_optional("translated.json") or []
        simplified_entries   = _load_json_optional("simplified.json") or []
        simplified_chapters  = _load_json_optional("simplified_chapters.json") or []
        tailo_entries        = _load_json_optional("tailo.json") or []

        if not translated_entries:
            raise ValueError("translated.json is empty — gt_translate must run first")
        logger.info(
            f"Loaded {len(source_segments)} source, "
            f"{len(translated_entries)} translated, "
            f"{len(simplified_entries)} simplified, "
            f"{len(simplified_chapters)} simplified_chapters, "
            f"{len(tailo_entries)} tailo"
        )

        # ── Concatenated text outputs ───────────────────────────────────
        gcs_translation = write_output(
            "full_translation.txt",
            _concat_text(translated_entries),
            "text/plain; charset=utf-8",
        )
        if simplified_chapters:
            # full_simplified.txt from whole-chapter simplified output
            chapter_text = "\n\n".join(
                ch.get("text", "") for ch in simplified_chapters
                if ch and ch.get("text", "").strip()
            )
            write_output("full_simplified.txt", chapter_text,
                         "text/plain; charset=utf-8")
        if tailo_entries:
            write_output("full_tailo.txt", _concat_text(tailo_entries),
                         "text/plain; charset=utf-8")

        # ── Side-by-side HTMLs ──────────────────────────────────────────
        sxc_html = format_source_vs_chinese(
            source_segments, translated_entries, metadata,
        )
        gcs_sxc  = write_output("source_vs_chinese.html", sxc_html,
                                "text/html; charset=utf-8")

        gcs_svt  = None
        if simplified_entries and tailo_entries:
            svt_html = format_simplified_vs_tailo(
                simplified_entries, tailo_entries, metadata,
            )
            gcs_svt  = write_output("simplified_tailo.html", svt_html,
                                    "text/html; charset=utf-8")

        gcs_sr   = None
        if simplified_chapters:
            sr_html  = format_simplified_reader(simplified_chapters, metadata)
            gcs_sr   = write_output("simplified_reader.html", sr_html,
                                    "text/html; charset=utf-8")

        gcs_fvs  = None
        if translated_entries and simplified_chapters:
            fvs_html = format_full_vs_simplified(
                translated_entries, simplified_chapters, metadata,
            )
            gcs_fvs  = write_output("full_vs_simplified.html", fvs_html,
                                    "text/html; charset=utf-8")

        update_order_field("gcs_output_path", gcs_sxc)

        update_order_field("status", "delivered")
        update_job_status("gt_deliver", "success", qa_result={
            "gcs_translation":             gcs_translation,
            "gcs_source_vs_chinese":       gcs_sxc,
            "gcs_simplified_vs_tailo":     gcs_svt,
            "gcs_simplified_reader":       gcs_sr,
            "gcs_full_vs_simplified":      gcs_fvs,
            "translation_chars": sum(
                len(e.get("translated", "")) for e in translated_entries
            ),
        })
        notify_stage("gt_deliver")
        logger.info(
            f"=== gt_deliver DONE — outputs in "
            f"gs://{cfg.BUCKET_OUTPUTS}/orders/{cfg.ORDER_ID}/ ==="
        )

    except Exception as e:
        logger.error(f"gt_deliver failed: {e}", exc_info=True)
        update_job_status("gt_deliver", "failed", error_message=str(e)[:500])
        raise


if __name__ == "__main__":
    run()
