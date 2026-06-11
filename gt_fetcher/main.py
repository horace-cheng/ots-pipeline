"""
gt_fetcher/main.py — Cloud Run Job (Gutenberg Track) v2

Fetches a book from Project Gutenberg (EPUB primary, plain-text fallback)
and writes the joined chapter text + minimal metadata to GCS. Chapter
detection and paragraph segmentation are handled by the next job in the
pipeline (``gt_chapter_splitter``), which uses Gemini to identify chapter
boundaries semantically and falls back to the regex chain in this file
if the LLM is unavailable.

Reads:
  - ORDER_ID env var
  - orders.notes JSON containing {"gutenberg_book_id": N}

Writes (GCS temp):
  - source/full_text.txt            entire book, plain text (chapters joined
                                    with "\\n\\n", license already stripped)
  - metadata.json                   book info: book_id, title, authors, language
                                    (num_chapters / num_segments / word_count
                                    are added by gt_chapter_splitter)
"""
import asyncio
import io
import json
import logging
import posixpath
import re
import sys
import zipfile
from html.parser import HTMLParser
from pathlib import Path
from typing import List

import httpx

sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import text

from shared.config import cfg
from shared.db import get_db, update_job_status, update_order_field
from shared.storage import get_client as get_gcs_client

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("gt_fetcher")

EPUB_URL_PATTERN  = "https://www.gutenberg.org/ebooks/{id}.epub.noimages"
TEXT_URL_PATTERNS = [
    "https://www.gutenberg.org/cache/epub/{id}/pg{id}.txt",
    "https://www.gutenberg.org/files/{id}/{id}-0.txt",
    "https://www.gutenberg.org/files/{id}/{id}.txt",
]
SOURCE_FULL_TEXT = "source/full_text.txt"
CHAPTERS_JSON    = "source/chapters.json"

# Plain-text chapter detection (fallback only)
CHAPTER_RE = re.compile(
    r'^[ \t]*CHAPTER[ \t]+[IVXLCDM\d]+[^\n]*$',
    re.IGNORECASE | re.MULTILINE,
)
# Bare Roman-numeral chapter markers: "I.", "II.", "XII. The Golden Age", "EPILOGUE".
# Used for books like H. G. Wells' "The Time Machine" which use Roman numerals
# without the word "CHAPTER". Caps at 50 (L) to avoid matching roman numerals in
# body text. The line must contain ONLY the marker (and optional title) — the
# rest of the line can be anything, but nothing should precede the marker.
# Also matches PROLOGUE / EPILOGUE / CONCLUSION on their own line.
# Allows trailing \r (CRLF line endings) — Python re's `$` only matches
# before \n in MULTILINE mode, so we explicitly permit \r at line end.
ROMAN_CHAPTER_RE = re.compile(
    r'^[ \t]*(?:'
    r'(?:I{1,3}|IV|VI{0,3}|IX|X|XI{0,3}|XIV|XV|XVI{0,3}|XIX|XX|XXI{0,3}|'
    r'XXIV|XXV|XXVI{0,3}|XXIX|XXX|XXXI{0,3}|XXXIV|XXXV|XXXVI{0,3}|XXXIX|XL|XLI{0,3}?|'
    r'XLIV|XLV|XLVI{0,3}|XLIX|L)(?:[.:][^\n\r]*)?'
    r'|PROLOGUE|EPILOGUE|CONCLUSION'
    r')[ \t\r]*$',
    re.IGNORECASE | re.MULTILINE,
)
META_RE     = re.compile(r'^(Title|Author|Language)\s*:\s*(.+?)\s*$', re.MULTILINE)
START_MARKER = re.compile(
    r'\*\*\*\s*START OF (THE|THIS) PROJECT GUTENBERG', re.IGNORECASE
)
END_MARKER = re.compile(
    r'\*\*\*\s*END OF (THE|THIS) PROJECT GUTENBERG', re.IGNORECASE
)
LANG_NAME_TO_CODE = {
    "english":  "en", "french":  "fr", "german":   "de", "spanish":  "es",
    "italian":  "it", "portuguese": "pt", "chinese": "zh", "japanese": "ja",
    "dutch":    "nl", "finnish": "fi", "swedish":  "sv", "latin":    "la",
}

# EPUB chapter classification
CHAPTER_ANCHORED_RE = re.compile(
    r'^(?:(chapter|letter)\s+)?([IVXLCDM]+|\d+)(?=[\s\.\-:]|$)',
    re.IGNORECASE,
)
CHAPTER_ANYWHERE_RE = re.compile(
    r'\b(CHAPTER|Chapter|chapter|Letter|LETTER)\s+([IVXLCDM]+|\d+)\b'
)
PART_RE = re.compile(r'^part\s+[a-z0-9]+', re.IGNORECASE)
FRONTMATTER_TERMS = (
    "title page", "contents", "illustrations", "preface", "foreword",
    "dedication", "colophon", "transcriber", "epigraph",
    "advertisement", "errata", "imprint",
)

NS_NCX  = {"ncx": "http://www.daisy.org/z3986/2005/ncx/"}
NS_OPF  = {"opf": "http://www.idpf.org/2007/opf",
           "dc":  "http://purl.org/dc/elements/1.1/"}
NS_HTML = {"x":  "http://www.w3.org/1999/xhtml"}


# ── HTTP ────────────────────────────────────────────────────────────────────

async def _get_with_retry(client: httpx.AsyncClient, url: str, max_attempts: int = 3) -> httpx.Response:
    delay = 0.5
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            return await client.get(url)
        except (httpx.RequestError, httpx.RemoteProtocolError) as e:
            last_exc = e
            if attempt < max_attempts - 1:
                await asyncio.sleep(delay)
                delay *= 2
    raise last_exc  # type: ignore[misc]


# ── Plain-text fallback ─────────────────────────────────────────────────────

def _lang_to_code(name: str) -> str:
    return LANG_NAME_TO_CODE.get(name.lower(), name.lower()[:2] or "en")


def parse_header_metadata(text: str, fallback_book_id: int = 0) -> dict:
    head = text[:2000]
    start = START_MARKER.search(head)
    header = head[: start.start()] if start else head
    title = ""
    authors: List[str] = []
    language = ""
    for match in META_RE.finditer(header):
        key, value = match.group(1), match.group(2).strip()
        if key == "Title" and not title:
            title = value
        elif key == "Author" and not authors:
            authors = [
                a.strip()
                for a in re.split(r',\s*(?![^()]*\))', value)
                if a.strip()
            ]
        elif key == "Language" and not language:
            language = _lang_to_code(value)
    return {
        "title":    title or f"Gutenberg Book {fallback_book_id}",
        "authors":  authors,
        "language": language or "en",
    }


def _strip_gutenberg_boilerplate(text: str) -> str:
    """Strip the Project Gutenberg license header and footer.

    Gutenberg text files contain a license header (before ``*** START OF ...``)
    and a license footer (after ``*** END OF ...``). Lines like
    `` EBOOK <TITLE> ***`` or ``*** EBOOK <TITLE> ***`` that follow the
    START marker are also Gutenberg boilerplate. If no markers are present,
    return the text unchanged.
    """
    start = START_MARKER.search(text)
    if start:
        text = text[start.end():]
        # Strip up to 3 consecutive boilerplate lines that contain
        # "EBOOK ... ***" near the top of the body.
        for _ in range(3):
            new_text = re.sub(
                r'^[ \t]*[^\n]*EBOOK[^\n]*\*\*\*[ \t]*\n+',
                '',
                text,
                count=1,
            )
            if new_text == text:
                break
            text = new_text
    end = END_MARKER.search(text)
    if end:
        text = text[: end.start()]
    return text.strip()


def split_text_structured(text: str) -> List[str]:
    # Strip Gutenberg license header/footer so they don't get counted as
    # "chapters" by the paragraph fallback (e.g. The Time Machine #35 had
    # 387 paragraphs because the entire license was included).
    text = _strip_gutenberg_boilerplate(text)

    # First try the explicit "CHAPTER X" pattern.
    chapter_splits = CHAPTER_RE.split(text)
    if len(chapter_splits) > 1:
        return _clean_chunks(chapter_splits)

    # Next try bare Roman-numeral chapter markers ("I.", "II.", "XII. Title",
    # "EPILOGUE") — used by books like H. G. Wells' "The Time Machine".
    # Require at least 3 matches to avoid splitting on stray "I." / "II."
    # lines in body text.
    roman_matches = ROMAN_CHAPTER_RE.findall(text)
    if len(roman_matches) >= 3:
        roman_splits = ROMAN_CHAPTER_RE.split(text)
        return _clean_chunks(roman_splits)

    paragraph_splits = re.split(r'\n\s*\n', text)
    if len(paragraph_splits) > 1:
        return _clean_chunks(paragraph_splits)
    sentence_splits = re.split(r'(?<=[.!?])\s+', text)
    return _clean_chunks(sentence_splits)


def _clean_chunks(raw: List[str]) -> List[str]:
    return [c.strip() for c in raw if c and c.strip()]


async def fetch_book_text(book_id: int) -> str:
    """Fetch plain-text body of a Gutenberg book (fallback path)."""
    async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
        for pattern in TEXT_URL_PATTERNS:
            url = pattern.format(id=book_id)
            try:
                resp = await _get_with_retry(client, url)
                if resp.status_code == 200:
                    logger.info(f"Fetched book {book_id} from {url}")
                    return resp.text
            except httpx.HTTPStatusError as e:
                if e.response.status_code == 404:
                    continue
                raise
        raise ValueError(
            f"No text file found for Gutenberg book {book_id} "
            f"(tried {len(TEXT_URL_PATTERNS)} patterns)"
        )


def fetch_book_metadata(book_id: int, text_content: str | None = None) -> dict:
    if text_content is None:
        text_content = asyncio.run(fetch_book_text(book_id))
    meta = parse_header_metadata(text_content, fallback_book_id=book_id)
    return {
        "book_id":  book_id,
        "title":    meta["title"],
        "authors":  meta["authors"],
        "language": meta["language"],
    }


# ── EPUB primary path ──────────────────────────────────────────────────────

class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: List[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _strip_html(html: str) -> str:
    parser = _TextExtractor()
    try:
        parser.feed(html)
    except Exception:
        pass
    text = "".join(parser.parts)
    text = re.sub(r'[ \t]+', ' ', text)
    text = re.sub(r'\n\s*\n+', '\n\n', text)
    return text.strip()


async def _fetch_epub_bytes(book_id: int) -> bytes | None:
    url = EPUB_URL_PATTERN.format(id=book_id)
    try:
        async with httpx.AsyncClient(timeout=60.0, follow_redirects=True) as client:
            resp = await _get_with_retry(client, url)
            if resp.status_code == 200 and len(resp.content) > 1000:
                logger.info(f"Fetched EPUB for book {book_id} ({len(resp.content)} bytes)")
                return resp.content
    except Exception as e:
        logger.warning(f"Failed to download EPUB for book {book_id}: {e}")
    return None


def _parse_opf(zf: zipfile.ZipFile) -> dict:
    opf_name = next((n for n in zf.namelist() if n.endswith(".opf")), None)
    if not opf_name:
        raise ValueError("No .opf in EPUB")
    root = __import__("xml.etree.ElementTree", fromlist=["fromstring"]).fromstring(
        zf.read(opf_name).decode("utf-8")
    )
    title    = root.find(".//dc:title",    NS_OPF)
    creator  = root.find(".//dc:creator",  NS_OPF)
    language = root.find(".//dc:language", NS_OPF)
    return {
        "title":    (title.text or "").strip(),
        "authors":  [creator.text.strip()] if creator is not None and creator.text else [],
        "language": (
            (language.text or "en").split("-")[0].lower()
            if language is not None and language.text else "en"
        ),
    }


def _parse_nav(zf: zipfile.ZipFile):
    ET = __import__("xml.etree.ElementTree", fromlist=["fromstring"]).fromstring
    ncx_name = next((n for n in zf.namelist() if n.endswith(".ncx")), None)
    if ncx_name:
        root = ET(zf.read(ncx_name).decode("utf-8"))
        result = []
        for np in root.findall(".//ncx:navPoint", NS_NCX):
            label = np.find("ncx:navLabel/ncx:text", NS_NCX)
            content = np.find("ncx:content", NS_NCX)
            if label is None or content is None:
                continue
            result.append({
                "label": (label.text or "").strip(),
                "src":   (content.get("src") or "").split("#")[0],
            })
        return result, ncx_name
    nav_name = next((n for n in zf.namelist() if n.endswith("nav.xhtml")), None)
    if nav_name:
        root = ET(zf.read(nav_name).decode("utf-8"))
        result = []
        for a in root.findall(".//x:a", NS_HTML):
            label = "".join(a.itertext()).strip()
            src = (a.get("href") or "").split("#")[0]
            if label and src:
                result.append({"label": label, "src": src})
        return result, nav_name
    return [], None


def _is_chapter_label(label: str) -> bool:
    label_clean = label.strip().lower()
    if not label_clean:
        return False
    if PART_RE.match(label_clean):
        return False
    if any(label_clean == t or label_clean.startswith(t + " ") for t in FRONTMATTER_TERMS):
        return False
    if CHAPTER_ANCHORED_RE.match(label.strip()):
        return True
    if CHAPTER_ANYWHERE_RE.search(label):
        return True
    return False


def _resolve_epub_path(zf: zipfile.ZipFile, src: str, ref_path: str) -> str | None:
    if not src:
        return None
    ref_dir = posixpath.dirname(ref_path)
    full = posixpath.normpath(posixpath.join(ref_dir, src)) if ref_dir else src
    return full if full in zf.namelist() else None


def _parse_epub(epub_bytes: bytes, fallback_book_id: int = 0) -> dict:
    zf = zipfile.ZipFile(io.BytesIO(epub_bytes))
    metadata = _parse_opf(zf)
    nav_points, ref_path = _parse_nav(zf)
    if not nav_points or not ref_path:
        raise ValueError("EPUB has no NCX/nav TOC")
    chapters: List[dict] = []
    for np in nav_points:
        if not _is_chapter_label(np["label"]):
            continue
        full_path = _resolve_epub_path(zf, np["src"], ref_path)
        if not full_path:
            continue
        try:
            xhtml = zf.read(full_path).decode("utf-8", errors="replace")
        except Exception as e:
            logger.warning(f"Failed to read {full_path}: {e}")
            continue
        text = _strip_html(xhtml)
        if not text:
            continue
        chapters.append({
            "index": len(chapters),
            "title": np["label"],
            "text":  text,
        })
    if not chapters:
        raise ValueError("No chapters extracted from EPUB")
    return {
        "book_id":  fallback_book_id,
        "title":    metadata["title"] or f"Gutenberg Book {fallback_book_id}",
        "authors":  metadata["authors"],
        "language": metadata["language"] or "en",
        "chapters": chapters,
    }


async def fetch_book(book_id: int) -> dict:
    """Fetch a book: EPUB primary, plain-text fallback. Returns chapters list."""
    epub_bytes = await _fetch_epub_bytes(book_id)
    if epub_bytes:
        try:
            return _parse_epub(epub_bytes, fallback_book_id=book_id)
        except Exception as e:
            logger.warning(f"EPUB parse failed for book {book_id}: {e}, falling back to text")
    text = await fetch_book_text(book_id)
    meta = parse_header_metadata(text, fallback_book_id=book_id)
    chunks = split_text_structured(text)
    chapters = [
        {"index": i, "title": f"Part {i+1}", "text": c}
        for i, c in enumerate(chunks)
    ]
    return {
        "book_id":  book_id,
        "title":    meta["title"],
        "authors":  meta["authors"],
        "language": meta["language"],
        "chapters": chapters,
    }


# ── GCS write helpers ──────────────────────────────────────────────────────

def _source_path(filename: str) -> str:
    return f"pipeline/{cfg.ORDER_ID}/{filename}"


def save_downloaded_text_to_gcs(chapters: List[dict], metadata: dict) -> dict:
    """Write ``source/full_text.txt`` and a minimal ``metadata.json``.

    This job is now download-only. Chapter detection and paragraph
    segmentation are handled by ``gt_chapter_splitter``, which adds
    ``num_chapters`` / ``num_segments`` / ``word_count`` to
    ``metadata.json`` on its own.

    The joined full text is just the chapter texts separated by ``\\n\\n``
    — exactly what the old v2 writer produced for ``source/full_text.txt``.
    The fallback path in ``gt_chapter_splitter`` relies on this layout to
    compute identical ``char_start`` / ``char_end`` offsets via a running
    counter.
    """
    client = get_gcs_client()
    bucket = client.bucket(cfg.BUCKET_TEMP)

    full_text = "\n\n".join(ch["text"].strip() for ch in chapters)
    bucket.blob(_source_path(SOURCE_FULL_TEXT)).upload_from_string(
        full_text.encode("utf-8"), content_type="text/plain",
    )

    minimal_metadata = {
        "book_id":  metadata["book_id"],
        "title":    metadata["title"],
        "authors":  metadata["authors"],
        "language": metadata["language"],
    }
    bucket.blob(_source_path("metadata.json")).upload_from_string(
        json.dumps(minimal_metadata, ensure_ascii=False, indent=2),
        content_type="application/json",
    )

    logger.info(
        f"Wrote download artifacts: source/full_text.txt "
        f"({len(full_text)} chars from {len(chapters)} source chapters) "
        f"+ metadata.json (book_id/title/authors/language)"
    )
    return minimal_metadata


# ── Paragraph segmentation (FT-style) — kept for splitter's fallback ──────
# ``split_text_structured`` already provides coarse chapter-level splits
# via the CHAPTER_RE / ROMAN_CHAPTER_RE / paragraph-split chain. The new
# ``gt_chapter_splitter`` job imports this function as its regex fallback
# (replacing the previous behaviour of computing char offsets in this job).
# ``split_paragraphs`` lives in ``gt_chapter_splitter`` now (the splitter
# is the only consumer of paragraph-level splitting).


# ── DB read of gutenberg_book_id from order notes ──────────────────────────

def get_book_id_from_order() -> int:
    with get_db() as db:
        row = db.execute(
            text("SELECT notes FROM orders WHERE id = :order_id"),
            {"order_id": cfg.ORDER_ID},
        ).fetchone()
        if not row:
            raise ValueError(f"Order not found: {cfg.ORDER_ID}")
        notes_raw = row[0]
        if not notes_raw:
            raise ValueError(f"Order {cfg.ORDER_ID} has no notes (missing gutenberg_book_id)")
        try:
            notes = json.loads(notes_raw) if isinstance(notes_raw, str) else notes_raw
        except json.JSONDecodeError as e:
            raise ValueError(f"Order notes is not valid JSON: {e}")
        book_id = notes.get("gutenberg_book_id")
        if not book_id:
            raise ValueError(f"Order notes missing gutenberg_book_id: {notes}")
        return int(book_id)


# ── Main entry point ───────────────────────────────────────────────────────

def run():
    logger.info(f"=== gt_fetcher START — order: {cfg.ORDER_ID} ===")
    update_job_status("gt_fetcher", "running")
    update_order_field("status", "processing")

    try:
        book_id = get_book_id_from_order()
        logger.info(f"Fetching Gutenberg book {book_id}")

        book = asyncio.run(fetch_book(book_id))
        metadata = {
            "book_id":  book["book_id"],
            "title":    book["title"],
            "authors":  book["authors"],
            "language": book["language"],
        }
        logger.info(f"Book: {metadata['title']} by {', '.join(metadata['authors'])}")
        logger.info(f"  source: EPUB={len(book['chapters'])} chapters" if book["chapters"] else "  source: text")

        chunks = [ch["text"] for ch in book["chapters"]]
        if not chunks:
            raise ValueError("fetch_book produced no chunks")

        num_source_chapters = len(book["chapters"])
        saved_metadata = save_downloaded_text_to_gcs(book["chapters"], metadata)

        update_order_field("title", metadata["title"])
        update_job_status("gt_fetcher", "success", qa_result={
            **saved_metadata,
            "num_source_chapters": num_source_chapters,
        })
        logger.info(
            f"=== gt_fetcher DONE — {num_source_chapters} source chapters, "
            f"{len(chunks) and sum(len(c) for c in chunks)} chars downloaded. "
            f"Run gt_chapter_splitter next. ==="
        )

    except Exception as e:
        logger.error(f"gt_fetcher failed: {e}", exc_info=True)
        update_job_status("gt_fetcher", "failed", error_message=str(e)[:500])
        raise


if __name__ == "__main__":
    run()
