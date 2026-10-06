"""Breezy HR: GET {t}.breezy.hr/json lists every job without descriptions; each posting page carries a schema.org
JobPosting (JSON-LD) with the description and, for remote jobs, where applicants may live."""

from __future__ import annotations

import json
import re
from typing import Any

from jobsearch.boards.common import JobPosting, apply_text_pay, html_to_text, iso_date, work_mode_from

_LD = re.compile(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", re.S | re.I)


def api_url(token: str) -> str:
    return f"https://{token}.breezy.hr/json"


def job_posting_ld(html: str) -> dict[str, Any] | None:
    """The page's schema.org JobPosting, if any."""
    for m in _LD.finditer(html or ""):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None


def _place(loc: dict[str, Any]) -> str:
    # remote_details "remote" = "Fully remote, no location restrictions"; "remote-location" = remote within the
    # listed places (values seen 2026-10-06).
    details = loc.get("remote_details") or {}
    if details.get("value") == "remote":
        return "Remote (anywhere)"
    name = loc.get("name") or ""
    country = (loc.get("country") or {}).get("name") or ""
    if country and country.lower() not in name.lower():  # "Atlanta, GA" alone reads as non-US to the filter
        name = f"{name}, {country}" if name else country
    return f"Remote ({name})" if loc.get("is_remote") else name


def _applicant_places(ld: dict[str, Any]) -> list[str]:
    req = ld.get("applicantLocationRequirements")
    items = req if isinstance(req, list) else [req] if isinstance(req, dict) else []
    return [str(x["name"]) for x in items if isinstance(x, dict) and x.get("name")]


def normalize_item(
    item: dict[str, Any], ld: dict[str, Any] | None, company_id: str, token: str
) -> JobPosting:
    primary = item.get("location") or {}
    locs = [x for x in item.get("locations") or [] if isinstance(x, dict)] or [primary]
    loc_text = "; ".join(dict.fromkeys(p for p in map(_place, locs) if p))
    remote = primary.get("is_remote") if isinstance(primary.get("is_remote"), bool) else None
    label = (primary.get("remote_details") or {}).get("label") or ""
    text = html_to_text((ld or {}).get("description"))
    if ld and (places := _applicant_places(ld)):
        text += f"\nApplicant location requirements: {', '.join(places)}"
    p: dict[str, Any] = {
        "board": "breezy",
        "board_job_id": str(item["id"]),
        "company_id": company_id,
        "title": (item.get("name") or "").strip(),
        "location_text": loc_text,
        "is_remote": remote,
        "work_mode": work_mode_from(f"{label} {loc_text}", remote),
        "url": item.get("url") or f"https://{token}.breezy.hr/p/{item.get('friendly_id') or item['id']}",
        "posted_at": iso_date(item.get("published_date") or (ld or {}).get("datePosted")),
        "raw_ref": item.get("url") if ld else api_url(token),
        "description_text": text,
    }
    apply_text_pay(p, f"{item.get('salary') or ''}\n{text}")
    return JobPosting(**p)
