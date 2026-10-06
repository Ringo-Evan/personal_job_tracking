import json

import httpx
import pytest

from conftest import FIXTURES
from jobsearch.boards import BoardError, ashby, fetch_postings, greenhouse, lever, smartrecruiters, workday
from jobsearch.boards.common import html_to_text, iso_date, pay_from_text, work_mode_from

BOARDS = FIXTURES / "boards"


def load(name):
    return json.loads((BOARDS / name).read_text(encoding="utf-8"))


def by_id(postings, job_id):
    return next(p for p in postings if p.board_job_id == job_id)


# --- normalizers against saved live responses ----------------------------------------------------


def test_greenhouse_normalize():
    ps = greenhouse.normalize(load("greenhouse_jobs_testlio.json"), "C140", "testlio")
    assert len(ps) == 5
    qa = by_id(ps, "6979353")
    assert qa.title == "QA Analyst (Future Roles)"
    assert qa.location_text == "Global"
    assert qa.url.startswith("https://job-boards.greenhouse.io/testlio/jobs/")
    assert qa.posted_at == "2025-06-18"
    assert (
        qa.raw_ref == "https://boards-api.greenhouse.io/v1/boards/testlio/jobs/6979353?pay_transparency=true"
    )
    assert "<" not in qa.description_text and "&lt;" not in qa.description_text
    assert qa.key == "greenhouse-6979353"
    assert by_id(ps, "8186522").work_mode == "Remote"  # "Remote in AMER"


def test_lever_normalize_structured_pay_and_workplace():
    ps = lever.normalize(load("lever_postings_wattpad.json"), "C109", "wattpad")
    assert len(ps) == 2
    p = by_id(ps, "a322eb2d-dec8-4c83-829a-c71e0c9abdb7")
    assert (p.pay_low, p.pay_high, p.currency, p.pay_period) == (110000, 135000, "CAD", "year")
    assert (p.work_mode, p.is_remote) == ("Hybrid", False)
    assert p.location_text == "Toronto, Ontario"
    assert p.apply_url.endswith("/apply")
    assert p.posted_at == "2026-07-22"


def test_ashby_normalize():
    ps = ashby.normalize(load("ashby_board_saronic.json"), "C036", "saronic")
    assert len(ps) == 5
    p = ps[0]
    assert p.title.startswith("Systems Architecture, Integration, and Test Engineer")
    assert (p.location_text, p.work_mode, p.is_remote) == ("Austin, TX", "On-site", False)
    assert p.url.startswith("https://jobs.ashbyhq.com/saronic/")


def test_ashby_compensation_and_unlisted():
    data = {
        "jobs": [
            {"id": "a", "title": "SDET", "location": "Remote", "isRemote": True, "workplaceType": "Remote",
             "isListed": True, "jobUrl": "https://jobs.ashbyhq.com/x/a", "publishedAt": "2026-10-01T00:00:00Z",
             "compensation": {"summaryComponents": [
                 {"compensationType": "EquityPercentage", "minValue": 0.1, "maxValue": 0.2},
                 {"compensationType": "Salary", "interval": "1 YEAR", "currencyCode": "USD",
                  "minValue": 150000, "maxValue": 190000}]}},
            {"id": "b", "title": "Hidden", "isListed": False},
        ]
    }  # fmt: skip
    (p,) = ashby.normalize(data, "C1", "x")
    assert (p.pay_low, p.pay_high, p.currency, p.pay_period) == (150000, 190000, "USD", "year")
    assert p.work_mode == "Remote"


def test_smartrecruiters_normalize():
    ps = smartrecruiters.normalize(load("smartrecruiters_postings_bosch.json"), "C9", "BoschGroup")
    assert len(ps) == 5
    p = next(p for p in ps if p.title == "Test Manager")
    assert p.location_text == "Taguig, Philippines"
    assert p.url == f"https://jobs.smartrecruiters.com/BoschGroup/{p.board_job_id}"
    assert p.work_mode == "On-site"


def test_workday_normalize_detail():
    listing = load("workday_jobs_tricentis.json")
    item = listing["jobPostings"][0]
    p = workday.normalize_detail(
        load("workday_detail_tricentis.json"), item, "C092", "tricentis|1|Tricentis_Careers"
    )
    assert p.board_job_id == "JR105936"
    assert p.location_text == "US - Atlanta; Austin, Texas"
    assert p.url.startswith("https://tricentis.wd1.myworkdayjobs.com/Tricentis_Careers/job/")
    assert p.raw_ref.startswith(
        "https://tricentis.wd1.myworkdayjobs.com/wday/cxs/tricentis/Tricentis_Careers/job/"
    )


# --- helpers -------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("The pay range is $120,000 - $150,000 per year.", (120000, 150000, "USD", "year")),
        ("Salary: $120,000–$150,000 USD", (120000, 150000, "USD", "year")),
        ("Base salary $95k to $130k", (95000, 130000, "USD", "year")),
        ("US$140,000 — US$170,000", (140000, 170000, "USD", "year")),
        ("CA$110,000 - CA$135,000", (110000, 135000, "CAD", "year")),
        ("$110,000 - $135,000 CAD", (110000, 135000, "CAD", "year")),
        ("$45 - $60 per hour", (45, 60, "USD", "hour")),
        ("$45.50-$62.25/hr", (45.5, 62.25, "USD", "hour")),
        ("We raised $5 - $10 million. Pay: $130,000 - $160,000", (130000, 160000, "USD", "year")),
        (
            "401k match and $1,000 - $2,000 learning stipend; salary $100,000-$120,000",
            (100000, 120000, "USD", "year"),
        ),
        ("Salary $5,000 - $7,000 monthly", (5000, 7000, "USD", "month")),
        ("$5 - $10 mm ARR", None),
        ("No pay listed.", None),
        ("$150,000 - $120,000", None),
    ],
)
def test_pay_from_text(text, expected):
    assert pay_from_text(text) == expected


def test_html_to_text_handles_greenhouse_double_escaping():
    assert html_to_text("&lt;p&gt;Hello &amp;amp; welcome&lt;/p&gt;&lt;ul&gt;&lt;li&gt;One&lt;/li&gt;") == (
        "Hello & welcome\nOne"
    )
    assert html_to_text(None) == ""


@pytest.mark.parametrize(
    ("value", "expected"),
    [("2026-10-02T16:07:02-04:00", "2026-10-02"), (1784744974309, "2026-07-22"), ("", None), (None, None),
     ("Posted 4 Days Ago", None)],
)  # fmt: skip
def test_iso_date(value, expected):
    assert iso_date(value) == expected


@pytest.mark.parametrize(
    ("text", "is_remote", "mode"),
    [("Remote - US", None, "Remote"), ("Hybrid - Boston", None, "Hybrid"), ("Austin, TX", False, "On-site"),
     ("Austin, TX", None, "Unknown"), ("", True, "Remote"), ("On-site, Bangor", None, "On-site")],
)  # fmt: skip
def test_work_mode_from(text, is_remote, mode):
    assert work_mode_from(text, is_remote) == mode


# --- fetching ------------------------------------------------------------------------------------


def test_fetch_greenhouse_lists_then_details_only_wanted_titles(make_client, cfg):
    full = load("greenhouse_jobs_testlio.json")
    by_job = {str(j["id"]): j for j in full["jobs"]}
    calls = []

    def handler(request):
        url = str(request.url)
        calls.append(url)
        if url == "https://boards-api.greenhouse.io/v1/boards/testlio/jobs":
            return httpx.Response(200, json={"jobs": [{k: v for k, v in j.items() if k != "content"}
                                                      for j in full["jobs"]]})  # fmt: skip
        job_id = url.rsplit("/", 1)[1].split("?")[0]
        assert url.endswith("?pay_transparency=true")
        detail = by_job[job_id] | {"pay_input_ranges": [{"min_cents": 9000000, "max_cents": 12000000,
                                                         "currency_type": "USD"}]}  # fmt: skip
        return httpx.Response(200, json=detail)

    c = make_client(handler)
    ps = fetch_postings(c, cfg, "greenhouse", "testlio", "C140", wanted=lambda t: t.startswith("QA"))
    assert len(ps) == 5
    detailed = [p for p in ps if p.description_text]
    assert sorted(p.title for p in detailed) == ["QA Analyst (Future Roles)", "QA Tester (Future Roles)"]
    assert (detailed[0].pay_low, detailed[0].pay_high, detailed[0].pay_period) == (90000, 120000, "year")
    assert len(calls) == 3

    fetch_postings(c, cfg, "greenhouse", "testlio", "C140", wanted=lambda t: t.startswith("QA"))
    assert len(calls) == 6  # watcher never serves boards from cache


def test_fetch_smartrecruiters_paginates(make_client, cfg):
    def handler(request):
        offset = int(request.url.params["offset"])
        content = [{"id": str(offset + i), "name": f"QA {offset + i}", "location": {"country": "us", "remote": True}}
                   for i in range(100 if offset < 200 else 7)]  # fmt: skip
        return httpx.Response(200, json={"offset": offset, "totalFound": 207, "content": content})

    ps = fetch_postings(make_client(handler), cfg, "smartrecruiters", "Acme", "C1")
    assert len(ps) == 207
    assert len({p.board_job_id for p in ps}) == 207


def test_fetch_workday_searches_each_term_and_only_details_wanted_titles(make_client, cfg):
    cfg.watch.workday_enabled = True
    cfg.watch.workday_terms = ["quality", "test"]
    base = "https://acme.wd5.myworkdayjobs.com/wday/cxs/acme/External"
    items = {
        "quality": [{"title": "QA Manager", "externalPath": "/job/Remote-USA/QA-Manager_R1", "bulletFields": ["R1"]},
                    {"title": "Sales Lead", "externalPath": "/job/NYC/Sales_R2", "bulletFields": ["R2"]}],
        "test": [{"title": "QA Manager", "externalPath": "/job/Remote-USA/QA-Manager_R1", "bulletFields": ["R1"]}],
    }  # fmt: skip
    seen = []

    def handler(request):
        url = str(request.url)
        seen.append((request.method, url))
        if request.method == "POST":
            term = json.loads(request.content)["searchText"]
            return httpx.Response(200, json={"total": len(items[term]), "jobPostings": items[term]})
        assert url == base + "/job/Remote-USA/QA-Manager_R1"
        return httpx.Response(200, json={"jobPostingInfo": {"title": "QA Manager", "location": "Remote - USA",
                                                            "jobReqId": "R1", "jobDescription": "<p>$120,000 - $140,000</p>"}})  # fmt: skip

    ps = fetch_postings(
        make_client(handler), cfg, "workday", "acme|5|External", "C1", wanted=lambda t: "QA" in t
    )
    assert [p.board_job_id for p in ps] == ["R1"]
    assert ps[0].work_mode == "Remote" and ps[0].pay_low == 120000
    assert [m for m, _ in seen].count("POST") == 2
    assert [m for m, _ in seen].count("GET") == 1


def test_fetch_workday_skips_a_listed_posting_without_its_link(make_client, cfg):
    cfg.watch.workday_enabled = True
    cfg.watch.workday_terms = ["quality"]
    listed = [{"title": "QA Lead", "bulletFields": ["R9"]},  # no externalPath: KeyError took the board down before
              {"title": "QA Manager", "externalPath": "/job/Remote-USA/QA-Manager_R1", "bulletFields": ["R1"]}]  # fmt: skip

    def handler(request):
        if request.method == "POST":
            return httpx.Response(200, json={"total": 2, "jobPostings": listed})
        return httpx.Response(200, json={"jobPostingInfo": {"title": "QA Manager", "location": "Remote - USA"}})

    ps = fetch_postings(make_client(handler), cfg, "workday", "acme|5|External", "C1", wanted=lambda t: True)
    assert [p.board_job_id for p in ps] == ["R1"]


def test_fetch_workday_disabled_returns_nothing(make_client, cfg):
    cfg.watch.workday_enabled = False
    assert fetch_postings(make_client(lambda r: httpx.Response(500)), cfg, "workday", "a|1|b", "C1") == []


@pytest.mark.parametrize(
    ("response", "message"),
    [
        (httpx.Response(404, text="Not Found"), "HTTP 404"),
        (httpx.Response(200, text="<html>maintenance</html>"), "bad JSON"),
        (httpx.Response(200, json={"jobs": [{"title": "no id"}]}), "response shape"),
    ],
)
def test_fetch_failures_raise_board_error(make_client, cfg, response, message):
    with pytest.raises(BoardError, match=message):
        fetch_postings(make_client(lambda r: response, max_attempts=1), cfg, "greenhouse", "x", "C1")


def test_network_failure_raises_board_error(make_client, cfg):
    def handler(request):
        raise httpx.ConnectError("down", request=request)

    with pytest.raises(BoardError, match="ConnectError"):
        fetch_postings(make_client(handler, max_attempts=1), cfg, "lever", "x", "C1")
