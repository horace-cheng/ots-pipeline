"""
shared/gemini.py

Gemini API 呼叫工具（via Google AI Developer API / genai SDK）。
比 Vertex AI SDK 更簡單，model 可用性更廣（gemini-2.5-pro/flash 直接可用）。
Claude API 作為備援，切換由 TRANSLATION_BACKEND 環境變數控制。
"""

import os
import time
import logging
from shared.config import cfg

logger = logging.getLogger(__name__)

BACKEND = os.environ.get("TRANSLATION_BACKEND", "gemini")  # gemini | claude

_genai_client = None


def _get_genai_client():
    """初始化 Google AI genai client（單例）"""
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


def call_gemini(prompt: str, model: str | None = None, max_tokens: int = 8192) -> str:
    """
    呼叫 Google AI Gemini（genai SDK）。
    失敗時自動 retry 3 次（指數退避）。
    """
    genai = _get_genai_client()
    model_name = model or cfg.GEMINI_PRO_MODEL

    generation_config = {
        "max_output_tokens": max_tokens,
        "temperature": 0.1,
    }

    for attempt in range(3):
        try:
            m    = genai.GenerativeModel(model_name)
            resp = m.generate_content(
                prompt,
                generation_config=generation_config,
            )
            return resp.text
        except Exception as e:
            wait = 2 ** attempt * 5
            logger.warning(f"Gemini attempt {attempt+1} failed: {e}. Retrying in {wait}s")
            if attempt < 2:
                time.sleep(wait)
            else:
                raise


def call_claude(prompt: str, max_tokens: int = 8192) -> str:
    """Claude API 備援"""
    import anthropic
    client = anthropic.Anthropic()
    msg = client.messages.create(
        model="claude-sonnet-4-5",
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


def translate(prompt: str, model: str | None = None, max_tokens: int = 8192) -> str:
    """
    翻譯入口。
    TRANSLATION_BACKEND=gemini（預設）→ Google AI Gemini
    TRANSLATION_BACKEND=claude         → Claude（備援）
    """
    if BACKEND == "claude":
        logger.info("Using Claude backend (fallback mode)")
        return call_claude(prompt, max_tokens)
    return call_gemini(prompt, model, max_tokens)


def judge(prompt: str) -> str:
    """LLM-as-Judge 固定用 Gemini Flash（成本低）"""
    return call_gemini(prompt, model=cfg.GEMINI_FLASH_MODEL, max_tokens=2048)
