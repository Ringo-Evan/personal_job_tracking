import json
import os
from pathlib import Path

import pytest

from jobsearch.ingest import PartialExportError, ingest_companies, ingest_inbox
from jobsearch.models import CompanyRecord, is_skipped
from jobsearch.store import Store
from jobsearch.tracker_csv import COMPANIES, COMPANY_LABELS, parse_file, parse_text

FIXTURES = Path(__file__).parent / "fixtures"
SAMPLE = FIXTURES / "tracker_companies_sample.csv"
EXPECTED = json.loads((FIXTURES / "tracker_companies_sample.expected.json").read_text(encoding="utf-8"))
T0 = "2026-10-05T08:00:00-04:00"
T1 = "2026-10-06T08:00:00-04:00"


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "data")


def snapshot(store: Store) -> dict[str, bytes]:
    return {p.name: p.read_bytes() for p in sorted(store.companies_dir.glob("*.json"))}


def csv_text(rows: list[dict]) -> str:
    keys = list(COMPANY_LABELS)
    lines = [",".join(f'"{COMPANY_LABELS[k]}"' for k in keys)]
    lines += [",".join(f'"{r.get(k, "")}"' for k in keys) for r in rows]
    return "\n".join(lines) + "\n"


def ingest_rows(store, rows, at, **kw):
    return ingest_companies(parse_text(csv_text(rows), COMPANIES), store, at=at, **kw)


# --- the sample export ---------------------------------------------------------------------------


def test_sample_parse_reports_ignored_columns_and_warnings():
    parsed = parse_file(SAMPLE)
    assert parsed.tab is COMPANIES
    assert parsed.header_row_index == 3
    assert parsed.ignored_columns == ["Extra column"]
    assert any("blank Company ID" in w for w in parsed.warnings)
    assert any("hiring_difficulty='high'" in w for w in parsed.warnings)


def test_sample_ingest_produces_expected_json(store):
    report = ingest_companies(parse_file(SAMPLE), store, at=T0)
    expected_ids = sorted(k for k in EXPECTED if not k.startswith("_"))
    assert sorted(report.created) == expected_ids
    assert store.company_ids() == expected_ids

    for cid in expected_ids:
        rec = store.load_company(cid)
        CompanyRecord.model_validate(rec)  # stored shape is valid
        exp = EXPECTED[cid]
        assert set(rec["tracker"]) == set(COMPANY_LABELS), cid
        non_null = {k: v for k, v in rec["tracker"].items() if v is not None}
        assert non_null == exp["tracker"], cid
        assert rec["owner_changes"] == exp["owner_changes"], cid
        assert is_skipped(rec) is exp["skipped"], cid
        assert rec["missing_from_tracker"] is False
        assert rec["history"] == [{"at": T0, "stage": "ingest", "changed": ["created"]}]
        assert rec["ats"] is rec["triage"] is rec["research"] is rec["last_exported_at"] is None


def test_reingest_same_file_is_noop(store):
    ingest_companies(parse_file(SAMPLE), store, at=T0)
    before = snapshot(store)
    mtimes = {p.name: p.stat().st_mtime_ns for p in store.companies_dir.glob("*.json")}

    report = ingest_companies(parse_file(SAMPLE), store, at=T1)

    assert not report.changed
    assert report.unchanged == 4
    assert snapshot(store) == before
    assert {p.name: p.stat().st_mtime_ns for p in store.companies_dir.glob("*.json")} == mtimes


def test_written_json_has_no_temp_files_left(store):
    ingest_companies(parse_file(SAMPLE), store, at=T0)
    assert [p.name for p in store.companies_dir.iterdir() if p.suffix != ".json"] == []


# --- updates and owner changes -------------------------------------------------------------------


def test_update_records_history_and_replaces_tracker_block(store):
    ingest_rows(store, [{"company_id": "C001", "company": "Acme", "sector": "Games"}], T0)
    report = ingest_rows(store, [{"company_id": "C001", "company": "Acme", "sector": "Sports"}], T1)
    assert report.updated == ["C001"]
    rec = store.load_company("C001")
    assert rec["tracker"]["sector"] == "Sports"
    assert rec["history"][-1] == {"at": T1, "stage": "ingest", "changed": ["sector"]}


def test_owner_status_and_rank_changes_are_detected(store):
    ingest_rows(store, [{"company_id": "C001", "research_status": "Not started"}], T0)
    report = ingest_rows(
        store, [{"company_id": "C001", "research_status": "Researching", "overall_rank": "2"}], T1
    )
    assert sorted(report.owner_changes) == [
        ("C001", "overall_rank", None, 2),
        ("C001", "research_status", "Not started", "Researching"),
    ]
    rec = store.load_company("C001")
    assert rec["owner_changes"]["research_status"] == {"value": "Researching", "at": T1}
    assert rec["owner_changes"]["overall_rank"] == {"value": 2, "at": T1}


def test_value_the_pipeline_exported_is_not_an_owner_change(store):
    ingest_rows(store, [{"company_id": "C001", "research_status": "Not started"}], T0)
    rec = store.load_company("C001")
    rec["exported"] = {"research_status": "Researched"}
    store.save_company(rec)

    report = ingest_rows(store, [{"company_id": "C001", "research_status": "Researched"}], T1)

    assert report.updated == ["C001"]
    assert report.owner_changes == []
    assert store.load_company("C001")["owner_changes"]["research_status"]["value"] == "Not started"


def test_ingest_preserves_pipeline_blocks(store):
    ingest_rows(store, [{"company_id": "C001", "sector": "A"}], T0)
    rec = store.load_company("C001")
    rec["ats"] = {"type": "greenhouse", "token": "acme"}
    rec["triage"] = {"fit": 4}
    store.save_company(rec)
    ingest_rows(store, [{"company_id": "C001", "sector": "B"}], T1)
    rec = store.load_company("C001")
    assert rec["ats"] == {"type": "greenhouse", "token": "acme"}
    assert rec["triage"] == {"fit": 4}


@pytest.mark.parametrize(
    ("status", "rank", "skipped"),
    [
        ("Parked", "", True),
        ("Not started", "4", True),
        ("Not started", "5", True),
        ("Not started", "3", False),
        ("Researched", "", False),
        ("Not started", "", False),
    ],
)
def test_skip_rules(store, status, rank, skipped):
    ingest_rows(store, [{"company_id": "C001", "research_status": status, "overall_rank": rank}], T0)
    assert is_skipped(store.load_company("C001")) is skipped


# --- vanished companies --------------------------------------------------------------------------


def test_missing_company_is_flagged_never_deleted_then_restored(store):
    rows = [{"company_id": "C001"}, {"company_id": "C002"}]
    ingest_rows(store, rows, T0)

    report = ingest_rows(store, rows[:1], T1, allow_partial=True)
    assert report.marked_missing == ["C002"]
    assert store.load_company("C002")["missing_from_tracker"] is True

    again = ingest_rows(store, rows[:1], T1, allow_partial=True)
    assert again.marked_missing == [] and not again.changed

    back = ingest_rows(store, rows, T1)
    assert back.restored == ["C002"]
    assert store.load_company("C002")["missing_from_tracker"] is False


def test_filtered_export_is_refused(store):
    ingest_rows(store, [{"company_id": f"C{i:03d}"} for i in range(1, 11)], T0)
    before = snapshot(store)
    with pytest.raises(PartialExportError, match="filtered"):
        ingest_rows(store, [{"company_id": "C001"}], T1)
    assert snapshot(store) == before


def test_duplicate_ids_keep_first_row(store):
    report = ingest_rows(
        store, [{"company_id": "C001", "sector": "A"}, {"company_id": "C001", "sector": "B"}], T0
    )
    assert store.load_company("C001")["tracker"]["sector"] == "A"
    assert any("duplicate" in w for w in report.warnings)


# --- inbox handling ------------------------------------------------------------------------------


def test_inbox_skips_already_ingested_files_and_other_tabs(store, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    (inbox / "companies.csv").write_bytes(SAMPLE.read_bytes())
    jobs = inbox / "jobs.csv"
    jobs.write_text("Job ID,Job title\nJ001,SDET\n", encoding="utf-8")
    os.utime(jobs, (1, 1))  # oldest

    first = ingest_inbox(store, inbox, at=T0)
    assert [r.source for r in first] == ["jobs.csv", "companies.csv"]
    assert "jobs export" in first[0].skipped_files[0]
    assert len(first[1].created) == 4

    second = ingest_inbox(store, inbox, at=T1)
    companies = next(r for r in second if r.source == "companies.csv")
    assert companies.skipped_files == ["companies.csv: already ingested"]

    forced = ingest_inbox(store, inbox, force=True, at=T1)
    assert not next(r for r in forced if r.source == "companies.csv").changed


def test_bom_encoded_export(store, tmp_path):
    f = tmp_path / "bom.csv"
    f.write_bytes("﻿Company ID,Company / group\nC001,Acme\n".encode())
    ingest_inbox(store, tmp_path, files=[f], at=T0)
    assert store.load_company("C001")["tracker"]["company"] == "Acme"


def test_company_ids_sort_numerically(store):
    for cid in ("C1000", "C101", "C2", "C010"):
        store.save_company({"company_id": cid, "tracker": {}, "history": []})
    assert store.company_ids() == ["C2", "C010", "C101", "C1000"]
