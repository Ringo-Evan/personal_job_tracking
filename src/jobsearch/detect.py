"""Detect which job-board system (ATS) hosts a company's openings (plan phase 2). No LLM."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from html import unescape
from typing import Any
from urllib.parse import quote, unquote, urljoin, urlsplit

import httpx

from jobsearch.config import Config
from jobsearch.http_client import BlockedHostError, PoliteClient
from jobsearch.models import is_skipped

SUPPORTED = ("greenhouse", "lever", "ashby", "smartrecruiters", "workday")


@dataclass(frozen=True)
class Candidate:
    type: str
    token: str

    @property
    def board_url(self) -> str:
        return board_url(self.type, self.token)


def board_url(ats_type: str, token: str) -> str:
    if ats_type == "greenhouse":
        return f"https://job-boards.greenhouse.io/{token}"
    if ats_type == "lever":
        return f"https://jobs.lever.co/{token}"
    if ats_type == "ashby":
        return f"https://jobs.ashbyhq.com/{quote(token)}"
    if ats_type == "smartrecruiters":
        return f"https://careers.smartrecruiters.com/{token}"
    if ats_type == "workday":
        tenant, n, site = token.split("|")
        return f"https://{tenant}.wd{n}.myworkdayjobs.com/{site}"
    raise ValueError(ats_type)


# --- URL patterns --------------------------------------------------------------------------------

_TOK = r"([A-Za-z0-9][A-Za-z0-9_.%-]*)"
# Path segments that are part of a board's own URL scheme, never a company token.
_NOT_TOKENS = {
    "embed", "v1", "boards", "jobs", "job_board", "js", "api", "static", "assets", "favicon.ico",
    "posting-api", "cdn", "careers", "postings", "wday", "cxs", "en-us", "javascripts", "stylesheets",
}  # fmt: skip

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("greenhouse", re.compile(
        r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/embed/job_board(?:/js)?\?"
        r"(?:[^\"'\s<>]*&(?:amp;)?)?for=" + _TOK,
        re.I,
    )),
    ("greenhouse", re.compile(r"boards-api\.greenhouse\.io/v1/boards/" + _TOK, re.I)),
    ("greenhouse", re.compile(r"(?:job-)?boards(?:\.eu)?\.greenhouse\.io/" + _TOK, re.I)),
    ("lever", re.compile(r"jobs\.lever\.co/" + _TOK, re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/" + _TOK, re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/" + _TOK, re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/" + _TOK, re.I)),
    ("smartrecruiters", re.compile(r"(?:careers|jobs)\.smartrecruiters\.com/" + _TOK, re.I)),
    ("smartrecruiters", re.compile(r"api\.smartrecruiters\.com/v1/companies/" + _TOK, re.I)),
    (
        "workday",
        re.compile(
            r"([A-Za-z0-9-]+)\.wd(\d+)\.myworkdayjobs\.com/(?:wday/cxs/[A-Za-z0-9_-]+/)?"
            r"(?:[a-zA-Z]{2}-[a-zA-Z]{2}/)?([A-Za-z0-9_-]+)"
        ),
    ),
]  # fmt: skip

# Recognized but unsupported systems, reported so the owner knows why a board isn't watched.
OTHER_ATS: list[tuple[str, re.Pattern[str]]] = [
    (name, re.compile(pattern, re.I))
    for name, pattern in [
        ("icims", r"\.icims\.com"),
        ("workable", r"apply\.workable\.com|\.workable\.com/"),
        ("jobvite", r"jobs\.jobvite\.com|\.jobvite\.com"),
        ("bamboohr", r"\.bamboohr\.com/(?:careers|jobs)"),
        ("recruitee", r"\.recruitee\.com"),
        ("teamtailor", r"\.teamtailor\.com"),
        ("breezy", r"\.breezy\.hr"),
        ("jazzhr", r"\.applytojob\.com"),
        ("paylocity", r"recruiting\.paylocity\.com"),
        ("ukg", r"recruiting\d*\.ultipro\.com|\.ukg\.net"),
        ("adp", r"workforcenow\.adp\.com|myjobs\.adp\.com"),
        ("taleo", r"\.taleo\.net"),
        ("successfactors", r"successfactors\.(?:com|eu)|jobs\.sap\.com"),
        ("oracle", r"\.oraclecloud\.com/hcmUI/CandidateExperience"),
        ("rippling", r"ats\.rippling\.com"),
        ("dayforce", r"jobs\.dayforcehcm\.com"),
        ("pinpoint", r"\.pinpointhq\.com"),
        ("dover", r"app\.dover\.com/jobs"),
        ("gem", r"jobs\.gem\.com"),
        ("wellfound", r"wellfound\.com/company"),
    ]
]


def _clean_token(ats_type: str, raw: str) -> str | None:
    token = unquote(raw).rstrip(".").strip()
    if ats_type == "ashby":
        token = token.split("?")[0].split("#")[0]
    if not token or token.lower() in _NOT_TOKENS:
        return None
    return token


def find_candidates(text: str) -> list[Candidate]:
    """All ATS (type, token) pairs referenced in text, in order of first appearance, de-duplicated."""
    if not text:
        return []
    hits: list[tuple[int, Candidate]] = []
    for ats_type, pattern in PATTERNS:
        for m in pattern.finditer(text):
            if ats_type == "workday":
                tenant, n, site = m.group(1), m.group(2), m.group(3)
                if site.lower() in ("wday", "cxs"):
                    continue
                token = f"{tenant.lower()}|{n}|{site}"
            else:
                token = _clean_token(ats_type, m.group(1))
                if token is None:
                    continue
                if ats_type in ("greenhouse", "lever"):
                    token = token.lower()  # both treat board tokens case-insensitively
            hits.append((m.start(), Candidate(ats_type, token)))
    seen: set[tuple[str, str]] = set()
    out = []
    for _, c in sorted(hits, key=lambda h: h[0]):
        key = (c.type, c.token.lower())
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# A careers page that names a system without linking its board (JS widgets, CSS class names).
HINTS: dict[str, re.Pattern[str]] = {
    "greenhouse": re.compile(r"greenhouse", re.I),
    "lever": re.compile(r"\blever(?:-|_|\.co\b)|\bLever\b"),
    "ashby": re.compile(r"\bashby", re.I),
}

_NAME_NOISE = {
    "inc",
    "llc",
    "ltd",
    "corp",
    "corporation",
    "co",
    "company",
    "group",
    "the",
    "pbc",
    "plc",
    "holdings",
}


def hinted_types(text: str) -> list[str]:
    return [t for t, p in HINTS.items() if p.search(text or "")]


def _words(s: str) -> list[str]:
    return [w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in _NAME_NOISE]


def guess_tokens(company: str, career_url: str) -> list[str]:
    """Plausible board tokens from the company name and careers-site domain, most likely first."""
    name = re.split(r"\s+/\s+|\(", company or "")[0]
    words = _words(name)
    out = []
    if words:
        out += ["".join(words), "-".join(words), words[0]]
    host = re.sub(r"^https?://", "", career_url or "").split("/")[0].lower()
    labels = [
        x for x in host.split(".") if x not in ("www", "careers", "jobs", "com", "org", "net", "io", "co")
    ]
    if labels:
        out.append(labels[-1])
    seen, tokens = set(), []
    for t in out:
        if len(t) >= 3 and t not in seen:
            seen.add(t)
            tokens.append(t)
    return tokens[:4]


def board_name_matches(board_name: str, company: str) -> bool:
    a, b = _words(board_name), _words(re.split(r"\s+/\s+|\(", company or "")[0])
    return bool(a and b) and (a[0] in b or b[0] in a or "".join(a) == "".join(b))


_HREF = re.compile(r"""href=["']([^"'#]+)["']""", re.I)
_JOBS_LIST_PATH = re.compile(
    r"/(?:jobs|careers|openings|positions|join-us|work-with-us)(?:/[^?]*)?/"
    r"(?:search|all|openings|open-positions|positions|list|open-roles|current-openings|job-board)\b",
    re.I,
)


def jobs_list_links(page_url: str, html: str, limit: int = 2) -> list[str]:
    """Same-site links that look like a full jobs listing, e.g. /jobs/search or /careers/all."""
    site = urlsplit(page_url).hostname or ""
    out: list[str] = []
    for href in _HREF.findall(html or ""):
        url = urljoin(page_url, unescape(href)).split("?")[0]
        if urlsplit(url).hostname != site or url.rstrip("/") == page_url.rstrip("/"):
            continue
        if _JOBS_LIST_PATH.search(urlsplit(url).path + "/") and url not in out:
            out.append(url)
        if len(out) >= limit:
            break
    return out


def find_other_ats(text: str) -> str | None:
    for name, pattern in OTHER_ATS:
        if pattern.search(text or ""):
            return name
    return None


# --- confirmation via board APIs -----------------------------------------------------------------


@dataclass
class Confirmation:
    confirmed: bool | None  # None = could not check (disabled or network failure)
    note: str = ""
    job_count: int | None = None


def confirm(client: PoliteClient, c: Candidate, cfg: Config) -> Confirmation:
    try:
        return _confirm(client, c, cfg)
    except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
        return Confirmation(None, f"confirm failed: {type(e).__name__}: {e}"[:200])


def _confirm(client: PoliteClient, c: Candidate, cfg: Config) -> Confirmation:
    t = quote(c.token, safe="")
    if c.type == "greenhouse":
        r = client.get(f"https://boards-api.greenhouse.io/v1/boards/{t}")
        if r.status == 404:
            return Confirmation(False, "greenhouse board not found")
        return Confirmation(True, r.json().get("name", "")) if r.ok else _http_fail(r.status)
    if c.type == "lever":
        r = client.get(f"https://api.lever.co/v0/postings/{t}?mode=json")
        if r.status == 404:
            return Confirmation(False, "lever board not found")
        return Confirmation(True, job_count=len(r.json())) if r.ok else _http_fail(r.status)
    if c.type == "ashby":
        r = client.get(f"https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true")
        if r.status == 404:
            return Confirmation(False, "ashby board not found")
        return Confirmation(True, job_count=len(r.json().get("jobs", []))) if r.ok else _http_fail(r.status)
    if c.type == "smartrecruiters":
        r = client.get(f"https://api.smartrecruiters.com/v1/companies/{t}/postings?limit=1")
        if not r.ok:
            return (
                Confirmation(False, "smartrecruiters company not found")
                if r.status == 404
                else _http_fail(r.status)
            )
        n = r.json().get("totalFound", 0)
        # SmartRecruiters answers 200 with zero postings for companies that don't exist, so zero is ambiguous.
        if n == 0:
            return Confirmation(None, "smartrecruiters returned 0 postings; token unconfirmed", 0)
        return Confirmation(True, job_count=n)
    if c.type == "workday":
        if not cfg.watch.workday_enabled:
            return Confirmation(None, "workday API disabled in config")
        tenant, n, site = c.token.split("|")
        r = client.post_json(
            f"https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
            {"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
        )
        if r.status in (404, 422):
            return Confirmation(False, f"workday site not found ({r.status})")
        return Confirmation(True, job_count=r.json().get("total")) if r.ok else _http_fail(r.status)
    raise ValueError(f"unsupported type {c.type}")


def _http_fail(status: int) -> Confirmation:
    return Confirmation(None, f"board API returned HTTP {status}")


# --- per-company detection -----------------------------------------------------------------------

TEXT_FIELDS = ("career_url", "benefit_source", "research_notes")


@dataclass
class Detection:
    ats: dict[str, Any] | None  # None means nothing to check (no URLs at all)
    fetched_page: bool = False


def _ats(type_, token, detected_by, today, source, confirmed=None, note="", job_count=None):
    return {
        "type": type_,
        "token": token,
        "board_url": board_url(type_, token) if token and type_ in SUPPORTED else None,
        "detected_by": detected_by,
        "checked_at": today,
        "confirmed": confirmed,
        "source": source,
        "note": note,
        "job_count": job_count,
    }


def _try_candidates(client, cfg, candidates, detected_by, today, source):
    """(ats, rejected): ats for the first confirmed candidate, else the first that couldn't be checked;
    rejected lists candidates whose board API said they don't exist."""
    fallback = None
    rejected: list[str] = []
    for c in candidates:
        conf = confirm(client, c, cfg)
        if conf.confirmed:
            ats = _ats(c.type, c.token, detected_by, today, source, True, conf.note, conf.job_count)
            return ats, rejected
        if conf.confirmed is None and fallback is None:
            fallback = _ats(c.type, c.token, detected_by, today, source, None, conf.note, conf.job_count)
        if conf.confirmed is False:
            rejected.append(f"{c.type}:{c.token}")
    return fallback, rejected


def _with_rejects(note: str, rejected: list[str]) -> str:
    return f"{note}; board API 404 for {', '.join(rejected)}" if rejected else note


def detect_company(record: dict[str, Any], client: PoliteClient, cfg: Config, today: str) -> Detection:
    t = record["tracker"]
    career_url = t.get("career_url") or ""
    source = career_url or None
    text = "\n".join(str(t.get(f) or "") for f in TEXT_FIELDS)

    candidates = find_candidates(text)
    ats, rejected = _try_candidates(client, cfg, candidates, "url-pattern", today, source)
    if ats:
        return Detection(ats)

    def unknown(note: str, by: str = "html-scan") -> dict[str, Any]:
        confirmed = False if rejected else None
        return _ats("unknown", None, by, today, source, confirmed, _with_rejects(note, rejected))

    if not career_url.lower().startswith(("http://", "https://")):
        return Detection(unknown("no careers page to scan", "url-pattern") if rejected else None)

    try:
        page = client.get(career_url)
    except BlockedHostError as e:
        return Detection(unknown(str(e)))
    except httpx.HTTPError as e:
        return Detection(unknown(f"fetch failed: {type(e).__name__}"))

    tried = set(candidates)

    def scan(text: str) -> dict[str, Any] | None:
        fresh = [c for c in find_candidates(text) if c not in tried]
        tried.update(fresh)
        found, rej = _try_candidates(client, cfg, fresh, "html-scan", today, source)
        rejected.extend(rej)
        return found

    scanned = f"{page.url}\n{page.text}"
    if ats := scan(scanned):
        return Detection(ats, fetched_page=True)
    if not page.ok:
        return Detection(unknown(f"careers page HTTP {page.status}"), fetched_page=True)

    # One hop: a "see all jobs" page on the same site often embeds the board.
    for link in jobs_list_links(page.url, page.text):
        try:
            sub = client.get(link)
        except httpx.HTTPError:
            continue
        if not sub.ok:
            continue
        scanned += f"\n{sub.url}\n{sub.text}"
        if ats := scan(f"{sub.url}\n{sub.text}"):
            return Detection(ats, fetched_page=True)

    # The page names a system but doesn't link it: try tokens guessed from the company's name and domain.
    company = t.get("company") or ""
    for ats_type in hinted_types(scanned):
        for token in guess_tokens(company, career_url):
            c = Candidate(ats_type, token)
            if c in tried:
                continue
            tried.add(c)
            conf = confirm(client, c, cfg)
            if not conf.confirmed:
                continue
            if ats_type == "greenhouse" and not board_name_matches(conf.note, company):
                continue
            note = f"token guessed from name; page mentions {ats_type}" + (
                f"; board name {conf.note}" if conf.note else ""
            )
            return Detection(
                _ats(ats_type, token, "name-guess", today, source, True, note, conf.job_count),
                fetched_page=True,
            )
    other = find_other_ats(scanned)
    note = _with_rejects(f"uses {other}" if other else "no known job-board system on careers page", rejected)
    return Detection(_ats("other", None, "html-scan", today, source, note=note), fetched_page=True)


def needs_detection(record: dict[str, Any], today: date, recheck_days: int, force: bool = False) -> bool:
    if is_skipped(record) or record.get("missing_from_tracker"):
        return False
    if force:
        return True
    ats = record.get("ats")
    if not ats:
        return True
    if ats.get("detected_by") == "agent":
        return False  # verify agent found it; the agent re-checks on refresh
    if ats.get("source") != (record["tracker"].get("career_url") or None):
        return True  # owner changed the careers URL
    if ats.get("confirmed") is True:
        return False
    checked = date.fromisoformat(ats["checked_at"])
    return (today - checked).days >= recheck_days
