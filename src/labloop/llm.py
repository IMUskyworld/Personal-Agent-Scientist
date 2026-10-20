"""LLM client (OpenAI-compatible) + redaction, shared by design and analyze backends."""

from __future__ import annotations

import json
import os
import re
import socket
from pathlib import Path
from typing import Any

import httpx

from labloop.config import LabloopConfig, llm_api_key, llm_ready
from labloop.errors import LLM_DISABLED, LLM_OUTPUT_INVALID, LLM_REQUEST_FAILED, LlmError

LLM_TIMEOUT_S = 60.0


def redact(text: str, *, home: str | None = None, user: str | None = None, host: str | None = None) -> str:
    """Replace home path, user name and host name; case-insensitive (DEVELOPMENT.md §16)."""
    home_value = home or str(Path.home())
    user_value = user if user is not None else (os.environ.get("USERNAME") or os.environ.get("USER") or "")
    host_value = host if host is not None else socket.gethostname()
    patterns: list[tuple[str, str]] = []
    for variant in {home_value, home_value.replace("\\", "/")}:
        if variant:
            patterns.append((re.escape(variant), "<home>"))
    if user_value:
        patterns.append((re.escape(user_value), "<user>"))
    if host_value:
        patterns.append((re.escape(host_value), "<host>"))
    result = text
    for pattern, replacement in patterns:
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result


def extract_json(text: str) -> dict[str, Any]:
    """Tolerate fenced blocks and surrounding prose; raise LlmError when there is no object."""
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", candidate)
        candidate = re.sub(r"\s*```\s*$", "", candidate)
    try:
        payload = json.loads(candidate)
    except json.JSONDecodeError:
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start == -1 or end <= start:
            raise LlmError("LLM 输出中没有 JSON 对象", code=LLM_OUTPUT_INVALID) from None
        try:
            payload = json.loads(candidate[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LlmError(f"LLM 输出不是合法 JSON：{exc}", code=LLM_OUTPUT_INVALID) from exc
    if not isinstance(payload, dict):
        raise LlmError("LLM 输出 JSON 顶层必须是对象", code=LLM_OUTPUT_INVALID)
    return payload


class LlmClient:
    def __init__(
        self,
        config: LabloopConfig,
        *,
        transport: httpx.BaseTransport | None = None,
        timeout_s: float = LLM_TIMEOUT_S,
    ) -> None:
        self._config = config
        self._transport = transport
        self._timeout_s = timeout_s

    def ready(self) -> tuple[bool, str]:
        return llm_ready(self._config)

    def chat_json(self, *, system: str, user: str, max_tokens: int = 900) -> dict[str, Any]:
        ready, reason = self.ready()
        if not ready:
            raise LlmError(reason, code=LLM_DISABLED)
        key = llm_api_key()
        if not key:  # pragma: no cover - llm_ready guards this
            raise LlmError("缺少 API key", code=LLM_DISABLED)
        base_url = self._config.llm.base_url
        model = self._config.llm.model
        assert base_url is not None and model is not None
        url = f"{base_url.rstrip('/')}/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": 0.2,
            "max_tokens": max_tokens,
        }
        try:
            with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
                response = client.post(url, json=payload, headers={"Authorization": f"Bearer {key}"})
                response.raise_for_status()
                data = response.json()
        except httpx.HTTPError as exc:
            raise LlmError(f"LLM 请求失败：{type(exc).__name__}: {exc}", code=LLM_REQUEST_FAILED) from exc
        except ValueError as exc:
            raise LlmError(f"LLM 响应不是合法 JSON：{exc}", code=LLM_OUTPUT_INVALID) from exc
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise LlmError("LLM 响应结构不符合 OpenAI 兼容格式", code=LLM_OUTPUT_INVALID) from exc
        if not isinstance(content, str) or not content.strip():
            raise LlmError("LLM 返回空内容", code=LLM_OUTPUT_INVALID)
        return extract_json(content)
