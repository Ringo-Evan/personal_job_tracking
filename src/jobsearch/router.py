"""One entry point for every runtime model call (plan-models.md §6).

Tiers are tried in the order `routing.<task>` lists them, endpoints in config order. An endpoint is skipped when its
API key is unset, its daily budget is spent, or it is marked exhausted after a 429. A schema failure is retried once
on the same endpoint with the error appended; a second failure escalates to the next tier. Good results are cached
by (task, model, prompt hash) so a rerun never re-spends quota. When every tier is out, `RouterExhausted` is raised
so the stage can stop cleanly and leave the queue for the next run.

Privacy (§1.4): when `needs_profile` is true, a tier outside `profile_allowed_tiers` only ever receives
`public_messages` (the trimmed profile). Without them, that tier is skipped.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import jsonschema

from jobsearch.config import Config, Endpoint
from jobsearch.usage import ROUTER_DB, connect, record_call, used_today

Messages = list[dict[str, str]]
# accept(data) -> None to keep the result, or a reason string to escalate to the next tier.
Accept = Callable[[Any], str | None]
SCHEMA_RETRIES = 1
# Endpoint prefixes that run through the owner's logged-in subscription CLIs instead of LiteLLM.
CLAUDE_CLI = "claude-cli/"
CODEX_CLI = "codex-cli/"
# Codex can run read-only shell commands, so it only gets prompts built from the pipeline's own data.
CODEX_TASKS = {"triage"}


class RouterExhausted(RuntimeError):
    """Every tier for a task is out of budget, unreachable or failing: stop the stage, keep the queue."""

    def __init__(self, message: str, attempts: list[Attempt]):
        super().__init__(message)
        self.attempts = attempts


@dataclass
class Attempt:
    tier: str
    model: str
    outcome: str  # ok | cached | schema | rejected | rate-limited | error | skipped
    detail: str = ""
    latency_s: float = 0.0
    tokens: dict[str, int] = field(default_factory=dict)


@dataclass
class RouteResult:
    data: Any
    tier: str
    model: str
    cached: bool
    attempts: list[Attempt]
    tokens: dict[str, int] = field(default_factory=dict)
    latency_s: float = 0.0

    @property
    def escalations(self) -> int:
        return len({a.tier for a in self.attempts if a.outcome != "skipped"}) - 1


def prompt_hash(messages: Messages, schema: dict[str, Any] | None) -> str:
    blob = json.dumps({"m": messages, "s": schema}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _is_rate_limit(e: Exception) -> bool:
    name = type(e).__name__
    return (
        getattr(e, "status_code", None) == 429
        or name in ("RateLimitError", "UsageLimitError")
        or "quota" in str(e).lower()
    )


def _retry_after(e: Exception) -> float | None:
    headers = getattr(getattr(e, "response", None), "headers", None) or {}
    try:
        return float(headers.get("retry-after"))
    except (TypeError, ValueError):
        return None


def _strip_fences(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else ""
        t = t.rsplit("```", 1)[0]
    return t.strip()


class Router:
    def __init__(
        self,
        cfg: Config,
        *,
        db: sqlite3.Connection | None = None,
        db_path: Path = ROUTER_DB,
        complete: Callable[..., Any] | None = None,
        clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        environ: dict[str, str] | None = None,
        claude_agent: Callable[..., Any] | None = None,
        codex_agent: Callable[..., Any] | None = None,
    ):
        self.cfg = cfg
        self.db = db or connect(db_path)
        self._complete = complete
        self._claude_agent = claude_agent
        self._codex_agent = codex_agent
        self.clock, self.sleep = clock, sleep
        self.environ = os.environ if environ is None else environ
        self._recent: dict[str, deque[float]] = {}  # model -> call times in the last minute, for RPM pacing

    # -- public ---------------------------------------------------------------------------------------------

    def call(
        self,
        task: str,
        messages: Messages,
        schema: dict[str, Any],
        *,
        needs_profile: bool,
        public_messages: Messages | None = None,
        accept: Accept | None = None,
        keep_rejected: bool = False,
        timeout_s: float = 300,
    ) -> RouteResult:
        """keep_rejected: `accept` is a soft gate. A rejected answer still escalates, but if no later tier does
        better, the first rejected answer is returned (uncached) instead of raising RouterExhausted."""
        tiers: list[str] = list(getattr(self.cfg.routing, task))
        attempts: list[Attempt] = []
        rejected: list[RouteResult] = []
        for tier in tiers:
            msgs = messages
            if needs_profile and tier not in self.cfg.profile_allowed_tiers:
                if public_messages is None:
                    attempts.append(Attempt(tier, "-", "skipped", "tier may not see the profile"))
                    continue
                msgs = public_messages
            for ep in self.cfg.providers.tier(tier):
                result = self._try_endpoint(
                    task,
                    tier,
                    ep,
                    msgs,
                    schema,
                    accept,
                    timeout_s,
                    attempts,
                    rejected if keep_rejected else None,
                )
                if result is not None:
                    return result
            if any(a.tier == tier and a.outcome in ("schema", "rejected") for a in attempts):
                self._log_escalation(task, tier, attempts)
        if rejected:
            best = rejected[0]
            best.attempts = attempts
            return best
        raise RouterExhausted(
            f"{task}: no tier succeeded: "
            + "; ".join(f"{a.tier}/{a.model} {a.outcome} {a.detail}".strip() for a in attempts)[:500],
            attempts,
        )

    # -- internals ------------------------------------------------------------------------------------------

    def _try_endpoint(
        self,
        task: str,
        tier: str,
        ep: Endpoint,
        messages: Messages,
        schema: dict[str, Any],
        accept: Accept | None,
        timeout_s: float,
        attempts: list[Attempt],
        rejected: list[RouteResult] | None = None,
    ) -> RouteResult | None:
        if ep.model.startswith(CODEX_CLI) and task not in CODEX_TASKS:
            attempts.append(
                Attempt(tier, ep.model, "skipped", f"codex is not used for {task} (it has shell access)")
            )
            return None
        if ep.key_env and not self.environ.get(ep.key_env):
            attempts.append(Attempt(tier, ep.model, "skipped", f"{ep.key_env} not set"))
            return None
        if ep.rpd and used_today(self.db, ep.model, self._today()) >= ep.rpd:
            attempts.append(Attempt(tier, ep.model, "skipped", f"daily budget {ep.rpd} used"))
            return None
        if (until := self._exhausted_until(ep.model)) is not None:
            attempts.append(Attempt(tier, ep.model, "skipped", f"rate-limited until {until}"))
            return None

        key = prompt_hash(messages, schema)
        if (hit := self._cache_get(task, ep.model, key)) is not None:
            attempts.append(Attempt(tier, ep.model, "cached"))
            return RouteResult(hit, tier, ep.model, True, attempts)

        convo = list(messages)
        for try_no in range(SCHEMA_RETRIES + 1):
            self._pace(ep)
            started = time.monotonic()
            try:
                resp = self._send(ep, convo, schema, timeout_s)
            except Exception as e:  # provider SDKs raise many types; any failure moves on, never crashes
                latency = time.monotonic() - started
                if _is_rate_limit(e):
                    self._mark_exhausted(ep.model, _retry_after(e))
                    attempts.append(Attempt(tier, ep.model, "rate-limited", str(e)[:200], latency))
                else:
                    attempts.append(
                        Attempt(tier, ep.model, "error", f"{type(e).__name__}: {str(e)[:200]}", latency)
                    )
                return None
            latency = time.monotonic() - started
            tokens = _usage(resp)
            text = _content(resp)
            data, error = self._parse(text, schema)
            if error is None and accept is not None and (reason := accept(data)):
                attempts.append(Attempt(tier, ep.model, "rejected", reason[:200], latency, tokens))
                if rejected is not None:
                    rejected.append(RouteResult(data, tier, ep.model, False, attempts, tokens, latency))
                return None  # a valid but unacceptable answer escalates; retrying the same model won't help
            if error is None:
                attempts.append(Attempt(tier, ep.model, "ok", "", latency, tokens))
                self._cache_put(task, ep.model, key, data)
                return RouteResult(data, tier, ep.model, False, attempts, tokens, latency)
            attempts.append(Attempt(tier, ep.model, "schema", error[:200], latency, tokens))
            if try_no < SCHEMA_RETRIES:
                convo = [
                    *messages,
                    {"role": "assistant", "content": text[:4000]},
                    {"role": "user", "content": f"That reply was invalid: {error[:500]}. "
                     "Reply again with only JSON that matches the schema."},
                ]  # fmt: skip
        return None

    def _send(self, ep: Endpoint, messages: Messages, schema: dict[str, Any], timeout_s: float) -> Any:
        if ep.model.startswith((CLAUDE_CLI, CODEX_CLI)):
            record_call(self.db, ep.model, self._today())
            return self._send_cli(ep, messages, schema, timeout_s)
        complete = self._complete
        if complete is None:
            from litellm import completion as complete  # slow import; only when actually calling
        kwargs: dict[str, Any] = dict(ep.extra)
        if ep.base_url:
            kwargs["api_base"] = ep.base_url
        if ep.key_env:
            kwargs["api_key"] = self.environ[ep.key_env]
        if ep.json_mode == "object":
            # Provider only does plain JSON mode (DeepSeek): state the schema in the prompt; _parse still validates.
            response_format: dict[str, Any] = {"type": "json_object"}
            messages = [
                {"role": "system", "content": "Reply with one JSON object matching this JSON Schema:\n"
                 + json.dumps(schema, separators=(",", ":"))},
                *messages,
            ]  # fmt: skip
        else:
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": "result", "schema": schema, "strict": True},
            }
        record_call(self.db, ep.model, self._today())
        return complete(
            model=ep.model,
            messages=messages,
            response_format=response_format,
            timeout=timeout_s,
            **kwargs,
        )

    def _send_cli(self, ep: Endpoint, messages: Messages, schema: dict[str, Any], timeout_s: float) -> Any:
        """One subscription CLI call (`claude -p` via agent.py, `codex exec` via codex_agent.py), shaped like a
        LiteLLM response. UsageLimitError propagates and counts as a 429.

        claude-cli extra: {thinking: false}. codex-cli extra: Codex `-c` overrides, e.g.
        {model_reasoning_effort: "low"}."""
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        turns = [m for m in messages if m["role"] != "system"]
        if len(turns) == 1:
            prompt = turns[0]["content"]
        else:  # schema retry: flatten the conversation into one prompt
            prompt = "\n\n".join(f"[{m['role']}]\n{m['content']}" for m in turns)
        common = {"prompt": prompt, "system_prompt": system, "schema": schema, "timeout_s": int(timeout_s)}
        if ep.model.startswith(CODEX_CLI):
            agent = self._codex_agent
            if agent is None:
                from jobsearch.codex_agent import run_codex as agent
            result = agent(self.cfg, model=ep.model.removeprefix(CODEX_CLI), config=dict(ep.extra), **common)
        else:
            agent = self._claude_agent
            if agent is None:
                from jobsearch.agent import run_agent as agent
            result = agent(
                self.cfg,
                model=ep.model.removeprefix(CLAUDE_CLI),
                tools=[],
                thinking=bool(ep.extra.get("thinking", True)),
                **common,
            )
        if not result.ok:
            raise RuntimeError(f"{ep.model} failed: {result.error}")
        u = result.usage or {}
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(result.structured)))],
            usage=SimpleNamespace(
                prompt_tokens=u.get("input_tokens", 0), completion_tokens=u.get("output_tokens", 0)
            ),
        )

    @staticmethod
    def _parse(text: str, schema: dict[str, Any]) -> tuple[Any, str | None]:
        if not text.strip():
            return None, "empty reply"
        try:
            data = json.loads(_strip_fences(text))
        except json.JSONDecodeError as e:
            return None, f"not JSON: {e}"
        try:
            jsonschema.validate(data, schema)
        except jsonschema.ValidationError as e:
            where = "/".join(str(p) for p in e.absolute_path) or "(root)"
            return None, f"schema: {where}: {e.message}"
        return data, None

    def _today(self) -> date:
        return datetime.fromtimestamp(self.clock()).date()

    def _pace(self, ep: Endpoint) -> None:
        """Proactive RPM pacing: wait for a slot instead of earning a 429."""
        if not ep.rpm:
            return
        q = self._recent.setdefault(ep.model, deque())
        now = self.clock()
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= ep.rpm:
            self.sleep(60 - (now - q[0]))
            q.popleft()
        q.append(self.clock())

    def _mark_exhausted(self, model: str, retry_after: float | None) -> None:
        if retry_after is not None and retry_after <= 120:
            until = self.clock() + retry_after
        else:  # quota errors rarely say when they reset; assume the daily quota, back tomorrow
            tomorrow = self._today() + timedelta(days=1)
            until = datetime(tomorrow.year, tomorrow.month, tomorrow.day).timestamp()
        self.db.execute(
            "INSERT INTO exhausted (model, until) VALUES (?, ?) ON CONFLICT (model) DO UPDATE SET until = excluded.until",
            (model, until),
        )
        self.db.commit()

    def _exhausted_until(self, model: str) -> str | None:
        row = self.db.execute("SELECT until FROM exhausted WHERE model = ?", (model,)).fetchone()
        if row and row[0] > self.clock():
            return datetime.fromtimestamp(row[0]).isoformat(timespec="minutes")
        return None

    def _cache_get(self, task: str, model: str, key: str) -> Any | None:
        row = self.db.execute(
            "SELECT response FROM cache WHERE task = ? AND model = ? AND prompt_hash = ?", (task, model, key)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def _cache_put(self, task: str, model: str, key: str, data: Any) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO cache (task, model, prompt_hash, response, at) VALUES (?, ?, ?, ?, ?)",
            (task, model, key, json.dumps(data, ensure_ascii=False), self.clock()),
        )
        self.db.commit()

    def _log_escalation(self, task: str, tier: str, attempts: list[Attempt]) -> None:
        last = next(a for a in reversed(attempts) if a.tier == tier and a.outcome in ("schema", "rejected"))
        self.db.execute(
            "INSERT INTO escalations (at, task, tier, model, reason) VALUES (?, ?, ?, ?, ?)",
            (self.clock(), task, tier, last.model, f"{last.outcome}: {last.detail}"[:500]),
        )
        self.db.commit()


def _content(resp: Any) -> str:
    try:
        return resp.choices[0].message.content or ""
    except (AttributeError, IndexError, KeyError, TypeError):
        return ""


def _usage(resp: Any) -> dict[str, int]:
    u = getattr(resp, "usage", None)
    if not u:
        return {}
    return {k: int(getattr(u, k, 0) or 0) for k in ("prompt_tokens", "completion_tokens")}
