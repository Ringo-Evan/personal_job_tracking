"""The job-search tracker (plan-tracker.md): one JSON document per row in data/tracker/<collection>/<ID>.json.

These are the rows the claude.ai tracker kept, with the same keys plus updated_by and updated_at. Every writer goes
through `Tracker.write`, which enforces who may change what: the owner ("you") anything; any other writer is an agent
(the pipeline writes as "pipeline") and may change only the agent-maintained fields, plus the insert-only ones when it
creates the row. Each change is appended to history.jsonl with its old values, under a lock so two processes can't
interleave a read-modify-write.
"""

from __future__ import annotations

import csv
import io
import json
import re
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from jobsearch.config import ROOT
from jobsearch.nightly import AlreadyRunning, run_lock
from jobsearch.store import id_sort_key, read_json, write_json
from jobsearch.tracker_csv import (
    ACTIVITY_LABELS,
    COMPANY_LABELS,
    JOB_LABELS,
    MATERIALS_LABELS,
    PEOPLE_LABELS,
    RESEARCH_STATUSES,
    normalize_label,
    parse_number,
)

TRACKER = ROOT / "data" / "tracker"
OWNER = "you"
PIPELINE = "pipeline"

STAGES = ("Lead", "Verified", "Applied", "Screen", "Interview", "Offer", "Accepted", "Rejected", "Withdrawn", "Closed")
CLOSED_STAGES = frozenset({"Accepted", "Rejected", "Withdrawn", "Closed"})
TRACKS = ("Core QA", "QA leadership", "SDE pivot", "Domain pivot", "Exploratory")
EVIDENCE = (
    "Employer page read", "Indexed lead - verify", "Historical lead - recheck", "Conflicting status - verify",
    "Verified live",
)  # fmt: skip
WORK_MODES = ("Remote", "Hybrid", "On-site", "Unknown")
REMOTE = ("High", "Medium", "Low", "Unknown")
PEOPLE_STATUSES = ("To research", "To contact", "Contacted", "Replied", "Meeting", "Referral", "Parked")

# Field ownership (plan.md 5.3, plan-tracker.md). Agent-maintained: any writer may set these at any time.
COMPANY_OWNED = (
    "career_url", "role_families", "hiring_difficulty", "estimate_confidence", "estimate_rationale", "research_notes",
    "research_status", "maine_remote", "benefit_source", "research_as_of", "next_research_action", "fit_score",
    "stability_score", "remote_likelihood", "size_estimate", "ai_summary", "ai_red_flags", "ai_scored_at",
    "approach_role", "people_search_url", "outreach_notes",
)  # fmt: skip
JOB_OWNED = (
    "company", "job_title", "posting_url", "application_url", "source_url", "listing_evidence", "last_checked",
    "location", "work_mode", "maine_eligible", "pay_low", "pay_high", "currency", "pay_period", "skill_match",
    "fit_notes",
)  # fmt: skip
PEOPLE_OWNED = ("name", "company", "title", "profile_url", "source_url", "why_approach")
# Insert-only: agents set these only when they create the row, so the owner's later changes are never reset.
JOB_INSERT_ONLY = ("track", "current_stage", "highest_stage", "next_action", "date_discovered")
PEOPLE_INSERT_ONLY = ("status", "relationship", "next_step")
COMPANY_INSERT_ONLY = ("company", "sector", "interests")  # an agent adding a company names and files it

# Near-duplicate sectors the claude.ai tracker accumulated (its write-up: "Merge them during migration").
SECTOR_MERGE = {
    "Labor": "Labor & organizing",
    "Maine employers": "Maine & New England",
    "Martial arts": "Martial arts & fitness",
}


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    kind: str  # text | long | num | date | url | select
    group: str
    options: tuple[str, ...] = ()


@dataclass(frozen=True)
class Collection:
    name: str
    title: str
    id_key: str
    prefix: str  # owner-entered IDs: the prefix plus 3 digits
    fields: tuple[Field, ...]
    columns: tuple[str, ...]  # the page's default table columns; "_due" and "pay" are computed
    agent: frozenset[str] = frozenset()
    insert_only: frozenset[str] = frozenset()
    defaults: tuple[tuple[str, str], ...] = ()  # set on a new row when the writer leaves them out

    @property
    def keys(self) -> frozenset[str]:
        return frozenset(f.key for f in self.fields)

    def field(self, key: str) -> Field | None:
        return next((f for f in self.fields if f.key == key), None)


def _fields(labels: dict[str, str], spec: list[tuple]) -> tuple[Field, ...]:
    return tuple(Field(key, labels[key], kind, group, tuple(opts[0]) if opts else ()) for key, kind, group, *opts in spec)


_STATUS, _LISTING, _FIT, _MC = "Status", "Listing", "Fit", "Materials & contacts"
JOBS = Collection(
    "jobs", "Jobs", "job_id", "J",
    _fields(JOB_LABELS, [
        ("job_id", "text", "Basics"), ("company", "text", "Basics"), ("company_id", "text", "Basics"),
        ("job_title", "text", "Basics"), ("track", "select", "Basics", TRACKS), ("job_rank", "num", "Basics"),
        ("current_stage", "select", _STATUS, STAGES), ("highest_stage", "select", _STATUS, ("None", *STAGES)),
        ("next_action", "text", _STATUS), ("follow_up_due", "date", _STATUS), ("date_applied", "date", _STATUS),
        ("application_method", "text", _STATUS), ("deadline", "date", _STATUS),
        ("posting_url", "url", _LISTING), ("application_url", "url", _LISTING), ("source_url", "url", _LISTING),
        ("listing_evidence", "select", _LISTING, EVIDENCE), ("last_checked", "date", _LISTING),
        ("date_discovered", "date", _LISTING),
        ("location", "text", _FIT), ("work_mode", "select", _FIT, WORK_MODES), ("maine_eligible", "text", _FIT),
        ("travel_fraction", "text", _FIT), ("pay_low", "num", _FIT), ("pay_high", "num", _FIT),
        ("currency", "text", _FIT), ("pay_period", "text", _FIT), ("hiring_difficulty", "num", _FIT),
        ("skill_match", "num", _FIT), ("fit_notes", "long", _FIT),
        ("resume_version_id", "text", _MC), ("resume_link", "url", _MC), ("cover_version_id", "text", _MC),
        ("cover_link", "url", _MC), ("recruiter_contact", "text", _MC), ("referral", "text", _MC),
        ("application_confirmation", "text", _MC), ("last_contact_date", "date", _MC),
        ("next_interview_date", "date", _MC), ("outcome_feedback", "long", _MC), ("role_research", "long", _MC),
    ]),
    ("job_id", "company", "job_title", "track", "current_stage", "_due", "next_action", "location", "work_mode",
     "maine_eligible", "pay", "hiring_difficulty", "listing_evidence", "date_discovered"),
    agent=frozenset((*JOB_OWNED, "company_id")),
    insert_only=frozenset(JOB_INSERT_ONLY),
    defaults=(("current_stage", "Lead"), ("highest_stage", "None")),
)  # fmt: skip
_SCORES, _RESEARCH, _SCREEN, _OUTREACH = "Your scores", "Research", "Screening", "Outreach"
COMPANIES = Collection(
    "companies", "Companies", "company_id", "C",
    _fields(COMPANY_LABELS, [
        ("company_id", "text", "Basics"), ("company", "text", "Basics"), ("sector", "text", "Basics"),
        ("interests", "text", "Basics"), ("career_url", "url", "Basics"), ("role_families", "text", "Basics"),
        ("overall_rank", "num", _SCORES), ("interest", "num", _SCORES), ("values", "num", _SCORES),
        ("pay", "num", _SCORES), ("flexibility", "num", _SCORES), ("travel_perks", "num", _SCORES),
        ("homestead_fit", "num", _SCORES), ("dealbreakers", "long", _SCORES),
        ("hiring_difficulty", "num", _RESEARCH), ("estimate_confidence", "text", _RESEARCH),
        ("estimate_rationale", "long", _RESEARCH), ("research_notes", "long", _RESEARCH),
        ("your_notes", "long", _RESEARCH), ("research_status", "select", _RESEARCH, RESEARCH_STATUSES),
        ("maine_remote", "text", _RESEARCH), ("benefit_source", "url", _RESEARCH), ("research_as_of", "date", _RESEARCH),
        ("next_research_action", "text", _RESEARCH),
        ("fit_score", "num", _SCREEN), ("stability_score", "num", _SCREEN),
        ("remote_likelihood", "select", _SCREEN, REMOTE), ("size_estimate", "text", _SCREEN),
        ("ai_summary", "long", _SCREEN), ("ai_red_flags", "long", _SCREEN), ("ai_scored_at", "date", _SCREEN),
        ("approach_role", "text", _OUTREACH), ("people_search_url", "url", _OUTREACH),
        ("outreach_notes", "long", _OUTREACH),
    ]),
    ("company_id", "company", "sector", "interests", "fit_score", "stability_score", "remote_likelihood",
     "overall_rank", "research_status"),
    agent=frozenset(COMPANY_OWNED),
    insert_only=frozenset(COMPANY_INSERT_ONLY),
    defaults=(("research_status", "Not started"),),
)  # fmt: skip
PEOPLE = Collection(
    "people", "People", "person_id", "P",
    _fields(PEOPLE_LABELS, [
        ("person_id", "text", "Who"), ("name", "text", "Who"), ("company", "text", "Who"),
        ("company_id", "text", "Who"), ("title", "text", "Who"), ("profile_url", "url", "Who"),
        ("source_url", "url", "Who"),
        ("status", "select", _OUTREACH, PEOPLE_STATUSES), ("relationship", "text", _OUTREACH),
        ("last_contact_date", "date", _OUTREACH), ("next_step", "text", _OUTREACH),
        ("next_step_date", "date", _OUTREACH), ("why_approach", "long", _OUTREACH), ("notes", "long", _OUTREACH),
    ]),
    ("person_id", "name", "company", "title", "status", "next_step", "next_step_date"),
    agent=frozenset((*PEOPLE_OWNED, "company_id")),
    insert_only=frozenset(PEOPLE_INSERT_ONLY),
    defaults=(("status", "To research"), ("relationship", "Cold")),
)  # fmt: skip
ACTIVITY = Collection(
    "activity", "Activity", "activity_id", "A",
    _fields(ACTIVITY_LABELS, [
        ("activity_id", "text", "Event"), ("job_id", "text", "Event"), ("company", "text", "Event"),
        ("company_id", "text", "Event"), ("date", "date", "Event"), ("type", "text", "Event"),
        ("stage_round", "text", "Event"), ("contact", "text", "Event"), ("channel", "text", "Event"),
        ("preparation", "long", "Details"), ("questions_asked", "long", "Details"),
        ("your_answers", "long", "Details"), ("feedback_outcome", "long", "Details"),
        ("next_action", "text", "Details"), ("next_action_date", "date", "Details"), ("notes_url", "url", "Details"),
    ]),
    ("activity_id", "date", "job_id", "company", "type", "stage_round", "next_action", "next_action_date"),
)  # fmt: skip
MATERIALS = Collection(
    "materials", "Materials", "version_id", "R",
    _fields(MATERIALS_LABELS, [
        ("version_id", "text", "Version"), ("doc_type", "text", "Version"), ("file_name", "text", "Version"),
        ("doc_url", "url", "Version"), ("created_date", "date", "Version"), ("target_role", "text", "Version"),
        ("key_changes", "long", "Version"), ("base_version_id", "text", "Version"), ("notes", "long", "Version"),
    ]),
    ("version_id", "doc_type", "file_name", "target_role", "created_date", "base_version_id"),
)  # fmt: skip
# In the page's tab order. Activity and Materials are the owner's alone: no agent fields at all.
COLLECTIONS = {c.name: c for c in (JOBS, COMPANIES, PEOPLE, ACTIVITY, MATERIALS)}


class TrackerError(ValueError):
    """A write the tracker refuses: an unknown row or field, a duplicate or changed ID, a busy lock."""


class OwnershipError(TrackerError):
    """A writer tried to change a field it doesn't own."""


class DuplicateError(TrackerError):
    """A new row's ID is already taken."""


class ConflictError(TrackerError):
    """A field changed after the writer read it. Nothing was written; `current` is the row as it is now."""

    def __init__(self, message: str, current: dict[str, Any] | None):
        super().__init__(message)
        self.current = current


def now_utc() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def collection(name: str) -> Collection:
    try:
        return COLLECTIONS[name]
    except KeyError:
        raise TrackerError(f"no collection {name!r}; choose from {', '.join(COLLECTIONS)}") from None


_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _check_id(row_id: str) -> None:
    if not _ID.match(row_id or ""):
        raise TrackerError(f"not a valid row ID: {row_id!r}")


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _same(a: Any, b: Any) -> bool:
    return (None if _blank(a) else a) == (None if _blank(b) else b)


def normalize(f: Field, value: Any) -> Any:
    """Trim text. Number fields hold numbers ("141,000" becomes 141000), kept as text when they don't parse."""
    if not isinstance(value, str):
        return value
    text = value.strip()
    if f.kind == "num" and text:
        n = parse_number(text)
        return text if n is None else n
    return text


def name_key(name: Any) -> str:
    """The claude.ai tracker's company-name key: lowercase, without parenthesized text, letters and digits only."""
    return re.sub(r"[^a-z0-9]", "", re.sub(r"\(.*?\)", "", str(name or "").lower()))


def _raise_highest(row: dict[str, Any], diff: dict[str, list[Any]]) -> None:
    """The highest stage follows the current stage forward: never back, never to Lead or a closed stage."""
    stage, high = row.get("current_stage"), row.get("highest_stage")
    if stage not in STAGES or stage == "Lead" or stage in CLOSED_STAGES:
        return
    if high in STAGES and STAGES.index(high) >= STAGES.index(stage):
        return
    diff["highest_stage"] = [high, stage]
    row["highest_stage"] = stage


@dataclass
class MigrateReport:
    created: Counter[str] = field(default_factory=Counter)
    unchanged: Counter[str] = field(default_factory=Counter)
    differs: list[str] = field(default_factory=list)  # already in the store and different: left alone
    sectors_merged: int = 0
    linked: int = 0  # jobs, people and activity given the company_id their company name matches
    unlinked: list[str] = field(default_factory=list)
    unknown_fields: Counter[str] = field(default_factory=Counter)  # kept as they are, but not in the schema


@dataclass
class ImportReport:
    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: int = 0
    refused: list[str] = field(default_factory=list)  # "J007: why"
    ignored_columns: list[str] = field(default_factory=list)


def to_csv(name: str, rows: list[dict[str, Any]]) -> str:
    """Every row given, the tab's labels as headers, in field order: the old page's export without its filters."""
    col = collection(name)
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([f.label for f in col.fields])
    for row in rows:
        w.writerow(["" if row.get(f.key) is None else row[f.key] for f in col.fields])
    return out.getvalue()


def parse_csv(name: str, text: str) -> tuple[list[dict[str, str]], list[str]]:
    """A tracker CSV's rows by the old page's import rules. Returns (rows, ignored column labels).

    The header is the first row whose first cell is the tab's ID label, so title rows above it are skipped. Columns
    match by normalized label and unmatched ones are ignored. Rows without an ID are skipped, and blank cells are left
    out, so an import never blanks a value.
    """
    col = collection(name)
    grid = list(csv.reader(io.StringIO(text.lstrip("﻿"))))
    id_label = col.fields[0].label
    start = next((i for i, row in enumerate(grid) if row and normalize_label(row[0]) == normalize_label(id_label)), None)
    if start is None:
        raise TrackerError(f'that file has no "{id_label}" header row: export the {col.title} tab and try again')
    by_label = {normalize_label(f.label): f.key for f in col.fields}
    keys = [by_label.get(normalize_label(h)) for h in grid[start]]
    ignored = [h for h, k in zip(grid[start], keys, strict=False) if k is None and h.strip()]
    rows = []
    for raw in grid[start + 1 :]:
        if not raw or not raw[0].strip():
            continue
        row = {k: cell.strip() for k, cell in zip(keys, raw, strict=False) if k and cell.strip()}
        row[col.id_key] = raw[0].strip()
        rows.append(row)
    return rows, ignored


class Tracker:
    def __init__(self, root: Path = TRACKER):
        self.root = root

    def _path(self, name: str, row_id: str) -> Path:
        _check_id(row_id)
        return self.root / name / f"{row_id}.json"

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Held for each read-modify-write; other processes wait up to 10 seconds for it."""
        try:
            with run_lock(self.root / "write.lock", wait_s=10, poll_s=0.05):
                yield
        except AlreadyRunning as e:
            raise TrackerError("the tracker is busy with another write; try again") from e

    def load(self, name: str) -> list[dict[str, Any]]:
        col = collection(name)
        d = self.root / name
        rows = [r for p in d.glob("*.json") if (r := read_json(p))] if d.is_dir() else []
        return sorted(rows, key=lambda r: id_sort_key(str(r.get(col.id_key) or "")))

    def get(self, name: str, row_id: str) -> dict[str, Any] | None:
        collection(name)
        return read_json(self._path(name, row_id))

    def next_id(self, name: str, prefix: str | None = None, pad: int = 3) -> str:
        """The highest number among IDs with exactly this prefix, plus one: J037 after J036, whatever JW#### exist."""
        prefix = prefix or collection(name).prefix
        pattern = re.compile(rf"^{re.escape(prefix)}(\d+)$")
        d = self.root / name
        top = max((int(m.group(1)) for p in d.glob("*.json") if (m := pattern.match(p.stem))), default=0)
        return f"{prefix}{top + 1:0{pad}d}"

    def write(
        self,
        name: str,
        row_id: str,
        changes: dict[str, Any],
        *,
        by: str,
        seen: dict[str, Any] | None = None,
        create: bool = False,
    ) -> dict[str, Any]:
        """Merge `changes` into a row as writer `by`, enforcing field ownership. Returns the row as saved.

        seen: the values the writer last read for the fields it changes. If one of them has changed since, nothing is
        written and ConflictError carries the current row. Changes to other fields of the row don't conflict.
        create: the row must not exist yet; without it, the row must exist.
        """
        with self.lock():
            return self._write(collection(name), row_id, changes, by.strip(), seen, create, now_utc())

    def _write(
        self,
        col: Collection,
        row_id: str,
        changes: dict[str, Any],
        by: str,
        seen: dict[str, Any] | None,
        create: bool,
        now: str,
    ) -> dict[str, Any]:
        if not by:
            raise TrackerError("every write needs a writer name")
        current = self.get(col.name, row_id)
        if create and current is not None:
            raise DuplicateError(f"{row_id} already exists")
        if not create and current is None:
            raise TrackerError(f"there is no {col.name} row {row_id}")
        values: dict[str, Any] = {}
        for key, value in changes.items():
            if key in ("updated_by", "updated_at"):
                continue
            if key == col.id_key:
                if str(value).strip() != row_id:
                    raise TrackerError(f"an ID can't change ({row_id} to {value})")
                continue
            f = col.field(key)
            if f is None:
                raise TrackerError(f"{col.name} has no field {key!r}")
            values[key] = normalize(f, value)
        if create:
            for key, default in col.defaults:
                if _blank(values.get(key)):
                    values[key] = default
            if col is JOBS and _blank(values.get("date_discovered")):
                values["date_discovered"] = date.today().isoformat()  # local date, like every other pipeline date
        if by != OWNER:
            values = _agent_values(col, values, create)
        if seen:
            was = current or {}
            stale = [k for k in values if k in seen and not _same(normalize(col.field(k), seen[k]), was.get(k))]
            if stale:
                raise ConflictError(f"{row_id}: {', '.join(stale)} changed since it was read", current)
        row = dict(current) if current else {col.id_key: row_id}
        diff: dict[str, list[Any]] = {}
        for key, value in values.items():
            old = row.get(key)
            if _blank(value):
                if not _blank(old):
                    diff[key] = [old, None]
                    del row[key]
            elif old != value:
                diff[key] = [old, value]
                row[key] = value
        if col is JOBS and "current_stage" in diff:
            _raise_highest(row, diff)
        if current is not None and not diff:
            return current
        row["updated_by"], row["updated_at"] = by, now
        write_json(self._path(col.name, row_id), dict(sorted(row.items())))
        op = "create" if current is None else "update"
        self._log({"at": now, "by": by, "op": op, "collection": col.name, "id": row_id, "changes": diff})
        return row

    def delete(self, name: str, row_id: str, *, by: str) -> None:
        """Only the owner deletes. The deleted row is kept in history.jsonl."""
        if by != OWNER:
            raise OwnershipError("only you can delete rows")
        collection(name)
        with self.lock():
            current = self.get(name, row_id)
            if current is None:
                raise TrackerError(f"there is no {name} row {row_id}")
            self._path(name, row_id).unlink()
            self._log({"at": now_utc(), "by": by, "op": "delete", "collection": name, "id": row_id, "row": current})

    def _log(self, entry: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        with open(self.root / "history.jsonl", "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def stats(self) -> dict[str, Counter[str]]:
        """Rows per collection, and per writer of each row's last change."""
        return {name: Counter(r.get("updated_by") or "?" for r in self.load(name)) for name in COLLECTIONS}

    def migrate(self, src: Path) -> MigrateReport:
        """Load the claude.ai tracker's downloaded collections (`<src>/<collection>/<ID>.json`) as they are.

        Provenance (updated_by, updated_at) is kept. Near-duplicate sectors are merged, and jobs, people and activity get
        the company_id their company name matches. A row already in the store is never overwritten: a second run
        changes nothing, and a row that differs from the source is listed and left alone.
        """
        rep = MigrateReport()
        companies: dict[str, str] = {}
        with self.lock():
            for name in ("companies", "jobs", "people", "activity", "materials"):  # companies first, for the links
                col = COLLECTIONS[name]
                for p in sorted((src / name).glob("*.json")) if (src / name).is_dir() else []:
                    row = read_json(p)
                    if not isinstance(row, dict) or row.get(col.id_key) != p.stem:
                        raise TrackerError(f"{p}: expected a row whose {col.id_key} is {p.stem}")
                    rep.unknown_fields.update(
                        f"{name}.{k}" for k in row if k not in col.keys and k not in ("updated_by", "updated_at")
                    )
                    if name == "companies":
                        if row.get("sector") in SECTOR_MERGE:
                            row["sector"] = SECTOR_MERGE[row["sector"]]
                            rep.sectors_merged += 1
                        companies.setdefault(name_key(row.get("company")), p.stem)
                    elif "company_id" in col.keys and row.get("company") and not row.get("company_id"):
                        if cid := companies.get(name_key(row["company"])):
                            row["company_id"] = cid
                            rep.linked += 1
                        else:
                            rep.unlinked.append(f"{p.stem} ({row['company']})")
                    row = dict(sorted(row.items()))
                    existing = self.get(name, p.stem)
                    if existing is None:
                        write_json(self._path(name, p.stem), row)
                        rep.created[name] += 1
                    elif existing == row:
                        rep.unchanged[name] += 1
                    else:
                        rep.differs.append(f"{name}/{p.stem}")
            if schema := read_json(src / "meta" / "schema.json"):  # the old agent rules, kept for reference
                write_json(self.root / "meta" / "schema.json", schema)
            if rep.created:
                self._log({"at": now_utc(), "by": "migration", "op": "migrate", "source": str(src),
                           "created": dict(rep.created)})  # fmt: skip
        return rep

    def import_csv(self, name: str, text: str, *, by: str = OWNER) -> ImportReport:
        """Merge a tracker CSV into the tab by ID, as `by`: blank cells never overwrite, new IDs become new rows."""
        rows, ignored = parse_csv(name, text)
        rep = ImportReport(ignored_columns=ignored)
        id_key = collection(name).id_key
        for row in rows:
            row_id = row.pop(id_key)
            try:
                existing = self.get(name, row_id)
                saved = self.write(name, row_id, row, by=by, create=existing is None)
            except TrackerError as e:
                rep.refused.append(f"{row_id}: {e}")
                continue
            if existing is None:
                rep.created.append(row_id)
            elif saved == existing:
                rep.unchanged += 1
            else:
                rep.updated.append(row_id)
        return rep

    # --- the bulk-add queue: an agent queues companies, the owner adds them from the Companies tab -------------------

    def queue_companies(self, rows: list[dict[str, Any]], *, label: str, by: str) -> str:
        """Queue companies for the owner to add with one click, rather than adding them one by one. Returns its ID.

        Rows hold only what an agent may set on a new company (name, sector, interests and the research fields), so
        a bad field is refused now, not when the owner clicks. Duplicates are checked when the owner adds them.
        """
        by = by.strip()
        if not by:
            raise TrackerError("every write needs a writer name")
        allowed = COMPANIES.agent | COMPANIES.insert_only
        clean = []
        for i, row in enumerate(rows, 1):
            if not isinstance(row, dict):
                raise TrackerError(f"row {i} isn't an object")
            if _blank(row.get("company")):
                raise TrackerError(f"row {i} has no company name")
            if bad := sorted(k for k in row if k not in allowed):
                raise OwnershipError(f"row {i}: agents can't set {', '.join(bad)} on a new company")
            clean.append({k: normalize(COMPANIES.field(k), v) for k, v in row.items() if not _blank(v)})
        if not clean:
            raise TrackerError("there are no rows to queue")
        now = now_utc()
        with self.lock():
            base = doc_id = "q" + re.sub(r"\D", "", now)  # q20261007221530
            n = 1
            while (self.root / "imports" / f"{doc_id}.json").exists():
                n += 1
                doc_id = f"{base}-{n}"
            doc = {"collection": "companies", "status": "pending", "label": label.strip() or "companies",
                   "added_by": by, "created_at": now, "rows": clean}  # fmt: skip
            write_json(self.root / "imports" / f"{doc_id}.json", doc)
            self._log({"at": now, "by": by, "op": "queue", "collection": "imports", "id": doc_id, "rows": len(clean)})
        return doc_id

    def imports(self) -> list[dict[str, Any]]:
        """Every queued batch, oldest first, with its ID and without its rows (just how many)."""
        d = self.root / "imports"
        docs = [{"id": p.stem, **doc} for p in d.glob("*.json") if (doc := read_json(p))] if d.is_dir() else []
        return [
            {k: v for k, v in doc.items() if k != "rows"} | {"rows": len(doc.get("rows") or [])}
            for doc in sorted(docs, key=lambda doc: (doc.get("created_at") or "", doc["id"]))
        ]

    def accept_import(self, doc_id: str) -> dict[str, Any]:
        """Add a queued batch, each company as the agent that queued it, with the next C### ID and research status
        "Not started". A company whose name is already in the tracker is skipped. Progress is saved every 10 rows, so
        after an interruption running it again carries on where it stopped. Returns the batch without its rows."""
        _check_id(doc_id)
        path = self.root / "imports" / f"{doc_id}.json"
        doc = read_json(path)
        if not doc:
            raise TrackerError(f"there is no queued batch {doc_id}")
        if doc.get("collection") != "companies":
            raise TrackerError(f"{doc_id} queues {doc.get('collection')!r}; only companies can be queued")
        if doc.get("status") != "done":
            rows = doc.get("rows") or []
            added: dict[str, str] = dict(doc.get("added") or {})  # name key -> company ID, kept across interruptions
            have = {name_key(r.get("company")) for r in self.load("companies")}
            skipped, refused = 0, []
            doc |= {"status": "running", "total": len(rows)}
            for i, row in enumerate(rows, 1):
                key = name_key(row.get("company"))
                if key in added:
                    pass  # added before an interruption
                elif not key or key in have:
                    skipped += 1
                else:
                    for _ in range(5):  # another writer can take the same next ID meanwhile
                        try:
                            cid = self.next_id("companies")
                            self.write("companies", cid, row, by=doc.get("added_by") or "import", create=True)
                        except DuplicateError:
                            continue
                        except TrackerError as e:
                            refused.append(f"{row.get('company')}: {e}")
                        else:
                            added[key] = cid
                            have.add(key)
                        break
                if i % 10 == 0:
                    write_json(path, doc | {"done_rows": i, "added": added})
            doc |= {"status": "done", "done_rows": len(rows), "added": added, "imported": len(added),
                    "skipped_duplicates": skipped, "refused": refused, "finished_at": now_utc()}  # fmt: skip
            write_json(path, doc)
        return {"id": doc_id} | {k: v for k, v in doc.items() if k != "rows"} | {"rows": len(doc.get("rows") or [])}


def _agent_values(col: Collection, values: dict[str, Any], create: bool) -> dict[str, Any]:
    """What an agent may write: agent-maintained fields, insert-only ones on a new row, and never a blank."""
    if not col.agent:
        raise OwnershipError(f"{col.title} is yours alone: agents can't write it")
    allowed = col.agent | (col.insert_only if create else frozenset())
    refused = sorted(k for k in values if k not in allowed)
    if refused:
        when = "" if create else " after the row exists"
        raise OwnershipError(f"agents can't write {', '.join(refused)} on {col.name}{when}")
    if create and col is JOBS and (values.get("current_stage"), values.get("highest_stage")) != ("Lead", "None"):
        raise OwnershipError("agents add jobs at stage Lead, highest stage None")
    return {k: v for k, v in values.items() if not _blank(v)}  # a blank from an agent never erases a value
