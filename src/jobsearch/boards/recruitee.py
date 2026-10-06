"""Recruitee careers-site API: GET {t}.recruitee.com/api/offers/ (every published offer, with descriptions)"""

from __future__ import annotations

from typing import Any

from jobsearch.boards.common import JobPosting, apply_text_pay, html_to_text, iso_date, number, pay_period


def api_url(token: str) -> str:
    return f"https://{token}.recruitee.com/api/offers/"


def _places(o: dict[str, Any]) -> list[str]:
    locs = [x for x in o.get("locations") or [] if isinstance(x, dict)] or [
        {"city": o.get("city"), "state": o.get("state_name"), "country": o.get("country")}
    ]
    places = [", ".join(str(x[k]) for k in ("city", "state", "country") if x.get(k)) for x in locs]
    return list(dict.fromkeys(p for p in places if p))


def normalize(data: dict[str, Any], company_id: str, token: str) -> list[JobPosting]:
    out = []
    for o in data.get("offers", []):
        if o.get("status") not in (None, "published"):
            continue
        places = _places(o)
        if o.get("remote"):
            mode, loc_text = "Remote", "; ".join(f"Remote ({p})" for p in places) or "Remote"
        else:
            mode = "Hybrid" if o.get("hybrid") else "On-site" if o.get("on_site") else "Unknown"
            loc_text = "; ".join(places)
        text = "\n".join(
            filter(None, [html_to_text(o.get("description")), html_to_text(o.get("requirements"))])
        )
        p: dict[str, Any] = {
            "board": "recruitee",
            "board_job_id": str(o["id"]),
            "company_id": company_id,
            "title": (o.get("title") or "").strip(),
            "location_text": loc_text,
            "is_remote": o["remote"] if isinstance(o.get("remote"), bool) else None,
            "work_mode": mode,
            "url": o.get("careers_url") or f"https://{token}.recruitee.com/o/{o.get('slug') or o['id']}",
            "apply_url": o.get("careers_apply_url"),
            "posted_at": iso_date(o.get("published_at") or o.get("created_at")),
            "raw_ref": api_url(token),
            "description_text": text,
        }
        salary = o.get("salary") or {}
        low, high = number(salary.get("min")), number(salary.get("max"))
        if low is not None:
            p.update(
                pay_low=low,
                pay_high=high if high is not None else low,
                currency=salary.get("currency") or None,
                pay_period=pay_period(salary.get("period"), high if high is not None else low),
            )
        apply_text_pay(p, text)
        out.append(JobPosting(**p))
    return out
