"""Which companies to verify next (plan.md Phase 5 queue order)."""

from __future__ import annotations

from datetime import date
from typing import Any

from jobsearch.models import is_skipped
from jobsearch.store import Store, id_sort_key, read_json

REMOTE_ORDER = {"High": 0, "Medium": 1, "Unknown": 2, "Low": 3}
MAX_REJECTS = 2


def _days_since(value: str | None, today: date) -> int | None:
    try:
        return (today - date.fromisoformat(str(value)[:10])).days
    except (TypeError, ValueError):
        return None


def skip_reason(record: dict[str, Any], today: date, refresh_days: int) -> str | None:
    t = record["tracker"]
    if is_skipped(record):
        return "parked or ranked 4+"
    if record.get("missing_from_tracker"):
        return "missing from tracker"
    if record.get("verify_rejects", 0) >= MAX_REJECTS:
        return f"{MAX_REJECTS} consecutive rejects"
    age = _days_since((record.get("research") or {}).get("at"), today)
    if age is not None and age < refresh_days:
        return f"researched {age} days ago"
    if t.get("research_status") == "Researched":
        owner_age = _days_since(t.get("research_as_of"), today)
        if owner_age is not None and owner_age < refresh_days:
            return f"owner marked Researched {owner_age} days ago"
    return None


def owner_rank(record: dict[str, Any]) -> float | None:
    rank = record["tracker"].get("overall_rank")
    return rank if isinstance(rank, int | float) and 1 <= rank <= 3 else None


HOT_SKILL = 3  # an open job scored at least this lifts its company in the queue


def hot_companies(store: Store) -> set[str]:
    """Companies with an open job the scorer rated skill_match >= 3 and not ruled out for Maine."""
    d = store.data_dir / "jobs"
    hot = set()
    for p in d.glob("*.json") if d.is_dir() else []:
        j = read_json(p) or {}
        s = j.get("score") or {}
        if (
            not j.get("closed_at")
            and (s.get("skill_match") or 0) >= HOT_SKILL
            and s.get("maine_eligible") != "no"
        ):
            hot.add(j.get("company_id"))
    return hot


def sort_key(record: dict[str, Any], hot: frozenset[str] | set[str] = frozenset()) -> tuple:
    """Owner rank 1-3 first (best first), then companies with a matching open job, then triage fit, stability,
    remote; untriaged companies last."""
    rank = owner_rank(record)
    tri = record.get("triage") or {}
    return (
        0 if rank is not None else 1,
        rank or 0,
        0 if record["company_id"] in hot else 1,
        -(tri.get("fit") or 0),
        -(tri.get("stability") or 0),
        REMOTE_ORDER.get(tri.get("remote") or "Unknown", 2),
        id_sort_key(record["company_id"]),
    )


def verify_queue(
    store: Store, today: date, refresh_days: int, only: list[str] | None = None, force: bool = False
) -> list[dict[str, Any]]:
    """only: just these ids, in queue order. force: ignore the skip rules except parked/ranked 4+."""
    records = [r for cid in (only or store.company_ids()) if (r := store.load_company(cid))]
    if force:
        todo = [r for r in records if not is_skipped(r)]
    else:
        todo = [r for r in records if skip_reason(r, today, refresh_days) is None]
    hot = hot_companies(store)
    return sorted(todo, key=lambda r: sort_key(r, hot))
