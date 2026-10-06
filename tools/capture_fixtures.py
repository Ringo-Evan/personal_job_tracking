"""Re-capture board API fixtures from live endpoints (run by hand when a board's shape changes).

    .venv/Scripts/python tools/capture_fixtures.py              # every board system
    .venv/Scripts/python tools/capture_fixtures.py workable     # only the named ones

Keeps each fixture small: QA-like postings first, then a few others.
"""

import json
import re
import sys
import time
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jobsearch.config import load_config  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "boards"
QA = re.compile(r"(?i)\b(qa|quality|test|sdet)\b")

cfg = load_config()
client = httpx.Client(
    headers={"User-Agent": f"jobsearch-research/fixtures (+mailto:{cfg.contact_email})"},
    timeout=30,
    follow_redirects=True,
)


def pick(items, title_of, n_qa=3, n_other=2):
    qa = [i for i in items if QA.search(title_of(i) or "")][:n_qa]
    other = [i for i in items if i not in qa][:n_other]
    return qa + other


def save(name, obj):
    (OUT / name).write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    print("wrote", name)


def get(url, **kw):
    time.sleep(1)
    r = client.get(url, **kw)
    r.raise_for_status()
    return r.json()


def greenhouse():
    gh = get("https://boards-api.greenhouse.io/v1/boards/testlio/jobs?content=true")
    gh["jobs"] = pick(gh["jobs"], lambda j: j["title"])
    save("greenhouse_jobs_testlio.json", gh)


def lever():
    lv = get("https://api.lever.co/v0/postings/wattpad?mode=json")
    save("lever_postings_wattpad.json", pick(lv, lambda j: j["text"]))


def ashby():
    ab = get("https://api.ashbyhq.com/posting-api/job-board/saronic?includeCompensation=true")
    ab["jobs"] = pick(ab["jobs"], lambda j: j["title"])
    save("ashby_board_saronic.json", ab)


def smartrecruiters():
    sr = get("https://api.smartrecruiters.com/v1/companies/BoschGroup/postings?limit=100&q=test")
    sr["content"] = pick(sr["content"], lambda j: j["name"])
    save("smartrecruiters_postings_bosch.json", sr)


def workday():
    base = "https://tricentis.wd1.myworkdayjobs.com/wday/cxs/tricentis/Tricentis_Careers"
    time.sleep(1)
    wd = client.post(
        f"{base}/jobs", json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": "quality"}
    ).json()
    save("workday_jobs_tricentis.json", wd)
    first = wd["jobPostings"][0]
    save("workday_detail_tricentis.json", get(base + first["externalPath"]))


def workable():
    wk = get("https://apply.workable.com/api/v1/widget/accounts/testronic?details=true")
    wk["jobs"] = pick(wk["jobs"], lambda j: j["title"])
    save("workable_widget_testronic.json", wk)


def rippling():
    # Onfleet lists its QA job once per location, which the normalizer must merge.
    base = "https://api.rippling.com/platform/api/ats/v1/board/onfleet-careers/jobs"
    jobs = get(base)
    qa = [j for j in jobs if QA.search(j["name"])]
    keep = qa + [j for j in jobs if j not in qa][:2]
    save("rippling_jobs_onfleet.json", keep)
    save("rippling_detail_onfleet.json", get(f"{base}/{qa[0]['uuid']}"))


def breezy():
    bz = get("https://aimpoint-digital.breezy.hr/json")
    bz = pick(bz, lambda j: j["name"], n_qa=2, n_other=3)
    save("breezy_json_aimpoint.json", bz)
    remote = next(j for j in bz if (j.get("location") or {}).get("is_remote"))
    time.sleep(1)
    page = client.get(remote["url"])
    page.raise_for_status()
    (OUT / "breezy_page_aimpoint.html").write_text(page.text, encoding="utf-8")
    print("wrote breezy_page_aimpoint.html for", remote["url"])


def pinpoint():
    save("pinpoint_postings_coforma.json", get("https://coforma.pinpointhq.com/postings.json"))


def recruitee():
    rc = get("https://intent.recruitee.com/api/offers/")
    rc["offers"] = pick(rc["offers"], lambda j: j["title"], n_qa=2, n_other=2)
    save("recruitee_offers_intent.json", rc)


CAPTURES = {f.__name__: f for f in (greenhouse, lever, ashby, smartrecruiters, workday, workable, rippling, breezy,
                                    pinpoint, recruitee)}  # fmt: skip

if __name__ == "__main__":
    for name in sys.argv[1:] or list(CAPTURES):
        CAPTURES[name]()
