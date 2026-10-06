"""Board discovery by web search (no LLM) for companies detect couldn't place.

Most tracker companies have no careers URL, so the careers-page scan (phase 2) never sees them. For each company
without a confirmed board this runs one SearXNG query ('"{name}" careers') and then:
1. takes job-board links from the result URLs and snippets whose token resembles the company name;
2. scans the company's own careers page, when a result is one, for an embedded board (the same scan as detect);
3. guesses board tokens from the name: Greenhouse's API returns the board's name to check against; a guessed
   Lever or Ashby board is kept only if its postings mention the company.
A board is adopted only when its API confirms it. Unsupported systems (iCIMS, Workable, ...) are noted for the
owner. Search queries carry only the company name.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from jobsearch.config import Config
from jobsearch.detect import (
    OTHER_ATS,
    SUPPORTED,
    Candidate,
    _ats,
    _words,
    confirm,
    detect_company,
    find_candidates,
    guess_tokens,
)
from jobsearch.evidence import search, search_name, skip_url
from jobsearch.http_client import PoliteClient
from jobsearch.models import is_skipped

QUERY = '"{company}" careers'
# Job aggregators, review and data sites: never the company's own careers page or board.
NOT_CAREERS_HOSTS = (
    "indeed.com", "glassdoor.com", "ziprecruiter.com", "builtin.com", "simplyhired.com", "monster.com",
    "wellfound.com", "comparably.com", "levels.fyi", "dice.com", "careerbuilder.com", "salary.com", "payscale.com",
    "crunchbase.com", "zoominfo.com", "wikipedia.org", "bloomberg.com", "theorg.com", "rocketreach.co", "lensa.com",
    "jooble.org", "talent.com", "themuse.com", "careerjet.com", "jobleads.com", "remoterocketship.com",
    "himalayas.app", "otta.com", "welcometothejungle.com", "workingnomads.com", "weworkremotely.com",
    "remoteok.com", "flexjobs.com", "jobgether.com", "startup.jobs", "ycombinator.com", "craft.co", "owler.com",
    "kununu.com", "energyjobline.com", "jobs.lever.co", "simplify.jobs", "trueup.io", "teal.com", "tealhq.com",
    "getwork.com", "careerbliss.com", "jobright.ai", "learn4good.com", "hiringcafe.com", "builtinboston.com",
)  # fmt: skip
_CAREERS_WORD = re.compile(r"(?i)career|jobs?\b|join|work-?with-?us|opportunit|hiring|openings|vacanc")
_GENERIC_LABELS = {"www", "careers", "career", "jobs", "job", "com", "org", "net", "io", "co", "us", "gov", "edu"}
NAME_GUESS_TYPES = ("lever", "ashby")  # besides greenhouse, whose API reports the board's name


def _is_host(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def _alnum(s: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (s or "").lower())


# Words that don't tell two companies apart. "Zoll Medical" is "zoll"; "NATIONAL" names nobody in particular.
GENERIC_WORDS = {
    "careers", "career", "jobs", "job", "join", "team", "explore", "at", "for", "of", "and", "technologies",
    "technology", "tech", "software", "systems", "solutions", "labs", "lab", "laboratory", "games", "gaming",
    "studios", "studio", "entertainment", "interactive", "digital", "health", "healthcare", "medical",
    "international", "global", "usa", "us", "america", "national", "office", "product", "products", "applications",
    "apps", "services", "network", "com", "io", "ai", "hq", "foundation", "robotics",
}  # fmt: skip


def _distinctive(name: str) -> list[str]:
    return [w for w in _words(name) if w not in GENERIC_WORDS]


def token_matches(token: str, company: str) -> bool:
    """A board token, Workday tenant or domain label that plausibly belongs to the company:
    'irongalaxystudios' or 'careersirongalaxystudios' for 'Iron Galaxy'; 'zoll' for 'Zoll Medical'.
    A fragment of a longer name doesn't count: 'mit' is not 'MIT Lincoln Laboratory'."""
    t = _alnum(token)
    full = "".join(_words(search_name(company)))
    core = "".join(_distinctive(search_name(company)))
    if len(t) < 3 or not core:
        return False
    if t in (full, core):
        return True
    for name in {full, core}:
        if len(name) >= 4 and name in t:
            return True
        if len(t) >= 5 and t in name and len(t) >= 0.6 * len(name):
            return True
    return False


def name_matches(board_name: str, company: str) -> bool:
    """A board's own display name (Greenhouse reports it) names this company, not a namesake: the distinctive words
    must be the same. 'Agile Six Applications' is 'Agile Six'; 'Action Garage Doors' is not 'Action Network', and
    'NATIONAL' is not 'National Instruments' (false matches found in the 2026-10-06 audit)."""
    a, b = set(_distinctive(board_name)), set(_distinctive(search_name(company)))
    return bool(a) and a == b


_COUNTRY_SLD = re.compile(r"\.(?:com|co|org|net|gov|ac)\.[a-z]{2}$")


def foreign_site(host: str) -> bool:
    """A country-specific site such as firstnationalbank.com.gh: a namesake abroad, not the tracker's company."""
    return bool(_COUNTRY_SLD.search(host))


def board_candidates(results: list[dict[str, Any]], company: str) -> list[Candidate]:
    """Supported boards linked from search results (URL or snippet) whose token resembles the company name."""
    out: list[Candidate] = []
    for r in results:
        for c in find_candidates(f"{r.get('url', '')}\n{r.get('content', '')}"):
            name = c.token.split("|")
            if any(token_matches(part, company) for part in (name[0], name[-1])) and c not in out:
                out.append(c)
    return out


def other_boards(results: list[dict[str, Any]], company: str) -> list[str]:
    """Unsupported job-board systems seen in result URLs for this company, e.g. 'jazzhr https://acme.applytojob.com/'."""
    out = []
    for r in results:
        url = r.get("url", "")
        for name, pattern in OTHER_ATS:
            if pattern.search(url):
                labels = re.split(r"[./]", urlsplit(url).netloc + urlsplit(url).path)
                if any(token_matches(x, company) for x in labels if x) and not any(
                    o.startswith(name + " ") for o in out
                ):
                    out.append(f"{name} {url}")
                break
    return out


def careers_page(results: list[dict[str, Any]], company: str) -> str | None:
    """The company's own careers page among the results: a careers-like URL on a domain named after it."""
    for r in results:
        url = r.get("url", "")
        if skip_url(url):
            continue
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if _is_host(host, NOT_CAREERS_HOSTS) or find_candidates(url) or foreign_site(host):
            continue
        if any(p.search(url) for _, p in OTHER_ATS):
            continue
        labels = [x for x in host.split(".") if x not in _GENERIC_LABELS]
        if not any(token_matches(x, company) for x in labels):
            continue
        if _CAREERS_WORD.search(host.split(".")[0] + " " + parts.path):
            return url
    return None


def _postings_mention(client: PoliteClient, c: Candidate, company: str) -> bool:
    """For a guessed Lever or Ashby token: do the board's postings name the company? (cached by the client)"""
    t = quote(c.token, safe="")
    url = (
        f"https://api.lever.co/v0/postings/{t}?mode=json"
        if c.type == "lever"
        else f"https://api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true"
    )
    try:
        r = client.get(url)
        data = r.json() if r.ok else None
    except (httpx.HTTPError, ValueError):
        return False
    jobs = data if isinstance(data, list) else (data or {}).get("jobs", [])
    text = " ".join(
        str(j.get("descriptionPlain") or j.get("description") or j.get("additionalPlain") or "")
        for j in jobs[:15]
        if isinstance(j, dict)
    ).lower()
    # The full name, or its first word when that is distinctive enough ("Veeva" for "Veeva Systems").
    words = _words(search_name(company))
    probes = {search_name(company).lower()} | ({words[0]} if words and len(words[0]) >= 5 else set())
    return any(re.search(r"(?<!\w)" + re.escape(p) + r"(?!\w)", text) for p in probes if p)


@dataclass
class Discovery:
    ats: dict[str, Any] | None  # a confirmed supported board, or None
    info: dict[str, Any] = field(default_factory=dict)  # stored as record["discovery"]
    requests: int = 0


def discover_company(
    record: dict[str, Any],
    client: PoliteClient,
    cfg: Config,
    today: str,
    results: list[dict[str, Any]],
) -> Discovery:
    """Find a confirmed board from one company's search results (plus a careers-page scan and name guesses)."""
    t = record["tracker"]
    company = t.get("company") or ""
    source = t.get("career_url") or None
    info: dict[str, Any] = {"at": today, "results": len(results), "careers_page": None, "found": None, "other": []}

    def adopt(c: Candidate, how: str, note: str, job_count) -> Discovery:
        info["found"] = f"{c.type}:{c.token} ({how})"
        ats = _ats(c.type, c.token, "search", today, source, True, f"{how}; {note}".strip("; "), job_count)
        return Discovery(ats, info)

    tried: set[Candidate] = set()
    for c in board_candidates(results, company):
        tried.add(c)
        conf = confirm(client, c, cfg)
        if not conf.confirmed:
            continue
        if c.type == "greenhouse" and conf.note and not name_matches(conf.note, company):
            continue
        return adopt(c, "linked from search results", conf.note, conf.job_count)

    info["other"] = other_boards(results, company)
    page = careers_page(results, company)
    info["careers_page"] = page
    if page and page != source:
        scanned = detect_company({**record, "tracker": {**t, "career_url": page}}, client, cfg, today).ats
        if scanned and scanned.get("confirmed") and scanned.get("type") in SUPPORTED:
            c = Candidate(scanned["type"], scanned["token"])
            return adopt(c, f"embedded in {page}", scanned.get("note") or "", scanned.get("job_count"))
        if scanned and scanned.get("note") and scanned.get("type") == "other":
            info["careers_page_note"] = scanned["note"][:200]

    for token in guess_tokens(company, source or page or ""):
        c = Candidate("greenhouse", token)
        if c in tried:
            continue
        tried.add(c)
        conf = confirm(client, c, cfg)
        if conf.confirmed and name_matches(conf.note, company):
            return adopt(c, "greenhouse token guessed from name", f"board name {conf.note}", conf.job_count)
    joined = "".join(_words(search_name(company)))
    if len(joined) >= 5:
        for type_ in NAME_GUESS_TYPES:
            c = Candidate(type_, joined)
            if c in tried:
                continue
            tried.add(c)
            conf = confirm(client, c, cfg)
            if conf.confirmed and conf.job_count and _postings_mention(client, c, company):
                return adopt(c, f"{type_} token guessed from name", "postings name the company", conf.job_count)
    return Discovery(None, info)


def needs_discovery(record: dict[str, Any], today: date, recheck_days: int, force: bool = False) -> bool:
    if is_skipped(record) or record.get("missing_from_tracker"):
        return False
    ats = record.get("ats") or {}
    if ats.get("type") in SUPPORTED and ats.get("confirmed") is True:
        return False
    if force:
        return True
    try:
        age = (today - date.fromisoformat((record.get("discovery") or {})["at"])).days
    except (KeyError, TypeError, ValueError):
        return True
    return age >= recheck_days


def discovery_order(record: dict[str, Any]) -> tuple:
    """Best triage fit first; owner-researched companies (no triage) count as fit 4."""
    tri = record.get("triage") or {}
    fit = tri.get("fit") if tri else 4
    remote = {"High": 0, "Medium": 1, "Unknown": 2, "Low": 3}.get(tri.get("remote") or "Unknown", 2)
    return (-(fit or 0), remote, -(tri.get("stability") or 0))


@dataclass
class DiscoverReport:
    searched: int = 0
    found: list[str] = field(default_factory=list)  # "C123 greenhouse:acme (how)"
    no_results: int = 0
    careers_pages: int = 0
    other: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    stopped: str = ""

    def counts(self) -> dict[str, int]:
        return {
            "searched": self.searched,
            "found": len(self.found),
            "no_results": self.no_results,
            "careers_pages": self.careers_pages,
            "other_boards": len(self.other),
            "errors": len(self.errors),
        }


def run_discover(
    store,
    client: PoliteClient,
    cfg: Config,
    *,
    today: str,
    now: str,
    records: list[dict[str, Any]],
    pause_s: float = 4.0,
    max_dry: int = 8,
    sleep: Callable[[float], None] = time.sleep,
    progress=lambda msg: None,
) -> DiscoverReport:
    """Search for each record in order; save adopted boards and the discovery note. Stops if the search engines
    keep returning nothing (rate-limited upstream), so a rerun picks up where this left off."""
    report = DiscoverReport()
    dry, backoffs = 0, 0
    for record in records:
        cid = record["company_id"]
        company = record["tracker"].get("company") or ""
        query = QUERY.format(company=search_name(company))
        try:
            results = search(client, cfg, query)
        except (httpx.HTTPError, ValueError) as e:
            report.errors.append(f"{cid}: {e}"[:200])
            progress(f"  ERR  {cid} {e}"[:150])
            continue
        report.searched += 1
        if not results:
            report.no_results += 1
            dry += 1
            progress(f"  ---  {cid} {company[:40]}: no results")
            if dry >= max_dry:
                if backoffs >= 2:
                    report.stopped = f"{dry} searches in a row returned nothing; search engines look rate-limited"
                    break
                backoffs += 1
                progress(f"  {dry} empty searches in a row; pausing {120 * backoffs}s")
                sleep(120 * backoffs)
                dry = 0
            continue
        dry = 0
        try:
            d = discover_company(record, client, cfg, today, results)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as e:
            report.errors.append(f"{cid}: {type(e).__name__}: {e}"[:200])
            continue
        if d.info.get("careers_page"):
            report.careers_pages += 1
        report.other += [f"{cid} {o}" for o in d.info.get("other", [])]
        # Re-read before saving: verify may have saved this company while we searched.
        record = store.load_company(cid) or record
        record["discovery"] = d.info
        changed = ["discovery"]
        if d.ats and (record.get("ats") or {}).get("confirmed") is True:
            d.info["found"] = f"{d.info['found']}; kept the board found meanwhile"
            d.ats = None
        if d.ats:
            record["ats"] = d.ats
            changed.append("ats")
            report.found.append(f"{cid} {d.info['found']}")
            progress(f"  FOUND {cid} {company[:32]}: {d.info['found']}")
        else:
            progress(
                f"  ..   {cid} {company[:32]}: {len(results)} results"
                + (f", page {d.info['careers_page']}" if d.info.get("careers_page") else "")
                + (f", {'; '.join(d.info['other'])}" if d.info.get("other") else "")
            )
        record.setdefault("history", []).append({"at": now, "stage": "discover", "changed": changed})
        store.save_company(record)
        sleep(pause_s)
    return report
