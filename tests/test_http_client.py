import httpx
import pytest

from jobsearch.http_client import BlockedHostError, PoliteClient, is_blocked_host


def ok(text="ok", status=200, headers=None):
    return lambda request: httpx.Response(status, text=text, headers=headers)


def test_requires_contact_email(cfg):
    cfg.contact_email = ""
    with pytest.raises(ValueError, match="contact_email"):
        PoliteClient(cfg)


def test_user_agent_identifies_owner(make_client):
    seen = []

    def handler(request):
        seen.append(request.headers["User-Agent"])
        return httpx.Response(200, text="hi")

    make_client(handler).get("https://a.example/")
    assert "jobsearch-research/" in seen[0]
    assert "mailto:owner+test@example.com" in seen[0]


def test_one_request_per_second_per_host(make_client, clock):
    times = []

    def handler(request):
        times.append((request.url.host, clock.now))
        return httpx.Response(200, text="x")

    c = make_client(handler)
    for path in ("1", "2", "3"):
        c.get(f"https://a.example/{path}", use_cache=False)
    c.get("https://b.example/1", use_cache=False)  # different host: no wait

    a_times = [t for h, t in times if h == "a.example"]
    assert all(b - a >= 1.0 for a, b in zip(a_times, a_times[1:], strict=False))
    assert times[-1][1] == a_times[-1]  # b.example went immediately


def test_cache_hit_within_ttl_and_refetch_after(make_client, clock, cfg):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(200, text=f"v{len(calls)}")

    c = make_client(handler)
    assert c.get("https://a.example/x").text == "v1"
    second = c.get("https://a.example/x")
    assert second.text == "v1" and second.from_cache
    clock.now += cfg.http.cache_hours * 3600 + 1
    assert c.get("https://a.example/x").text == "v2"
    assert len(calls) == 2


def test_cache_keys_on_post_body(make_client):
    def handler(request):
        return httpx.Response(200, text=request.content.decode())

    c = make_client(handler)
    assert c.post_json("https://a.example/s", {"q": "qa"}).text == '{"q":"qa"}'
    assert c.post_json("https://a.example/s", {"q": "test"}).text == '{"q":"test"}'


def test_404_is_returned_and_cached(make_client):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(404, text="nope")

    c = make_client(handler)
    assert c.get("https://a.example/x").status == 404
    assert c.get("https://a.example/x").status == 404
    assert len(calls) == 1


def test_retries_with_backoff_then_succeeds(make_client, clock):
    statuses = iter([503, 502, 200])

    def handler(request):
        return httpx.Response(next(statuses), text="body")

    c = make_client(handler)
    r = c.get("https://a.example/x")
    assert r.status == 200
    assert c.stats["retries"] == 2
    assert 2.0 in clock.sleeps and 4.0 in clock.sleeps  # exponential backoff


def test_honours_retry_after(make_client, clock):
    statuses = iter([429, 200])

    def handler(request):
        s = next(statuses)
        return httpx.Response(s, text="x", headers={"Retry-After": "30"} if s == 429 else None)

    make_client(handler).get("https://a.example/x")
    assert 30.0 in clock.sleeps


def test_persistent_5xx_returns_last_response_uncached(make_client):
    calls = []

    def handler(request):
        calls.append(1)
        return httpx.Response(500, text="down")

    c = make_client(handler, max_attempts=3)
    assert c.get("https://a.example/x").status == 500
    assert len(calls) == 3
    c.get("https://a.example/x")
    assert len(calls) == 6  # not served from cache


def test_connection_errors_raise_after_retries(make_client):
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(httpx.ConnectError):
        make_client(handler, max_attempts=2).get("https://a.example/x")


@pytest.mark.parametrize(
    ("url", "blocked"),
    [
        ("https://www.linkedin.com/company/acme", True),
        ("https://linkedin.com/in/someone", True),
        ("https://LinkedIn.com/x", True),
        ("https://uk.linkedin.com/x", True),
        ("https://notlinkedin.com/x", False),
        ("https://linkedin.com.evil.example/x", False),
        ("https://example.com/?u=linkedin.com", False),
    ],
)
def test_is_blocked_host(url, blocked):
    assert is_blocked_host(url) is blocked


def test_linkedin_never_requested(make_client):
    calls = []
    c = make_client(lambda r: calls.append(r) or httpx.Response(200))
    with pytest.raises(BlockedHostError):
        c.get("https://www.linkedin.com/company/acme/people")
    assert calls == []


def test_redirect_to_linkedin_is_refused(make_client):
    def handler(request):
        if request.url.host == "a.example":
            return httpx.Response(302, headers={"Location": "https://www.linkedin.com/company/acme"})
        return httpx.Response(200, text="linkedin page")

    with pytest.raises(BlockedHostError):
        make_client(handler).get("https://a.example/careers")
