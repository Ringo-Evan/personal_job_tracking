"""Nightly run (plan.md Phase 7): stage order, the lock, skipping search stages, failures that don't stop the run.

No real stage ever runs here: a fake runner records the commands instead.
"""

import re
import subprocess
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobsearch.nightly import LOCK_WAIT_S, AlreadyRunning, run_lock, run_nightly, stages

TODAY = "2026-10-06"
REGISTER = Path(__file__).parents[1] / "tools" / "register_tasks.ps1"


def names(cfg, **kw):
    return [s.name for s in stages(cfg, **kw)]


def test_lock_is_exclusive_and_released(tmp_path):
    path = tmp_path / "nightly.lock"
    with run_lock(path), pytest.raises(AlreadyRunning), run_lock(path):
        pass
    with run_lock(path):  # free again once the first run ends
        pass


def test_stage_order_and_export_mode(cfg):
    assert names(cfg) == ["ingest", "detect", "discover", "watch", "triage", "verify", "rank", "export", "digest"]
    export = next(s for s in stages(cfg) if s.name == "export")
    assert export.args == ("export",)  # straight into the tracker by default (plan-tracker.md)
    cfg.nightly.export = "preview"
    assert next(s for s in stages(cfg) if s.name == "export").args == ("export", "--dry-run")
    cfg.nightly.discover_limit = 0
    assert "discover" not in names(cfg)
    assert names(cfg, watch_only=True) == ["watch", "rank"]


def test_scheduled_task_outlasts_every_stage_limit(cfg):
    # A slow night must still reach the report before Task Scheduler stops the run, even after waiting for the lock.
    script = REGISTER.read_text(encoding="utf-8")
    hours = int(re.search(r"-ExecutionTimeLimit \(New-TimeSpan -Hours (\d+)\)", script).group(1))
    cfg.verify.max_minutes = 120  # the owner's setting
    assert LOCK_WAIT_S + sum(s.timeout_s for s in stages(cfg)) < hours * 3600


class Runner:
    def __init__(self, codes=None, hang=()):
        self.codes, self.hang, self.calls = codes or {}, hang, []

    def __call__(self, argv, **kw):
        stage = argv[3]  # python -m jobsearch <stage> ...
        self.calls.append(stage)
        if stage in self.hang:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return SimpleNamespace(returncode=self.codes.get(stage, 0))


def run(cfg, tmp_path, runner, search_ok=True, logged=None, **kw):
    logged = [] if logged is None else logged
    entry = run_nightly(cfg, runner=runner, search_ok=lambda c: search_ok, log=logged.append,
                        lock_path=tmp_path / "lock", logs_dir=tmp_path / "logs", today=TODAY, **kw)  # fmt: skip
    return entry, logged


def test_search_down_skips_discover_and_verify_and_a_failure_does_not_stop_the_run(cfg, tmp_path):
    runner = Runner(codes={"rank": 1}, hang=("watch",))
    entry, logged = run(cfg, tmp_path, runner, search_ok=False)
    assert runner.calls == ["ingest", "detect", "watch", "triage", "rank", "export", "digest"]
    assert {s["stage"]: s.get("skipped") for s in entry["stages"]}["verify"] == "search is down"
    assert any("SearXNG" in a for a in entry["alerts"])
    assert any(a.startswith("watch ran past its time limit") for a in entry["alerts"])
    assert any(a.startswith("rank failed (exit 1)") for a in entry["alerts"])
    # logged before the report is written, so the report can show these alerts
    assert len(logged) == 1 and logged[0]["command"] == "nightly" and len(logged[0]["alerts"]) == 3
    assert (tmp_path / "logs" / f"nightly-{TODAY}.txt").read_text(encoding="utf-8").count("=====") == 7


def test_watch_only_runs_two_stages_without_needing_search(cfg, tmp_path):
    runner = Runner()
    entry, logged = run(cfg, tmp_path, runner, search_ok=False, watch_only=True)
    assert runner.calls == ["watch", "rank"] and entry["alerts"] == []
    assert logged[0]["command"] == "nightly --watch-only"


def test_the_nightly_waits_for_a_watch_run_to_finish(cfg, tmp_path):
    watch_run, waits = ExitStack(), []
    watch_run.enter_context(run_lock(tmp_path / "lock"))

    def sleep(s):
        waits.append(s)
        if len(waits) == 3:
            watch_run.close()  # the watch run ends

    runner = Runner()
    run(cfg, tmp_path, runner, sleep=sleep)
    assert len(waits) == 3 and runner.calls[-1] == "digest"
    log = (tmp_path / "logs" / f"nightly-{TODAY}.txt").read_text(encoding="utf-8")
    assert "nightly: waited 3 minutes" in log


def test_a_run_that_cannot_get_the_lock_says_so(cfg, tmp_path):
    logged, waits = [], []
    with run_lock(tmp_path / "lock"):
        with pytest.raises(AlreadyRunning):
            run(cfg, tmp_path, Runner(), logged=logged, watch_only=True, sleep=waits.append)
        assert waits == [] and logged == []  # steps aside at once: the run going watches the boards too
        with pytest.raises(AlreadyRunning):
            run(cfg, tmp_path, Runner(), logged=logged, sleep=waits.append)
    assert sum(waits) == LOCK_WAIT_S
    [entry] = logged  # the nightly's alert goes in the next report
    assert entry["command"] == "nightly" and "did not start" in entry["alerts"][0]
    log = (tmp_path / "logs" / f"nightly-{TODAY}.txt").read_text(encoding="utf-8")
    assert "nightly --watch-only: skipped" in log and "nightly: did not run" in log
