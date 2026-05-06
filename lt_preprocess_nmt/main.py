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
from shared.db      import update_job_status, get_order_info, update_order_field, get_db
from shared.storage import read_upload, read_temp_json, write_temp_json, list_support_files
from shared.gemini  import translate

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
LT_PROMPT_EN = """You are a professional literary translator specializing in {source_lang_label} to English translation.

Translate the following text with careful attention to literary style, tone, rhythm, and emotional nuance.
This is a literary work — preserve the author's voice, imagery, and artistic intent.

Rules:
1. Preserve paragraph structure exactly — do not merge or split paragraphs
2. Translate ALL content faithfully. Maintain cultural references with brief contextual hints only when essential for comprehension
3. Preserve proper nouns, place names, and cultural terms with appropriate romanization
4. Maintain the original tone (formal, colloquial, poetic, etc.)
5. Literary devices (metaphor, alliteration, rhythm) should be preserved where possible
6. Output only the translation, no explanations or preamble
{context_injection}

Source text:
{source_text}

English translation:"""

LT_PROMPT_JA = """あなたは{text source_lang_label}から日本語への文学翻訳の専門家です。

文学的な文体、トーン、リズム、感情のニュアンスに細心の注意を払って翻訳してください。
これは文学作品です — 作者の声、イメージ、芸術的意図を保持してください。

規則：
1. 段落構造を完全に保持（段落の統合・分割は不可）
2. すべてのコンテンツを忠実に翻訳。文化的参照は理解に必要な場合のみ簡潔な文脈ヒントを添える
3. 固有名詞、地名、文化用語は適切なローマ字表記を保持
4. 原文のトーン（形式的、口語的、詩的など）を維持
5. 文学的修辞（隠喩、頭韻法、リズム）は可能な限り保持
6. 翻訳結果のみを出力、説明や前置きは不要
{context_injection}

原文：
{source_text}

日本語翻訳："""

LT_PROMPT_KO = """당신은 {source_lang_label}에서 한국어로의 문학 번역 전문가입니다.

문학적 스타일, 어조, 리듬, 감정적 뉘앙스에 세심한 주의를 기울여 번역하세요.
이것은 문학 작품입니다 — 작가의 목소리, 이미지, 예술적 의도를 보존하세요.

규칙:
1. 단락 구조를 완전히 유지（단락 병합 또는 분리 금지）
2. 모든 콘텐츠를 충실히 번역. 문화적 참조는 이해에 필수적인 경우에만 간단한 문맥 힌트 추가
3. 고유명사, 지명, 문화 용어는 적절한 로마자 표기 유지
4. 원문의 어조（격식체, 구어체, 시적 등）유지
5. 문학적 장치（은유, 두운, 리듬）는 가능한 한 유지
6. 번역 결과만 출력, 설명이나 서문 불필요
{context_injection}

원문:
{source_text}

한국어 번역:"""

LT_PROMPTS = {
    "en": LT_PROMPT_EN,
    "ja": LT_PROMPT_JA,
    "ko": LT_PROMPT_KO,
}

LANG_LABELS = {
    "tai-lo":     "Taiwanese Hokkien",
    "hakka":      "Hakka",
    "indigenous": "Taiwanese Indigenous",
    "zh-tw":      "Traditional Chinese",
    "en":         "English",
    "ja":         "Japanese",
    "ko":         "Korean",
}


# ── 分批翻譯（大型檔案支援）────────────────────────────────────────────────────
def translate_batch(
    segments: list[dict],
    prompt_template: str,
    context_inj: str,
    source_lang: str,
    batch_size: int = 3,
) -> list[str]:
    """Translate segments in batches. LT uses smaller batches for higher quality."""
    results = [""] * len(segments)

    PREAMBLE_RE = re.compile(
        r'^(here are|following|below|sure|certainly|of course|'
        r'here is|here\'s|below is|the following|'
        r'以下是|好的|當然|翻譯如下)',
        re.IGNORECASE
    )

    for i in range(0, len(segments), batch_size):
        batch = segments[i:i + batch_size]

        combined = "\n\n[PARA_SEP]\n\n".join(
            f"[{j+1}] {seg['text']}" for j, seg in enumerate(batch)
        )

        prompt = prompt_template.format(
            source_text      = combined,
            context_injection = context_inj,
            source_lang_label = LANG_LABELS.get(source_lang, source_lang),
        )

        response = translate(prompt, max_tokens=16384)

        # Parse numbered responses
        numbered_parts = re.findall(r"\[(\d+)\]\s*(.*?)(?=\[\d+\]|$)", response, re.DOTALL)
        if numbered_parts and len(numbered_parts) == len(batch):
            parts = [p.strip() for _, p in numbered_parts]
        else:
            first_marker = re.search(r"\n?\[1\]", response)
            clean_response = response[first_marker.end():] if first_marker else response
            parts = re.split(r"\[PARA_SEP\]|\[\d+\]", clean_response)
            parts = [p.strip() for p in parts if p.strip()]

        # Handle part count mismatch
        if len(parts) > len(batch):
            if len(parts[0]) < len(parts[1]) * 0.3 and not parts[0].rstrip().endswith("."):
                parts[0] = parts[0] + " " + parts[1]
                parts.pop(1)
            while len(parts) > len(batch):
                parts[-2] = parts[-2] + " " + parts.pop(-1)

        elif len(parts) < len(batch):
            first_marker = re.search(r"\n?\[1\]", response)
            clean_response = response[first_marker.end():] if first_marker else response
            fallback = [p.strip() for p in re.split(r"\n{2,}", clean_response) if p.strip()]
            while fallback and PREAMBLE_RE.match(fallback[0]):
                fallback.pop(0)
            if len(fallback) > len(batch):
                if len(fallback[0]) < len(fallback[1]) * 0.3 and not fallback[0].rstrip().endswith("."):
                    fallback[0] = fallback[0] + " " + fallback[1]
                    fallback.pop(1)
                while len(fallback) > len(batch):
                    fallback[-2] = fallback[-2] + " " + fallback.pop(-1)
            if len(fallback) >= len(batch):
                parts = fallback[:len(batch)]

        for k, part in enumerate(parts[:len(batch)]):
            results[i + k] = part

        for k in range(len(parts), len(batch)):
            results[i + k] = ""
            logger.warning(f"Missing translation for segment {i+k}")

        logger.info(f"Translated batch {i//batch_size + 1}: segments {i}–{min(i+len(batch)-1, len(segments)-1)} ({len(segments)} total)")

    return results


# ── 讀取支援材料 ───────────────────────────────────────────────────────────────
def load_support_context(order_id: str) -> str:
    """Read support materials and return as context string for translation prompt."""
    try:
        from shared.storage import get_client
        from shared.config import cfg as gcfg
        from google.cloud import storage

        client = get_client()
        bucket = client.bucket(gcfg.BUCKET_UPLOADS)
        prefix = f"support/{order_id}/"

        blobs = list(bucket.list_blobs(prefix=prefix))
        if not blobs:
            return ""

        context_parts = []
        for blob in blobs:
            if blob.name.endswith("/"):
                continue
            try:
                text = blob.download_as_text(encoding="utf-8")
                filename = blob.name.split("/")[-1]
                context_parts.append(f"--- Reference: {filename} ---\n{text[:3000]}")
            except Exception as e:
                logger.warning(f"Failed to read support file {blob.name}: {e}")

        if not context_parts:
            return ""

        return "\n\n".join(context_parts)
    except Exception as e:
        logger.warning(f"Support context loading failed (non-critical): {e}")
        return ""


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

        # ── 4. 載入支援材料 ──────────────────────────────────────────────────
        context = load_support_context(cfg.ORDER_ID)
        if context:
            logger.info(f"Loaded support materials ({len(context)} chars)")
        else:
            logger.info("No support materials found")

        # ── 5. NMT 翻譯（文學風格 prompt）────────────────────────────────────
        if target_lang not in LT_PROMPTS:
            raise ValueError(f"Unsupported target language: {target_lang}")

        translations = translate_batch(
            segments        = [{"index": i, "text": p} for i, p in enumerate(paragraphs)],
            prompt_template = LT_PROMPTS[target_lang],
            context_inj     = context,
            source_lang     = source_lang,
            batch_size      = 3,  # smaller batches for literary quality
        )

        # ── 6. 寫入中間產物 ──────────────────────────────────────────────────
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
            "has_support":    bool(context),
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
