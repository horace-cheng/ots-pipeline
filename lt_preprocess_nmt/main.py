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

import sys, re, unicodedata, logging, zipfile, io, time
from pathlib import Path
from xml.etree import ElementTree

sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config  import cfg
from shared.db      import update_job_status, get_order_info, update_order_field, get_lang_labels
from shared.storage import (
    read_upload, write_temp_json,
    save_preprocess_artifacts, load_preprocess_artifacts,
    save_batch_checkpoint, load_batch_checkpoint,
    aggregate_checkpoints,
)
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

Translate the following text. It contains exactly {num_segments} segments, each marked with === SEGMENT N ===.

Translate each segment in order. After EACH translation, output <<<TRANSLATION_END>>> on its own line.

For example:
Translation of the first segment here. It can span multiple lines.
<<<TRANSLATION_END>>>
Translation of the second segment here.
<<<TRANSLATION_END>>>

Rules:
1. Preserve paragraph structure exactly — do not merge or split paragraphs
2. Translate ALL content faithfully. Maintain cultural references with brief contextual hints only when essential for comprehension
3. Preserve proper nouns, place names, and cultural terms with appropriate romanization
4. Maintain the original tone (formal, colloquial, poetic, etc.)
5. Literary devices (metaphor, alliteration, rhythm) should be preserved where possible
6. Output ONLY the translations with <<<TRANSLATION_END>>> markers — no explanations, no preamble, no segment numbers
7. Reference materials (glossaries, style guides, background documents) are attached for translation context — use them to inform terminology and style
{hanzi_instruction}
Source text:
{source_text}

{target_lang} translations:"""

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


# ── 批次建構 ───────────────────────────────────────────────────────────────────
def build_batches(
    segments: list[dict],
    max_batch_words: int = 2000,
    batch_size: int = 15,
) -> list[dict]:
    """Build deterministic batch boundaries from segments.

    Batches are formed by word count (max_batch_words) capped by max segment
    count (batch_size), whichever is hit first — short segments pack densely,
    long segments get their own batch.

    **batch_size=15** is the empirically verified maximum the Gemini model
    can reliably translate in a single API call. Batch sizes >15 produce
    truncated output (finish_reason=2) with <50% completion.

    Returns list of dicts:
        [{"batch_id": 0, "start": 0, "count": 5}, ...]
    """
    batches: list[dict] = []
    current_segs: list[dict] = []
    current_words = 0
    next_start = 0
    batch_id = 0

    for i, seg in enumerate(segments):
        seg_words = len(seg['text'].split())
        if current_segs and (current_words + seg_words > max_batch_words or len(current_segs) >= batch_size):
            batches.append({
                "batch_id": batch_id,
                "start":    next_start,
                "count":    len(current_segs),
            })
            batch_id += 1
            current_segs = []
            current_words = 0
            next_start = i
        current_segs.append(seg)
        current_words += seg_words

    if current_segs:
        batches.append({
            "batch_id": batch_id,
            "start":    next_start,
            "count":    len(current_segs),
        })

    return batches


# ── 分批翻譯（順序執行 + 每批獨立 Checkpoint）─────────────────────────────
def translate_batch(
    segments: list[dict],
    prompt_template: str,
    source_lang: str,
    target_lang: str,
    batches: list[dict],
    store_name: str | None = None,
) -> None:
    """Translate all batches sequentially with per-batch checkpointing.

    ``batches`` is never mutated — iteration is a simple ``for`` loop.
    If ``checkpoint_batch_{id}.json`` exists with matching ``count``/``start``
    the batch is skipped (resume).  Stale checkpoints are ignored.

    Each failed batch is retried up to 4 times.  If all attempts fail no
    checkpoint is written — ``aggregate_checkpoints`` will raise.
    """
    total = len(segments)
    hanzi_instr = _get_hanzi_instruction(target_lang)

    logger.info(
        f"translate_batch: {len(batches)} total batches, "
        f"first 3: {[(b['batch_id'], b['start'], b['count']) for b in batches[:3]]}, "
        f"last 3: {[(b['batch_id'], b['start'], b['count']) for b in batches[-3:]]}"
    )

    for batch in batches:
        batch_id = batch["batch_id"]
        start   = batch["start"]
        count   = batch["count"]

        # ── Skip if valid checkpoint already exists ──
        existing = load_batch_checkpoint(batch_id)
        if existing is not None:
            ckpt_count = existing.get("count")
            ckpt_start = existing.get("start")
            if ckpt_count == count and ckpt_start == start:
                done = sum(1 for t in existing.get("translations", []) if t)
                logger.info(
                    f"Batch {batch_id}: checkpoint exists "
                    f"({done}/{count} segments) — skipping"
                )
                continue
            logger.warning(
                f"Batch {batch_id}: stale checkpoint ignored "
                f"(expected {count} segs @ {start}, "
                f"got {ckpt_count} segs @ {ckpt_start})"
            )

        logger.info(f"Batch {batch_id}: translating segments {start}–{start + count - 1} ({total} total)")

        batch_segs = list(segments[start:start + count])
        result_parts: list[str] = [""] * count
        pending_indices: list[int] = list(range(count))
        success = False

        for attempt in range(4):
            if not pending_indices:
                success = True
                break

            if attempt > 0:
                logger.info(
                    f"Retry {attempt}/3 for batch {batch_id}: "
                    f"{len(pending_indices)} segments remain"
                )
                time.sleep(5 * attempt)

            current_slice = [batch_segs[i] for i in pending_indices]

            try:
                # ── Build prompt ──
                parts = []
                for j, seg in enumerate(current_slice):
                    parts.append(f"=== SEGMENT {j+1} ===\n{seg['text']}")
                combined = "\n\n".join(parts)

                prompt = prompt_template.format(
                    source_text       = combined,
                    source_lang_label = LANG_LABELS.get(source_lang, source_lang),
                    target_lang       = LANG_LABELS.get(target_lang, target_lang),
                    hanzi_instruction = hanzi_instr,
                    num_segments      = len(current_slice),
                )

                response = translate(
                    prompt, max_tokens=16384, store_name=store_name,
                    job_type="lt_preprocess_nmt",
                )

                # ── Parse delimiter-separated response ──
                parsed = re.split(r'<<<TRAN(?:SLATION)?_END?>>>', response)
                parsed = [p.strip() for p in parsed]
                if parsed and not parsed[0]:
                    parsed.pop(0)
                if parsed and not parsed[-1]:
                    parsed.pop(-1)

                newly_done: list[int] = []
                for j, p in enumerate(parsed):
                    if j < len(pending_indices) and p:
                        original_idx = pending_indices[j]
                        result_parts[original_idx] = p
                        newly_done.append(original_idx)

                # ── tai-lo Hanzi check ──
                if target_lang == "tai-lo":
                    for seg_idx in list(newly_done):
                        if not _has_sufficient_hanzi(result_parts[seg_idx]):
                            logger.warning(
                                f"Segment {start + seg_idx} has insufficient Han "
                                f"characters (tai-lo target), retrying"
                            )
                            result_parts[seg_idx] = ""
                            newly_done.remove(seg_idx)

                pending_indices = [i for i in pending_indices if i not in newly_done]

                non_empty = sum(1 for p in result_parts if p)
                if pending_indices:
                    if attempt < 3:
                        logger.info(
                            f"Batch {batch_id}: attempt {attempt+1}/4 — "
                            f"{non_empty}/{count} segments, "
                            f"{len(pending_indices)} pending"
                        )
                    else:
                        logger.warning(
                            f"Batch {batch_id}: final attempt — "
                            f"{non_empty}/{count} segments, proceeding "
                            f"with {len(pending_indices)} gaps"
                        )
                        success = True
                else:
                    success = True

            except Exception as e:
                logger.warning(f"Attempt {attempt + 1}/4 for batch {batch_id} failed: {e}")
                if attempt < 3:
                    continue
                logger.error(f"Batch {batch_id} permanently failed after 4 attempts")

        # ── Save checkpoint (or skip if permanently failed) ──
        if success and result_parts is not None:
            ckpt_data = {
                "batch_id":     batch_id,
                "start":        start,
                "count":        len(result_parts),
                "translations": result_parts,
            }
            save_batch_checkpoint(batch_id, ckpt_data)
            done_after = sum(1 for t in result_parts if t)
            logger.info(
                f"Batch {batch_id}: saved checkpoint — "
                f"{done_after}/{len(result_parts)} segments, "
                f"segments {start}–{start + count - 1}"
            )
        else:
            logger.error(
                f"Batch {batch_id} permanently failed — no checkpoint written. "
                f"Segments {start}–{start + count - 1} will be blank."
            )


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
        order = get_order_info()
        source_lang = order["source_lang"]
        target_lang = order["target_lang"]
        gcs_path    = order.get("gcs_upload_path", "")

        if not gcs_path:
            raise ValueError(f"No upload file found for order {cfg.ORDER_ID}")

        # ── Phase 1: Preprocess (load or create) ───────────────────────────
        artifacts = load_preprocess_artifacts()
        if artifacts is not None:
            segments, batches, metadata = artifacts
            paragraphs = [s["text"] for s in segments]
            logger.info(
                f"Loaded preprocess artifacts: {len(segments)} segments, "
                f"{len(batches)} batches"
            )
        else:
            logger.info(f"Reading upload: {gcs_path}")

            raw_bytes  = read_upload(gcs_path)
            text       = normalize_text(raw_bytes)
            logger.info(f"Source text length: {len(text)} chars")

            paragraphs = split_paragraphs(text)
            logger.info(f"Split into {len(paragraphs)} segments (LT large-segment mode)")

            segments = [
                {"index": i, "text": p, "char_count": len(p)}
                for i, p in enumerate(paragraphs)
            ]
            batches = build_batches(segments)

            metadata = {
                "order_id":       cfg.ORDER_ID,
                "source_lang":    source_lang,
                "target_lang":    target_lang,
                "total_chars":    len(text),
                "para_count":     len(paragraphs),
                "gcs_upload":     gcs_path,
                "track_type":     "literary",
            }

            save_preprocess_artifacts(segments, batches, metadata)
            metadata["has_support"] = False  # placeholder, updated after Phase 1

        # ── Phase 2: Support materials ─────────────────────────────────────
        store_name = load_support_context(cfg.ORDER_ID)
        if store_name:
            logger.info(f"File Search Store created: {store_name}")
        else:
            logger.info("No support materials found")
        metadata["has_support"] = bool(store_name)

        # ── Phase 3: Sequential per-batch NMT ──────────────────────────────
        seg_dicts = [{"index": i, "text": p} for i, p in enumerate(paragraphs)]

        translate_batch(
            segments        = seg_dicts,
            prompt_template = LT_PROMPT,
            source_lang     = source_lang,
            target_lang     = target_lang,
            batches         = batches,
            store_name      = store_name,
        )

        # ── Phase 4: Aggregate per-batch checkpoints ──────────────────────
        translations = aggregate_checkpoints(batches, len(paragraphs))

        # ── Phase 5: Cleanup File Search Store ────────────────────────────
        if store_name:
            delete_file_search_store(store_name)

        # ── Phase 6: Write output artifacts ───────────────────────────────
        translations_out = [
            {
                "index":      i,
                "source":     src,
                "translated": tgt,
                "comments":   "",
            }
            for i, (src, tgt) in enumerate(zip(paragraphs, translations))
        ]

        write_temp_json("translations.json",      translations_out)
        write_temp_json("translations_raw.json",   translations_out)
        write_temp_json("metadata.json",           metadata)

        update_job_status("lt_preprocess_nmt", "success")
        logger.info(
            f"=== lt_preprocess_nmt DONE — {len(paragraphs)} segments, "
            f"{metadata['total_chars']} chars ==="
        )

    except Exception as e:
        logger.exception(f"lt_preprocess_nmt FAILED: {e}")
        update_job_status("lt_preprocess_nmt", "failed", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
