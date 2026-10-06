"""Evidence gathering (plan-models.md §4 step 1) against a fake SearXNG and fake pages."""

import json
from urllib.parse import parse_qs

import httpx

from jobsearch.evidence import gather, render, search_name, skip_url

ARTICLE = "<html><head><title>{t}</title></head><body><article><h1>{t}</h1>{p}</article></body></html>"
PARA = (
    "<p>"
    + "Acme makes inventory software for grocery co-ops and hires remote engineers across the US. " * 6
    + "</p>"
)


def handler(seen):
    def h(request: httpx.Request):
        seen.append(str(request.url))
        if request.url.host == "127.0.0.1":
            q = parse_qs(request.url.query.decode())["q"][0]
            results = [
                {"url": "https://www.linkedin.com/company/acme", "title": "LinkedIn"},
                {"url": "https://acme.example/careers", "title": "dupe of careers page"},
                {"url": f"https://news.example/{abs(hash(q)) % 1000}", "title": f"News for {q}"},
                {"url": "https://acme.example/report.pdf", "title": "PDF"},
                {"url": "https://www.facebook.com/acme", "title": "FB"},
                {"url": "https://acme.example/tiny", "title": "Tiny"},
            ]
            return httpx.Response(200, json={"results": results})
        if request.url.path == "/tiny":
            return httpx.Response(200, text="<html><body>Login</body></html>")
        if request.url.path == "/careers":
            body = PARA + '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acme"></iframe>'
            return httpx.Response(200, text=ARTICLE.format(t="Careers", p=body))
        return httpx.Response(200, text=ARTICLE.format(t="News", p=PARA))

    return h


def rec(**t):
    tracker = {
        "company_id": "C1",
        "company": "Acme / Acme Labs",
        "career_url": "https://acme.example/careers",
    } | t
    return {"company_id": "C1", "tracker": tracker, "ats": None}


def test_search_name():
    assert search_name("Roll20 / Demiplane") == "Roll20"
    assert search_name("Acme (formerly Foo)") == "Acme"
    assert search_name("Plain Co") == "Plain Co"


def test_skip_url():
    assert skip_url("https://www.linkedin.com/in/x")
    assert skip_url("https://m.facebook.com/acme")
    assert skip_url("https://acme.example/file.PDF")
    assert skip_url("mailto:a@b.c")
    assert not skip_url("https://acme.example/careers")
    assert not skip_url(
        "https://example.com/x.company"
    )  # endswith check is on the path, not a substring of host


def test_gather_builds_capped_numbered_pack(cfg, make_client, tmp_path, clock):
    seen = []
    cfg.evidence.max_pages = 3
    cfg.evidence.max_chars_per_page = 500
    cfg.evidence.max_total_chars = 1200
    with make_client(handler(seen)) as client:
        pack = gather(rec(), client, cfg, cache_dir=tmp_path / "ev", clock=clock)
    assert pack.searches == 4
    assert [s.id for s in pack.sources] == ["S1", "S2", "S3"]
    assert pack.sources[0].url == "https://acme.example/careers"  # careers page first
    assert all(len(s.text) <= 500 for s in pack.sources)
    assert sum(len(s.text) for s in pack.sources) <= 1200
    assert not any("linkedin" in u or "facebook" in u or u.endswith(".pdf") for u in seen)
    assert ("greenhouse", "acme") in pack.ats_candidates
    queries = [parse_qs(httpx.URL(u).query.decode())["q"][0] for u in seen if "127.0.0.1" in u]
    assert queries[0] == '"Acme" careers'


def test_tiny_pages_are_skipped(cfg, make_client, tmp_path, clock):
    with make_client(handler([])) as client:
        pack = gather(rec(career_url=None), client, cfg, cache_dir=tmp_path / "ev", clock=clock)
    assert all(s.url != "https://acme.example/tiny" for s in pack.sources)
    assert any("too little text" in f for f in pack.failures)


def test_pack_is_cached_for_cache_hours(cfg, make_client, tmp_path, clock):
    seen = []
    with make_client(handler(seen)) as client:
        first = gather(rec(), client, cfg, cache_dir=tmp_path / "ev", clock=clock)
        n = len(seen)
        again = gather(rec(), client, cfg, cache_dir=tmp_path / "ev", clock=clock)
        assert len(seen) == n and again.sources == first.sources
        clock.now += cfg.http.cache_hours * 3600 + 1
        json.loads((tmp_path / "ev" / "C1.json").read_text(encoding="utf-8"))
        gather(rec(), client, cfg, cache_dir=tmp_path / "ev", clock=clock, use_cache=True)


def test_board_data_becomes_a_source(cfg, make_client, tmp_path, clock):
    r = rec()
    r["ats"] = {"type": "greenhouse", "token": "acme", "confirmed": True,
                "board_url": "https://job-boards.greenhouse.io/acme"}  # fmt: skip
    jobs = [{"title": "Senior SDET", "location_text": "Remote - US", "work_mode": "Remote", "url": "https://x/1"},
            {"title": "Old QA", "closed_at": "2026-09-01", "url": "https://x/2"}]  # fmt: skip
    with make_client(handler([])) as client:
        pack = gather(r, client, cfg, jobs=jobs, cache_dir=tmp_path / "ev", clock=clock)
    s1 = pack.sources[0]
    assert s1.url == "https://job-boards.greenhouse.io/acme"
    assert "Senior SDET | Remote - US | Remote | https://x/1" in s1.text and "Old QA" not in s1.text


def test_render_fences_sources():
    from jobsearch.evidence import Pack, Source

    text = render(Pack("C1", 0, [Source("S1", "https://a", "T", "2026-10-05", "body")]))
    assert text.startswith('<source id="S1" url="https://a"') and text.endswith("</source>")
    assert render(Pack("C1", 0, [])) == "(no sources could be fetched)"
