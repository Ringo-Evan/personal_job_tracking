"""Fetch and normalize postings from each supported job-board system."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx

from jobsearch.boards import ashby, greenhouse, lever, smartrecruiters, workday
from jobsearch.boards.common import BoardError, JobPosting
from jobsearch.config import Config
from jobsearch.http_client import PoliteClient, Response

__all__ = ["BoardError", "JobPosting", "fetch_postings"]


def _json(r: Response) -> Any:
    if not r.ok:
        raise BoardError(f"HTTP {r.status} from {r.url}")
    try:
        return r.json()
    except json.JSONDecodeError as e:
        raise BoardError(f"bad JSON from {r.url}: {e}") from e


def fetch_postings(
    client: PoliteClient,
    cfg: Config,
    ats_type: str,
    token: str,
    company_id: str,
    wanted: Callable[[str], bool] = lambda title: True,
) -> list[JobPosting]:
    """All current postings on one board. `wanted(title)` lets Workday skip detail calls for other roles.

    Watcher fetches bypass the cache so every run sees the live board. Raises BoardError on failure.
    """
    try:
        return _fetch(client, cfg, ats_type, token, company_id, wanted)
    except httpx.HTTPError as e:
        raise BoardError(f"{type(e).__name__}: {e}") from e
    except (KeyError, TypeError, ValueError) as e:
        if isinstance(e, BoardError):
            raise
        raise BoardError(f"unexpected {ats_type} response shape: {type(e).__name__}: {e}") from e


def _fetch(client, cfg, ats_type, token, company_id, wanted) -> list[JobPosting]:
    get = lambda url: _json(client.get(url, use_cache=False))  # noqa: E731
    if ats_type == "greenhouse":
        out = []
        for j in get(greenhouse.api_url(token)).get("jobs", []):
            if wanted(j.get("title") or ""):
                j = get(greenhouse.detail_url(token, j["id"]))
            out.append(greenhouse.normalize_job(j, company_id, token))
        return out
    if ats_type == "lever":
        return lever.normalize(get(lever.api_url(token)), company_id, token)
    if ats_type == "ashby":
        return ashby.normalize(get(ashby.api_url(token)), company_id, token)
    if ats_type == "smartrecruiters":
        postings, offset = [], 0
        while True:
            page = get(smartrecruiters.api_url(token, offset))
            postings += smartrecruiters.normalize(page, company_id, token)
            offset += smartrecruiters.PAGE
            if offset >= page.get("totalFound", 0) or not page.get("content") or offset >= 2000:
                return postings
    if ats_type == "workday":
        if not cfg.watch.workday_enabled:
            return []
        return _fetch_workday(client, cfg, token, company_id, wanted)
    raise BoardError(f"unsupported board type {ats_type}")


def _fetch_workday(client, cfg, token, company_id, wanted) -> list[JobPosting]:
    base = workday.base_url(token)
    items: dict[str, dict[str, Any]] = {}
    for term in cfg.watch.workday_terms:
        offset = 0
        while offset < workday.MAX_PER_TERM:
            page = _json(client.post_json(f"{base}/jobs", workday.search_body(term, offset), use_cache=False))
            for item in page.get("jobPostings") or []:
                items.setdefault(item["externalPath"], item)
            offset += workday.PAGE
            if offset >= (page.get("total") or 0):
                break
    out = []
    for path, item in items.items():
        if not wanted(item.get("title", "")):
            continue
        detail = _json(client.get(base + path, use_cache=False))
        out.append(workday.normalize_detail(detail, item, company_id, token))
    return out
