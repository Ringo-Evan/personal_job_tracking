"""Run OpenAI Codex CLI headless (`codex exec`) on the owner's ChatGPT login, for tasks with no web text.

Same sandbox rules as agent.py: a fresh empty working directory, a scrubbed environment, no session files, no user
config, and a read-only sandbox. Codex can still run read-only shell commands, so the router only uses it for
prompts built from the pipeline's own data (triage), never for evidence packs of web text.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from jobsearch.agent import _LIMIT_HINTS, AgentResult, Runner, UsageLimitError, agent_env
from jobsearch.config import Config

DEFAULT_PATHS = [
    Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "OpenAI" / "Codex" / "bin" / "codex.exe"
]
_CODEX_ENV = re.compile(r"(?i)^codex_home$")


def find_codex() -> str | None:
    return shutil.which("codex") or next((str(p) for p in DEFAULT_PATHS if p.is_file()), None)


def codex_env(cfg: Config, base: dict[str, str] | None = None) -> dict[str, str]:
    base = dict(os.environ if base is None else base)
    env = agent_env(cfg, base)
    env.pop("ANTHROPIC_API_KEY", None)
    env.update({k: v for k, v in base.items() if _CODEX_ENV.match(k)})  # where the ChatGPT login lives
    return env


def build_command(
    codex: str, model: str, schema_path: str, out_path: str, workdir: str, config: dict
) -> list[str]:
    cmd = [
        codex, "exec",
        "--sandbox", "read-only",
        "--ephemeral",
        "--ignore-user-config",
        "--ignore-rules",
        "--skip-git-repo-check",
        "--color", "never",
        "--json",
        "--cd", workdir,
        "--output-schema", schema_path,
        "--output-last-message", out_path,
    ]  # fmt: skip
    if model and model != "default":
        cmd += ["--model", model]
    for k, v in config.items():
        cmd += ["-c", f"{k}={json.dumps(v)}"]
    return cmd + ["-"]  # prompt on stdin


def _usage(jsonl: str) -> dict[str, int]:
    """Sum token usage from any event that reports it; Codex's event names change between versions."""
    total = {"input_tokens": 0, "output_tokens": 0}
    for line in jsonl.splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        u = ev.get("usage") if isinstance(ev, dict) else None
        if isinstance(u, dict):
            for k in total:
                total[k] += int(u.get(k) or 0)
    return total


def run_codex(
    cfg: Config,
    *,
    prompt: str,
    system_prompt: str,
    model: str,
    schema: dict[str, Any],
    timeout_s: int,
    config: dict[str, Any] | None = None,
    runner: Runner = subprocess.run,
    codex_path: str | None = None,
) -> AgentResult:
    """One headless call. Raises UsageLimitError on ChatGPT usage/rate limits; returns ok=False on other failures."""
    codex = codex_path or find_codex()
    if not codex:
        return AgentResult(False, error="codex CLI not found")
    workdir = tempfile.mkdtemp(prefix="jobsearch-codex-")
    io_dir = tempfile.mkdtemp(
        prefix="jobsearch-codex-io-"
    )  # schema and answer files stay outside the workdir
    schema_path, out_path = os.path.join(io_dir, "schema.json"), os.path.join(io_dir, "answer.json")
    Path(schema_path).write_text(json.dumps(schema), encoding="utf-8")
    started = time.monotonic()
    try:
        proc = runner(
            build_command(codex, model, schema_path, out_path, workdir, config or {}),
            input=f"{system_prompt}\n\n{prompt}" if system_prompt else prompt,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=workdir,
            env=codex_env(cfg),
            timeout=timeout_s,
        )
        answer = Path(out_path).read_text(encoding="utf-8") if os.path.isfile(out_path) else ""
    except subprocess.TimeoutExpired:
        return AgentResult(
            False, error=f"timed out after {timeout_s}s", duration_s=time.monotonic() - started
        )
    finally:
        leftovers = os.listdir(workdir) if os.path.isdir(workdir) else []
        shutil.rmtree(workdir, ignore_errors=True)
        shutil.rmtree(io_dir, ignore_errors=True)
    duration = time.monotonic() - started
    if leftovers:
        return AgentResult(
            False, error=f"codex wrote files to its workdir: {leftovers[:5]}", duration_s=duration
        )
    usage = _usage(proc.stdout or "")
    if proc.returncode != 0 or not answer.strip():
        blob = f"{proc.stderr or ''} {(proc.stdout or '')[-2000:]}"
        if _LIMIT_HINTS.search(blob):
            raise UsageLimitError(blob.strip()[:300])
        return AgentResult(
            False, error=f"codex exit {proc.returncode}: {blob.strip()[:300]}", duration_s=duration
        )
    try:
        structured = json.loads(answer)
    except json.JSONDecodeError:
        return AgentResult(
            False, text=answer, error="codex answer is not JSON", duration_s=duration, usage=usage
        )
    return AgentResult(True, structured=structured, text=answer, duration_s=duration, usage=usage)
