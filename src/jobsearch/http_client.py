"""Polite HTTP client (plan hard rule 7).

- at most one request per `min_interval_s_per_host` per host
- descriptive User-Agent with the owner's contact email
- retry with exponential backoff on 429, 5xx and connection errors
- on-disk response cache for `cache_hours`
- refuses linkedin.com outright (hard rule 2)
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

from jobsearch import __version__
from jobsearch.config import ROOT, Config

CACHE_DIR = ROOT / "cache" / "http"
RETRY_STATUSES = {429, 500, 502, 503, 504}
MAX_BODY_BYTES = 8_000_000


class BlockedHostError(ValueError):
    pass


def is_blocked_host(url: str) -> bool:
    host = (urlsplit(url).hostname or "").lower()
    return host == "linkedin.com" or host.endswith(".linkedin.com")


@dataclass
class Response:
    url: str  # final URL after redirects
    status: int
    text: str
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 300

    def json(self) -> Any:
        return json.loads(self.text)


class PoliteClient:
    def __init__(
        self,
        cfg: Config,
        cache_dir: Path = CACHE_DIR,
        transport: httpx.BaseTransport | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        max_attempts: int = 4,
        backoff_s: float = 2.0,
        timeout_s: float = 20.0,
    ):
        if not cfg.contact_email:
            raise ValueError("config.yaml contact_email must be set before making HTTP requests")
        self.min_interval = cfg.http.min_interval_s_per_host
        self.cache_s = cfg.http.cache_hours * 3600
        self.cache_dir = cache_dir
        self.clock, self.wall_clock, self.sleep = clock, wall_clock, sleep
        self.max_attempts, self.backoff_s = max_attempts, backoff_s
        self.user_agent = (
            f"jobsearch-research/{__version__} (personal job search; +mailto:{cfg.contact_email})"
        )
        self._last_hit: dict[str, float] = {}
        self.stats = {"requests": 0, "cache_hits": 0, "retries": 0}
        self._client = httpx.Client(
            headers={"User-Agent": self.user_agent, "Accept": "application/json, text/html;q=0.9, */*;q=0.5"},
            follow_redirects=True,
            timeout=timeout_s,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> PoliteClient:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- cache ------------------------------------------------------------------------------------

    def _cache_path(self, method: str, url: str, body: Any) -> Path:
        key = json.dumps([method, url, body], sort_keys=True)
        return self.cache_dir / f"{hashlib.sha256(key.encode()).hexdigest()}.json"

    def _cache_get(self, path: Path) -> Response | None:
        if self.cache_s <= 0 or not path.exists():
            return None
        try:
            entry = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if self.wall_clock() - entry["fetched_at"] > self.cache_s:
            return None
        return Response(entry["url"], entry["status"], entry["text"], from_cache=True)

    def _cache_put(self, path: Path, resp: Response) -> None:
        if self.cache_s <= 0:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {"fetched_at": self.wall_clock(), "url": resp.url, "status": resp.status, "text": resp.text}
        path.write_text(json.dumps(entry), encoding="utf-8")

    # --- requests ---------------------------------------------------------------------------------

    def _wait_for_host(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = self.min_interval - (self.clock() - last)
            if wait > 0:
                self.sleep(wait)
        self._last_hit[host] = self.clock()

    def request(self, method: str, url: str, json_body: Any = None, use_cache: bool = True) -> Response:
        """Return a Response for any HTTP status; raise httpx.HTTPError if every attempt failed."""
        if is_blocked_host(url):
            raise BlockedHostError(f"refusing to fetch {url}: LinkedIn is off-limits")
        cache_path = self._cache_path(method, url, json_body)
        if use_cache and (cached := self._cache_get(cache_path)):
            self.stats["cache_hits"] += 1
            return cached

        host = (urlsplit(url).hostname or "").lower()
        last_error: Exception | None = None
        resp: Response | None = None
        for attempt in range(1, self.max_attempts + 1):
            if attempt > 1:
                self.stats["retries"] += 1
                self.sleep(self.backoff_s * 2 ** (attempt - 2))
            self._wait_for_host(host)
            self.stats["requests"] += 1
            try:
                r = self._client.request(method, url, json=json_body)
            except httpx.TransportError as e:
                last_error = e
                continue
            final = str(r.url)
            if is_blocked_host(final):
                raise BlockedHostError(f"{url} redirected to LinkedIn; not reading it")
            resp = Response(
                final, r.status_code, r.content[:MAX_BODY_BYTES].decode(r.encoding or "utf-8", "replace")
            )
            if r.status_code in RETRY_STATUSES:
                if retry_after := _retry_after_s(r):
                    self.sleep(min(retry_after, 120))
                continue
            self._cache_put(cache_path, resp)
            return resp
        if resp is not None:
            return resp  # still failing after retries; returned uncached
        assert last_error is not None
        raise last_error

    def get(self, url: str, **kw) -> Response:
        return self.request("GET", url, **kw)

    def post_json(self, url: str, body: Any, **kw) -> Response:
        return self.request("POST", url, json_body=body, **kw)


def _retry_after_s(r: httpx.Response) -> float | None:
    value = r.headers.get("Retry-After")
    if value and value.strip().isdigit():
        return float(value)
    return None
