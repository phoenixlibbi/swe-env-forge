"""Network helpers: HuggingFace datasets-server, SWE-bench Pro scripts, GitHub REST API.

Standard library only. Responses can be cached on disk (``SWEKIT_CACHE`` or
``~/.cache/swe-env-kit``).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Iterator

USER_AGENT = "swe-env-kit (+https://github.com/phoenixlibbi/swe-env-kit)"
HF_ROWS = "https://datasets-server.huggingface.co/rows"

DATASETS = {
    "swebench": "SWE-bench/SWE-bench",
    "swebench-lite": "SWE-bench/SWE-bench_Lite",
    "swebench-verified": "SWE-bench/SWE-bench_Verified",
    "swebench-multimodal": "SWE-bench/SWE-bench_Multimodal",
    "swebench-pro": "ScaleAI/SWE-bench_Pro",
}


class HTTPError(RuntimeError):
    def __init__(self, url: str, status: int, body: str = ""):
        super().__init__(f"HTTP {status} for {url}: {body[:200]}")
        self.url, self.status, self.body = url, status, body


def cache_dir() -> Path:
    root = Path(os.environ.get("SWEKIT_CACHE") or Path.home() / ".cache" / "swe-env-kit")
    root.mkdir(parents=True, exist_ok=True)
    return root


def http_get(url: str, headers: dict[str, str] | None = None, *, timeout: float = 60, retries: int = 4,
             cache: bool = False) -> tuple[bytes, dict[str, str]]:
    """GET with retries on 429 / 5xx and optional on-disk cache. Returns (body, headers)."""
    key = hashlib.sha256((url + json.dumps(headers or {}, sort_keys=True)).encode()).hexdigest()
    cpath = cache_dir() / "http" / key
    if cache and cpath.exists():
        return cpath.read_bytes(), {}
    hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
    delay = 2.0
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, headers=hdrs)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                body = resp.read()
                if cache:
                    cpath.parent.mkdir(parents=True, exist_ok=True)
                    cpath.write_bytes(body)
                return body, {k.lower(): v for k, v in resp.headers.items()}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace") if exc.fp else ""
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else delay)
                delay *= 2
                continue
            raise HTTPError(url, exc.code, text) from None
        except (urllib.error.URLError, TimeoutError) as exc:
            if attempt < retries:
                time.sleep(delay)
                delay *= 2
                continue
            raise HTTPError(url, 0, str(exc)) from None
    raise HTTPError(url, 0, "retries exhausted")


def get_json(url: str, headers: dict[str, str] | None = None, **kw) -> Any:
    body, _ = http_get(url, headers, **kw)
    return json.loads(body)


# ------------------------------------------------------------------ HuggingFace
def resolve_dataset(name: str) -> str:
    return DATASETS.get(name, name)


def iter_hf_rows(dataset: str, split: str = "test", config: str = "default", *, limit: int | None = None,
                 offset: int = 0, page: int = 100) -> Iterator[dict[str, Any]]:
    """Stream rows of a public HuggingFace dataset through the datasets-server API."""
    dataset = resolve_dataset(dataset)
    got = 0
    while limit is None or got < limit:
        length = page if limit is None else min(page, limit - got)
        q = urllib.parse.urlencode({"dataset": dataset, "config": config, "split": split,
                                    "offset": offset + got, "length": length})
        data = get_json(f"{HF_ROWS}?{q}")
        rows = data.get("rows") or []
        for r in rows:
            yield r["row"]
        got += len(rows)
        total = data.get("num_rows_total")
        if not rows or (total is not None and offset + got >= total):
            return


# ----------------------------------------------------------------- SWE-bench Pro
def fetch_pro_scripts(instance_id: str, ref: str = "main") -> dict[str, str]:
    """``run_script.sh`` and ``parser.py`` for one SWE-bench Pro instance (cached)."""
    from .presets import PRO_SCRIPTS_REPO

    out = {}
    for key, fname in (("run_script", "run_script.sh"), ("parser_script", "parser.py")):
        url = f"https://raw.githubusercontent.com/{PRO_SCRIPTS_REPO}/{ref}/run_scripts/{instance_id}/{fname}"
        body, _ = http_get(url, cache=True)
        out[key] = body.decode("utf-8")
    return out


# ----------------------------------------------------------------------- GitHub
def github_token() -> str | None:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    if shutil.which("gh"):
        try:
            res = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True, timeout=10)
            return res.stdout.strip() or None
        except (OSError, subprocess.SubprocessError):
            return None
    return None


class GitHub:
    """Tiny GitHub REST client with pagination and rate-limit waits."""

    def __init__(self, token: str | None = None, api: str = "https://api.github.com"):
        self.token = token if token is not None else github_token()
        self.api = api

    def _headers(self, accept: str = "application/vnd.github+json") -> dict[str, str]:
        h = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def get(self, path: str, params: dict | None = None, accept: str | None = None) -> Any:
        url = path if path.startswith("http") else f"{self.api}{path}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        for _ in range(3):
            try:
                body, _h = http_get(url, self._headers(accept or "application/vnd.github+json"))
                return body.decode("utf-8") if accept and "diff" in accept else json.loads(body)
            except HTTPError as exc:
                if exc.status == 403 and "rate limit" in exc.body.lower():
                    time.sleep(60)
                    continue
                raise
        raise HTTPError(url, 403, "rate limited")

    def paginate(self, path: str, params: dict | None = None, limit: int | None = None) -> Iterator[Any]:
        params = dict(params or {}, per_page=100)
        page, seen = 1, 0
        while True:
            items = self.get(path, {**params, "page": page})
            if not items:
                return
            for it in items:
                yield it
                seen += 1
                if limit is not None and seen >= limit:
                    return
            if len(items) < 100:
                return
            page += 1

    def license_spdx(self, repo: str) -> str | None:
        try:
            data = self.get(f"/repos/{repo}/license")
        except HTTPError as exc:
            if exc.status == 404:
                return None
            raise
        return ((data or {}).get("license") or {}).get("spdx_id")
