"""A minimal client for OpenAI-compatible chat endpoints (OpenAI, OpenRouter, vLLM, Ollama, ...).

Configuration comes from arguments or environment variables:
``SWEKIT_LLM_BASE_URL`` (default ``https://api.openai.com/v1``), ``SWEKIT_LLM_API_KEY``
(falls back to ``OPENAI_API_KEY``) and ``SWEKIT_LLM_MODEL``.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field


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
    model: str = field(default_factory=lambda: os.environ.get("SWEKIT_LLM_MODEL", ""))
    base_url: str = field(default_factory=lambda: os.environ.get("SWEKIT_LLM_BASE_URL", "https://api.openai.com/v1"))
    api_key: str = field(default_factory=lambda: os.environ.get("SWEKIT_LLM_API_KEY") or os.environ.get("OPENAI_API_KEY", ""))
    input_price: float = 0.0      # USD per million prompt tokens
    output_price: float = 0.0     # USD per million completion tokens
    temperature: float = 0.0
    max_tokens: int = 4096
    timeout: float = 300.0
    retries: int = 3
    usage: Usage = field(default_factory=Usage)

    def chat(self, messages: list[dict[str, str]], **extra) -> tuple[str, Usage]:
        if not self.model:
            raise LLMError("no model set (pass --model or set SWEKIT_LLM_MODEL)")
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
            text = data["choices"][0]["message"].get("content") or ""
        except (KeyError, IndexError, TypeError):
            raise LLMError(f"unexpected response: {str(data)[:300]}") from None
        u = data.get("usage") or {}
        usage = Usage(prompt_tokens=int(u.get("prompt_tokens", 0)), completion_tokens=int(u.get("completion_tokens", 0)),
                      calls=1)
        usage.cost = (usage.prompt_tokens * self.input_price + usage.completion_tokens * self.output_price) / 1e6
        self.usage.add(usage)
        return text, usage
