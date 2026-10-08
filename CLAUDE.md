Follow plan.md, with plan-models.md and plan-tracker.md overriding where they conflict (plan-tracker.md wins on
anything about the tracker: this repo is now the job-search tracker, replacing the claude.ai one).

## Working on the tracker

The tracker is `data/tracker/`: one JSON file per row, in five tabs (jobs, companies, people, activity, materials).
The owner works in it through the page (`python -m jobsearch serve`, http://127.0.0.1:8787). Agents (Claude Code,
Codex, scripts) use the `tracker` commands; the nightly pipeline writes through the same code as "pipeline". These
rules replace the old claude.ai tracker's agent rules (kept in `data/tracker/meta/schema.json`).

1. Write only with `python -m jobsearch tracker add|update|queue-companies ... --by "<your agent name>"`. Never edit
   the files in `data/tracker/` by hand and never write as "you": that's the owner. The commands enforce these rules
   and log every change, with its old value, to `data/tracker/history.jsonl`. Read freely: `tracker list`,
   `tracker get`, or the files themselves.
2. Look before adding. Run `tracker list companies --json` (or `--where company=<name>`) so a company is never added
   twice, and set a job's or person's `company` to the exact existing name, with `company_id` when you know it.
   `tracker add` takes the next free ID (J037, C1220, P005, ...); never reuse or renumber one.
3. Who may change what (a refused write changes nothing and exits 1):
   - Yours to keep up to date: companies' career_url, role_families, hiring_difficulty, estimate_confidence,
     estimate_rationale, research_notes, research_status, maine_remote, benefit_source, research_as_of,
     next_research_action, fit_score, stability_score, remote_likelihood, size_estimate, ai_summary, ai_red_flags,
     ai_scored_at, approach_role, people_search_url, outreach_notes; jobs' company, company_id, job_title,
     posting_url, application_url, source_url, listing_evidence, last_checked, location, work_mode, maine_eligible,
     pay_low, pay_high, currency, pay_period, skill_match, fit_notes; people's name, company, company_id, title,
     profile_url, source_url, why_approach.
   - Only when you add the row: a company's name, sector and interests; a job's track, current_stage (always Lead),
     highest_stage (None), next_action and date_discovered; a person's status (To research), relationship (Cold) and
     next_step.
   - Never: everything else. That is the owner's ranks, preference scores, dealbreakers and notes; a job's stage after
     it's added and every application, materials and interview field; a person's status after it's added, notes and
     contact dates; the whole Activity and Materials tabs; and deleting rows.
4. Dates are YYYY-MM-DD. Numbers are numbers (pay, ranks, scores, difficulty); "120,000" is fine, the command
   parses it.
5. listing_evidence is one of: Employer page read, Indexed lead - verify, Historical lead - recheck, Conflicting
   status - verify, Verified live.
6. Never invent pay, eligibility or benefits; leave unknown fields out. Blank values you send are ignored, so nothing
   the owner filled in can be erased.
7. People come only from public professional sources (company pages, press, talks, engineering blogs, job posts),
   with that page in source_url: name and title, nothing more. No personal contact details, home locations or private
   social accounts, and never fetch or scrape LinkedIn. A LinkedIn search link in people_search_url is fine.
8. Notes from model knowledge start "From general knowledge – verify". Write "Verified" only for something you
   fetched in that session. When you research a company, replace the screening estimates (remote_likelihood,
   maine_remote) with what you verified.
9. More than about 10 companies: queue them rather than adding them one by one, with
   `tracker queue-companies --file companies.json --label "<what they are>" --by "<your agent name>"` and a JSON list
   of {company, sector, interests, career_url, research_notes}. The owner adds them from the Companies tab, which
   skips names already listed and assigns the IDs.
