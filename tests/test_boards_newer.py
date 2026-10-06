"""Workable, Rippling, Breezy, Pinpoint and Recruitee (added 2026-10-06): normalizers on saved live responses, the
two-step fetches, URL detection, API confirmation, and adopting boards an earlier search noted (--recheck-other)."""

import json

import httpx
import pytest

from conftest import FIXTURES
from jobsearch.boards import breezy, fetch_postings, pinpoint, recruitee, rippling, workable
from jobsearch.boards.common import number, pay_period
from jobsearch.detect import Candidate, board_url, confirm, find_candidates
from jobsearch.discover import (
    board_name_ok,
    discover_company,
    promote_stored,
    reject_board,
    stored_board_candidates,
)
from jobsearch.filters import PostingFilter

BOARDS = FIXTURES / "boards"
TODAY = "2026-10-06"
ONFLEET_QA = "3df1f768-e81c-4d36-854b-7db308519340"


def load(name):
    return json.loads((BOARDS / name).read_text(encoding="utf-8"))


def by_id(postings, job_id):
    return next(p for p in postings if p.board_job_id == job_id)


def routes(table):
    """MockTransport handler from {url-prefix: (status, body)}, first match wins; unknown URLs 404."""
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        for prefix, (status, body) in table.items():
            if url.startswith(prefix):
                return httpx.Response(status, text=body if isinstance(body, str) else json.dumps(body))
        return httpx.Response(404, text="{}")

    handler.seen = seen
    return handler


def company(name, cid="C001", **extra):
    return {"company_id": cid, "tracker": {"company": name}, "ats": None, "history": []} | extra


# --- normalizers ---------------------------------------------------------------------------------


def test_workable_marks_remote_locations_and_the_filter_drops_non_us(cfg):
    ps = workable.normalize(load("workable_widget_testronic.json"), "C652", "testronic")
    assert len(ps) == 3
    lqa = by_id(ps, "F44092F173")
    assert (lqa.title, lqa.location_text, lqa.is_remote, lqa.work_mode) == (
        "LQA Game Tester with Japanese", "Remote (Poland)", True, "Remote",
    )  # fmt: skip
    assert lqa.url == "https://apply.workable.com/j/F44092F173"
    assert lqa.posted_at == "2026-07-07"
    assert lqa.description_text and "<p>" not in lqa.description_text
    manila = by_id(ps, "E7CBF203FD")
    assert (manila.location_text, manila.is_remote, manila.work_mode) == (
        "Mandaluyong, National Capital Region, Philippines", False, "On-site",
    )  # fmt: skip
    assert {PostingFilter(cfg).classify(p).verdict for p in ps} == {"drop"}  # Philippines, UK, Poland


def test_rippling_list_is_merged_per_job():
    jobs = rippling.group(load("rippling_jobs_onfleet.json"))
    assert len(jobs) == 2
    assert jobs[ONFLEET_QA]["name"] == "Full-Stack Engineer (Quality & Automation)"
    assert jobs[ONFLEET_QA]["locations"] == ["Brazil", "Mexico", "Canada", "Argentina"]


def test_rippling_detail_gives_description_locations_and_date():
    listed = rippling.group(load("rippling_jobs_onfleet.json"))[ONFLEET_QA]
    p = rippling.normalize_job(
        ONFLEET_QA, listed, load("rippling_detail_onfleet.json"), "C251", "onfleet-careers"
    )
    assert p.location_text == "Mexico; Canada; Argentina; Brazil"
    assert p.posted_at == "2026-09-04"
    assert p.raw_ref == rippling.detail_url("onfleet-careers", ONFLEET_QA)
    assert len(p.description_text) > 1000 and "<div" not in p.description_text


def test_breezy_json_ld_description_and_remote_locations(cfg):
    items = load("breezy_json_aimpoint.json")
    ld = breezy.job_posting_ld((BOARDS / "breezy_page_aimpoint.html").read_text(encoding="utf-8"))
    assert ld["@type"] == "JobPosting"
    flt = PostingFilter(cfg)

    p = breezy.normalize_item(items[0], ld, "C850", "aimpoint-digital")
    assert p.location_text == "Remote (United States); Remote (Colombia)"
    assert (p.is_remote, p.work_mode) == (True, "Remote")
    assert p.description_text.endswith("Applicant location requirements: US")
    assert flt.classify(p).verdict == "eligible"

    anywhere = breezy.normalize_item(items[1], None, "C850", "aimpoint-digital")  # "no location restrictions"
    assert anywhere.location_text == "Remote (anywhere)"
    assert flt.classify(anywhere).verdict == "eligible"

    atlanta = breezy.normalize_item(items[2], None, "C850", "aimpoint-digital")  # remote within Atlanta, GA
    assert atlanta.location_text == "Remote (Atlanta, GA, United States)"
    assert flt.classify(atlanta).verdict == "unknown"  # tied to Georgia: kept for the scorer, not dropped


def test_pinpoint_pay_sections_and_state_list(cfg):
    ps = pinpoint.normalize(load("pinpoint_postings_coforma.json"), "C439", "coforma")
    assert len(ps) == 6
    cm = by_id(ps, "587174")
    assert (cm.pay_low, cm.pay_high, cm.currency, cm.pay_period) == (150235.8, 169146.6, "USD", "year")
    assert (cm.location_text, cm.is_remote, cm.work_mode) == ("Remote (select US states)", True, "Remote")
    assert "Benefits" in cm.description_text
    assert cm.url.startswith("https://coforma.pinpointhq.com/")
    # Coforma's state list leaves out Maine (checked by hand 2026-10-06): never "eligible".
    assert PostingFilter(cfg).classify(by_id(ps, "510809")).verdict == "unknown"


def test_recruitee_pay_periods_location_and_links():
    ps = recruitee.normalize(load("recruitee_offers_intent.json"), "X1", "intent")
    lead = by_id(ps, "2310879")
    assert (lead.title, lead.location_text, lead.work_mode) == (
        "QA Lead", "Remote (Warsaw, Mazowieckie, Poland)", "Remote",
    )  # fmt: skip
    assert (lead.pay_low, lead.pay_high, lead.currency, lead.pay_period) == (20000, 28000, "PLN", "month")
    assert lead.posted_at == "2025-10-15"
    assert lead.url.startswith("https://intent.recruitee.com/o/")
    assert by_id(ps, "739479").pay_period == "hour"  # 90-130 with no period is an hourly rate


@pytest.mark.parametrize(
    ("label", "high", "period"),
    [
        ("year", None, "year"),
        ("Annual", None, "year"),
        ("per hour", None, "hour"),
        ("month", 9000, "month"),
        (None, 130, "hour"),
        (None, 120000, "year"),
        ("week", 2000, None),
        (None, None, None),
    ],
)
def test_pay_period(label, high, period):
    assert pay_period(label, high) == period


def test_number():
    assert (number("23,000"), number(5), number(None), number("n/a"), number(True)) == (
        23000,
        5,
        None,
        None,
        None,
    )


# --- two-step fetches ----------------------------------------------------------------------------


def test_fetch_rippling_gets_detail_only_for_wanted_titles(make_client, cfg):
    base = rippling.api_url("onfleet-careers")
    h = routes({
        f"{base}/{ONFLEET_QA}": (200, load("rippling_detail_onfleet.json")),
        base: (200, load("rippling_jobs_onfleet.json")),
    })  # fmt: skip
    ps = fetch_postings(
        make_client(h), cfg, "rippling", "onfleet-careers", "C251", wanted=lambda t: "Quality" in t
    )
    assert len(ps) == 2
    assert h.seen == [base, f"{base}/{ONFLEET_QA}"]
    assert by_id(ps, ONFLEET_QA).description_text
    other = next(p for p in ps if p.board_job_id != ONFLEET_QA)
    assert (other.description_text, other.location_text) == ("", "Remote (Brazil); Remote (Mexico)")


def test_fetch_breezy_reads_pages_only_for_wanted_titles_and_survives_a_closed_page(make_client, cfg):
    items = load("breezy_json_aimpoint.json")
    page = (BOARDS / "breezy_page_aimpoint.html").read_text(encoding="utf-8")
    api = breezy.api_url("aimpoint-digital")
    h = routes({items[0]["url"]: (200, page), items[1]["url"]: (404, "gone"), api: (200, items)})
    ps = fetch_postings(
        make_client(h, max_attempts=1), cfg, "breezy", "aimpoint-digital", "C850",
        wanted=lambda t: t.startswith(("AI", "Client")),
    )  # fmt: skip
    assert len(ps) == 3
    assert h.seen == [api, items[0]["url"], items[1]["url"]]
    assert by_id(ps, items[0]["id"]).description_text
    assert by_id(ps, items[1]["id"]).description_text == ""


# --- detection and confirmation ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "found"),
    [
        ('<a href="https://apply.workable.com/acme-co/">Jobs</a>', [("workable", "acme-co")]),
        ("https://apply.workable.com/api/v1/widget/accounts/acme?details=true", [("workable", "acme")]),
        ("https://apply.workable.com/j/ABC123DEF", []),  # a job link doesn't name the account
        ("https://Acme.workable.com/jobs/123", [("workable", "acme")]),
        ("https://ats.rippling.com/shippo/jobs/4ce377ff", [("rippling", "shippo")]),
        ("https://ats.rippling.com/en-GB/acme-careers/jobs", [("rippling", "acme-careers")]),
        (
            "https://api.rippling.com/platform/api/ats/v1/board/tive-careers/jobs",
            [("rippling", "tive-careers")],
        ),
        ("https://beta-bionics-inc.breezy.hr/p/492877972d16-assembler", [("breezy", "beta-bionics-inc")]),
        ("https://www.breezy.hr/", []),
        ("https://coforma.pinpointhq.com/en/postings/cdf03ccf", [("pinpoint", "coforma")]),
        ("https://intent.recruitee.com/o/qa-lead", [("recruitee", "intent")]),
    ],
)
def test_find_candidates_newer_boards(text, found):
    assert [(c.type, c.token) for c in find_candidates(text)] == found


@pytest.mark.parametrize(
    ("ats_type", "url"),
    [
        ("workable", "https://apply.workable.com/acme/"),
        ("rippling", "https://ats.rippling.com/acme/jobs"),
        ("breezy", "https://acme.breezy.hr/"),
        ("pinpoint", "https://acme.pinpointhq.com/"),
        ("recruitee", "https://acme.recruitee.com/"),
    ],
)
def test_board_url_newer(ats_type, url):
    assert board_url(ats_type, "acme") == url


def test_confirm_reports_company_name_and_job_count(make_client, cfg):
    h = routes({
        workable.api_url("acme", details=False): (200, {"name": "Acme Robotics", "jobs": [{"title": "QA"}]}),
        rippling.detail_url("shippo", "u1"): (200, {"companyName": "Shippo"}),
        rippling.api_url("shippo"): (200, [{"uuid": "u1", "name": "QA"}, {"uuid": "u1", "name": "QA"},
                                           {"uuid": "u2", "name": "SRE"}]),
        breezy.api_url("acme"): (200, [{"id": "1", "name": "QA", "company": {"name": "Acme Inc"}}]),
        pinpoint.api_url("acme"): (200, {"data": [{"id": 1}, {"id": 2}]}),
        recruitee.api_url("acme"): (200, {"offers": [{"id": 1, "company_name": "Acme"}]}),
    })  # fmt: skip
    c = make_client(h)
    got = {
        t: confirm(c, Candidate(t, tok), cfg)
        for t, tok in [("workable", "acme"), ("rippling", "shippo"), ("breezy", "acme"), ("pinpoint", "acme"),
                       ("recruitee", "acme")]
    }  # fmt: skip
    assert (got["workable"].confirmed, got["workable"].note, got["workable"].job_count) == (
        True,
        "Acme Robotics",
        1,
    )
    assert (got["rippling"].note, got["rippling"].job_count) == (
        "Shippo",
        2,
    )  # u1 is listed once per location
    assert (got["breezy"].note, got["breezy"].job_count) == ("Acme Inc", 1)
    assert (got["pinpoint"].confirmed, got["pinpoint"].job_count) == (True, 2)
    assert (got["recruitee"].note, got["recruitee"].job_count) == ("Acme", 1)
    assert confirm(c, Candidate("breezy", "nobody"), cfg).confirmed is False  # every one of them 404s


# --- namesakes and adopting stored boards --------------------------------------------------------


@pytest.mark.parametrize(
    ("board", "name", "ok"),
    [
        ("Leupold + Stevens, Inc.", "Leupold", True),
        ("Jagex: The RuneScape Company", "Jagex", True),
        ("Feeld", "Feeld", True),
        ("U.S. Digital Response", "US Digital Response", True),
        ("Clever Benefits", "Clever Real Estate", False),
        ("NATIONAL", "National Instruments", False),
        ("Acme Robotics", "Zenith Robotics", False),
    ],
)
def test_board_name_ok(board, name, ok):
    assert board_name_ok(board, name) is ok


def test_search_linked_board_must_name_the_company(make_client, cfg):
    def found(token, board_name):
        # A distinct token per case: the clients share one response cache.
        results = [{"url": f"https://apply.workable.com/{token}/", "title": "", "content": ""}]
        h = routes({workable.api_url(token, details=False): (200, {"name": board_name, "jobs": [{}]})})
        return discover_company(company("Testronic"), make_client(h), cfg, TODAY, results).ats

    assert found("testronic", "Global App Testing") is None
    ats = found("testronic-ltd", "Testronic")
    assert (ats["type"], ats["token"], ats["confirmed"]) == ("workable", "testronic-ltd", True)


def test_promote_stored_picks_the_busiest_confirmed_board(make_client, cfg):
    rec = company("Coforma", "C439", discovery={"at": "2026-10-05", "other": [
        "workable https://apply.workable.com/coforma/", "pinpoint https://coforma.pinpointhq.com/"]})  # fmt: skip
    assert [(c.type, c.token) for c in stored_board_candidates(rec)] == [
        ("workable", "coforma"),
        ("pinpoint", "coforma"),
    ]
    h = routes({
        workable.api_url("coforma", details=False): (200, {"name": "Coforma", "jobs": []}),
        pinpoint.api_url("coforma"): (200, load("pinpoint_postings_coforma.json")),
    })  # fmt: skip
    ats = promote_stored(rec, make_client(h), cfg, TODAY)
    assert (ats["type"], ats["token"], ats["confirmed"], ats["job_count"], ats["detected_by"]) == (
        "pinpoint", "coforma", True, 6, "search",
    )  # fmt: skip
    assert ats["board_url"] == "https://coforma.pinpointhq.com/"


def test_rejected_board_is_cleared_and_never_adopted_again(make_client, cfg):
    rec = company("Sense", "C991", ats={"type": "breezy", "token": "sense-engineering", "confirmed": True},
                  discovery={"other": ["breezy https://sense-engineering.breezy.hr/"]})  # fmt: skip
    assert reject_board(rec, "breezy:sense-engineering") is True
    assert rec["ats"] is None and rec["discovery"]["rejected"] == ["breezy:sense-engineering"]
    assert reject_board(rec, "greenhouse:other") is False  # not the current board: only remembered
    never_searched = company("Acme", discovery=None)
    assert reject_board(never_searched, "lever:acme") is False
    assert never_searched["discovery"] == {"rejected": ["lever:acme"]}
    h = routes({breezy.api_url("sense-engineering"): (200, [{"id": "1", "company": {"name": "Sense"}}])})
    assert promote_stored(rec, make_client(h), cfg, TODAY) is None
    results = [{"url": "https://sense-engineering.breezy.hr/p/1", "title": "", "content": ""}]
    d = discover_company(rec, make_client(h), cfg, TODAY, results)
    assert d.ats is None
    assert d.info["rejected"] == ["breezy:sense-engineering", "greenhouse:other"]  # survives the new search
    assert not [u for u in h.seen if "sense-engineering" in u]  # a ruled-out board isn't even checked


def test_promote_stored_rejects_a_namesake_and_ignores_unsupported_systems(make_client, cfg):
    rec = company("Clever Real Estate", "C542", discovery={"other": [
        "workable https://apply.workable.com/clever-benefits/", "jazzhr https://clever.applytojob.com/apply"]})  # fmt: skip
    assert [c.type for c in stored_board_candidates(rec)] == ["workable"]
    h = routes(
        {workable.api_url("clever-benefits", details=False): (200, {"name": "Clever Benefits", "jobs": [{}]})}
    )
    assert promote_stored(rec, make_client(h), cfg, TODAY) is None
