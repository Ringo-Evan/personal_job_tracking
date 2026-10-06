"""Nightly run (plan.md Phase 7): every stage in order under one lock, ending with the report.

Each stage runs as its own `python -m jobsearch <stage>` process, so a crash or a hang in one can't take the rest
down; their output goes to logs/nightly-<date>.txt. Stages that need web search are skipped, with an alert in the
report, when SearXNG isn't answering. `--watch-only` is the 6-hourly job: watch the boards and score new postings.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from jobsearch.config import ROOT, Config
from jobsearch.runlog import log_run

LOCK = ROOT / "state" / "nightly.lock"
LOGS = ROOT / "logs"


class AlreadyRunning(RuntimeError):
    """Another nightly or watch-only run holds the lock."""


@contextmanager
def run_lock(path: Path = LOCK) -> Iterator[None]:
    """An OS-level lock held for the whole run. The OS frees it if the process dies, so it never goes stale."""
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")  # noqa: SIM115 - held open for the lifetime of the lock
    try:
        try:
            f.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as e:
            raise AlreadyRunning(f"another run holds {path}") from e
        try:
            yield
        finally:
            f.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    finally:
        f.close()


@dataclass(frozen=True)
class Stage:
    name: str
    args: tuple[str, ...]
    needs_search: bool = False
    timeout_s: int = 3600


WATCH = Stage("watch", ("watch", "--quiet"), timeout_s=2 * 3600)  # 45 minutes for 472 boards on 2026-10-06


def stages(cfg: Config, watch_only: bool = False) -> list[Stage]:
    if watch_only:
        return [WATCH, Stage("rank", ("rank",))]
    out = [Stage("ingest", ("ingest",), timeout_s=600), Stage("detect", ("detect",))]
    if cfg.nightly.discover_limit:
        out.append(Stage("discover", ("discover", "--limit", str(cfg.nightly.discover_limit)), needs_search=True))
    return out + [
        WATCH,
        Stage("triage", ("triage",)),
        Stage("verify", ("verify",), needs_search=True, timeout_s=cfg.verify.max_minutes * 60 + 900),
        Stage("rank", ("rank",)),
        Stage("export", ("export",) if cfg.nightly.export == "real" else ("export", "--dry-run"), timeout_s=900),
        Stage("digest", ("digest",), timeout_s=600),
    ]


def searxng_ok(cfg: Config) -> bool:
    try:
        return httpx.get(f"{cfg.search.searxng_url.rstrip('/')}/healthz", timeout=15).status_code == 200
    except httpx.HTTPError:
        return False


def run_stage(stage: Stage, log_file: Path, runner: Callable[..., Any] = subprocess.run) -> int | None:
    """The stage's exit code, or None if it ran past its timeout."""
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(f"\n===== {datetime.now():%H:%M:%S} {stage.name}: jobsearch {' '.join(stage.args)}\n")
        log.flush()
        try:
            return runner(
                [sys.executable, "-m", "jobsearch", *stage.args],
                cwd=ROOT,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=stage.timeout_s,
                env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                check=False,
            ).returncode
        except subprocess.TimeoutExpired:
            return None


def run_nightly(
    cfg: Config,
    *,
    watch_only: bool = False,
    runner: Callable[..., Any] = subprocess.run,
    search_ok: Callable[[Config], bool] = searxng_ok,
    log: Callable[[dict[str, Any]], None] = log_run,
    lock_path: Path = LOCK,
    logs_dir: Path = LOGS,
    today: str | None = None,
) -> dict[str, Any]:
    """Run every stage; never stops early on a failed stage. Returns the run-log entry (also logged)."""
    today = today or date.today().isoformat()
    log_file = logs_dir / f"nightly-{today}.txt"
    started = time.monotonic()
    entry: dict[str, Any] = {"command": "nightly --watch-only" if watch_only else "nightly", "stages": [], "alerts": []}
    with run_lock(lock_path):
        search = True
        if any(s.needs_search for s in stages(cfg, watch_only)) and not (search := search_ok(cfg)):
            entry["alerts"].append(
                "SearXNG is not answering, so discover and verify were skipped. Start Docker Desktop, then run "
                "`docker compose up -d` in the repo folder."
            )
        for stage in stages(cfg, watch_only):
            if stage.needs_search and not search:
                entry["stages"].append({"stage": stage.name, "skipped": "search is down"})
                continue
            if stage.name == "digest":  # log first, so the report can show this run's alerts
                log(entry | {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "duration_s": round(time.monotonic() - started, 2)})  # fmt: skip
            t0 = time.monotonic()
            code = run_stage(stage, log_file, runner)
            entry["stages"].append({"stage": stage.name, "exit": code, "minutes": round((time.monotonic() - t0) / 60, 1)})
            if code != 0:
                entry["alerts"].append(
                    f"{stage.name} " + ("ran past its time limit" if code is None else f"failed (exit {code})")
                    + f"; see logs/nightly-{today}.txt"
                )  # fmt: skip
    if watch_only:
        log(entry | {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                     "duration_s": round(time.monotonic() - started, 2)})  # fmt: skip
    return entry
