# Addendum: Multi-Model Routing (cut Claude usage to near zero at runtime)

Read this together with `plan.md`. Where the two disagree, this file wins. It replaces:
- `plan.md` Phase 4 (triage)
- `plan.md` Phase 5 (verification agent)
- the `models:` block in `plan.md` section 9

Everything else in `plan.md` still applies. That includes the hard rules, the CSV contract, field ownership, the board watcher and export.

## 1. Principles

1. **No LLM where code works.** Job-board polling, job-board detection, fetching, cleaning, validation, dedupe and CSV export are all plain Python.
2. **Code browses; models read.** The verification step must not be an agent making a dozen tool calls. Python gathers an *evidence pack* (search results plus cleaned page text, with numbered sources). A model then reads that pack once and returns JSON. That means 1–2 model calls per company instead of 10+, and small or local models can do the job.
3. **Cheapest capable model first, escalate on failure.** Every call goes through one router. If validation or grounding fails, or a high-value company comes back with low confidence, retry once on the next tier up.
4. **The profile stays local.** Free cloud tiers may use prompts for training. Cloud models only ever see public company evidence. The candidate profile (`prompts/profile.md`) is sent only to the local model, or to a paid tier the owner explicitly allows.
5. **Free tiers change constantly.** Quotas live in config, not code. Every provider has a daily budget counter. When one runs out, the router moves on to the next provider instead of failing the run.

## 2. Components

- **LiteLLM** (Python library, not the proxy) as the single call interface. It covers Ollama (local), Gemini (AI Studio), Groq, OpenRouter, OpenAI API and Anthropic API, and all calls are OpenAI-shaped.
  - If LiteLLM can't do JSON-schema output for a provider, use the provider's native SDK for that one provider, behind the same interface.
- **Ollama** for local models, using JSON-schema structured output (the `format` parameter).
- **SearXNG**, self-hosted in Docker on the same machine, bound to localhost only, with its JSON output format enabled. This is the search engine for evidence gathering.
  - Optional fallback: one free-tier search API, configured in `config.yaml`. It is off by default.
- **trafilatura** to turn fetched HTML into clean main text, plus `httpx` with the politeness rules from `plan.md` §2.7.
- **SQLite** at `state/router.db` for per-provider daily usage counters, a response cache (keyed by prompt hash and model), and the escalation log.

## 3. Task routing

| Task | Needs profile? | Tier order (first that passes wins) | Escalate when |
|---|---|---|---|
| A. Posting relevance: title + description → `skill_match`, `track`, Maine eligibility from text | yes | local | never (fall back to the regex filters) |
| B. Company triage from model knowledge: 1,200 names → rough fit / stability / remote | yes, trimmed* | local-large → free-cloud | never; triage is only for queue ordering |
| C. Fact extraction: evidence pack → facts JSON | **no** | local → free-cloud → paid-cheap | grounding check fails, or schema fails twice |
| D. Personal judgment: facts + profile → fit, difficulty, approach role, outreach angle | yes | local → paid-cheap (owner opt-in) | confidence low AND facts show remote = yes or varies |
| E. Weekly synthesis, final top-50 review, outreach drafts | yes | **manual in chat** (Claude or ChatGPT, whichever has usage left) | n/a |

\* The trimmed profile has role targets, the location constraint and interest *sectors*. Leave out anything else personal. Use the trimmed version for any non-local call.

Tier names map to concrete models in `config.yaml`. Ask the owner what local hardware he has and pick models that fit:
- `local`: the best instruction model that runs at a usable speed on the owner's machine. Roughly 7–14B parameters with 8–16 GB of VRAM; 20–30B with 24 GB or more of VRAM, or 32 GB or more of Apple unified memory.
- `local-large`: the biggest local model that fits, even if it's slow. Batch B runs overnight.
- `free-cloud`: an ordered list of free-tier endpoints (for example Gemini Flash/Flash-Lite in AI Studio, Groq-hosted open models, OpenRouter `:free` models). Each entry has its own RPM/RPD limits taken from config.
- `paid-cheap`: off by default. The owner can set an API key and a monthly dollar cap.

**Never route runtime calls to the owner's Claude or ChatGPT chat subscriptions.** Those are for building the code and for task E.

## 4. Verification, rewritten (replaces plan.md Phase 5)

For each company in the queue (the queue rules are unchanged):

**1. Gather** (code, no LLM)
- Run SearXNG queries built from templates:
  - `"{company}" careers`
  - `"{company}" remote work policy`
  - `"{company}" layoffs OR funding OR acquisition 2025 OR 2026`
  - `"{company}" engineering OR QA OR quality team`
- Add the known `career_url` and job-board API data if available.
- Fetch the top results: at most 8 pages, skipping `linkedin.com` and social sites. Extract main text, then truncate each page to `evidence.max_chars_per_page`.
- Build `evidence.json`: a list of `{id: "S1", url, title, fetched_at, text}`. The whole pack is capped at `evidence.max_total_chars`.

**2. Extract** (task C; no profile)
- The prompt is `prompts/extract.md` plus the evidence pack.
- Output: the facts schema (§5). Every fact cites a source ID and includes a verbatim `quote` of 25 words or fewer.

**3. Grounding check** (code)
- For every fact, the cited source ID must exist in the pack, and the `quote` must appear in that source's text after whitespace and case normalization.
- Drop facts that fail. If more than 30% of facts fail, the whole extraction fails, and the router escalates to the next tier.

**4. Judge** (task D)
- The prompt is `prompts/judge.md` plus the profile plus the grounded facts only. The model never sees raw web text.
- Output: `{fit, hiring_difficulty, role_families, approach_role, outreach_angle, why, red_flags, confidence}`.

**5. Merge** the facts and the judgment into the research record. Map it to tracker columns exactly as in `plan.md` §6.

**6. Record** the provider, model, tokens, latency and escalations for each step in `logs/runs.jsonl`.

The prompt-injection exposure drops sharply with this design:
- the extractor only outputs quotes, and they must exist in the page text
- the judge never sees web text at all
- no model has tools

Keep the injection fixture test from `plan.md` §8.4 and point it at steps 2–4.

## 5. Facts schema (task C output)

```json
{
  "career_url": {"value": "https://…", "source": "S2", "quote": "…"} ,
  "ats": {"type": "greenhouse|lever|ashby|smartrecruiters|workday|other|unknown", "token": "…|null", "source": "S2", "quote": "…"},
  "remote_policy": {"summary": "≤40 words", "maine_eligible": "yes|no|varies|unknown", "source": "S3", "quote": "…"},
  "stability_signals": [{"kind": "layoffs|funding|revenue|acquisition|growth|public-sector|other", "summary": "≤25 words", "date": "YYYY-MM|null", "source": "S5", "quote": "…"}],
  "size": {"value": "<50|50-500|500-5k|5k+|Unknown", "source": "S1|null", "quote": "…|null"},
  "qa_openings": [{"title": "…", "location": "…", "work_mode": "Remote|Hybrid|On-site|Unknown", "url": "https://…", "source": "S4", "quote": "…"}],
  "people": [{"name": "…", "title": "…", "source": "S6", "quote": "…"}]
}
```

Rules:
- Unknown facts are `null`, never guessed.
- `people` has at most 5 entries, and each person's name must appear inside their quote.
- The `plan.md` validation rules still apply: no emails, no phone numbers, no LinkedIn URLs.

## 6. Router behavior

`router.call(task, messages, schema, needs_profile, value_hint)`:
1. Pick the tier list for the task (§3). If `needs_profile` is true, drop any tier not allowed to see the profile.
2. For each tier, try each of its endpoints in order. Skip endpoints that are over their daily budget or in RPM backoff.
3. On HTTP 429 or a quota error: mark the endpoint exhausted until its reset time, then move on.
4. On a schema failure: retry once on the same endpoint with the validation error appended. On a second failure, move to the next tier.
5. Cache successful results by `(task, model, prompt_hash)`. A rerun must not re-spend quota.
6. If every tier is exhausted, stop the stage cleanly and leave the rest of the queue for the next run.

## 7. Config additions (replaces `models:` in plan.md §9)

```yaml
providers:
  local:        [{model: "ollama/<model>", base_url: "http://localhost:11434", rpm: 0, rpd: 0}]   # 0 = unlimited
  local-large:  [{model: "ollama/<bigger-model>", base_url: "http://localhost:11434", rpm: 0, rpd: 0}]
  free-cloud:
    - {model: "gemini/<flash-lite-model>", key_env: GEMINI_API_KEY, rpm: 10, rpd: 400}   # set from AI Studio's current limits
    - {model: "groq/<open-model>",          key_env: GROQ_API_KEY,   rpm: 20, rpd: 1000}
    - {model: "openrouter/<model>:free",    key_env: OPENROUTER_API_KEY, rpm: 15, rpd: 45}
  paid-cheap:   []        # owner opt-in; add model + key_env + monthly_usd_cap
profile_allowed_tiers: [local, local-large]       # add paid-cheap only if owner agrees
routing:
  posting_relevance: [local]
  triage:            [local-large, free-cloud]
  extract:           [local, free-cloud, paid-cheap]
  judge:             [local, paid-cheap]
search:
  searxng_url: "http://127.0.0.1:8888"
  fallback_api: null
evidence:
  max_pages: 8
  max_chars_per_page: 6000
  max_total_chars: 30000
```

Set the RPM/RPD numbers from each provider's dashboard on the day you configure them. Don't trust blog posts. Free tiers change month to month.

## 8. Phase changes

- **New Phase 2b: Local stack.**
  - Install Ollama and pull the chosen models.
  - Add `docker-compose.yml` for SearXNG (localhost only, JSON format enabled).
  - Smoke test: `python -m jobsearch doctor` checks every configured provider with a 1-token call, checks SearXNG with one query, and prints remaining daily budgets.
- **Phase 4 (triage):** use the router (task B). Same acceptance checks as `plan.md`.
- **Phase 5 (verification):** use §4 of this file.
  - **Acceptance:** run the same 5 hand-picked companies through every tier separately. Show grounding pass rates and the owner's spot-check side by side, so the owner can pick the cheapest tier that's good enough.
- **Tests to add:**
  - The router with fake providers: 429 handling, budget exhaustion, schema-retry-then-escalate, cache hits.
  - The grounding check: a quote that exists passes, a paraphrase fails, and a quote cited to the wrong source fails.
  - The privacy test: no profile text in any request sent to a tier outside `profile_allowed_tiers` (assert on captured requests).

## 9. Building with less Claude usage

- Both Claude Code and Codex CLI can build from these files.
  - Add `CLAUDE.md` with one line: "Follow plan.md, with plan-models.md overriding where they conflict."
  - Add `AGENTS.md` (for Codex) with the same line.
- Suggested split:
  - Codex builds the scaffolding and the mechanical phases (0, 1, 2, 2b, 3, 6, 7).
  - Claude Code reviews the diffs and builds the trickier parts: the router, the grounding check, and the injection tests.
  - Either one can review the other's work.
- Keep sessions short. Start a fresh session per phase instead of one long session.
