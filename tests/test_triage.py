import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from jobsearch.router import Router
from jobsearch.store import Store
from jobsearch.triage import (
    build_messages,
    build_prompt,
    company_line,
    needs_triage,
    run_triage,
    summarize,
    validate_batch,
)
from jobsearch.usage import connect

TODAY, NOW = "2026-10-05", "2026-10-05T09:00:00-04:00"
MODEL = "ollama_chat/big"


def verdict(cid, **kw):
    base = {"id": cid, "why": "Plausible QA roles.", "fit": 3, "stability": 4, "remote": "Medium", "size": "50-500",
            "roles": ["Senior QA", "SDET"], "approach": "Director of Engineering", "flags": ""}  # fmt: skip
    return base | kw


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "data")
    for i in range(1, 46):
        cid = f"C{i:03d}"
        s.save_company({"company_id": cid, "tracker": {"company_id": cid, "company": f"Co {i}", "sector": "Games",
                        "interests": "TTRPGs, fantasy", "research_status": "Not started"}, "history": []})  # fmt: skip
    return s


@pytest.fixture
def tcfg(cfg):
    from jobsearch.config import Endpoint

    c = cfg.model_copy(deep=True)
    c.providers.local_large = [Endpoint(model=MODEL)]
    c.providers.free_cloud = []
    c.routing.triage = ["local-large", "free-cloud"]
    return c


class FakeModel:
    """complete() stand-in answering from the prompt's company lines; `drop` ids are left out.

    fail_first: the first N completions return garbage. limit_on_call: that completion raises a 429."""

    def __init__(self, drop=(), fail_first=0, limit_on_call=None, bad=None):
        self.drop, self.fail_first, self.limit_on_call = set(drop), fail_first, limit_on_call
        self.bad = bad or {}
        self.prompts = []
        self.requests = []

    def __call__(self, **kw):
        self.requests.append(kw)
        prompt = kw["messages"][1]["content"]
        self.prompts.append(prompt)
        n = len(self.prompts)
        if n == self.limit_on_call:
            e = Exception("429 rate limit")
            e.status_code = 429
            raise e
        if n <= self.fail_first:
            content = "garbage"
        else:
            ids = [
                line.split(" | ")[0]
                for line in prompt.split("(id | name | sector | interests)\n")[1].splitlines()
            ]
            content = json.dumps(
                {"companies": [self.bad.get(i, verdict(i)) for i in ids if i not in self.drop]}
            )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
            usage=SimpleNamespace(prompt_tokens=100, completion_tokens=50),
        )


def router(tcfg, tmp_path, fake, name="router.db"):
    return Router(tcfg, db=connect(tmp_path / name), complete=fake, environ={})


def test_prompt_contains_instructions_profile_and_company_lines(store):
    recs = [store.load_company("C001"), store.load_company("C002")]
    p = build_prompt(recs)
    assert "screening employers" in p and "Columbia, Maine" in p
    assert "C001 | Co 1 | Games | TTRPGs, fantasy" in p


def test_trimmed_profile_leaves_out_name_and_employers(store):
    p = build_prompt([store.load_company("C001")], profile_file="profile-trimmed.md")
    assert "SDET" in p and "Maine" in p and "C001 | Co 1" in p
    for personal in ("Evan", "Ringo", "Columbia", "DEKA", "Position Imaging", "AFL", "currently employed"):
        assert personal not in p


def test_messages_have_system_then_user(store):
    m = build_messages([store.load_company("C001")])
    assert [x["role"] for x in m] == ["system", "user"] and "no tools" in m[0]["content"]


def test_company_line_neutralizes_separators_and_newlines():
    rec = {"tracker": {"company_id": "C9", "company": "A | B", "sector": "x\ny", "interests": None}}
    assert company_line(rec) == "C9 | A / B | x y | "


def test_batches_of_20_and_all_triaged(store, tcfg, tmp_path):
    fake = FakeModel()
    r = run_triage(store, tcfg, today=TODAY, now=NOW, router=router(tcfg, tmp_path, fake))
    assert r.calls == 3 and len(fake.requests) == 3  # 45 companies -> 20 + 20 + 5
    assert len(r.triaged) == 45 and r.failed == []
    assert r.models == {MODEL: 45} and r.tokens["completion_tokens"] == 150
    rec = store.load_company("C007")
    assert rec["triage"]["fit"] == 3 and rec["triage"]["model"] == MODEL and rec["triage"]["at"] == TODAY
    assert "id" not in rec["triage"]
    assert rec["history"][-1]["stage"] == "triage"


def test_profile_goes_only_to_allowed_tier(store, tcfg, tmp_path):
    fake = FakeModel()
    run_triage(store, tcfg, today=TODAY, now=NOW, limit=5, router=router(tcfg, tmp_path, fake))
    assert "Columbia, Maine" in fake.prompts[0]  # local-large may see the full profile


def test_missing_ids_retried_once_in_smaller_batch(store, tcfg, tmp_path):
    fake = FakeModel(drop={"C003", "C004"})
    r = run_triage(store, tcfg, today=TODAY, now=NOW, limit=20, router=router(tcfg, tmp_path, fake))
    # first call misses 2, retry call (both in one batch of <=5) misses them again -> failed, no third try
    assert r.calls == 2
    assert sorted(r.failed) == ["C003", "C004"]
    assert fake.prompts[1].count("\nC00") == 2
    assert store.load_company("C003").get("triage") is None


def test_unusable_batch_saves_reject_and_retries_smaller(store, tcfg, tmp_path):
    fake = FakeModel(fail_first=2)  # the router's schema retry also fails -> batch fails, run goes on
    r = run_triage(store, tcfg, today=TODAY, now=NOW, limit=20, router=router(tcfg, tmp_path, fake))
    assert len(r.triaged) == 20  # all 20 recovered via 4 retry batches of 5
    assert r.calls == 5 and len(fake.requests) == 6
    assert len(r.rejects) == 1 and not r.stopped_for_limit
    saved = json.loads(Path(r.rejects[0]).read_text(encoding="utf-8"))
    assert [a["outcome"] for a in saved["attempts"]] == ["schema", "schema"]


def test_resumable_no_duplicate_work(store, tcfg, tmp_path):
    first = FakeModel(limit_on_call=2)
    r1 = run_triage(store, tcfg, today=TODAY, now=NOW, router=router(tcfg, tmp_path, first))
    assert r1.stopped_for_limit and len(r1.triaged) == 20
    second = FakeModel()
    r2 = run_triage(store, tcfg, today=TODAY, now=NOW, router=router(tcfg, tmp_path, second, "next-day.db"))
    assert len(r2.triaged) == 25
    assert not any("C001 |" in p for p in second.prompts)  # already-triaged companies never resent


def test_rerun_of_same_batch_hits_cache(store, tcfg, tmp_path):
    fake = FakeModel()
    run_triage(store, tcfg, today=TODAY, now=NOW, limit=5, dry_run=True, router=router(tcfg, tmp_path, fake))
    r = run_triage(store, tcfg, today=TODAY, now=NOW, limit=5, router=router(tcfg, tmp_path, fake))
    assert len(fake.requests) == 1 and r.cached == 1 and len(r.triaged) == 5


def test_dry_run_saves_nothing_and_includes_triaged(store, tcfg, tmp_path):
    rec = store.load_company("C001")
    rec["triage"] = {"fit": 1}
    store.save_company(rec)
    fake = FakeModel(bad={"C001": verdict("C001", fit=5)})
    r = run_triage(
        store, tcfg, today=TODAY, now=NOW, limit=3, dry_run=True, router=router(tcfg, tmp_path, fake)
    )
    assert sorted(r.results) == ["C001", "C002", "C003"] and r.results["C001"]["fit"] == 5
    assert r.results["C002"]["model"] == MODEL
    assert store.load_company("C001")["triage"] == {"fit": 1}
    assert store.load_company("C002").get("triage") is None


def test_only_not_started_untriaged_and_unskipped(store):
    for cid, change in [("C001", {"research_status": "Initial research"}), ("C002", {"research_status": "Parked"}),
                        ("C003", {"overall_rank": 4})]:  # fmt: skip
        rec = store.load_company(cid)
        rec["tracker"] |= change
        store.save_company(rec)
    rec = store.load_company("C004")
    rec["triage"] = {"fit": 2}
    store.save_company(rec)
    assert [needs_triage(store.load_company(c)) for c in ("C001", "C002", "C003", "C004", "C005")] == [
        False, False, False, False, True]  # fmt: skip
    assert needs_triage(store.load_company("C004"), force=True)
    assert not needs_triage(store.load_company("C002"), force=True)  # force never overrides Parked


@pytest.mark.parametrize(
    ("item", "error_part"),
    [
        (verdict("C001", fit=9), "fit"),
        (verdict("C001", stability=0), "stability"),
        (verdict("C001", remote="Maybe"), "remote"),
        (verdict("C001", size="huge"), "size"),
        (verdict("C001", roles=[]), "roles"),
        (verdict("C001") | {"email": "ceo@example.com"}, "email"),
        (verdict("C999"), "unexpected id"),
    ],
)
def test_validation_rejects_bad_items(item, error_part):
    good, errors = validate_batch({"companies": [item]}, {"C001"}, [])
    assert good == {} and error_part in errors[0]


def test_validation_truncates_long_text_and_logs():
    notes = []
    long_why = " ".join(["word"] * 40)
    good, errors = validate_batch({"companies": [verdict("C001", why=long_why, roles=["a", "b", "c", "d"])]},
                                  {"C001"}, notes)  # fmt: skip
    assert errors == []
    assert len(good["C001"].why.split()) == 25 and len(good["C001"].roles) == 3
    assert any("why truncated" in n for n in notes) and any("roles truncated" in n for n in notes)


def test_duplicate_ids_flagged():
    good, errors = validate_batch({"companies": [verdict("C001"), verdict("C001", fit=1)]}, {"C001"}, [])
    assert good["C001"].fit == 3 and "duplicate" in errors[0]


def test_summary_distribution_and_sample(store, tcfg, tmp_path):
    fake = FakeModel(bad={"C001": verdict("C001", fit=5)})
    run_triage(store, tcfg, today=TODAY, now=NOW, limit=20, router=router(tcfg, tmp_path, fake))
    s = summarize(store, sample=5, seed=1)
    assert s["count"] == 20 and s["models"] == {MODEL: 20}
    assert s["distribution"]["fit"] == {3: 19, 5: 1}
    assert len(s["sample"]) == 5 and {"why", "company", "fit"} <= set(s["sample"][0])
