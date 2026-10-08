"""The tracker store (plan-tracker.md Phase T1): field ownership, conflicts, history, IDs and the claude.ai migration."""

import json

import pytest

from jobsearch.store import write_json
from jobsearch.tracker import (
    COLLECTIONS,
    OWNER,
    PIPELINE,
    ConflictError,
    OwnershipError,
    Tracker,
    TrackerError,
    name_key,
)
from jobsearch.tracker_csv import COMPANY_LABELS, JOB_LABELS, PEOPLE_LABELS


@pytest.fixture
def t(tmp_path):
    return Tracker(tmp_path / "tracker")


def history(t):
    return [json.loads(line) for line in (t.root / "history.jsonl").read_text(encoding="utf-8").splitlines()]


def test_schema_keeps_the_tracker_labels_and_every_owned_field_exists():
    for name, labels in (("companies", COMPANY_LABELS), ("jobs", JOB_LABELS), ("people", PEOPLE_LABELS)):
        col = COLLECTIONS[name]
        assert {f.key: f.label for f in col.fields} == labels  # the labels are the CSV contract
        assert col.fields[0].key == col.id_key
    for col in COLLECTIONS.values():
        assert col.agent <= col.keys and col.insert_only <= col.keys
        assert set(col.columns) - {"_due", "pay"} <= col.keys


def test_an_agent_cannot_touch_owner_fields_activity_materials_or_deletes(t):
    t.write("companies", "C001", {"company": "Acme", "sector": "Games"}, by=PIPELINE, create=True)
    with pytest.raises(OwnershipError, match="overall_rank"):
        t.write("companies", "C001", {"overall_rank": 1, "fit_score": 4}, by=PIPELINE)
    assert "fit_score" not in t.get("companies", "C001")  # a refused write writes nothing
    with pytest.raises(OwnershipError, match="sector"):  # insert-only: naming it once is all an agent gets
        t.write("companies", "C001", {"sector": "Travel"}, by="Claude Code")
    for name, row_id in (("activity", "A001"), ("materials", "R001")):
        with pytest.raises(OwnershipError):
            t.write(name, row_id, {"type": "Call"} if name == "activity" else {"doc_type": "Resume"}, by=PIPELINE,
                    create=True)  # fmt: skip
    with pytest.raises(OwnershipError):
        t.delete("companies", "C001", by=PIPELINE)


def test_agent_jobs_start_at_lead_and_insert_only_fields_are_set_once(t):
    job = t.write("jobs", "JW0001", {"company": "Acme", "job_title": "SDET", "track": "Core QA"}, by=PIPELINE,
                  create=True)  # fmt: skip
    assert (job["current_stage"], job["highest_stage"]) == ("Lead", "None") and job["date_discovered"]
    with pytest.raises(OwnershipError, match="current_stage"):
        t.write("jobs", "JW0001", {"current_stage": "Applied"}, by=PIPELINE)
    with pytest.raises(OwnershipError, match="Lead"):
        t.write("jobs", "JW0002", {"company": "Acme", "current_stage": "Applied"}, by=PIPELINE, create=True)
    with pytest.raises(OwnershipError, match="date_applied"):
        t.write("jobs", "JW0001", {"date_applied": "2026-10-07"}, by=PIPELINE)


def test_an_agent_blank_never_erases_but_the_owner_can_clear(t):
    t.write("jobs", "J001", {"company": "Acme", "fit_notes": "good"}, by=OWNER, create=True)
    t.write("jobs", "J001", {"fit_notes": "", "pay_low": None}, by=PIPELINE)
    assert t.get("jobs", "J001")["fit_notes"] == "good"
    t.write("jobs", "J001", {"fit_notes": " "}, by=OWNER)
    assert "fit_notes" not in t.get("jobs", "J001")


def test_the_owner_writes_anything_and_stage_moves_raise_the_highest_stage(t):
    t.write("jobs", "J001", {"company": "Acme"}, by=OWNER, create=True)
    assert t.get("jobs", "J001")["highest_stage"] == "None"  # Lead isn't progress
    for stage, highest in (("Applied", "Applied"), ("Interview", "Interview"), ("Screen", "Interview"),
                           ("Rejected", "Interview")):  # fmt: skip
        row = t.write("jobs", "J001", {"current_stage": stage}, by=OWNER)
        assert (row["current_stage"], row["highest_stage"]) == (stage, highest)
    row = t.write("jobs", "J002", {"company": "Acme", "current_stage": "Applied"}, by=OWNER, create=True)
    assert row["highest_stage"] == "Applied" and row["updated_by"] == OWNER
    t.delete("jobs", "J002", by=OWNER)
    assert t.get("jobs", "J002") is None and history(t)[-1]["row"]["job_id"] == "J002"


def test_a_stale_write_is_refused_but_other_fields_of_the_row_go_through(t):
    t.write("jobs", "J001", {"company": "Acme", "current_stage": "Lead"}, by=OWNER, create=True)
    seen = {"current_stage": "Lead"}  # the page opened the drawer
    t.write("jobs", "J001", {"pay_low": "120,000"}, by=PIPELINE)  # the nightly updates another field meanwhile
    t.write("jobs", "J001", {"current_stage": "Applied"}, by=OWNER, seen=seen)
    with pytest.raises(ConflictError) as e:  # a second tab still thinks the stage is Lead
        t.write("jobs", "J001", {"current_stage": "Withdrawn"}, by=OWNER, seen=seen)
    assert e.value.current["current_stage"] == "Applied" and e.value.current["pay_low"] == 120000


def test_history_keeps_old_values_and_unchanged_writes_leave_no_trace(t):
    t.write("companies", "C001", {"company": "Acme", "fit_score": "3"}, by=PIPELINE, create=True)
    row = t.write("companies", "C001", {"fit_score": 4}, by=PIPELINE)
    assert t.write("companies", "C001", {"fit_score": "4"}, by=PIPELINE)["updated_at"] == row["updated_at"]
    log = history(t)
    assert [e["op"] for e in log] == ["create", "update"]
    assert log[1]["changes"] == {"fit_score": [3, 4]} and log[1]["by"] == PIPELINE


def test_numbers_parse_like_the_tracker_and_bad_input_is_refused(t):
    row = t.write("jobs", "J001", {"company": "Acme", "pay_low": "141,000", "pay_high": "about 90k"}, by=OWNER,
                  create=True)  # fmt: skip
    assert row["pay_low"] == 141000 and row["pay_high"] == "about 90k"  # kept as text rather than dropped
    for bad in ({"no_such_field": 1}, {"job_id": "J999"}):
        with pytest.raises(TrackerError):
            t.write("jobs", "J001", bad, by=OWNER)
    with pytest.raises(TrackerError, match="already exists"):
        t.write("jobs", "J001", {}, by=OWNER, create=True)
    with pytest.raises(TrackerError, match="no jobs row"):
        t.write("jobs", "J404", {"company": "Acme"}, by=OWNER)
    with pytest.raises(TrackerError, match="valid row ID"):
        t.write("jobs", "../escape", {}, by=OWNER, create=True)


def test_next_id_counts_only_its_own_prefix(t):
    for row_id in ("J001", "J036", "JW0154"):
        t.write("jobs", row_id, {"company": "Acme"}, by=OWNER, create=True)
    assert t.next_id("jobs") == "J037"
    assert t.next_id("jobs", "JW", pad=4) == "JW0155"
    assert t.next_id("activity") == "A001"


def test_name_key_matches_the_claude_ai_tracker():
    assert name_key("Roll20 (Demiplane)") == name_key("roll20") == "roll20"
    assert name_key("First National Bank (The First Bancorp)") == "firstnationalbank"


def snapshot(tmp_path):
    src = tmp_path / "snap"
    rows = {
        "companies/C001": {"company_id": "C001", "company": "Acme Games (Acme)", "sector": "Labor",
                           "updated_by": "Claude (seed)", "updated_at": "2026-09-30T14:30:43.404Z"},
        "companies/C002": {"company_id": "C002", "company": "Beta", "sector": "Games & stories", "legacy": "x"},
        "jobs/J001": {"job_id": "J001", "company": "Acme Games", "current_stage": "Lead", "highest_stage": "None"},
        "jobs/J002": {"job_id": "J002", "company": "Gone Inc", "current_stage": "Lead"},
        "people/P001": {"person_id": "P001", "company": "Beta", "name": "Ada Lovelace", "status": "To research"},
        "meta/schema": {"agent_rules": ["List existing documents first."]},
    }  # fmt: skip
    for rel, row in rows.items():
        write_json(src / f"{rel}.json", row)
    return src


def test_migration_loads_rows_as_they_are_merges_sectors_links_companies_and_reruns_as_a_no_op(t, tmp_path):
    src = snapshot(tmp_path)
    rep = t.migrate(src)
    assert dict(rep.created) == {"companies": 2, "jobs": 2, "people": 1} and rep.sectors_merged == 1
    c1 = t.get("companies", "C001")
    assert c1["sector"] == "Labor & organizing" and c1["updated_by"] == "Claude (seed)"  # provenance kept
    assert t.get("jobs", "J001")["company_id"] == "C001" and t.get("people", "P001")["company_id"] == "C002"
    assert rep.unlinked == ["J002 (Gone Inc)"] and rep.unknown_fields == {"companies.legacy": 1}
    assert t.get("companies", "C002")["legacy"] == "x"  # unknown fields are kept, never lost
    assert (t.root / "meta" / "schema.json").exists()

    t.write("jobs", "J001", {"current_stage": "Applied"}, by=OWNER)  # the owner works in the new tracker
    again = t.migrate(src)
    assert not again.created and sum(again.unchanged.values()) == 4 and again.differs == ["jobs/J001"]
    assert t.get("jobs", "J001")["current_stage"] == "Applied"  # never overwritten
    assert [e["op"] for e in history(t)] == ["migrate", "update"]


def test_queued_companies_are_checked_up_front_and_added_once_even_after_an_interruption(t):
    t.write("companies", "C001", {"company": "Acme"}, by=OWNER, create=True)
    with pytest.raises(OwnershipError, match="overall_rank"):
        t.queue_companies([{"company": "X", "overall_rank": 1}], label="bad", by="Codex")
    with pytest.raises(TrackerError, match="no company name"):
        t.queue_companies([{"sector": "Games"}], label="bad", by="Codex")
    rows = [{"company": "Gamma"}, {"company": "ACME (Acme Inc)"}, {"company": "Delta", "sector": "Travel"}]
    batch = t.queue_companies(rows, label="three", by="Codex")
    [queued] = t.imports()
    assert (queued["id"], queued["status"], queued["rows"], queued["added_by"]) == (batch, "pending", 3, "Codex")
    # Stopped after adding Gamma: the batch recorded it, and Gamma is in the tracker.
    t.write("companies", "C002", {"company": "Gamma"}, by="Codex", create=True)
    path = t.root / "imports" / f"{batch}.json"
    write_json(path, json.loads(path.read_text(encoding="utf-8")) | {"status": "running", "added": {"gamma": "C002"}})
    done = t.accept_import(batch)
    assert (done["status"], done["imported"], done["skipped_duplicates"], done["refused"]) == ("done", 2, 1, [])
    delta = t.get("companies", "C003")
    assert (delta["company"], delta["research_status"], delta["updated_by"]) == ("Delta", "Not started", "Codex")
    assert t.get("companies", "C004") is None  # nothing added twice
    assert t.accept_import(batch)["imported"] == 2 and t.get("companies", "C004") is None  # done stays done
