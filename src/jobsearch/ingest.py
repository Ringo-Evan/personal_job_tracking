"""Refresh data/companies/*.json from the tracker (plan phase 1; plan-tracker.md).

The tracker's company rows in data/tracker/ are the source now. A tracker CSV export can still be read once by hand
(`ingest --file`), the original path.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from jobsearch.models import CompanyRecord
from jobsearch.store import Store, read_json, write_json
from jobsearch.tracker import Tracker
from jobsearch.tracker_csv import COMPANIES, COMPANY_LABELS, ParseResult, detect_tab, parse_text, read_file

# Owner-set fields whose changes drive the section 5.3 exceptions.
WATCHED_FIELDS = ("research_status", "overall_rank")

# An export holding fewer than this share of known companies was probably filtered in the tracker.
PARTIAL_EXPORT_RATIO = 0.5


class PartialExportError(RuntimeError):
    pass


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


@dataclass
class IngestReport:
    source: str = ""
    rows: int = 0
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    marked_missing: list[str] = field(default_factory=list)
    restored: list[str] = field(default_factory=list)
    owner_changes: list[tuple[str, str, Any, Any]] = field(default_factory=list)  # (id, field, old, new)
    ignored_columns: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)

    @property
    def changed(self) -> bool:
        return bool(self.created or self.updated or self.marked_missing or self.restored)

    def counts(self) -> dict[str, int]:
        return {
            "rows": self.rows,
            "created": len(self.created),
            "updated": len(self.updated),
            "unchanged": self.unchanged,
            "marked_missing": len(self.marked_missing),
            "restored": len(self.restored),
            "owner_changes": len(self.owner_changes),
            "warnings": len(self.warnings),
        }


def _new_record(row: dict[str, Any], at: str) -> dict[str, Any]:
    owner_changes = {f: {"value": row[f], "at": at} for f in WATCHED_FIELDS if row.get(f) is not None}
    rec = CompanyRecord(
        company_id=row["company_id"],
        tracker=row,
        owner_changes=owner_changes,
        history=[{"at": at, "stage": "ingest", "changed": ["created"]}],
    )
    return rec.model_dump()


def ingest_companies(
    parsed: ParseResult, store: Store, at: str | None = None, allow_partial: bool = False
) -> IngestReport:
    """Upsert parsed company rows. Idempotent: the same rows a second time write nothing."""
    if parsed.tab is not COMPANIES:
        raise ValueError(f"expected a Companies export, got {parsed.tab.name}")
    at = at or now_iso()
    report = IngestReport(
        rows=len(parsed.rows), ignored_columns=parsed.ignored_columns, warnings=list(parsed.warnings)
    )

    seen: dict[str, dict[str, Any]] = {}
    for row in parsed.rows:
        cid = row["company_id"]
        if cid in seen:
            report.warnings.append(f"duplicate Company ID {cid}; kept the first row")
            continue
        seen[cid] = row

    known = set(store.company_ids())
    present_known = sum(1 for cid in known if not (store.load_company(cid) or {}).get("missing_from_tracker"))
    if not allow_partial and present_known and len(seen) < present_known * PARTIAL_EXPORT_RATIO:
        raise PartialExportError(
            f"export has {len(seen)} companies but {present_known} are known; it looks filtered. "
            "Clear the tracker's search and filters and export again, or pass --allow-partial."
        )

    for cid, row in seen.items():
        old = store.load_company(cid)
        if old is None:
            store.save_company(_new_record(row, at))
            report.created.append(cid)
            continue

        rec = CompanyRecord.model_validate(old).model_dump()
        changed = sorted(k for k in set(rec["tracker"]) | set(row) if rec["tracker"].get(k) != row.get(k))
        if rec["missing_from_tracker"]:
            rec["missing_from_tracker"] = False
            changed.append("missing_from_tracker")
            report.restored.append(cid)
        if not changed:
            report.unchanged += 1
            continue

        for f in WATCHED_FIELDS:
            new_val = row.get(f)
            if f in changed and new_val is not None and new_val != rec["exported"].get(f):
                rec["owner_changes"][f] = {"value": new_val, "at": at}
                report.owner_changes.append((cid, f, rec["tracker"].get(f), new_val))
        rec["tracker"] = row
        rec["history"].append({"at": at, "stage": "ingest", "changed": changed})
        store.save_company(CompanyRecord.model_validate(rec).model_dump())
        report.updated.append(cid)

    for cid in sorted(known - set(seen)):
        rec = store.load_company(cid)
        if rec.get("missing_from_tracker"):
            continue
        rec["missing_from_tracker"] = True
        rec["history"].append({"at": at, "stage": "ingest", "changed": ["missing_from_tracker"]})
        store.save_company(rec)
        report.marked_missing.append(cid)

    return report


def tracker_rows(tracker: Tracker) -> ParseResult:
    """The tracker's company rows in the shape a CSV export parses to: every Companies field, blanks as None."""
    rows = [
        {k: None if r.get(k) in (None, "") else r[k] for k in COMPANY_LABELS} for r in tracker.load("companies")
    ]
    return ParseResult(COMPANIES, rows, 0, [], [])


def ingest_tracker(store: Store, tracker: Tracker, at: str | None = None) -> IngestReport:
    """Refresh every company's `tracker` block from the tracker's rows, the way a CSV export was ingested: owner
    changes to status and rank are recorded, and a company gone from the tracker is marked missing, never deleted."""
    parsed = tracker_rows(tracker)
    known = [cid for cid in store.company_ids() if not (store.load_company(cid) or {}).get("missing_from_tracker")]
    if known and len(parsed.rows) < len(known) * PARTIAL_EXPORT_RATIO:
        # Most likely a damaged or half-restored data/tracker/: refuse rather than mark everything missing.
        raise PartialExportError(
            f"the tracker holds {len(parsed.rows)} companies but {len(known)} are known; refusing to mark the rest "
            f"missing. Check {tracker.root / 'companies'}."
        )
    report = ingest_companies(parsed, store, at=at, allow_partial=True)
    report.source = "tracker"
    return report


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def ingest_inbox(
    store: Store,
    inbox: Path,
    files: list[Path] | None = None,
    force: bool = False,
    allow_partial: bool = False,
    at: str | None = None,
) -> list[IngestReport]:
    """Ingest Companies exports from inbox/ (oldest first), skipping files already ingested."""
    state = read_json(store.ingest_state_path) or {"files": {}}
    paths = files if files is not None else sorted(inbox.glob("*.csv"), key=lambda p: p.stat().st_mtime)
    reports = []
    for path in paths:
        digest = _sha256(path)
        if digest in state["files"] and not force:
            r = IngestReport(source=path.name)
            r.skipped_files.append(f"{path.name}: already ingested")
            reports.append(r)
            continue
        text = read_file(path)
        tab = detect_tab(text)
        if tab is not COMPANIES:
            r = IngestReport(source=path.name)
            what = tab.name if tab else "unrecognized"
            r.skipped_files.append(f"{path.name}: {what} export; not used until a later phase")
            reports.append(r)
            continue
        report = ingest_companies(parse_text(text, tab), store, at=at, allow_partial=allow_partial)
        report.source = path.name
        reports.append(report)
        state["files"][digest] = {"name": path.name, "tab": tab.name, "rows": report.rows}
        write_json(store.ingest_state_path, state)
    return reports
