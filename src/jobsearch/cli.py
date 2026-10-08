"""Command-line entry point: python -m jobsearch <command>."""

from __future__ import annotations

import json
import time
from collections import Counter
from datetime import date
from pathlib import Path
from typing import Annotated, NoReturn

import typer
import yaml

from jobsearch.config import ROOT, load_config, pin_endpoint
from jobsearch.detect import SUPPORTED, detect_company, needs_detection
from jobsearch.http_client import PoliteClient
from jobsearch.ingest import PartialExportError, ingest_inbox, now_iso
from jobsearch.models import is_skipped
from jobsearch.runlog import log_run
from jobsearch.store import Store
from jobsearch.triage import dumps_summary, run_triage, summarize
from jobsearch.watch import JobStore, run_watch

app = typer.Typer(no_args_is_help=True, add_completion=False)

DATA_DIRS = ["data/companies", "data/jobs", "data/people", "data/rejects", "inbox", "out"]


def count_files(root: Path = ROOT) -> dict[str, int]:
    counts = {}
    for rel in DATA_DIRS:
        d = root / rel
        if not d.is_dir():
            counts[rel] = 0
            continue
        counts[rel] = sum(1 for p in d.iterdir() if p.is_file() and not p.name.startswith("."))
    return counts


@app.command()
def status(config: Annotated[Path | None, typer.Option(help="Path to config.yaml")] = None) -> None:
    """Print the effective config and counts of data files."""
    cfg = load_config(config)
    typer.echo("# config")
    typer.echo(yaml.safe_dump(cfg.model_dump(), sort_keys=False, width=1000).rstrip())
    typer.echo("\n# data files")
    for rel, n in count_files().items():
        typer.echo(f"{rel:<16} {n}")


@app.command()
def ingest(
    file: Annotated[
        list[Path] | None,
        typer.Option(help="One-off: read these tracker CSV exports into the pipeline instead of the tracker"),
    ] = None,
    force: Annotated[bool, typer.Option(help="With --file: re-read files already read")] = False,
    allow_partial: Annotated[
        bool, typer.Option(help="With --file: accept an export much smaller than the known company list")
    ] = False,
) -> None:
    """Refresh the pipeline's copy of every company from the tracker (data/tracker/companies)."""
    from jobsearch.ingest import ingest_tracker
    from jobsearch.tracker import Tracker

    started = time.monotonic()
    try:
        if file:
            reports = ingest_inbox(Store(), ROOT / "inbox", files=file, force=force, allow_partial=allow_partial)
            typer.echo("note: the tracker itself is unchanged, so the next ingest from it undoes what differs. "
                       "To bring a CSV into the tracker, import it on the tracker page.")  # fmt: skip
        else:
            reports = [ingest_tracker(Store(), Tracker())]
    except PartialExportError as e:
        typer.echo(f"Refused: {e}", err=True)
        log_run({"at": now_iso(), "command": "ingest", "error": str(e)})
        raise typer.Exit(code=1) from e
    totals: dict[str, int] = {}
    for r in reports:
        for msg in r.skipped_files:
            typer.echo(f"skip   {msg}")
        if r.skipped_files:
            continue
        typer.echo(f"ingest {r.source}: " + ", ".join(f"{k}={v}" for k, v in r.counts().items()))
        for cid, f, old, new in r.owner_changes:
            typer.echo(f"  owner change {cid} {f}: {old!r} -> {new!r}")
        if r.marked_missing:
            typer.echo(f"  missing from tracker: {', '.join(r.marked_missing)}")
        if r.ignored_columns:
            typer.echo(f"  ignored columns: {', '.join(r.ignored_columns)}")
        for w in r.warnings:
            typer.echo(f"  warning: {w}")
        for k, v in r.counts().items():
            totals[k] = totals.get(k, 0) + v
    log_run(
        {
            "at": now_iso(),
            "command": "ingest",
            "files": [r.source for r in reports],
            "counts": totals,
            "duration_s": round(time.monotonic() - started, 2),
        }
    )


@app.command()
def detect(
    only: Annotated[list[str] | None, typer.Option(help="Only these company IDs")] = None,
    limit: Annotated[int | None, typer.Option(help="Stop after this many companies")] = None,
    force: Annotated[bool, typer.Option(help="Re-detect even confirmed boards")] = False,
) -> None:
    """Detect job-board systems from career URLs (no LLM)."""
    cfg = load_config()
    store = Store()
    today = date.today()
    started = time.monotonic()
    ids = only or store.company_ids()
    todo = [
        r
        for cid in ids
        if (r := store.load_company(cid)) and needs_detection(r, today, cfg.detect.recheck_days, force)
    ]
    if limit:
        todo = todo[:limit]
    typer.echo(f"detect: {len(todo)} companies to check")
    counts: Counter[str] = Counter()
    with PoliteClient(cfg) as client:
        for rec in todo:
            result = detect_company(rec, client, cfg, today.isoformat())
            if result.ats is None:
                counts["no-url"] += 1
                continue
            ats = result.ats
            key = ats["type"] + (
                ""
                if ats["type"] not in SUPPORTED
                else " (confirmed)"
                if ats["confirmed"]
                else " (unconfirmed)"
            )
            counts[key] += 1
            old = rec.get("ats") or {}
            changed = sorted(k for k in ats if k != "checked_at" and old.get(k) != ats[k])
            rec["ats"] = ats
            if changed:
                rec["history"].append(
                    {"at": now_iso(), "stage": "detect", "changed": [f"ats.{k}" for k in changed]}
                )
            store.save_company(rec)
            token = f" {ats['token']}" if ats["token"] else ""
            name = (rec["tracker"].get("company") or "")[:32]
            typer.echo(f"  {rec['company_id']:<6} {name:<32} {key}{token}  {ats['note'][:60]}")
    typer.echo("\nsummary: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    typer.echo(f"http: {client.stats}")
    log_run(
        {
            "at": now_iso(),
            "command": "detect",
            "counts": dict(counts),
            "http": client.stats,
            "duration_s": round(time.monotonic() - started, 2),
        }
    )


@app.command()
def discover(
    only: Annotated[list[str] | None, typer.Option(help="Only these company IDs")] = None,
    limit: Annotated[int | None, typer.Option(help="Stop after this many companies")] = None,
    pause: Annotated[float, typer.Option(help="Seconds between searches (spares the search engines)")] = 4.0,
    force: Annotated[bool, typer.Option(help="Search again even if searched recently")] = False,
    recheck_other: Annotated[
        bool, typer.Option(help="No search: adopt boards earlier searches noted on systems supported since")
    ] = False,
    reject: Annotated[
        list[str] | None,
        typer.Option(help="Rule out a wrong board for good, e.g. C991=breezy:sense-engineering (clears it if adopted)"),
    ] = None,
) -> None:
    """Find job boards by web search for companies without a confirmed board (no LLM). Best triage fit first."""
    from jobsearch.discover import discovery_order, needs_discovery, reject_board, run_discover

    cfg = load_config()
    store = Store()
    today = date.today()
    started = time.monotonic()
    if reject:
        for item in reject:
            cid, _, board = item.partition("=")
            if not (r := store.load_company(cid.strip())) or ":" not in board:
                raise typer.BadParameter(f"expected ID=type:token, got {item!r}")
            cleared = reject_board(r, board.strip())
            r.setdefault("history", []).append(
                {"at": now_iso(), "stage": "discover", "changed": ["discovery.rejected"] + (["ats"] if cleared else [])}
            )
            store.save_company(r)
            typer.echo(f"{cid}: ruled out {board}" + ("; it was the current board, so detect will start over" if cleared else ""))
        return
    if recheck_other:
        _promote_stored_boards(cfg, store, only, today.isoformat(), started)
        return
    todo = [
        r
        for cid in (only or store.company_ids())
        if (r := store.load_company(cid)) and needs_discovery(r, today, cfg.detect.recheck_days, force)
    ]
    todo.sort(key=discovery_order)
    if limit:
        todo = todo[:limit]
    typer.echo(f"discover: {len(todo)} companies to search")
    with PoliteClient(cfg) as client:
        report = run_discover(
            store, client, cfg, today=today.isoformat(), now=now_iso(), records=todo, pause_s=pause,
            progress=typer.echo,
        )  # fmt: skip
    typer.echo("\nsummary: " + ", ".join(f"{k}={v}" for k, v in report.counts().items()))
    if report.stopped:
        typer.echo(f"STOPPED: {report.stopped}")
    log_run(
        {
            "at": now_iso(),
            "command": "discover",
            "counts": report.counts(),
            "found": report.found,
            "other": report.other,
            "errors": report.errors,
            "stopped": report.stopped,
            "http": client.stats,
            "duration_s": round(time.monotonic() - started, 2),
        }
    )


def _promote_stored_boards(cfg, store: Store, only: list[str] | None, today: str, started: float) -> None:
    from jobsearch.detect import SUPPORTED
    from jobsearch.discover import promote_stored, stored_board_candidates

    found: list[str] = []
    checked = 0
    with PoliteClient(cfg) as client:
        for cid in only or store.company_ids():
            r = store.load_company(cid)
            if not r or is_skipped(r) or r.get("missing_from_tracker"):
                continue
            ats = r.get("ats") or {}
            if (ats.get("type") in SUPPORTED and ats.get("confirmed") is True) or not stored_board_candidates(r):
                continue
            checked += 1
            if not (new := promote_stored(r, client, cfg, today)):
                typer.echo(f"  ..   {cid} {r['tracker'].get('company', '')[:32]}: no confirmed board")
                continue
            r["ats"] = new
            r.setdefault("history", []).append({"at": now_iso(), "stage": "discover", "changed": ["ats"]})
            store.save_company(r)
            found.append(f"{cid} {new['type']}:{new['token']}")
            typer.echo(f"  FOUND {cid} {r['tracker'].get('company', '')[:32]}: {new['type']}:{new['token']} "
                       f"({new['job_count']} jobs; {new['note']})")  # fmt: skip
    typer.echo(f"\nsummary: checked={checked}, found={len(found)}")
    log_run({"at": now_iso(), "command": "discover --recheck-other", "counts": {"checked": checked,
             "found": len(found)}, "found": found, "http": client.stats,
             "duration_s": round(time.monotonic() - started, 2)})  # fmt: skip


@app.command()
def watch(
    only: Annotated[list[str] | None, typer.Option(help="Only these company IDs")] = None,
    quiet: Annotated[bool, typer.Option(help="Only print the summary")] = False,
    explain: Annotated[bool, typer.Option(help="List title matches dropped by the location rules")] = False,
) -> None:
    """Poll job-board APIs for new matching postings (no LLM)."""
    cfg = load_config()
    started = time.monotonic()
    with PoliteClient(cfg) as client:
        report = run_watch(
            Store(),
            client,
            cfg,
            today=date.today().isoformat(),
            now=now_iso(),
            only=only,
            progress=(lambda m: None) if quiet else typer.echo,
        )
    jobs = JobStore(Store())
    for key in report.new:
        j = jobs.load(key)
        pay = (
            f"  {j['currency']} {j['pay_low']:,.0f}-{j['pay_high']:,.0f}/{j['pay_period']}"
            if j["pay_low"]
            else ""
        )
        typer.echo(
            f"  NEW  {j['company'][:24]:<24} {j['title'][:60]:<60} [{j['maine_eligible']}] {j['location_text'][:40]}{pay}"
        )
    for key in report.closed:
        typer.echo(f"  CLOSED {key}")
    typer.echo("\nsummary: " + ", ".join(f"{k}={v}" for k, v in report.counts().items()))
    for f in report.boards_failed:
        typer.echo(f"  failed: {f}")
    if explain:
        typer.echo("\ntitle matches dropped by location:")
        for cid, title, loc, reason in report.dropped:
            typer.echo(f"  {cid:<6} {title[:55]:<55} | {loc[:45]:<45} | {reason[:50]}")
    log_run(
        {
            "at": now_iso(),
            "command": "watch",
            "counts": report.counts(),
            "failed": report.boards_failed,
            "http": client.stats,
            "duration_s": round(time.monotonic() - started, 2),
        }
    )


@app.command()
def triage(
    limit: Annotated[int | None, typer.Option(help="Triage at most this many companies")] = None,
    only: Annotated[list[str] | None, typer.Option(help="Only these company IDs")] = None,
    report_only: Annotated[
        bool, typer.Option("--report", help="Only show distribution and a sample")
    ] = False,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="Save nothing; re-triage even already-triaged companies and compare with their stored triage",
        ),
    ] = False,
    sample: Annotated[int, typer.Option(help="Random rationales to show")] = 10,
    model: Annotated[
        str | None,
        typer.Option(
            help="Use only this configured endpoint (e.g. gemini/gemini-3.5-flash-lite), for comparisons"
        ),
    ] = None,
) -> None:
    """Screen un-triaged companies through the model router (no tools). Safe to kill and rerun."""
    from dotenv import load_dotenv

    from jobsearch.triage import summarize_triages

    load_dotenv(ROOT / ".env")
    cfg = load_config()
    if model:
        try:
            pin_endpoint(cfg, "triage", model)
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--model") from None
    store = Store()
    if report_only:
        typer.echo(dumps_summary(summarize(store, sample=sample)))
        return
    started = time.monotonic()
    r = run_triage(
        store,
        cfg,
        today=date.today().isoformat(),
        now=now_iso(),
        limit=limit,
        only=only,
        dry_run=dry_run,
        progress=typer.echo,
    )
    typer.echo(
        f"\ntriaged={len(r.triaged)} failed={len(r.failed)} calls={r.calls} rejects={len(r.rejects)} "
        f"escalations={r.escalations} cached={r.cached} models={dict(r.models)} tokens={dict(r.tokens)}"
        + (f"  STOPPED: {r.stopped_for_limit}" if r.stopped_for_limit else "")
    )
    for n in r.notes:
        typer.echo(f"  note: {n}")
    log_run(
        {
            "at": now_iso(),
            "command": "triage" + (" --dry-run" if dry_run else ""),
            "models": dict(r.models),
            "counts": {
                "triaged": len(r.triaged),
                "failed": len(r.failed),
                "calls": r.calls,
                "rejects": len(r.rejects),
                "escalations": r.escalations,
                "cached": r.cached,
            },  # fmt: skip
            "tokens": dict(r.tokens),
            "failed": r.failed,
            "call_durations_s": [round(d, 1) for d in r.durations],
            "stopped_for_limit": r.stopped_for_limit,
            "duration_s": round(time.monotonic() - started, 2),
        }
    )
    if not dry_run:
        typer.echo(dumps_summary(summarize(store, sample=sample)))
        return
    rows = []
    for cid, new in r.results.items():
        rec = store.load_company(cid)
        rows.append(
            {"id": cid, "company": rec["tracker"].get("company"), "new": new, "stored": rec.get("triage")}
        )
    out = ROOT / "logs" / f"triage-dryrun-{now_iso()[:19].replace(':', '')}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(dumps_summary({"results": rows}), encoding="utf-8")
    typer.echo(dumps_summary(summarize_triages([(x["id"], x["company"], x["new"]) for x in rows], sample)))
    compared = [x for x in rows if x["stored"]]
    if compared:
        typer.echo(
            f"\n# new vs stored triage ({compared[0]['stored'].get('model')}), {len(compared)} companies"
        )
        typer.echo(f"{'id':<6} {'company':<28} fit  stab remote")
        for x in compared:
            n, o = x["new"], x["stored"]
            typer.echo(f"{x['id']:<6} {str(x['company'])[:28]:<28} {o['fit']}->{n['fit']} {o['stability']}->{n['stability']} "
                       f"{o['remote']}->{n['remote']}")  # fmt: skip
        same = sum(abs(x["new"]["fit"] - x["stored"]["fit"]) <= 1 for x in compared)
        typer.echo(f"fit within 1 point of stored: {same}/{len(compared)}")
    typer.echo(f"\nfull results: {out}")


@app.command()
def doctor(
    skip_models: Annotated[bool, typer.Option(help="Only check SearXNG and budgets")] = False,
) -> None:
    """Check every configured model endpoint (1-token call), SearXNG, and remaining daily budgets."""
    from dotenv import load_dotenv

    from jobsearch import doctor as doc
    from jobsearch.config import TIERS
    from jobsearch.usage import connect

    load_dotenv(ROOT / ".env")
    cfg = load_config()
    db = connect()
    checks = []
    if not skip_models:
        for tier in TIERS:
            for ep in cfg.providers.tier(tier):
                typer.echo(f"checking {tier}: {ep.model} ...")
                checks.append((tier, doc.check_endpoint(ep, db)))
    checks.append(("search", doc.check_searxng(cfg.search.searxng_url)))
    typer.echo("")
    for tier, c in checks:
        mark = "SKIP" if c.ok is None else "OK  " if c.ok else "FAIL"
        typer.echo(f"{mark} {tier:<11} {c.name:<36} {c.detail}")
    typer.echo("\n# daily budgets left")
    for tier, model, left in doc.budgets(cfg, db):
        typer.echo(f"{tier:<11} {model:<36} {left}")
    failed = [c.name for _, c in checks if c.ok is False]
    log_run({"at": now_iso(), "command": "doctor", "failed": failed})
    if failed:
        raise typer.Exit(code=1)


@app.command()
def verify(
    limit: Annotated[
        int | None, typer.Option(help="Verify at most this many (capped by verify.per_run)")
    ] = None,
    only: Annotated[list[str] | None, typer.Option(help="Only these company IDs (hand-picked runs)")] = None,
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Save nothing to data/; write results to logs/ for comparison")
    ] = False,
    model: Annotated[
        str | None,
        typer.Option(help="Use only this configured endpoint for both extract and judge (tier comparisons)"),
    ] = None,
    show_queue: Annotated[int, typer.Option("--queue", help="Only print the first N of the queue")] = 0,
    force: Annotated[
        bool, typer.Option(help="Verify --only companies even if researched recently or rejected twice")
    ] = False,
) -> None:
    """Verify queued companies: evidence pack -> extract -> grounding check -> judge. Safe to kill and rerun."""
    import json

    from dotenv import load_dotenv

    from jobsearch.queue import verify_queue
    from jobsearch.verify import run_verify

    load_dotenv(ROOT / ".env")
    cfg = load_config()
    store = Store()
    if show_queue:
        q = verify_queue(store, date.today(), cfg.verify.refresh_days, only, force)
        typer.echo(f"queue: {len(q)} companies")
        for r in q[:show_queue]:
            t = r.get("triage") or {}
            typer.echo(f"  {r['company_id']:<6} {str(r['tracker'].get('company'))[:36]:<36} "
                       f"rank={r['tracker'].get('overall_rank')} fit={t.get('fit')} stab={t.get('stability')} "
                       f"remote={t.get('remote')}")  # fmt: skip
        return
    if model:
        try:
            pin_endpoint(cfg, "extract", model)
            pin_endpoint(cfg, "judge", model)
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--model") from None
    started = time.monotonic()
    with PoliteClient(cfg) as client:
        r = run_verify(store, cfg, client, today=date.today().isoformat(), now=now_iso(), limit=limit,
                       only=only, force=force or dry_run, dry_run=dry_run, progress=typer.echo)  # fmt: skip
    counts = {"queued": r.queued, "verified": r.count("verified"), "rejected": r.count("rejected")}
    typer.echo("\nsummary: " + ", ".join(f"{k}={v}" for k, v in counts.items())
               + (f"  STOPPED: {r.stopped}" if r.stopped else ""))  # fmt: skip
    for o in r.outcomes:
        if o.reject_path:
            typer.echo(f"  reject saved: {o.reject_path}")
    companies = [
        {"id": o.company_id, "status": o.status, "detail": o.detail, "grounding": o.grounding, "steps": o.steps,
         "new_ats": o.new_ats, "pack": o.pack, "notes": o.notes, "duration_s": round(o.duration_s, 1)}
        for o in r.outcomes
    ]  # fmt: skip
    log_run({"at": now_iso(), "command": "verify" + (" --dry-run" if dry_run else ""), "model": model,
             "counts": counts, "stopped": r.stopped, "http": client.stats, "companies": companies,
             "duration_s": round(time.monotonic() - started, 2)})  # fmt: skip
    if dry_run:
        tag = (model or "routed").replace("/", "_").replace(":", "_")
        out = ROOT / "logs" / f"verify-dryrun-{tag}-{now_iso()[:19].replace(':', '')}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        rows = [c | {"research": o.research} for c, o in zip(companies, r.outcomes, strict=True)]
        out.write_text(
            json.dumps({"model": model, "results": rows}, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        typer.echo(f"full results: {out}")


@app.command()
def rank(
    limit: Annotated[
        int | None, typer.Option(help="Score at most this many jobs (capped by rank.per_run)")
    ] = None,
    rescore: Annotated[
        bool, typer.Option(help="Rescore jobs even if their posting text is unchanged")
    ] = False,
    model: Annotated[str | None, typer.Option(help="Use only this configured endpoint")] = None,
) -> None:
    """Score open jobs against the profile (skill match, seniority, Maine eligibility from the posting text)."""
    from dotenv import load_dotenv

    from jobsearch.rank import run_rank

    load_dotenv(ROOT / ".env")
    cfg = load_config()
    if model:
        try:
            pin_endpoint(cfg, "posting_relevance", model)
        except ValueError as e:
            raise typer.BadParameter(str(e), param_hint="--model") from None
    started = time.monotonic()
    with PoliteClient(cfg) as client:
        r = run_rank(Store(), cfg, client, today=date.today().isoformat(), limit=limit, rescore=rescore,
                     progress=typer.echo)  # fmt: skip
    typer.echo("\nsummary: " + ", ".join(f"{k}={v}" for k, v in r.counts().items())
               + (f"  STOPPED: {r.stopped}" if r.stopped else ""))  # fmt: skip
    for f in r.failed:
        typer.echo(f"  failed: {f}")
    log_run({"at": now_iso(), "command": "rank", "counts": r.counts(), "models": r.models, "failed": r.failed,
             "stopped": r.stopped, "duration_s": round(time.monotonic() - started, 2)})  # fmt: skip


@app.command()
def export(
    dry_run: Annotated[
        bool,
        typer.Option("--dry-run", help="Change nothing; write a CSV preview of the export to out/DATE/preview/"),
    ] = False,
    csv: Annotated[bool, typer.Option("--csv", help="Also write the import CSVs to out/DATE/ as a backup")] = False,
) -> None:
    """Send the pipeline's research, jobs and people into the tracker (changed cells only), as the pipeline."""
    from jobsearch.export import run_export
    from jobsearch.ingest import ingest_tracker
    from jobsearch.tracker import Tracker

    started = time.monotonic()
    store, tracker = Store(), Tracker()
    if not dry_run:
        ingest_tracker(store, tracker)  # so the rules see the owner's latest edits, research status above all
    r = run_export(store, load_config(), today=date.today().isoformat(), now=now_iso(), dry_run=dry_run,
                   tracker=tracker, csv_backup=csv)  # fmt: skip
    typer.echo(
        ("would export: " if dry_run else "exported: ") + ", ".join(f"{k}={v}" for k, v in r.counts().items())
    )
    if r.skipped_status:
        typer.echo(f"  research_status left alone (owner set it): {len(r.skipped_status)} companies")
    if r.gone:
        typer.echo(f"  deleted in the tracker, left deleted: {', '.join(r.gone)}")
    for msg in r.refused[:20]:
        typer.echo(f"  refused: {msg}")
    for name, path in r.files.items():
        typer.echo(f"  {name}: {path}")
    if not dry_run:
        log_run({"at": now_iso(), "command": "export", "counts": r.counts(), "failed": r.refused,
                 "files": {k: str(v) for k, v in r.files.items()},
                 "duration_s": round(time.monotonic() - started, 2)})  # fmt: skip


@app.command()
def dismiss(
    jobs: Annotated[list[str], typer.Argument(help="Posting URLs as the report links them, or job keys")],
    reason: Annotated[str, typer.Option(help="Why, for the record")] = "",
    undo: Annotated[bool, typer.Option(help="Bring them back")] = False,
) -> None:
    """Hide jobs you've ruled out from the report. The tracker is never touched: close them there yourself."""
    from jobsearch.export import DISMISSED, _all
    from jobsearch.store import read_json, write_json

    store = Store()
    path = store.data_dir / DISMISSED
    gone = read_json(path) if path.exists() else {}
    known = {j["key"]: j for j in _all(store.data_dir / "jobs")}
    by_url = {j.get("url"): k for k, j in known.items()}
    for item in jobs:
        key = by_url.get(item, item)
        if key not in known:
            typer.echo(f"  no job with that key or URL: {item}")
            continue
        if undo:
            gone.pop(key, None)
        else:
            gone[key] = reason or "dismissed by hand"
        typer.echo(f"  {'restored' if undo else 'dismissed'}: {known[key].get('company')} - {known[key]['title']}")
    write_json(path, dict(sorted(gone.items())))


@app.command()
def digest() -> None:
    """Write the report: out/YYYY-MM-DD/digest.md and digest.html (open it in a browser)."""
    from jobsearch.digest import write_digest
    from jobsearch.tracker import Tracker

    cfg = load_config()
    path = write_digest(Store(), date.today().isoformat(), tracker=Tracker(), top=cfg.nightly.report_top)
    typer.echo(f"report: {path.with_suffix('.html')} (markdown: {path})")


@app.command()
def nightly(
    watch_only: Annotated[
        bool, typer.Option(help="Only watch the boards and score new postings")
    ] = False,
) -> None:
    """Run every stage in order and end with the report (plan phase 7).

    If another run is going, waits up to an hour for it (--watch-only exits at once instead).
    """
    from jobsearch.nightly import AlreadyRunning, run_nightly

    try:
        entry = run_nightly(load_config(), watch_only=watch_only)
    except AlreadyRunning as e:
        typer.echo(f"nightly: {e}; exiting")
        if not watch_only:  # Task Scheduler's last result then shows the nightly didn't run
            raise typer.Exit(code=1) from e
        return
    for s in entry["stages"]:
        state = s.get("skipped") or ("ok" if s.get("exit") == 0 else f"exit {s.get('exit')}")
        typer.echo(f"  {s['stage']:<9} {state:<16} {s.get('minutes', 0):>5} min")
    for a in entry["alerts"]:
        typer.echo(f"ALERT: {a}")


@app.command()
def serve(
    port: Annotated[int, typer.Option(help="Port on 127.0.0.1")] = 8787,
    data: Annotated[
        Path | None, typer.Option(help="Use another tracker folder instead of data/tracker (for trying things out)")
    ] = None,
    open_browser: Annotated[bool, typer.Option("--open", help="Open the page in your browser")] = False,
) -> None:
    """Open the tracker: a page on this PC only, at http://127.0.0.1:8787 (plan-tracker.md)."""
    from jobsearch.server import serve as run

    try:
        run(port=port, root=data, open_browser=open_browser)
    except OSError as e:
        typer.echo(f"serve: can't use port {port} ({e}); is the tracker already open? Try --port 8788", err=True)
        raise typer.Exit(code=1) from e


tracker_app = typer.Typer(no_args_is_help=True, help="The job-search tracker in data/tracker/ (plan-tracker.md).")
app.add_typer(tracker_app, name="tracker")

CLAUDE_AI_SNAPSHOT = ROOT / "data" / "snapshots" / "claude-ai-tracker-2026-10-07"


@tracker_app.command("migrate")
def tracker_migrate(
    src: Annotated[
        Path, typer.Option("--from", help="The claude.ai tracker's downloaded collections, one folder each")
    ] = CLAUDE_AI_SNAPSHOT,
) -> None:
    """Load the claude.ai tracker's rows into data/tracker/. A row already there is never overwritten."""
    from jobsearch.tracker import Tracker

    started = time.monotonic()
    rep = Tracker().migrate(src)
    typer.echo("created:   " + (", ".join(f"{k} {v}" for k, v in rep.created.items()) or "nothing"))
    typer.echo("unchanged: " + (", ".join(f"{k} {v}" for k, v in rep.unchanged.items()) or "nothing"))
    typer.echo(f"sectors merged: {rep.sectors_merged}; rows linked to their company: {rep.linked}")
    for label, items in (("no company matches", rep.unlinked), ("left alone, differs from the source", rep.differs)):
        if items:
            typer.echo(f"{label} ({len(items)}): " + ", ".join(items[:20]) + (" ..." if len(items) > 20 else ""))
    if rep.unknown_fields:
        typer.echo("kept, but not in the schema: " + ", ".join(f"{k} {v}" for k, v in rep.unknown_fields.items()))
    log_run({"at": now_iso(), "command": "tracker migrate", "counts": dict(rep.created),
             "duration_s": round(time.monotonic() - started, 2)})  # fmt: skip


TrackerData = Annotated[Path | None, typer.Option("--data", help="Use another tracker folder instead of data/tracker")]
AgentName = Annotated[str, typer.Option("--by", help='Your agent name, shown as the writer (never "you": that is the owner)')]
Fields = Annotated[list[str] | None, typer.Argument(help='Fields as key=value, e.g. research_notes="..."')]
TabName = Annotated[str, typer.Argument(help="jobs, companies, people, activity or materials")]


def _tracker(data: Path | None):
    from jobsearch.tracker import Tracker

    return Tracker(data) if data else Tracker()


def _refuse(message: str) -> NoReturn:
    typer.echo(f"refused: {message}", err=True)
    raise typer.Exit(code=1)


def _agent(by: str) -> str:
    """Agents write under their own name. The owner is "you" and edits in the page, so the CLI never writes as "you"."""
    from jobsearch.tracker import OWNER

    by = by.strip()
    if not by or by.lower() == OWNER:
        _refuse('--by must be your agent name; "you" is the owner, who edits in the page')
    return by


def _pairs(items: list[str] | None) -> dict[str, str]:
    out = {}
    for item in items or []:
        key, sep, value = item.partition("=")
        if not sep or not key.strip():
            _refuse(f"expected key=value, got {item!r}")
        out[key.strip()] = value
    return out


@tracker_app.command("stats")
def tracker_stats(data: TrackerData = None) -> None:
    """Rows per tab, and who last wrote them."""
    for name, writers in _tracker(data).stats().items():
        by = ", ".join(f"{w} {n}" for w, n in writers.most_common())
        typer.echo(f"{name:<10} {sum(writers.values()):>5}" + (f"   last written by: {by}" if by else ""))


@tracker_app.command("list")
def tracker_list(
    name: TabName,
    where: Annotated[
        list[str] | None, typer.Option(help='Only rows whose field equals the value, e.g. research_status="Not started"')
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print the rows as JSON")] = False,
    data: TrackerData = None,
) -> None:
    """List a tab's rows. Check what's there before adding anything."""
    from jobsearch.tracker import TrackerError, collection

    try:
        col, rows = collection(name), _tracker(data).load(name)
    except TrackerError as e:
        _refuse(str(e))
    for key, value in _pairs(where).items():
        rows = [r for r in rows if str(r.get(key, "")) == value]
    if as_json:
        typer.echo(json.dumps(rows, ensure_ascii=False, indent=2))
        return
    cols = [c for c in col.columns if c in col.keys][:5]
    for r in rows:
        typer.echo(" | ".join(str(r.get(c, ""))[:40] for c in cols))
    typer.echo(f"{len(rows)} {name}")


@tracker_app.command("get")
def tracker_get(
    name: TabName, row_id: Annotated[str, typer.Argument(help="The row's ID")], data: TrackerData = None
) -> None:
    """Print one row as JSON."""
    from jobsearch.tracker import TrackerError

    try:
        row = _tracker(data).get(name, row_id)
    except TrackerError as e:
        _refuse(str(e))
    if row is None:
        _refuse(f"there is no {name} row {row_id}")
    typer.echo(json.dumps(row, ensure_ascii=False, indent=2))


@tracker_app.command("next-id")
def tracker_next_id(name: TabName, data: TrackerData = None) -> None:
    """The ID the next added row gets: J037, C1220, P005..."""
    from jobsearch.tracker import TrackerError

    try:
        typer.echo(_tracker(data).next_id(name))
    except TrackerError as e:
        _refuse(str(e))


@tracker_app.command("add")
def tracker_add(
    name: TabName,
    fields: Fields = None,
    by: AgentName = ...,
    row_id: Annotated[str | None, typer.Option("--id", help="The new row's ID (default: the next free one)")] = None,
    data: TrackerData = None,
) -> None:
    """Add a row as an agent. Jobs start at stage Lead. A field you may not set refuses the whole row."""
    from jobsearch.tracker import TrackerError

    t, by = _tracker(data), _agent(by)
    try:
        row_id = row_id or t.next_id(name)
        row = t.write(name, row_id, _pairs(fields), by=by, create=True)
    except TrackerError as e:
        _refuse(str(e))
    typer.echo(f"added {row_id}")
    typer.echo(json.dumps(row, ensure_ascii=False, indent=2))


@tracker_app.command("update")
def tracker_update(
    name: TabName,
    row_id: Annotated[str, typer.Argument(help="The row's ID")],
    fields: Fields = None,
    by: AgentName = ...,
    data: TrackerData = None,
) -> None:
    """Change fields of a row as an agent. A field you may not change refuses the whole update; blanks are ignored."""
    from jobsearch.tracker import TrackerError

    t, by = _tracker(data), _agent(by)
    try:
        before = t.get(name, row_id) or {}
        after = t.write(name, row_id, _pairs(fields), by=by)
    except TrackerError as e:
        _refuse(str(e))
    changed = sorted(k for k in after if k not in ("updated_by", "updated_at") and before.get(k) != after.get(k))
    typer.echo(f"updated {row_id}: {', '.join(changed)}" if changed else f"{row_id}: nothing changed")


@tracker_app.command("queue-companies")
def tracker_queue_companies(
    file: Annotated[Path, typer.Option(help="A JSON list of {company, sector, interests, career_url, research_notes}")],
    label: Annotated[str, typer.Option(help='What the batch is, shown to the owner, e.g. "Maine nonprofits from Codex"')],
    by: AgentName = ...,
    data: TrackerData = None,
) -> None:
    """Queue companies for the owner to add with one click on the Companies tab. Use it for more than a few."""
    from jobsearch.tracker import TrackerError

    by = _agent(by)
    try:
        rows = json.loads(file.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        _refuse(f"can't read {file}: {e}")
    rows = rows.get("rows") if isinstance(rows, dict) else rows
    if not isinstance(rows, list):
        _refuse("the file must hold a JSON list of companies")
    try:
        batch = _tracker(data).queue_companies(rows, label=label, by=by)
    except TrackerError as e:
        _refuse(str(e))
    typer.echo(f"queued {len(rows)} companies as {batch}: they wait on the Companies tab until the owner adds them")
