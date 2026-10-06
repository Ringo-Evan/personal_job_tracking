"""The tracker CSV contract (plan section 5).

Mirrors the tracker's own Import CSV rules: header detection by ID label, normalized label matching,
blank cells meaning "no value", comma-stripped numbers and YYYY-MM-DD dates.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

COMPANY_LABELS: dict[str, str] = {
    "company_id": "Company ID",
    "company": "Company / group",
    "sector": "Sector",
    "interests": "Interests / connection",
    "career_url": "Career / company URL",
    "role_families": "Role families to search",
    "overall_rank": "Your overall rank (1 best)",
    "interest": "Interest (1-5 high best)",
    "values": "Values (1-5 high best)",
    "pay": "Pay (1-5 high best)",
    "flexibility": "Flexibility (1-5 high best)",
    "travel_perks": "Travel / perks (1-5 high best)",
    "homestead_fit": "Homestead fit (1-5 high best)",
    "dealbreakers": "Your dealbreakers",
    "hiring_difficulty": "Hiring difficulty (1-5 hard)",
    "estimate_confidence": "Estimate confidence",
    "estimate_rationale": "Estimate rationale",
    "research_notes": "Research notes from search",
    "your_notes": "Your research notes",
    "research_status": "Research status",
    "maine_remote": "Maine / remote eligibility",
    "benefit_source": "Known benefit / growth source",
    "research_as_of": "Research as of",
    "next_research_action": "Next research action",
    "fit_score": "Fit score (1-5, Claude estimate)",
    "stability_score": "Stability in a downturn (1-5, Claude estimate)",
    "remote_likelihood": "Remote-from-Maine likelihood",
    "size_estimate": "Size estimate",
    "ai_summary": "Why it might fit",
    "ai_red_flags": "Red flags",
    "ai_scored_at": "Screened on",
    "approach_role": "Who to approach (role)",
    "people_search_url": "People search link",
    "outreach_notes": "Outreach angle",
}

JOB_LABELS: dict[str, str] = {
    "job_id": "Job ID",
    "company": "Company / group",
    "job_title": "Job title",
    "track": "Track",
    "job_rank": "Your job rank (1 best)",
    "current_stage": "Current stage",
    "highest_stage": "Highest stage reached",
    "next_action": "Next action",
    "follow_up_due": "Follow-up due",
    "date_applied": "Date applied",
    "application_method": "Application method",
    "deadline": "Deadline",
    "posting_url": "Job posting URL",
    "application_url": "Application URL / portal",
    "source_url": "Source URL",
    "listing_evidence": "Listing evidence",
    "last_checked": "Last checked",
    "date_discovered": "Date discovered",
    "location": "Location",
    "work_mode": "Work mode",
    "maine_eligible": "Maine eligible?",
    "travel_fraction": "Travel fraction",
    "pay_low": "Published pay low",
    "pay_high": "Published pay high",
    "currency": "Currency",
    "pay_period": "Pay period",
    "hiring_difficulty": "Hiring difficulty (1-5 hard)",
    "skill_match": "Skill match (1-5 high)",
    "fit_notes": "Difficulty / fit notes",
}

PEOPLE_LABELS: dict[str, str] = {
    "person_id": "Person ID",
    "name": "Name",
    "company": "Company / group",
    "title": "Title",
    "profile_url": "Profile URL",
    "source_url": "Where found (source URL)",
    "status": "Status",
    "relationship": "Connection (cold / warm / referral)",
    "last_contact_date": "Last contact date",
    "next_step": "Next step",
    "next_step_date": "Next step date",
    "why_approach": "Why approach them",
    "notes": "Notes / conversation log",
}


@dataclass(frozen=True)
class Tab:
    name: str
    id_key: str
    labels: dict[str, str]
    number_keys: frozenset[str] = field(default_factory=frozenset)

    @property
    def id_label(self) -> str:
        return self.labels[self.id_key]


COMPANIES = Tab(
    "companies",
    "company_id",
    COMPANY_LABELS,
    frozenset(
        {
            "overall_rank",
            "interest",
            "values",
            "pay",
            "flexibility",
            "travel_perks",
            "homestead_fit",
            "hiring_difficulty",
            "fit_score",
            "stability_score",
        }
    ),
)
JOBS = Tab(
    "jobs",
    "job_id",
    JOB_LABELS,
    frozenset({"job_rank", "pay_low", "pay_high", "hiring_difficulty", "skill_match", "travel_fraction"}),
)
PEOPLE = Tab("people", "person_id", PEOPLE_LABELS)
TABS = (COMPANIES, JOBS, PEOPLE)

RESEARCH_STATUSES = ("Not started", "Initial research", "Researching", "Researched", "Parked")

_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_label(label: str) -> str:
    """lowercase, drop '(auto)', collapse non-alphanumeric runs to one space, trim."""
    s = label.lower().replace("(auto)", "")
    return _NON_ALNUM.sub(" ", s).strip()


def parse_number(text: str) -> int | float | None:
    """Tracker number rule: strip commas, parse. Returns None if it isn't a number."""
    s = text.replace(",", "").strip()
    if not s:
        return None
    try:
        n = float(s)
    except ValueError:
        return None
    return int(n) if n.is_integer() else n


@dataclass
class ParseResult:
    tab: Tab
    rows: list[dict[str, Any]]
    header_row_index: int
    ignored_columns: list[str]
    warnings: list[str]


def detect_tab(text: str) -> Tab | None:
    """Which tab a CSV export belongs to, by the first ID label found in column 0."""
    for row in csv.reader(io.StringIO(text)):
        if not row:
            continue
        first = row[0].strip()
        for tab in TABS:
            if first == tab.id_label:
                return tab
    return None


def parse_text(text: str, tab: Tab) -> ParseResult:
    reader = list(csv.reader(io.StringIO(text)))
    header_idx = next(
        (i for i, row in enumerate(reader) if row and row[0].strip() == tab.id_label),
        None,
    )
    if header_idx is None:
        raise ValueError(f"No header row starting with {tab.id_label!r} found")

    by_norm = {normalize_label(label): key for key, label in tab.labels.items()}
    header = reader[header_idx]
    col_keys: list[str | None] = []
    ignored: list[str] = []
    for label in header:
        key = by_norm.get(normalize_label(label))
        col_keys.append(key)
        if key is None and label.strip():
            ignored.append(label)

    rows: list[dict[str, Any]] = []
    warnings: list[str] = []
    for line_no, raw in enumerate(reader[header_idx + 1 :], start=header_idx + 2):
        if not any(cell.strip() for cell in raw):
            continue
        row: dict[str, Any] = {}
        for key, cell in zip(col_keys, raw, strict=False):
            if key is None:
                continue
            value = cell.strip()
            if value == "":
                row[key] = None
            elif key in tab.number_keys:
                num = parse_number(value)
                if num is None:
                    warnings.append(f"line {line_no}: {key}={value!r} is not a number; kept as text")
                    row[key] = value
                else:
                    row[key] = num
            else:
                row[key] = value
        if not row.get(tab.id_key):
            warnings.append(f"line {line_no}: blank {tab.id_label}; row skipped")
            continue
        rows.append(row)
    return ParseResult(tab, rows, header_idx, ignored, warnings)


def read_file(path: Path) -> str:
    return path.read_text(encoding="utf-8-sig")


def parse_file(path: Path, tab: Tab | None = None) -> ParseResult:
    text = read_file(path)
    tab = tab or detect_tab(text)
    if tab is None:
        raise ValueError(f"{path.name}: no known ID header (Company ID / Job ID / Person ID)")
    return parse_text(text, tab)
