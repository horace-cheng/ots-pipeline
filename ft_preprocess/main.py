"""
ft_preprocess/main.py — Cloud Run Job

Fast Track Step 1: Pre-process
- 從 GCS 讀取客戶上傳的原始文本
- UTF-8 正規化
- 台羅拼音 / 漢字雙軌識別
- 段落分割
- 初始化 Firestore 術語詞庫
- 寫入中間產物到 GCS temp
- 更新 pipeline_jobs 狀態
"""

import sys, re, unicodedata, logging, zipfile, io
from pathlib import Path
from xml.etree import ElementTree

# 將 shared 目錄加入 Python path
sys.path.insert(0, str(Path(__file__).parent.parent))

from shared.config  import cfg
from shared.db      import update_job_status, get_order_info, update_order_field
from shared.storage import read_upload, write_temp_json
from shared.terminology import init_terms

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("ft_preprocess")


# ── 台羅拼音偵測（常見聲調符號與拼音字母組合）────────────────────────────────
TAILO_PATTERN = re.compile(
    r"[a-zA-Z]+"                      # 基本拼音字母
    r"(?:[aeiouAEIOU\u0100-\u017E]+)"  # 含變音符號的母音
    r"(?:\d+|[a-zA-Z]*)",             # 聲調數字（台羅）或結尾子音
)

def detect_script(text: str) -> str:
    """偵測文本主要書寫系統"""
    han_count   = sum(1 for c in text if unicodedata.category(c) == "Lo")
    latin_count = sum(1 for c in text if c.isalpha() and ord(c) < 128)
    tailo_matches = len(TAILO_PATTERN.findall(text))

    if tailo_matches > 5 and latin_count > han_count:
        return "tailo"       # 台羅拼音為主
    elif han_count > latin_count:
        return "han"         # 漢字（台文漢字）為主
    else:
        return "mixed"       # 混合


def _extract_text_from_docx(raw: bytes) -> str:
    """Extract text from a .docx file (ZIP archive of XMLs)."""
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            doc_xml = zf.read("word/document.xml")
            root = ElementTree.fromstring(doc_xml)
    except (zipfile.BadZipFile, KeyError) as e:
        logger.warning(f"Not a valid .docx file: {e}")
        return None

    # Extract text from all <w:t> elements, join paragraphs by <w:p> boundaries
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
    # Don't forget last paragraph
    text = "".join(current_text)
    if text.strip():
        paragraphs.append(text.strip())

    return "\n\n".join(paragraphs)


def normalize_text(raw: bytes) -> str:
    """UTF-8 正規化，支援 .docx 文件提取文字。"""
    # 嘗試作為 .docx 提取文字（ZIP 檔案以 PK\x03\x04 開頭）
    if raw[:4] == b"PK\x03\x04":
        docx_text = _extract_text_from_docx(raw)
        if docx_text is not None:
            logger.info("Detected .docx file, extracted text via ZIP/XML")
            text = docx_text
        else:
            logger.info("Not a valid .docx, falling back to plain text decode")
            try:
                text = raw.decode("utf-8-sig")
            except UnicodeDecodeError:
                text = raw.decode("big5", errors="replace")
    else:
        try:
            text = raw.decode("utf-8-sig")  # 處理 BOM
        except UnicodeDecodeError:
            text = raw.decode("big5", errors="replace")

    # 正規化 Unicode（NFC）
    text = unicodedata.normalize("NFC", text)
    # 統一換行符
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # 移除零寬字元
    text = re.sub(r"[\u200b\u200c\u200d\ufeff]", "", text)
    return text


def split_paragraphs(text: str) -> list[str]:
    """
    段落分割策略：
    1. 空行分割（主要）
    2. 若結果 < 3 段，改用句號/段落標記分割
    3. 移除空段落，長度不足 10 字的段落合併到前一段
    """
    # 嘗試空行分割
    paragraphs = [p.strip() for p in re.split(r"\n{2,}", text)]
    paragraphs = [p for p in paragraphs if p]

    if len(paragraphs) < 3:
        # 用句號 + 換行分割
        paragraphs = [p.strip() for p in re.split(r"[。\n]", text)]
        paragraphs = [p for p in paragraphs if p and len(p) >= 10]

    # 合併過短段落到前一段
    merged = []
    for para in paragraphs:
        if merged and len(para) < 15:
            merged[-1] += " " + para
        else:
            merged.append(para)

    return merged


def run():
    logger.info(f"=== ft_preprocess START — order: {cfg.ORDER_ID} ===")
    update_job_status("preprocess", "running")
    update_order_field("status", "processing")

    try:
        # ── 1. 取得訂單資訊 ──────────────────────────────────────────────
        order = get_order_info()
        source_lang = order["source_lang"]
        target_lang = order["target_lang"]
        gcs_path    = order.get("gcs_upload_path", "")

        if not gcs_path:
            raise ValueError(f"No upload file found for order {cfg.ORDER_ID}")

        logger.info(f"Reading upload: {gcs_path}")

        # ── 2. 讀取並正規化 ──────────────────────────────────────────────
        raw_bytes = read_upload(gcs_path)
        text      = normalize_text(raw_bytes)
        script    = detect_script(text)

        logger.info(f"Script detected: {script}, text length: {len(text)} chars")

        # ── 3. 段落分割 ──────────────────────────────────────────────────
        paragraphs = split_paragraphs(text)
        logger.info(f"Split into {len(paragraphs)} paragraphs")

        # ── 4. 初始化術語詞庫 ────────────────────────────────────────────
        term_dict_id = init_terms(cfg.ORDER_ID, source_lang, target_lang)
        update_order_field("term_dict_id", term_dict_id)

        # ── 5. 寫入中間產物 ──────────────────────────────────────────────
        segments = [
            {"index": i, "text": para, "char_count": len(para)}
            for i, para in enumerate(paragraphs)
        ]

        metadata = {
            "order_id":    cfg.ORDER_ID,
            "source_lang": source_lang,
            "target_lang": target_lang,
            "script":      script,
            "total_chars": len(text),
            "para_count":  len(paragraphs),
            "gcs_upload":  gcs_path,
        }

        write_temp_json("segments.json",  segments)
        write_temp_json("metadata.json",  metadata)

        # ── 6. 更新狀態 ──────────────────────────────────────────────────
        update_job_status("preprocess", "success")
        logger.info(f"=== ft_preprocess DONE — {len(paragraphs)} segments ===")

    except Exception as e:
        logger.exception(f"ft_preprocess FAILED: {e}")
        update_job_status("preprocess", "failed", error_message=str(e))
        sys.exit(1)


if __name__ == "__main__":
    run()
