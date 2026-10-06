"""Board watcher (plan phase 3): plain HTTP to job-board APIs, no LLM."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jobsearch.boards import BoardError, JobPosting, fetch_postings
from jobsearch.config import Config
from jobsearch.filters import PostingFilter, track_for
from jobsearch.http_client import PoliteClient
from jobsearch.models import is_skipped
from jobsearch.store import Store, read_json, write_json

CLOSE_AFTER_MISSES = 2
# Fields refreshed from the board on every run (the rest are set once).
LIVE_FIELDS = (
    "title", "location_text", "is_remote", "work_mode", "url", "apply_url", "posted_at",
    "pay_low", "pay_high", "currency", "pay_period", "raw_ref", "maine_eligible", "eligibility_reason",
    "description",
)  # fmt: skip
DESCRIPTION_CHARS = 6000  # kept for the job scorer (rank); never exported
DESCRIPTION_TAIL = 1800  # eligible-state lists and pay usually sit at the end of a posting


def clip_description(text: str, limit: int = DESCRIPTION_CHARS, tail: int = DESCRIPTION_TAIL) -> str:
    """Head and tail of a long posting: cutting only the end dropped the state list the scorer needs."""
    if len(text) <= limit:
        return text
    marker = " [...] "
    return text[: limit - tail - len(marker)] + marker + text[-tail:]


@dataclass
class WatchReport:
    boards_ok: int = 0
    boards_failed: list[str] = field(default_factory=list)
    fetched: int = 0
    kept: int = 0
    title_matches: int = 0
    # (company_id, title, location, reason) for postings whose title matched but location didn't
    dropped: list[tuple[str, str, str, str]] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    updated: int = 0
    closed: list[str] = field(default_factory=list)
    reopened: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        return {
            "boards_ok": self.boards_ok,
            "boards_failed": len(self.boards_failed),
            "fetched": self.fetched,
            "title_matches": self.title_matches,
            "kept": self.kept,
            "new": len(self.new),
            "updated": self.updated,
            "closed": len(self.closed),
            "reopened": len(self.reopened),
        }


def watchable(record: dict[str, Any], cfg: Config) -> bool:
    ats = record.get("ats") or {}
    if is_skipped(record) or record.get("missing_from_tracker") or not ats.get("token"):
        return False
    if ats.get("type") == "workday":
        return cfg.watch.workday_enabled and ats.get("confirmed") is not False
    if ats.get("type") == "smartrecruiters":
        return ats.get("confirmed") is not False  # zero postings can't be told apart from a bad token
    return ats.get("type") in ("greenhouse", "lever", "ashby") and ats.get("confirmed") is True


class JobStore:
    def __init__(self, store: Store):
        self.dir = store.data_dir / "jobs"

    def path(self, key: str) -> Path:
        return self.dir / f"{key}.json"

    def load(self, key: str) -> dict[str, Any] | None:
        return read_json(self.path(key))

    def save(self, job: dict[str, Any]) -> None:
        write_json(self.path(job["key"]), job)

    def for_board(self, board: str, token: str) -> list[dict[str, Any]]:
        if not self.dir.is_dir():
            return []
        jobs = (read_json(p) for p in self.dir.glob(f"{board}-*.json"))
        return [j for j in jobs if j and j.get("board_token") == token]


def _job_record(p: JobPosting, token: str, company: str, verdict, today: str) -> dict[str, Any]:
    return {
        "key": p.key,
        "board": p.board,
        "board_token": token,
        "board_job_id": p.board_job_id,
        "company_id": p.company_id,
        "company": company,
        "title": p.title,
        "location_text": p.location_text,
        "is_remote": p.is_remote,
        "work_mode": p.work_mode,
        "url": p.url,
        "apply_url": p.apply_url,
        "posted_at": p.posted_at,
        "pay_low": p.pay_low,
        "pay_high": p.pay_high,
        "currency": p.currency,
        "pay_period": p.pay_period,
        "raw_ref": p.raw_ref,
        "maine_eligible": "yes" if verdict.verdict == "eligible" else "unknown",
        "eligibility_reason": verdict.reason,
        "description": clip_description(p.description_text or ""),
        "track": track_for(p.title),
        "source": "watcher",
        "first_seen": today,
        "last_seen": today,
        "missed_runs": 0,
        "closed_at": None,
        "reopened_at": None,
        "job_id": None,  # tracker ID, allocated at first export (phase 6)
    }


def _update_board_state(state: dict[str, Any], board_key: str, error: str | None, now: str) -> None:
    s = state.setdefault("boards", {}).setdefault(board_key, {"consecutive_failures": 0})
    if error:
        s["consecutive_failures"] = s.get("consecutive_failures", 0) + 1
        s["last_error"] = error[:300]
        s["last_error_at"] = now
    else:
        s["consecutive_failures"] = 0
        s["last_ok_at"] = now


def watch_board(
    client: PoliteClient,
    cfg: Config,
    flt: PostingFilter,
    jobs: JobStore,
    record: dict[str, Any],
    today: str,
    report: WatchReport,
) -> None:
    """Fetch one company's board and reconcile it with data/jobs. Raises BoardError on fetch failure."""
    ats = record["ats"]
    board, token, cid = ats["type"], ats["token"], record["company_id"]
    company = record["tracker"].get("company") or cid
    postings = fetch_postings(client, cfg, board, token, cid, wanted=flt.title_ok)
    report.fetched += len(postings)

    seen: set[str] = set()
    for p in postings:
        if not flt.title_ok(p.title):
            continue
        verdict = flt.classify(p)
        report.title_matches += 1
        if verdict.verdict == "drop":
            report.dropped.append((cid, p.title, p.location_text, verdict.reason))
            continue
        report.kept += 1
        seen.add(p.key)
        fresh = _job_record(p, token, company, verdict, today)
        old = jobs.load(p.key)
        if old is None:
            jobs.save(fresh)
            report.new.append(p.key)
            continue
        job = dict(old)
        for f in LIVE_FIELDS:
            job[f] = fresh[f]
        job["last_seen"] = today
        job["missed_runs"] = 0
        if job.get("closed_at"):
            job["closed_at"] = None
            job["reopened_at"] = today
            report.reopened.append(p.key)
        if job != old:
            jobs.save(job)
            report.updated += 1

    for job in jobs.for_board(board, token):
        if job["key"] in seen or job.get("closed_at"):
            continue
        job["missed_runs"] = job.get("missed_runs", 0) + 1
        if job["missed_runs"] >= CLOSE_AFTER_MISSES:
            job["closed_at"] = today
            report.closed.append(job["key"])
        jobs.save(job)


def run_watch(
    store: Store,
    client: PoliteClient,
    cfg: Config,
    today: str,
    now: str,
    only: list[str] | None = None,
    progress=lambda msg: None,
) -> WatchReport:
    flt = PostingFilter(cfg)
    jobs = JobStore(store)
    state_path = store.data_dir / "watch_state.json"
    state = read_json(state_path) or {"boards": {}}
    report = WatchReport()
    done: set[tuple[str, str]] = set()

    for cid in only or store.company_ids():
        record = store.load_company(cid)
        if not record or not watchable(record, cfg):
            continue
        board_key = f"{record['ats']['type']}:{record['ats']['token']}"
        if (record["ats"]["type"], record["ats"]["token"]) in done:
            continue
        done.add((record["ats"]["type"], record["ats"]["token"]))
        before = len(report.new)
        try:
            watch_board(client, cfg, flt, jobs, record, today, report)
        except BoardError as e:
            report.boards_failed.append(f"{cid} {board_key}: {e}")
            _update_board_state(state, board_key, str(e), now)
            progress(f"  FAIL {cid} {board_key}: {e}")
            continue
        report.boards_ok += 1
        _update_board_state(state, board_key, None, now)
        progress(f"  ok   {cid} {board_key}: {len(report.new) - before} new")
    state["last_run_at"] = now
    write_json(state_path, state)
    return report
