# Plan: the pipeline becomes the tracker

Owner decision, 2026-10-07: this repo replaces the claude.ai Job Search Tracker as the one place to work. The claude.ai
tracker could only be written by claude.ai chats and its own page, so everything else (this pipeline, Claude Code,
Codex, local models) had to go through CSV files the owner imported by hand. Its own write-up ("Job Search Tracker -
How It Works and Why.md") names that as the main reason to re-implement.

Owner's choices: a local page on the data this repo already keeps (not Postgres + NocoDB, not SQLite), reachable from
this PC only. This plan overrides plan.md where they conflict: section 5 (the CSV contract) becomes the rules for
writing into the local store, Phase 1's CSV ingest and Phase 6's CSV export become the sync between the pipeline and
the store, and the runbook's import steps go away.

## Design

**Store.** `data/tracker/<collection>/<ID>.json`, one flat JSON document per row, exactly as the claude.ai tracker kept
them: the same snake_case keys (so the field labels and every existing mapping still apply), plus `updated_by` and
`updated_at`. Collections: `jobs`, `companies`, `people`, `activity`, `materials`, and `imports` (the bulk-add queue).
Every change also appends `{at, by, collection, id, changes: {field: [old, new]}}` to `data/tracker/history.jsonl`, so
old values are kept (the claude.ai tracker kept only the last writer). `data/` is in git.

**One write path.** Every writer (the page, the pipeline, the agent CLI) goes through one function in
`src/jobsearch/tracker.py`, under a short lock so two processes can't interleave a read-modify-write.

**Field ownership** (the claude.ai tracker's rules, now enforced in code rather than by convention):

| Tier | Fields | Who may write |
|---|---|---|
| Agent-maintained | Companies: research, screening and outreach columns (plan.md 5.3). Jobs: listing, location, pay and fit columns. People: name, title, company, source URL, why approach | Anyone |
| Insert-only | Jobs: track, current stage (Lead), highest stage (None), next action, date discovered. People: status (To research), relationship (Cold), next step | Agents only when creating the row; the owner any time |
| Owner-only | Everything else: company ranks, preference scores, dealbreakers, your notes; job stages after creation, application, materials and interview fields; people status after creation, notes, contact dates; all of Activity and Materials | The owner only |

The owner's writer name is `you`. Any other name is an agent (the pipeline writes as `pipeline`). Agents can't delete
rows and can't write Activity or Materials. Agent writes never blank a field (the CSV rule "a blank cell never
overwrites"); the owner can clear one. Raising `highest_stage` when the stage moves forward happens in the write path,
so it holds for every writer.

**Concurrent edits.** A writer passes the values it last saw for the fields it changes. If any of those fields changed
since, the write is refused and the writer reloads. Edits to different fields of the same row never collide, so the
nightly can update a job's pay while the owner edits its stage.

**IDs.** Owner-entered rows keep `J###`, `C###`, `P###`, `A###`, `R###`: the next ID is the highest number with that
exact prefix, plus one. Pipeline rows use `JW####` and `PA####` from the counters in `data/ids.json`, never reused.

**The pipeline and the store.** The pipeline keeps its working records (`data/companies`, `data/jobs`, `data/people`).
The store is the record the owner works in. Two syncs connect them, reusing the CSV contract's rules:

- ingest: the store's company rows replace the CSV export as the source of each company's `tracker` block;
- export: the agent-maintained fields go straight into the store as `pipeline`, only cells whose value changed since
  the last export, insert-only fields on first export only. CSV stays available as a backup and for one-off imports.

**Which pipeline jobs reach the Jobs tab:** jobs on the shortlist (skill match 3+, Maine not ruled out, not dismissed),
plus any job already in the store, which keeps getting updates. The rest stay in the report, ranked low.

**The page.** A port of the claude.ai tracker's page, with the same layout and behavior: header totals, the stage
strip, tabs with row counts and default columns, search, sort, track and sector filters, the edit drawer, due flags,
provenance under the ID, CSV import and export, light and dark mode, phone width. Served by
`python -m jobsearch serve` on 127.0.0.1 only. Writes need a custom request header and a local Host header, so other
websites open in the browser can't write to it. The page polls for changes, so the nightly's updates appear without a
reload. Added: a **Today** tab with the latest report. Changed: CSV export always writes every row (the claude.ai export
silently dropped filtered rows), and the Agent guide is rewritten for local agents. Dropped: "Screen with Claude" (the
nightly triage does this).

**Migration.** The claude.ai tracker's database was downloaded read-only on 2026-10-07: 1,219 companies, 36 jobs (all
Lead, none applied), 4 people, no activity, no materials, no owner fields filled in, and the `meta/schema` document.
It's loaded as is, provenance kept, with the near-duplicate sectors merged (Labor into Labor & organizing, Maine
employers into Maine & New England, Martial arts into Martial arts & fitness). Jobs, people and activity also get a
`company_id`, matched by company name, so they no longer depend on the name alone (the write-up's checklist: "a real
company_id foreign key ... with name kept as a display field"). The download is kept in
`data/snapshots/claude-ai-tracker-2026-10-07/`. Afterwards the claude.ai tracker is a frozen snapshot: kept, no
longer edited.

## Phases

Stop after each phase's acceptance checks and wait for the owner's go-ahead.

### Phase T1: the store and the migration
- `tracker.py`: field schema (keys, labels, types, groups, enums, ownership tiers) for all five collections, load,
  get, the one write path, delete, next ID, history log, lock.
- `python -m jobsearch tracker migrate --from <downloaded dir>` and `tracker stats`.
- **Accept:** the store holds 1,219 companies, 36 jobs, 4 people, 0 activity, 0 materials; every field of every row
  equals the download except the merged sectors and the added company_id; a second migration changes nothing. Tests: an agent can't write an
  owner-only field, sets insert-only fields only on create, never blanks a field and can't delete; the owner can do
  all of these; a stale write to a changed field is refused while a write to another field goes through; history keeps
  old values; next IDs per prefix.

### Phase T2: the page
- `python -m jobsearch serve`, the ported page, a small JSON API over the store, polling, CSV import and export, the
  Today tab, the local Agent guide.
- **Accept:** the owner opens the page and finds the same tracker; editing a job's stage saves it as `you`, raises the
  highest stage and updates the strip; due flags, search, sort and filters work; a write without the custom header or
  from a non-local Host is refused.

### Phase T3: the pipeline writes straight in
- ingest reads company rows from the store; export writes into the store; the nightly no longer writes CSV previews.
- The report knows job stages: jobs past Lead leave Apply next; follow-ups due and jobs in progress get their own
  section; the Jobs-tab stage replaces `dismiss` for jobs in the store.
- **Accept:** a nightly run fills the pipeline's research, screening and jobs into the store; an owner edit made during
  the day survives it; marking a job Applied takes it out of Apply next the next morning.

### Phase T4: agents and cutover
- `AGENTS.md` and `CLAUDE.md` carry the agent rules (from `meta/schema`, updated for local agents); a `tracker` CLI
  for agents (list, get, add, update, with `--by`); the bulk-add queue on the Companies tab; the page starts at logon
  (Task Scheduler); README.
- **Accept:** Claude Code adds a company and updates a job through the CLI, and an owner-only field write is refused;
  the claude.ai tracker is frozen.

## Status

All four phases were done on 2026-10-07, with T3 before T2: T3 changes the nightly, so it went first, to land and be
tested well before that night's 02:30 run. Results:

- T1: 1,219 companies, 36 jobs and 4 people migrated; every field equals the download except 16 merged sectors and
  the added company_id (40 rows linked); a second run changed nothing.
- T3: the first export into the store wrote 1,074 companies, 25 shortlisted jobs (JW0001-JW0025) and 45 people
  (PA0001-PA0045), all as "pipeline"; a second export wrote nothing; on a copy, a stage the owner set survived the
  next export and the job moved from Apply next to In progress.
- T2: the page runs at http://127.0.0.1:8787; checked in headless Edge on a copy (stage edit as "you", highest stage
  raised, refused conflicting save, polling, filters, dark mode at phone width).
- T4: on a copy, an agent added a company and updated a job through the CLI, owner-only writes were refused with
  exit 1, and a queued batch of 3 (one a duplicate) was accepted through the API: 2 added with the next C IDs, 1
  skipped. The `jobsearch-tracker` task starts the page at logon.

Deviations from the plan as written:

- `nightly.export` takes `store` (the default: write into the tracker) or `preview` (CSV preview only), not
  preview/real. `export --csv` writes the CSVs as a backup alongside.
- `ingest --file` stays as a one-off CSV path; the next ingest from the store undoes whatever it changed, so CSVs
  belong in the page's Import CSV, which writes as "you" and never blanks a value.
- Export found that research leads duplicating a watched posting reached the store; that's fixed, but one duplicate
  was already written (JW0015, same Motional posting as JW0010) for the owner to delete. Rows the owner deletes stay
  deleted.
- The page does CSV import and export on the server, so PapaParse is gone; Today is the first and default tab.
- The agent CLI refuses `--by you`: the owner edits in the page, so an agent can't borrow the owner's rights.
  Agents adding rows get the owner-style next ID (J037, C1220), as the old tracker's agent rules had it; only the
  pipeline uses JW#### and PA####.
- Queued batches are checked when queued (only fields an agent may set on a new company), not when the owner
  adds them.
- `tools/register_tasks.ps1` takes `-Only nightly|tracker`, so one task can be registered without the other.
- The claude.ai tracker itself was left untouched: it's frozen by no longer being used, and nothing was published
  to it.
