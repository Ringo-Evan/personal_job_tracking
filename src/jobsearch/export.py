"""Export: the pipeline's research, jobs and people go into the tracker (plan.md Phase 6, plan-tracker.md).

Only pipeline-owned fields (section 5.3) are written, as the writer "pipeline", through the tracker's one write path,
which refuses anything else. Within a row, only cells whose value changed since the last export are sent, so an
owner's hand edit of a pipeline field survives until the pipeline's own value changes. Insert-only fields are written
when a row is created only. What was exported is remembered per record (`exported`), so a rerun writes nothing new.
The tracker's import CSVs can still be written: as a preview (`--dry-run`) or as a backup (`--csv`).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import quote

from jobsearch.config import ROOT, Config
from jobsearch.evidence import skip_url
from jobsearch.filters import PostingFilter
from jobsearch.grounding import person_problem
from jobsearch.models import is_skipped
from jobsearch.rank import maine_verdict
from jobsearch.store import Store, read_json, write_json
from jobsearch.tracker import (  # section 5.3 field ownership
    COMPANY_OWNED,
    JOB_INSERT_ONLY,
    JOB_OWNED,
    PEOPLE_INSERT_ONLY,
    PEOPLE_OWNED,
    PIPELINE,
    Tracker,
    TrackerError,
)
from jobsearch.tracker_csv import COMPANIES, JOBS, PEOPLE, Tab
from jobsearch.watch import JobStore

OUT = ROOT / "out"
SHORTLIST_SKILL = 3  # skill match from the job scorer, 1-5
DISMISSED = "dismissed.json"  # in data/: {job key: reason} for jobs ruled out by hand (`jobsearch dismiss`)

OWNER_STATUSES_KEPT = ("Researching", "Researched", "Parked")  # never overwritten if the owner set them last
SIGNAL_STRENGTH = ("revenue", "growth", "funding", "public-sector", "acquisition", "other", "layoffs")
REMOTE_LIKELIHOOD = {"yes": "High", "varies": "Medium", "no": "Low", "unknown": "Unknown"}
MAINE_LABEL = {"yes": "Yes", "no": "No", "unknown": "Unknown", "varies": "Varies"}


def columns(tab: Tab, owned: tuple[str, ...], insert_only: tuple[str, ...] = ()) -> list[str]:
    """ID column first, then the tab's name column, then owned and insert-only keys in tracker label order."""
    name = "name" if tab is PEOPLE else "company"
    keep = set(owned) | set(insert_only)
    rest = [k for k in tab.labels if k in keep and k not in (tab.id_key, name)]
    return [tab.id_key, name, *rest]


def people_search_url(role: str, company: str) -> str:
    """A LinkedIn *search link* for the owner to click (hard rule 2 allows generating these as text)."""
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote(f"{role} {company}".strip())


# --- companies ------------------------------------------------------------------------------------------


def _strongest_signal_url(signals: list[dict[str, Any]]) -> str | None:
    ranked = sorted(
        signals, key=lambda s: SIGNAL_STRENGTH.index(s["kind"]) if s["kind"] in SIGNAL_STRENGTH else 99
    )
    return ranked[0]["url"] if ranked else None


def company_values(record: dict[str, Any]) -> dict[str, Any]:
    """Pipeline-owned company fields from research (plan §6 mapping) or, before verification, from triage."""
    t = record["tracker"]
    name = t.get("company") or record["company_id"]
    r, tri = record.get("research"), record.get("triage")
    v: dict[str, Any] = {}
    if r:
        rp = r["remote_policy"]
        notes = (r.get("notes") or "").strip()
        srcs = r.get("sources", [])[:3]
        v.update(
            # Fill a blank careers URL only: the tracker's were set by hand and are usually better than the
            # page verify happened to cite (an employee-story page, say).
            career_url=None if t.get("career_url") else r.get("career_url"),
            research_notes=(f"{notes} Sources: {' '.join(srcs)}" if srcs else notes) or None,
            research_status="Researched",
            research_as_of=r["at"],
            maine_remote=f"{rp['maine_eligible']}: {rp['summary']}",
            remote_likelihood=REMOTE_LIKELIHOOD[rp["maine_eligible"]],
            stability_score=r["stability"]["score"],
            benefit_source=_strongest_signal_url(r["stability"]["signals"]),
            fit_score=r["fit"]["score"],
            ai_summary=r["fit"]["rationale"],
            role_families="; ".join(r["fit"]["role_families"]) or None,
            ai_red_flags=r.get("red_flags") or None,
            ai_scored_at=r["at"],
            size_estimate=r["size_estimate"],
            hiring_difficulty=r["hiring_difficulty"],
            estimate_confidence=r["confidence"].capitalize(),
            approach_role=r["approach_role"],
            outreach_notes=r["outreach_angle"],
        )
        ats = record.get("ats") or {}
        career = r.get("career_url") or t.get("career_url")
        v["next_research_action"] = (
            f"Watching {ats['type']} board"
            if ats.get("token") and ats.get("confirmed") is not False and ats.get("type") != "other"
            else f"No supported job board - check {career} manually"
            if career
            else "No supported job board found"
        )
    elif tri:
        v.update(
            research_status="Initial research",
            fit_score=tri["fit"],
            stability_score=tri["stability"],
            remote_likelihood=tri["remote"],
            size_estimate=tri["size"],
            role_families="; ".join(tri.get("roles") or []) or None,
            approach_role=tri.get("approach") or None,
            ai_summary=tri.get("why") or None,
            ai_red_flags=tri.get("flags") or None,
            ai_scored_at=tri.get("at"),
        )
    else:
        return {}
    if v.get("approach_role"):
        v["people_search_url"] = people_search_url(v["approach_role"], name)
    if not _may_write_status(record, v.get("research_status")):
        v.pop("research_status", None)
    return {k: val for k, val in v.items() if val not in (None, "")}


def _may_write_status(record: dict[str, Any], new: str | None) -> bool:
    """Section 5.3: don't export research_status when the owner moved it past 'Initial research' after the
    pipeline's last write. Also never step an owner's status backwards."""
    if not new:
        return False
    current = record["tracker"].get("research_status")
    if current not in OWNER_STATUSES_KEPT:
        return True
    change = (record.get("owner_changes") or {}).get("research_status") or {}
    owner_set_after_us = change.get("value") == current and (change.get("at") or "") > (
        record.get("last_exported_at") or ""
    )
    if owner_set_after_us:
        return False
    return not (new == "Initial research" and current in OWNER_STATUSES_KEPT)


# --- jobs and people ------------------------------------------------------------------------------------


def _key(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).lower().encode("utf-8")).hexdigest()[:12]


def sync_leads(store: Store, record: dict[str, Any], today: str, title_ok=lambda title: True) -> int:
    """Research qa_openings not already in data/jobs become 'lead' job records. Returns how many were added."""
    r = record.get("research") or {}
    jobs = JobStore(store)
    for p in jobs.dir.glob("lead-*.json") if jobs.dir.is_dir() else []:
        lead = read_json(p) or {}
        if (
            lead.get("company_id") == record["company_id"]
            and not lead.get("job_id")
            and not title_ok(lead["title"])
        ):
            p.unlink()  # saved under older rules and never exported
    known_urls = {j.get("url") for j in _all(jobs.dir)}
    added = 0
    for o in r.get("qa_openings", []):
        if o["url"] in known_urls or not title_ok(o["title"]):
            continue
        key = f"lead-{_key(record['company_id'], o['title'], o['url'])}"
        if jobs.load(key):
            continue
        jobs.save({
            "key": key, "board": "lead", "company_id": record["company_id"],
            "company": record["tracker"].get("company"), "title": o["title"], "location_text": o["location"],
            "work_mode": o["work_mode"], "url": o["url"], "apply_url": None, "source_url": o["url"],
            "maine_eligible": "unknown", "eligibility_reason": "found by verify; check the posting",
            "track": None, "source": "verify", "first_seen": today, "last_seen": today, "closed_at": None,
            "job_id": None,
        })  # fmt: skip
        known_urls.add(o["url"])
        added += 1
    return added


def sync_people(store: Store, record: dict[str, Any], today: str) -> int:
    r = record.get("research") or {}
    d = store.data_dir / "people"
    added = 0
    for p in r.get("people", []):
        key = _key(record["company_id"], re.sub(r"\s+", " ", p["name"]).strip())
        path = d / f"{key}.json"
        old = read_json(path) or {}
        if person_problem(p["name"], p["title"]) or skip_url(p["source_url"]):
            # Research saved under older rules: never-exported records that now fail are removed.
            if old and not old.get("person_id"):
                path.unlink()
            continue
        rec = old | {
            "key": key, "company_id": record["company_id"], "company": record["tracker"].get("company"),
            "name": p["name"], "title": p["title"], "source_url": p["source_url"],
            "why_approach": f"{p['title']} at {record['tracker'].get('company')}. {r.get('outreach_angle', '')}".strip(),
            "first_seen": old.get("first_seen", today), "person_id": old.get("person_id"),
            "exported": old.get("exported", {}),
        }  # fmt: skip
        added += not old
        write_json(path, rec)
    return added


def _all(d: Path) -> list[dict[str, Any]]:
    return [j for p in sorted(d.glob("*.json")) if (j := read_json(p))] if d.is_dir() else []


def job_values(job: dict[str, Any], company: dict[str, Any] | None) -> dict[str, Any]:
    lead = job.get("source") == "verify"
    closed = job.get("closed_at")
    score = job.get("score") or {}
    notes = " ".join(x for x in (score.get("notes"), job.get("eligibility_reason")) if x)
    if closed:
        notes = f"Posting removed {closed}. {notes}".strip()
    board_url = ((company or {}).get("ats") or {}).get("board_url")
    v = {
        "company": job.get("company"),
        "job_title": job.get("title"),
        "posting_url": job.get("url"),
        "application_url": job.get("apply_url"),
        "source_url": job.get("source_url") or board_url or job.get("url"),
        "listing_evidence": "Historical lead - recheck" if closed else "Indexed lead - verify" if lead else "Verified live",
        "last_checked": job.get("last_seen"),
        "location": job.get("location_text"),
        "work_mode": job.get("work_mode") or "Unknown",
        "maine_eligible": MAINE_LABEL[maine_verdict(job)],
        "pay_low": job.get("pay_low"),
        "pay_high": job.get("pay_high"),
        "currency": job.get("currency"),
        "pay_period": job.get("pay_period"),
        "skill_match": score.get("skill_match"),
        "fit_notes": notes or None,
    }  # fmt: skip
    return {k: val for k, val in v.items() if val not in (None, "")}


def job_insert_values(job: dict[str, Any]) -> dict[str, Any]:
    from jobsearch.filters import track_for

    return {
        "track": (job.get("score") or {}).get("track")
        or job.get("track")
        or track_for(job.get("title") or ""),
        "current_stage": "Lead",
        "highest_stage": "None",
        "next_action": "Verify posting is live, then apply"
        if job.get("source") == "verify"
        else "Review and apply",
        "date_discovered": job.get("first_seen"),
    }


def person_values(p: dict[str, Any]) -> dict[str, Any]:
    v = {k: p.get(k) for k in ("name", "company", "title", "source_url", "why_approach")}
    return {k: val for k, val in v.items() if val not in (None, "")}


PERSON_INSERT = {
    "status": "To research",
    "relationship": "Cold",
    "next_step": "Look up on LinkedIn (People search link)",
}


# --- IDs ------------------------------------------------------------------------------------------------


class Ids:
    """data/ids.json counters. IDs are never reused: the counter only goes up."""

    def __init__(self, store: Store, cfg: Config):
        self.path = store.data_dir / "ids.json"
        self.state = read_json(self.path) or {"job": 0, "person": 0}
        self.prefix = {"job": cfg.ids.job_prefix, "person": cfg.ids.person_prefix}

    def next(self, kind: str) -> str:
        self.state[kind] += 1
        return f"{self.prefix[kind]}{self.state[kind]:04d}"

    def save(self) -> None:
        write_json(self.path, self.state)


# --- the export -----------------------------------------------------------------------------------------


@dataclass
class ExportReport:
    out_dir: Path | None = None
    files: dict[str, Path] = field(default_factory=dict)
    companies: list[str] = field(default_factory=list)
    jobs_new: list[str] = field(default_factory=list)  # job keys exported for the first time
    jobs_updated: list[str] = field(default_factory=list)
    people_new: list[str] = field(default_factory=list)
    people_updated: list[str] = field(default_factory=list)
    leads_added: int = 0
    skipped_status: list[str] = field(default_factory=list)  # owner-set research_status left alone
    refused: list[str] = field(default_factory=list)  # rows the tracker refused: tried again next time
    gone: list[str] = field(default_factory=list)  # rows deleted in the tracker: left deleted

    def counts(self) -> dict[str, int]:
        return {"companies": len(self.companies), "jobs_new": len(self.jobs_new),
                "jobs_updated": len(self.jobs_updated), "people_new": len(self.people_new),
                "people_updated": len(self.people_updated), "leads_added": self.leads_added,
                "refused": len(self.refused)}  # fmt: skip


def _diff(values: dict[str, Any], exported: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in values.items() if exported.get(k) != v}


def to_csv(cols: list[str], tab: Tab, rows: list[dict[str, Any]]) -> str:
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow([tab.labels[c] for c in cols])
    for row in rows:
        w.writerow(["" if row.get(c) is None else _cell(row[c]) for c in cols])
    return buf.getvalue()


def _cell(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _out_path(out_dir: Path, name: str, stamp: str) -> Path:
    """Never overwrite an earlier export of the same day: the owner may not have imported it yet."""
    p = out_dir / f"{name}.csv"
    return p if not p.exists() else out_dir / f"{name}-{stamp}.csv"


def dismissed(store: Store) -> dict[str, str]:
    path = store.data_dir / DISMISSED
    return read_json(path) if path.exists() else {}


def _title_key(job: dict[str, Any]) -> tuple[Any, str]:
    return job.get("company_id"), re.sub(r"[^a-z0-9]+", "", (job.get("title") or "").lower())


def lead_duplicates(jobs: list[dict[str, Any]]) -> set[str]:
    """Keys of research leads for a posting the watcher already tracks: the same job twice (Motional, 2026-10-06)."""
    watched = {_title_key(j) for j in jobs if not j.get("closed_at") and j.get("source") != "verify"}
    return {j["key"] for j in jobs if j.get("source") == "verify" and _title_key(j) in watched}


def publishable(job: dict[str, Any], gone: dict[str, str]) -> bool:
    """Which pipeline jobs reach the tracker's Jobs tab (plan-tracker.md): open ones on the shortlist (skill match
    3+, Maine not ruled out) that weren't dismissed. The rest stay in the report, ranked low."""
    return (
        not job.get("closed_at")
        and ((job.get("score") or {}).get("skill_match") or 0) >= SHORTLIST_SKILL
        and maine_verdict(job) != "no"
        and job["key"] not in gone
    )


def run_export(
    store: Store,
    cfg: Config,
    *,
    today: str,
    now: str,
    out_root: Path = OUT,
    dry_run: bool = False,
    tracker: Tracker | None = None,
    csv_backup: bool = False,
) -> ExportReport:
    """Send everything that changed since the last export into `tracker`.

    A row the tracker refuses goes in `refused` and is tried again next time; a job or person deleted in the tracker
    stays deleted. dry_run: change nothing anywhere and write the CSV preview to out/<date>/preview/ instead.
    csv_backup: also write the import CSVs to out/<date>/.
    """
    if tracker is None and not dry_run:
        raise ValueError("a real export needs the tracker to write into")
    rep = ExportReport()
    ids = Ids(store, cfg)
    flt = PostingFilter(cfg)  # leads get the same title rules as board postings
    gone = dismissed(store)
    company_rows: list[dict[str, Any]] = []
    companies: dict[str, dict[str, Any]] = {}

    def send(name: str, row_id: str, values: dict[str, Any], create: bool = False) -> bool:
        if dry_run:
            return True
        try:
            tracker.write(name, row_id, values, by=PIPELINE, create=create)
        except TrackerError as e:
            rep.refused.append(f"{name}/{row_id}: {e}"[:300])
            return False
        return True

    def deleted(name: str, row_id: str) -> bool:
        if dry_run or tracker.get(name, row_id) is not None:
            return False
        rep.gone.append(row_id)
        return True

    for rec in store.iter_companies():
        companies[rec["company_id"]] = rec
        if is_skipped(rec) or rec.get("missing_from_tracker"):
            continue
        # Lead and people records are pipeline data, not export marks: synced on dry runs too.
        rep.leads_added += sync_leads(store, rec, today, flt.title_ok)
        sync_people(store, rec, today)
        values = company_values(rec)
        if (
            rec["tracker"].get("research_status") in OWNER_STATUSES_KEPT
            and "research_status" not in values
            and (rec.get("research") or rec.get("triage"))
        ):
            rep.skipped_status.append(rec["company_id"])
        changed = _diff(values, rec.get("exported") or {})
        if not changed or not send("companies", rec["company_id"], changed):  # the pipeline never adds companies
            continue
        company_rows.append(
            {"company_id": rec["company_id"], "company": rec["tracker"].get("company"), **changed}
        )
        rep.companies.append(rec["company_id"])
        if not dry_run:
            rec["exported"] = (rec.get("exported") or {}) | changed
            rec["last_exported_at"] = now
            rec["history"].append({"at": now, "stage": "export", "changed": sorted(changed)})
            store.save_company(rec)

    jobs = JobStore(store)
    job_rows: list[dict[str, Any]] = []
    all_jobs = _all(jobs.dir)
    dupes = lead_duplicates(all_jobs)
    for job in all_jobs:
        company = companies.get(job.get("company_id"))
        if not company or is_skipped(company) or company.get("missing_from_tracker"):
            continue
        job["company"] = company["tracker"].get("company")  # tracker's exact name string (5.4)
        values = job_values(job, company) | ({"company_id": job["company_id"]} if job.get("company_id") else {})
        first = not job.get("job_id")
        if first and (not publishable(job, gone) or job["key"] in dupes):
            continue
        changed = _diff(values, job.get("exported") or {})
        if not changed and not first:
            continue
        if first:
            job_id = ids.next("job")
            row = values | job_insert_values(job)
            if not send("jobs", job_id, row, create=True):
                continue
            job["job_id"] = job_id
            job_rows.append({"job_id": job_id, **row})
            rep.jobs_new.append(job["key"])
            job["first_exported"] = today
        else:
            if deleted("jobs", job["job_id"]) or not send("jobs", job["job_id"], changed):
                continue
            job_rows.append({"job_id": job["job_id"], "company": values.get("company"), **changed})
            rep.jobs_updated.append(job["key"])
        job["exported"] = (job.get("exported") or {}) | values
        if not dry_run:
            jobs.save(job)

    people_dir = store.data_dir / "people"
    people_rows: list[dict[str, Any]] = []
    for p in _all(people_dir):
        company = companies.get(p.get("company_id"))
        if not company or is_skipped(company) or company.get("missing_from_tracker"):
            continue
        p["company"] = company["tracker"].get("company")
        values = person_values(p) | ({"company_id": p["company_id"]} if p.get("company_id") else {})
        first = not p.get("person_id")
        changed = _diff(values, p.get("exported") or {})
        if not changed and not first:
            continue
        if first:
            person_id = ids.next("person")
            if not send("people", person_id, values | PERSON_INSERT, create=True):
                continue
            p["person_id"] = person_id
            people_rows.append({"person_id": person_id, **values, **PERSON_INSERT})
            rep.people_new.append(p["key"])
            p["first_exported"] = today
        else:
            if deleted("people", p["person_id"]) or not send("people", p["person_id"], changed):
                continue
            people_rows.append({"person_id": p["person_id"], "name": values.get("name"), **changed})
            rep.people_updated.append(p["key"])
        p["exported"] = (p.get("exported") or {}) | values
        if not dry_run:
            write_json(people_dir / f"{p['key']}.json", p)

    if not dry_run:
        ids.save()
    if not dry_run and not csv_backup:
        return rep
    # A dry run writes a preview (same IDs a real export would allocate) and marks nothing.
    out_dir = out_root / today / "preview" if dry_run else out_root / today
    out_dir.mkdir(parents=True, exist_ok=True)
    rep.out_dir = out_dir
    stamp = now[11:19].replace(":", "")
    for name, tab, cols, rows in (
        ("companies_import", COMPANIES, columns(COMPANIES, COMPANY_OWNED), company_rows),
        ("jobs_import", JOBS, columns(JOBS, JOB_OWNED, JOB_INSERT_ONLY), job_rows),
        ("people_import", PEOPLE, columns(PEOPLE, PEOPLE_OWNED, PEOPLE_INSERT_ONLY), people_rows),
    ):
        if not rows:
            continue
        path = out_dir / f"{name}.csv" if dry_run else _out_path(out_dir, name, stamp)
        path.write_text(to_csv(cols, tab, rows), encoding="utf-8", newline="")
        rep.files[name] = path
    return rep
