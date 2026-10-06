"""Load and validate config.yaml."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = ROOT / "config.yaml"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Models(_Strict):
    triage: str = ""
    verify: str = ""


class TriageCfg(_Strict):
    batch_size: int = Field(20, ge=1)
    timeout_s: int = Field(900, ge=10)  # per model call; local-large is slow


class VerifyCfg(_Strict):
    per_run: int = Field(40, ge=1)
    max_minutes: int = Field(120, ge=1)
    timeout_s: int = Field(360, ge=10)
    refresh_days: int = Field(45, ge=1)


class DetectCfg(_Strict):
    recheck_days: int = Field(30, ge=1)


class WatchCfg(_Strict):
    interval_hours: int = Field(6, ge=1)
    workday_enabled: bool = False
    workday_terms: list[str] = ["quality", "test", "SDET", "QA"]


class Filters(_Strict):
    title_include: str
    title_exclude: str
    maine_terms: list[str]
    remote_terms: list[str]
    us_terms: list[str]


class HttpCfg(_Strict):
    min_interval_s_per_host: float = Field(1.0, ge=1.0)
    cache_hours: float = Field(6, ge=0)


class Ids(_Strict):
    job_prefix: str = "JW"
    person_prefix: str = "PA"


# subscription: the owner's logged-in Claude Code (`claude-cli/<model>`), capped per day by rpd.
TIERS = ("local", "local-large", "free-cloud", "subscription", "paid-cheap")
Tier = Literal["local", "local-large", "free-cloud", "subscription", "paid-cheap"]


class Endpoint(_Strict):
    model: str  # LiteLLM model string ("ollama_chat/qwen3.5:9b", "gemini/<model>") or "claude-cli/<model>"
    base_url: str | None = None
    key_env: str | None = None  # env var holding the API key; endpoint is skipped when it is unset
    rpm: int = Field(0, ge=0)  # 0 = unlimited
    rpd: int = Field(0, ge=0)
    monthly_usd_cap: float | None = Field(None, ge=0)
    # Provider-specific completion kwargs, e.g. {think: false, num_ctx: 16384} for Ollama.
    extra: dict[str, Any] = {}
    # schema: provider enforces the JSON Schema. object: provider only has plain JSON mode (DeepSeek), so the
    # schema goes in the prompt. The router validates every reply against the schema either way.
    json_mode: Literal["schema", "object"] = "schema"


class Providers(_Strict):
    local: list[Endpoint] = []
    local_large: list[Endpoint] = Field([], alias="local-large")
    free_cloud: list[Endpoint] = Field([], alias="free-cloud")
    subscription: list[Endpoint] = []
    paid_cheap: list[Endpoint] = Field([], alias="paid-cheap")

    def tier(self, name: str) -> list[Endpoint]:
        return getattr(self, name.replace("-", "_"))


class Routing(_Strict):
    posting_relevance: list[Tier] = ["paid-cheap", "local"]
    triage: list[Tier] = ["free-cloud", "subscription", "local-large"]
    extract: list[Tier] = ["local", "free-cloud", "paid-cheap"]
    judge: list[Tier] = ["local", "paid-cheap"]


class SearchCfg(_Strict):
    searxng_url: str = "http://127.0.0.1:8888"
    fallback_api: str | None = None


class RankCfg(_Strict):
    per_run: int = Field(60, ge=1)  # job scorer calls per run
    max_chars: int = Field(6000, ge=500)  # posting text sent to the model
    timeout_s: int = Field(300, ge=10)


class EvidenceCfg(_Strict):
    max_pages: int = Field(8, ge=1)
    max_chars_per_page: int = Field(6000, ge=500)
    max_total_chars: int = Field(30000, ge=1000)


class Config(_Strict):
    runtime: Literal["local", "gitlab"] = "local"
    auth: Literal["claude-code", "api-key"] = "claude-code"
    contact_email: str = ""
    models: Models = Models()
    triage: TriageCfg = TriageCfg()
    verify: VerifyCfg = VerifyCfg()
    detect: DetectCfg = DetectCfg()
    watch: WatchCfg = WatchCfg()
    filters: Filters
    http: HttpCfg = HttpCfg()
    ids: Ids = Ids()
    providers: Providers = Providers()
    profile_allowed_tiers: list[Tier] = ["local", "local-large"]
    routing: Routing = Routing()
    search: SearchCfg = SearchCfg()
    evidence: EvidenceCfg = EvidenceCfg()
    rank: RankCfg = RankCfg()


def load_config(path: Path | None = None) -> Config:
    path = path or DEFAULT_CONFIG
    with open(path, encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}
    return Config.model_validate(raw)


def pin_endpoint(cfg: Config, task: str, model: str) -> None:
    """Route `task` to exactly one configured endpoint (comparison runs). Profile rules still apply to its tier."""
    for tier in TIERS:
        for ep in cfg.providers.tier(tier):
            if ep.model == model:
                setattr(cfg.providers, tier.replace("-", "_"), [ep])
                setattr(cfg.routing, task, [tier])
                return
    known = [ep.model for t in TIERS for ep in cfg.providers.tier(t)]
    raise ValueError(f"{model!r} is not a configured endpoint; choose from {known}")
