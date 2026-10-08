"""Daily report (plan.md Phase 6; one document since 2026-10-06): everything worth acting on, best first.

Writes out/<date>/digest.md and digest.html (the same report, for a browser). Built only from data/, logs/runs.jsonl,
data/rejects/ and state/: no network and no model calls. "New" means new since the previous report: each report
saves the open jobs and people it saw (state/report_state.json), so a skipped night loses nothing.
"""

from __future__ import annotations

import html
import json
import os
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

from markdown_it import MarkdownIt

from jobsearch.config import ROOT
from jobsearch.detect import SUPPORTED
from jobsearch.export import OUT, SHORTLIST_SKILL, _all, company_values, dismissed, lead_duplicates
from jobsearch.rank import maine_verdict, priority
from jobsearch.runlog import RUNS_LOG
from jobsearch.store import Store, read_json, write_json
from jobsearch.tracker import CLOSED_STAGES, STAGES, Tracker
from jobsearch.usage import ROUTER_DB

REPORT_STATE = ROOT / "state" / "report_state.json"
# Stages past Lead and Verified: the owner is on it, so the job leaves Apply next and goes under In progress.
ACTIVE_STAGES = ("Applied", "Screen", "Interview", "Offer")


def _day(value: Any) -> date | None:
    """A tracker date: YYYY-MM-DD, or MM/DD/YY(YY) from the owner's old spreadsheet."""
    s = str(value or "").strip()
    if m := re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})", s):
        y, mo, d = (int(x) for x in m.groups())
    elif m := re.match(r"^(\d{1,2})/(\d{1,2})/(\d{2,4})$", s):
        mo, d, y = (int(x) for x in m.groups())
        y += 2000 if y < 100 else 0
    else:
        return None
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def due_state(row: dict[str, Any], today: date) -> str:
    """The tracker page's due flag: Overdue, Today, Soon (within 3 days) or nothing; never for a closed job."""
    if (row.get("current_stage") or "Lead") in CLOSED_STAGES or not (due := _day(row.get("follow_up_due"))):
        return ""
    days = (due - today).days
    return "Overdue" if days < 0 else "Today" if days == 0 else "Soon" if days <= 3 else ""


# --- small helpers ---------------------------------------------------------------------------------------


def _fit(company: dict[str, Any] | None) -> int:
    if not company:
        return 0
    r, t = company.get("research") or {}, company.get("triage") or {}
    return (r.get("fit") or {}).get("score") or t.get("fit") or 0


def _stability(company: dict[str, Any] | None) -> int:
    if not company:
        return 0
    r, t = company.get("research") or {}, company.get("triage") or {}
    return (r.get("stability") or {}).get("score") or t.get("stability") or 0


def _dt(value: Any) -> datetime | None:
    """An aware datetime from an ISO string; naive strings are local time."""
    try:
        d = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


def _md(text: Any) -> str:
    """Text from the web, made safe for a table cell or link text."""
    s = str(text or "").replace("|", "/").replace("\n", " ").replace("[", "(").replace("]", ")")
    return s.strip()


def _url(u: Any) -> str:
    """A markdown link destination that survives spaces and parentheses in the URL."""
    return "<" + str(u or "").replace(" ", "%20").replace("<", "%3C").replace(">", "%3E") + ">"


def _short(text: Any, n: int = 240) -> str:
    s = _md(text)
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


def _pay(j: dict[str, Any]) -> str:
    lo, hi = j.get("pay_low"), j.get("pay_high")
    if not lo:
        return ""
    cur = j.get("currency") or "USD"
    sym = "$" if cur == "USD" else f"{cur} "
    per = {"year": "/yr", "hour": "/hr", "month": "/mo"}.get(j.get("pay_period") or "", "")
    return f"{sym}{lo:,.0f}" + (f"-{hi:,.0f}" if hi and hi != lo else "") + per


def people_search_url(company: dict[str, Any]) -> str:
    """The tracker's own People search link, else a LinkedIn people search to open by hand (never fetched)."""
    t = company.get("tracker") or {}
    if t.get("people_search_url"):
        return t["people_search_url"]
    q = f'"{t.get("company") or ""}" (QA OR quality OR test) (manager OR director OR head)'
    return "https://www.linkedin.com/search/results/people/?keywords=" + quote_plus(q)


def _runs_since(log: Path, since: datetime) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if (at := _dt(e.get("at"))) and at >= since:
            out.append(e)
    return out


def _by_command(runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for e in runs:
        out.setdefault(e.get("command", "?"), []).append(e)
    return out


def _model_calls(router_db: Path | None, since: datetime) -> dict[str, int]:
    """Model calls per endpoint from state/router.db (daily counters), read-only."""
    if not router_db or not router_db.exists():
        return {}
    try:
        db = sqlite3.connect(f"file:{router_db.as_posix()}?mode=ro", uri=True)
        try:
            rows = db.execute(
                "SELECT model, SUM(calls) FROM usage WHERE day >= ? GROUP BY model ORDER BY 2 DESC",
                (since.date().isoformat(),),
            ).fetchall()
        finally:
            db.close()
    except sqlite3.Error:
        return {}
    return {m: int(n) for m, n in rows if n}


def _cover_letters(out_root: Path) -> dict[str, list[Path]]:
    """Drafted cover letters on disk, keyed by the company slug that starts their file name."""
    found: dict[str, list[Path]] = {}
    for p in sorted(out_root.glob("*/recommendations/cover-letters/*.md")):
        found.setdefault(p.stem.split("-")[0], []).append(p)
    return found


def _materials(out_root: Path, out_dir: Path) -> str:
    """Links to the latest hand-reviewed recommendations (picks, resume, outreach, cover letters), if any."""
    readmes = sorted(out_root.glob("*/recommendations/README.md"))
    if not readmes:
        return ""
    folder = readmes[-1].parent
    parts = [
        f"[{label}]({_url(os.path.relpath(folder / name, out_dir).replace(os.sep, '/'))})"
        for label, name in (("picks and decisions", "README.md"), ("resume suggestions", "resume-suggestions.md"),
                            ("outreach drafts", "outreach.md"), ("cover letters", "cover-letters"))
        if (folder / name).exists()
    ]  # fmt: skip
    return f"**Hand-reviewed materials ({folder.parent.name}):** " + " · ".join(parts)


def _sentence(text: Any, n: int) -> str:
    s = _short(text, n)
    return s if not s or s[-1] in ".!?…" else s + "."


def _changed_since(record: dict[str, Any], since: datetime, prefix: str, stages: tuple[str, ...]) -> bool:
    for h in record.get("history") or []:
        at = _dt(h.get("at"))
        if at and at >= since and h.get("stage") in stages and any(str(c).startswith(prefix) for c in h["changed"]):
            return True
    return False


def snapshot(store: Store, now: str) -> dict[str, Any]:
    """What the next report compares against."""
    jobs = sorted(j["key"] for j in _all(store.data_dir / "jobs") if not j.get("closed_at"))
    people = sorted(p["key"] for p in _all(store.data_dir / "people"))
    return {"at": now, "jobs": jobs, "people": people}


# --- the report -------------------------------------------------------------------------------------------


def build_digest(
    store: Store,
    today: str,
    runs_log: Path = RUNS_LOG,
    *,
    prev: dict[str, Any] | None = None,
    now: datetime | None = None,
    out_root: Path = OUT,
    router_db: Path | None = None,
    tracker: Tracker | None = None,
    top: int = 8,
) -> str:
    """tracker: where the owner's job stages live; without it every published job counts as still at Lead."""
    now = now or datetime.now().astimezone()
    since = _dt((prev or {}).get("at")) or datetime.fromisoformat(today).astimezone()  # first report: since midnight
    companies = {r["company_id"]: r for r in store.iter_companies()}
    name = {cid: r["tracker"].get("company") or cid for cid, r in companies.items()}
    out_dir = out_root / today
    letters = _cover_letters(out_root)
    all_jobs = _all(store.data_dir / "jobs")
    gone = dismissed(store)
    dupes = lead_duplicates(all_jobs)
    open_jobs = [j for j in all_jobs if not j.get("closed_at") and j["key"] not in gone and j["key"] not in dupes]
    # The owner's stages, set in the tracker: a job they've applied to, or closed, leaves the lists below.
    rows = tracker.load("jobs") if tracker else []
    stage = {r["job_id"]: r.get("current_stage") or "Lead" for r in rows if r.get("job_id")}
    open_jobs = [j for j in open_jobs if stage.get(j.get("job_id") or "", "Lead") in ("Lead", "Verified")]
    day = date.fromisoformat(today)
    in_progress = sorted(
        (r for r in rows if (r.get("current_stage") or "Lead") in ACTIVE_STAGES),
        key=lambda r: (_day(r.get("follow_up_due")) or date.max, -STAGES.index(r["current_stage"]), r["job_id"]),
    )
    due = [r for r in rows if due_state(r, day) in ("Overdue", "Today")]
    runs = _runs_since(runs_log, since)

    def rank_key(j: dict[str, Any]) -> tuple:
        # Ties go to the steadier employer (the profile says downturn stability matters), then the better pay.
        c = companies.get(j.get("company_id"))
        return (-priority(j, c), -_stability(c), -(j.get("pay_high") or 0), j.get("title") or "")

    prev_jobs, prev_people = set((prev or {}).get("jobs") or []), set((prev or {}).get("people") or [])

    def is_new_job(j: dict[str, Any]) -> bool:
        return j["key"] not in prev_jobs if prev else (j.get("first_seen") or "") >= today

    new_jobs = sorted([j for j in open_jobs if is_new_job(j)], key=rank_key)
    shortlist = sorted(
        [
            j
            for j in open_jobs
            if ((j.get("score") or {}).get("skill_match") or 0) >= SHORTLIST_SKILL and maine_verdict(j) != "no"
        ],
        key=rank_key,
    )
    picks, rest = shortlist[:top], shortlist[top:]

    lines = [
        f"# Job search report, {today}",
        "",
        f"_Generated {now:%Y-%m-%d %H:%M}. Covers changes since {since:%Y-%m-%d %H:%M}. Built from public "
        "sources only; nothing is ever applied for, sent or posted._",
    ]
    if materials := _materials(out_root, out_dir):
        lines += ["", materials]

    # alerts ---------------------------------------------------------------------------------------------------
    alerts = []
    for e in runs:
        for a in e.get("alerts") or []:
            alerts.append(a)
        for k in ("stopped", "stopped_for_limit"):
            if e.get(k):
                alerts.append(f"`{e.get('command')}` stopped early: {e[k]}")
        if e.get("error"):
            alerts.append(f"`{e.get('command')}` failed: {e['error']}")
    verify_counts = [e.get("counts") or {} for e in runs if e.get("command") == "verify"]
    checked = sum(c.get("verified", 0) + c.get("rejected", 0) for c in verify_counts)
    rejected = sum(c.get("rejected", 0) for c in verify_counts)
    if checked >= 5 and rejected / checked > 0.15:
        alerts.append(f"Research rejected {rejected} of {checked} companies (over 15%): check the rejects below.")
    unscored = sum(1 for j in new_jobs if not j.get("score"))
    if unscored:
        alerts.append(f"{unscored} new jobs are not scored yet: run `python -m jobsearch rank`.")
    for r in due:
        alerts.append(f"Follow-up {due_state(r, day).lower()}: {r['job_id']} {r.get('job_title') or ''}, "
                      f"{r.get('company') or ''} (due {r.get('follow_up_due')}). {r.get('next_action') or ''}".strip())  # fmt: skip
    if alerts:
        lines += ["", "> **Alerts**", ">"] + [f"> - {_md(a)[:300]}" for a in alerts]

    # at a glance ----------------------------------------------------------------------------------------------
    watched = sum(
        1 for r in companies.values() if (r.get("ats") or {}).get("type") in SUPPORTED and r["ats"].get("confirmed")
    )
    researched = sum(1 for r in companies.values() if r.get("research"))
    watches = [e for e in runs if e.get("command") == "watch" and e.get("counts")]
    last_full = max(watches, key=lambda e: e["counts"].get("boards_ok", 0), default=None)
    lines += ["", "## At a glance", "", "| | |", "|---|---|"]
    lines.append(f"| Open jobs tracked | {len(open_jobs)} ({len(new_jobs)} new) |")
    if in_progress:
        lines.append(f"| In progress (applied or later) | {len(in_progress)} ({len(due)} follow-ups due) |")
    lines.append(f"| Shortlist (skill {SHORTLIST_SKILL}+, Maine not ruled out) | {len(shortlist)} |")
    if gone:
        lines.append(f"| Dismissed by hand (hidden here, untouched in the tracker) | {len(gone)} |")
    lines.append(f"| Companies with a watched job board | {watched} of {len(companies)} |")
    lines.append(f"| Companies researched | {researched} |")
    if last_full:
        c = last_full["counts"]
        lines.append(
            f"| Largest board check this period | {c.get('boards_ok', 0)} boards, {c.get('fetched', 0):,} postings, "
            f"{c.get('title_matches', 0):,} title matches, {c.get('kept', 0)} kept |"
        )
    if calls := _model_calls(router_db, since):
        lines.append(f"| Model calls | {', '.join(f'{m} {n}' for m, n in calls.items())} |")

    # in progress ----------------------------------------------------------------------------------------------
    lines += ["", f"## In progress ({len(in_progress)})", ""]
    if in_progress:
        lines += ["| Due | Stage | Job | Company | Next action | Applied |", "|---|---|---|---|---|---|"]
        for r in in_progress:
            flag = due_state(r, day)
            when = f"{flag} {r['follow_up_due']}" if flag else _md(r.get("follow_up_due"))
            title = _md(r.get("job_title")) or r["job_id"]
            link = f"[{title}]({_url(r['posting_url'])})" if r.get("posting_url") else title
            lines.append(f"| {when} | {r['current_stage']} | {link} ({r['job_id']}) | {_md(r.get('company'))} | "
                         f"{_short(r.get('next_action'), 120)} | {_md(r.get('date_applied'))} |")  # fmt: skip
    else:
        lines.append("Nothing yet. When you apply, set the job's stage in the tracker and it moves here.")

    # apply next -----------------------------------------------------------------------------------------------
    lines += ["", f"## Apply next ({len(picks)})", ""]
    if picks:
        lines.append("Best open jobs first. Priority = 2 × skill match + company fit, minus penalties. Applied, or "
                     "not for you? Set the job's stage in the tracker (each card shows its ID); "
                     "`python -m jobsearch dismiss <posting URL>` hides a job that isn't in the tracker.")  # fmt: skip
    else:
        lines.append("No open job has a skill match of 3 or more without a Maine 'no' yet.")
    for i, j in enumerate(picks, 1):
        lines += ["", *_job_card(i, j, companies.get(j.get("company_id")), letters, out_dir)]

    # new since the last report --------------------------------------------------------------------------------
    lines += ["", f"## New since the last report ({len(new_jobs)})", ""]
    lines += _job_table(new_jobs, companies) if new_jobs else ["None."]

    # rest of the shortlist ------------------------------------------------------------------------------------
    lines += ["", f"## Rest of the shortlist ({len(rest)})", ""]
    lines += _job_table(rest, companies) if rest else ["None."]

    # approach without an opening ------------------------------------------------------------------------------
    with_job = {j.get("company_id") for j in shortlist}
    approach = sorted(
        [
            r
            for r in companies.values()
            if r.get("research")
            and _fit(r) >= 4
            and _stability(r) >= 3
            and (r["research"].get("remote_policy") or {}).get("maine_eligible") in ("yes", "varies")
            and r["company_id"] not in with_job
        ],
        key=lambda r: (-_fit(r), -_stability(r), name[r["company_id"]]),
    )
    people_by_company: dict[str, list[dict[str, Any]]] = {}
    all_people = _all(store.data_dir / "people")
    for p in all_people:
        people_by_company.setdefault(p.get("company_id"), []).append(p)
    lines += ["", f"## Approach without an opening ({len(approach)})", ""]
    if approach:
        lines.append("Researched, Maine-eligible, fit 4+ and stable, with no shortlisted job open right now.")
    for r in approach[:10]:
        x = r["research"]
        who = "; ".join(f"{p['name']}, {p['title']}" for p in people_by_company.get(r["company_id"], [])[:3])
        lines += [
            "",
            f"- **{_md(name[r['company_id']])}**: fit {_fit(r)}, stability {_stability(r)}, Maine "
            f"{x['remote_policy']['maine_eligible']}. {_short(x['fit']['rationale'], 200)}",
            f"  - Approach: {_md(x.get('approach_role')) or 'engineering or QA leadership'}"
            + (f" ({_md(who)})" if who else "")
            + f". Angle: {_short(x.get('outreach_angle'), 200)} [People search]({_url(people_search_url(r))})",
        ]
    if len(approach) > 10:
        lines += ["", f"…and {len(approach) - 10} more with the same profile in the tracker."]

    # companies researched -------------------------------------------------------------------------------------
    verified = sorted(
        [r for r in companies.values() if r.get("research") and _changed_since(r, since, "research", ("verify",))],
        key=lambda r: -_fit(r),
    )
    lines += ["", f"## Companies researched ({len(verified)})", ""]
    if verified:
        lines += ["| Company | Fit | Stability | Remote (Maine) | Why |", "|---|---|---|---|---|"]
        for r in verified:
            x = r["research"]
            lines.append(f"| {_md(name[r['company_id']])} | {x['fit']['score']} | {x['stability']['score']} | "
                         f"{x['remote_policy']['maine_eligible']} | {_short(x['fit']['rationale'])} |")  # fmt: skip
    else:
        lines.append("None.")

    # people ---------------------------------------------------------------------------------------------------
    people = [
        p
        for p in all_people
        if (p["key"] not in prev_people if prev else (p.get("first_seen") or "") >= today)
    ]
    lines += ["", f"## People found ({len(people)})", ""]
    lines += [
        f"- {_md(p['name'])}, {_md(p['title'])}, {_md(p.get('company'))} ([source]({_url(p['source_url'])}))"
        for p in people
    ] or ["None."]

    # job boards -----------------------------------------------------------------------------------------------
    new_boards = [
        r
        for r in companies.values()
        if (r.get("ats") or {}).get("type") in SUPPORTED
        and r["ats"].get("confirmed") is True
        and _changed_since(r, since, "ats", ("detect", "discover", "verify"))
    ]
    manual = [r for r in verified if "No supported job board" in str(company_values(r).get("next_research_action"))]
    lines += ["", "## Job boards", "", f"**New boards being watched ({len(new_boards)}).** Check the names: a wrong "
              "board shows jobs under the wrong company. Rule one out with "
              "`python -m jobsearch discover --reject ID=type:token`.", ""]  # fmt: skip
    for r in sorted(new_boards, key=lambda r: name[r["company_id"]]):
        a = r["ats"]
        said = re.search(r"board name ([^;]+)", a.get("note") or "")
        lines.append(f"- {r['company_id']} {_md(name[r['company_id']])}: {a['type']}:{a['token']} "
                     f"({a.get('job_count') if a.get('job_count') is not None else '?'} jobs"
                     + (f"; the board calls itself {_md(said.group(1))}" if said else "") + ")")  # fmt: skip
    if not new_boards:
        lines.append("- None.")
    lines += ["", f"**Researched, but no supported board: check by hand ({len(manual)}).**", ""]
    lines += [
        f"- {_md(name[r['company_id']])}: "
        f"{r['research'].get('career_url') or r['tracker'].get('career_url') or 'no careers URL'}"
        for r in manual
    ] or ["- None."]

    # rejects and errors ---------------------------------------------------------------------------------------
    rejects = []
    for p in sorted((store.data_dir / "rejects").glob("*-*T*.json")):
        m = re.search(r"-(\d{8}T\d{6})", p.name)
        if m and datetime.strptime(m.group(1), "%Y%m%dT%H%M%S").astimezone() >= since:
            rejects.append(p)
    errors = [f"{e['command']}: {f}" for e in runs for f in e.get("failed") or []]
    lines += ["", f"## Rejects and errors ({len(rejects) + len(errors)})", ""]
    for p in rejects:
        detail = json.loads(p.read_text(encoding="utf-8")).get("detail") or ""
        lines.append(f"- Rejected research `{p.name}`: {_md(detail)[:200]}")
    lines += [f"- {_md(x)[:300]}" for x in errors]
    if not rejects and not errors:
        lines.append("None.")

    # run totals -----------------------------------------------------------------------------------------------
    lines += ["", "## Run totals", ""]
    if runs:
        lines += ["| Command | Runs | Totals | Time |", "|---|---|---|---|"]
        for command, entries in _by_command(runs).items():
            totals: dict[str, float] = {}
            for e in entries:
                for k, v in (e.get("counts") or {}).items():
                    if isinstance(v, int | float):
                        totals[k] = totals.get(k, 0) + v
            counts = ", ".join(f"{k}={v:g}" for k, v in totals.items() if v)
            secs = sum(e.get("duration_s") or 0 for e in entries)
            lines.append(f"| {command} | {len(entries)} | {counts or '-'} | {secs / 60:.1f} min |")
    else:
        lines.append("No runs logged in this period.")
    return "\n".join(lines) + "\n"


def _job_card(
    i: int, j: dict[str, Any], c: dict[str, Any] | None, letters: dict[str, list[Path]], out_dir: Path
) -> list[str]:
    s = j.get("score") or {}
    facts = " · ".join(
        x
        for x in (
            f"priority {priority(j, c):g}",
            f"skill {s.get('skill_match') or '?'}/5",
            f"company fit {_fit(c) or '?'}/5",
            f"Maine: {maine_verdict(j)}",
            j.get("work_mode") if j.get("work_mode") not in (None, "Unknown") else "",
            _pay(j),
            f"posted {j['posted_at']}" if j.get("posted_at") else "",
            f"tracker {j['job_id']}" if j.get("job_id") else "",
        )
        if x
    )
    out = [f"### {i}. {_md(j['title'])}, {_md(j.get('company'))}", "", f"{facts} · [open the posting]({_url(j['url'])})", ""]
    if s.get("why"):
        out.append(f"- **Why:** {_short(s['why'], 400)}")
    if s.get("notes"):
        out.append(f"- **Check first:** {_short(s['notes'], 400)}")
    if j.get("location_text"):
        rule = _md(j.get("eligibility_reason")).split(": ")[0]  # the rule; its quote of the location is noise here
        out.append(f"- **Where:** {_short(j['location_text'], 120)}" + (f" ({rule})" if rule else ""))
    if c and (x := c.get("research")):
        flags = f" Red flags: {_sentence(x.get('red_flags'), 200)}" if x.get("red_flags") else ""
        out.append(f"- **Company:** {_sentence(x['fit']['rationale'], 300)} Stability "
                   f"{x['stability']['score']}/5.{flags}")  # fmt: skip
        role = _md(x.get("approach_role")) or "the hiring manager"
        people = (x.get("people") or [])[:3]
        who = "; ".join(f"{p['name']} ({p['title']})" if p["title"] != x.get("approach_role") else p["name"]
                        for p in people)  # fmt: skip
        out.append(
            f"- **Approach:** {role}"
            + (f": {_md(who)}" if who else "")
            + f". {_sentence(x.get('outreach_angle'), 250)} [People search]({_url(people_search_url(c))})"
        )
    elif c:
        out.append(f"- **Company:** not researched yet. [People search]({_url(people_search_url(c))})")
    company_slug = _slug(j.get("company") or "")
    for p in letters.get(company_slug, []):
        rel = os.path.relpath(p, out_dir).replace(os.sep, "/")
        out.append(f"- **Draft cover letter:** [{p.name}]({_url(rel)})")
    return out


def _job_table(js: list[dict[str, Any]], companies: dict[str, dict[str, Any]]) -> list[str]:
    out = ["| Priority | Skill | Co. fit | Company | Job | Maine | Where | Note |", "|---|---|---|---|---|---|---|---|"]
    for j in js:
        c = companies.get(j.get("company_id"))
        s = j.get("score") or {}
        note = s.get("notes") or ("lead from research, verify it" if j.get("source") == "verify" else "")
        out.append(f"| {priority(j, c):g} | {s.get('skill_match') or '-'} | {_fit(c) or '-'} | "
                   f"{_md(j.get('company'))} | [{_md(j['title'])}]({_url(j['url'])}) | {maine_verdict(j)} | "
                   f"{_short(j.get('location_text'), 40)} | {_short(note, 160)} |")  # fmt: skip
    return out


# --- files ------------------------------------------------------------------------------------------------

_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
:root {{ --bg: #fbfaf7; --fg: #1d1d1b; --muted: #6b6a66; --line: #e3e0d8; --accent: #2d5f8b; --note: #f3efe4; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg: #17171a; --fg: #e8e6e1; --muted: #a09d96; --line: #34343a; --accent: #8cb8e0; --note: #24231f; }}
}}
body {{ background: var(--bg); color: var(--fg); font: 16px/1.55 system-ui, sans-serif; margin: 0; }}
main {{ max-width: 980px; margin: 0 auto; padding: 24px 16px 64px; }}
h1 {{ font-size: 1.7rem; margin: 0 0 4px; }} h2 {{ margin-top: 2.2rem; border-bottom: 1px solid var(--line); }}
h3 {{ margin: 1.6rem 0 .3rem; }} a {{ color: var(--accent); }} em {{ color: var(--muted); }}
table {{ border-collapse: collapse; width: 100%; font-size: .92rem; display: block; overflow-x: auto; }}
th, td {{ border-bottom: 1px solid var(--line); padding: 6px 8px; text-align: left; vertical-align: top; }}
blockquote {{ background: var(--note); border-left: 4px solid #c8902e; margin: 16px 0; padding: 8px 16px; }}
code {{ font-size: .9em; }}
</style></head><body><main>
{body}
</main></body></html>
"""


def render_html(md: str, title: str) -> str:
    # html=False: job titles and notes come from the web, so any HTML in them is shown as text, never run.
    body = MarkdownIt("commonmark", {"html": False}).enable("table").render(md)
    return _PAGE.format(title=html.escape(title), body=body)


def write_digest(
    store: Store,
    today: str,
    out_root: Path = OUT,
    runs_log: Path = RUNS_LOG,
    *,
    state_path: Path = REPORT_STATE,
    router_db: Path | None = ROUTER_DB,
    tracker: Tracker | None = None,
    top: int = 8,
) -> Path:
    out_dir = out_root / today
    out_dir.mkdir(parents=True, exist_ok=True)
    prev = read_json(state_path) if state_path.exists() else None
    now = datetime.now().astimezone()
    md = build_digest(store, today, runs_log, prev=prev, now=now, out_root=out_root, router_db=router_db,
                      tracker=tracker, top=top)  # fmt: skip
    path = out_dir / "digest.md"
    path.write_text(md, encoding="utf-8")
    (out_dir / "digest.html").write_text(render_html(md, f"Job search report, {today}"), encoding="utf-8")
    write_json(state_path, snapshot(store, now.isoformat(timespec="seconds")))
    sugg = out_dir / "new_companies.md"
    if not sugg.exists():
        sugg.write_text(
            f"# New company suggestions, {today}\n\nNone. Verification extracts facts about tracker companies only; "
            "it does not collect other employers yet.\n",
            encoding="utf-8",
        )
    return path
