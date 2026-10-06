"""Smoke-test the local stack (plan-models.md Phase 2b): every provider endpoint, SearXNG, daily budgets."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import httpx

from jobsearch.config import TIERS, Config, Endpoint
from jobsearch.usage import record_call, used_today


@dataclass
class Check:
    name: str
    ok: bool | None  # None = skipped (not configured)
    detail: str


def _ollama_tags(base_url: str) -> set[str] | None:
    try:
        r = httpx.get(base_url.rstrip("/") + "/api/tags", timeout=5)
        r.raise_for_status()
    except httpx.HTTPError:
        return None
    return {m["name"] for m in r.json().get("models", [])}


def _ollama_name(model: str) -> str | None:
    prefix, _, name = model.partition("/")
    return name if prefix in ("ollama", "ollama_chat") else None


def check_endpoint(
    ep: Endpoint, db: sqlite3.Connection, complete: Callable[..., Any] | None = None, timeout: float = 300
) -> Check:
    if ep.key_env and not os.environ.get(ep.key_env):
        return Check(ep.model, None, f"skipped: {ep.key_env} not set")
    if ep.rpd and used_today(db, ep.model) >= ep.rpd:
        return Check(ep.model, None, f"skipped: daily budget {ep.rpd} used")
    if ep.model.startswith("claude-cli/"):  # no ping: a real call would spend subscription usage
        import shutil

        found = shutil.which("claude")
        return Check(ep.model, bool(found), f"claude CLI at {found}" if found else "claude CLI not on PATH")
    if ep.model.startswith("codex-cli/"):
        return check_codex(ep)
    if (name := _ollama_name(ep.model)) is not None:
        tags = _ollama_tags(ep.base_url or "http://localhost:11434")
        if tags is None:
            return Check(ep.model, False, "Ollama server not reachable (is the Ollama app running?)")
        if name not in tags and f"{name}:latest" not in tags:
            return Check(ep.model, False, f"model not pulled: run `ollama pull {name}`")
    if complete is None:
        from litellm import completion as complete  # slow import; only when actually calling
    kwargs: dict[str, Any] = dict(ep.extra) | ({"api_base": ep.base_url} if ep.base_url else {})
    if ep.key_env:
        kwargs["api_key"] = os.environ[ep.key_env]
    started = time.monotonic()
    try:
        complete(
            model=ep.model,
            messages=[{"role": "user", "content": "Reply with the word OK."}],
            max_tokens=1,
            timeout=timeout,
            **kwargs,
        )
    except Exception as e:  # provider SDKs raise many types; doctor reports, never crashes
        return Check(ep.model, False, f"{type(e).__name__}: {str(e)[:160]}")
    finally:
        if not _ollama_name(ep.model):
            record_call(db, ep.model)  # cloud 1-token pings still spend daily quota
    return Check(ep.model, True, f"ok in {time.monotonic() - started:.1f}s (includes model load)")


def check_codex(ep: Endpoint, runner=subprocess.run) -> Check:
    """`codex login status` only: no model call, so no ChatGPT usage spent."""
    from jobsearch.codex_agent import find_codex

    codex = find_codex()
    if not codex:
        return Check(ep.model, False, "codex CLI not found")
    try:
        proc = runner([codex, "login", "status"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        return Check(ep.model, False, f"{type(e).__name__}: {e}")
    status = f"{proc.stdout} {proc.stderr}".strip()
    if proc.returncode != 0 or "not logged in" in status.lower():
        return Check(ep.model, False, "not logged in: run `codex login`")
    return Check(ep.model, True, status[:80])


def check_searxng(url: str) -> Check:
    try:
        r = httpx.get(
            url.rstrip("/") + "/search", params={"q": "greenhouse careers", "format": "json"}, timeout=20
        )
    except httpx.HTTPError as e:
        return Check(
            "searxng", False, f"not reachable at {url} ({type(e).__name__}); run `docker compose up -d`"
        )
    if r.status_code == 403:
        return Check("searxng", False, "403: JSON format not enabled in searxng/settings.yml")
    if r.status_code != 200:
        return Check("searxng", False, f"HTTP {r.status_code}")
    n = len(r.json().get("results", []))
    return Check("searxng", n > 0, f"{n} results for a test query" + ("" if n else " (all engines failing?)"))


def budgets(cfg: Config, db: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = []
    for tier in TIERS:
        for ep in cfg.providers.tier(tier):
            left = "unlimited" if not ep.rpd else f"{max(ep.rpd - used_today(db, ep.model), 0)}/{ep.rpd}"
            rows.append((tier, ep.model, left))
    return rows
