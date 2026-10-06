"""Export and digest (plan.md Phase 6): CSV contract, field ownership, insert-only, change tracking."""

import copy
import csv
import io

import pytest

from jobsearch.digest import build_digest, write_digest
from jobsearch.export import (
    COMPANY_OWNED,
    JOB_INSERT_ONLY,
    JOB_OWNED,
    PEOPLE_INSERT_ONLY,
    PEOPLE_OWNED,
    company_values,
    run_export,
)
from jobsearch.store import Store, read_json, write_json
from jobsearch.tracker_csv import COMPANIES, JOBS, PEOPLE, normalize_label, parse_text

TODAY = "2026-10-05"
NOW = "2026-10-05T03:00:00-04:00"

RESEARCH = {
    "company_id": "C1", "career_url": "https://acme.example/careers",
    "ats": {"type": "greenhouse", "token": "acme", "board_url": "https://job-boards.greenhouse.io/acme"},
    "remote_policy": {"summary": "Remote anywhere in the US.", "maine_eligible": "yes",
                      "evidence_url": "https://acme.example/careers"},
    "stability": {"score": 4, "signals": [
        {"kind": "layoffs", "summary": "Cut 5%", "date": None, "url": "https://news.example/layoffs"},
        {"kind": "revenue", "summary": "Profitable", "date": "2026-03", "url": "https://news.example/revenue"}]},
    "size_estimate": "500-5k", "fit": {"score": 4, "rationale": "Remote QA-heavy SaaS.", "role_families": ["SDET", "QA lead"]},
    "hiring_difficulty": 3,
    "qa_openings": [{"title": "Senior SDET", "location": "Remote, US", "work_mode": "Remote",
                     "url": "https://acme.example/jobs/42"}],
    "people": [{"name": "Dana Whitfield", "title": "VP of Engineering", "source_url": "https://acme.example/team"}],
    "approach_role": "Director of QA", "outreach_angle": "Regulated test automation for their platform.",
    "red_flags": "", "confidence": "medium",
    "sources": ["https://acme.example/careers", "https://news.example/revenue", "https://news.example/layoffs",
                "https://acme.example/team"],
    "notes": "Remote US; profitable.", "at": TODAY, "model": {"extract": "x", "judge": "y"},
}  # fmt: skip
TRIAGE = {"fit": 3, "stability": 2, "remote": "Medium", "size": "50-500", "roles": ["QA"], "approach": "CTO",
          "why": "Small studio.", "flags": "", "model": "m", "at": "2026-10-01"}  # fmt: skip
WATCHED_JOB = {
    "key": "greenhouse-1", "board": "greenhouse", "board_token": "acme", "board_job_id": "1", "company_id": "C1",
    "company": "Acme", "title": "QA Manager", "location_text": "Remote - US", "is_remote": True, "work_mode": "Remote",
    "url": "https://job-boards.greenhouse.io/acme/jobs/1", "apply_url": "https://job-boards.greenhouse.io/acme/jobs/1#app",
    "posted_at": "2026-10-01", "pay_low": 120000.0, "pay_high": 150000.0, "currency": "USD", "pay_period": "year",
    "raw_ref": "x", "maine_eligible": "yes", "eligibility_reason": "remote US", "track": "QA leadership",
    "source": "watcher", "first_seen": TODAY, "last_seen": TODAY, "missed_runs": 0, "closed_at": None,
    "reopened_at": None, "job_id": None,
}  # fmt: skip


def company(cid, name, status="Not started", research=None, triage=None, **extra):
    t = {"company_id": cid, "company": name, "research_status": status, "overall_rank": None,
         "dealbreakers": "secret owner note", "your_notes": "owner only", "interest": 5}  # fmt: skip
    t |= extra.pop("tracker", {})
    return {"company_id": cid, "tracker": t, "missing_from_tracker": False, "owner_changes": {}, "exported": {},
            "ats": {"type": "greenhouse", "token": "acme", "confirmed": True,
                    "board_url": "https://job-boards.greenhouse.io/acme"},
            "triage": triage, "research": research, "history": [], "last_exported_at": None} | extra  # fmt: skip


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    s.save_company(
        company("C1", "Acme, Inc.", "Initial research", research=copy.deepcopy(RESEARCH), triage=TRIAGE)
    )
    s.save_company(company("C2", "Tiny Games", triage=TRIAGE))
    s.save_company(company("C3", "Parked Co", "Parked", triage=TRIAGE))
    s.save_company(company("C4", "Ranked Out", triage=TRIAGE, tracker={"overall_rank": 4}))
    s.save_company(company("C5", "Owner Researched", "Researched", triage=TRIAGE,
                           owner_changes={"research_status": {"value": "Researched", "at": "2026-10-04T10:00:00"}}))  # fmt: skip
    write_json(s.data_dir / "jobs" / "greenhouse-1.json", dict(WATCHED_JOB))
    return s


def export(store, tmp_path, now=NOW):
    return run_export(store, None or _cfg(), today=TODAY, now=now, out_root=tmp_path / "out")


def _cfg():
    from jobsearch.config import load_config

    return load_config()


class Parsed:
    def __init__(self, result):
        # The tracker treats blank cells as "leave alone": drop them so rows show only what is written.
        self.rows = [{k: v for k, v in r.items() if v is not None} for r in result.rows]


def parsed(rep, name, tab):
    return Parsed(parse_text(rep.files[name].read_text(encoding="utf-8"), tab))


# --- the contract -------------------------------------------------------------------------------------


def test_every_header_maps_to_a_known_field_with_the_trackers_own_parser(store, tmp_path):
    rep = export(store, tmp_path)
    for name, tab in (("companies_import", COMPANIES), ("jobs_import", JOBS), ("people_import", PEOPLE)):
        text = rep.files[name].read_text(encoding="utf-8")
        p = parse_text(text, tab)
        assert p.ignored_columns == [] and p.warnings == [] and p.header_row_index == 0
        header = next(csv.reader(io.StringIO(text)))
        known = {normalize_label(label) for label in tab.labels.values()}
        assert all(normalize_label(h) in known for h in header)
        assert all(h in tab.labels.values() for h in header)  # exact labels, not just normalized matches
        assert header[0] == tab.id_label
        assert not text.startswith("﻿")


def test_headers_are_exact_labels(store, tmp_path):
    rep = export(store, tmp_path)
    header = rep.files["companies_import"].read_text(encoding="utf-8").splitlines()[0]
    assert header.startswith("Company ID,Company / group,Career / company URL,Role families to search")
    assert "Stability in a downturn (1-5, Claude estimate)" in header


def test_owner_owned_fields_never_appear(store, tmp_path):
    rep = export(store, tmp_path)
    allowed = {
        "companies_import": ({"company_id", "company"} | set(COMPANY_OWNED), COMPANIES),
        "jobs_import": ({"job_id"} | set(JOB_OWNED) | set(JOB_INSERT_ONLY), JOBS),
        "people_import": ({"person_id"} | set(PEOPLE_OWNED) | set(PEOPLE_INSERT_ONLY), PEOPLE),
    }
    for name, (keys, tab) in allowed.items():
        for row in parsed(rep, name, tab).rows:
            assert set(row) <= keys, set(row) - keys
    text = rep.files["companies_import"].read_text(encoding="utf-8")
    assert "secret owner note" not in text and "owner only" not in text and "Your overall rank" not in text


def test_research_maps_to_tracker_columns(store, tmp_path):
    rows = {r["company_id"]: r for r in parsed(export(store, tmp_path), "companies_import", COMPANIES).rows}
    c1 = rows["C1"]
    assert c1["company"] == "Acme, Inc."  # comma survives quoting
    assert c1["research_status"] == "Researched"
    assert c1["maine_remote"] == "yes: Remote anywhere in the US."
    assert c1["remote_likelihood"] == "High"
    assert c1["fit_score"] == 4 and c1["stability_score"] == 4 and c1["hiring_difficulty"] == 3
    assert c1["benefit_source"] == "https://news.example/revenue"  # revenue beats layoffs
    assert c1["role_families"] == "SDET; QA lead"
    assert c1["research_notes"].startswith("Remote US; profitable. Sources: https://acme.example/careers")
    assert c1["research_notes"].count("https://") == 3
    assert c1["next_research_action"] == "Watching greenhouse board"
    assert c1["estimate_confidence"] == "Medium"
    assert c1["people_search_url"] == ("https://www.linkedin.com/search/results/people/?keywords="
                                       "Director%20of%20QA%20Acme%2C%20Inc.")  # fmt: skip
    assert c1["research_as_of"] == c1["ai_scored_at"] == TODAY
    assert "ai_red_flags" not in c1  # empty stays blank, never overwrites
    assert rows["C2"]["research_status"] == "Initial research" and rows["C2"]["fit_score"] == 3


def test_skipped_companies_and_owner_status_are_left_alone(store, tmp_path):
    rep = export(store, tmp_path)
    rows = {r["company_id"]: r for r in parsed(rep, "companies_import", COMPANIES).rows}
    assert "C3" not in rows and "C4" not in rows  # parked, rank 4
    assert "research_status" not in rows["C5"]  # owner set Researched after the pipeline's last write
    assert rep.skipped_status == ["C5"]


def test_research_status_exported_when_owner_changed_it_before_our_last_write(store):
    rec = company("C9", "X", "Researching", research=copy.deepcopy(RESEARCH),
                  owner_changes={"research_status": {"value": "Researching", "at": "2026-10-01T00:00:00"}},
                  last_exported_at="2026-10-02T00:00:00")  # fmt: skip
    assert company_values(rec)["research_status"] == "Researched"
    rec["tracker"]["research_status"] = "Researched"
    rec["research"] = None
    rec["triage"] = TRIAGE
    assert "research_status" not in company_values(rec)  # never step back to Initial research


# --- jobs, people, IDs -----------------------------------------------------------------------------------


def test_jobs_watched_and_leads(store, tmp_path):
    rep = export(store, tmp_path)
    rows = parsed(rep, "jobs_import", JOBS).rows
    by_title = {r["job_title"]: r for r in rows}
    w = by_title["QA Manager"]
    assert w["job_id"] == "JW0001" and w["listing_evidence"] == "Verified live"
    assert w["company"] == "Acme, Inc."  # tracker's exact name, not the board's
    assert w["pay_low"] == 120000 and w["maine_eligible"] == "Yes" and w["date_discovered"] == TODAY
    assert w["track"] == "QA leadership" and w["current_stage"] == "Lead" and w["highest_stage"] == "None"
    lead = by_title["Senior SDET"]
    assert lead["listing_evidence"] == "Indexed lead - verify" and lead["job_id"] == "JW0002"
    assert rep.leads_added == 1


def test_lead_not_duplicated_when_watcher_has_the_url(store, tmp_path):
    rec = store.load_company("C1")
    rec["research"]["qa_openings"][0]["url"] = WATCHED_JOB["url"]
    store.save_company(rec)
    rep = export(store, tmp_path)
    assert rep.leads_added == 0
    assert [r["job_title"] for r in parsed(rep, "jobs_import", JOBS).rows] == ["QA Manager"]


def test_people_rows(store, tmp_path):
    rows = parsed(export(store, tmp_path), "people_import", PEOPLE).rows
    assert rows == [{"person_id": "PA0001", "name": "Dana Whitfield", "company": "Acme, Inc.",
                     "title": "VP of Engineering", "source_url": "https://acme.example/team",
                     "status": "To research", "relationship": "Cold",
                     "next_step": "Look up on LinkedIn (People search link)",
                     "why_approach": "VP of Engineering at Acme, Inc.. Regulated test automation for their platform."}]  # fmt: skip


def test_second_export_is_empty_and_insert_only_fields_appear_once(store, tmp_path):
    first = export(store, tmp_path)
    assert first.counts()["jobs_new"] == 2
    again = export(store, tmp_path, now="2026-10-05T04:00:00-04:00")
    assert again.counts() == {"companies": 0, "jobs_new": 0, "jobs_updated": 0, "people_new": 0,
                              "people_updated": 0, "leads_added": 0}  # fmt: skip
    assert again.files == {}

    # The board refreshes last_checked: only that cell (plus ID/company) is exported, no insert-only fields.
    job = read_json(store.data_dir / "jobs" / "greenhouse-1.json")
    job["last_seen"] = "2026-10-06"
    write_json(store.data_dir / "jobs" / "greenhouse-1.json", job)
    rep = export(store, tmp_path, now="2026-10-05T05:00:00-04:00")
    rows = parsed(rep, "jobs_import", JOBS).rows
    assert rows == [{"job_id": "JW0001", "company": "Acme, Inc.", "last_checked": "2026-10-06"}]
    assert rep.files["jobs_import"].name == "jobs_import-050000.csv"  # earlier file not overwritten


def test_only_changed_cells_are_exported(store, tmp_path):
    export(store, tmp_path)
    rec = store.load_company("C1")
    rec["research"]["fit"]["score"] = 5
    store.save_company(rec)
    rows = parsed(
        export(store, tmp_path, now="2026-10-05T06:00:00-04:00"), "companies_import", COMPANIES
    ).rows
    assert rows == [{"company_id": "C1", "company": "Acme, Inc.", "fit_score": 5}]


def test_closed_job_exported_once_as_historical(store, tmp_path):
    export(store, tmp_path)
    job = read_json(store.data_dir / "jobs" / "greenhouse-1.json")
    job["closed_at"] = "2026-10-07"
    write_json(store.data_dir / "jobs" / "greenhouse-1.json", job)
    rows = parsed(export(store, tmp_path, now="2026-10-05T07:00:00-04:00"), "jobs_import", JOBS).rows
    assert rows[0]["listing_evidence"] == "Historical lead - recheck"
    assert rows[0]["fit_notes"].startswith("Posting removed 2026-10-07. ")
    assert "current_stage" not in rows[0]  # stage never touched
    assert export(store, tmp_path, now="2026-10-05T08:00:00-04:00").counts()["jobs_updated"] == 0


def test_ids_never_reused(store, tmp_path):
    export(store, tmp_path)
    (store.data_dir / "jobs" / "greenhouse-1.json").unlink()
    write_json(
        store.data_dir / "jobs" / "greenhouse-2.json",
        dict(WATCHED_JOB, key="greenhouse-2", url="https://x/2"),
    )
    rows = parsed(export(store, tmp_path, now="2026-10-05T09:00:00-04:00"), "jobs_import", JOBS).rows
    assert [r["job_id"] for r in rows] == ["JW0003"]
    assert read_json(store.data_dir / "ids.json") == {"job": 3, "person": 1}


def test_dry_run_writes_and_marks_nothing(store, tmp_path):
    rep = run_export(store, _cfg(), today=TODAY, now=NOW, out_root=tmp_path / "out", dry_run=True)
    assert rep.counts()["companies"] == 3
    assert rep.files["companies_import"] == tmp_path / "out" / TODAY / "preview" / "companies_import.csv"
    preview = parsed(rep, "jobs_import", JOBS).rows
    assert store.load_company("C1")["exported"] == {}
    assert read_json(store.data_dir / "jobs" / "greenhouse-1.json")["job_id"] is None
    assert not (store.data_dir / "ids.json").exists()
    real = export(store, tmp_path)
    assert real.counts()["companies"] == 3
    assert parsed(real, "jobs_import", JOBS).rows == preview  # the preview is exactly what gets exported


# --- digest ---------------------------------------------------------------------------------------------


def test_digest_sections(store, tmp_path):
    rec = store.load_company("C2")
    rec["research"] = copy.deepcopy(RESEARCH) | {"company_id": "C2", "qa_openings": [], "people": []}
    store.save_company(rec)
    store.save_company(store.load_company("C2") | {"ats": {"type": "other", "token": None}})
    export(store, tmp_path)
    write_json(
        store.data_dir / "rejects" / "C7-20261005T020000.json", {"detail": "extract: grounding failed"}
    )
    log = tmp_path / "runs.jsonl"
    log.write_text('{"at": "2026-10-05T01:00:00", "command": "watch", "counts": {"new": 1}, "failed": ["C9 lever:x: 500"], "duration_s": 3}\n'
                   '{"at": "2026-10-04T01:00:00", "command": "watch", "counts": {}, "failed": ["old"]}\n',
                   encoding="utf-8")  # fmt: skip
    md = build_digest(store, TODAY, runs_log=log)
    for heading in ("## 1. New jobs (2)", "## 2. Companies verified today (2)", "## 3. People found (1)",
                    "## 4. No supported job board, check by hand (1)", "## 5. Rejects (1)", "## 6. Errors (1)",
                    "## 7. Run totals"):  # fmt: skip
        assert heading in md, heading
    assert "[QA Manager](https://job-boards.greenhouse.io/acme/jobs/1)" in md
    assert "Tiny Games: https://acme.example/careers" in md
    assert "C9 lever:x: 500" in md and "old" not in md
    path = write_digest(store, TODAY, out_root=tmp_path / "out", runs_log=log)
    assert path.read_text(encoding="utf-8") == md
    assert (path.parent / "new_companies.md").exists()


def test_people_failing_current_rules_are_not_exported_and_unexported_records_are_removed(store, tmp_path):
    rec = store.load_company("C1")
    rec["research"]["people"] += [
        {"name": "Jeff Smith", "title": "QA head", "source_url": "https://www.thelayoff.com/acme"},
        {"name": "Jo B.", "title": "Engineering Manager", "source_url": "https://acme.example/team"},
        {"name": "Sam Ortiz", "title": "Senior Quality Engineer", "source_url": "https://acme.example/team"},
    ]
    rec["research"]["sources"] += ["https://www.thelayoff.com/acme"]
    store.save_company(rec)
    stale = store.data_dir / "people" / "stale.json"
    from jobsearch.export import _key

    stale = store.data_dir / "people" / f"{_key('C1', 'Sam Ortiz')}.json"
    write_json(stale, {"key": stale.stem, "company_id": "C1", "name": "Sam Ortiz", "person_id": None})
    rows = parsed(export(store, tmp_path), "people_import", PEOPLE).rows
    assert [r["name"] for r in rows] == ["Dana Whitfield"]
    assert not stale.exists()


def test_skip_url_blocks_anonymous_forums():
    from jobsearch.evidence import skip_url

    assert skip_url("https://www.thelayoff.com/becton-dickinson") and skip_url("https://www.teamblind.com/x")


def test_tracker_career_url_is_not_overwritten(store, tmp_path):
    rec = store.load_company("C1")
    rec["tracker"]["career_url"] = "https://acme.example/jobs"
    store.save_company(rec)
    rows = {r["company_id"]: r for r in parsed(export(store, tmp_path), "companies_import", COMPANIES).rows}
    assert "career_url" not in rows["C1"]


def test_leads_get_the_title_filter_and_stale_leads_are_removed(store, tmp_path):
    stale = store.data_dir / "jobs" / "lead-stale.json"
    write_json(stale, {"key": "lead-stale", "company_id": "C1", "title": "QA Intern", "url": "https://x/i",
                       "job_id": None})  # fmt: skip
    rec = store.load_company("C1")
    rec["research"]["qa_openings"].append(
        {
            "title": "Supplier Quality Engineer",
            "location": "Ohio",
            "work_mode": "On-site",
            "url": "https://x/s",
        }
    )
    store.save_company(rec)
    rep = export(store, tmp_path)
    assert rep.leads_added == 1  # Senior SDET only
    assert not stale.exists()
