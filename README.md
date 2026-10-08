# jobsearch

Your job-search tracker and the research pipeline that fills it. You work in one page on this PC,
http://127.0.0.1:8787: the daily report, your jobs and their stages, the companies you're tracking, the people to
approach, your activity log and your materials. Every night the pipeline researches those companies, reads their job
boards for QA and test openings you could take from Maine, scores the openings against your profile, and writes what
it found straight into the tracker, with a fresh report on top.

It never applies to jobs, contacts anyone or touches LinkedIn, and it can't change your ranks, stages, notes,
activity or materials. Those stay with you.

This replaced the claude.ai Job Search Tracker on 2026-10-07, which is kept as a frozen snapshot (see
[The tracker](#the-tracker)). `plan.md` is the original design; `plan-models.md` overrides it on which AI models do
what, and `plan-tracker.md` on everything about the tracker. This README describes the system as it is built today.

**Contents.** [Part 1: the simple version](#part-1-the-simple-version): what it does, your routine, where things live.
[Part 2: nuts and bolts](#part-2-nuts-and-bolts): the nightly run, each stage, the tracker, models, safety rules, what
the pipeline writes, data files. [Part 3: reference](#part-3-reference): every command and option, every setting,
scheduling, setup, development, troubleshooting.

## Part 1: The simple version

### What it does

```
  you ──► the tracker page, http://127.0.0.1:8787 ──► data/tracker/ (one file per row)
                                                           │     ▲
                                every night at 02:30       ▼     │
  ┌─────────────────────────────────────────────────────────────────────────────┐
  │ 1 ingest    read your companies, ranks and statuses from the tracker        │
  │ 2 detect    find each company's job board from its careers page             │
  │ 3 discover  ...or by web search                                             │
  │ 4 watch     read every board for QA and test jobs you could take from Maine │
  │ 5 triage    AI: a quick screen of companies you haven't researched          │
  │ 6 verify    AI: research companies, every fact checked against its page     │
  │ 7 rank      AI: score each job against your profile                         │
  │ 8 export    write research, shortlisted jobs and people into the tracker    │
  │ 9 digest    write the report you read on the page's Today tab               │
  └─────────────────────────────────────────────────────────────────────────────┘
```

- **One place.** The tracker page is where you read the report, track applications, rank companies and log what
  happened. The nightly writes into the same tracker, and the page shows its changes within seconds: there's nothing
  to import or export.
- **The companies are yours.** It works only on the companies in your tracker and never adds employers on its own
  (agents can queue some for you to add). Companies you park, or rank 4 or worse, are left alone entirely.
- **You keep control.** The pipeline and agents can't change your ranks, preference scores, notes, job stages,
  application details, Activity or Materials: the tracker refuses the write. Every change, by anyone, is logged with
  its old value.
- **The jobs come straight from each company's job board** (Greenhouse, Lever, Ashby, Workday and six more), read
  through the boards' public APIs, so they're current. The filters are wide on purpose: an unclear match is kept and
  ranked low in the report, and only the shortlist reaches your Jobs tab.
- **AI is used only for judgment calls:** screening companies, researching them and scoring jobs. Finding boards and
  postings is plain code. Most calls go to a cheap paid model (DeepSeek, cents a night), with free and local models
  as backups.
- **Research is checked, not trusted.** Code collects the web pages, a model pulls out facts with word-for-word
  quotes, code checks every quote against its page, and a second model judges fit from the checked facts alone.

### Your routine

**Every day (about 10 minutes):**

1. Open http://127.0.0.1:8787; the page starts when you log in. The **Today** tab is the report: deal with any alerts
   at the top first.
2. Work down **Apply next**; each card shows the job's tracker ID. When you apply, open the job on the **Jobs** tab
   and set its stage to Applied, with the date and a follow-up date: it leaves Apply next and shows under
   **In progress**, flagged when the follow-up comes due. For a job you don't want, set its stage to Withdrawn or
   Closed. A job that isn't in the tracker (not on the shortlist) leaves the report with
   `.venv\Scripts\python -m jobsearch dismiss <posting URL>`.
3. Log calls and interviews on the **Activity** tab, and each resume or cover-letter version on **Materials**.

There are no CSVs to import or export and no tracker export to drop anywhere: the nightly reads your ranks and stages
from the tracker and writes its findings back. To add companies, use **Add** on the Companies tab, or have an agent
queue them for you (the **Agent guide** tab has the briefs).

Everything runs on its own while you're logged in to this PC: the page from logon, the pipeline at 02:30. Keep Docker
Desktop running: it hosts the search engine that research uses. No report in the morning? See
[Troubleshooting](#troubleshooting).

### Where things live

| Folder or file | What's in it |
|---|---|
| http://127.0.0.1:8787 | The tracker page: Today's report, Jobs, Companies, People, Activity, Materials, Agent guide |
| `data/tracker/` | The tracker itself: one JSON file per row, and `history.jsonl` with every change and its old value |
| `out/<date>/` | Each night's report (the Today tab shows the newest) |
| `data/` (the rest) | The pipeline's working records: one JSON file per company, job posting and person it found |
| `logs/` | What each run did: `nightly-<date>.txt` for each night, `runs.jsonl` with one line per command |
| `config.yaml` | Settings: filters, budgets, which models do what |
| `.env` | API keys, never committed |
| `prompts/` | The instructions and the profile the AI models see |
| `state/`, `cache/` | Bookkeeping: model budgets and answer cache, the last report's snapshot, cached web pages |
| `src/jobsearch/`, `tests/`, `tools/` | The code (the page is `src/jobsearch/web/tracker.html`), its tests, and the scheduling and fixture scripts |

## Part 2: Nuts and bolts

### The nightly run

`python -m jobsearch nightly` runs nine stages in order. Each stage runs as its own process
(`python -m jobsearch <stage>`), so a crash or a hang in one can't take the others down, and a failed stage never
stops the run. All their output goes to `logs/nightly-<date>.txt`.

| # | Stage | What it does | AI | Web search | Time limit | Usual time |
|---|---|---|---|---|---|---|
| 1 | `ingest` | Reads your companies, ranks and statuses from the tracker | | | 10 min | seconds |
| 2 | `detect` | Finds job boards from careers URLs | | | 1 h | seconds to minutes |
| 3 | `discover` | Finds job boards by web search, up to 60 companies | | yes | 1 h | about 7 min |
| 4 | `watch` | Reads every job board | | | 2 h | about 45 min |
| 5 | `triage` | Screens companies you haven't researched | yes | | 1 h | minutes |
| 6 | `verify` | Researches the next companies in the queue | yes | yes | 2 h 15 min | about 30 min for 40 companies |
| 7 | `rank` | Scores new and changed jobs, up to 60 | yes | | 1 h | 1 to 5 min |
| 8 | `export` | Writes research, shortlisted jobs and people into the tracker | | | 15 min | under a minute |
| 9 | `digest` | Writes the report | | | 10 min | seconds |

The first full run, on 2026-10-07, took 85 minutes: 47 for watch, 29 for verify (it reached its 40-company cap) and 7
for discover.

- **Search down.** The run first asks the local search engine (SearXNG) whether it's up. If not, `discover` and
  `verify` are skipped and the report's alerts say how to start it.
- **Time limits.** A stage that runs past its limit is stopped and reported as an alert, and the run moves on. The
  scheduled task's own 10-hour limit stays above the lock wait plus every stage's limit (a test checks this), so a
  slow night still reaches the report.
- **One run at a time.** Runs share a lock, `state/nightly.lock`, which Windows frees if the process dies, so it never
  goes stale. If another run holds it, the nightly waits up to an hour, checking every minute, then gives up;
  `nightly --watch-only` exits at once instead. Either way the day's log says so. A nightly that gave up also leaves
  an alert for the next report and exits with code 1, so Task Scheduler shows it failed.
- **Run log.** Just before the report is written, the run adds a line to `logs/runs.jsonl` with each stage's exit
  code and minutes, plus its alerts, so the report can show them. Every command adds its own line too.
- **Exit code.** 0 once every stage has been tried, even if some failed; failures are alerts in the report.

### Stage by stage

#### 1. ingest: read your tracker

- Refreshes the pipeline's copy of every company (the `tracker` block of `data/companies/<ID>.json`) from the
  tracker's rows in `data/tracker/companies/`, so the night works from your latest ranks, statuses and careers URLs.
- It notes when you change a company's research status or your overall rank, so export never overwrites your
  decisions.
- A company you delete from the tracker is marked `missing_from_tracker` and left alone by every stage until it's
  back. Its file is never deleted.
- If the tracker holds fewer than half the companies the pipeline knows, ingest refuses rather than mark the rest
  missing: the folder is probably damaged or half restored.
- `ingest --file <export.csv>` still reads a tracker CSV export into the pipeline's copy, as a one-off. The next
  ingest from the tracker undoes whatever differs, so to bring a spreadsheet into the tracker, use **Import CSV** on
  the page.

**Companies you've ruled out.** Research status `Parked`, or your overall rank 4 or worse, means the pipeline leaves
the company alone: no board search, no watching, no research, no job scoring, no export.

#### 2. detect: job boards from careers URLs (no AI)

For each company with a careers URL in the tracker:

1. Looks for a job-board address in the URL and the company's tracker text (`job-boards.greenhouse.io/acme`).
2. Fetches the careers page and looks for a board link or an embedded board. If there's none, it follows one "see
   all jobs" link on the same site and looks there.
3. If the page names a board system without linking to it, it guesses board names from the company's name and domain.
4. Adopts a board only after the board's own API confirms it. A Greenhouse board's name must also match the company.
5. Otherwise it records `other` with the system it saw, one the watcher can't read (iCIMS, Jobvite, BambooHR,
   Teamtailor, JazzHR, Paylocity, UKG, ADP, Taleo, SuccessFactors, Oracle, Dayforce, Dover, Gem, Wellfound), or
   `unknown` when the page was blocked or failed.

Supported board systems: Greenhouse, Lever, Ashby, SmartRecruiters, Workday, Workable, Rippling, Breezy, Pinpoint and
Recruitee. Companies without a confirmed board are checked again every `detect.recheck_days` (30); confirmed boards
only when you change the careers URL. Boards that research found are left to research.

#### 3. discover: job boards by web search (no AI)

Most tracker companies have no careers URL, so detect can't place them. For each company still without a confirmed
board, best triage fit first, discover runs one search through SearXNG, `"<company>" careers` (with the name
shortened, so "Roll20 / Demiplane" searches for "Roll20"), and then:

1. takes job-board links from the results whose board name resembles the company's;
2. scans the company's own careers page, when a result is one, the way detect does;
3. guesses board names from the company name: Greenhouse's API reports the board's name to check against, and a
   guessed Lever or Ashby board is kept only if its postings mention the company.

- A board is adopted only when its API confirms it. Workable, Rippling, Breezy and Recruitee also report a company
  name, which must match.
- Job aggregators and review sites (Indeed, Glassdoor, ZipRecruiter and the like) are never taken for a careers page.
- Boards on systems the watcher can't read are noted in the company record. `discover --recheck-other` adopts the
  noted boards whose system has become supported since, without searching again.
- **Namesakes.** When a board turns out to belong to a different company, rule it out for good with
  `discover --reject C991=breezy:sense-engineering`. It's removed if it was adopted, and no stage adopts it again.
- The nightly searches at most `nightly.discover_limit` companies (60), 4 seconds apart, to spare the search engines.
  A company is searched again after `detect.recheck_days` (30). Searches carry only the company name.

#### 4. watch: read every job board (no AI)

Reads every confirmed board of a supported system through its public API: about 470 boards and 22,000 postings in
October 2026, some 2,700 requests and 45 minutes. Board reads skip the cache, so each run sees the live board.
Workday's API is unofficial, so it's read only while `watch.workday_enabled` is on, with one search per term in
`watch.workday_terms`. Greenhouse, Workday, Rippling and Breezy fetch a posting's full text only when its title
matches.

**Title filter.** A posting stays when its title matches `filters.title_include` (QA, quality, test, SDET,
verification, validation, automation, reliability, release and more) and doesn't match `filters.title_exclude`
(intern, junior, inspector, supplier quality, manufacturing quality and more).

**Location rules.** Each remaining posting gets a Maine verdict from its location and description:

| Verdict | When |
|---|---|
| eligible | The location names the Maine area (`filters.maine_terms`, or "ME"), or the job is remote with no place beyond the US ("Remote", "Remote - US", "Anywhere"); and the description doesn't list eligible states without Maine |
| unknown, kept for you to check | Remote but tied to another state; a broad region (Americas, Global, Worldwide); no location at all; just "United States"; a US city whose description says the job is remote; a remote job whose description lists eligible states without Maine |
| dropped | Everything else: on-site or hybrid away from Maine, or outside the US |

`watch --explain` lists the title matches the location rules dropped, so you can check the rules aren't too tight.

**What's stored.** Each kept posting is `data/jobs/<board>-<id>.json`: title, location, links, pay (from the board's
fields, or read from the description), the description (up to 6,000 characters, beginning and end, because
eligible-state lists and pay tend to sit at the end), the Maine verdict and its reason, a track from the title (QA
leadership, SDE pivot or Core QA), and when it was first and last seen. Each run refreshes the live fields. A posting
missing from 2 runs in a row is marked closed, and reopened if it comes back.

**Board health.** `data/watch_state.json` keeps each board's last success, last error and consecutive failures. A
board that fails is listed under errors in the report; one that keeps returning HTTP 404 has usually moved.

#### 5. triage: screen companies (AI, no web)

- Takes companies whose research status is `Not started` and that have no triage yet, 20 per model call.
- From the model's general knowledge it rates fit (1-5), stability in a downturn (1-5), how likely the company is to
  hire remotely from Maine (High, Medium, Low, Unknown) and size, and suggests likely roles, who to approach, why it
  might fit and red flags.
- The result orders the discover and verify queues and fills the tracker's screening columns. Research replaces it
  later.
- Every answer is checked against a schema; companies missing from an answer get one retry, in batches of 5. Safe to
  stop and rerun at any time.

#### 6. verify: research companies (AI plus checked web evidence)

**The queue** (`verify --queue 20` prints it): your overall rank 1-3 first, best first; then companies with an open
job scored skill 3 or more and not ruled out for Maine; then by triage fit, stability and remote likelihood, with
untriaged companies last. Skipped: parked or ranked 4 or worse, missing from the tracker, researched within
`verify.refresh_days` (45) or marked Researched by you within that time, and companies rejected twice in a row.

**For each company:**

1. **Evidence pack (code).** Four searches (careers; remote work policy; layoffs, funding or acquisitions in
   2025-2026; the engineering or QA team), plus the known careers page and job-board data. Pages are fetched and cut
   down to their main text. Social media, LinkedIn, job aggregators, review sites, documents and images are skipped.
   At most `evidence.max_pages` (8) pages, `max_chars_per_page` (6,000) each and `max_total_chars` (30,000) in all.
   Packs are cached in `cache/evidence/`.
2. **Extract (model, without your profile).** Pulls out facts: careers URL, job board, remote policy and Maine
   eligibility, size, stability signals (layoffs, funding, revenue, acquisitions, growth), QA openings, and named
   engineering or QA leaders. Each fact names its source and quotes it word for word.
3. **Grounding check (code).** Every quote must appear in its source. Differences in case, spacing and punctuation
   style are forgiven; a paraphrase is not. Facts with an email address, phone number or LinkedIn link are dropped,
   and people need a full name and a leadership title (5 at most). If more than 30% of the facts fail, or a rich pack
   (2,000+ characters) yields nothing, the next model tier tries instead.
4. **Judge (model, with your profile).** Sees only the checked facts, never the web text. It rates fit, stability
   and hiring difficulty, and suggests role families, the role to approach, an outreach angle and red flags. A
   low-confidence verdict on a company that may hire from Maine is retried on the next tier; if no tier is more
   confident, the verdict stands.
5. **Validate (code).** The combined result must pass a strict schema: every URL it cites must be one of its sources,
   and no email address, phone number or LinkedIn reference may appear anywhere.

A verified company's research is saved in its `research` block, with the models used and the grounding numbers. A job
board found along the way is adopted only if its API confirms it and the company has no confirmed board yet. A
company that fails is saved to `data/rejects/<ID>-<time>.json` for review; after two failures in a row it leaves the
queue (`verify --only <ID> --force` tries again).

A run stops cleanly after `verify.per_run` companies (40), after `verify.max_minutes` (120), or when every model tier
is out of budget. The rest wait for the next night.

#### 7. rank: score jobs (AI)

- Scores each open job, at a company you haven't ruled out, that hasn't been scored yet or whose posting text
  changed. Jobs at the best-fit companies go first, at most `rank.per_run` (60) per run; the rest wait for the next
  run. `--rescore` redoes them all.
- Openings found by research have no description, so their page is fetched (politely, and cached).
- One model call per job, with your profile (the trimmed one, except on local models) and up to `rank.max_chars`
  (6,000) of the posting, returns a short why, skill match (1-5), seniority (below, match or above you), Maine
  eligibility as the text states it, a track (Core QA, QA leadership, SDE pivot, Domain pivot or Exploratory) and a
  note.
- **Maine verdict.** The watcher's location rules and the score combine: a "no" from the posting text always wins; a
  state list without Maine that the watcher saw keeps it "unknown" even if the model says yes; otherwise a "yes" from
  either makes it yes.
- **Priority** orders the report: 2 × skill match + company fit (the research fit, else the triage fit), then Maine
  "no" −15 (below every job you can take), Maine unknown −1, a junior role (seniority below you) −2, closed −30. A
  missing score or fit counts as 2.5. Ties go to the more stable employer, then the higher pay.

#### 8. export: into the tracker

Writes what the pipeline found into the tracker as the writer `pipeline`, through the same code that enforces field
ownership for every writer, and only into the fields the pipeline owns (see [The tracker](#the-tracker)):

- **Companies:** the research, screening and outreach columns of companies already in the tracker. The pipeline never
  creates a company.
- **Jobs:** open jobs on the shortlist (skill match 3+, Maine not ruled out, not dismissed), plus every job already in
  the tracker, which keeps getting updates. A new one gets a `JW####` ID and starts at stage Lead. The rest stay in
  the report, ranked low.
- **People:** named engineering and QA leaders, as `PA####` rows.
- Only cells whose value changed since the last export are written, so an edit you make to one of the pipeline's
  columns stays until the pipeline's own value changes. A row you delete in the tracker stays deleted. A row the
  tracker refuses is listed under the report's errors and tried again the next night.
- Before writing, research openings that no watched board already has become `data/jobs/lead-*.json` (if their
  titles pass the title filter), and named leaders become `data/people/*.json`.

`export --dry-run` changes nothing and writes a CSV preview to `out/<date>/preview/` instead; `export --csv` also
writes the CSVs as a backup. `nightly.export: preview` makes the nightly do the dry run. What each column gets is
under [What the pipeline writes into the tracker](#what-the-pipeline-writes-into-the-tracker).

#### 9. digest: the report

`out/<date>/digest.html` (and `digest.md`, the same text) is built from local files only: no web, no models. The
page's **Today** tab shows the newest one. From the top:

- A link to the latest hand-reviewed materials in `out/*/recommendations/` (picks, resume suggestions, outreach and
  cover-letter drafts). Those are written in chat sessions, not by the pipeline.
- **Alerts:** a stage that failed, timed out or stopped early; search down; a nightly that couldn't start; research
  rejecting more than 15% of companies; new jobs not scored yet; follow-ups overdue or due today.
- **At a glance:** open jobs, the shortlist, jobs in progress and follow-ups due, boards watched, companies
  researched, the last board check, model calls.
- **In progress:** every job you've moved past Lead and Verified (Applied, Screen, Interview, Offer), yours or the
  pipeline's, with its stage, next action, date applied and follow-up date, flagged Overdue, Today or Soon.
- **Apply next:** the best `nightly.report_top` (8) shortlisted jobs still at Lead or Verified. Each has its tracker
  ID, why it fits, what to check first, Maine eligibility and where that comes from, pay, the company's research and
  red flags, who to approach (named people from public pages, plus a LinkedIn people-search link to open yourself) and
  a link to any cover-letter draft on file.
- **New since the last report**, then **the rest of the shortlist** (skill match 3+, Maine not ruled out).
- **Approach without an opening:** researched companies with fit 4+, stability 3+ and a remote policy open to Maine
  that have no shortlisted job, with the role to approach and an outreach angle.
- **Companies researched**, **people found**, **job boards** (new boards being watched, worth a glance at the names,
  and boards to check by hand), **rejects and errors**, and **run totals**.

Jobs at Accepted, Rejected, Withdrawn or Closed drop out of the report. "New" means new since the previous report:
each report saves the open jobs and people it saw (`state/report_state.json`), so a missed night loses nothing. Jobs
you dismissed stay out (`data/dismissed.json`). The digest also writes `new_companies.md`, which stays a placeholder:
research doesn't collect other employers yet.

### The tracker

**Rows.** `data/tracker/<tab>/<ID>.json`: one flat JSON document per row, with the fields the claude.ai tracker had,
plus `updated_by` and `updated_at`. Jobs, people and activity also carry `company_id`, so renaming a company doesn't
orphan them. Rows you or an agent add get `J###`, `C###`, `P###`, `A###` or `R###` (the next number for that prefix);
the pipeline's jobs and people get `JW####` and `PA####` from the counters in `data/ids.json`. An ID is never reused.

**Who may change what.** Every write, from the page, the pipeline or an agent, goes through one function
(`Tracker.write` in `src/jobsearch/tracker.py`), which enforces three tiers:

| Tier | Fields | Who may write them |
|---|---|---|
| Agent-maintained | Companies' research, screening and outreach columns; jobs' listing, location, pay and fit columns; people's name, title, company, source URL and why approach | Anyone |
| Set when the row is added | A job's track, stage (Lead), highest stage (None), next action and date found; a person's status (To research), connection (Cold) and next step; a company's name, sector and interests | Agents only when adding the row; you any time |
| Yours | Everything else: ranks, preference scores, dealbreakers and your notes; a job's stage after it's added and every application, materials and interview field; a person's status after it's added, notes and contact dates; all of Activity and Materials | You only |

The writer `you` is you; any other name is an agent, and the nightly writes as `pipeline`. Agents can't delete rows
and can't blank a field you filled in. A job's highest stage follows its stage forward on its own.

**Edits never collide silently.** When you save in the drawer, the page sends only the fields you changed, with the
values you started from. If one of them changed meanwhile (the nightly, an agent, another browser tab), the save is
refused with "Changed elsewhere" and the row reloads. Changes to other fields of the same row go through, so the
nightly can update a job's pay while you change its stage.

**History.** Every change is appended to `data/tracker/history.jsonl` with who, when, and the old and new values; a
deleted row is kept there whole. `data/` is in git too.

**The page.** `python -m jobsearch serve`, started at logon by the `jobsearch-tracker` task, serves the page and a
small API at http://127.0.0.1:8787. It's a port of the claude.ai tracker's page: header totals, the stage strip (click
a count to filter the Jobs tab), tabs with counts, search, sort, track and sector filters, the edit drawer, due flags
(Overdue, Today, Soon), "by <name>" under rows an agent wrote last, light and dark mode, phone width. It checks for
changes every 5 seconds. **Import CSV** merges a tracker CSV into the tab by ID, and a blank cell never overwrites;
**Export CSV** writes every row of the tab, whatever the search and filters show.

**This PC only.** The server listens on 127.0.0.1, refuses any request that doesn't name 127.0.0.1:8787 or
localhost:8787 as its host, and refuses writes that lack the page's own header or come from another website. It sends
no CORS headers, so no other site open in your browser can read or change the tracker.

**Agents.** Claude Code, Codex and scripts follow `AGENTS.md`: they write only through
`python -m jobsearch tracker add|update|queue-companies ... --by "<agent name>"`, and a refused write changes nothing
and exits 1. The page's Agent guide tab has briefs to paste.

**The queue.** An agent adding more than a few companies queues them (`tracker queue-companies`) instead. The
Companies tab then shows "N companies are waiting to be added" with **Add them**: each one gets the next company ID
and research status Not started, a name already in the tracker is skipped, and if adding stops halfway, clicking
again carries on without adding anything twice.

**The claude.ai tracker** is a frozen snapshot. Its rows and its page were saved to
`data/snapshots/claude-ai-tracker-2026-10-07/` and loaded with `tracker migrate`; it isn't edited any more.

### Models and the router

Every model call goes through one router, `src/jobsearch/router.py`. Each task has an order of tiers (`routing:` in
`config.yaml`), and each tier lists its endpoints (`providers:`).

| Task | Used by | Tier order now | Profile it sees |
|---|---|---|---|
| `triage` | triage | paid-cheap, free-cloud, subscription, local-large | trimmed (full on local tiers) |
| `extract` | verify | paid-cheap, free-cloud, local | none |
| `judge` | verify | paid-cheap, local | trimmed (full on local tiers) |
| `posting_relevance` | rank | paid-cheap, local | trimmed (full on local tiers) |

| Tier | Endpoints now | Notes |
|---|---|---|
| `paid-cheap` | DeepSeek flash | Prepaid balance; 300 calls a day. About 2 calls per researched company; triaging 1,070 companies cost $0.22 |
| `free-cloud` | Gemini 3.5 and 3.1 flash-lite | Free tier, 200 calls a day each. Free-tier prompts may be used for training |
| `subscription` | Codex (your ChatGPT login), Claude Haiku (your Claude Code login) | 30 calls a day each, so they can't eat your weekly limits. Codex is used only for triage |
| `local-large` | gemma4:12b on Ollama | Slow, and needs most of the PC's memory |
| `local` | qwen3.5:9b on Ollama | Fits in the graphics card's memory |

**How a call is routed:**

- Tiers are tried in order, and the endpoints within a tier in config order.
- An endpoint is skipped when its API key isn't set, its daily cap (`rpd`) is used up, or it answered "rate limited"
  (HTTP 429) today. `rpm` paces calls per minute; 0 means no limit.
- Every answer must match the task's JSON schema. A bad answer is retried once on the same endpoint with the error
  attached; a second bad answer moves on to the next tier. Providers that can't enforce a schema (DeepSeek, marked
  `json_mode: object`) get it in the prompt, and the router checks the answer either way.
- Some tasks add a quality gate (verify's grounding check, the judge's confidence). Failing it moves on to the next
  tier.
- Good answers are cached by task, model and prompt, so a rerun or a comparison never pays twice.
- When every tier is out, the stage stops cleanly and leaves its queue for the next run.
- Daily counts, the answer cache, escalations and benched endpoints live in `state/router.db`. `doctor` shows today's
  remaining budgets.

**Privacy.** Only the tiers in `profile_allowed_tiers` (the local ones) ever see `prompts/profile.md`, which has your
name and employers. Every other tier gets `prompts/profile-trimmed.md`, which has neither. Extraction sees no profile
at all.

**Claude and Codex.** Both run headless (`claude -p`, `codex exec`) from an empty temporary folder with a cleaned
environment (no API keys or other secrets), no settings, no MCP servers and no saved sessions. Codex can run
read-only shell commands, so it only gets triage, never web text.

**Trying another model.** Add an endpoint under `providers:` (and its key in `.env`), then compare without saving
anything: `verify --dry-run --model <endpoint> --only C158 --only C256`, or
`triage --dry-run --model <endpoint> --limit 40`. Results go to `logs/`.

### Safety rules

- **Hands off.** It never applies or contacts anyone. In the tracker it writes only its own fields: your ranks,
  stages, notes, application details, Activity and Materials are refused to it, and so is deleting rows.
- **This PC only.** The tracker page answers only here (see [The tracker](#the-tracker)).
- **No LinkedIn.** The HTTP client refuses linkedin.com outright. People come only from public pages (team and
  leadership pages, press releases, talks, engineering blogs, job posts); the report gives you a LinkedIn search link
  to open yourself.
- **Polite to websites.** At most one request per second per site (`http.min_interval_s_per_host`); a User-Agent that
  names the project and your contact address (`contact_email`), never a fake browser one; retries with backoff on
  rate limits and server errors; a 6-hour cache (`http.cache_hours`).
- **Web text is data, not instructions.** Prompts fence it off and say so, the models have no tools (Codex has a
  read-only shell and never gets web text), and every answer is checked against a schema.
- **Personal data.** People are stored with name, title, company and source URL only: no email addresses, phone
  numbers or personal accounts.
- **Secrets** stay in `.env`, which git ignores, and never go into logs or prompts.
- **Your full profile** goes only to models running on this PC.

### What the pipeline writes into the tracker

- **Companies:** only the pipeline's columns: careers URL (only when yours is blank), role families, hiring
  difficulty, estimate confidence and rationale, research notes, research status, Maine/remote eligibility, known
  benefit or growth source, research date, next research action, fit and stability scores, remote likelihood, size,
  why it might fit, red flags, screened-on date, who to approach, people search link and outreach angle.
- **Research status** becomes "Initial research" after triage and "Researched" after verify. It never overwrites
  Researching, Researched or Parked when you set them after its last write, and never moves those back to "Initial
  research".
- **Jobs:** new `JW####` rows for shortlisted jobs, both board postings (listing evidence `Verified live`) and
  openings research found (`Indexed lead - verify`). Track, stage Lead, highest stage None, next action ("Review and
  apply", or "Verify posting is live, then apply" for a research lead) and date discovered are set once. After that it updates only the
  listing, location, pay, skill match, fit notes, Maine eligibility and last-checked date. A removed posting is
  written once as `Historical lead - recheck`, its fit notes starting "Posting removed <date>."
- **People:** new `PA####` rows for named engineering or QA leaders found on public pages: name, title, company and
  source URL, with status "To research", relationship "Cold" and next step "Look up on LinkedIn (People search
  link)".
- A cell is written only when the pipeline's value changed since its last write, so your edit of one of its columns
  stays until its own value changes.

### Data files

| Path | What it holds |
|---|---|
| `data/tracker/<tab>/<ID>.json` | The tracker's rows: jobs, companies, people, activity, materials |
| `data/tracker/history.jsonl` | Every change to the tracker: who, when, old and new values |
| `data/tracker/imports/<ID>.json` | Companies agents queued, and how each batch was added |
| `data/tracker/meta/schema.json` | The claude.ai tracker's agent rules, kept for reference |
| `data/tracker/write.lock` | Held during each write (not in git) |
| `data/snapshots/claude-ai-tracker-2026-10-07/` | The frozen claude.ai tracker: its rows, and its page as `page.html` |
| `data/companies/<ID>.json` | The pipeline's working record per company: `tracker` (its copy of your row, refreshed by ingest), `ats` (job board: system, token, board URL, how it was found, whether it's confirmed), `discovery` (the last web search: careers page, boards seen, boards ruled out), `triage`, `research` (with the models used and grounding numbers), `history`, `exported` (what it last wrote into the tracker), `owner_changes`, `verify_rejects`, `missing_from_tracker` |
| `data/jobs/<board>-<id>.json` | One per job-board posting, open or closed, with its `score` once ranked and its `job_id` once it's in the tracker |
| `data/jobs/lead-*.json` | Openings research found that aren't on a watched board |
| `data/people/*.json` | Named leaders found by research |
| `data/rejects/` | Research that failed validation, kept for review |
| `data/dismissed.json` | Jobs you dismissed from the report, with reasons |
| `data/ids.json` | Counters for the pipeline's `JW` and `PA` IDs |
| `data/ingest_state.json` | Tracker CSVs already read with `ingest --file` |
| `data/watch_state.json` | Each board's health and the last watch run |
| `state/router.db` | Model call counts, answer cache, escalations, benched endpoints |
| `state/report_state.json` | What the last report saw, for "new since the last report" |
| `state/nightly.lock` | The nightly's run lock |
| `cache/http/`, `cache/evidence/` | Fetched web responses (6 hours) and evidence packs |
| `out/<date>/` | `digest.html`, `digest.md`, `new_companies.md`; `preview/` and the CSVs only when export writes them; `recommendations/` when written in chat |
| `logs/nightly-<date>.txt` | Every stage's output for that night |
| `logs/runs.jsonl` | One line per command run: counts, failures, alerts, duration |

`data/` and `logs/` are in git, so you can see how the tracker and the pipeline's knowledge changed. `out/`, `cache/`,
`state/`, `inbox/*.csv`, `.env` and lock files are not.

## Part 3: Reference

### Commands

Run them as `.venv\Scripts\python -m jobsearch <command> [options]`; `--help` after any command lists its options.
Repeatable options take one value each time (`--only C158 --only C256`). The pipeline's commands add a line to
`logs/runs.jsonl` and are safe to stop and rerun: finished work is saved per company or job as it goes.

**`nightly`**: every stage in order, then the report (see [The nightly run](#the-nightly-run)).

| Option | Default | What it does |
|---|---|---|
| `--watch-only` | off | Only watch the boards and score new postings. Exits at once if another run is going |

**`serve`**: the tracker page at http://127.0.0.1:8787, this PC only. The `jobsearch-tracker` task runs it from logon.

| Option | Default | What it does |
|---|---|---|
| `--port N` | 8787 | Port on 127.0.0.1 |
| `--data DIR` | `data/tracker` | Serve another tracker folder, for trying things out |
| `--open` | off | Open the page in your browser |

**`status`**: print the settings in effect and how many data files there are.

| Option | Default | What it does |
|---|---|---|
| `--config PATH` | `config.yaml` | Read a different config file |

**`doctor`**: check every model endpoint with a one-token call, SearXNG, and today's remaining budgets. For your
subscriptions it only checks that the Claude CLI is installed and that Codex is logged in, so no usage is spent.

| Option | Default | What it does |
|---|---|---|
| `--skip-models` | off | Only check SearXNG and the budgets |

**`ingest`**: refresh the pipeline's copy of every company from the tracker.

| Option | Default | What it does |
|---|---|---|
| `--file PATH` | | One-off: read this tracker CSV export into the pipeline instead (repeatable). The next ingest from the tracker undoes what differs; to bring a CSV into the tracker, use Import CSV on the page |
| `--force` | off | With `--file`: read files again even if already read |
| `--allow-partial` | off | With `--file`: accept an export with fewer than half the known companies |

**`detect`**: find job boards from careers URLs (no AI).

| Option | Default | What it does |
|---|---|---|
| `--only ID` | all due | Only these companies (repeatable) |
| `--limit N` | none | Stop after N companies |
| `--force` | off | Check again even confirmed boards |

**`discover`**: find job boards by web search (no AI), best triage fit first.

| Option | Default | What it does |
|---|---|---|
| `--only ID` | all due | Only these companies (repeatable) |
| `--limit N` | none (the nightly uses 60) | Stop after N companies |
| `--pause SECONDS` | 4.0 | Wait between searches, to spare the search engines |
| `--force` | off | Search again even if searched in the last 30 days |
| `--recheck-other` | off | No searching: adopt boards earlier searches noted on systems supported since |
| `--reject ID=type:token` | | Rule out a wrong board for good, e.g. `C991=breezy:sense-engineering`; removes it if adopted (repeatable) |

**`watch`**: read every job board for matching postings (no AI).

| Option | Default | What it does |
|---|---|---|
| `--only ID` | all boards | Only these companies' boards (repeatable) |
| `--quiet` | off | Leave out the per-board progress lines (the nightly uses this) |
| `--explain` | off | Also list title matches the location rules dropped |

**`triage`**: screen untriaged `Not started` companies.

| Option | Default | What it does |
|---|---|---|
| `--limit N` | all due | Triage at most N companies |
| `--only ID` | | Only these companies (repeatable) |
| `--report` | off | Only show the score distribution and a sample; no model calls |
| `--sample N` | 10 | How many random rationales the printed summary shows |
| `--dry-run` | off | Save nothing; triage even already-triaged companies and compare with what's stored |
| `--model ENDPOINT` | | Use only this configured endpoint, e.g. `gemini/gemini-3.5-flash-lite` |

**`verify`**: research the next companies in the queue.

| Option | Default | What it does |
|---|---|---|
| `--limit N` | `verify.per_run` | Research at most N companies (never more than `verify.per_run`) |
| `--only ID` | | Only these companies, in queue order (repeatable) |
| `--force` | off | With `--only`: research even if researched recently or rejected twice |
| `--dry-run` | off | Save nothing to `data/`; write the results to `logs/` for comparison |
| `--model ENDPOINT` | | Use only this endpoint for both extract and judge |
| `--queue N` | 0 | Only print the first N companies of the queue |

**`rank`**: score open jobs against your profile.

| Option | Default | What it does |
|---|---|---|
| `--limit N` | `rank.per_run` | Score at most N jobs (never more than `rank.per_run`) |
| `--rescore` | off | Score again even jobs whose posting text hasn't changed |
| `--model ENDPOINT` | | Use only this configured endpoint |

**`export`**: write the pipeline's research, shortlisted jobs and people into the tracker as `pipeline` (changed
cells only).

| Option | Default | What it does |
|---|---|---|
| `--dry-run` | off | Change nothing; write a CSV preview to `out/<date>/preview/` |
| `--csv` | off | Also write the CSVs to `out/<date>/` as a backup |

**`digest`**: write the report, `out/<date>/digest.html` and `digest.md`. No options.

**`dismiss`**: hide jobs you've ruled out from the report, for jobs that aren't in your tracker. For a job in the
tracker, set its stage to Withdrawn or Closed instead; `dismiss` never touches the tracker.

| Argument or option | Default | What it does |
|---|---|---|
| `JOB ...` | required | One or more posting URLs as the report links them, or job keys like `greenhouse-7421839` |
| `--reason TEXT` | empty | Why, for the record |
| `--undo` | off | Bring them back |

**`tracker`**: the tracker from the command line. Agents use these, and you use the page. Every write needs `--by`
with the agent's name (never `you`), and every command but `migrate` takes `--data DIR` to work on another tracker
folder. A refused write prints `refused: <why>` and exits 1.

| Command | What it does |
|---|---|
| `tracker list <tab> [--where key=value ...] [--json]` | List a tab's rows, optionally only those whose field equals a value |
| `tracker get <tab> <ID>` | Print one row as JSON |
| `tracker next-id <tab>` | The ID the next added row gets (J037, C1220, ...) |
| `tracker add <tab> key=value ... --by NAME [--id ID]` | Add a row as an agent: the next free ID unless `--id`; jobs start at stage Lead |
| `tracker update <tab> <ID> key=value ... --by NAME` | Change fields as an agent; a field it may not change refuses the whole update |
| `tracker queue-companies --file rows.json --label TEXT --by NAME` | Queue companies (a JSON list of {company, sector, interests, career_url, research_notes}) for you to add from the Companies tab |
| `tracker stats` | Rows per tab, and who last wrote them |
| `tracker migrate [--from DIR]` | Load the claude.ai tracker's downloaded rows; never overwrites a row already there (done on 2026-10-07) |

### config.yaml

Every setting, with its value now. Unknown keys are rejected, so a typo fails loudly; `status` prints the settings
in effect.

**General**

| Setting | Now | What it does |
|---|---|---|
| `contact_email` | your plus-address | Goes in the User-Agent of every web request, so site owners can reach you |
| `auth` | `claude-code` | `api-key` makes Claude calls use `ANTHROPIC_API_KEY` from `.env` instead of your login |
| `profile_allowed_tiers` | `[local, local-large]` | Tiers allowed to see your full profile; the rest get the trimmed one |
| `runtime` | `local` | Records where it runs; nothing reads it yet |
| `models.triage`, `models.verify` | Haiku, Sonnet | Left from the first design; nothing reads them now (the router decides) |

**`triage`, `verify`, `detect`**

| Setting | Now | What it does |
|---|---|---|
| `triage.batch_size` | 20 | Companies per triage call |
| `triage.timeout_s` | 900 | Seconds allowed per triage call (local models are slow) |
| `verify.per_run` | 40 | Most companies researched per run |
| `verify.max_minutes` | 120 | A research run stops starting new companies after this |
| `verify.timeout_s` | 360 | Seconds allowed per research model call |
| `verify.refresh_days` | 45 | Research is redone after this many days |
| `detect.recheck_days` | 30 | Unconfirmed boards are checked again, and companies searched again, after this many days |

**`watch` and `filters`**

| Setting | Now | What it does |
|---|---|---|
| `watch.workday_enabled` | true | Read Workday boards (unofficial API; failures are logged and skipped). Defaults to false |
| `watch.workday_terms` | quality, test, SDET, QA | One Workday search per term |
| `watch.interval_hours` | 6 | Left from the first design; nothing reads it (boards are read once a night) |
| `filters.title_include` | regex | A title must match this to be kept |
| `filters.title_exclude` | regex | A title matching this is dropped |
| `filters.maine_terms` | maine, ", me", bangor, bar harbor, ellsworth, machias, orono | Places that make a location eligible |
| `filters.remote_terms` | remote, anywhere, work from home, distributed | Words that mark a location remote |
| `filters.us_terms` | us, u.s., usa, united states, north america | Words that mark a location US-wide |

**`http`, `search`, `evidence`, `ids`**

| Setting | Now | What it does |
|---|---|---|
| `http.min_interval_s_per_host` | 1.0 | Seconds between requests to the same site (1.0 is the minimum allowed) |
| `http.cache_hours` | 6 | How long fetched pages and evidence packs are reused |
| `search.searxng_url` | `http://127.0.0.1:8888` | The local search engine |
| `search.fallback_api` | none | Reserved for a backup search API; nothing reads it yet |
| `evidence.max_pages` | 8 | Pages fetched per research evidence pack |
| `evidence.max_chars_per_page` | 6000 | Text kept per page |
| `evidence.max_total_chars` | 30000 | Text kept per pack |
| `ids.job_prefix`, `ids.person_prefix` | `JW`, `PA` | Prefixes of new tracker IDs |

**`rank` and `nightly`**

| Setting | Now | What it does |
|---|---|---|
| `rank.per_run` | 60 | Most jobs scored per run |
| `rank.max_chars` | 6000 | Posting text sent to the model |
| `rank.timeout_s` | 300 | Seconds allowed per scoring call |
| `nightly.discover_limit` | 60 | Companies searched per night; 0 leaves discover out of the nightly |
| `nightly.export` | `store` | `store`: the nightly writes research, shortlisted jobs and people straight into the tracker. `preview`: it only writes a CSV preview to `out/<date>/preview/` and changes nothing |
| `nightly.report_top` | 8 | Jobs written up in full under Apply next |

**`providers`**: the model endpoints, grouped by tier (`local`, `local-large`, `free-cloud`, `subscription`,
`paid-cheap`). Each endpoint takes:

| Field | What it does |
|---|---|
| `model` | The model: a LiteLLM model string (`deepseek/deepseek-flash`, `gemini/<model>`, `ollama_chat/<model>`), or `claude-cli/<model>` or `codex-cli/default` for your subscriptions |
| `key_env` | The `.env` variable holding its API key; the endpoint is skipped while that's unset |
| `base_url` | Where the server is (Ollama: `http://localhost:11434`) |
| `rpm`, `rpd` | Calls per minute and per day; 0 means unlimited |
| `json_mode` | `schema` (the provider enforces the JSON schema, the default) or `object` (plain JSON only; the schema goes in the prompt) |
| `extra` | Settings passed through to the provider. Ollama needs `think: false` (or the small models think for 8,000 tokens and answer nothing) and a `num_ctx` that fits prompt and answer |
| `monthly_usd_cap` | Not enforced yet; cap spending in each provider's billing dashboard |

**`routing`**: the tier order per task: `triage`, `extract`, `judge` and `posting_relevance` (job scoring). See
[Models and the router](#models-and-the-router).

### .env

Copy `.env.example` to `.env` and fill in the keys you have. An endpoint whose key is missing is simply skipped.

| Key | Used for |
|---|---|
| `DEEPSEEK_API_KEY` | The `paid-cheap` tier (DeepSeek) |
| `GEMINI_API_KEY` | The `free-cloud` tier (Gemini) |
| `GROQ_API_KEY`, `OPENROUTER_API_KEY` | Other free-cloud providers, if you add their endpoints |
| `XAI_API_KEY`, `OPENAI_API_KEY` | Other paid providers, if you add their endpoints |
| `ANTHROPIC_API_KEY` | Only with `auth: api-key`; normally Claude runs on your Claude Code login |
| `SEARXNG_SECRET` | Read by `docker compose` for SearXNG; any random string |

### Scheduling

Two Windows tasks run as you, only while you're logged in, without a console window (`pythonw.exe`):

- **`jobsearch-nightly`** runs `nightly` at 02:30 every day (Docker Desktop and its search engine live in your
  session). A run missed while the PC was off or asleep starts as soon as it's back, a second start while one is
  running is ignored, and the task stops a run after 10 hours.
- **`jobsearch-tracker`** runs `serve` when you log in: the page at http://127.0.0.1:8787. It has no time limit, and
  if it stops with an error it's restarted up to 3 times, a minute apart.

```powershell
Get-ScheduledTaskInfo jobsearch-nightly    # last result (0 = ran) and next run time
Start-ScheduledTask jobsearch-nightly      # run it now, exactly as at 02:30
Start-ScheduledTask jobsearch-tracker      # start the page if it isn't running
Disable-ScheduledTask jobsearch-nightly    # pause; Enable-ScheduledTask to resume
```

`powershell -ExecutionPolicy Bypass -File tools\register_tasks.ps1` registers both tasks again (add `-Only nightly` or
`-Only tracker` for one of them), which you need after moving the repo folder (the tasks hold its path). The script's
header shows how to remove them. There used to be a third task that read the boards during the day; it was dropped on
2026-10-07, because only the nightly writes the report and it reads the boards itself.

### Setup (Windows)

1. Python 3.12 or later, then the project and its test tools:

   ```powershell
   py -3.13 -m venv .venv
   .venv\Scripts\python -m pip install -e ".[dev]"
   ```

2. Copy `.env.example` to `.env` and fill in your keys (see [.env](#env)).
3. Docker Desktop, then start SearXNG from the repo folder: `docker compose up -d`. It listens only on this PC
   (`127.0.0.1:8888`); its settings are in `searxng/settings.yml`.
4. Ollama, then the local models: `ollama pull qwen3.5:9b` and `ollama pull gemma4:12b`.
5. Log in once to `claude` (Claude Code) and `codex` (Codex CLI, with your ChatGPT account).
6. Check everything: `.venv\Scripts\python -m jobsearch doctor`.
7. Register both tasks: `powershell -ExecutionPolicy Bypass -File tools\register_tasks.ps1`. Until you next log in,
   `Start-ScheduledTask jobsearch-tracker` starts the page; then open http://127.0.0.1:8787.

### Development

```powershell
.venv\Scripts\python -m pytest            # the full suite, minus live tests
.venv\Scripts\python -m pytest -m live    # only the live tests: real network and model calls
.venv\Scripts\python -m ruff check .
```

- Board adapters live in `src/jobsearch/boards/`, one module per system, each tested against a saved API response in
  `tests/fixtures/boards/`. When a board system changes its response, `.venv\Scripts\python tools\capture_fixtures.py
  [system ...]` captures fresh, small fixtures from live endpoints.
- A new board system needs an adapter in `boards/` (wired into `boards/__init__.py`), its address patterns and API
  check in `detect.py`, its name in `SUPPORTED` (`detect.py`) and `ATS_TYPES` (`grounding.py`), and a fixture with
  tests. Commit `b0369ae` added five systems this way.
- The prompts are in `prompts/`: `triage.md`, `extract.md`, `judge.md`, `score_job.md`, plus the full and trimmed
  profiles.
- The tracker's rules live in one place, `Tracker.write` in `src/jobsearch/tracker.py`, next to the field schema the
  page draws from (it fetches `/api/schema`), so a new field is added there once. The page is
  `src/jobsearch/web/tracker.html` (plain JavaScript, no build step), served by `src/jobsearch/server.py`.
  `tests/test_tracker.py`, `test_server.py` and `test_tracker_cli.py` cover them.

### Troubleshooting

- **The page won't open.** Run `Get-ScheduledTask jobsearch-tracker`. If it isn't Running,
  `Start-ScheduledTask jobsearch-tracker`, or run `.venv\Scripts\python -m jobsearch serve --open` to see any error.
  "can't use port 8787" means something else holds the port: stop it, or use `serve --port 8788`.
- **"Changed elsewhere after you opened it."** The nightly, an agent or another browser tab changed one of the fields
  you edited while the drawer was open. Nothing was saved; the row is reloaded, so make your change again.
- **An agent's write was refused** (`refused: agents can't write ...`). It tried to change one of your fields, or one
  it may only set when adding the row. That's the rules working: the change is yours to make in the page.
- **A duplicate job.** For example JW0015 is the same Motional posting as JW0010: open it on the Jobs tab and click
  Delete. Only you can delete rows; the history log keeps a copy, and the nightly never brings it back.
- **No report this morning.** Run `Get-ScheduledTaskInfo jobsearch-nightly`. A last result of 0 means the run
  finished; anything else means it didn't (1: it gave up waiting for another run, or it crashed). Then read
  `logs/nightly-<date>.txt`. If the log has nothing for that night, run `.venv\Scripts\python -m jobsearch status`:
  a broken `config.yaml` shows up there at once. If the PC was off or asleep, or you weren't logged in, the run
  starts when you're back.
- **"SearXNG is not answering."** Start Docker Desktop, run `docker compose up -d` in the repo folder, then `doctor`.
- **A board fails every run** (HTTP 404 under errors). The company has probably moved or closed its board. To look
  for a new one, rule the old one out with `discover --reject <ID>=<type>:<token>`, then run
  `discover --only <ID> --force` (discover never searches for a company that still has a confirmed board). If the
  company's own careers page can't list jobs either, leave it alone: the watcher picks the board up again if it comes
  back. HubSpot (C402) has been like this since 2026-10-06.
- **A board belongs to a different company.** `discover --reject <ID>=<type>:<token>`. In PowerShell, quote it when
  the token has `|` in it, as Workday tokens do: `--reject "C365=workday:firstnational|12|fnbocareers"`.
- **"stopped early: per-run budget of 60 scores used."** The rest are scored next run, or run `rank` again now.
- **A model tier is out of budget, or a stage stopped with every tier out.** The queue waits for the next night.
  `doctor` shows what's left today. DeepSeek is prepaid: top up its balance when it runs low.
- **Ingest refused: "the tracker holds N companies but M are known".** `data/tracker/companies/` is damaged or half
  restored. Put it back (git has every committed version) before the next nightly, rather than letting companies be
  marked missing.
- **The PC is slow overnight.** gemma4:12b (`local-large`) needs most of the memory; it's only a fallback for triage.
