"""
shared/gemini.py

Gemini API 呼叫工具（via Google AI Developer API / genai SDK）。
比 Vertex AI SDK 更簡單，model 可用性更廣（gemini-2.5-pro/flash 直接可用）。
Claude API 作為備援，切換由 TRANSLATION_BACKEND 環境變數控制。
"""

import os
import time
import random
import logging
import io
import tempfile
from pathlib import Path
from shared.config import cfg

logger = logging.getLogger(__name__)

BACKEND = os.environ.get("TRANSLATION_BACKEND", "gemini")  # gemini | claude

_genai_client = None
_new_genai_client = None


def _get_genai_client():
    """初始化舊版 Google AI genai client（google.generativeai，單例）"""
    global _genai_client
    if _genai_client is not None:
        return _genai_client

    import google.generativeai as genai

    api_key = os.environ.get("GOOGLE_AI_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_AI_API_KEY environment variable is required")

    genai.configure(api_key=api_key)
    _genai_client = genai
    return _genai_client


def _get_new_genai_client():
    """初始化新版 Google AI genai client（google.genai，單例，用於 File Search）"""
    global _new_genai_client
    if _new_genai_client is not None:
        return _new_genai_client

    from google import genai as genai_new

    api_key = os.environ.get("GOOGLE_AI_API_KEY", "")
    if not api_key:
        raise ValueError("GOOGLE_AI_API_KEY environment variable is required")

    _new_genai_client = genai_new.Client(api_key=api_key)
    return _new_genai_client


def upload_file_to_gemini(data: bytes, display_name: str, mime_type: str):
    """Upload a file to Gemini File API and wait for processing. Returns the File object."""
    genai = _get_genai_client()
    ext = Path(display_name).suffix or ".bin"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        uploaded = genai.upload_file(
            path=tmp_path,
            display_name=display_name,
            mime_type=mime_type,
        )
        while uploaded.state.name == "PROCESSING":
            time.sleep(1)
            uploaded = genai.get_file(uploaded.name)
        if uploaded.state.name == "FAILED":
            raise RuntimeError(f"Gemini file processing failed: {uploaded.state.name}")
        logger.info(f"Gemini file ready: {uploaded.uri} ({uploaded.display_name})")
        return uploaded
    finally:
        os.unlink(tmp_path)


def delete_gemini_file(file_obj):
    """Delete a previously uploaded Gemini file."""
    try:
        genai = _get_genai_client()
        genai.delete_file(file_obj.name)
        logger.info(f"Deleted Gemini file: {file_obj.uri}")
    except Exception as e:
        logger.warning(f"Failed to delete Gemini file {getattr(file_obj, 'name', '?')}: {e}")


def call_gemini(
    prompt: str,
    model: str | None = None,
    max_tokens: int = 8192,
    files: list | None = None,
    response_mime_type: str | None = None,
) -> tuple[str, dict]:
    """
    呼叫 Google AI Gemini（genai SDK）。
    支援傳入 File 物件（genai.upload_file 回傳值）作為附加上下文。

    使用 count_tokens API 在送出前檢查是否超過模型 context window。
    若超過則立即 raise ValueError("TOKEN_LIMIT: ...") 讓呼叫端縮減輸入。

    response_mime_type: "application/json" 啟用 JSON 模式（API 強制輸出合法 JSON）

    Returns (text, usage_dict) where usage_dict has prompt_tokens,
    candidates_tokens, total_tokens keys.
    """
    genai = _get_genai_client()
    model_name = model or cfg.GEMINI_PRO_MODEL

    generation_config = {
        "max_output_tokens": max_tokens,
        "temperature": 0.1,
    }
    if response_mime_type:
        generation_config["response_mime_type"] = response_mime_type

    # ── 預先計算 token 數量 ──
    m = genai.GenerativeModel(model_name)
    contents = []
    if files:
        contents.extend(files)
    contents.append(prompt)
    try:
        token_count = m.count_tokens(contents)
        if token_count.total_tokens > 950_000:
            raise ValueError(
                f"TOKEN_LIMIT: input {token_count.total_tokens} tokens exceeds safe limit "
                f"(model max ~1,048,576, leaving room for output)"
            )
    except ValueError:
        raise
    except Exception as e:
        logger.warning(f"count_tokens failed (non-fatal): {e}")

    for attempt in range(6):
        try:
            resp = m.generate_content(contents, generation_config=generation_config)
            usage = getattr(resp, 'usage_metadata', None)
            _usage = {
                "prompt_tokens":     usage.prompt_token_count     if usage else 0,
                "candidates_tokens": usage.candidates_token_count if usage else 0,
                "total_tokens":      usage.total_token_count      if usage else 0,
            } if usage else {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}
            return resp.text, _usage
        except Exception as e:
            err_str = str(e)
            if "exceeds the maximum number of tokens" in err_str:
                raise ValueError(f"TOKEN_LIMIT: {err_str}") from e
            wait = 2 ** attempt * 5 + random.uniform(0, 5)
            logger.warning(f"Gemini attempt {attempt+1} failed: {e}. Retrying in {wait:.0f}s")
            if attempt < 5:
                time.sleep(wait)
            else:
                raise


# ── File Search helpers (新版 google.genai SDK) ─────────────────────────────────


def create_file_search_store(order_id: str) -> str:
    """Create a File Search Store for this order. Returns the fully qualified store name."""
    client = _get_new_genai_client()
    from google.genai import types

    display_name = f"ots-order-{order_id}-{cfg.ENV}"
    logger.info(f"Creating File Search Store: {display_name}")

    store = client.file_search_stores.create(
        config={
            "display_name": display_name,
            "embedding_model": "models/gemini-embedding-2",
        }
    )
    logger.info(f"File Search Store created: {store.name}")
    return store.name


def upload_to_file_search_store(store_name: str, data: bytes, display_name: str, mime_type: str = "text/plain"):
    """Upload a support file to the File Search Store."""
    client = _get_new_genai_client()

    ext = Path(display_name).suffix or ".txt"
    with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name

    logger.info(f"Uploading {display_name} ({len(data)} bytes) to File Search Store: {store_name}")
    try:
        op = client.file_search_stores.upload_to_file_search_store(
            file=tmp_path,
            file_search_store_name=store_name,
            config={"display_name": display_name},
        )
        if op.done:
            if getattr(op, "error", None):
                raise RuntimeError(f"Upload failed for {display_name}: {op.error}")
        else:
            while True:
                time.sleep(3)
                op = client.operations.get(op)
                if op.done:
                    if getattr(op, "error", None):
                        raise RuntimeError(f"Upload failed for {display_name}: {op.error}")
                    break
        logger.info(f"File uploaded to File Search Store: {display_name}")
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def call_gemini_with_file_search(
    prompt: str,
    store_name: str,
    model: str | None = None,
    max_tokens: int = 8192,
    response_mime_type: str | None = None,
    extra_files: list | None = None,
) -> tuple[str, dict]:
    """
    使用 File Search（RAG）呼叫 Gemini。
    自動從 File Search Store 檢索相關上下文。
    extra_files: 額外上傳至 Gemini File API 的 File 物件（例如 translation memory），
                 會以 FileData Part 形式附在 contents 前端。
    """
    client = _get_new_genai_client()
    from google.genai import types

    model_name = model or cfg.GEMINI_PRO_MODEL

    # ── Build contents: extra_files first, then prompt ──
    contents: list = []
    if extra_files:
        for f in extra_files:
            file_part = types.Part(
                file_data=types.FileData(
                    file_uri=f.uri,
                    mime_type=f.mime_type,
                )
            )
            contents.append(file_part)
    contents.append(prompt)

    # ── count_tokens pre-flight ──
    try:
        token_count = client.models.count_tokens(
            model=model_name,
            contents=contents,
        )
        if token_count.total_tokens > 950_000:
            raise ValueError(
                f"TOKEN_LIMIT: input {token_count.total_tokens} tokens exceeds safe limit "
                f"(model max ~1,048,576, leaving room for output)"
            )
    except Exception as e:
        logger.warning(f"count_tokens with TM file failed (non-fatal): {e}")

    config = types.GenerateContentConfig(
        max_output_tokens=max_tokens,
        temperature=0.1,
        tools=[
            types.Tool(
                file_search=types.FileSearch(
                    file_search_store_names=[store_name],
                )
            )
        ],
    )

    for attempt in range(6):
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            usage = getattr(response, 'usage_metadata', None)
            _usage = {
                "prompt_tokens":     usage.prompt_token_count     if usage else 0,
                "candidates_tokens": usage.candidates_token_count if usage else 0,
                "total_tokens":      usage.total_token_count      if usage else 0,
            } if usage else {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}
            return response.text, _usage
        except Exception as e:
            err_str = str(e)
            if "TOKEN_LIMIT" in err_str or "exceeds the maximum number of tokens" in err_str:
                raise ValueError(f"TOKEN_LIMIT: {err_str}") from e
            wait = 2 ** attempt * 5 + random.uniform(0, 5)
            logger.warning(f"Gemini w/FileSearch attempt {attempt+1} failed: {e}. Retrying in {wait:.0f}s")
            if attempt < 5:
                time.sleep(wait)
            else:
                raise


def delete_file_search_store(store_name: str):
    """Delete a File Search Store."""
    try:
        client = _get_new_genai_client()
        client.file_search_stores.delete(name=store_name)
        logger.info(f"Deleted File Search Store: {store_name}")
    except Exception as e:
        logger.warning(f"Failed to delete File Search Store {store_name}: {e}")


def call_claude(prompt: str, max_tokens: int = 8192) -> tuple[str, dict]:
    """Claude API 備援（回傳空的 usage dict，不追蹤）"""
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text, {"prompt_tokens": 0, "candidates_tokens": 0, "total_tokens": 0}


def translate(
    prompt: str,
    model: str | None = None,
    max_tokens: int = 8192,
    files: list | None = None,
    store_name: str | None = None,
    job_type: str | None = None,
    response_mime_type: str | None = None,
    extra_files: list | None = None,
) -> str:
    """
    翻譯入口。
    TRANSLATION_BACKEND=gemini（預設）→ Google AI Gemini
    TRANSLATION_BACKEND=claude         → Claude（備援）

    若傳入 store_name，則使用 File Search（RAG）而非 raw File API 附件。
    extra_files 為額外透過 Gemini File API 上傳的 File 物件（如 translation memory），
    會與 File Search RAG 並存。
    若提供 job_type，則自動記錄 token 用量至 DB。
    response_mime_type: "application/json" 啟用 JSON 模式。
    """
    if BACKEND == "claude":
        if files:
            logger.warning("Claude backend does not support file attachments; skipping support files")
        if store_name:
            logger.warning("Claude backend does not support File Search; skipping RAG context")
        logger.info("Using Claude backend (fallback mode)")
        text, _ = call_claude(prompt, max_tokens)
        return text

    if store_name:
        text, usage = call_gemini_with_file_search(
            prompt, store_name, model, max_tokens,
            response_mime_type=response_mime_type,
            extra_files=extra_files,
        )
    else:
        text, usage = call_gemini(prompt, model, max_tokens, files, response_mime_type=response_mime_type)

    if job_type and usage and (usage["prompt_tokens"] or usage["candidates_tokens"]):
        from shared.db import log_token_usage
        log_token_usage(
            job_type=job_type,
            model=model or cfg.GEMINI_PRO_MODEL,
            prompt_tokens=usage["prompt_tokens"],
            candidates_tokens=usage["candidates_tokens"],
            total_tokens=usage["total_tokens"],
        )

    return text


def judge(prompt: str, job_type: str | None = None) -> str:
    """LLM-as-Judge 固定用 Gemini Flash（成本低）"""
    text, usage = call_gemini(prompt, model=cfg.GEMINI_FLASH_MODEL, max_tokens=2048)

    if job_type and usage and (usage["prompt_tokens"] or usage["candidates_tokens"]):
        from shared.db import log_token_usage
        log_token_usage(
            job_type=job_type,
            model=cfg.GEMINI_FLASH_MODEL,
            prompt_tokens=usage["prompt_tokens"],
            candidates_tokens=usage["candidates_tokens"],
            total_tokens=usage["total_tokens"],
        )

    return text
