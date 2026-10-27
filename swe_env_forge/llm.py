"""A minimal client for OpenAI-compatible chat endpoints (OpenAI, OpenRouter, vLLM, Ollama, ...).

Configuration comes from arguments or environment variables:
``SWEFORGE_LLM_BASE_URL`` (default ``https://api.openai.com/v1``), ``SWEFORGE_LLM_API_KEY``
(falls back to ``OPENAI_API_KEY``) and ``SWEFORGE_LLM_MODEL``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


@dataclass
class Usage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost: float = 0.0
    calls: int = 0

    def add(self, other: "Usage") -> None:
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.cost += other.cost
        self.calls += other.calls


class LLMError(RuntimeError):
    pass


@dataclass
class ChatClient:
    model: str = field(default_factory=lambda: os.environ.get("SWEFORGE_LLM_MODEL", ""))
    base_url: str = field(default_factory=lambda: os.environ.get("SWEFORGE_LLM_BASE_URL", "https://api.openai.com/v1"))
    api_key: str = field(default_factory=lambda: os.environ.get("SWEFORGE_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", ""))
    input_price: float = 0.0      # USD per million prompt tokens
    output_price: float = 0.0     # USD per million completion tokens
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout: float = 300.0
    retries: int = 3
    usage: Usage = field(default_factory=Usage)

    def chat(self, messages: list[dict[str, Any]], **extra) -> tuple[str, Usage]:
        """Plain text completion."""
        message, usage = self.complete(messages, **extra)
        return message.get("content") or "", usage

    def complete(self, messages: list[dict[str, Any]], tools: list[dict] | None = None,
                 **extra) -> tuple[dict[str, Any], Usage]:
        """Full assistant message (``content`` and, with ``tools``, ``tool_calls``) plus usage."""
        if tools:
            extra = {"tools": tools, "tool_choice": "auto", **extra}
        return self._request(messages, **extra)

    def _request(self, messages: list[dict[str, Any]], **extra) -> tuple[dict[str, Any], Usage]:
        if not self.model:
            raise LLMError("no model set (pass --model or set SWEFORGE_LLM_MODEL)")
        body = {"model": self.model, "messages": messages, "temperature": self.temperature,
                "max_tokens": self.max_tokens, **extra}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        url = self.base_url.rstrip("/") + "/chat/completions"
        delay = 2.0
        for attempt in range(self.retries + 1):
            req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    data = json.loads(resp.read())
                break
            except urllib.error.HTTPError as exc:
                text = exc.read().decode("utf-8", "replace") if exc.fp else ""
                if exc.code in (429, 500, 502, 503, 504, 529) and attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                if exc.code == 400 and ("context" in text.lower() and "length" in text.lower()):
                    raise LLMError(f"context_overflow: {text[:300]}") from None
                raise LLMError(f"HTTP {exc.code}: {text[:300]}") from None
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt < self.retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise LLMError(f"connection failed: {exc}") from None
        try:
            message = dict(data["choices"][0]["message"])
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected response: {str(data)[:300]}") from None
        if (data["choices"][0].get("finish_reason") == "length" and not message.get("content")
                and not message.get("tool_calls")):
            raise LLMError("the reply was cut off by max_tokens before it said anything")
        u = data.get("usage") or {}
        usage = Usage(prompt_tokens=int(u.get("prompt_tokens", 0)), completion_tokens=int(u.get("completion_tokens", 0)),
                      calls=1)
        usage.cost = (usage.prompt_tokens * self.input_price + usage.completion_tokens * self.output_price) / 1e6
        self.usage.add(usage)
        message.setdefault("role", "assistant")
        return message, usage
