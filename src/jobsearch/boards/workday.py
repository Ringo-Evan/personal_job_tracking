"""Workday career sites (unofficial cxs API; behind watch.workday_enabled).

Search:  POST https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs
Detail:  GET  https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}{externalPath}
Search results only say "2 Locations", so the watcher fetches detail for titles that pass the filter.
"""

from __future__ import annotations

from typing import Any

from jobsearch.boards.common import JobPosting, apply_text_pay, html_to_text, iso_date, work_mode_from

PAGE = 20
MAX_PER_TERM = 100


def split_token(token: str) -> tuple[str, str, str]:
    tenant, n, site = token.split("|")
    return tenant, n, site


def base_url(token: str) -> str:
    tenant, n, site = split_token(token)
    return f"https://{tenant}.wd{n}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"


def search_body(term: str, offset: int) -> dict[str, Any]:
    return {"appliedFacets": {}, "limit": PAGE, "offset": offset, "searchText": term}


def posting_id(item: dict[str, Any]) -> str:
    bullets = item.get("bulletFields") or []
    return bullets[0] if bullets else item["externalPath"].rsplit("/", 1)[-1]


def normalize_detail(detail: dict[str, Any], item: dict[str, Any], company_id: str, token: str) -> JobPosting:
    info = detail.get("jobPostingInfo") or {}
    tenant, n, site = split_token(token)
    locations = [info.get("location") or item.get("locationsText") or ""] + list(
        info.get("additionalLocations") or []
    )
    loc_text = "; ".join(x for x in locations if x)
    remote_type = (info.get("remoteType") or "").lower()
    text = html_to_text(info.get("jobDescription"))
    p: dict[str, Any] = {
        "board": "workday",
        "board_job_id": info.get("jobReqId") or posting_id(item),
        "company_id": company_id,
        "title": (info.get("title") or item.get("title") or "").strip(),
        "location_text": loc_text + (f" ({info['remoteType']})" if info.get("remoteType") else ""),
        "is_remote": True if "remote" in remote_type else None,
        "work_mode": work_mode_from(f"{loc_text} {remote_type}"),
        "url": info.get("externalUrl")
        or f"https://{tenant}.wd{n}.myworkdayjobs.com/{site}{item['externalPath']}",
        "posted_at": iso_date(info.get("startDate")),
        "raw_ref": base_url(token) + item["externalPath"],
        "description_text": text,
    }
    apply_text_pay(p, text)
    return JobPosting(**p)
