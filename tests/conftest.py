from pathlib import Path

import httpx
import pytest

from jobsearch.config import load_config
from jobsearch.http_client import PoliteClient

FIXTURES = Path(__file__).parent / "fixtures"


class FakeClock:
    """Monotonic + wall clock whose sleep() just advances time."""

    def __init__(self, start: float = 1_000_000.0):
        self.now = start
        self.sleeps: list[float] = []

    def __call__(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


@pytest.fixture
def cfg():
    c = load_config()
    c.contact_email = "owner+test@example.com"
    return c


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def make_client(cfg, clock, tmp_path):
    """make_client(handler) -> PoliteClient backed by httpx.MockTransport, a temp cache and a fake clock."""
    clients = []

    def _make(handler, **kw):
        c = PoliteClient(
            kw.pop("cfg", cfg),
            cache_dir=tmp_path / "cache",
            transport=httpx.MockTransport(handler),
            clock=clock,
            wall_clock=clock,
            sleep=clock.sleep,
            **kw,
        )
        clients.append(c)
        return c

    yield _make
    for c in clients:
        c.close()


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")
