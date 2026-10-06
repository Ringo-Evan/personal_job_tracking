"""Router with fake providers (plan-models.md §8): 429s, budgets, schema retry then escalate, cache, privacy."""

import json
from types import SimpleNamespace

import pytest

from jobsearch.agent import AgentResult, UsageLimitError
from jobsearch.config import Endpoint
from jobsearch.router import Router, RouterExhausted, prompt_hash
from jobsearch.usage import connect, record_call

SCHEMA = {
    "type": "object",
    "properties": {"n": {"type": "integer", "minimum": 1, "maximum": 5}},
    "required": ["n"],
    "additionalProperties": False,
}
PROFILE = "SECRET-PROFILE Columbia, Maine"
MSGS = [{"role": "user", "content": f"{PROFILE} rate it"}]
PUBLIC = [{"role": "user", "content": "Downeast Maine rate it"}]


def reply(content, prompt_tokens=10, completion_tokens=3):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        usage=SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens),
    )


class RateLimited(Exception):
    status_code = 429

    def __init__(self, retry_after=None):
        super().__init__("429 Too Many Requests")
        self.response = SimpleNamespace(headers={"retry-after": str(retry_after)} if retry_after else {})


class FakeProviders:
    """complete(**kwargs) stand-in. `script[model]` is a list of replies or exceptions consumed in order."""

    def __init__(self, script=None, default='{"n": 3}'):
        self.script = {k: list(v) for k, v in (script or {}).items()}
        self.default = default
        self.requests = []

    def __call__(self, **kw):
        self.requests.append(kw)
        queue = self.script.get(kw["model"])
        item = queue.pop(0) if queue else self.default
        if isinstance(item, Exception):
            raise item
        return reply(item)

    def models(self):
        return [r["model"] for r in self.requests]


class FakeClaude:
    """run_agent stand-in. `results` are AgentResults or exceptions consumed in order; default is garbage."""

    def __init__(self, results=()):
        self.results = list(results)
        self.calls = []

    def __call__(self, cfg, **kw):
        self.calls.append(kw)
        item = self.results.pop(0) if self.results else AgentResult(True, structured={"bad": 1})
        if isinstance(item, Exception):
            raise item
        return item


class Clock:
    def __init__(self, t=1_790_000_000.0):
        self.t = t
        self.slept = []

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.slept.append(s)
        self.t += s


@pytest.fixture
def rcfg(cfg):
    c = cfg.model_copy(deep=True)
    c.providers.local = [Endpoint(model="ollama_chat/small")]
    c.providers.local_large = [Endpoint(model="ollama_chat/big")]
    c.providers.free_cloud = [
        Endpoint(model="gemini/flash", key_env="GEMINI_API_KEY", rpm=2, rpd=3),
        Endpoint(model="groq/open", key_env="GROQ_API_KEY", rpd=100),
    ]
    c.providers.subscription = [Endpoint(model="claude-cli/haiku", rpd=5, extra={"thinking": False})]
    c.providers.paid_cheap = [Endpoint(model="openai/mini", key_env="OPENAI_API_KEY")]
    c.profile_allowed_tiers = ["local", "local-large"]
    c.routing.extract = ["local", "free-cloud", "paid-cheap"]
    c.routing.triage = ["local-large", "free-cloud", "subscription"]
    c.routing.judge = ["local", "paid-cheap"]
    return c


@pytest.fixture
def make_router(rcfg, tmp_path):
    def _make(fake, env=None, clock=None, db=None, claude=None):
        clock = clock or Clock()
        env = {"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q"} if env is None else env
        return Router(
            rcfg,
            db=db or connect(tmp_path / "router.db"),
            complete=fake,
            clock=clock,
            sleep=clock.sleep,
            environ=env,
            claude_agent=claude or FakeClaude(),
        )

    return _make


def test_first_tier_wins_and_reports_tokens(make_router):
    fake = FakeProviders()
    res = make_router(fake).call("extract", MSGS, SCHEMA, needs_profile=False)
    assert res.data == {"n": 3} and res.tier == "local" and res.model == "ollama_chat/small"
    assert res.tokens == {"prompt_tokens": 10, "completion_tokens": 3} and res.escalations == 0
    sent = fake.requests[0]
    assert sent["response_format"]["json_schema"]["schema"] == SCHEMA


def test_endpoint_extra_kwargs_and_key_are_passed(make_router, rcfg):
    rcfg.providers.local = [
        Endpoint(model="ollama_chat/small", base_url="http://x:1", extra={"think": False})
    ]
    rcfg.routing.extract = ["local", "free-cloud"]
    fake = FakeProviders({"ollama_chat/small": [RuntimeError("down")]})
    make_router(fake).call("extract", MSGS, SCHEMA, needs_profile=False)
    assert fake.requests[0]["think"] is False and fake.requests[0]["api_base"] == "http://x:1"
    assert fake.requests[1]["api_key"] == "g"


def test_schema_failure_retries_once_with_error_then_escalates(make_router, tmp_path):
    fake = FakeProviders({"ollama_chat/small": ['{"n": 9}', "not json at all"]})
    r = make_router(fake)
    res = r.call("extract", MSGS, SCHEMA, needs_profile=False)
    assert fake.models() == ["ollama_chat/small", "ollama_chat/small", "gemini/flash"]
    retry_msgs = fake.requests[1]["messages"]
    assert "invalid" in retry_msgs[-1]["content"] and "maximum" in retry_msgs[-1]["content"]
    assert res.tier == "free-cloud" and res.escalations == 1
    assert [a.outcome for a in res.attempts] == ["schema", "schema", "ok"]
    rows = r.db.execute("SELECT task, tier, model FROM escalations").fetchall()
    assert rows == [("extract", "local", "ollama_chat/small")]


def test_schema_retry_can_succeed_on_same_endpoint(make_router):
    fake = FakeProviders({"ollama_chat/small": ["```json\n{}\n```", '```json\n{"n": 2}\n```']})
    res = make_router(fake).call("extract", MSGS, SCHEMA, needs_profile=False)
    assert res.data == {"n": 2} and res.tier == "local" and len(fake.requests) == 2


def test_429_marks_endpoint_exhausted_and_moves_on(make_router, rcfg, tmp_path):
    rcfg.routing.extract = ["free-cloud"]
    db = connect(tmp_path / "router.db")
    fake = FakeProviders({"gemini/flash": [RateLimited()]})
    res = make_router(fake, db=db).call("extract", MSGS, SCHEMA, needs_profile=False)
    assert res.model == "groq/open"
    # A new router (next run, same day) skips gemini without calling it.
    fake2 = FakeProviders()
    res2 = make_router(fake2, db=db).call(
        "extract", [{"role": "user", "content": "other"}], SCHEMA, needs_profile=False
    )
    assert fake2.models() == ["groq/open"] and res2.attempts[0].outcome == "skipped"


def test_short_retry_after_is_a_brief_backoff(make_router, rcfg, tmp_path):
    rcfg.routing.extract = ["free-cloud"]
    db, clock = connect(tmp_path / "router.db"), Clock()
    make_router(FakeProviders({"gemini/flash": [RateLimited(retry_after=30)]}), db=db, clock=clock).call(
        "extract", MSGS, SCHEMA, needs_profile=False
    )
    clock.t += 31
    fake = FakeProviders()
    make_router(fake, db=db, clock=clock).call("extract", PUBLIC, SCHEMA, needs_profile=False)
    assert fake.models() == ["gemini/flash"]


def test_daily_budget_exhaustion_skips_endpoint(make_router, rcfg, tmp_path):
    rcfg.routing.extract = ["free-cloud"]
    clock = Clock()
    db = connect(tmp_path / "router.db")
    r = make_router(FakeProviders(), db=db, clock=clock)
    for _ in range(3):
        record_call(db, "gemini/flash", r._today())
    res = r.call("extract", MSGS, SCHEMA, needs_profile=False)
    assert res.model == "groq/open"
    assert res.attempts[0].outcome == "skipped" and "budget" in res.attempts[0].detail


def test_missing_key_skips_endpoint(make_router, rcfg):
    rcfg.routing.extract = ["free-cloud", "paid-cheap"]
    fake = FakeProviders()
    res = make_router(fake, env={"GROQ_API_KEY": "q"}).call("extract", MSGS, SCHEMA, needs_profile=False)
    assert fake.models() == ["groq/open"] and res.model == "groq/open"


def test_rpm_pacing_waits_instead_of_failing(make_router, rcfg):
    rcfg.routing.extract = ["free-cloud"]
    clock = Clock()
    fake = FakeProviders()
    r = make_router(fake, clock=clock)
    for i in range(3):
        r.call("extract", [{"role": "user", "content": f"q{i}"}], SCHEMA, needs_profile=False)
    assert fake.models() == ["gemini/flash"] * 3
    assert clock.slept == [60]  # rpm=2: the third call waited for the window


def test_cache_hit_spends_nothing(make_router, tmp_path):
    db = connect(tmp_path / "router.db")
    fake = FakeProviders()
    make_router(fake, db=db).call("extract", MSGS, SCHEMA, needs_profile=False)
    res = make_router(fake, db=db).call("extract", MSGS, SCHEMA, needs_profile=False)
    assert len(fake.requests) == 1 and res.cached and res.data == {"n": 3}
    other = make_router(fake, db=db).call("judge", MSGS, SCHEMA, needs_profile=False)
    assert not other.cached  # cache key includes the task


def test_failures_are_not_cached(make_router, tmp_path):
    db = connect(tmp_path / "router.db")
    fake = FakeProviders({"ollama_chat/small": ["bad", "bad"]})
    make_router(fake, db=db).call("extract", MSGS, SCHEMA, needs_profile=False)
    row = db.execute(
        "SELECT 1 FROM cache WHERE model = 'ollama_chat/small' AND prompt_hash = ?",
        (prompt_hash(MSGS, SCHEMA),),
    ).fetchone()
    assert row is None


def test_accept_rejection_escalates(make_router):
    fake = FakeProviders({"ollama_chat/small": ['{"n": 1}']}, default='{"n": 4}')
    res = make_router(fake).call(
        "extract", MSGS, SCHEMA, needs_profile=False, accept=lambda d: "low" if d["n"] < 2 else None
    )
    assert res.data == {"n": 4} and res.tier == "free-cloud"
    assert res.attempts[0].outcome == "rejected"


def test_all_tiers_exhausted_raises_with_attempts(make_router, rcfg):
    rcfg.routing.extract = ["local"]
    fake = FakeProviders({"ollama_chat/small": [ConnectionError("refused")]})
    with pytest.raises(RouterExhausted) as e:
        make_router(fake).call("extract", MSGS, SCHEMA, needs_profile=False)
    assert [a.outcome for a in e.value.attempts] == ["error"]


@pytest.mark.parametrize("task", ["triage", "extract", "judge"])
def test_profile_never_reaches_disallowed_tiers(make_router, task):
    """Privacy (plan-models §1.4): force every allowed tier to fail and capture what the others received."""
    fake = FakeProviders(
        {"ollama_chat/small": [RuntimeError("x")], "ollama_chat/big": [RuntimeError("x")]}, default="garbage"
    )
    claude = FakeClaude()
    with pytest.raises(RouterExhausted):
        make_router(
            fake, env={"GEMINI_API_KEY": "g", "GROQ_API_KEY": "q", "OPENAI_API_KEY": "o"}, claude=claude
        ).call(task, MSGS, SCHEMA, needs_profile=True, public_messages=PUBLIC)
    if task == "triage":
        assert claude.calls and all(PROFILE not in json.dumps(c) for c in claude.calls)
    outside = [r for r in fake.requests if not r["model"].startswith("ollama_chat/")]
    assert outside, "test must exercise at least one non-local tier"
    assert all(PROFILE not in json.dumps(r["messages"]) for r in outside)
    assert any(
        r["model"].startswith("ollama_chat/") and PROFILE in json.dumps(r["messages"]) for r in fake.requests
    )


def test_disallowed_tier_skipped_without_public_messages(make_router):
    fake = FakeProviders({"ollama_chat/big": [RuntimeError("x")]})
    with pytest.raises(RouterExhausted) as e:
        make_router(fake).call("triage", MSGS, SCHEMA, needs_profile=True)
    assert fake.models() == ["ollama_chat/big"]
    assert e.value.attempts[-1].outcome == "skipped" and "profile" in e.value.attempts[-1].detail


def test_claude_cli_endpoint_runs_without_tools_and_counts_budget(make_router, rcfg, tmp_path):
    rcfg.routing.triage = ["subscription"]
    claude = FakeClaude(
        [AgentResult(True, structured={"n": 4}, usage={"input_tokens": 7, "output_tokens": 2})]
    )
    r = make_router(FakeProviders(), claude=claude)
    res = r.call("triage", [{"role": "system", "content": "SYS"}, *PUBLIC], SCHEMA, needs_profile=False)
    assert res.data == {"n": 4} and res.model == "claude-cli/haiku" and res.tokens["prompt_tokens"] == 7
    call = claude.calls[0]
    assert call["tools"] == [] and call["model"] == "haiku" and call["thinking"] is False
    assert (
        call["system_prompt"] == "SYS" and call["prompt"] == PUBLIC[0]["content"] and call["schema"] == SCHEMA
    )
    from jobsearch.usage import used_today

    assert used_today(r.db, "claude-cli/haiku", r._today()) == 1


def test_claude_cli_daily_cap_is_respected(make_router, rcfg, tmp_path):
    rcfg.routing.triage = ["subscription"]
    claude = FakeClaude()
    r = make_router(FakeProviders(), claude=claude)
    for _ in range(5):
        record_call(r.db, "claude-cli/haiku", r._today())
    with pytest.raises(RouterExhausted) as e:
        r.call("triage", PUBLIC, SCHEMA, needs_profile=False)
    assert not claude.calls and "budget" in e.value.attempts[0].detail


def test_claude_usage_limit_counts_as_rate_limit(make_router, rcfg, tmp_path):
    rcfg.routing.triage = ["subscription", "local-large"]
    db = connect(tmp_path / "router.db")
    claude = FakeClaude([UsageLimitError("usage limit reached")])
    res = make_router(FakeProviders(), db=db, claude=claude).call(
        "triage", PUBLIC, SCHEMA, needs_profile=False
    )
    assert res.model == "ollama_chat/big" and res.attempts[0].outcome == "rate-limited"
    claude2 = FakeClaude()
    make_router(FakeProviders(), db=db, claude=claude2).call("triage", MSGS, SCHEMA, needs_profile=False)
    assert not claude2.calls  # stays off until tomorrow


def test_claude_cli_schema_retry_flattens_conversation(make_router, rcfg):
    rcfg.routing.triage = ["subscription"]
    claude = FakeClaude([AgentResult(True, structured={"n": 9}), AgentResult(True, structured={"n": 2})])
    res = make_router(FakeProviders(), claude=claude).call("triage", PUBLIC, SCHEMA, needs_profile=False)
    assert res.data == {"n": 2}
    retry = claude.calls[1]["prompt"]
    assert "[user]" in retry and "[assistant]" in retry and "invalid" in retry


def test_claude_cli_failure_moves_on(make_router, rcfg):
    rcfg.routing.triage = ["subscription", "local-large"]
    claude = FakeClaude([AgentResult(False, error="timed out after 900s")])
    res = make_router(FakeProviders(), claude=claude).call("triage", PUBLIC, SCHEMA, needs_profile=False)
    assert res.attempts[0].outcome == "error" and "timed out" in res.attempts[0].detail
    assert res.model == "ollama_chat/big"


def test_codex_cli_used_for_triage_with_config_overrides(make_router, rcfg):
    rcfg.providers.subscription = [
        Endpoint(model="codex-cli/default", rpd=5, extra={"model_reasoning_effort": "low"})
    ]
    rcfg.routing.triage = ["subscription"]
    calls = []

    def codex(cfg, **kw):
        calls.append(kw)
        return AgentResult(True, structured={"n": 5}, usage={"input_tokens": 3, "output_tokens": 1})

    r = make_router(FakeProviders())
    r._codex_agent = codex
    res = r.call("triage", PUBLIC, SCHEMA, needs_profile=False)
    assert res.data == {"n": 5} and res.model == "codex-cli/default"
    assert calls[0]["model"] == "default" and calls[0]["config"] == {"model_reasoning_effort": "low"}
    assert "tools" not in calls[0]


def test_codex_never_gets_web_text_tasks(make_router, rcfg):
    rcfg.providers.subscription = [Endpoint(model="codex-cli/default")]
    rcfg.routing.extract = ["subscription", "local"]
    r = make_router(FakeProviders())
    r._codex_agent = lambda cfg, **kw: pytest.fail("codex must not be called for extract")
    res = r.call("extract", PUBLIC, SCHEMA, needs_profile=False)
    assert res.attempts[0].outcome == "skipped" and "shell" in res.attempts[0].detail


def test_json_object_mode_puts_schema_in_prompt_and_still_validates(make_router, rcfg):
    rcfg.providers.local = [Endpoint(model="deepseek/flash", json_mode="object")]
    rcfg.routing.extract = ["local", "free-cloud"]
    fake = FakeProviders({"deepseek/flash": ['{"n": 7}', '{"n": 8}']})
    res = make_router(fake).call("extract", MSGS, SCHEMA, needs_profile=False)
    first = fake.requests[0]
    assert first["response_format"] == {"type": "json_object"}
    assert first["messages"][0]["role"] == "system" and '"maximum":5' in first["messages"][0]["content"]
    assert res.model == "gemini/flash"  # out-of-range answers still fail validation and escalate


def test_pin_endpoint_routes_task_to_one_model(rcfg):
    from jobsearch.config import pin_endpoint

    pin_endpoint(rcfg, "triage", "groq/open")
    assert rcfg.routing.triage == ["free-cloud"] and [e.model for e in rcfg.providers.free_cloud] == [
        "groq/open"
    ]
    with pytest.raises(ValueError, match="not a configured endpoint"):
        pin_endpoint(rcfg, "triage", "nope/x")
