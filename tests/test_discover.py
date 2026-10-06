"""Board discovery by web search (discover.py): candidate filtering, careers-page pick, name guesses, the run loop."""

import json
from datetime import date

import httpx
import pytest

from conftest import fixture_text
from jobsearch.discover import (
    board_candidates,
    careers_page,
    discover_company,
    discovery_order,
    foreign_site,
    name_matches,
    needs_discovery,
    other_boards,
    run_discover,
    token_matches,
)
from jobsearch.store import Store

TODAY = "2026-10-06"


def record(company, cid="C001", **tracker):
    tracker |= {"company_id": cid, "company": company}
    return {"company_id": cid, "tracker": tracker, "ats": None, "history": []}


def routes(table):
    """MockTransport handler from {url-prefix: (status, body)}; unknown URLs 404. Records requested URLs."""
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        for prefix, (status, body) in table.items():
            if url.startswith(prefix):
                return httpx.Response(status, text=body)
        return httpx.Response(404, text="{}")

    handler.seen = seen
    return handler


def result(url, content=""):
    return {"url": url, "title": "", "content": content}


# --- matching --------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "company", "ok"),
    [
        ("irongalaxystudios", "Iron Galaxy", True),
        ("careersirongalaxystudios", "Iron Galaxy", True),
        ("duckcreek", "Duck Creek", True),
        ("zillowgroup", "Zillow (Housing Connector)", True),
        ("hudl", "Hudl", True),
        ("zoll", "Zoll Medical", True),
        ("wikimedia", "Wikimedia Foundation", True),
        ("ptc", "PTC", True),
        ("mit", "MIT Lincoln Laboratory", False),
        ("acme", "Globex Corporation", False),
        ("data", "Datadog", False),  # short fragments of a longer name don't count
        ("ab", "AB Testing Co", False),
    ],
)
def test_token_matches(token, company, ok):
    assert token_matches(token, company) is ok


@pytest.mark.parametrize(
    ("board_name", "company", "ok"),
    [
        ("Hudl", "Hudl", True),
        ("Agile Six Applications", "Agile Six", True),
        ("Join the FOLX Team!", "Folx Health", True),
        ("Explore Careers at Netlify", "Netlify", True),
        ("Firaxis", "Firaxis Games", True),
        # namesakes adopted by the first discover run (2026-10-05) and reverted after review
        ("Action Garage Doors", "Action Network", False),
        ("NATIONAL", "National Instruments (Emerson)", False),
        ("Brennan Industries", "Brennan Center", False),
        ("Bethesda Physical Therapy", "Bethesda (ZeniMax)", False),
        ("Lex Cooling, Heating, Plumbing and Electric", "Lex", False),
        ("Akuna Capital for MIT students", "MIT Lincoln Laboratory", False),
        ("GOAL", "Goal Zero", False),
    ],
)
def test_board_display_name_must_name_this_company(board_name, company, ok):
    assert name_matches(board_name, company) is ok


def test_careers_page_on_a_foreign_namesake_site_is_ignored():
    results = [result("https://www.online.firstnationalbank.com.gh/careers/")]
    assert careers_page(results, "First National Bank") is None
    assert foreign_site("www.online.firstnationalbank.com.gh") and not foreign_site("careers.zoll.com")


def test_board_candidates_keep_only_boards_named_after_the_company():
    results = [
        result("https://jobs.lever.co/othercorp/123"),  # a different company's board in the results
        result("https://duckcreek.wd1.myworkdayjobs.com/duckcreekcareers"),
        result("https://www.duckcreek.com/careers/", "Apply at boards.greenhouse.io/duckcreek"),
    ]
    got = [(c.type, c.token) for c in board_candidates(results, "Duck Creek")]
    assert got == [("workday", "duckcreek|1|duckcreekcareers"), ("greenhouse", "duckcreek")]


def test_careers_page_skips_aggregators_and_other_domains():
    results = [
        result("https://www.indeed.com/cmp/Iron-Galaxy-Studios"),
        result("https://www.linkedin.com/company/iron-galaxy-studios"),
        result("https://irongalaxy.applytojob.com/"),  # a board, not the careers page
        result("https://www.irongalaxystudios.com/"),  # right domain, not a careers path
        result("https://www.irongalaxystudios.com/careers"),
    ]
    assert careers_page(results, "Iron Galaxy") == "https://www.irongalaxystudios.com/careers"
    assert careers_page(results[:2], "Iron Galaxy") is None


def test_other_boards_reports_unsupported_systems_once():
    results = [result("https://irongalaxy.applytojob.com/"), result("https://irongalaxy.applytojob.com/apply/x")]
    assert other_boards(results, "Iron Galaxy") == ["jazzhr https://irongalaxy.applytojob.com/"]
    assert other_boards([result("https://someoneelse.applytojob.com/")], "Iron Galaxy") == []


# --- one company -------------------------------------------------------------------------------------


def test_board_linked_from_results_is_confirmed_and_adopted(make_client, cfg):
    h = routes({"https://boards-api.greenhouse.io/v1/boards/hudl": (200, fixture_text("boards/greenhouse_board_hudl.json"))})
    d = discover_company(record("Hudl"), make_client(h), cfg, TODAY, [result("https://job-boards.greenhouse.io/hudl")])
    assert (d.ats["type"], d.ats["token"], d.ats["confirmed"], d.ats["detected_by"]) == (
        "greenhouse", "hudl", True, "search"
    )  # fmt: skip
    assert d.ats["source"] is None  # tracker has no careers URL: detect won't treat this as an owner URL change
    assert d.info["found"].startswith("greenhouse:hudl")


def test_greenhouse_board_with_another_companys_name_is_rejected(make_client, cfg):
    body = json.dumps({"name": "Hudl Industries of Elsewhere", "content": ""})
    h = routes({"https://boards-api.greenhouse.io/v1/boards/acme": (200, body)})
    d = discover_company(record("Acme"), make_client(h), cfg, TODAY, [result("https://boards.greenhouse.io/acme")])
    assert d.ats is None


def test_careers_page_scan_finds_embedded_board(make_client, cfg):
    page = '<html><iframe src="https://jobs.ashbyhq.com/saronic?embed=js"></iframe></html>'
    h = routes({
        "https://www.saronic.com/careers": (200, page),
        "https://api.ashbyhq.com/posting-api/job-board/saronic": (200, fixture_text("boards/ashby_board_saronic_2jobs.json")),
    })  # fmt: skip
    d = discover_company(record("Saronic"), make_client(h), cfg, TODAY, [result("https://www.saronic.com/careers")])
    assert (d.ats["type"], d.ats["token"]) == ("ashby", "saronic")
    assert "embedded in https://www.saronic.com/careers" in d.ats["note"]
    assert d.info["careers_page"] == "https://www.saronic.com/careers"


def test_name_guess_on_lever_needs_postings_that_name_the_company(make_client, cfg):
    postings = json.dumps([{"descriptionPlain": "Veeva builds cloud software for life sciences.", "text": "SDET"}])
    h = routes({"https://api.lever.co/v0/postings/veevasystems": (200, postings)})
    d = discover_company(record("Veeva Systems"), make_client(h), cfg, TODAY, [result("https://news.example/x")])
    assert (d.ats["type"], d.ats["token"]) == ("lever", "veevasystems")


def test_name_guess_on_lever_rejected_when_postings_name_someone_else(make_client, cfg):
    stranger = json.dumps([{"descriptionPlain": "We are Globex.", "text": "SDET"}])
    h = routes({"https://api.lever.co/v0/postings/veevasystems": (200, stranger)})
    d = discover_company(record("Veeva Systems"), make_client(h), cfg, TODAY, [result("https://news.example/x")])
    assert d.ats is None


def test_nothing_found_records_the_search(make_client, cfg):
    d = discover_company(record("Obscure Shop"), make_client(routes({})), cfg, TODAY, [result("https://news.example/a")])
    assert d.ats is None
    assert d.info == {"at": TODAY, "results": 1, "careers_page": None, "found": None, "other": []}


# --- queue and run loop ----------------------------------------------------------------------------------


def test_needs_discovery():
    today = date.fromisoformat(TODAY)
    rec = record("Acme")
    assert needs_discovery(rec, today, 30)
    rec["ats"] = {"type": "greenhouse", "confirmed": True}
    assert not needs_discovery(rec, today, 30, force=True)  # a confirmed board is never searched again
    rec["ats"] = {"type": "other", "confirmed": None}
    rec["discovery"] = {"at": "2026-10-01"}
    assert not needs_discovery(rec, today, 30)
    assert needs_discovery(rec, today, 30, force=True)
    rec["discovery"] = {"at": "2026-08-01"}
    assert needs_discovery(rec, today, 30)


def test_discovery_order_best_fit_first_untriaged_as_four():
    recs = [
        {"triage": {"fit": 3, "remote": "High"}},
        {"triage": None},
        {"triage": {"fit": 5, "remote": "Low"}},
        {"triage": {"fit": 4, "remote": "High", "stability": 5}},
    ]
    order = sorted(range(4), key=lambda i: discovery_order(recs[i]))
    assert order == [2, 3, 1, 0]


def test_run_saves_found_board_and_backs_off_when_search_dries_up(make_client, cfg, tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    recs = [record("Hudl", "C1"), record("Nothing Here", "C2"), record("Nothing Either", "C3")]
    for r in recs:
        store.save_company(r)
    answers = {"Hudl": [result("https://job-boards.greenhouse.io/hudl")]}
    monkeypatch.setattr(
        "jobsearch.discover.search", lambda client, cfg, q: answers.get(q.split('"')[1], [])
    )
    h = routes({"https://boards-api.greenhouse.io/v1/boards/hudl": (200, fixture_text("boards/greenhouse_board_hudl.json"))})
    sleeps = []
    rep = run_discover(store, make_client(h), cfg, today=TODAY, now="2026-10-06T09:00:00-04:00", records=recs,
                       pause_s=0, max_dry=2, sleep=sleeps.append)  # fmt: skip
    saved = store.load_company("C1")
    assert saved["ats"]["token"] == "hudl" and saved["discovery"]["found"].startswith("greenhouse:hudl")
    assert saved["history"][-1]["stage"] == "discover"
    assert store.load_company("C2").get("discovery") is None  # empty search: try again next run
    assert rep.counts()["found"] == 1 and rep.no_results == 2
    assert 120 in sleeps  # two empty searches in a row: paused before going on


def test_run_keeps_a_board_saved_meanwhile_by_another_command(make_client, cfg, tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    stale = record("Hudl", "C1")
    fresh = record("Hudl", "C1") | {"ats": {"type": "lever", "token": "hudl", "confirmed": True}, "research": {"x": 1}}
    store.save_company(fresh)  # e.g. verify saved its research and a board after our queue was built
    monkeypatch.setattr("jobsearch.discover.search", lambda c, g, q: [result("https://job-boards.greenhouse.io/hudl")])
    h = routes({"https://boards-api.greenhouse.io/v1/boards/hudl": (200, fixture_text("boards/greenhouse_board_hudl.json"))})
    run_discover(store, make_client(h), cfg, today=TODAY, now="t", records=[stale], pause_s=0, sleep=lambda s: None)
    saved = store.load_company("C1")
    assert saved["ats"]["type"] == "lever" and saved["research"] == {"x": 1}
    assert "kept the board found meanwhile" in saved["discovery"]["found"]
