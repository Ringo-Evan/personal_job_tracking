# jobsearch

Background research pipeline for the job-search tracker. See `plan.md` for the full design and `plan-models.md` for
the model routing that overrides parts of it.
It researches companies, watches job boards, ranks jobs, writes one daily report, and writes CSVs you import into the
tracker. It never applies, contacts anyone, or writes to the tracker directly.

## Setup (Windows)

```powershell
py -3.13 -m venv .venv      # any Python >= 3.12
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m jobsearch status
.venv\Scripts\python -m pytest            # live tests are skipped; run them with -m live
.venv\Scripts\python -m ruff check .
```

Copy `.env.example` to `.env` and fill in the API keys you have. Start Ollama and SearXNG
(`docker compose up -d`), log in to `claude` and `codex` once, then check everything with
`python -m jobsearch doctor`. It never spends Claude or ChatGPT usage: it only checks that those CLIs are logged in.

## How it works

1. **Find.** `ingest` reads your tracker export. `triage` gives every "Not started" company a rough fit, stability
   and remote score from model knowledge alone. `detect` (from your careers URLs) and `discover` (one web search per
   company) find job boards; `watch` pulls matching postings from Greenhouse, Lever, Ashby, SmartRecruiters, Workday,
   Workable, Rippling, Breezy, Pinpoint and Recruitee boards. The title and location filters are wide on purpose:
   weak or unclear matches are ranked low, not dropped.
2. **Research companies.** `verify` works down a queue: your rank 1-3 first, then companies with a well-scored open
   job, then triage fit. For each company, code gathers an evidence pack (SearXNG search plus fetched pages), a model
   extracts facts with word-for-word quotes, code checks every quote against the page, and a second model judges fit
   from the checked facts only. Newly found job boards are confirmed with the board's API and fed to the watcher.
3. **Rank jobs.** `rank` scores each open posting against your profile (skill match, seniority, Maine eligibility
   read from the description, track). Priority = 2 x skill match + company fit, minus penalties (a Maine "no" sinks
   below every job you can take; junior roles lose 2). Ties go to the more stable employer, then the better pay.
4. **Report.** `digest` writes one document with everything worth acting on (below); `export` writes the import CSVs.
   `nightly` runs all of the above in order and ends with the report.

## The daily report

`out/<date>/digest.html` (open it in a browser; `digest.md` is the same text). From the top:

- **Alerts:** a stage that failed or stopped early, SearXNG down, too many rejected companies, unscored jobs.
- **At a glance:** open jobs, the shortlist, boards watched, companies researched, the last board check, model calls.
- **Apply next:** the best open jobs, each with why it fits, what to check first, Maine eligibility and where it
  comes from, pay, the company's research and red flags, who to approach (named people from public pages, plus a
  LinkedIn people-search link to open yourself) and a link to any cover-letter draft on file.
- **New since the last report**, then **the rest of the shortlist** (skill match 3+, Maine not ruled out).
- **Approach without an opening:** researched companies with fit 4+, Maine-eligible and stable, with the role to
  approach and an outreach angle.
- **Companies researched, people found, job boards** (new boards to sanity-check, boards to check by hand),
  **rejects and errors, run totals**, and how to import.
- A link at the top to the latest hand-reviewed materials (`out/*/recommendations/`: picks, resume suggestions,
  outreach and cover-letter drafts), which are written in chat, not by the pipeline.

"New" means new since the previous report, so a missed night loses nothing. Jobs you rule out stay out of the
report with `dismiss` (the tracker is never touched). A board that belongs to a different company goes away for good
with `discover --reject ID=type:token`.

## Models

Every runtime model call goes through one router (`src/jobsearch/router.py`). Each task tries the tiers listed under
`routing:` in `config.yaml`, in order, and moves to the next tier when a model is out of budget, rate-limited, down,
or gives an invalid answer twice.

| Task | Tier order | Notes |
|---|---|---|
| `triage` | DeepSeek, Gemini, Codex/Haiku, gemma4 | 1,070 companies cost $0.22 |
| `extract` (verify facts) | DeepSeek, Gemini, qwen3.5 | No profile. Escalates when >30% of quotes fail the check, or nothing is found in a rich pack |
| `judge` (verify fit) | DeepSeek, qwen3.5 | Sees checked facts only, never web text |
| `posting_relevance` (rank) | DeepSeek, qwen3.5 | |

| Tier | Endpoints now | Notes |
|---|---|---|
| `paid-cheap` | DeepSeek flash | Prepaid balance; 300 calls/day cap |
| `free-cloud` | Gemini 3.5 / 3.1 flash-lite | Free tier; prompts may be used for training |
| `subscription` | Codex (ChatGPT login), Claude Haiku (`claude -p`) | 30 calls/day each so they can't eat weekly limits; Codex is used only for triage |
| `local-large`, `local` | gemma4:12b, qwen3.5:9b on Ollama | gemma4 needs most of the PC's RAM; avoid daytime runs |

- Only local tiers see `prompts/profile.md`. Every other tier gets `prompts/profile-trimmed.md`: no name, no employers.
- Daily call counts, the response cache, and rate-limit backoffs live in `state/router.db`. Re-running the same
  prompt on the same model is free.
- To try a model, add an endpoint to `providers:` and its key to `.env`, then compare without saving anything:
  `python -m jobsearch verify --dry-run --model <endpoint> --only C158 --only C256 ...` (or `triage --dry-run`).

## Commands

| Command | What it does |
|---|---|
| `status` | Print the effective config and data-file counts |
| `doctor` | Check every model endpoint, SearXNG and the remaining daily budgets |
| `ingest` | Read the tracker's Companies CSV export from `inbox/` into `data/companies/` |
| `detect` | Find each company's job-board system from its career URL (no LLM) |
| `discover` | Find job boards by web search for companies without one (no LLM). `--limit N`; `--recheck-other` adopts boards earlier searches noted on newly supported systems, without searching; `--reject ID=type:token` rules out a wrong board for good |
| `watch` | Poll job-board APIs for new matching postings (no LLM). `--explain` lists title matches dropped by the location rules |
| `triage` | Score untriaged "Not started" companies. `--dry-run` saves nothing; `--report` shows the distribution |
| `verify` | Research the next companies in the queue (`verify.per_run`, `verify.max_minutes`). `--queue N` shows the queue; `--only ID` picks companies; `--force` ignores freshness; `--dry-run` saves nothing |
| `rank` | Score open jobs not yet scored or whose posting changed. `--rescore` redoes all |
| `export` | Write `out/<date>/companies_import.csv`, `jobs_import.csv`, `people_import.csv`: only rows and cells changed since the last export. `--dry-run` writes a preview to `out/<date>/preview/` and marks nothing |
| `digest` | Write the daily report, `out/<date>/digest.html` and `digest.md` (see above) |
| `dismiss` | Hide jobs you've ruled out from the report: `dismiss <posting URL or job key> --reason "..."`; `--undo` brings them back |
| `nightly` | Run ingest, detect, discover, watch, triage, verify, rank, export (preview) and digest in order, under a lock; a failed stage never stops the rest. `--watch-only` is the 6-hourly job (watch + rank). Stage output: `logs/nightly-<date>.txt` |

Run any command as `.venv\Scripts\python -m jobsearch <command> --help` for its options. Every finished run appends a
line to `logs/runs.jsonl`. All commands are safe to kill and rerun: finished work is saved per company or job.

## What gets written to the tracker

- **Companies:** only pipeline-owned columns (plan section 5.3). Your rank, preference scores, dealbreakers and notes
  are never exported. A cell is filled only when its value changed since the last export, so your hand edits of a
  pipeline column survive until the pipeline's own value changes. The careers URL is only filled in when yours is
  blank. Research status is left alone when you moved it past "Initial research" yourself.
- **Jobs:** new `JW####` rows for board postings (`Verified live`) and openings found by research
  (`Indexed lead - verify`). Track, stage and next action are set on the first export only; stages are never touched
  after that. Skill match and fit notes come from `rank`. A removed posting is exported once as
  `Historical lead - recheck`.
- **People:** new `PA####` rows, only named engineering or QA leaders found on public pages, with name, title,
  company and source URL. Never LinkedIn; use the company's "People search link" to look them up yourself.

## Runbook

**Weekly, or after big tracker edits:** in the tracker, open the Companies tab, clear the search and all filters,
click Export CSV and save the file to `inbox/`. The next nightly run reads it (or run `ingest` yourself).

**Scheduling:** two Windows tasks run as you while you're logged in: `jobsearch-nightly` (02:30 daily) and
`jobsearch-watch` (every 6 hours). They're registered on this PC; `powershell -ExecutionPolicy Bypass -File
tools\register_tasks.ps1` registers them again, which you need after moving the repo folder (the tasks hold its
path). Missed runs catch up when the PC is back on. Keep Docker Desktop running for SearXNG; if it's down, the report
says so and the search stages wait for the next night.

```powershell
Get-ScheduledTaskInfo jobsearch-nightly    # last result and next run time (same for jobsearch-watch)
Disable-ScheduledTask jobsearch-nightly    # pause; Enable-ScheduledTask to resume
```

The script's header shows how to remove them.

**Daily (about 10 minutes):**

1. Open `out/<today>/digest.html`. Deal with any alerts first.
2. Work down **Apply next**. Rule out what doesn't fit with `dismiss <posting URL> --reason "..."` so it stops
   coming back, and mark it Withdrawn or Closed in the tracker yourself; the pipeline never changes a job's stage.
3. When you want the tracker updated: `.venv\Scripts\python -m jobsearch export`, then import
   `companies_import.csv`, `jobs_import.csv` and `people_import.csv` from `out/<today>/` on their tabs. The nightly
   run only writes a preview (`out/<date>/preview/`) and marks nothing as sent, so skipping a day never loses
   changes: the next export carries everything since the last one you ran. (Set `nightly.export: real` in
   `config.yaml` to export every night instead; then every night's files must be imported, in order.)

**Without the scheduler:** `.venv\Scripts\python -m jobsearch nightly` does the same by hand (2-3 hours, most of it
`verify`).
