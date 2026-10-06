"""SmartRecruiters postings API: GET api.smartrecruiters.com/v1/companies/{t}/postings

The list endpoint has no description, so eligibility comes from location fields only.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobsearch.boards.common import JobPosting, iso_date, work_mode_from

PAGE = 100


def api_url(token: str, offset: int = 0) -> str:
    return f"https://api.smartrecruiters.com/v1/companies/{quote(token, safe='')}/postings?limit={PAGE}&offset={offset}"


def normalize(data: dict[str, Any], company_id: str, token: str) -> list[JobPosting]:
    out = []
    for j in data.get("content", []):
        loc = j.get("location") or {}
        loc_text = loc.get("fullLocation") or ", ".join(
            x for x in (loc.get("city"), loc.get("region"), (loc.get("country") or "").upper()) if x
        )
        loc_text = ", ".join(part.strip() for part in loc_text.split(",") if part.strip())
        if loc.get("remote"):
            mode = "Remote"
        elif loc.get("hybrid"):
            mode = "Hybrid"
        else:
            mode = work_mode_from(loc_text, False if loc_text else None)
        out.append(
            JobPosting(
                board="smartrecruiters",
                board_job_id=str(j["id"]),
                company_id=company_id,
                title=(j.get("name") or "").strip(),
                location_text=loc_text + (" (Remote)" if loc.get("remote") else ""),
                is_remote=loc.get("remote"),
                work_mode=mode,
                url=f"https://jobs.smartrecruiters.com/{token}/{j['id']}",
                posted_at=iso_date(j.get("releasedDate")),
                raw_ref=api_url(token),
            )
        )
    return out
