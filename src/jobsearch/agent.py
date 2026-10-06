"""Run Claude headless (`claude -p`) with a tool allowlist, a timeout and JSON output (plan hard rule 3).

Every call runs from a fresh, empty temp directory with a scrubbed environment, no MCP servers, no user/project
settings, no session persistence and no skills. `tools` is the complete allowlist: [] means no tools at all.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jobsearch.config import Config

# Environment variables that look like credentials never reach the agent process (hard rule 5).
_SECRET_ENV = re.compile(r"(?i)(key|token|secret|password|passwd|credential|cookie|session)")
# What the agent may keep: enough for the CLI to find its own login and run on Windows.
_ENV_ALLOW = re.compile(
    r"(?i)^(path|pathext|systemroot|windir|comspec|temp|tmp|home|userprofile|appdata|localappdata|"
    r"homedrive|homepath|username|programdata|programfiles.*|xdg_.*|lang|lc_.*|claude_config_dir)$"
)
_LIMIT_HINTS = re.compile(r"(?i)usage limit|rate limit|rate_limit|overloaded|quota|limit reached|429")


class UsageLimitError(RuntimeError):
    """Claude usage or rate limit hit: stop the run cleanly and leave the queue for next time."""


@dataclass
class AgentResult:
    ok: bool
    structured: Any = None
    text: str = ""
    error: str = ""
    duration_s: float = 0.0
    cost_usd: float | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] | None = None


def agent_env(cfg: Config, base: dict[str, str] | None = None) -> dict[str, str]:
    base = dict(os.environ if base is None else base)
    env = {k: v for k, v in base.items() if _ENV_ALLOW.match(k) and not _SECRET_ENV.search(k)}
    if cfg.auth == "api-key" and base.get("ANTHROPIC_API_KEY"):
        env["ANTHROPIC_API_KEY"] = base["ANTHROPIC_API_KEY"]  # the CLI's own auth; never put in a prompt
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    return env


def build_command(
    claude: str, model: str, tools: list[str], system_prompt: str, schema: dict | None
) -> list[str]:
    cmd = [
        claude, "-p",
        "--output-format", "json",
        "--model", model,
        "--tools", ",".join(tools),
        "--strict-mcp-config",
        "--setting-sources", "",
        "--no-session-persistence",
        "--disable-slash-commands",
        "--permission-prompts", "none",
        "--system-prompt", system_prompt,
    ]  # fmt: skip
    if tools:
        cmd += ["--allowedTools", ",".join(tools)]
    if schema is not None:
        cmd += ["--json-schema", json.dumps(schema, separators=(",", ":"))]
    return cmd


Runner = Callable[..., subprocess.CompletedProcess]


def run_agent(
    cfg: Config,
    *,
    prompt: str,
    system_prompt: str,
    model: str,
    tools: list[str],
    schema: dict | None,
    timeout_s: int,
    runner: Runner = subprocess.run,
    claude_path: str | None = None,
    thinking: bool = True,
) -> AgentResult:
    """Run one headless call. Raises UsageLimitError on usage/rate limits; returns ok=False on other failures."""
    claude = claude_path or shutil.which("claude")
    if not claude:
        return AgentResult(False, error="claude CLI not found on PATH")
    cmd = build_command(claude, model, tools, system_prompt, schema)
    workdir = tempfile.mkdtemp(prefix="jobsearch-agent-")
    started = time.monotonic()
    try:
        proc = runner(
            cmd,
            input=prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=workdir,
            env=agent_env(cfg) | ({} if thinking else {"MAX_THINKING_TOKENS": "0"}),
            timeout=timeout_s,
        )
    except subprocess.TimeoutExpired:
        return AgentResult(
            False, error=f"timed out after {timeout_s}s", duration_s=time.monotonic() - started
        )
    finally:
        leftovers = os.listdir(workdir) if os.path.isdir(workdir) else []
        shutil.rmtree(workdir, ignore_errors=True)
    duration = time.monotonic() - started
    if leftovers:
        # The agent has no file tools, so anything here means the sandbox assumption broke.
        return AgentResult(
            False, error=f"agent wrote files to its workdir: {leftovers[:5]}", duration_s=duration
        )
    return parse_output(proc.stdout, proc.stderr, proc.returncode, duration)


def parse_output(stdout: str, stderr: str, returncode: int, duration: float) -> AgentResult:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        blob = f"{stdout or ''} {stderr or ''}"
        if _LIMIT_HINTS.search(blob):
            raise UsageLimitError(blob.strip()[:300]) from None
        return AgentResult(
            False, error=f"non-JSON output (exit {returncode}): {blob.strip()[:300]}", duration_s=duration
        )

    result = AgentResult(
        ok=False,
        text=data.get("result") or "",
        duration_s=duration,
        cost_usd=data.get("total_cost_usd"),
        usage=data.get("usage") or {},
        raw=data,
    )
    if data.get("is_error") or data.get("subtype") != "success":
        detail = f"{data.get('subtype')} {data.get('api_error_status') or ''} {result.text}".strip()
        if data.get("api_error_status") == 429 or _LIMIT_HINTS.search(detail):
            raise UsageLimitError(detail[:300])
        result.error = detail[:300]
        return result
    structured = data.get("structured_output")
    if structured is None:
        try:
            structured = json.loads(result.text)
        except json.JSONDecodeError:
            result.error = "no structured_output and result text is not JSON"
            return result
    result.structured = structured
    result.ok = True
    return result
