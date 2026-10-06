"""Pydantic models for stored records: company record, triage verdict, verified research."""

from __future__ import annotations

import re
from typing import Annotated, Any, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator


class HistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    at: str
    stage: str  # ingest | detect | triage | verify | export
    changed: list[str]


class OwnerChange(BaseModel):
    model_config = ConfigDict(extra="forbid")
    value: Any
    at: str


class CompanyRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company_id: str
    tracker: dict[str, Any]
    missing_from_tracker: bool = False
    # Last time the owner changed a watched field (research_status, overall_rank) to a value the
    # pipeline didn't export itself. Feeds the section 5.3 exceptions.
    owner_changes: dict[str, OwnerChange] = Field(default_factory=dict)
    # Values the pipeline last exported, per field; set in phase 6.
    exported: dict[str, Any] = Field(default_factory=dict)
    ats: dict[str, Any] | None = None
    discovery: dict[str, Any] | None = None  # last board search (discover.py): careers page, boards seen
    triage: dict[str, Any] | None = None
    research: dict[str, Any] | None = None
    verify_rejects: int = 0  # consecutive verify rejects; the queue skips a company at 2
    history: list[HistoryEntry] = Field(default_factory=list)
    last_exported_at: str | None = None


Score = Annotated[int, Field(ge=1, le=5)]
Remote = Literal["High", "Medium", "Low", "Unknown"]
Size = Literal["<50", "50-500", "500-5k", "5k+", "Unknown"]


def cap_words(text: str, limit: int, notes: list[str], label: str) -> str:
    """Truncate to `limit` words, recording that it happened (plan: truncate and log)."""
    words = (text or "").split()
    if len(words) <= limit:
        return " ".join(words)
    notes.append(f"{label} truncated from {len(words)} to {limit} words")
    return " ".join(words[:limit])


class Triage(BaseModel):
    """One company's triage verdict as returned by the model (plan 8.2)."""

    model_config = ConfigDict(extra="forbid")
    id: str
    fit: Score
    stability: Score
    remote: Remote
    size: Size
    roles: list[str] = Field(min_length=1, max_length=5)
    approach: str = Field(max_length=120)
    why: str
    flags: str = ""


def is_skipped(record: dict[str, Any]) -> bool:
    """Section 5.3: Parked or owner rank >= 4 means no research and no export."""
    t = record.get("tracker", {})
    rank = t.get("overall_rank")
    return t.get("research_status") == "Parked" or (isinstance(rank, int | float) and rank >= 4)


# --- verified research (plan.md section 6) ------------------------------------------------------------

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"(?<![\w/])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]\d{3}[\s.-]\d{4}(?!\d)")


def _http_url(v: str) -> str:
    if not re.match(r"(?i)^https?://[^\s/]+", v or ""):
        raise ValueError(f"not an http(s) URL: {v!r}")
    if "linkedin.com" in v.lower():
        raise ValueError(f"LinkedIn URLs are not allowed: {v!r}")
    return v


Url = Annotated[str, AfterValidator(_http_url)]


class _F(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ResearchAts(_F):
    type: Literal["greenhouse", "lever", "ashby", "smartrecruiters", "workday", "other", "none", "unknown"]
    token: str | None = None
    board_url: Url | None = None


class RemotePolicy(_F):
    summary: str
    maine_eligible: Literal["yes", "no", "varies", "unknown"]
    evidence_url: Url | None = None


class Signal(_F):
    kind: Literal["layoffs", "funding", "revenue", "acquisition", "growth", "public-sector", "other"]
    summary: str
    date: Annotated[str, Field(pattern=r"^\d{4}-\d{2}$")] | None = None
    url: Url


class Stability(_F):
    score: Score
    signals: list[Signal] = []


class Fit(_F):
    score: Score
    rationale: str
    role_families: list[str] = Field(default_factory=list, max_length=5)


class Opening(_F):
    title: str
    location: str
    work_mode: Literal["Remote", "Hybrid", "On-site", "Unknown"]
    url: Url


class PersonRef(_F):
    name: str
    title: str
    source_url: Url


class Research(_F):
    """Validated verify output. Unknown keys, out-of-range scores, uncited URLs, LinkedIn and PII are rejected."""

    company_id: str
    career_url: Url | None = None
    ats: ResearchAts
    remote_policy: RemotePolicy
    stability: Stability
    size_estimate: Size
    fit: Fit
    hiring_difficulty: Score
    qa_openings: list[Opening] = []
    people: list[PersonRef] = Field(default_factory=list, max_length=5)
    approach_role: str
    outreach_angle: str
    red_flags: str = ""
    confidence: Literal["low", "medium", "high"]
    sources: list[Url]
    notes: str = ""

    @model_validator(mode="after")
    def _cited_and_clean(self) -> Research:
        cited = [("remote_policy.evidence_url", self.remote_policy.evidence_url)]
        cited += [(f"stability.signals[{i}].url", s.url) for i, s in enumerate(self.stability.signals)]
        cited += [(f"people[{i}].source_url", p.source_url) for i, p in enumerate(self.people)]
        cited += [(f"qa_openings[{i}].url", o.url) for i, o in enumerate(self.qa_openings)]
        missing = [k for k, u in cited if u and u not in self.sources]
        if missing:
            raise ValueError(f"URLs not listed in sources: {', '.join(missing)}")
        for path, text in _strings(self.model_dump()):
            if text.lower().startswith(("http://", "https://")):
                continue
            if EMAIL_RE.search(text):
                raise ValueError(f"email address in {path}")
            if PHONE_RE.search(text):
                raise ValueError(f"phone number in {path}")
            if "linkedin.com" in text.lower():
                raise ValueError(f"LinkedIn reference in {path}")
        return self


def _strings(obj: Any, path: str = ""):
    if isinstance(obj, str):
        yield path, obj
    elif isinstance(obj, dict):
        for k, v in obj.items():
            yield from _strings(v, f"{path}.{k}" if path else k)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            yield from _strings(v, f"{path}[{i}]")
