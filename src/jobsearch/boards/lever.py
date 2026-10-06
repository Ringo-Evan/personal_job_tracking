"""Lever postings API: GET api.lever.co/v0/postings/{t}?mode=json"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobsearch.boards.common import JobPosting, apply_text_pay, iso_date, work_mode_from

_WORKPLACE = {"remote": "Remote", "hybrid": "Hybrid", "on-site": "On-site", "onsite": "On-site"}
_INTERVAL = {"per-year-salary": "year", "per-hour-wage": "hour", "per-month-salary": "month"}


def api_url(token: str) -> str:
    return f"https://api.lever.co/v0/postings/{quote(token, safe='')}?mode=json"


def normalize(data: list[dict[str, Any]], company_id: str, token: str) -> list[JobPosting]:
    out = []
    for j in data:
        cats = j.get("categories") or {}
        locations = cats.get("allLocations") or ([cats["location"]] if cats.get("location") else [])
        loc_text = "; ".join(locations)
        workplace = (j.get("workplaceType") or "").lower()
        mode = _WORKPLACE.get(workplace) or work_mode_from(loc_text)
        text = "\n".join(
            filter(None, [j.get("descriptionPlain"), *(lst.get("text", "") for lst in j.get("lists") or []),
                          j.get("additionalPlain")])
        )  # fmt: skip
        p: dict[str, Any] = {
            "board": "lever",
            "board_job_id": j["id"],
            "company_id": company_id,
            "title": (j.get("text") or "").strip(),
            "location_text": loc_text,
            "is_remote": workplace == "remote" if workplace in _WORKPLACE else None,
            "work_mode": mode,
            "url": j.get("hostedUrl") or f"https://jobs.lever.co/{token}/{j['id']}",
            "apply_url": j.get("applyUrl"),
            "posted_at": iso_date(j.get("createdAt")),
            "raw_ref": api_url(token),
            "description_text": text,
        }
        if sr := j.get("salaryRange"):
            p.update(
                pay_low=sr.get("min"),
                pay_high=sr.get("max"),
                currency=sr.get("currency"),
                pay_period=_INTERVAL.get(sr.get("interval", ""), sr.get("interval")),
            )
        apply_text_pay(p, text)
        out.append(JobPosting(**p))
    return out
