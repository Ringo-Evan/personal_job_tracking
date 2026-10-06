"""Daily digest (plan.md Phase 6): a five-minute read built from data/, logs/runs.jsonl and data/rejects/."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from jobsearch.export import OUT, _all, company_values
from jobsearch.rank import maine_verdict, priority
from jobsearch.runlog import RUNS_LOG
from jobsearch.store import Store


def _fit(company: dict[str, Any] | None) -> int:
    if not company:
        return 0
    r, t = company.get("research") or {}, company.get("triage") or {}
    return (r.get("fit") or {}).get("score") or t.get("fit") or 0


def _runs_today(log: Path, today: str) -> list[dict[str, Any]]:
    if not log.exists():
        return []
    out = []
    for line in log.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        if str(e.get("at", "")).startswith(today):
            out.append(e)
    return out


def _by_command(runs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for e in runs:
        out.setdefault(e.get("command", "?"), []).append(e)
    return out


def _md(text: Any) -> str:
    return str(text or "").replace("|", "/").replace("\n", " ").strip()


def build_digest(store: Store, today: str, runs_log: Path = RUNS_LOG) -> str:
    companies = {r["company_id"]: r for r in store.iter_companies()}
    name = {cid: r["tracker"].get("company") or cid for cid, r in companies.items()}
    lines = [f"# Job search digest, {today}", ""]

    # 1. new jobs, ranked (job fit x company fit x Maine), then the best open jobs overall
    all_jobs = _all(store.data_dir / "jobs")

    def ranked(js: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return sorted(
            js, key=lambda j: (-priority(j, companies.get(j.get("company_id"))), j.get("title") or "")
        )

    def job_table(js: list[dict[str, Any]]) -> list[str]:
        out = ["| Rank | Skill | Co. fit | Company | Job | Maine | Note |", "|---|---|---|---|---|---|---|"]
        for j in js:
            c = companies.get(j.get("company_id"))
            s = j.get("score") or {}
            note = s.get("notes") or ("lead from research, verify it" if j.get("source") == "verify" else "")
            out.append(f"| {priority(j, c):g} | {s.get('skill_match') or '-'} | {_fit(c) or '-'} | "
                       f"{_md(j.get('company'))} | [{_md(j['title'])}]({j['url']}) | {maine_verdict(j)} | "
                       f"{_md(note)} |")  # fmt: skip
        return out

    jobs = ranked([j for j in all_jobs if j.get("first_exported") == today])
    lines += [f"## 1. New jobs ({len(jobs)})", ""]
    lines += job_table(jobs) if jobs else ["None today."]
    unscored = sum(1 for j in jobs if not j.get("score"))
    if unscored:
        lines += ["", f"{unscored} new jobs are not scored yet (run `python -m jobsearch rank`)."]
    best = [j for j in ranked([j for j in all_jobs if not j.get("closed_at")]) if j not in jobs][:10]
    if best:
        lines += ["", "**Best open jobs from earlier days:**", "", *job_table(best)]

    # 2. companies verified today
    verified = [r for r in companies.values() if (r.get("research") or {}).get("at") == today]
    verified.sort(key=lambda r: -_fit(r))
    lines += ["", f"## 2. Companies verified today ({len(verified)})", ""]
    if verified:
        lines += ["| Company | Fit | Stability | Remote (Maine) | Why |", "|---|---|---|---|---|"]
        for r in verified:
            x = r["research"]
            lines.append(f"| {_md(name[r['company_id']])} | {x['fit']['score']} | {x['stability']['score']} | "
                         f"{x['remote_policy']['maine_eligible']} | {_md(x['fit']['rationale'])} |")  # fmt: skip
    else:
        lines.append("None today.")

    # 3. people found
    people = [p for p in _all(store.data_dir / "people") if p.get("first_exported") == today]
    lines += ["", f"## 3. People found ({len(people)})", ""]
    lines += [f"- {_md(p['name'])}, {_md(p['title'])}, {_md(p.get('company'))} ([source]({p['source_url']}))"
              for p in people] or ["None today."]  # fmt: skip

    # 4. no supported job board
    manual = [
        r for r in verified if "No supported job board" in str(company_values(r).get("next_research_action"))
    ]
    lines += ["", f"## 4. No supported job board, check by hand ({len(manual)})", ""]
    lines += [f"- {_md(name[r['company_id']])}: {r['research'].get('career_url') or r['tracker'].get('career_url') or 'no careers URL'}"
              for r in manual] or ["None."]  # fmt: skip

    # 5. rejects
    stamp = today.replace("-", "")
    rejects = sorted(p.name for p in (store.data_dir / "rejects").glob(f"*-{stamp}T*.json"))
    lines += ["", f"## 5. Rejects ({len(rejects)})", ""]
    for fname in rejects:
        detail = (
            json.loads((store.data_dir / "rejects" / fname).read_text(encoding="utf-8")).get("detail") or ""
        )
        lines.append(f"- `{fname}`: {_md(detail)[:200]}")
    if not rejects:
        lines.append("None.")

    # 6. errors, 7. totals
    runs = _runs_today(runs_log, today)
    errors = []
    for e in runs:
        for f in e.get("failed") or []:
            errors.append(f"{e['command']}: {f}")
        if e.get("error"):
            errors.append(f"{e['command']}: {e['error']}")
        for k in ("stopped", "stopped_for_limit"):
            if e.get(k):
                errors.append(f"{e['command']} stopped early: {e[k]}")
    lines += ["", f"## 6. Errors ({len(errors)})", ""]
    lines += [f"- {_md(x)[:300]}" for x in errors] or ["None."]

    lines += ["", "## 7. Run totals", ""]
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
        lines.append("No runs logged today.")
    return "\n".join(lines) + "\n"


def write_digest(store: Store, today: str, out_root: Path = OUT, runs_log: Path = RUNS_LOG) -> Path:
    out_dir = out_root / today
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "digest.md"
    path.write_text(build_digest(store, today, runs_log), encoding="utf-8")
    sugg = out_dir / "new_companies.md"
    if not sugg.exists():
        sugg.write_text(
            f"# New company suggestions, {today}\n\nNone. Verification extracts facts about tracker companies only; "
            "it does not collect other employers yet.\n",
            encoding="utf-8",
        )
    return path
