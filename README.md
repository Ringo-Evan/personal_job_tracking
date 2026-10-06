# jobsearch

Background research pipeline for the job-search tracker. See `plan.md` for the full design and `plan-models.md` for
the model routing that overrides parts of it.
It researches companies, watches job boards, ranks jobs, and writes CSVs you import into the tracker. It never
applies, contacts anyone, or writes to the tracker directly.

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
   and remote score from model knowledge alone. `detect` and `watch` find job boards and pull matching postings
   (wide title filter on purpose: weak matches are ranked low, not dropped).
2. **Research companies.** `verify` works down a queue: your rank 1-3 first, then companies with a well-scored open
   job, then triage fit. For each company, code gathers an evidence pack (SearXNG search plus fetched pages), a model
   extracts facts with word-for-word quotes, code checks every quote against the page, and a second model judges fit
   from the checked facts only. Newly found job boards are confirmed with the board's API and fed to the watcher.
3. **Rank jobs.** `rank` scores each open posting against your profile (skill match, seniority, Maine eligibility
   read from the description, track). Priority = 2 x skill match + company fit, minus penalties (a Maine "no" sinks
   below every job you can take; junior roles lose 2).
4. **Export.** `export` writes the import CSVs; `digest` writes the daily summary.

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
| `watch` | Poll job-board APIs for new matching postings (no LLM) |
| `triage` | Score untriaged "Not started" companies. `--dry-run` saves nothing; `--report` shows the distribution |
| `verify` | Research the next companies in the queue (`verify.per_run`, `verify.max_minutes`). `--queue N` shows the queue; `--only ID` picks companies; `--force` ignores freshness; `--dry-run` saves nothing |
| `rank` | Score open jobs not yet scored or whose posting changed. `--rescore` redoes all |
| `export` | Write `out/<date>/companies_import.csv`, `jobs_import.csv`, `people_import.csv`: only rows and cells changed since the last export. `--dry-run` writes a preview to `out/<date>/preview/` and marks nothing |
| `digest` | Write `out/<date>/digest.md`: ranked new jobs, best open jobs, companies verified, people, boards to check by hand, rejects, errors, run totals |
| `nightly` | Not built yet (phase 7) |

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
click Export CSV and save the file to `inbox/`. Then run `ingest`.

**Daily (until phase 7 schedules it):**

```powershell
.venv\Scripts\python -m jobsearch watch
.venv\Scripts\python -m jobsearch verify
.venv\Scripts\python -m jobsearch rank
.venv\Scripts\python -m jobsearch export
.venv\Scripts\python -m jobsearch digest
```

Then read `out/<today>/digest.md`, and in the tracker import each CSV from `out/<today>/` on its tab (Companies, Jobs,
People). Apply from the top of the ranked job list. Mark bad fits Withdrawn or Closed yourself; the pipeline never
changes a job's stage.

Full runbook (scheduling, pausing, alerts) comes in phases 7-8.
