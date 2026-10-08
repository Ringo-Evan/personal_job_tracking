"""Nightly run (plan.md Phase 7): every stage in order under one lock, ending with the report.

Each stage runs as its own `python -m jobsearch <stage>` process, so a crash or a hang in one can't take the rest
down; their output goes to logs/nightly-<date>.txt. Stages that need web search are skipped, with an alert in the
report, when SearXNG isn't answering. `--watch-only` only watches the boards and scores new postings.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import httpx

from jobsearch.config import ROOT, Config
from jobsearch.runlog import log_run

LOCK = ROOT / "state" / "nightly.lock"
LOGS = ROOT / "logs"
# How long the nightly waits for a run holding the lock (a watch run took 43 minutes on 2026-10-07). A watch-only run
# never waits: whatever holds the lock watches the boards too.
LOCK_WAIT_S = 3600


class AlreadyRunning(RuntimeError):
    """Another nightly or watch-only run holds the lock."""


@contextmanager
def run_lock(
    path: Path = LOCK, wait_s: float = 0, poll_s: float = 60, sleep: Callable[[float], None] = time.sleep
) -> Iterator[float]:
    """An OS-level lock held for the whole run. The OS frees it if the process dies, so it never goes stale.

    Waits up to `wait_s` seconds for another run to let go, then raises AlreadyRunning. Yields the seconds waited.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    f = open(path, "a+b")  # noqa: SIM115 - held open for the lifetime of the lock
    try:
        waited = 0.0
        while True:
            try:
                f.seek(0)
                if os.name == "nt":
                    import msvcrt

                    msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as e:
                if waited >= wait_s:
                    raise AlreadyRunning(f"another run holds {path}") from e
            sleep(poll_s)
            waited += poll_s
        try:
            yield waited
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
        Stage("export", ("export",) if cfg.nightly.export == "store" else ("export", "--dry-run"), timeout_s=900),
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


def _note(log_file: Path, text: str) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(f"\n===== {datetime.now():%H:%M:%S} {text}\n")


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
    sleep: Callable[[float], None] = time.sleep,
) -> dict[str, Any]:
    """Run every stage; never stops early on a failed stage. Returns the run-log entry (also logged).

    A run that can't get the lock says so in the day's log, and a nightly that couldn't is an alert in the next
    report: on 2026-10-07 the nightly exited without a trace while a watch run held the lock, and there was no report.
    """
    today = today or date.today().isoformat()
    log_file = logs_dir / f"nightly-{today}.txt"
    started = time.monotonic()
    entry: dict[str, Any] = {"command": "nightly --watch-only" if watch_only else "nightly", "stages": [], "alerts": []}
    wait_s = 0 if watch_only else LOCK_WAIT_S  # watch-only steps aside: the run going watches the boards too
    with ExitStack() as held:
        try:
            waited = held.enter_context(run_lock(lock_path, wait_s=wait_s, sleep=sleep))
        except AlreadyRunning:
            if watch_only:
                _note(log_file, "nightly --watch-only: skipped, another run is going")
            else:
                gave_up = f"another run still held the lock after {LOCK_WAIT_S // 60} minutes"
                _note(log_file, f"nightly: did not run, {gave_up}")
                entry["alerts"].append(f"No report on {today}: the nightly did not start, {gave_up}.")
                log(entry | {"at": datetime.now().astimezone().isoformat(timespec="seconds"),
                             "duration_s": round(time.monotonic() - started, 2)})  # fmt: skip
            raise
        if waited:
            _note(log_file, f"nightly: waited {waited / 60:.0f} minutes for another run to finish")
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
