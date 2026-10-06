"""Greenhouse job board API.

List:    GET boards-api.greenhouse.io/v1/boards/{t}/jobs            (titles and locations only)
Detail:  GET boards-api.greenhouse.io/v1/boards/{t}/jobs/{id}?pay_transparency=true

The plan's `?content=true` list embeds every description; for big boards (Axon: >8 MB) that is
wasteful, so the watcher lists without content and fetches detail only for titles that pass the
filter. Detail also carries structured pay (`pay_input_ranges`).
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobsearch.boards.common import JobPosting, apply_text_pay, html_to_text, iso_date, work_mode_from


def api_url(token: str) -> str:
    return f"https://boards-api.greenhouse.io/v1/boards/{quote(token, safe='')}/jobs"


def detail_url(token: str, job_id: Any) -> str:
    return f"{api_url(token)}/{job_id}?pay_transparency=true"


def normalize_job(j: dict[str, Any], company_id: str, token: str) -> JobPosting:
    """One job from the list (no content) or from the detail endpoint (content and pay)."""
    location = (j.get("location") or {}).get("name") or ""
    offices = ", ".join(o.get("name", "") for o in j.get("offices") or [] if o.get("name"))
    loc_text = location if not offices or offices in location else f"{location} ({offices})"
    text = html_to_text(j.get("content"))
    p: dict[str, Any] = {
        "board": "greenhouse",
        "board_job_id": str(j["id"]),
        "company_id": company_id,
        "title": (j.get("title") or "").strip(),
        "location_text": loc_text.strip(),
        "is_remote": None,
        "work_mode": work_mode_from(loc_text),
        "url": j.get("absolute_url") or f"https://job-boards.greenhouse.io/{token}/jobs/{j['id']}",
        "posted_at": iso_date(j.get("first_published") or j.get("updated_at")),
        "raw_ref": detail_url(token, j["id"]) if "content" in j else api_url(token),
        "description_text": text,
    }
    for r in j.get("pay_input_ranges") or []:
        if r.get("min_cents") is not None and r.get("max_cents") is not None:
            p.update(
                pay_low=r["min_cents"] / 100,
                pay_high=r["max_cents"] / 100,
                currency=r.get("currency_type") or "USD",
                pay_period="hour" if r["max_cents"] < 50_000 else "year",
            )
            break
    apply_text_pay(p, text)
    return JobPosting(**p)


def normalize(data: dict[str, Any], company_id: str, token: str) -> list[JobPosting]:
    return [normalize_job(j, company_id, token) for j in data.get("jobs", [])]
