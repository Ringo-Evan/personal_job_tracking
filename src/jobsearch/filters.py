"""Title and location rules for job postings (plan phase 3)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from jobsearch.boards.common import JobPosting
from jobsearch.config import Config

Verdict = Literal["eligible", "unknown", "drop"]

US_STATES = [
    "Alabama", "Alaska", "Arizona", "Arkansas", "California", "Colorado", "Connecticut", "Delaware",
    "Florida", "Georgia", "Hawaii", "Idaho", "Illinois", "Indiana", "Iowa", "Kansas", "Kentucky",
    "Louisiana", "Maine", "Maryland", "Massachusetts", "Michigan", "Minnesota", "Mississippi", "Missouri",
    "Montana", "Nebraska", "Nevada", "New Hampshire", "New Jersey", "New Mexico", "New York",
    "North Carolina", "North Dakota", "Ohio", "Oklahoma", "Oregon", "Pennsylvania", "Rhode Island",
    "South Carolina", "South Dakota", "Tennessee", "Texas", "Utah", "Vermont", "Virginia", "Washington",
    "West Virginia", "Wisconsin", "Wyoming",
]  # fmt: skip
# Longest first so "West Virginia" wins over "Virginia".
_STATE_NAMES = sorted(US_STATES, key=len, reverse=True)
_STATE_ABBR = "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC"
_STATE_NAME_RE = re.compile(r"\b(" + "|".join(re.escape(s) for s in _STATE_NAMES) + r")\b", re.I)
# Abbreviations only count inside a comma/slash/semicolon separated run, so "IN" or "OR" in prose don't.
_ABBR = r"\b(?:" + _STATE_ABBR.replace(" ", "|") + r")\b"
_ABBR_RUN_RE = re.compile(_ABBR + r"(?:\s*[,/;]\s*(?:and\s+)?" + _ABBR + r"){2,}")
_ELIGIBILITY_CUE = re.compile(
    r"(?i)eligib|resid(?:e|ent|ing)|located in|living in|hire in|hiring in|employ(?:ees)? in|"
    r"following states|states?:|registered to do business|we can (?:only )?hire|payroll"
)

_LEADERSHIP = re.compile(r"(?i)\b(manager|director|head|lead|leader)\b")
_DEV = re.compile(r"(?i)\b(engineer|developer|sde|swe|programmer|architect)\b")
_TEST_WORDS = re.compile(
    r"(?i)\b(qa|quality|test|tests|tester|testing|sdet|set|verification|validation|v&v)\b"
)

# A description saying the job is remote although the board lists a US city (often just the HQ): kept as unknown.
_REMOTE_CUE = re.compile(
    r"(?i)\b(?:fully remote|100% remote|remote[- ]first|"
    r"(?:role|position|job|opportunity) (?:is|can be|will be) (?:fully |100% )?remote|"
    r"remote (?:role|position|opportunity)|work from home|work from anywhere|"
    r"remote (?:within|anywhere in|in) the (?:u\.?s\.?|united states))\b"
)
# A location that is just the country: often a remote or multi-site posting.
_BARE_US = re.compile(r"(?i)^\s*(?:the\s+)?(?:united states(?: of america)?|u\.?s\.?a?\.?)\s*$")
_US_ABBRS = set(_STATE_ABBR.split())

# Broad regions that may or may not include the US: kept for the owner to check rather than dropped.
_BROAD_REGIONS = ("americas", "amer", "global", "worldwide", "any location")
# Locations are terse: "Belfast ME" (no comma) is Maine. Uppercase only, so the word "me" doesn't count.
_MAINE_ABBR = re.compile(r"\bME\b")
_LOC_ABBR = re.compile(r"(?:,\s*|\s)([A-Z]{2})\b")


def _term_re(term: str) -> re.Pattern[str]:
    t = term.strip().lower()
    if t.startswith(","):
        return re.compile(r",\s*" + re.escape(t[1:].strip()) + r"(?!\w)", re.I)
    return re.compile(r"(?<!\w)" + re.escape(t) + r"(?!\w)", re.I)


@dataclass
class Classification:
    verdict: Verdict
    reason: str


class PostingFilter:
    def __init__(self, cfg: Config):
        f = cfg.filters
        self.include = re.compile(f.title_include)
        self.exclude = re.compile(f.title_exclude)
        self.maine = [_term_re(t) for t in f.maine_terms]
        self.remote = [_term_re(t) for t in f.remote_terms]
        self.us = [_term_re(t) for t in f.us_terms]
        self.broad = [_term_re(t) for t in _BROAD_REGIONS]

    def title_ok(self, title: str) -> bool:
        return bool(self.include.search(title or "")) and not self.exclude.search(title or "")

    # --- location ---------------------------------------------------------------------------------

    def _has(self, patterns, text: str) -> bool:
        return any(p.search(text) for p in patterns)

    def _residual(self, part: str) -> str:
        """What's left of a location after removing remote words and punctuation."""
        s = part
        for p in self.remote:
            s = p.sub(" ", s)
        s = re.sub(r"(?i)\b(fully|100%|based|work|from|home|only|position|role|in|-)\b", " ", s)
        return re.sub(r"[^a-z0-9]+", "", s.lower())

    def _is_maine(self, part: str) -> bool:
        return self._has(self.maine, part) or bool(_MAINE_ABBR.search(part))

    def _other_state(self, part: str) -> str | None:
        """A US state other than Maine named in a location, e.g. 'Seattle, Washington (Washington-Remote)'."""
        for name in _STATE_NAME_RE.findall(part):
            if name.lower() != "maine":
                return name
        for abbr in _LOC_ABBR.findall(part):
            if abbr not in ("ME", "US"):
                return abbr
        return None

    def _us_place(self, part: str) -> bool:
        """A US city/state or 'United States' in a location part."""
        if self._has(self.us, part) or _STATE_NAME_RE.search(part):
            return True
        return any(a in _US_ABBRS for a in _LOC_ABBR.findall(part))

    def classify(self, p: JobPosting) -> Classification:
        parts = [x.strip() for x in re.split(r";|\|| / |\n", p.location_text or "") if x.strip()] or [""]
        eligible: Classification | None = None
        unknown: Classification | None = None
        for part in parts:
            if self._is_maine(part):
                return Classification("eligible", f"location mentions Maine area: {part}")
            remote = self._has(self.remote, part) or (p.is_remote is True and len(parts) == 1)
            if remote and (self._has(self.us, part) or self._residual(part) == ""):
                if state := self._other_state(part):
                    unknown = unknown or Classification(
                        "unknown", f"remote but tied to {state}, check Maine eligibility: {part}"
                    )
                else:
                    eligible = eligible or Classification("eligible", f"US remote: {part or '(no location)'}")
            elif self._has(self.broad, part):
                # "Global" or "Remote in AMER" may include the US; keep for the owner to check.
                unknown = unknown or Classification("unknown", f"broad region, check US eligibility: {part}")
        verdict = eligible or unknown
        if verdict is None:
            # Owner's standing preference (2026-10-05): too wide beats too narrow. Ambiguous postings are kept as
            # "unknown" and the scorer reads eligibility from the description; only clear misses are dropped.
            if not p.location_text.strip():
                return Classification("unknown", "no location given, check eligibility")
            if any(_BARE_US.match(part) for part in parts):
                return Classification("unknown", f"US-wide location, check remote eligibility: {p.location_text}")
            if any(self._us_place(part) for part in parts) and _REMOTE_CUE.search(p.description_text or ""):
                return Classification(
                    "unknown", f"listed at {p.location_text[:60]} but the description says remote; check"
                )
            return Classification("drop", f"not US-remote or near Maine: {p.location_text}")
        if verdict.verdict == "eligible":
            return self._check_state_list(p.description_text, verdict)
        return verdict

    def _check_state_list(self, text: str, verdict: Classification) -> Classification:
        """A remote posting that lists eligible states without Maine is 'unknown' (plan phase 3)."""
        for m in _ELIGIBILITY_CUE.finditer(text or ""):
            window = text[max(0, m.start() - 200) : m.end() + 600]
            names = {s.lower() for s in _STATE_NAME_RE.findall(window)}
            abbrs = {
                a for run in _ABBR_RUN_RE.finditer(window) for a in re.findall(r"\b[A-Z]{2}\b", run.group(0))
            }
            if len(names) + len(abbrs) < 3:
                continue
            if "maine" in names or "ME" in abbrs:
                return Classification("eligible", verdict.reason + "; Maine is in the posting's state list")
            return Classification("unknown", verdict.reason + "; posting lists eligible states without Maine")
        return verdict


def track_for(title: str) -> str:
    """Track heuristic for new rows: leadership, SDE pivot, else core QA."""
    if _LEADERSHIP.search(title or ""):
        return "QA leadership"
    if _DEV.search(title or "") and not _TEST_WORDS.search(title or ""):
        return "SDE pivot"
    return "Core QA"
