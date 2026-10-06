"""Pinpoint: GET {t}.pinpointhq.com/postings.json (every posting, with descriptions and visible pay)"""

from __future__ import annotations

from typing import Any

from jobsearch.boards.common import (
    JobPosting,
    apply_text_pay,
    html_to_text,
    number,
    pay_period,
    work_mode_from,
)

_WORKPLACE = {
    "remote": "Remote",
    "hybrid": "Hybrid",
    "onsite": "On-site",
    "on_site": "On-site",
    "office": "On-site",
}
_SECTIONS = ("description", "key_responsibilities", "skills_knowledge_expertise", "benefits")


def api_url(token: str) -> str:
    return f"https://{token}.pinpointhq.com/postings.json"


def normalize(data: dict[str, Any], company_id: str, token: str) -> list[JobPosting]:
    out = []
    for j in data.get("data", []):
        loc = j.get("location") or {}
        place = loc.get("name") or ", ".join(x.strip() for x in (loc.get("city"), loc.get("province")) if x)
        workplace = (j.get("workplace_type") or "").lower()
        mode = _WORKPLACE.get(workplace) or work_mode_from(place)
        loc_text = f"Remote ({place})" if mode == "Remote" and "remote" not in place.lower() else place
        text = "\n".join(
            filter(None, [f"{j.get(s + '_header') or ''}\n{html_to_text(j.get(s))}".strip() for s in _SECTIONS
                          if j.get(s)])
        )  # fmt: skip
        p: dict[str, Any] = {
            "board": "pinpoint",
            "board_job_id": str(j["id"]),
            "company_id": company_id,
            "title": (j.get("title") or "").strip(),
            "location_text": loc_text or ("Remote" if mode == "Remote" else ""),
            "is_remote": workplace == "remote" if workplace in _WORKPLACE else None,
            "work_mode": mode,
            "url": j.get("url") or f"https://{token}.pinpointhq.com{j.get('path') or ''}",
            "raw_ref": api_url(token),
            "description_text": text,
        }
        low, high = number(j.get("compensation_minimum")), number(j.get("compensation_maximum"))
        if j.get("compensation_visible") and low is not None:
            p.update(
                pay_low=low,
                pay_high=high if high is not None else low,
                currency=j.get("compensation_currency") or "USD",
                pay_period=pay_period(j.get("compensation_frequency"), high),
            )
        apply_text_pay(p, text)
        out.append(JobPosting(**p))
    return out
