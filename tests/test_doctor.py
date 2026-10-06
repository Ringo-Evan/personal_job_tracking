import httpx
import respx

from jobsearch import doctor
from jobsearch.config import Endpoint, load_config
from jobsearch.usage import connect, record_call, used_today


def _db(tmp_path):
    return connect(tmp_path / "router.db")


def test_config_has_routing_and_profile_stays_local():
    cfg = load_config()
    assert cfg.providers.tier("local"), "a local model must be configured"
    assert set(cfg.profile_allowed_tiers) <= {"local", "local-large", "paid-cheap"}
    assert "free-cloud" not in cfg.profile_allowed_tiers


def test_usage_counts_per_day(tmp_path):
    db = _db(tmp_path)
    record_call(db, "gemini/x")
    record_call(db, "gemini/x")
    assert used_today(db, "gemini/x") == 2
    assert used_today(db, "groq/y") == 0


def test_missing_key_is_skipped_not_called(tmp_path, monkeypatch):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    called = []
    c = doctor.check_endpoint(
        Endpoint(model="groq/m", key_env="GROQ_API_KEY"), _db(tmp_path), complete=lambda **k: called.append(k)
    )
    assert c.ok is None and not called


def test_exhausted_budget_is_skipped(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    db = _db(tmp_path)
    record_call(db, "groq/m")
    c = doctor.check_endpoint(
        Endpoint(model="groq/m", key_env="GROQ_API_KEY", rpd=1), db, complete=lambda **k: None
    )
    assert c.ok is None and "budget" in c.detail


def test_cloud_ping_spends_budget_and_reports_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "k")
    db = _db(tmp_path)

    def boom(**k):
        raise RuntimeError("429 rate limited")

    c = doctor.check_endpoint(Endpoint(model="groq/m", key_env="GROQ_API_KEY", rpd=5), db, complete=boom)
    assert c.ok is False and "429" in c.detail
    assert used_today(db, "groq/m") == 1


@respx.mock
def test_ollama_model_not_pulled(tmp_path):
    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "other:7b"}]})
    )
    c = doctor.check_endpoint(
        Endpoint(model="ollama_chat/qwen3.5:9b", base_url="http://localhost:11434"), _db(tmp_path)
    )
    assert c.ok is False and "ollama pull qwen3.5:9b" in c.detail


@respx.mock
def test_ollama_ok_passes_base_url_and_records_nothing(tmp_path):
    respx.get("http://localhost:11434/api/tags").mock(
        return_value=httpx.Response(200, json={"models": [{"name": "qwen3.5:9b"}]})
    )
    seen = {}
    db = _db(tmp_path)
    c = doctor.check_endpoint(
        Endpoint(model="ollama_chat/qwen3.5:9b", base_url="http://localhost:11434"),
        db,
        complete=lambda **k: seen.update(k),
    )
    assert c.ok is True
    assert seen["api_base"] == "http://localhost:11434" and seen["max_tokens"] == 1
    assert used_today(db, "ollama_chat/qwen3.5:9b") == 0


@respx.mock
def test_searxng_json_disabled():
    respx.get("http://127.0.0.1:8888/search").mock(return_value=httpx.Response(403))
    assert "JSON format" in doctor.check_searxng("http://127.0.0.1:8888").detail


@respx.mock
def test_searxng_ok():
    respx.get("http://127.0.0.1:8888/search").mock(
        return_value=httpx.Response(200, json={"results": [{"url": "https://a"}]})
    )
    assert doctor.check_searxng("http://127.0.0.1:8888").ok is True
