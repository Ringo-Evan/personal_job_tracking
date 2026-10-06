import httpx
import pytest

from jobsearch.store import Store
from jobsearch.watch import JobStore, run_watch, watchable

GH_URL = "https://boards-api.greenhouse.io/v1/boards/acme/jobs"


def gh_job(job_id, title="Senior SDET", location="Remote - US", content=""):
    return {"id": job_id, "title": title, "location": {"name": location},
            "absolute_url": f"https://job-boards.greenhouse.io/acme/jobs/{job_id}",
            "first_published": "2026-10-01T00:00:00Z", "content": content}  # fmt: skip


class Board:
    """A fake Greenhouse board whose postings (or failure) the test controls between runs."""

    def __init__(self, jobs):
        self.jobs = jobs
        self.fail = False
        self.calls: list[str] = []

    def __call__(self, request):
        url = str(request.url)
        self.calls.append(url)
        if self.fail:
            return httpx.Response(503, text="down")
        if url == GH_URL:  # list: no content
            listing = [{k: v for k, v in j.items() if k != "content"} for j in self.jobs]
            return httpx.Response(200, json={"jobs": listing, "meta": {"total": len(self.jobs)}})
        job_id = int(url.removeprefix(GH_URL + "/").split("?")[0])
        return httpx.Response(200, json=next(j for j in self.jobs if j["id"] == job_id))


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    s.save_company(
        {
            "company_id": "C001",
            "tracker": {"company_id": "C001", "company": "Acme, Inc.", "research_status": "Not started"},
            "ats": {"type": "greenhouse", "token": "acme", "confirmed": True},
            "history": [],
        }
    )
    return s


@pytest.fixture
def run(store, make_client, cfg):
    def _run(board, today="2026-10-05"):
        client = make_client(board, max_attempts=1)
        return run_watch(store, client, cfg, today=today, now=f"{today}T08:00:00-04:00")

    return _run


def test_new_matching_postings_are_stored(run, store):
    board = Board([gh_job(1), gh_job(2, title="Account Executive"), gh_job(3, location="London, UK"),
                   gh_job(4, title="QA Manager", location="Global")])  # fmt: skip
    report = run(board)
    assert sorted(report.new) == ["greenhouse-1", "greenhouse-4"]
    assert report.fetched == 4 and report.kept == 2
    job = JobStore(store).load("greenhouse-1")
    assert job["company"] == "Acme, Inc."  # tracker's exact name string
    assert (job["maine_eligible"], job["track"], job["first_seen"], job["closed_at"]) == (
        "yes", "Core QA", "2026-10-05", None)  # fmt: skip
    assert JobStore(store).load("greenhouse-4")["maine_eligible"] == "unknown"
    assert JobStore(store).load("greenhouse-4")["track"] == "QA leadership"
    assert JobStore(store).load("greenhouse-2") is None


def test_second_run_produces_zero_new_rows(run, store):
    board = Board([gh_job(1), gh_job(4, title="QA Manager")])
    assert len(run(board).new) == 2
    files = {p.name: p.read_bytes() for p in (store.data_dir / "jobs").iterdir()}
    second = run(board)
    assert second.new == []
    assert {p.name: p.read_bytes() for p in (store.data_dir / "jobs").iterdir()} == files


def test_posting_closes_after_two_missed_runs_and_reopens(run, store):
    board = Board([gh_job(1)])
    run(board, "2026-10-05")
    board.jobs = []
    r1 = run(board, "2026-10-05")
    assert r1.closed == []
    assert JobStore(store).load("greenhouse-1")["missed_runs"] == 1
    r2 = run(board, "2026-10-06")
    assert r2.closed == ["greenhouse-1"]
    assert JobStore(store).load("greenhouse-1")["closed_at"] == "2026-10-06"
    assert run(board, "2026-10-06").closed == []  # closed once only

    board.jobs = [gh_job(1)]
    r4 = run(board, "2026-10-07")
    assert r4.reopened == ["greenhouse-1"] and r4.new == []
    job = JobStore(store).load("greenhouse-1")
    assert (job["closed_at"], job["reopened_at"], job["missed_runs"]) == (None, "2026-10-07", 0)


def test_one_miss_then_back_resets_counter(run, store):
    board = Board([gh_job(1)])
    run(board)
    board.jobs = []
    run(board)
    board.jobs = [gh_job(1)]
    run(board)
    board.jobs = []
    assert run(board).closed == []


def test_failed_board_never_closes_postings_and_counts_failures(run, store):
    board = Board([gh_job(1)])
    run(board)
    board.fail = True
    for _ in range(3):
        report = run(board)
        assert report.boards_failed and report.closed == []
    assert JobStore(store).load("greenhouse-1")["missed_runs"] == 0
    state = (store.data_dir / "watch_state.json").read_text(encoding="utf-8")
    assert '"consecutive_failures": 3' in state
    board.fail = False
    run(board)
    assert '"consecutive_failures": 0' in (store.data_dir / "watch_state.json").read_text(encoding="utf-8")


def test_live_fields_refresh_but_first_seen_and_track_do_not(run, store):
    board = Board([gh_job(1, content="")])
    run(board, "2026-10-05")
    job = JobStore(store).load("greenhouse-1")
    job["track"] = "SDE pivot"  # pretend this was set at insert
    JobStore(store).save(job)
    board.jobs = [gh_job(1, title="Senior SDET II", content="&lt;p&gt;$130,000 - $150,000&lt;/p&gt;")]
    report = run(board, "2026-10-06")
    job = JobStore(store).load("greenhouse-1")
    assert report.updated == 1
    assert (job["title"], job["pay_low"], job["first_seen"], job["last_seen"], job["track"]) == (
        "Senior SDET II", 130000, "2026-10-05", "2026-10-06", "SDE pivot")  # fmt: skip


def test_shared_board_token_fetched_once(run, store):
    rec = store.load_company("C001")
    rec["company_id"] = "C002"
    rec["tracker"] = rec["tracker"] | {"company_id": "C002"}
    store.save_company(rec)
    board = Board([gh_job(1)])
    run(board)
    assert board.calls.count(GH_URL) == 1


@pytest.mark.parametrize(
    ("ats", "tracker", "workday_enabled", "ok"),
    [
        ({"type": "greenhouse", "token": "a", "confirmed": True}, {}, False, True),
        ({"type": "greenhouse", "token": "a", "confirmed": None}, {}, False, False),
        ({"type": "lever", "token": "a", "confirmed": False}, {}, False, False),
        ({"type": "smartrecruiters", "token": "a", "confirmed": None}, {}, False, True),
        ({"type": "workday", "token": "a|1|b", "confirmed": True}, {}, True, True),
        ({"type": "workday", "token": "a|1|b", "confirmed": True}, {}, False, False),
        ({"type": "other", "token": None, "confirmed": None}, {}, False, False),
        (
            {"type": "greenhouse", "token": "a", "confirmed": True},
            {"research_status": "Parked"},
            False,
            False,
        ),
        ({"type": "greenhouse", "token": "a", "confirmed": True}, {"overall_rank": 5}, False, False),
    ],
)
def test_watchable(cfg, ats, tracker, workday_enabled, ok):
    cfg.watch.workday_enabled = workday_enabled
    assert watchable({"ats": ats, "tracker": tracker}, cfg) is ok


def test_long_description_keeps_head_and_tail_for_the_scorer():
    from jobsearch.watch import DESCRIPTION_CHARS, clip_description

    text = "About us. " * 900 + "We can hire in: Arizona, Texas, Ohio."
    clipped = clip_description(text)
    assert len(clipped) == DESCRIPTION_CHARS
    assert clipped.startswith("About us.") and clipped.endswith("We can hire in: Arizona, Texas, Ohio.")
    assert clip_description("short") == "short"
