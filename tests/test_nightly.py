"""Nightly run (plan.md Phase 7): stage order, the lock, skipping search stages, failures that don't stop the run.

No real stage ever runs here: a fake runner records the commands instead.
"""

import subprocess
from types import SimpleNamespace

import pytest

from jobsearch.nightly import AlreadyRunning, run_lock, run_nightly, stages

TODAY = "2026-10-06"


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
    assert export.args == ("export", "--dry-run")  # preview by default: the owner exports when importing
    cfg.nightly.export = "real"
    assert next(s for s in stages(cfg) if s.name == "export").args == ("export",)
    cfg.nightly.discover_limit = 0
    assert "discover" not in names(cfg)
    assert names(cfg, watch_only=True) == ["watch", "rank"]


class Runner:
    def __init__(self, codes=None, hang=()):
        self.codes, self.hang, self.calls = codes or {}, hang, []

    def __call__(self, argv, **kw):
        stage = argv[3]  # python -m jobsearch <stage> ...
        self.calls.append(stage)
        if stage in self.hang:
            raise subprocess.TimeoutExpired(argv, kw["timeout"])
        return SimpleNamespace(returncode=self.codes.get(stage, 0))


def run(cfg, tmp_path, runner, search_ok=True, **kw):
    logged = []
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


def test_a_second_run_exits_while_the_first_holds_the_lock(cfg, tmp_path):
    with run_lock(tmp_path / "lock"), pytest.raises(AlreadyRunning):
        run(cfg, tmp_path, Runner())
