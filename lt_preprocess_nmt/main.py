"""
lt_preprocess_nmt/main.py — Cloud Run Job

Literary Track Step 1: Preprocess + NMT (AI Draft)
- 從 GCS 讀取客戶上傳的原始文本 + 支援材料
- UTF-8 正規化、段落分割
- 讀取支援材料（glossaries, style guides, background docs）作為翻譯上下文
- 針對大型檔案（>10K 字）進行分批翻譯
- 文學翻譯風格 prompt（保留文學性、風格、韻律）
- 寫入中間產物到 GCS temp
- 更新訂單狀態 → processing（等待 admin 指派編輯）
"""

import sys, re, unicodedata, logging, zipfile, io, json
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config  import cfg
from shared.db      import update_job_status, get_order_info, update_order_field, get_db, get_lang_labels
from shared.storage import read_upload, read_temp_json, write_temp_json
from shared.gemini  import translate, create_file_search_store, upload_to_file_search_store, delete_file_search_store

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("lt_preprocess_nmt")


# ── 段落分割（沿用 FT 邏輯）────────────────────────────────────────────────────
def split_paragraphs(text: str) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text)]
    paragraphs = [p for p in paragraphs if p]

    if len(paragraphs) < 2:
        raw_splits = re.split(r"(?<=。)", text)
        paragraphs = [p.strip() for p in raw_splits if p.strip()]

    merged = []
    for para in paragraphs:
        if merged and len(para) < 15:
            merged[-1] += para
        else:
            merged.append(para)

    # LT 使用較大的 segment 以保留上下文（4000 chars vs FT 的 500）
    MAX_SEGMENT_CHARS = 4000
    final = []
    for para in merged:
        if len(para) <= MAX_SEGMENT_CHARS:
            final.append(para)
        else:
            sentences = re.split(r"(?<=。)", para)
            sentences = [s for s in sentences if s.strip()]
            chunk = ""
            for sent in sentences:
                if len(chunk) + len(sent) > MAX_SEGMENT_CHARS and chunk:
                    final.append(chunk)
                    chunk = sent
                else:
                    chunk += sent
            if chunk:
                final.append(chunk)

    return final


# ── 文本提取（沿用 FT 邏輯）────────────────────────────────────────────────────
def _extract_text_from_docx(raw: bytes) -> str | None:
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            doc_xml = zf.read("word/document.xml")
            root = ElementTree.fromstring(doc_xml)
    except (zipfile.BadZipFile, KeyError) as e:
        logger.warning(f"Not a valid .docx file: {e}")
        return None

    paragraphs = []
    current_text = []
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "p":
            text = "".join(current_text)
            if text.strip():
                paragraphs.append(text.strip())
            current_text = []
        elif tag == "t" and elem.text:
            current_text.append(elem.text)
    text = "".join(current_text)
    if text.strip():
        paragraphs.append(text.strip())

    return "\n\n".join(paragraphs)


def _extract_text_from_pdf(raw: bytes) -> str | None:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(raw))
    except Exception as e:
        logger.warning(f"Failed to parse PDF: {e}")
        return None

    pages_text = []
    for page in reader.pages:
        page_text = page.extract_text() or ""
        if not page_text.strip():
            continue

        lines = page_text.split("\n")
        paragraphs = []
        current = []

        for line in lines:
            stripped = line.strip()
            if not stripped:
                if current:
                    paragraphs.append("".join(current))
                    current = []
                continue

            current.append(stripped)

            if stripped[-1:] in '。！？…．!?\u2026' and len(line.rstrip()) < len(line):
                paragraphs.append("".join(current))
                current = []

        if current:
            paragraphs.append("".join(current))

        joined = "\n\n".join(p for p in paragraphs if p.strip())
        if joined:
            pages_text.append(joined)

    result = "\n\n".join(pages_text)
    if not result or len(result.strip()) < 10:
        logger.warning("PDF extraction returned empty or minimal content")
        return None

    return result


def normalize_text(raw: bytes) -> str:
    if raw[:4] == b"%PDF":
        pdf_text = _extract_text_from_pdf(raw)
        if pdf_text is not None:
            text = pdf_text
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("big5", errors="replace")
    elif raw[:4] == b"PK\x03\x04":
        docx_text = _extract_text_from_docx(raw)
        if docx_text is not None:
            text = docx_text
        else:
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("big5", errors="replace")
    elif raw[:1] == b"<" and (b"<!DOCTYPE html" in raw[:200].lower() or b"<html" in raw[:200].lower() or b"<head" in raw[:200].lower() or b"<body" in raw[:200].lower()):
        from html.parser import HTMLParser
        class _TagStripper(HTMLParser):
            def __init__(self):
                super().__init__()
                self._text = []
            def handle_data(self, d):
                if d.strip():
                    self._text.append(d)
        text = raw.decode("utf-8-sig", errors="replace")
        stripper = _TagStripper()
        stripper.feed(text)
        text = "\n".join(stripper._text)
    else:
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("big5", errors="replace")

    text = unicodedata.normalize("NFC", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    return text


# ── 文學翻譯 Prompt ───────────────────────────────────────────────────────────
LT_PROMPT = """You are a professional literary translator specializing in {source_lang_label} to {target_lang} translation.

Translate the following text with careful attention to literary style, tone, rhythm, and emotional nuance.
This is a literary work — preserve the author's voice, imagery, and artistic intent.

Rules:
1. Preserve paragraph structure exactly — do not merge or split paragraphs
2. Translate ALL content faithfully. Maintain cultural references with brief contextual hints only when essential for comprehension
3. Preserve proper nouns, place names, and cultural terms with appropriate romanization
4. Maintain the original tone (formal, colloquial, poetic, etc.)
5. Literary devices (metaphor, alliteration, rhythm) should be preserved where possible
6. Output only the translation, no explanations or preamble
7. Reference materials (glossaries, style guides, background documents) are attached for translation context — use them to inform terminology and style
8. Output ONLY the translations, one per numbered segment, in order. Do NOT include any separator markers like [PARA_SEP] or the segment numbers in your output.
{hanzi_instruction}
Source text:
{source_text}

{target_lang} translation:"""

LANG_LABELS = get_lang_labels("en")


def _get_hanzi_instruction(target_lang: str) -> str:
    """Return extra instruction for Hanzi output when target is tai-lo."""
    if target_lang != "tai-lo":
        return ""
    return (
        "9. CRITICAL — Taiwanese Hokkien output MUST be written in Han characters (台語漢字), "
        "NOT in Pe̍h-ōe-jī romanization.\n"
        "   Correct examples: 我 (not góa), 的 (not ê), 是 (not sī), 有 (not ū), 人 (not lâng), "
        "愛 (not ài), 講 (not kóng), 看 (not khòaⁿ), 這 (not che), 佇 (not tī).\n"
        "   Use Tailo romanization ONLY in parentheses after the Han form for terms without "
        "standard Han characters (e.g., 泅水 (siû-chúi)).\n"
        "   IMPORTANT: Pure romanization output will be rejected. You must produce Han-dominant text "
        "readable by native Taiwanese speakers.\n"
    )


def _has_sufficient_hanzi(text: str, threshold: float = 0.15) -> bool:
    """Check if a tai-lo output has enough Han characters vs pure romanization."""
    if not text.strip():
        return True
    cjk = re.compile(r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufaff]")
    content_chars = re.sub(r"[\s\d\W_]", "", text)
    if not content_chars:
        return False
    hanzi_count = len(cjk.findall(content_chars))
    return hanzi_count / len(content_chars) >= threshold


# ── 分批翻譯（大型檔案支援）────────────────────────────────────────────────────
def translate_batch(
    segments: list[dict],
    prompt_template: str,
    source_lang: str,
    target_lang: str,
    batch_size: int = 3,
    store_name: str | None = None,
) -> list[str]:
    """Translate segments in batches. LT uses smaller batches for higher quality.

    When store_name is provided, support file context is retrieved via
    File Search (RAG) — relevant chunks are auto-retrieved per request,
    so every batch gets reference context without consuming the full
    file token budget.
    """
    results = [""] * len(segments)

    PREAMBLE_RE = re.compile(
        r'^(here are|following|below|sure|certainly|of course|'
        r'here is|here\'s|below is|the following|'
        r'以下是|好的|當然|翻譯如下)',
        re.IGNORECASE
    )

    i = 0
    batch_num = 0
    hanzi_instr = _get_hanzi_instruction(target_lang)
    while i < len(segments):
        remaining = len(segments) - i
        cur_size = min(batch_size, remaining)
        batch = segments[i:i + cur_size]
        batch_slice = list(batch)

        while True:
            combined = "\n\n[PARA_SEP]\n\n".join(
                f"[{j+1}] {seg['text']}" for j, seg in enumerate(batch_slice)
            )

            prompt = prompt_template.format(
                source_text       = combined,
                source_lang_label = LANG_LABELS.get(source_lang, source_lang),
                target_lang       = LANG_LABELS.get(target_lang, target_lang),
                hanzi_instruction = hanzi_instr,
            )

            try:
                response = translate(prompt, max_tokens=16384, store_name=store_name, job_type="lt_preprocess_nmt")
                break  # success
            except ValueError as e:
                if not str(e).startswith("TOKEN_LIMIT:"):
                    raise
                if len(batch_slice) > 1:
                    batch_slice = batch_slice[:max(1, len(batch_slice) // 2)]
                    logger.warning(f"Context window exceeded, halving batch to {len(batch_slice)}")
                    continue
                raise ValueError(f"Segment too large ({len(batch_slice[0]['text'])} chars) even with RAG") from e

        actual_size = len(batch_slice)

        # Parse numbered responses
        numbered_parts = re.findall(r"\[(\d+)\]\s*(.*?)(?=\[\d+\]|$)", response, re.DOTALL)
        if numbered_parts and len(numbered_parts) == actual_size:
            parts = [p.strip() for _, p in numbered_parts]
            # Defensive: filter out PARA_SEP markers the LLM sometimes echoes back
            for k in range(len(parts)):
                if re.match(r'^\[?PARA_SEP\]?$', parts[k], re.IGNORECASE):
                    parts[k] = ""
                    logger.warning(f"Segment {i+k} contains PARA_SEP marker instead of translation")
        else:
            first_marker = re.search(r"\n?\[1\]", response)
            clean_response = response[first_marker.end():] if first_marker else response
            parts = re.split(r"\[PARA_SEP\]|\[\d+\]", clean_response)
            parts = [p.strip() for p in parts if p.strip()]

        # Handle part count mismatch
        if len(parts) > actual_size:
            if len(parts[0]) < len(parts[1]) * 0.3 and not parts[0].rstrip().endswith("."):
                parts[0] = parts[0] + " " + parts[1]
                parts.pop(1)
            while len(parts) > actual_size:
                parts[-2] = parts[-2] + " " + parts.pop(-1)

        elif len(parts) < actual_size:
            first_marker = re.search(r"\n?\[1\]", response)
            clean_response = response[first_marker.end():] if first_marker else response
            fallback = [p.strip() for p in re.split(r"\n{2,}", clean_response) if p.strip()]
            while fallback and PREAMBLE_RE.match(fallback[0]):
                fallback.pop(0)
            if len(fallback) > actual_size:
                if len(fallback[0]) < len(fallback[1]) * 0.3 and not fallback[0].rstrip().endswith("."):
                    fallback[0] = fallback[0] + " " + fallback[1]
                    fallback.pop(1)
                while len(fallback) > actual_size:
                    fallback[-2] = fallback[-2] + " " + fallback.pop(-1)
            if len(fallback) >= actual_size:
                parts = fallback[:actual_size]

        for k, part in enumerate(parts[:actual_size]):
            results[i + k] = part
            if target_lang == "tai-lo" and not _has_sufficient_hanzi(part):
                logger.warning(
                    f"Segment {i+k} in batch {batch_num + 1} has insufficient Han characters "
                    f"(tai-lo target) — prompt may need strengthening"
                )

        for k in range(len(parts), actual_size):
            results[i + k] = ""
            logger.warning(f"Missing translation for segment {i+k}")

        batch_num += 1
        logger.info(f"Translated batch {batch_num}: segments {i}–{i+actual_size-1} ({len(segments)} total)")
        i += actual_size

    return results


# ── 讀取支援材料 ───────────────────────────────────────────────────────────────
GEMINI_SUPPORTED_MIMES = {
    "application/pdf", "text/plain", "text/html", "text/csv", "text/xml",
    "image/png", "image/jpeg", "image/webp", "image/heic", "image/heif",
}

DOCX_MIMES = {
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
}


def _convert_docx_to_html(raw: bytes) -> str | None:
    """Convert a .docx file to HTML using mammoth."""
    try:
        import mammoth
        result = mammoth.convert_to_html(io.BytesIO(raw))
        html = result.value
        if not html.strip():
            logger.warning("mammoth returned empty HTML, falling back to text extraction")
            return None
        return f"<!DOCTYPE html><html><meta charset=\"utf-8\"><body>{html}</body></html>"
    except Exception as e:
        logger.warning(f"mammoth conversion failed: {e}")
        return None


def _strip_html(html_text: str) -> str:
    """Strip HTML tags, returning plain text."""
    from html.parser import HTMLParser
    class _Stripper(HTMLParser):
        def __init__(self):
            super().__init__()
            self._text = []
        def handle_data(self, d):
            if d.strip():
                self._text.append(d)
    stripper = _Stripper()
    stripper.feed(html_text)
    return "\n".join(stripper._text)


def load_support_context(order_id: str) -> str | None:
    """Upload support materials to a Gemini File Search Store and return its name.

    File Search (RAG) replaces raw File API attachment — files are
    auto-chunked + indexed, and Gemini retrieves relevant chunks per request.
    This means every translation batch gets reference context without
    consuming the full file token budget.

    Returns the store name (str) or None if no support files found.
    """
    try:
        from shared.storage import get_client
        from shared.config import cfg as gcfg

        client = get_client()
        bucket = client.bucket(gcfg.BUCKET_UPLOADS)
        prefix = f"orders/{order_id}/support/"

        blobs = list(bucket.list_blobs(prefix=prefix))
        if not blobs:
            return None

        store_name = create_file_search_store(order_id)
        uploaded_count = 0

        for blob in blobs:
            if blob.name.endswith("/"):
                continue
            try:
                raw = blob.download_as_bytes()
                filename = blob.name.split("/")[-1]
                mime_type = blob.content_type or "text/plain"

                if mime_type in GEMINI_SUPPORTED_MIMES:
                    if mime_type == "text/html":
                        text = _strip_html(raw.decode("utf-8-sig", errors="replace"))
                        txt_name = Path(filename).stem + ".txt"
                        upload_to_file_search_store(
                            store_name, data=text.encode("utf-8"),
                            display_name=txt_name, mime_type="text/plain",
                        )
                    else:
                        upload_to_file_search_store(
                            store_name, data=raw,
                            display_name=filename, mime_type=mime_type,
                        )
                elif mime_type in DOCX_MIMES:
                    html = _convert_docx_to_html(raw)
                    if html:
                        text = _strip_html(html)
                    else:
                        text = normalize_text(raw)
                    txt_name = Path(filename).stem + ".txt"
                    upload_to_file_search_store(
                        store_name, data=text.encode("utf-8"),
                        display_name=txt_name, mime_type="text/plain",
                    )
                else:
                    text = normalize_text(raw)
                    txt_name = Path(filename).stem + ".txt"
                    upload_to_file_search_store(
                        store_name, data=text.encode("utf-8"),
                        display_name=txt_name, mime_type="text/plain",
                    )

                uploaded_count += 1
                logger.info(f"Uploaded support file to File Search Store: {filename}")
            except Exception as e:
                logger.warning(f"Failed to upload support file {blob.name} to File Search Store: {e}")

        if uploaded_count == 0:
            logger.info("No support files were uploaded; deleting empty store")
            delete_file_search_store(store_name)
            return None

        logger.info(f"File Search Store ready: {uploaded_count} file(s) indexed")
        return store_name
    except Exception as e:
        logger.warning(f"Support context loading failed (non-critical): {e}")
        return None


# ── Main ──────────────────────────────────────────────────────────────────────
def run():
    logger.info(f"=== lt_preprocess_nmt START — order: {cfg.ORDER_ID} ===")
    update_job_status("lt_preprocess_nmt", "running")
    update_order_field("status", "processing")

    try:
        # ── 1. 取得訂單資訊 ──────────────────────────────────────────────────
        order = get_order_info()
        source_lang = order["source_lang"]
        target_lang = order["target_lang"]
        gcs_path    = order.get("gcs_upload_path", "")

        if not gcs_path:
            raise ValueError(f"No upload file found for order {cfg.ORDER_ID}")

        logger.info(f"Reading upload: {gcs_path}")

        # ── 2. 讀取並正規化原文 ──────────────────────────────────────────────
        raw_bytes = read_upload(gcs_path)
        text      = normalize_text(raw_bytes)
        logger.info(f"Source text length: {len(text)} chars")

        # ── 3. 段落分割（LT 使用較大的 segment）──────────────────────────────
        paragraphs = split_paragraphs(text)
        logger.info(f"Split into {len(paragraphs)} segments (LT large-segment mode)")

        # ── 4. 載入支援材料（File Search Store）─────────────────────────────
        store_name = load_support_context(cfg.ORDER_ID)
        if store_name:
            logger.info(f"File Search Store created: {store_name}")
        else:
            logger.info("No support materials found")

        # ── 5. NMT 翻譯（文學風格 prompt + File Search RAG）─────────────────
        translations = translate_batch(
            segments        = [{"index": i, "text": p} for i, p in enumerate(paragraphs)],
            prompt_template = LT_PROMPT,
            source_lang     = source_lang,
            target_lang     = target_lang,
            batch_size      = 3,  # smaller batches for literary quality
            store_name      = store_name,
        )

        # ── 6. 清理 File Search Store ───────────────────────────────────────
        if store_name:
            delete_file_search_store(store_name)

        # ── 7. 寫入中間產物 ──────────────────────────────────────────────────
        segments_out = [
            {"index": i, "text": para, "char_count": len(para)}
            for i, para in enumerate(paragraphs)
        ]

        translations_out = [
            {
                "index":      i,
                "source":     src,
                "translated": tgt,
                "comments":   "",
            }
            for i, (src, tgt) in enumerate(zip(paragraphs, translations))
        ]

        metadata = {
            "order_id":       cfg.ORDER_ID,
            "source_lang":    source_lang,
            "target_lang":    target_lang,
            "total_chars":    len(text),
            "para_count":     len(paragraphs),
            "gcs_upload":     gcs_path,
            "track_type":     "literary",
            "has_support":    bool(store_name),
        }

        write_temp_json("segments.json",       segments_out)
        write_temp_json("translations.json",    translations_out)
        write_temp_json("translations_raw.json", translations_out)
        write_temp_json("metadata.json",        metadata)

        update_job_status("lt_preprocess_nmt", "success")
        logger.info(f"=== lt_preprocess_nmt DONE — {len(paragraphs)} segments, {len(text)} chars ===")

    except Exception as e:
        logger.exception(f"lt_preprocess_nmt FAILED: {e}")
        update_job_status("lt_preprocess_nmt", "failed", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
