"""
shared/deliver_html.py — Shared HTML rendering helpers for delivery jobs.

Provides a polished, book-style HTML template used by:
  - gt_deliver  (3 side-by-side HTMLs)
  - ft_deliver  (single-column + bilingual)
  - lt_deliver  (single-column + bilingual + literary package)

Design goals
------------
1. **Readable** — proper font pairing (serif for English source, sans for
   Chinese translation), generous line-height, balanced columns.
2. **Scannable** — alternating row backgrounds, chapter anchors, optional
   table-of-contents sidebar.
3. **Printable** — `@media print` rules for clean A4 PDF export.
4. **Responsive** — stacks gracefully on phones and tablets.
5. **Accessible** — semantic HTML, `lang` attributes, sufficient contrast.
"""
from __future__ import annotations

import re
from html import escape
from typing import Dict, List, Optional


# ── Public constants ─────────────────────────────────────────────────────

# Web fonts (Google Fonts) — Iansui is the project's house CJK face.
FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2?"
    "family=Noto+Sans+TC:wght@400;500;700&"
    "family=Noto+Serif+TC:wght@400;500;700&"
    "family=Noto+Sans:wght@400;500;700&"
    "family=Noto+Serif:ital,wght@0,400;0,700;1,400&"
    "family=Iansui:wght@400;700&display=swap');"
)

# Master stylesheet — used by all 3 deliver modules.
# Color palette is intentionally muted and book-like (slate / indigo accent).
BASE_CSS = """
:root {
  --bg:           #f7f5f1;
  --surface:      #ffffff;
  --surface-alt:  #fafaf7;
  --ink:          #1f1d1a;
  --ink-muted:    #5b574d;
  --ink-soft:     #8a8478;
  --accent:       #2d3a5f;
  --accent-soft:  #e8ecf3;
  --accent-line:  #c7d0e0;
  --warn:         #9a4a2e;
  --border:       #e6e1d6;
  --shadow:       0 1px 2px rgba(31, 29, 26, 0.04),
                  0 4px 12px rgba(31, 29, 26, 0.06);
  --serif-en:     'Noto Serif', 'Iowan Old Style', Georgia, 'Times New Roman', serif;
  --serif-tc:     'Noto Serif TC', 'Iansui', Georgia, serif;
  --sans-tc:      'Iansui', 'Noto Sans TC', 'Noto Sans', system-ui, -apple-system, sans-serif;
  --sans-en:      'Noto Sans', system-ui, -apple-system, sans-serif;
}

* { box-sizing: border-box; }

html { scroll-behavior: smooth; }

body {
  margin: 0;
  font-family: var(--sans-tc);
  background: var(--bg);
  color: var(--ink);
  line-height: 1.7;
  -webkit-font-smoothing: antialiased;
  -moz-osx-font-smoothing: grayscale;
}

.page {
  max-width: 1200px;
  margin: 0 auto;
  padding: 0 1.5rem 4rem;
}

/* ── Book header ─────────────────────────────────────────────────────── */
.book-header {
  background: linear-gradient(135deg, #1f2742 0%, #2d3a5f 50%, #3d4d7a 100%);
  color: #f5f1e8;
  padding: 2.75rem 2.25rem 2.5rem;
  border-radius: 0 0 16px 16px;
  margin: 0 -1.5rem 2rem;
  box-shadow: var(--shadow);
  position: relative;
  overflow: hidden;
}
.book-header::before {
  content: '';
  position: absolute;
  inset: 0;
  background:
    radial-gradient(ellipse at top right, rgba(255,255,255,0.08), transparent 60%),
    radial-gradient(ellipse at bottom left, rgba(255,255,255,0.05), transparent 50%);
  pointer-events: none;
}
.book-header > * { position: relative; }
.book-eyebrow {
  display: inline-block;
  font-family: var(--sans-en);
  font-size: 0.7rem;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: rgba(245, 241, 232, 0.7);
  padding-bottom: 0.5rem;
  margin-bottom: 0.75rem;
  border-bottom: 1px solid rgba(245, 241, 232, 0.2);
}
.book-title {
  font-family: var(--serif-tc);
  font-size: clamp(1.75rem, 4vw, 2.5rem);
  font-weight: 700;
  letter-spacing: -0.01em;
  line-height: 1.2;
  margin: 0 0 0.5rem;
  color: #ffffff;
}
.book-author {
  font-family: var(--serif-en);
  font-size: 1.05rem;
  font-style: italic;
  color: rgba(245, 241, 232, 0.85);
  margin: 0 0 1.25rem;
}
.book-author:empty { display: none; }
.book-meta {
  display: flex;
  flex-wrap: wrap;
  gap: 0.5rem;
  font-size: 0.8rem;
  font-family: var(--sans-en);
}
.lang-badge {
  display: inline-flex;
  align-items: center;
  gap: 0.4rem;
  padding: 0.3rem 0.75rem;
  background: rgba(255, 255, 255, 0.1);
  border: 1px solid rgba(255, 255, 255, 0.15);
  border-radius: 999px;
  color: #f5f1e8;
}
.lang-badge .arrow { color: rgba(245, 241, 232, 0.6); }

/* ── Page subtitle (e.g. "原文 ↔ 標準翻譯 對照") ─────────────────────── */
.page-title {
  font-family: var(--serif-tc);
  font-size: 1.35rem;
  font-weight: 500;
  color: var(--accent);
  margin: 1.5rem 0 1.25rem;
  padding: 0.75rem 0 0.75rem 1rem;
  border-left: 4px solid var(--accent);
  line-height: 1.4;
}
.page-intro {
  background: var(--surface);
  border: 1px solid var(--border);
  border-left: 3px solid var(--accent);
  border-radius: 0 6px 6px 0;
  padding: 0.9rem 1.1rem;
  color: var(--ink-muted);
  font-size: 0.95rem;
  line-height: 1.7;
  margin: 0 0 2rem;
}
.page-intro b { color: var(--ink); font-weight: 600; }

/* ── Table of contents (auto-generated from chapters) ─────────────── */
.toc {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1.1rem 1.4rem 1.25rem;
  margin: 0 0 2rem;
  box-shadow: var(--shadow);
  position: sticky;
  top: 0;
  z-index: 10;
}
.toc h3 {
  margin: 0 0 0.65rem;
  font-family: var(--sans-en);
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink-soft);
  font-weight: 600;
}
.toc ol {
  margin: 0;
  padding: 0;
  list-style: none;
  counter-reset: toc;
  columns: 3;
  column-gap: 1.5rem;
}
.toc li {
  counter-increment: toc;
  padding: 0.18rem 0;
  break-inside: avoid;
}
.toc a {
  display: flex;
  align-items: baseline;
  gap: 0.5rem;
  color: var(--accent);
  text-decoration: none;
  font-size: 0.9rem;
  border-bottom: 1px dotted transparent;
  padding: 0.1rem 0;
  transition: border-color 0.15s;
}
.toc a:hover {
  border-bottom-color: var(--accent);
}
.toc a::before {
  content: counter(toc, decimal-leading-zero);
  font-family: var(--sans-en);
  font-size: 0.7rem;
  color: var(--ink-soft);
  font-variant-numeric: tabular-nums;
  min-width: 1.6em;
}
@media (max-width: 768px) { .toc ol { columns: 2; } }
@media (max-width: 480px) { .toc ol { columns: 1; } }

/* ── Chapter heading ───────────────────────────────────────────────── */
.chapter {
  margin: 2.5rem 0 1rem;
  scroll-margin-top: 140px;
}
.chapter-eyebrow {
  font-family: var(--sans-en);
  font-size: 0.7rem;
  letter-spacing: 0.18em;
  text-transform: uppercase;
  color: var(--ink-soft);
  margin: 0 0 0.25rem;
}
.chapter h2 {
  font-family: var(--serif-tc);
  font-size: 1.5rem;
  font-weight: 600;
  color: var(--accent);
  margin: 0;
  padding: 0.85rem 1.1rem;
  background: var(--accent-soft);
  border-left: 4px solid var(--accent);
  border-radius: 0 8px 8px 0;
  line-height: 1.35;
}
.chapter h2 a {
  color: inherit;
  text-decoration: none;
  display: block;
}
.chapter h2 a:hover {
  opacity: 0.8;
}

/* ── Tables (side-by-side comparison) ─────────────────────────────── */
.bilingual-table {
  width: 100%;
  border-collapse: separate;
  border-spacing: 0;
  background: var(--surface);
  border-radius: 8px;
  overflow: hidden;
  box-shadow: var(--shadow);
  margin: 0 0 2.25rem;
  table-layout: fixed;
}
.bilingual-table thead th {
  font-family: var(--sans-tc);
  font-size: 0.78rem;
  font-weight: 600;
  letter-spacing: 0.04em;
  color: var(--ink-muted);
  text-align: left;
  padding: 0.9rem 1.1rem;
  background: var(--surface-alt);
  border-bottom: 2px solid var(--border);
  position: sticky;
  top: 0;
  z-index: 1;
}
.bilingual-table tbody td {
  padding: 1.1rem 1.25rem;
  border-bottom: 1px solid var(--border);
  vertical-align: top;
  line-height: 1.85;
  font-size: 0.97rem;
}
.bilingual-table tbody tr:last-child td { border-bottom: none; }
.bilingual-table tbody tr:nth-child(even) td { background: var(--surface-alt); }
.bilingual-table tbody tr:hover td { background: var(--accent-soft); }

/* Column-specific typography */
.bilingual-table td.src,
.bilingual-table td.simp,
.bilingual-table td.trans,
.bilingual-table td.tailo { word-wrap: break-word; }
.bilingual-table td.src {
  font-family: var(--serif-en);
  color: #2c2a26;
}
.bilingual-table td.trans,
.bilingual-table td.simp {
  font-family: var(--sans-tc);
  color: var(--ink);
}
.bilingual-table td.tailo {
  font-family: var(--sans-tc);
  color: var(--ink);
  font-size: 0.93rem;
  line-height: 1.95;
}

/* Column widths for 2-col and 4-col */
.bilingual-table.cols-2 td.src,
.bilingual-table.cols-2 td.trans,
.bilingual-table.cols-2 td.simp,
.bilingual-table.cols-2 td.tailo { width: 50%; }
.bilingual-table.cols-4 td.src { width: 28%; }
.bilingual-table.cols-4 td.trans { width: 26%; }
.bilingual-table.cols-4 td.simp { width: 23%; }
.bilingual-table.cols-4 td.tailo { width: 23%; }

/* Number badge for segments */
.seg-num {
  display: inline-block;
  font-family: var(--sans-en);
  font-size: 0.7rem;
  color: var(--ink-soft);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 0.05rem 0.4rem;
  margin-right: 0.4rem;
  font-variant-numeric: tabular-nums;
  vertical-align: middle;
}

/* ── Single-column flow (for narrative delivery) ───────────────────── */
.para-flow { max-width: 720px; margin: 0 auto; }
.para-flow .para {
  font-family: var(--serif-tc);
  font-size: 1.05rem;
  line-height: 2;
  text-align: justify;
  text-justify: inter-ideograph;
  margin: 0 0 1.3em;
  text-indent: 0;
}
.para-flow .para + .para { text-indent: 2em; }

/* ── Literary package (synopsis, bio, market) ──────────────────────── */
.lit-package {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 1.25rem 1.5rem;
  margin: 0 0 2rem;
  box-shadow: var(--shadow);
}
.lit-package h2 {
  font-family: var(--serif-tc);
  font-size: 1.2rem;
  font-weight: 600;
  color: var(--accent);
  margin: 0 0 0.75rem;
  padding: 0;
  background: none;
  border: none;
  border-radius: 0;
}
.lit-package .field {
  font-family: var(--sans-tc);
  font-size: 0.85rem;
  color: var(--ink-soft);
  margin-right: 0.5rem;
}
.lit-package .field-row { padding: 0.35rem 0; border-bottom: 1px dotted var(--border); }
.lit-package .field-row:last-child { border-bottom: none; }
.lit-package .synopsis,
.lit-package .bio,
.lit-package .market {
  font-family: var(--serif-tc);
  font-size: 0.98rem;
  line-height: 1.85;
  color: var(--ink);
}
.lit-package p { margin: 0 0 0.8em; }
.lit-package strong { color: var(--accent); }

/* ── QA score chip ─────────────────────────────────────────────────── */
.qa-score {
  display: inline-block;
  background: #eaf3de;
  color: #3b6d11;
  padding: 0.25rem 0.7rem;
  border-radius: 4px;
  font-size: 0.85rem;
  font-family: var(--sans-en);
  font-weight: 500;
}

/* ── Footer ────────────────────────────────────────────────────────── */
.footer {
  margin-top: 3rem;
  padding: 1.25rem 0 0;
  border-top: 1px solid var(--border);
  color: var(--ink-soft);
  font-size: 0.78rem;
  text-align: center;
  font-family: var(--sans-en);
  letter-spacing: 0.02em;
}
.footer a { color: var(--accent); text-decoration: none; }

/* ── Print styles ──────────────────────────────────────────────────── */
@media print {
  body { background: white; color: black; }
  .page { max-width: 100%; padding: 0 1cm 2cm; }
  .book-header {
    background: var(--accent) !important;
    color: white !important;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
    box-shadow: none;
    border-radius: 0;
  }
  .book-header::before { display: none; }
  .book-title { color: white !important; }
  .lang-badge {
    background: rgba(255, 255, 255, 0.15) !important;
    -webkit-print-color-adjust: exact;
    print-color-adjust: exact;
  }
  .toc { page-break-after: always; box-shadow: none; }
  .chapter { page-break-before: always; }
  .bilingual-table {
    box-shadow: none;
    border: 1px solid #ccc;
    page-break-inside: auto;
  }
  .bilingual-table tr { page-break-inside: avoid; }
  .bilingual-table tbody tr:hover td { background: transparent !important; }
  .bilingual-table thead th { position: static; }
  .lit-package { box-shadow: none; }
}

/* ── Reader section (single-column simplified narrative) ──────────────── */
.reader-section { max-width: 720px; margin: 0 auto; }
.reader-section .para {
  font-family: var(--serif-tc);
  font-size: 1.05rem;
  line-height: 2;
  text-align: justify;
  text-justify: inter-ideograph;
  margin: 0 0 1.3em;
  text-indent: 0;
}
.reader-section .para + .para { text-indent: 2em; }

/* ── Side-by-side full vs simplified (2-col narrative per chapter) ─────── */
.comparison-chapter {
  margin: 2rem 0;
  display: flex;
  gap: 1.5rem;
  flex-wrap: wrap;
}
.comparison-chapter .col {
  flex: 1 1 300px;
  min-width: 280px;
  background: var(--surface);
  border-radius: 8px;
  padding: 1.1rem 1.25rem;
  box-shadow: var(--shadow);
}
.comparison-chapter .col-label {
  font-family: var(--sans-en);
  font-size: 0.72rem;
  letter-spacing: 0.12em;
  text-transform: uppercase;
  color: var(--ink-soft);
  font-weight: 600;
  margin: 0 0 0.65rem;
  padding-bottom: 0.5rem;
  border-bottom: 1px solid var(--border);
}
.comparison-chapter .col p {
  font-family: var(--serif-tc);
  font-size: 1rem;
  line-height: 1.9;
  text-align: justify;
  text-justify: inter-ideograph;
  margin: 0 0 1em;
  text-indent: 0;
}
.comparison-chapter .col p + p { text-indent: 2em; }
@media (max-width: 640px) {
  .page { padding: 0 0.75rem 2rem; }
  .book-header { margin: 0 -0.75rem 1.25rem; padding: 1.75rem 1.25rem 1.5rem; }
  .book-title { font-size: 1.5rem; }
  .book-author { font-size: 0.95rem; }
  .page-title { font-size: 1.1rem; }
  .chapter h2 { font-size: 1.2rem; padding: 0.7rem 0.9rem; }
  .bilingual-table tbody td { padding: 0.85rem 0.95rem; font-size: 0.92rem; }
  .bilingual-table thead th { padding: 0.65rem 0.85rem; font-size: 0.7rem; }
  .bilingual-table.cols-4 td,
  .bilingual-table.cols-4 th { font-size: 0.82rem; padding: 0.65rem 0.55rem; }
}
"""


# ── Public helpers ───────────────────────────────────────────────────────

def _safe_anchor(title: str, index: int) -> str:
    """Make a stable, HTML-safe anchor id from a chapter title."""
    slug = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", title or "").strip("-").lower()
    return f"ch-{index:03d}-{slug}"[:80] or f"ch-{index:03d}"


def html_text(text: str) -> str:
    """HTML-escape and convert newlines to <br>."""
    if not text:
        return ""
    return escape(text).replace("\n", "<br>")


def book_header(
    title: str,
    *,
    authors: Optional[List[str]] = None,
    eyebrow: str = "OTS 翻譯服務",
    source_lang: str = "",
    target_lang: str = "",
    extra_meta: str = "",
) -> str:
    """Render the book-cover style header.

    `extra_meta` is raw HTML for additional chips (e.g. chapter count, QA score).
    """
    title_safe = escape(title or "Untitled")
    author_str = escape(", ".join(authors)) if authors else ""

    lang_chip = ""
    if source_lang or target_lang:
        sl = escape(source_lang) if source_lang else "原文"
        tl = escape(target_lang) if target_lang else "譯文"
        lang_chip = (
            f'<span class="lang-badge">{sl}'
            f'<span class="arrow">→</span>{tl}</span>'
        )

    return f"""
<header class="book-header">
  <div class="book-eyebrow">{escape(eyebrow)}</div>
  <h1 class="book-title">{title_safe}</h1>
  <p class="book-author">{author_str}</p>
  <div class="book-meta">
    {lang_chip}
    {extra_meta}
  </div>
</header>
"""


def toc(chapters: List[dict]) -> str:
    """Render a 3-column table of contents linking to chapter anchors."""
    if not chapters:
        return ""
    items = []
    for i, ch in enumerate(chapters):
        title = escape(ch.get("title") or f"Chapter {i + 1}")
        anchor = _safe_anchor(ch.get("title", ""), i)
        items.append(f'<li><a href="#{anchor}">{title}</a></li>')
    return f"""
<nav class="toc" aria-label="Table of contents">
  <h3>目錄 · Contents</h3>
  <ol>{"".join(items)}</ol>
</nav>
"""


def page_intro(text: str) -> str:
    """Render a short intro/description card under the page title."""
    if not text:
        return ""
    return f'<div class="page-intro">{text}</div>\n'


def chapter_heading(title: str, index: int) -> str:
    """Render an anchored chapter heading.

    The heading itself renders as a self-referencing anchor link so
    that clicking it (or right-click → Copy Link Address) scrolls
    to the top of the chapter section.
    """
    safe_title = escape(title or f"Chapter {index + 1}")
    anchor = _safe_anchor(title, index)
    num = f"{index + 1:02d}"
    return f"""
<section class="chapter" id="{anchor}">
  <div class="chapter-eyebrow">Chapter {num}</div>
  <h2><a href="#{anchor}">{safe_title}</a></h2>
</section>
"""


def table_open(num_cols: int, labels: List[str]) -> str:
    """Open a styled bilingual table with column headers."""
    klass = "bilingual-table"
    klass += f" cols-{num_cols}"
    ths = "".join(f"<th>{escape(lbl)}</th>" for lbl in labels)
    return f'<table class="{klass}"><thead><tr>{ths}</tr></thead><tbody>'


def table_close() -> str:
    return "</tbody></table>"


def footer(order_id: str = "", contact: str = "service@ots.tw") -> str:
    """Render the page footer with attribution."""
    order_note = f" · 訂單 {escape(order_id)}" if order_id else ""
    return (
        f'<div class="footer">'
        f'本譯文由 OTS 翻譯服務（AI 輔助翻譯）提供{order_note} · '
        f'聯繫 <a href="mailto:{escape(contact)}">{escape(contact)}</a>'
        f'</div>'
    )


# ── High-level document builders ─────────────────────────────────────────

def build_bilingual_document(
    *,
    title: str,
    authors: Optional[List[str]] = None,
    source_lang: str = "",
    target_lang: str = "",
    eyebrow: str = "OTS 翻譯服務",
    page_subtitle: str = "",
    page_description: str = "",
    chapters: Optional[List[dict]] = None,
    extra_meta: str = "",
    order_id: str = "",
    html_lang: str = "zh-Hant",
) -> str:
    """Wrap HTML body content in a full document with header + optional TOC.

    Use for side-by-side or single-column translations.
    """
    body = []

    body.append(book_header(
        title,
        authors=authors,
        eyebrow=eyebrow,
        source_lang=source_lang,
        target_lang=target_lang,
        extra_meta=extra_meta,
    ))

    if page_subtitle:
        body.append(f'<h2 class="page-title">{escape(page_subtitle)}</h2>')

    if page_description:
        body.append(page_intro(page_description))

    if chapters:
        body.append(toc(chapters))

    body.append('<main class="page">')

    return "\n".join(body)


def close_document(order_id: str = "") -> str:
    """Close `<main>`, the document, with a footer."""
    return f"{footer(order_id)}\n</main>\n</body>\n</html>"


def render_doc(
    *,
    title: str,
    body_html: str,
    authors: Optional[List[str]] = None,
    source_lang: str = "",
    target_lang: str = "",
    eyebrow: str = "OTS 翻譯服務",
    page_subtitle: str = "",
    page_description: str = "",
    chapters: Optional[List[dict]] = None,
    extra_meta: str = "",
    order_id: str = "",
    html_lang: str = "zh-Hant",
) -> str:
    """Render a complete <!DOCTYPE html> document with all chrome.

    `body_html` is the raw inner content (chapter tables / paragraphs).
    """
    # If chapters provided, inject anchored headings + TOC into a default body
    if chapters and body_html:
        toc_html = toc(chapters)
    else:
        toc_html = ""

    head = build_bilingual_document(
        title=title,
        authors=authors,
        source_lang=source_lang,
        target_lang=target_lang,
        eyebrow=eyebrow,
        page_subtitle=page_subtitle,
        page_description=page_description,
        chapters=None,  # TOC rendered after main wrapper
        extra_meta=extra_meta,
        order_id=order_id,
        html_lang=html_lang,
    )
    # The build_bilingual_document returns everything up to and including <main class="page">.
    # For our use we want header + subtitle + intro + <main>; TOC goes inside main.

    parts = [
        "<!DOCTYPE html>",
        f'<html lang="{escape(html_lang)}">',
        "<head>",
        '<meta charset="UTF-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1.0">',
        f"<title>{escape(title)}</title>",
        f"<style>{FONT_IMPORT}{BASE_CSS}</style>",
        "</head>",
        "<body>",
        book_header(
            title,
            authors=authors,
            eyebrow=eyebrow,
            source_lang=source_lang,
            target_lang=target_lang,
            extra_meta=extra_meta,
        ),
    ]
    if page_subtitle:
        parts.append(f'<h2 class="page-title">{escape(page_subtitle)}</h2>')
    if page_description:
        parts.append(page_intro(page_description))
    parts.append('<main class="page">')
    if toc_html:
        parts.append(toc_html)
    parts.append(body_html)
    parts.append(footer(order_id))
    parts.append("</main>")
    parts.append("</body>")
    parts.append("</html>")
    return "\n".join(parts)


def chapter_segments_rows(
    chapters: List[dict],
    source_segments: List[dict],
    entries_by_index: Dict[int, dict],
    *,
    num_cols: int,
    cell_keys: List[str],
    cell_classes: List[str],
) -> str:
    """Render rows for a multi-chapter bilingual/comparison table.

    `cell_keys`/`cell_classes` are parallel lists; one per column.
    `entries_by_index[i]` is a dict like `{"text": ..., "translated": ..., "simplified": ..., "tailo": ...}`.
    Missing source/entries are skipped per-row.
    """
    out: List[str] = []
    for ch in chapters:
        seg_start = ch.get("segment_start", 0)
        seg_end   = ch.get("segment_end", seg_start)
        title     = ch.get("title") or f"Chapter {ch.get('index', 0) + 1}"
        out.append(chapter_heading(title, ch.get("index", 0)))

        any_row = False
        for i in range(seg_start, seg_end):
            src_seg = source_segments[i] if i < len(source_segments) else {}
            entry   = entries_by_index.get(i, {})
            cells = []
            for key, cls in zip(cell_keys, cell_classes):
                if key == "source" or key == "text":
                    text = src_seg.get("text", "")
                else:
                    text = entry.get(key, "")
                if cls == "src" and "seg-num" not in str(text):
                    text_with_num = (
                        f'<span class="seg-num">{i + 1}</span>{html_text(text)}'
                    )
                    cells.append(f'<td class="{cls}">{text_with_num}</td>')
                else:
                    cells.append(f'<td class="{cls}">{html_text(text)}</td>')
            # Skip rows where all cells are empty
            if not any(
                (k == "source" and src_seg.get("text"))
                or (k != "source" and entry.get(k))
                for k in cell_keys
            ):
                continue
            out.append("<tr>" + "".join(cells) + "</tr>")
            any_row = True
        # If a chapter has no rows (unlikely), still show the heading
    return "\n".join(out)


def chapter_text_block(chapter_entry: dict, index: int) -> str:
    """Render a chapter's simplified text as a narrative paragraph block.

    The output is a ``<section>`` with anchored heading + ``.reader-section``
    paragraph flow. Used by the reader-style single-column simplified output.
    """
    title = chapter_entry.get("title") or f"Chapter {index + 1}"
    text = chapter_entry.get("text", "")
    heading = chapter_heading(title, index)
    if not text.strip():
        return heading

    paras = re.split(r"\n\n+", text.strip())
    para_html = "\n".join(
        f'<p class="para">{escape(p.strip())}</p>'
        for p in paras if p.strip()
    )
    return heading + f'\n<div class="reader-section">{para_html}</div>'


def comparison_chapter_block(
    full_text: str,
    simplified_text: str,
    title: str,
    index: int,
    *,
    label_full: str = "標準翻譯",
    label_simplified: str = "青少年版",
) -> str:
    """Render a chapter as a side-by-side comparison of two versions.

    ``full_text`` is the standard Chinese translation (all segments
    concatenated). ``simplified_text`` is the simplified version for
    the same chapter. Rendered as two equal-width columns.
    """
    heading = chapter_heading(title, index)

    def _para_div(text: str, label: str) -> str:
        paras = re.split(r"\n\n+", text.strip())
        para_html = "\n".join(
            f"<p>{escape(p.strip())}</p>" for p in paras if p.strip()
        )
        return (
            f'<div class="col">'
            f'<div class="col-label">{escape(label)}</div>'
            f"{para_html}"
            f"</div>"
        )

    col_full = _para_div(full_text, label_full)
    col_simp = _para_div(simplified_text, label_simplified)

    return (
        f'{heading}\n<div class="comparison-chapter">'
        f"{col_full}{col_simp}</div>"
    )
