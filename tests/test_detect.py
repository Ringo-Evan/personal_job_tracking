from datetime import date

import httpx
import pytest

from conftest import fixture_text
from jobsearch.detect import (
    Candidate,
    board_url,
    detect_company,
    find_candidates,
    find_other_ats,
    needs_detection,
)

TODAY = "2026-10-05"

# --- patterns ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # greenhouse
        ("https://boards.greenhouse.io/hudl", [("greenhouse", "hudl")]),
        ("https://boards.greenhouse.io/Axon/jobs/12345?gh_src=x", [("greenhouse", "axon")]),
        ("https://job-boards.greenhouse.io/recidiviz", [("greenhouse", "recidiviz")]),
        ("https://job-boards.eu.greenhouse.io/smartbear/jobs/1", [("greenhouse", "smartbear")]),
        ("https://boards.greenhouse.io/embed/job_board?for=bitfocus", [("greenhouse", "bitfocus")]),
        ("https://boards.greenhouse.io/embed/job_board/js?for=bitfocus", [("greenhouse", "bitfocus")]),
        (
            '<script src="https://boards.greenhouse.io/embed/job_board/js?b=1&amp;for=acme">',
            [("greenhouse", "acme")],
        ),
        ("https://boards-api.greenhouse.io/v1/boards/acme/jobs", [("greenhouse", "acme")]),
        # lever
        ("https://jobs.lever.co/veeva", [("lever", "veeva")]),
        ("https://jobs.lever.co/Veeva/abc-123/apply", [("lever", "veeva")]),
        ("https://api.lever.co/v0/postings/veeva?mode=json", [("lever", "veeva")]),
        # ashby
        ("https://jobs.ashbyhq.com/saronic", [("ashby", "saronic")]),
        ("https://jobs.ashbyhq.com/Halter/1234-uuid", [("ashby", "Halter")]),
        ("https://jobs.ashbyhq.com/acme%20labs?utm=x", [("ashby", "acme labs")]),
        ("https://api.ashbyhq.com/posting-api/job-board/saronic", [("ashby", "saronic")]),
        # smartrecruiters
        ("https://careers.smartrecruiters.com/Visa", [("smartrecruiters", "Visa")]),
        ("https://jobs.smartrecruiters.com/Visa/7438-qa", [("smartrecruiters", "Visa")]),
        # workday
        ("https://acme.wd5.myworkdayjobs.com/en-US/External", [("workday", "acme|5|External")]),
        ("https://acme.wd1.myworkdayjobs.com/Careers/job/Remote/SDET_R1", [("workday", "acme|1|Careers")]),
        ("https://Acme.wd12.myworkdayjobs.com/en-us/Ext_Site", [("workday", "acme|12|Ext_Site")]),
        ("https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs", [("workday", "acme|5|External")]),
        # several, in order of appearance, de-duplicated
        (
            "see jobs.lever.co/foo and boards.greenhouse.io/bar then jobs.lever.co/FOO",
            [("lever", "foo"), ("greenhouse", "bar")],
        ),
        # not boards
        ("https://www.greenhouse.io/blog", []),
        ("https://boards.greenhouse.io/embed/", []),
        ("https://acme.wd5.myworkdayjobs.com/", []),
        ("https://www.hudl.com/jobs", []),
        ("", []),
    ],
)
def test_find_candidates(text, expected):
    assert [(c.type, c.token) for c in find_candidates(text)] == expected


@pytest.mark.parametrize(
    ("ats_type", "token", "url"),
    [
        ("greenhouse", "hudl", "https://job-boards.greenhouse.io/hudl"),
        ("lever", "veeva", "https://jobs.lever.co/veeva"),
        ("ashby", "acme labs", "https://jobs.ashbyhq.com/acme%20labs"),
        ("smartrecruiters", "Visa", "https://careers.smartrecruiters.com/Visa"),
        ("workday", "acme|5|External", "https://acme.wd5.myworkdayjobs.com/External"),
    ],
)
def test_board_url(ats_type, token, url):
    assert board_url(ats_type, token) == url
    # and the board URL round-trips through detection
    assert find_candidates(url) == [Candidate(ats_type, token)]


@pytest.mark.parametrize(
    ("html", "name"),
    [
        ('<a href="https://careers-acme.icims.com/jobs">', "icims"),
        ('<iframe src="https://apply.workable.com/acme/">', "workable"),
        ("https://acme.bamboohr.com/careers", "bamboohr"),
        ("https://recruiting.paylocity.com/recruiting/jobs/All/abc", "paylocity"),
        ("<p>We are hiring!</p>", None),
    ],
)
def test_find_other_ats(html, name):
    assert find_other_ats(html) == name


# --- detect_company against mocked board APIs ----------------------------------------------------


def record(**tracker):
    tracker.setdefault("company_id", "C001")
    return {"company_id": tracker["company_id"], "tracker": tracker, "ats": None, "history": []}


def routes(table):
    """MockTransport handler from {url-prefix: (status, body)}; records requested URLs."""
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append(url)
        for prefix, (status, body) in table.items():
            if url.startswith(prefix):
                return httpx.Response(status, text=body)
        raise AssertionError(f"unexpected request {url}")

    handler.seen = seen
    return handler


GH_HUDL = (
    "https://boards-api.greenhouse.io/v1/boards/hudl",
    (200, fixture_text("boards/greenhouse_board_hudl.json")),
)


def test_url_pattern_confirmed_without_fetching_careers_page(make_client, cfg):
    h = routes(dict([GH_HUDL]))
    rec = record(career_url="https://boards.greenhouse.io/hudl")
    result = detect_company(rec, make_client(h), cfg, TODAY)
    assert result.ats == {
        "type": "greenhouse",
        "token": "hudl",
        "board_url": "https://job-boards.greenhouse.io/hudl",
        "detected_by": "url-pattern",
        "checked_at": TODAY,
        "confirmed": True,
        "source": "https://boards.greenhouse.io/hudl",
        "note": "Hudl",
        "job_count": None,
    }
    assert h.seen == ["https://boards-api.greenhouse.io/v1/boards/hudl"]


def test_pattern_in_research_notes_is_used(make_client, cfg):
    h = routes(
        {
            "https://api.lever.co/v0/postings/veeva": (
                200,
                fixture_text("boards/lever_postings_veeva_1job.json"),
            )
        }
    )
    rec = record(career_url="https://careers.veeva.com", research_notes="Listings at jobs.lever.co/veeva.")
    ats = detect_company(rec, make_client(h), cfg, TODAY).ats
    assert (ats["type"], ats["token"], ats["confirmed"], ats["job_count"]) == ("lever", "veeva", True, 1)


def test_html_scan_finds_embedded_board(make_client, cfg):
    page = '<html><iframe src="https://jobs.ashbyhq.com/saronic?embed=js"></iframe></html>'
    h = routes(
        {
            "https://www.saronic.com/careers": (200, page),
            "https://api.ashbyhq.com/posting-api/job-board/saronic": (
                200,
                fixture_text("boards/ashby_board_saronic_2jobs.json"),
            ),
        }
    )
    ats = detect_company(record(career_url="https://www.saronic.com/careers"), make_client(h), cfg, TODAY).ats
    assert (ats["type"], ats["token"], ats["detected_by"], ats["confirmed"], ats["job_count"]) == (
        "ashby",
        "saronic",
        "html-scan",
        True,
        2,
    )


def test_redirect_target_is_scanned(make_client, cfg):
    def handler(request):
        url = str(request.url)
        if url == "https://acme.example/jobs":
            return httpx.Response(301, headers={"Location": "https://job-boards.greenhouse.io/hudl"})
        if url == "https://job-boards.greenhouse.io/hudl":
            return httpx.Response(200, text="<html>board</html>")
        if url == GH_HUDL[0]:
            return httpx.Response(200, text=GH_HUDL[1][1])
        raise AssertionError(url)

    ats = detect_company(record(career_url="https://acme.example/jobs"), make_client(handler), cfg, TODAY).ats
    assert (ats["type"], ats["token"], ats["confirmed"]) == ("greenhouse", "hudl", True)


def test_api_404_marks_unknown(make_client, cfg):
    h = routes(
        {
            "https://boards-api.greenhouse.io/v1/boards/gone": (
                404,
                fixture_text("boards/greenhouse_404.json"),
            ),
            "https://acme.example/careers": (200, "<p>no board here</p>"),
        }
    )
    rec = record(
        career_url="https://acme.example/careers", research_notes="old link boards.greenhouse.io/gone"
    )
    ats = detect_company(rec, make_client(h), cfg, TODAY).ats
    # 404 on the notes candidate, so the careers page is still scanned
    assert ats["type"] == "other"
    assert ats["note"] == "no known job-board system on careers page; board API 404 for greenhouse:gone"


def test_api_404_with_board_url_as_career_url(make_client, cfg):
    h = routes(
        {
            "https://api.lever.co/v0/postings/gone": (404, fixture_text("boards/lever_404.json")),
            "https://jobs.lever.co/gone": (404, "not found"),
        }
    )
    ats = detect_company(record(career_url="https://jobs.lever.co/gone"), make_client(h), cfg, TODAY).ats
    assert (ats["type"], ats["confirmed"]) == ("unknown", False)
    assert ats["note"] == "careers page HTTP 404; board API 404 for lever:gone"
    assert h.seen.count("https://api.lever.co/v0/postings/gone?mode=json") == 1  # not re-confirmed after scan


def test_stale_link_in_notes_without_careers_page(make_client, cfg):
    h = routes(
        {"https://boards-api.greenhouse.io/v1/boards/gone": (404, fixture_text("boards/greenhouse_404.json"))}
    )
    ats = detect_company(record(research_notes="boards.greenhouse.io/gone"), make_client(h), cfg, TODAY).ats
    assert (ats["type"], ats["confirmed"]) == ("unknown", False)
    assert "greenhouse:gone" in ats["note"]


def test_first_confirmed_candidate_wins(make_client, cfg):
    h = routes(
        {
            "https://boards-api.greenhouse.io/v1/boards/old": (
                404,
                fixture_text("boards/greenhouse_404.json"),
            ),
            GH_HUDL[0]: GH_HUDL[1],
        }
    )
    rec = record(research_notes="boards.greenhouse.io/old moved to boards.greenhouse.io/hudl")
    ats = detect_company(rec, make_client(h), cfg, TODAY).ats
    assert (ats["token"], ats["confirmed"]) == ("hudl", True)


def test_smartrecruiters_zero_postings_is_unconfirmed(make_client, cfg):
    h = routes(
        {
            "https://api.smartrecruiters.com/v1/companies/Ghost/postings": (
                200,
                fixture_text("boards/smartrecruiters_empty.json"),
            )
        }
    )
    ats = detect_company(
        record(career_url="https://careers.smartrecruiters.com/Ghost"), make_client(h), cfg, TODAY
    ).ats
    assert (ats["type"], ats["token"], ats["confirmed"]) == ("smartrecruiters", "Ghost", None)
    assert "unconfirmed" in ats["note"]


def test_smartrecruiters_with_postings_is_confirmed(make_client, cfg):
    body = '{"offset":0,"limit":1,"totalFound":42,"content":[{"id":"1"}]}'
    h = routes({"https://api.smartrecruiters.com/v1/companies/Visa/postings": (200, body)})
    ats = detect_company(
        record(career_url="https://careers.smartrecruiters.com/Visa"), make_client(h), cfg, TODAY
    ).ats
    assert (ats["confirmed"], ats["job_count"]) == (True, 42)


def test_workday_disabled_records_unconfirmed_without_calling_api(make_client, cfg):
    cfg.watch.workday_enabled = False
    h = routes({})
    ats = detect_company(
        record(career_url="https://acme.wd5.myworkdayjobs.com/en-US/External"), make_client(h), cfg, TODAY
    ).ats
    assert (ats["type"], ats["token"], ats["confirmed"]) == ("workday", "acme|5|External", None)
    assert h.seen == []


def test_workday_enabled_posts_to_cxs(make_client, cfg):
    cfg.watch.workday_enabled = True
    h = routes({"https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External/jobs": (200, '{"total": 7}')})
    ats = detect_company(
        record(career_url="https://acme.wd5.myworkdayjobs.com/en-US/External"), make_client(h), cfg, TODAY
    ).ats
    assert (ats["confirmed"], ats["job_count"]) == (True, 7)


def test_other_ats_named_in_note(make_client, cfg):
    h = routes(
        {"https://acme.example/careers": (200, '<a href="https://acme.bamboohr.com/careers">Jobs</a>')}
    )
    ats = detect_company(record(career_url="https://acme.example/careers"), make_client(h), cfg, TODAY).ats
    assert (ats["type"], ats["note"]) == ("other", "uses bamboohr")


def test_careers_page_error_is_unknown(make_client, cfg):
    h = routes({"https://acme.example/careers": (403, "forbidden")})
    ats = detect_company(record(career_url="https://acme.example/careers"), make_client(h), cfg, TODAY).ats
    assert (ats["type"], ats["note"]) == ("unknown", "careers page HTTP 403")


def test_network_failure_is_unknown(make_client, cfg):
    def handler(request):
        raise httpx.ConnectError("dns", request=request)

    c = make_client(handler, max_attempts=1)
    ats = detect_company(record(career_url="https://acme.example/careers"), c, cfg, TODAY).ats
    assert ats["type"] == "unknown" and "ConnectError" in ats["note"]


def test_linkedin_career_url_is_never_fetched(make_client, cfg):
    h = routes({})
    ats = detect_company(
        record(career_url="https://www.linkedin.com/company/acme/jobs"), make_client(h), cfg, TODAY
    ).ats
    assert ats["type"] == "unknown" and "LinkedIn" in ats["note"]
    assert h.seen == []


def test_no_urls_means_nothing_to_check(make_client, cfg):
    h = routes({})
    assert detect_company(record(career_url=None), make_client(h), cfg, TODAY).ats is None
    assert detect_company(record(career_url="see website"), make_client(h), cfg, TODAY).ats is None
    assert h.seen == []


# --- which companies to (re)check ----------------------------------------------------------------


def ats(**kw):
    base = {"type": "greenhouse", "confirmed": True, "detected_by": "url-pattern", "checked_at": "2026-09-01",
            "source": "https://x.example/careers"}  # fmt: skip
    return base | kw


@pytest.mark.parametrize(
    ("tracker", "ats_block", "force", "expected"),
    [
        ({"career_url": "https://x.example/careers"}, None, False, True),
        ({"career_url": "https://x.example/careers"}, ats(), False, False),
        ({"career_url": "https://x.example/careers"}, ats(), True, True),
        ({"career_url": "https://x.example/new"}, ats(), False, True),  # owner changed URL
        (
            {"career_url": "https://x.example/careers"},
            ats(type="other", confirmed=None),
            False,
            True,
        ),  # 34 days
        (
            {"career_url": "https://x.example/careers"},
            ats(type="other", confirmed=None, checked_at="2026-09-20"),
            False,
            False,
        ),
        ({"career_url": "https://x.example/careers"}, ats(detected_by="agent", confirmed=None), False, False),
        ({"career_url": "https://x.example/careers", "research_status": "Parked"}, None, True, False),
        ({"career_url": "https://x.example/careers", "overall_rank": 4}, None, False, False),
    ],
)
def test_needs_detection(tracker, ats_block, force, expected):
    rec = {"tracker": tracker, "ats": ats_block}
    assert needs_detection(rec, date(2026, 10, 5), 30, force) is expected


def test_missing_from_tracker_is_not_checked():
    rec = {"tracker": {"career_url": "https://x"}, "ats": None, "missing_from_tracker": True}
    assert needs_detection(rec, date(2026, 10, 5), 30) is False


# --- one-hop and name-guess fallbacks ------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "types"),
    [
        ('<div class="greenhouse-jobs-index">', ["greenhouse"]),
        ('<div class="lever-job_item">', ["lever"]),
        ("Powered by Lever", ["lever"]),
        ("we leverage data", []),
        ("Apply via Ashby", ["ashby"]),
        ("<p>hello</p>", []),
    ],
)
def test_hinted_types(text, types):
    from jobsearch.detect import hinted_types

    assert hinted_types(text) == types


@pytest.mark.parametrize(
    ("company", "url", "tokens"),
    [
        ("Hudl", "https://www.hudl.com/jobs", ["hudl"]),
        ("Sauce Labs", "https://saucelabs.com/careers", ["saucelabs", "sauce-labs", "sauce"]),
        ("Halter", "https://www.halterhq.com/en-us/careers", ["halter", "halterhq"]),
        (
            "Wizards of the Coast / Hasbro",
            "https://company.wizards.com/careers",
            ["wizardsofcoast", "wizards-of-coast", "wizards"],
        ),
        ("Fenix International (OnlyFans)", "", ["fenixinternational", "fenix-international", "fenix"]),
        ("Kickstarter, PBC", "https://www.kickstarter.com/jobs", ["kickstarter"]),
    ],
)
def test_guess_tokens(company, url, tokens):
    from jobsearch.detect import guess_tokens

    assert guess_tokens(company, url) == tokens


@pytest.mark.parametrize(
    ("board", "company", "ok"),
    [("Axon", "Axon", True), ("Sauce Labs Inc.", "Sauce Labs", True), ("Axon Pharma", "Axon", True),
     ("Hasbro", "Wizards of the Coast / Hasbro", False), ("Acme", "Axon", False), ("", "Axon", False)],
)  # fmt: skip
def test_board_name_matches(board, company, ok):
    from jobsearch.detect import board_name_matches

    assert board_name_matches(board, company) is ok


def test_jobs_list_links():
    from jobsearch.detect import jobs_list_links

    html = """
      <a href="/jobs">Jobs</a> <a href="/jobs/benefits">Benefits</a>
      <a href="/jobs/search">See all</a> <a href="https://www.hudl.com/jobs/search?x=1">dup</a>
      <a href="/careers/all?greenhouse-jobs-index%5Bquery%5D=x">filtered</a>
      <a href="https://other.example/jobs/search">offsite</a>
    """
    assert jobs_list_links("https://www.hudl.com/jobs", html) == [
        "https://www.hudl.com/jobs/search",
        "https://www.hudl.com/careers/all",
    ]


def test_one_hop_finds_board_on_jobs_list_page(make_client, cfg):
    h = routes(
        {
            "https://www.hudl.com/jobs/search": (
                200,
                '<script>fetch("https://boards-api.greenhouse.io/v1/boards/hudl/jobs")</script>',
            ),
            "https://www.hudl.com/jobs": (200, '<a href="/jobs/search">All jobs</a>'),
            GH_HUDL[0]: GH_HUDL[1],
        }
    )
    ats = detect_company(
        record(company="Hudl", career_url="https://www.hudl.com/jobs"), make_client(h), cfg, TODAY
    ).ats
    assert (ats["type"], ats["token"], ats["detected_by"], ats["confirmed"]) == (
        "greenhouse",
        "hudl",
        "html-scan",
        True,
    )


def test_name_guess_confirms_greenhouse_when_board_name_matches(make_client, cfg):
    h = routes(
        {
            "https://www.axon.com/careers": (200, '<div class="greenhouse-jobs-index"></div>'),
            "https://boards-api.greenhouse.io/v1/boards/axon": (200, '{"name": "Axon"}'),
        }
    )
    ats = detect_company(
        record(company="Axon", career_url="https://www.axon.com/careers"), make_client(h), cfg, TODAY
    ).ats
    assert (ats["type"], ats["token"], ats["detected_by"], ats["confirmed"]) == (
        "greenhouse",
        "axon",
        "name-guess",
        True,
    )
    assert "guessed" in ats["note"]


def test_name_guess_rejects_greenhouse_board_of_another_company(make_client, cfg):
    h = routes(
        {
            "https://www.axon.com/careers": (200, '<div class="greenhouse-jobs-index"></div>'),
            "https://boards-api.greenhouse.io/v1/boards/axon": (
                200,
                '{"name": "Axon Pharmaceuticals"}'.replace("Axon ", "Zed "),
            ),
        }
    )
    ats = detect_company(
        record(company="Axon", career_url="https://www.axon.com/careers"), make_client(h), cfg, TODAY
    ).ats
    assert ats["type"] == "other"


def test_name_guess_only_tries_hinted_systems(make_client, cfg):
    h = routes(
        {
            "https://www.halterhq.com/careers": (200, '<div class="lever-job_item"></div>'),
            # longer prefix first: routes() matches by prefix
            "https://api.lever.co/v0/postings/halterhq": (200, "[]"),
            "https://api.lever.co/v0/postings/halter": (404, fixture_text("boards/lever_404.json")),
        }
    )
    ats = detect_company(
        record(company="Halter", career_url="https://www.halterhq.com/careers"), make_client(h), cfg, TODAY
    ).ats
    assert (ats["type"], ats["token"], ats["detected_by"]) == ("lever", "halterhq", "name-guess")
    assert not any("greenhouse" in u or "ashby" in u for u in h.seen)
