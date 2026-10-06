"""Shared posting model and normalizing helpers for all board modules."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from html import unescape
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

WorkMode = Literal["Remote", "Hybrid", "On-site", "Unknown"]


class BoardError(RuntimeError):
    """A board endpoint failed; the watcher logs it and skips that board this run."""


class JobPosting(BaseModel):
    model_config = ConfigDict(extra="forbid")
    board: str
    board_job_id: str
    company_id: str
    title: str
    location_text: str
    is_remote: bool | None
    work_mode: WorkMode
    url: str
    apply_url: str | None = None
    posted_at: str | None = None  # YYYY-MM-DD
    pay_low: float | None = None
    pay_high: float | None = None
    currency: str | None = None
    pay_period: str | None = None  # year | hour | month
    raw_ref: str  # API URL the posting came from
    description_text: str = ""  # plain text; the watcher stores the first 6000 chars for the job scorer

    @property
    def key(self) -> str:
        return f"{self.board}-{_safe(self.board_job_id)}"


def _safe(s: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", s)[:120]


_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def html_to_text(html: str | None) -> str:
    """Rough HTML to text; handles Greenhouse's double-escaped content."""
    if not html:
        return ""
    s = unescape(html)
    if "&lt;" in s or "&amp;" in s:
        s = unescape(s)
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</h\d>|</div>", "\n", s)
    s = _TAG.sub(" ", s)
    return "\n".join(_WS.sub(" ", line).strip() for line in s.splitlines() if line.strip())


def iso_date(value: Any) -> str | None:
    """Date from ISO strings or epoch milliseconds."""
    if value in (None, ""):
        return None
    if isinstance(value, int | float):
        return datetime.fromtimestamp(value / 1000, tz=UTC).date().isoformat()
    m = re.match(r"\d{4}-\d{2}-\d{2}", str(value))
    return m.group(0) if m else None


def work_mode_from(text: str, is_remote: bool | None = None) -> WorkMode:
    t = (text or "").lower()
    if "hybrid" in t:
        return "Hybrid"
    if is_remote or re.search(r"\bremote\b|\banywhere\b|work from home", t):
        return "Remote"
    if re.search(r"on-?site|in[- ]office", t):
        return "On-site"
    if is_remote is False and t.strip():
        return "On-site"
    return "Unknown"


# --- pay ------------------------------------------------------------------------------------------

_CUR = r"(?:US\$|CA\$|C\$|\$|USD\s?|CAD\s?|£|€)"
_AMT = r"\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?"
_PAY_RANGE = re.compile(
    rf"(?P<cur>{_CUR})\s?(?P<lo>{_AMT})\s*(?P<lok>[kK])?"
    rf"\s*(?:-|–|—|to)\s*{_CUR}?\s?(?P<hi>{_AMT})\s*(?P<hik>[kK])?"
    r"(?=(?P<tail>[^\n]{0,40}))"  # lookahead, so a later range in the same sentence is still found
)
_CURRENCY = {
    "$": "USD",
    "US$": "USD",
    "USD": "USD",
    "CA$": "CAD",
    "C$": "CAD",
    "CAD": "CAD",
    "£": "GBP",
    "€": "EUR",
}


def _num(s: str, k: str | None) -> float:
    n = float(s.replace(",", ""))
    return n * 1000 if k else n


def pay_from_text(text: str) -> tuple[float, float, str, str] | None:
    """First plausible pay range in free text: (low, high, currency, period)."""
    for m in _PAY_RANGE.finditer(text or ""):
        lo, hi = _num(m["lo"], m["lok"]), _num(m["hi"], m["hik"])
        if hi < lo:
            continue
        tail = (m["tail"] or "").lower()
        if re.match(r"\s*(?:million|billion|mm?\b|bn?\b)", tail):
            continue  # funding or revenue, not pay
        currency = _CURRENCY.get(m["cur"].strip(), "USD")
        if "cad" in tail:
            currency = "CAD"
        if re.search(r"hour|/hr|\bhr\b|hourly", tail) or hi < 500:
            period = "hour"
        elif re.search(r"month|/mo\b", tail):
            period = "month"
        else:
            period = "year"
        if period == "year" and lo < 15000:
            continue  # "$5 - $10 million in funding" and similar
        return lo, hi, currency, period
    return None


_PERIOD_WORDS = (("hour", ("hour", "hr")), ("month", ("month",)), ("year", ("year", "annual", "yr", "salary")))


def pay_period(label: Any, high: float | None = None) -> str | None:
    """'year', 'hour' or 'month' from a board's pay-interval label; unlabeled small amounts are hourly."""
    v = str(label or "").lower()
    for period, words in _PERIOD_WORDS:
        if any(w in v for w in words):
            return period
    if not v and high is not None:
        return "hour" if high < 500 else "year"
    return None


def number(value: Any) -> float | None:
    """A pay amount from a number or a numeric string ('23000'); None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return float(value)
    try:
        return float(str(value).replace(",", ""))
    except ValueError:
        return None


def apply_text_pay(p: dict[str, Any], text: str) -> None:
    if p.get("pay_low") is None and (found := pay_from_text(text)):
        p["pay_low"], p["pay_high"], p["currency"], p["pay_period"] = found
