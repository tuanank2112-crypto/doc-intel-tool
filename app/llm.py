from __future__ import annotations

import asyncio
import json
import re
from typing import Any

from openai import AsyncOpenAI, OpenAI

from .config import settings


def _client_kwargs() -> dict[str, Any]:
    key = settings.api_key
    if not key:
        raise RuntimeError(
            "Missing API key. Set LLM_API_KEY in environment / .env"
        )
    base = (settings.llm_base_url or "").rstrip("/") + "/"
    if settings.provider == "gemini" and "generativelanguage.googleapis.com" not in base:
        base = "https://generativelanguage.googleapis.com/v1beta/openai/"
    return {
        "api_key": key,
        "base_url": base,
        "timeout": float(settings.llm_timeout_seconds),
        "max_retries": 1,
    }


def sync_client() -> OpenAI:
    return OpenAI(**_client_kwargs())


def async_client() -> AsyncOpenAI:
    return AsyncOpenAI(**_client_kwargs())


def llm_enabled() -> bool:
    return bool(settings.api_key)


_JSON_FENCE = re.compile(r"```(?:json)?\s*([\s\S]*?)```", re.I)


def parse_json_object(text: str) -> dict[str, Any]:
    """Best-effort JSON object extraction from model output."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) else {"value": obj}
    except json.JSONDecodeError:
        pass
    m = _JSON_FENCE.search(text)
    if m:
        try:
            obj = json.loads(m.group(1).strip())
            return obj if isinstance(obj, dict) else {"value": obj}
        except json.JSONDecodeError:
            pass
    start, end = text.find("{"), text.rfind("}")
    if start >= 0 and end > start:
        try:
            obj = json.loads(text[start : end + 1])
            return obj if isinstance(obj, dict) else {"value": obj}
        except json.JSONDecodeError:
            pass
    return {"raw": text}


def _use_json_mode() -> bool:
    # Many OpenAI-compatible proxies (9flare/Claude) hang or double-fail on response_format
    if settings.llm_force_json_mode:
        return True
    base = (settings.llm_base_url or "").lower()
    model = (settings.llm_model or "").lower()
    if "9flare" in base or "claude" in model or model.startswith("pro/"):
        return False
    if settings.provider in ("openai_compatible", "xai"):
        # xAI often supports json_object; 9flare already excluded
        return "x.ai" in base or "openai.com" in base
    return False


async def achat_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    client = async_client()
    messages = [
        {
            "role": "system",
            "content": system
            + "\n\nTrả về đúng một JSON object hợp lệ, không markdown, không giải thích ngoài JSON.",
        },
        {"role": "user", "content": user},
    ]
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if _use_json_mode():
        kwargs["response_format"] = {"type": "json_object"}

    timeout = float(settings.llm_timeout_seconds)

    async def _call() -> str:
        resp = await client.chat.completions.create(**kwargs)
        return resp.choices[0].message.content or ""

    try:
        content = await asyncio.wait_for(_call(), timeout=timeout)
    except asyncio.TimeoutError as e:
        raise TimeoutError(f"LLM timeout after {timeout}s") from e
    except Exception as exc:
        exc_str = str(exc).lower()
        # Chỉ retry khi lỗi liên quan response_format không được hỗ trợ.
        # Không retry AuthenticationError, RateLimitError, v.v. (tốn thêm timeout vô ích).
        is_format_error = (
            "response_format" in exc_str
            or "invalid_request" in exc_str
            or "unsupported" in exc_str
        )
        if not is_format_error:
            raise
        kwargs.pop("response_format", None)
        try:
            content = await asyncio.wait_for(_call(), timeout=timeout)
        except asyncio.TimeoutError as e2:
            raise TimeoutError(f"LLM timeout after {timeout}s (retry)") from e2
    return parse_json_object(content)


async def achat_text(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 2048,
) -> str:
    client = async_client()
    timeout = float(settings.llm_timeout_seconds)

    async def _call() -> str:
        resp = await client.chat.completions.create(
            model=settings.llm_model,
            temperature=temperature,
            max_tokens=max_tokens,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        return (resp.choices[0].message.content or "").strip()

    return await asyncio.wait_for(_call(), timeout=timeout)


def chat_json_sync(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 4096,
) -> dict[str, Any]:
    client = sync_client()
    messages = [
        {
            "role": "system",
            "content": system
            + "\n\nTrả về đúng một JSON object hợp lệ, không markdown.",
        },
        {"role": "user", "content": user},
    ]
    kwargs: dict[str, Any] = {
        "model": settings.llm_model,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if _use_json_mode():
        kwargs["response_format"] = {"type": "json_object"}
    try:
        resp = client.chat.completions.create(**kwargs)
    except Exception:
        kwargs.pop("response_format", None)
        resp = client.chat.completions.create(**kwargs)
    content = resp.choices[0].message.content or ""
    return parse_json_object(content)
