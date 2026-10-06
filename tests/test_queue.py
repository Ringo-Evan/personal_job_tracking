"""Verify queue order and skip rules (plan.md Phase 5)."""

from datetime import date
from pathlib import Path

from jobsearch.queue import skip_reason, verify_queue
from jobsearch.store import Store

TODAY = date(2026, 10, 5)


def rec(cid, rank=None, fit=None, stab=None, remote=None, **extra):
    t = {"company_id": cid, "company": cid, "research_status": "Not started", "overall_rank": rank}
    t |= extra.pop("tracker", {})
    triage = {"fit": fit, "stability": stab, "remote": remote} if fit else None
    return {"company_id": cid, "tracker": t, "triage": triage, "research": None, "history": []} | extra


def ids(records):
    s = {r["company_id"]: r for r in records}

    class Fake(Store):
        def company_ids(self):
            return list(s)

        def load_company(self, cid):
            return s.get(cid)

    return lambda: Fake(Path(__file__).parent / "no-such-data")


def queue(*records, only=None):
    store = ids(records)()
    return [r["company_id"] for r in verify_queue(store, TODAY, 45, only)]


def test_owner_rank_first_then_fit_stability_remote():
    order = queue(
        rec("A", fit=5, stab=5, remote="High"),
        rec("B", rank=2, fit=1, stab=1, remote="Low"),
        rec("C", rank=1, fit=2, stab=2, remote="Low"),
        rec("D", fit=5, stab=4, remote="High"),
        rec("E", fit=5, stab=5, remote="Low"),
        rec("F", fit=5, stab=5, remote="Unknown"),
        rec("G", fit=5, stab=5, remote="Medium"),
        rec("H"),  # untriaged goes last
    )
    assert order == ["C", "B", "A", "G", "F", "E", "D", "H"]


def test_rank_4_and_parked_are_skipped():
    assert queue(
        rec("A", rank=4, fit=5), rec("B", fit=5, tracker={"research_status": "Parked"}), rec("C")
    ) == ["C"]


def test_recently_researched_skipped_until_refresh():
    fresh = rec("A", fit=5, research={"at": "2026-09-01"})
    stale = rec("B", fit=5, research={"at": "2026-08-01"})
    assert queue(fresh, stale) == ["B"]
    assert skip_reason(fresh, TODAY, 45) == "researched 34 days ago"


def test_owner_marked_researched_recently_is_skipped():
    owner = rec("A", tracker={"research_status": "Researched", "research_as_of": "2026-09-30"})
    old = rec("B", tracker={"research_status": "Researched", "research_as_of": "2026-01-01"})
    assert queue(owner, old) == ["B"]


def test_two_rejects_skip_and_missing_from_tracker_skip():
    assert queue(
        rec("A", verify_rejects=2), rec("B", verify_rejects=1), rec("C", missing_from_tracker=True)
    ) == ["B"]


def test_only_keeps_skip_rules_and_force_bypasses_all_but_parked():
    fresh = rec("A", fit=5, research={"at": "2026-10-04"})
    parked = rec("B", tracker={"research_status": "Parked"})
    new = rec("C", fit=3)
    assert queue(fresh, parked, new, only=["A", "B", "C"]) == ["C"]
    store = ids([fresh, parked, new])()
    assert [r["company_id"] for r in verify_queue(store, TODAY, 45, ["A", "B"], force=True)] == ["A"]
