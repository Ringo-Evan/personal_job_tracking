"""Rippling ATS board API: GET api.rippling.com/platform/api/ats/v1/board/{t}/jobs, then /jobs/{uuid} for detail.

The list has no descriptions and repeats a job once per location, so jobs are merged by uuid first.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobsearch.boards.common import JobPosting, apply_text_pay, html_to_text, iso_date, work_mode_from


def api_url(token: str) -> str:
    return f"https://api.rippling.com/platform/api/ats/v1/board/{quote(token, safe='')}/jobs"


def detail_url(token: str, uuid: str) -> str:
    return f"{api_url(token)}/{quote(uuid, safe='')}"


def group(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """{uuid: {name, url, locations}} from the list endpoint, one entry per job."""
    jobs: dict[str, dict[str, Any]] = {}
    for item in items:
        job = jobs.setdefault(
            item["uuid"], {"name": item.get("name") or "", "url": item.get("url"), "locations": []}
        )
        label = (item.get("workLocation") or {}).get("label")
        if label and label not in job["locations"]:
            job["locations"].append(label)
    return jobs


def normalize_job(
    uuid: str, listed: dict[str, Any], detail: dict[str, Any] | None, company_id: str, token: str
) -> JobPosting:
    d = detail or {}
    locations = [x for x in d.get("workLocations") or [] if isinstance(x, str)] or listed["locations"]
    loc_text = "; ".join(locations)
    desc = d.get("description") or {}
    text = "\n".join(filter(None, [html_to_text(desc.get("role")), html_to_text(desc.get("company"))]))
    p: dict[str, Any] = {
        "board": "rippling",
        "board_job_id": uuid,
        "company_id": company_id,
        "title": (d.get("name") or listed["name"]).strip(),
        "location_text": loc_text,
        "is_remote": None,
        "work_mode": work_mode_from(loc_text),
        "url": d.get("url") or listed.get("url") or f"https://ats.rippling.com/{token}/jobs/{uuid}",
        "posted_at": iso_date(d.get("createdOn")),
        "raw_ref": detail_url(token, uuid) if detail else api_url(token),
        "description_text": text,
    }
    # payRangeDetails was empty on every job sampled (2026-10-06), so its shape is unknown; pay comes from the text.
    apply_text_pay(p, text)
    return JobPosting(**p)
