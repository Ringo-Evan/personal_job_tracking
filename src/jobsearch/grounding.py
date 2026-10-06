"""Facts schema (plan-models.md §5) and the grounding check (§4 step 3).

Every extracted fact cites a source id and a verbatim quote. A fact survives only if the source exists in the pack,
the quote appears in that source's text (after whitespace/case/punctuation-style normalization), and the fact carries
no email, phone number or LinkedIn URL. More than 30% of facts failing fails the whole extraction.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from jobsearch.evidence import Pack, canonical
from jobsearch.models import EMAIL_RE, PHONE_RE, cap_words

MAX_FAIL_RATE = 0.30
MIN_PACK_CHARS_FOR_FACTS = 2000  # a pack this big should yield at least one grounded fact
QUOTE_WORDS = 25
MAX_PEOPLE = 5
ATS_TYPES = [
    "greenhouse", "lever", "ashby", "smartrecruiters", "workday", "workable", "rippling", "breezy", "pinpoint",
    "recruitee", "other", "unknown",
]  # fmt: skip
SIGNAL_KINDS = ["layoffs", "funding", "revenue", "acquisition", "growth", "public-sector", "other"]
SIZES = ["<50", "50-500", "500-5k", "5k+", "Unknown"]
WORK_MODES = ["Remote", "Hybrid", "On-site", "Unknown"]
MAINE = ["yes", "no", "varies", "unknown"]


def has_pii(text: str) -> str | None:
    if EMAIL_RE.search(text):
        return "email address"
    if PHONE_RE.search(text):
        return "phone number"
    return None


def is_linkedin(text: str) -> bool:
    return "linkedin.com" in text.lower()


# --- schema handed to the extractor --------------------------------------------------------------


def _obj(props: dict[str, Any]) -> dict[str, Any]:
    return {"type": "object", "properties": props, "required": list(props), "additionalProperties": False}


def _nullable(schema: dict[str, Any]) -> dict[str, Any]:
    return {"anyOf": [schema, {"type": "null"}]}


_S = {"type": "string"}
_CITE = {"source": _S, "quote": _S}  # first, so the model copies the quote before it summarizes


def facts_schema() -> dict[str, Any]:
    return _obj(
        {
            "career_url": _nullable(_obj({**_CITE, "value": _S})),
            "ats": _nullable(_obj({**_CITE, "type": {"enum": ATS_TYPES}, "token": _nullable(_S)})),
            "remote_policy": _nullable(_obj({**_CITE, "summary": _S, "maine_eligible": {"enum": MAINE}})),
            "stability_signals": {
                "type": "array",
                "items": _obj({**_CITE, "kind": {"enum": SIGNAL_KINDS}, "summary": _S, "date": _nullable(_S)}),
            },
            "size": _nullable(_obj({**_CITE, "value": {"enum": SIZES}})),
            "qa_openings": {
                "type": "array",
                "items": _obj({**_CITE, "title": _S, "location": _S, "work_mode": {"enum": WORK_MODES}, "url": _S}),
            },
            "people": {"type": "array", "items": _obj({**_CITE, "name": _S, "title": _S})},
        }
    )  # fmt: skip


SINGLE_FACTS = ("career_url", "ats", "remote_policy", "size")
LIST_FACTS = ("stability_signals", "qa_openings", "people")


# --- grounding -----------------------------------------------------------------------------------

_PUNCT = str.maketrans({"‘": "'", "’": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                        "−": "-", " ": " "})  # fmt: skip
_EDGE = " \t\n\"'`.,;:…"


def normalize(text: str) -> str:
    """Case, whitespace and typographic punctuation only. A paraphrase still fails."""
    t = unicodedata.normalize("NFKC", text or "").translate(_PUNCT).lower()
    return re.sub(r"\s+", " ", t).strip()


def _strip_quote(q: str) -> str:
    q = q.strip()
    while q.endswith("...") or q.startswith("..."):
        q = q.removesuffix("...").removeprefix("...")
    return q.strip(_EDGE)


@dataclass
class Dropped:
    path: str  # e.g. "people[2]"
    reason: str


@dataclass
class Grounded:
    facts: dict[str, Any]  # kept facts, same shape as the schema; failed singles become null
    total: int = 0
    dropped: list[Dropped] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)  # truncations

    @property
    def kept(self) -> int:
        return self.total - len(self.dropped)

    @property
    def fail_rate(self) -> float:
        return len(self.dropped) / self.total if self.total else 0.0

    @property
    def passed(self) -> bool:
        return self.fail_rate <= MAX_FAIL_RATE

    def summary(self) -> dict[str, Any]:
        return {"total": self.total, "kept": self.kept, "fail_rate": round(self.fail_rate, 3),
                "dropped": [f"{d.path}: {d.reason}" for d in self.dropped]}  # fmt: skip


JOBS_URL_RE = re.compile(
    r"(?i)career|jobs?\b|/join|work-with|hiring|opportunit|openings|greenhouse\.io|lever\.co|ashbyhq\.com|"
    r"smartrecruiters\.com|myworkdayjobs\.com|workable\.com|rippling\.com|breezy\.hr|pinpointhq\.com|recruitee\.com"
)


def _in_pack(url: str, pack: Pack) -> bool:
    """A fetched page's own URL, or a jobs-looking URL written in a page. Page text can carry any URL (an
    injected exfiltration link, say), so a bare mention of a non-jobs URL is not enough."""
    key = canonical(url)
    if any(canonical(s.url) == key for s in pack.sources):
        return True
    return bool(JOBS_URL_RE.search(url)) and any(url in s.text for s in pack.sources)


def _check(
    path: str, fact: dict[str, Any], pack: Pack, notes: list[str], quote_optional: bool = False
) -> str | None:
    """None if the fact is grounded and clean, else the reason it fails. Truncates the quote to 25 words.
    quote_optional: the source itself proves the fact (a careers URL that is the cited page's own URL)."""
    src = pack.by_id().get(str(fact.get("source") or ""))
    if src is None:
        return f"cites unknown source {fact.get('source')!r}"
    quote = _strip_quote(cap_words(str(fact.get("quote") or ""), QUOTE_WORDS, notes, f"{path} quote"))
    fact["quote"] = quote
    if not quote_optional:
        if len(normalize(quote)) < 3:
            return "empty quote"
        # The page title counts: job pages often carry the job title only in <title>.
        if normalize(quote) not in normalize(f"{src.title}\n{src.text}"):
            return f"quote not found in {src.id}"
    for k, v in fact.items():
        if isinstance(v, str):
            if reason := has_pii(v):
                return f"{k} contains a {reason}"
            if is_linkedin(v):
                return f"{k} contains a LinkedIn URL"
    return None


LEADER_TITLE = re.compile(
    r"(?i)\b(head|chief|c[eti]o|vp|svp|evp|vice president|director|manager|lead|leader|president|founder|"
    r"co-founder|owner|officer|principal|partner)\b"
)


def person_problem(name: str, title: str) -> str | None:
    """Plan: engineering/QA *leaders* with a full name. Shared by grounding and export."""
    n = normalize(name)
    if n == normalize(title):
        return "name is a job title, not a person"
    parts = n.replace(".", " ").split()
    if len(parts) < 2 or len(parts[-1]) < 2:
        return "not a full name"
    if not LEADER_TITLE.search(title or ""):
        return f"not a leadership title: {title!r}"
    return None


def _person_problem(fact: dict[str, Any]) -> str | None:
    name = normalize(str(fact.get("name") or ""))
    if name not in normalize(fact["quote"]):
        return "person's name is not in their quote"
    return person_problem(str(fact.get("name") or ""), str(fact.get("title") or ""))


def ground(facts: dict[str, Any], pack: Pack) -> Grounded:
    """Check every fact against the pack. Input is the extractor's schema-valid output (not modified)."""
    out: dict[str, Any] = {}
    g = Grounded(out)

    for key in SINGLE_FACTS:
        fact = facts.get(key)
        if key == "ats" and fact and fact.get("type") == "unknown" and not fact.get("token"):
            fact = None  # "don't know" is not a claim
        if not fact:
            out[key] = None
            continue
        fact = dict(fact)
        g.total += 1
        src = pack.by_id().get(str(fact.get("source") or ""))
        own_page = (
            key == "career_url"
            and src is not None
            and canonical(str(fact.get("value") or "")) == canonical(src.url)
        )
        reason = _check(key, fact, pack, g.notes, quote_optional=own_page)
        if reason is None and key == "career_url":
            url = str(fact.get("value") or "")
            if not url.startswith(("http://", "https://")):
                reason = "career_url is not an http(s) URL"
            elif not _in_pack(url, pack):
                reason = "career_url appears nowhere in the evidence"
        if reason is None and key == "remote_policy":
            fact["summary"] = cap_words(fact.get("summary", ""), 40, g.notes, "remote_policy summary")
        if reason:
            g.dropped.append(Dropped(key, reason))
            out[key] = None
        else:
            out[key] = fact

    for key in LIST_FACTS:
        kept = []
        items = list(facts.get(key) or [])
        if key == "people" and len(items) > MAX_PEOPLE:
            g.notes.append(f"people truncated from {len(items)} to {MAX_PEOPLE}")
            items = items[:MAX_PEOPLE]
        for i, fact in enumerate(items):
            path = f"{key}[{i}]"
            fact = dict(fact)
            g.total += 1
            reason = _check(path, fact, pack, g.notes)
            if reason is None and key == "people":
                reason = _person_problem(fact)
            if reason is None and key == "qa_openings":
                url = str(fact.get("url") or "")
                if not (url.startswith(("http://", "https://")) and _in_pack(url, pack)):
                    # A URL the model can't point to in the evidence may be invented: use the page it came from.
                    fact["url"] = pack.by_id()[fact["source"]].url
            if reason is None and key == "stability_signals":
                fact["summary"] = cap_words(fact.get("summary", ""), 25, g.notes, f"{path} summary")
                date = str(fact.get("date") or "")
                if date and date[:4] not in fact["quote"]:
                    # Models guess dates from context (all five got Dexcom's layoff month wrong): keep only
                    # dates whose year the quote itself states.
                    g.notes.append(f"{path} date {date} dropped: year not in quote")
                    fact["date"] = None
            if reason:
                g.dropped.append(Dropped(path, reason))
            else:
                kept.append(fact)
        out[key] = kept
    return g


def grounding_gate(pack: Pack) -> Any:
    """Router `accept` callback: escalate when more than 30% of facts fail grounding, or when a pack with real
    content yields no grounded facts at all (a model can pass the rate check by claiming nothing)."""

    def accept(data: Any) -> str | None:
        g = ground(data, pack)
        content = sum(len(s.text) for s in pack.sources)
        if g.kept == 0 and content >= MIN_PACK_CHARS_FOR_FACTS:
            return f"no grounded facts from {len(pack.sources)} sources ({content} chars)"
        if g.passed:
            return None
        return f"grounding failed for {len(g.dropped)}/{g.total} facts: " + "; ".join(
            f"{d.path} {d.reason}" for d in g.dropped[:5]
        )

    return accept
