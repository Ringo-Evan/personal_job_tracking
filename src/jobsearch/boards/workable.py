"""Workable widget API: GET apply.workable.com/api/v1/widget/accounts/{t}?details=true (all jobs with descriptions)"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobsearch.boards.common import JobPosting, apply_text_pay, html_to_text, iso_date, work_mode_from


def api_url(token: str, details: bool = True) -> str:
    return f"https://apply.workable.com/api/v1/widget/accounts/{quote(token, safe='')}" + (
        "?details=true" if details else ""
    )


def _places(j: dict[str, Any]) -> list[str]:
    locs = [x for x in j.get("locations") or [] if isinstance(x, dict) and not x.get("hidden")] or [
        {"city": j.get("city"), "region": j.get("state"), "country": j.get("country")}
    ]
    places = [", ".join(str(x[k]) for k in ("city", "region", "country") if x.get(k)) for x in locs]
    return list(dict.fromkeys(p for p in places if p))


def normalize(data: dict[str, Any], company_id: str, token: str) -> list[JobPosting]:
    out = []
    for j in data.get("jobs", []):
        title = (j.get("title") or "").strip()
        remote = j.get("telecommuting")
        places = _places(j)
        # Workable flags remote jobs but lists only where they're open; say so, as other boards do.
        loc_text = ("; ".join(f"Remote ({p})" for p in places) or "Remote") if remote else "; ".join(places)
        text = html_to_text(j.get("description"))
        p: dict[str, Any] = {
            "board": "workable",
            "board_job_id": j["shortcode"],
            "company_id": company_id,
            "title": title,
            "location_text": loc_text,
            "is_remote": remote if isinstance(remote, bool) else None,
            "work_mode": work_mode_from(f"{title} {loc_text}", remote if isinstance(remote, bool) else None),
            "url": j.get("url")
            or j.get("shortlink")
            or f"https://apply.workable.com/{token}/j/{j['shortcode']}",
            "apply_url": j.get("application_url"),
            "posted_at": iso_date(j.get("published_on") or j.get("created_at")),
            "raw_ref": api_url(token),
            "description_text": text,
        }
        apply_text_pay(p, text)
        out.append(JobPosting(**p))
    return out
