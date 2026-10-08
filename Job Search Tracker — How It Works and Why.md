# Job Search Tracker — How It Works and Why

Oct 7, 2026 · @Evan Ringo

## Purpose and scope

The tracker is a single-owner job-search database with a web UI. It tracks whole companies, not just openings, plus the people inside them worth approaching. It is built so AI agents can read and write it alongside the owner.

What it has to support:

- **A very wide net.** About 1,200 companies across hobby sectors (games, martial arts, history, fiction, firearms, travel, homesteading), mission sectors (labor, housing, food access, justice reform, addiction recovery, LGBTQ, sex-worker rights and the adult industry), and traditional software employers that match a 15-year QA and SDET background.
- **Three tracks of work.** Researching companies, following individual job postings from Lead to Offer, and approaching managers directly whether or not a job is open.
- **High throughput.** In a past search the owner sent about 20 applications a day, so the list has to be sortable and filterable, and the next action has to be obvious.
- **Agent participation.** Agents add and enrich rows. The owner keeps control of rankings, stages, notes and every outreach decision.

The owner lives in Columbia, Maine and is currently employed. That makes two things matter more than usual. One is remote-from-Maine eligibility, or a commute of about an hour or less (Bangor, Bar Harbor, Machias). The other is how stable the employer would be in a downturn.

## Architecture

The tracker has no server of its own. It is one self-contained HTML page, published as a claude.ai artifact, and it gets three runtime capabilities from the platform: a per-artifact database (db), file saving (downloads), and model calls charged to the viewer (sample).

&#91;embedded content: tracker architecture · 6 parts, 5 connections\]

Claude chats write rows directly. The page writes rows and calls the model. Anything outside claude.ai (scripts, Claude Code, Codex, local models) reaches the data only through CSV import.

**Page stack.** It is plain JavaScript with no framework. PapaParse 5.4.1 is loaded from cdnjs for CSV parsing, and fonts come from Google Fonts. Colors are tokens, redefined for dark mode. The page's content-security policy blocks every other network request, so the page cannot call outside APIs.

**Database semantics.** Rows live in named collections, one JSON document per row. A write can replace a whole document (set) or merge fields into it (update). The page subscribes to each collection and re-renders on every change. Every viewer the owner shares the page with sees the same data.

## Data model

The data is stored as seven collections of JSON documents. Each row is one document, and the document's ID is the row's ID field. Fields are snake\_case keys. Every field also has a human label, and that label is the CSV header used for import and export.

| Collection | ID field and format | Purpose |
| --- | --- | --- |
| jobs | job\_id: J### (owner or Claude), JW#### (board watcher, planned) | One row per job posting |
| companies | company\_id: C### | One row per employer, tracked whether or not it has openings |
| people | person\_id: P### (owner or Claude), PA#### (pipeline, planned) | Managers and leaders to approach |
| activity | activity\_id: A### | One row per event: call, interview, follow-up |
| materials | version\_id: R### | Each resume or cover-letter version, kept unchanged once sent |
| meta | single doc: schema | Machine-readable field list, enums and agent rules |
| imports | free-form doc ID | Queue of bulk rows waiting for the owner to accept |

Every row also carries two provenance fields: updated\_by (you, Claude, import, Claude (seed), or an agent name) and updated\_at (an ISO timestamp).

### jobs

| Group | Fields (key → CSV label) |
| --- | --- |
| Basics | job\_id → Job ID; company → Company / group; job\_title → Job title; track → Track; job\_rank → Your job rank (1 best) |
| Status | current\_stage → Current stage; highest\_stage → Highest stage reached; next\_action → Next action; follow\_up\_due → Follow-up due; date\_applied → Date applied; application\_method → Application method; deadline → Deadline |
| Listing | posting\_url → Job posting URL; application\_url → Application URL / portal; source\_url → Source URL; listing\_evidence → Listing evidence; last\_checked → Last checked; date\_discovered → Date discovered |
| Fit | location → Location; work\_mode → Work mode; maine\_eligible → Maine eligible?; travel\_fraction → Travel fraction; pay\_low → Published pay low; pay\_high → Published pay high; currency → Currency; pay\_period → Pay period; hiring\_difficulty → Hiring difficulty (1-5 hard); skill\_match → Skill match (1-5 high); fit\_notes → Difficulty / fit notes |
| Materials & contacts | resume\_version\_id → Resume version ID; resume\_link → Exact resume link used; cover\_version\_id → Cover letter version ID; cover\_link → Exact cover letter link used; recruiter\_contact → Recruiter / contact; referral → Referral / connection; application\_confirmation → Application confirmation; last\_contact\_date → Last contact date; next\_interview\_date → Next interview date; outcome\_feedback → Outcome / feedback; role\_research → Your role research / questions |

### companies

| Group | Fields (key → CSV label) |
| --- | --- |
| Basics | company\_id → Company ID; company → Company / group; sector → Sector; interests → Interests / connection; career\_url → Career / company URL; role\_families → Role families to search |
| Owner's scores | overall\_rank → Your overall rank (1 best); interest, values, pay, flexibility, travel\_perks, homestead\_fit → each (1-5 high best); dealbreakers → Your dealbreakers |
| Research | hiring\_difficulty → Hiring difficulty (1-5 hard); estimate\_confidence → Estimate confidence; estimate\_rationale → Estimate rationale; research\_notes → Research notes from search; your\_notes → Your research notes; research\_status → Research status; maine\_remote → Maine / remote eligibility; benefit\_source → Known benefit / growth source; research\_as\_of → Research as of; next\_research\_action → Next research action |
| Claude screening | fit\_score → Fit score (1-5, Claude estimate); stability\_score → Stability in a downturn (1-5, Claude estimate); remote\_likelihood → Remote-from-Maine likelihood; size\_estimate → Size estimate; ai\_summary → Why it might fit; ai\_red\_flags → Red flags; ai\_scored\_at → Screened on |
| Outreach | approach\_role → Who to approach (role); people\_search\_url → People search link; outreach\_notes → Outreach angle |

### people, activity, materials

| Collection | Fields (key → CSV label) |
| --- | --- |
| people | person\_id → Person ID; name → Name; company → Company / group; title → Title; profile\_url → Profile URL; source\_url → Where found (source URL); status → Status; relationship → Connection (cold / warm / referral); last\_contact\_date → Last contact date; next\_step → Next step; next\_step\_date → Next step date; why\_approach → Why approach them; notes → Notes / conversation log |
| activity | activity\_id → Activity ID; job\_id → Job ID; company → Company / group; date → Date; type → Type; stage\_round → Stage / round; contact → Contact / interviewer; channel → Channel; preparation → Preparation / research; questions\_asked → Questions asked; your\_answers → Your answers / examples; feedback\_outcome → Feedback / outcome; next\_action → Next action; next\_action\_date → Next action date; notes\_url → Notes / document URL |
| materials | version\_id → Version ID; doc\_type → Document type; file\_name → File / version name; doc\_url → Drive / document URL; created\_date → Created date; target\_role → Target role / sector; key\_changes → Key changes / emphasis; base\_version\_id → Base version ID; notes → Notes |

Relationships are joined by name, not by a foreign key. jobs.company, people.company and activity.company hold the company's exact name string, and activity.job\_id points at a Job ID. Nothing enforces these joins; see Known limitations.

## Enumerations and conventions

Enums are soft. The UI offers dropdowns, but it also keeps and shows a stored value that isn't in the list, so imported data never gets lost.

| Field | Allowed values |
| --- | --- |
| jobs.current\_stage | Lead, Verified, Applied, Screen, Interview, Offer, Accepted, Rejected, Withdrawn, Closed |
| jobs.highest\_stage | None + the stages above |
| jobs.track | Core QA, QA leadership, SDE pivot, Domain pivot, Exploratory |
| jobs.work\_mode | Remote, Hybrid, On-site, Unknown |
| jobs.listing\_evidence | Employer page read, Indexed lead - verify, Historical lead - recheck, Conflicting status - verify, Verified live |
| companies.research\_status | Not started, Initial research, Researching, Researched, Parked |
| companies.remote\_likelihood | High, Medium, Low, Unknown |
| people.status | To research, To contact, Contacted, Replied, Meeting, Referral, Parked |
| companies.size\_estimate (convention) | <50, 50-500, 500-5k, 5k+, Unknown |

**The closed stage set** is Accepted, Rejected, Withdrawn and Closed. A job in any of these stops raising follow-up flags. It also never raises highest\_stage.

**Dates** are text fields. Write them as YYYY-MM-DD. The parser also accepts MM/DD/YY and MM/DD/YYYY, because rows imported from the original spreadsheet use that format. Due-date math works with either.

**Numbers** (pay, ranks, scores, difficulty) are stored as numbers. On input, commas are stripped ("141,000" becomes 141000). If a value won't parse, it is kept as text rather than dropped.

**Pay** is stored as low, high, currency and period (Annual, Monthly). Never compare pay across rows without normalizing currency and period first. One row is EUR per month.

**Scores run in two directions on purpose.** Ranks are 1 = best. Preference scores are 1–5, high = best. Hiring difficulty is 1–5, high = harder. Claude's fit and stability scores are 1–5, high = better. Labels always state the direction.

**Provenance shows in the UI.** If updated\_by is anything other than you, the row shows "by \<name>" under its ID. That way the owner can see which rows agents wrote. Research notes that come from model knowledge rather than a source fetched that session start with "From general knowledge – verify" or "Seeded … not verified".

## UI behavior

The UI is one page. It has a header, a stage strip, tabs, a table per tab, and a slide-in drawer for editing.

**Header and stage strip.** The header shows totals: jobs, companies, and how many jobs have a date applied. Under it is a clickable strip of counts:

- one count per active stage: Lead, Verified, Applied, Screen, Interview, Offer
- Follow-ups due
- Closed

Clicking a count opens the Jobs tab filtered to that stage. Clicking it again clears the filter. This strip is the one visually bold element. It answers "where am I" in one glance.

**Tabs** are Jobs, Companies, People, Activity, Materials and Agent guide. Each tab label shows its row count. Each tab has a fixed default set of visible columns, chosen for scanning:

| Tab | Default columns |
| --- | --- |
| Jobs | ID, company, title, track, stage, due flag, next action, location, work mode, Maine eligible, pay range, difficulty, listing evidence, date discovered |
| Companies | ID, company, sector, interests, fit, stability, remote, overall rank, research status |
| People | ID, name, company, title, status, next step, next step date |
| Activity | ID, date, job ID, company, type, stage/round, next action, next action date |
| Materials | ID, type, file name, target role, created date, base version |

**The toolbar** has these controls:

- free-text search across all fields of the current tab
- a track filter (Jobs) or a sector filter (Companies)
- Add, Import CSV and Export CSV

Clicking a column header sorts by that column. Clicking it again reverses the sort. Sorting is numeric-aware, and empty values always sort last. The default sort is by ID.

**The drawer** opens when you click any row, press Enter on it, or click Add. It shows every field, grouped by the field groups in the data model. The ID is read-only on existing rows. Number fields get a decimal keypad on mobile.

**On save, the drawer does the following:**

1. Trims every value and converts number fields.
2. Blocks a duplicate ID on new rows.
3. Sets updated\_by to you and stamps updated\_at.
4. Writes the whole document.

Delete asks for confirmation first.

**New-row defaults:**

- The next ID is the highest trailing number in the tab, plus one, zero-padded to 3 digits.
- New jobs start at stage Lead, with highest stage None and date discovered set to today.

**Computed values.** None of these are stored; they are recalculated on every render.

| Value | Rule |
| --- | --- |
| Due flag | Empty if the stage is closed or follow\_up\_due is blank. Otherwise Overdue if the date is past, Today if it is today, Soon if within 3 days, and empty beyond that. |
| Highest stage | Raised on save when the current stage sits later in the stage order than the stored highest stage, and the current stage isn't a closed one. |
| Pay display | low–high currency / period, with thousands separators. |

**Live updates.** The page subscribes to every collection. Changes from agents or another browser tab show up without a reload. Re-rendering pauses while the drawer is open, so an incoming update can't wipe what the owner is typing. It also pauses during bulk operations, which then render once at the end.

**Theme and layout.** Light and dark mode follow the system setting, and the layout is responsive down to phone width. Fonts are IBM Plex Sans for body text and IBM Plex Sans Condensed for the dense tables.

## Getting data in and out

There are four paths: CSV import, CSV export, the bulk imports queue, and direct database writes by Claude.

### CSV import (per tab, merge by ID)

1. **Find the header row.** Parse the file, then use the first row whose first cell matches the tab's ID label (Job ID, Company ID, and so on). Title and description rows above it are skipped. That's why the original spreadsheet's exported sheets import as-is.
2. **Match columns.** Normalize each header: lowercase it, remove "(auto)", turn every run of non-alphanumeric characters into one space, and trim. Then match it to a field label normalized the same way. Unmatched columns are ignored, which is why the old sheet's computed columns (Due flag, Days since applied) drop out.
3. **Merge.** If the ID already exists, start from the stored row. Only non-blank cells overwrite stored values, so a blank cell never erases data. Number fields are parsed. Rows with a blank ID are skipped.
4. **Provenance.** updated\_by keeps its existing value. New rows get "import".
5. **Writes.** Rows are written one at a time. Each write retries up to 4 times with increasing waits on rate-limit errors. The UI shows progress every 10 rows and renders once at the end.

The merge rule is the contract that external pipelines rely on. A pipeline can send only the columns it owns and leave everything else alone. To protect fields the owner edits, columns like stage go out only the first time a row is exported.

### CSV export (per tab)

Export writes every field, using labels as headers, in the tab's field order. **It exports only the rows currently visible**, so active search and filters apply. Clear them before a full export. The file is named \<tab>-\<date>.csv and saved through the downloads capability.

### Bulk imports queue

The queue is for adding many companies without hundreds of separate writes, and without bypassing the owner.

- **Queue document format:** a document in the imports collection shaped like `{collection: "companies", status: "pending", label, added_by, created_at, rows: [ {company, sector, interests, …} ] }`. One document can hold up to about 256 KB of rows.
- **Prompt:** when pending rows exist, the Companies tab shows "N companies are waiting to be added (label)" with an Add them button.
- **On Add them:**
  1. Skip any row whose normalized name already exists. Names are normalized by lowercasing, removing parenthesized text, and stripping non-alphanumerics.
  2. Assign the next C### ID.
  3. Default research\_status to Not started.
  4. Set updated\_by to the queue doc's added\_by.
  5. Write rows one at a time, about 40 ms apart, with retry.
  6. Mark the queue doc done, recording the count imported and duplicates skipped.
- **If interrupted:** click again. Duplicates are skipped, so nothing doubles.

Agents are told to use the queue for any addition of more than about 10 companies. Small additions can go straight into the companies collection.

### Direct writes by Claude

A claude.ai chat that has the tracker's link can read and write any collection through the Artifact tool's read\_db and write\_db actions. Batches of up to 50 writes apply atomically, and pinned versions (if\_version) prevent overwriting someone else's concurrent edit. This is how the original 24 jobs and 70 companies were loaded.

## Claude screening feature

"Screen with Claude" is a cheap first pass. It scores companies from the model's own knowledge, with no web lookups, so they can be sorted before real research happens. It is not verification, and the UI says so.

**How it runs:**

- It appears on the Companies tab when any company has no fit\_score.
- Each click processes up to 100 unscored companies, 10 per request, using the "quick" model tier.
- Requests run in the viewer's browser and draw on their own Claude usage.
- The browser tab has to stay open. A Stop button cancels the request in flight.

**The prompt** gives the model:

- the candidate profile: stack, domains, location constraint, situation, and interest sectors
- a scoring rubric
- the batch, as `id | name | sector | note` lines

It asks for a JSON array only.

| Output | Rubric | Written to |
| --- | --- | --- |
| fit 1–5 | 5 = senior QA/SDET/quality-leadership roles are plausible, remote-from-Maine or commutable, and a strong domain or interest match. 3 = plausible with a clear gap. 1 = no realistic role. | fit\_score |
| stability 1–5 | Durability and job security in a downturn | stability\_score |
| remote | Likelihood of US-remote roles open to Maine residents: High, Medium, Low or Unknown | remote\_likelihood |
| size | <50, 50-500, 500-5k, 5k+, Unknown | size\_estimate |
| why (≤25 words), flags (≤15 words) | Free text | ai\_summary, ai\_red\_flags |
| roles, approach | Role families to search, and the manager title to contact | role\_families, approach\_role, written only if empty |

ai\_scored\_at is set to the date. Results go through a merge update, so owner fields are never touched.

**Error handling:** a rate-limit error waits 5 seconds and retries the batch. A not-granted error stops the run with a message. Any other error skips that batch and is reported at the end.

**Known weakness:** model knowledge is thinnest on small niche companies, which make up most of the hobby and mission sectors. Treat these scores as a way to order the research queue, not as findings. Verified research should replace remote\_likelihood and maine\_remote.

## Agent access

Agents learn the rules from the data itself, through the `meta/schema` document. The page and the agent briefs both point to it first.

**meta/schema** contains:

- purpose
- the ID convention for each collection
- the field lists for each collection
- enums
- candidate constraints: role targets, Maine location, stack
- a list of job-board sources
- agent\_rules

The agent\_rules, as currently written:

1. List existing documents first. Take the next free ID, and never reuse or renumber one.
2. Check companies by name before adding one. Set jobs.company to the exact existing name.
3. New jobs start at stage Lead, with highest stage None. Agents never set application or interview fields.
4. Dates are YYYY-MM-DD. Numbers are stored as numbers.
5. listing\_evidence must be one of the five allowed values.
6. Set updated\_by to the agent's name, and set updated\_at.
7. To change an existing document, update it (merge). Never blank out a field the owner filled.
8. Never invent pay, eligibility or benefits. Leave unknown fields out.
9. People come only from public professional sources, with source\_url filled. No personal contact details or private accounts.
10. Notes based on model knowledge are marked "From general knowledge – verify". Write "Verified" only for something fetched in that session.
11. Replace the model-estimated remote and eligibility fields with verified facts once researched.
12. Bulk additions go through the imports queue.

**Field ownership.** This is the rule that makes multi-writer use safe.

| Owner | Fields | Agents may |
| --- | --- | --- |
| Owner only | Company ranks and preference scores, dealbreakers, your\_notes. Job stage changes after creation, every application, materials and interview field. People status after creation, notes, contact dates. The whole Activity and Materials tabs. | Read only |
| Insert-only | Job track, current\_stage (Lead), highest\_stage (None), next\_action, date\_discovered. Person status (To research), relationship (Cold), next\_step. | Set when creating the row, never after |
| Agent-maintained | Company research, screening and outreach fields. Job listing, location, pay and fit fields. Person name, title, company, source\_url, why\_approach. | Create and update |

**The Agent guide tab** holds two paste-in briefs:

- **Verify a batch:** take the top N companies by fit\_score that are still Not started, research each on the web, update the company, add any matching jobs and publicly named leaders, then summarize.
- **Add more companies in bulk:** write a single imports doc for the owner to accept.

The tab also prints the live field list.

## What's in it now

The tracker holds 36 jobs, 149 companies and 4 people. Another 1,070 companies sit in the imports queue until the owner clicks Add them. Activity and Materials are empty, and no application has been recorded yet.

The rows vary a lot in how far they can be trusted, so carry their provenance over when migrating.

| Rows | Origin | Trust level |
| --- | --- | --- |
| J001–J024, C001–C070 | The owner's original spreadsheet (research pass of 2026-09-29), imported as-is | Mostly search-index leads. Every job says verify. Company scores are low-confidence estimates. |
| J025–J030, C071–C076 | First Claude session: web searches for remote QA in regulated health, Maine employers and RFID | Seen in search results that session. Postings not confirmed live. |
| J031–J036, C077–C098 | Hobby and mission sweep with web searches (Recidiviz, Propel, Bitfocus, Gymdesk, SmartBear and others) | research\_notes start with "Verified in 09/30 search". Facts came from search results, not from opened pages. |
| C099–C149 | Same sweep, from model knowledge | Notes start with "From general knowledge – verify" |
| 1,070 queued companies | Wide-net seed list from model knowledge: name, sector and a one-line hook only | "Seeded … not verified". Some may be defunct, renamed or wrongly described. |
| P001–P004 | Executives named on public company pages (Gymdesk ×2, Kicksite, Propel) | Name and title come from the cited source\_url. Status: To research. |

**Sector values in use:** Games & stories, Sports, Adult & sexuality, Mission & organizing, Firearms, Growth technology, Travel, Agriculture, Regulated healthtech, Maine employers, Maine & New England, Public-sector software, IoT / location tech, Test tooling, Martial arts, Martial arts & fitness, History, Books, history & media, Food access, Housing, Addiction & recovery, Criminal justice reform, Labor, Labor & organizing, LGBTQ, Enterprise & govtech, Remote-first software, Defense, aerospace & robotics, and AI & evals.

Several of these overlap: Labor and Labor & organizing, Maine employers and Maine & New England, Martial arts and Martial arts & fitness. Merge them during migration.

## Design decisions and rationale

| Decision | Why | What it cost |
| --- | --- | --- |
| Host on a claude.ai artifact with its own database, not the Google Sheet | The Drive connector could create and replace files but not edit cells. The artifact database gave Claude row-level reads and writes, live updates and a custom UI in one step. | Only claude.ai chats and the page itself can write. External agents need a bridge (CSV). This is the main reason to re-implement. |
| One document per row; document ID = row ID | Atomic per-row writes, simple merges and concurrency pins, and direct lookups by ID for agents | No server-side joins or aggregation. Every count is computed in the browser. |
| Keep the original sheet's column labels as CSV headers | The existing spreadsheet imported unchanged, and every external tool can round-trip through CSV | Labels are long and are effectively an API. Renaming one breaks importers. |
| Blank cells never overwrite | Pipelines send only the columns they own. Re-imports are safe. | Clearing a field through CSV is impossible. It has to be done in the UI. |
| Field ownership: owner-only, insert-only, agent-maintained | The owner's ranks, stages and notes can never be reset by a re-import or an over-eager agent | Agents can't fix a stage the owner forgot to update. They can only flag it. |
| Separate ID prefixes for automated sources (JW, PA) | Agents and pipelines can't collide with the owner's sequential IDs, even with stale snapshots | Two ID shapes per collection. Sorting by ID groups them separately. |
| Track companies independently of openings, plus a People tab | The strategy includes direct manager outreach without an open req | More data to maintain. Joins by company name are fragile. |
| Bulk adds via an imports queue that the owner accepts | The owner stays in the loop for list growth. One write instead of hundreds. Dedupe happens in one place. | Rows aren't visible until accepted, and acceptance needs the page open |
| Soft enums | Imported legacy values are never destroyed | Typos and near-duplicates accumulate, as the sector list shows |
| Dates and numbers stored loosely, parsed leniently | The old MM/DD/YY data works without migration | Agents have to be told the preferred format. Mixed formats exist. |
| Model-knowledge screening labeled as an estimate, separate from verified research | Lets 1,000+ rows be sorted cheaply without passing guesses off as facts | Two parallel sets of remote and fit fields until research replaces them |
| Only public professional sources for people; no LinkedIn automation | Privacy, and LinkedIn's terms. Protects the account the owner needs for outreach. | People coverage depends on what companies publish. The owner does the LinkedIn lookups via the generated search links. |
| Location gate (remote-from-Maine or about 1 hour) as a first-class field | The owner lives Downeast, so most leads fail on eligibility, not on skills | Eligibility is often unknowable until a posting is read |
| Stability in a downturn scored separately from fit | The owner is employed and hedging against an economic slide. Security can outweigh interest. | One more estimate to verify |
| Provenance on every row (updated\_by, plus verify markers in notes) | The owner can tell which rows they or an agent wrote, and how much to trust each | Discipline is required. Agents that skip updated\_by blur the trail. |

## Known limitations and gotchas

These are the things a re-implementation should fix, or at least not repeat by accident.

- **Writers are limited.** Only claude.ai chats (Artifact tool) and the page can write. Claude Code, Codex, local models and cron jobs cannot. Everything external goes through manual CSV import.
- **Export is filter-sensitive.** Export CSV only writes visible rows, so a forgotten search term silently truncates the file.
- **Joins are name-based.** If a company is renamed, its jobs, people and activity rows are orphaned. There is no company\_id on jobs or people.
- **IDs are computed client-side.** The next ID is the highest existing number plus one. Two writers working at the same time could pick the same ID. Separate prefixes avoid this for pipelines but not for two chat agents.
- **There's no delete from CSV, and no way to clear a field from CSV** (blank cells are ignored).
- **There's no history.** Updates overwrite in place. updated\_by and updated\_at record only the last writer. The original values of edited fields aren't kept.
- **Rendering doesn't scale far.** Every collection loads fully into the browser, and the table renders all matching rows. That's fine at about 1,200 companies and gets sluggish well beyond that. There's no pagination or virtualization.
- **Bulk operations need an open tab.** Screening and Add them both stop if the tab closes. They're resumable, but not in the background.
- **Screening spends the owner's Claude usage.** That makes it a poor fit for background work. This is why the external pipeline plan moved to local and free models.
- **Search is a substring match across every field.** There's no field-specific query, saved views, or multi-filter combinations beyond stage, track and sector.
- **Sector values have drifted.** Several near-duplicates exist (listed in What's in it now).
- **Rows may be stale.** Some research\_as\_of and last\_checked dates come from the 2026-09-29 and 2026-09-30 passes, and job postings may have closed since.

## Re-implementation guide

The recommended rebuild is Postgres as the store, a self-hosted spreadsheet-style UI on top of it, and database roles that enforce field ownership. Every agent (Claude Code, Codex, local scripts, MCP clients) can then write directly, and the CSV bridge goes away.

### Requirements checklist

- [ ] The same five tables and fields as the Data model section, with the same keys, so the existing plan.md pipeline maps over.
- [ ] A real company\_id foreign key on jobs, people and activity, with name kept as a display field.
- [ ] IDs from database sequences, keeping the prefixes (J, C, P, A, R for people-entered rows; JW, PA for pipelines).
- [ ] Field ownership enforced by the database, not by convention (see the sketch below).
- [ ] updated\_by and updated\_at set by triggers, plus a history table that keeps old values.
- [ ] The "blank never overwrites" merge semantics for any bulk upsert path.
- [ ] Enums as lookup tables or CHECK constraints, with the near-duplicate sectors merged.
- [ ] Owner views equivalent to the current UI: stage strip, due flags, the default columns per tab, an edit form, search, sort, and filters by stage, track and sector.
- [ ] An imports or staging table with owner approval for bulk company additions.
- [ ] Owner-only CSV export that ignores UI filters, or a scheduled database dump.
- [ ] The rules from meta/schema stored as a document in the repo (AGENTS.md / CLAUDE.md), so every agent loads them.

### Stack options

| Option | Owner UI | Agent access | Fit |
| --- | --- | --- | --- |
| Postgres + NocoDB or Baserow (self-hosted, Docker) | Spreadsheet-like grid, forms, filters, views | SQL with a restricted role, a Postgres MCP server, or the tool's REST API | Best overall. Column-level grants enforce ownership. |
| Supabase (hosted Postgres) | Table editor, or a small custom UI | SQL, REST, MCP | Good if you don't want to self-host. Watch the free-tier limits. |
| SQLite in the Git repo + Datasette | Read-mostly browser UI | Direct file access from every local agent | Simplest. Weak for concurrent writers and for editing. |
| Google Sheets via the Sheets API | The familiar sheet | Service-account API from scripts | Easy for the owner. Hard to enforce ownership, and gets clumsy past about 1,000 rows. |

### Ownership enforced with Postgres column privileges (sketch)

```sql
-- agent role: may insert new jobs with stage defaults, but may only UPDATE listing/fit columns
CREATE ROLE agent LOGIN PASSWORD '…';
GRANT SELECT ON jobs, companies, people TO agent;
GRANT INSERT (job_id, company_id, company, job_title, track, current_stage, highest_stage, next_action,
              date_discovered, posting_url, application_url, source_url, listing_evidence, last_checked,
              location, work_mode, maine_eligible, pay_low, pay_high, currency, pay_period,
              skill_match, fit_notes, updated_by) ON jobs TO agent;
GRANT UPDATE (posting_url, application_url, source_url, listing_evidence, last_checked, location,
              work_mode, maine_eligible, pay_low, pay_high, currency, pay_period, skill_match,
              fit_notes, updated_by) ON jobs TO agent;   -- stage, rank, application fields: no UPDATE grant
-- companies: UPDATE only research/screening/outreach columns; no grant on overall_rank, preference scores, your_notes
-- activity, materials: no grants at all
```

### Migration steps

1. In the current tracker, click Add them so the 1,070 queued companies become real rows.
2. Clear the search box and all filters. Export CSV on each of the five tabs.
3. Create the schema. Load each CSV by mapping its header labels to keys using the tables in Data model. Normalize dates to YYYY-MM-DD.
4. Fill in company\_id on jobs, people and activity by joining on normalized company name. Review the unmatched rows by hand.
5. Merge the duplicate sectors and validate every enum column against its allowed list.
6. Carry provenance over: updated\_by and updated\_at, and the verify markers in research\_notes.
7. Point the pipeline (plan.md and plan-models.md) at the database. Swap its section 5 CSV export for upserts under the agent role, and keep the insert-only rules.
8. Put the agent\_rules into AGENTS.md and CLAUDE.md. Make the old artifact read-only by keeping it but no longer editing it, as a frozen snapshot.
