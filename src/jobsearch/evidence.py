"""Evidence gathering for verification (plan-models.md §4 step 1): code browses, models read.

SearXNG queries plus the known careers page and board data become a numbered evidence pack
(`[{id: "S1", url, title, fetched_at, text}]`) capped at `evidence.max_total_chars`. No LLM here.
Packs are cached under cache/evidence/ for `http.cache_hours`, so a rerun or a tier comparison reads the same pack.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx

from jobsearch.boards.common import html_to_text
from jobsearch.config import ROOT, Config
from jobsearch.detect import Candidate, find_candidates
from jobsearch.http_client import BlockedHostError, PoliteClient, is_blocked_host

EVIDENCE_CACHE = ROOT / "cache" / "evidence"
QUERIES = (
    '"{company}" careers',
    '"{company}" remote work policy',
    '"{company}" layoffs OR funding OR acquisition 2025 OR 2026',
    '"{company}" engineering OR QA OR quality team',
)
# Social sites: little evidence, lots of login walls, and personal profiles we must not collect.
SKIP_HOSTS = (
    "linkedin.com", "facebook.com", "instagram.com", "twitter.com", "x.com", "tiktok.com", "youtube.com",
    "pinterest.com", "threads.net", "reddit.com", "glassdoor.com", "indeed.com", "ziprecruiter.com",
    "thelayoff.com", "teamblind.com",  # anonymous forums: unverifiable claims, not evidence
)  # fmt: skip
SKIP_EXT = (".pdf", ".doc", ".docx", ".ppt", ".pptx", ".xls", ".xlsx", ".zip", ".jpg", ".png", ".mp4")
MIN_PAGE_CHARS = 200  # less than this is a login wall, cookie banner or error page


@dataclass
class Source:
    id: str
    url: str
    title: str
    fetched_at: str
    text: str


@dataclass
class Pack:
    company_id: str
    built_at: float
    sources: list[Source] = field(default_factory=list)
    ats_candidates: list[tuple[str, str]] = field(default_factory=list)  # (type, token) seen in fetched HTML
    searches: int = 0
    fetches: int = 0
    failures: list[str] = field(default_factory=list)

    def by_id(self) -> dict[str, Source]:
        return {s.id: s for s in self.sources}

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_json(cls, d: dict[str, Any]) -> Pack:
        d = dict(d)
        d["sources"] = [Source(**s) for s in d["sources"]]
        d["ats_candidates"] = [tuple(c) for c in d.get("ats_candidates", [])]
        return cls(**d)


def search_name(company: str) -> str:
    """'Roll20 / Demiplane' -> 'Roll20'; 'Acme (formerly Foo)' -> 'Acme'. Search engines choke on the full label."""
    return re.split(r"\s+/\s+|\s*\(", company.strip(), maxsplit=1)[0].strip() or company.strip()


def skip_url(url: str) -> bool:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return True
    if is_blocked_host(url) or any(host == h or host.endswith("." + h) for h in SKIP_HOSTS):
        return True
    return parts.path.lower().endswith(SKIP_EXT)


def canonical(url: str) -> str:
    p = urlsplit(url.strip())
    return urlunsplit((p.scheme.lower(), (p.netloc or "").lower(), p.path.rstrip("/") or "/", p.query, ""))


def page_text(html: str, url: str) -> tuple[str, str]:
    """(title, main text) via trafilatura, falling back to a plain tag strip."""
    import trafilatura

    text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True, favor_recall=True)
    title = ""
    try:
        meta = trafilatura.extract_metadata(html)
        title = (meta.title or "") if meta else ""
    except Exception:  # metadata is a nicety; never fail a page over it
        title = ""
    if not text:
        text = html_to_text(html)
    return title.strip(), re.sub(r"[ \t]+", " ", text or "").strip()


def search(client: PoliteClient, cfg: Config, query: str) -> list[dict[str, Any]]:
    url = cfg.search.searxng_url.rstrip("/") + "/search?" + urlencode({"q": query, "format": "json"})
    r = client.get(url)
    if not r.ok:
        raise httpx.HTTPError(f"SearXNG HTTP {r.status} for {query!r}")
    return [x for x in r.json().get("results", []) if x.get("url")]


def board_source_text(record: dict[str, Any], jobs: list[dict[str, Any]]) -> str | None:
    """Known board info and the watcher's open matching postings, as a source the extractor can quote."""
    ats = record.get("ats") or {}
    if not ats.get("confirmed") or not ats.get("board_url"):
        return None
    lines = [f"Job board: {ats['type']} (token {ats['token']}), board URL {ats['board_url']}."]
    open_jobs = [j for j in jobs if not j.get("closed_at")]
    if open_jobs:
        lines.append("Open postings matching QA/test/quality titles:")
        lines += [f"- {j['title']} | {j.get('location_text') or ''} | {j.get('work_mode') or 'Unknown'} | "
                  f"{j.get('url') or ''}" for j in open_jobs[:20]]  # fmt: skip
    else:
        lines.append("No open postings matching QA/test/quality titles right now.")
    return "\n".join(lines)


def _ordered_candidates(career_url: str | None, results: list[list[dict[str, Any]]]) -> list[tuple[str, str]]:
    """Careers page first, then round-robin across queries so each query contributes its best hits."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(url: str, title: str) -> None:
        key = canonical(url)
        if key not in seen and not skip_url(url):
            seen.add(key)
            out.append((url, title))

    if career_url:
        add(career_url, "Careers page (from tracker)")
    depth = max((len(r) for r in results), default=0)
    for rank in range(depth):
        for r in results:
            if rank < len(r):
                add(r[rank]["url"], r[rank].get("title") or "")
    return out


def gather(
    record: dict[str, Any],
    client: PoliteClient,
    cfg: Config,
    *,
    jobs: list[dict[str, Any]] | None = None,
    cache_dir: Path = EVIDENCE_CACHE,
    clock=time.time,
    use_cache: bool = True,
) -> Pack:
    cid = record["company_id"]
    cache = cache_dir / f"{cid}.json"
    if use_cache and cache.exists():
        try:
            pack = Pack.from_json(json.loads(cache.read_text(encoding="utf-8")))
            if clock() - pack.built_at < cfg.http.cache_hours * 3600:
                return pack
        except (OSError, ValueError, TypeError, KeyError):
            pass

    t = record["tracker"]
    ev = cfg.evidence
    pack = Pack(cid, clock())
    today = time.strftime("%Y-%m-%d", time.localtime(clock()))
    budget = ev.max_total_chars

    def add_source(url: str, title: str, text: str) -> None:
        nonlocal budget
        text = text[: min(ev.max_chars_per_page, budget)]
        if not text:
            return
        pack.sources.append(Source(f"S{len(pack.sources) + 1}", url, title[:200], today, text))
        budget -= len(text)

    if board_text := board_source_text(record, jobs or []):
        add_source(record["ats"]["board_url"], "Job board data (pipeline watcher)", board_text)

    name = search_name(t.get("company") or "")
    results: list[list[dict[str, Any]]] = []
    for template in QUERIES:
        q = template.format(company=name)
        pack.searches += 1
        try:
            results.append(search(client, cfg, q))
        except (httpx.HTTPError, ValueError) as e:
            pack.failures.append(f"search {q!r}: {e}"[:200])
            results.append([])

    pages = 0
    found: set[Candidate] = set()
    for url, title in _ordered_candidates(t.get("career_url"), results):
        if pages >= ev.max_pages or budget <= 0 or pack.fetches >= ev.max_pages * 2:
            break
        pack.fetches += 1
        try:
            r = client.get(url)
        except (httpx.HTTPError, BlockedHostError) as e:
            pack.failures.append(f"fetch {url}: {type(e).__name__}"[:200])
            continue
        if not r.ok or skip_url(r.url):
            pack.failures.append(f"fetch {url}: HTTP {r.status}")
            continue
        found.update(find_candidates(r.url + "\n" + r.text))
        page_title, text = page_text(r.text, r.url)
        if len(text) < MIN_PAGE_CHARS:
            pack.failures.append(f"fetch {url}: too little text ({len(text)} chars)")
            continue
        add_source(r.url, page_title or title, text)
        pages += 1
    pack.ats_candidates = sorted((c.type, c.token) for c in found)

    if use_cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(pack.to_json(), ensure_ascii=False), encoding="utf-8")
    return pack


def render(pack: Pack) -> str:
    """The pack as prompt text. Each source is fenced so page text can't pose as prompt structure."""
    parts = []
    for s in pack.sources:
        parts.append(f'<source id="{s.id}" url="{s.url}" fetched="{s.fetched_at}">\n'
                     f"title: {s.title}\n{s.text}\n</source>")  # fmt: skip
    return "\n\n".join(parts) if parts else "(no sources could be fetched)"
