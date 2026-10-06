"""Ashby posting API: GET api.ashbyhq.com/posting-api/job-board/{t}?includeCompensation=true"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from jobsearch.boards.common import JobPosting, apply_text_pay, iso_date, work_mode_from

_WORKPLACE = {"remote": "Remote", "hybrid": "Hybrid", "onsite": "On-site"}
_INTERVAL = {"1 YEAR": "year", "1 HOUR": "hour", "1 MONTH": "month"}


def api_url(token: str) -> str:
    return f"https://api.ashbyhq.com/posting-api/job-board/{quote(token, safe='')}?includeCompensation=true"


def normalize(data: dict[str, Any], company_id: str, token: str) -> list[JobPosting]:
    out = []
    for j in data.get("jobs", []):
        if j.get("isListed") is False:
            continue
        locations = [j.get("location") or ""] + [
            s.get("location", "") for s in j.get("secondaryLocations") or [] if isinstance(s, dict)
        ]
        loc_text = "; ".join(x for x in locations if x)
        workplace = (j.get("workplaceType") or "").lower()
        is_remote = j.get("isRemote")
        text = j.get("descriptionPlain") or ""
        p: dict[str, Any] = {
            "board": "ashby",
            "board_job_id": j["id"],
            "company_id": company_id,
            "title": (j.get("title") or "").strip(),
            "location_text": loc_text,
            "is_remote": is_remote,
            "work_mode": _WORKPLACE.get(workplace) or work_mode_from(loc_text, is_remote),
            "url": j.get("jobUrl") or f"https://jobs.ashbyhq.com/{quote(token)}/{j['id']}",
            "apply_url": j.get("applyUrl"),
            "posted_at": iso_date(j.get("publishedAt")),
            "raw_ref": api_url(token),
            "description_text": text,
        }
        for comp in (j.get("compensation") or {}).get("summaryComponents") or []:
            if comp.get("compensationType") == "Salary" and comp.get("minValue") is not None:
                p.update(
                    pay_low=comp.get("minValue"),
                    pay_high=comp.get("maxValue"),
                    currency=comp.get("currencyCode"),
                    pay_period=_INTERVAL.get(comp.get("interval", ""), comp.get("interval")),
                )
                break
        apply_text_pay(p, text)
        out.append(JobPosting(**p))
    return out
