"""The tracker's page and API (plan-tracker.md Phase T2): routes, owner writes, conflicts, CSV, and who may write."""

import http.client
import threading

import httpx
import pytest

from jobsearch.server import TrackerServer
from jobsearch.tracker import OWNER, PIPELINE, Tracker

W = {"X-Tracker": "1"}  # what the page sends with every write


@pytest.fixture
def site(tmp_path):
    t = Tracker(tmp_path / "tracker")
    t.write("jobs", "J001", {"company": "Acme", "job_title": "SDET", "follow_up_due": "2020-01-01"}, by=OWNER,
            create=True)  # fmt: skip
    t.write("jobs", "JW0001", {"company": "Beta", "job_title": "QA Lead", "pay_low": 100000}, by=PIPELINE, create=True)
    t.write("companies", "C001", {"company": "Acme", "research_notes": "keep me"}, by=OWNER, create=True)
    out = tmp_path / "out"
    for day in ("2026-10-06", "2026-10-07"):
        (out / day).mkdir(parents=True)
        (out / day / "digest.html").write_text(f"<h1>Report {day}</h1>", encoding="utf-8")
    (out / "2026-10-08").mkdir()  # no report in it yet
    (tmp_path / "secret.txt").write_text("not for the web", encoding="utf-8")
    server = TrackerServer(t, port=0, out_root=out)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    with httpx.Client(base_url=f"http://127.0.0.1:{server.port}", timeout=10) as client:
        yield client, t, server
    server.shutdown()
    server.server_close()


def raw_get(server, path):
    """http.client sends the path exactly as given, with no clean-up of '..' segments (httpx would normalize them)."""
    conn = http.client.HTTPConnection("127.0.0.1", server.port, timeout=10)
    try:
        conn.request("GET", path)
        r = conn.getresponse()
        return r.status, r.read()
    finally:
        conn.close()


def test_page_schema_rows_report_and_next_id(site):
    client, _, _ = site
    page = client.get("/")
    assert page.status_code == 200 and "Job search tracker" in page.text and "X-Tracker" in page.text
    schema = client.get("/api/schema").json()
    assert [c["name"] for c in schema["collections"]] == ["jobs", "companies", "people", "activity", "materials"]
    assert schema["stages"][0] == "Lead" and "Withdrawn" in schema["closed"]
    assert [r["job_id"] for r in client.get("/api/rows/jobs").json()["rows"]] == ["J001", "JW0001"]
    state = client.get("/api/state").json()
    assert state["report"] == "/out/2026-10-07/digest.html"  # the newest day that has a report
    assert client.get(state["report"]).text == "<h1>Report 2026-10-07</h1>"
    assert client.get("/api/next-id/jobs").json() == {"id": "J002"}


def test_an_owner_save_is_by_you_raises_the_highest_stage_and_shows_in_the_poll(site):
    client, t, _ = site
    before = client.get("/api/state").json()["collections"]["jobs"]
    r = client.post("/api/rows/jobs/JW0001", headers=W, json={
        "changes": {"current_stage": "Applied", "date_applied": "2026-10-07"}, "seen": {"current_stage": "Lead"}})  # fmt: skip
    row = r.json()["row"]
    assert (row["current_stage"], row["highest_stage"], row["updated_by"]) == ("Applied", "Applied", "you")
    assert t.get("jobs", "JW0001")["date_applied"] == "2026-10-07"
    assert client.get("/api/state").json()["collections"]["jobs"] != before
    new = client.post("/api/rows/jobs/J002", headers=W, json={"changes": {"company": "Gamma"}, "create": True})
    assert new.status_code == 200 and t.get("jobs", "J002")["current_stage"] == "Lead"


def test_writes_need_the_header_a_local_host_and_a_local_origin(site):
    client, t, server = site
    change = {"changes": {"current_stage": "Withdrawn"}}
    assert client.post("/api/rows/jobs/J001", json=change).status_code == 403  # a form posted from another site
    evil = {**W, "Origin": "https://evil.example"}
    assert client.post("/api/rows/jobs/J001", json=change, headers=evil).status_code == 403
    assert client.get("/api/rows/jobs", headers={"Host": "evil.example"}).status_code == 403  # DNS rebinding
    assert client.post("/api/rows/jobs/J001", json=change, headers={**W, "Host": "evil.example"}).status_code == 403
    assert client.delete("/api/rows/jobs/J001").status_code == 403
    pre = client.options("/api/rows/jobs/J001", headers={"Origin": "https://evil.example",
                                                         "Access-Control-Request-Method": "POST"})  # fmt: skip
    assert pre.status_code >= 400 and not any(h.lower().startswith("access-control") for h in pre.headers)
    assert t.get("jobs", "J001")["current_stage"] == "Lead"  # nothing got through
    local = {**W, "Origin": f"http://localhost:{server.port}"}
    assert client.post("/api/rows/jobs/J001", json=change, headers=local).status_code == 200


def test_a_stale_save_is_a_409_with_the_current_row_and_bad_input_a_400(site):
    client, t, _ = site
    t.write("jobs", "J001", {"current_stage": "Applied"}, by=OWNER)  # another tab saved first
    r = client.post("/api/rows/jobs/J001", headers=W, json={"changes": {"current_stage": "Withdrawn"},
                                                            "seen": {"current_stage": "Lead"}})  # fmt: skip
    assert r.status_code == 409 and r.json()["current"]["current_stage"] == "Applied"
    assert t.get("jobs", "J001")["current_stage"] == "Applied"
    bad = client.post("/api/rows/jobs/J001", headers=W, json={"changes": {"no_such": 1}})
    assert bad.status_code == 400 and "no field" in bad.json()["error"]
    assert client.post("/api/rows/jobs/J001", headers=W, json=["not", "an", "object"]).status_code == 400
    assert client.get("/api/rows/nope").status_code == 400


def test_delete(site):
    client, t, _ = site
    assert client.delete("/api/rows/jobs/J001", headers=W).json() == {"deleted": "J001"}
    assert t.get("jobs", "J001") is None
    assert client.delete("/api/rows/jobs/J001", headers=W).status_code == 400


def test_csv_export_has_every_row_with_the_tab_labels(site):
    client, _, _ = site
    r = client.get("/api/csv/jobs")
    assert r.headers["content-disposition"].startswith('attachment; filename="jobs-')
    lines = r.content.decode("utf-8-sig").splitlines()
    assert lines[0].startswith("Job ID,Company / group,Company ID,Job title,Track,")
    assert [line.split(",")[0] for line in lines[1:]] == ["J001", "JW0001"]


def test_csv_import_skips_title_rows_merges_by_id_and_never_blanks(site):
    client, t, _ = site
    text = (
        "Companies,,,,\n"
        "Exported from the old spreadsheet,,,,\n"
        "Company ID,Company / group,Research notes from search,Your overall rank (1 best),Days since (auto)\n"
        "C001,Acme,,2,9\n"
        "C002,Beta Games,Seen in search,3,1\n"
        ",No ID row,,,\n"
    )
    rep = client.post("/api/csv/companies", content=text.encode("utf-8"), headers={**W, "Content-Type": "text/csv"}).json()
    assert (rep["created"], rep["updated"], rep["refused"]) == (["C002"], ["C001"], [])
    assert rep["ignored_columns"] == ["Days since (auto)"]
    c1 = t.get("companies", "C001")
    assert c1["research_notes"] == "keep me" and c1["overall_rank"] == 2 and c1["updated_by"] == "you"
    assert t.get("companies", "C002")["research_status"] == "Not started"
    no_header = client.post("/api/csv/companies", content=b"Name,Sector\nX,Y\n", headers=W)
    assert no_header.status_code == 400 and "Company ID" in no_header.json()["error"]


def test_out_is_read_only_and_cannot_be_escaped(site):
    client, _, server = site
    for path in ("/out/../secret.txt", "/out/%2e%2e/secret.txt", "/out/..%5csecret.txt",
                 "/out/2026-10-07/../../secret.txt", "/out/C:%5cWindows%5cwin.ini"):  # fmt: skip
        status, body = raw_get(server, path)
        assert status == 404 and b"not for the web" not in body, path
    assert client.post("/out/2026-10-07/digest.html", headers=W).status_code == 404


def test_a_second_server_cannot_take_the_same_port(site):
    _, t, server = site
    with pytest.raises(OSError):  # on Windows SO_REUSEADDR would have let it bind and split the requests
        TrackerServer(t, port=server.port)


def test_queued_companies_are_listed_and_added_only_from_the_page(site):
    client, t, _ = site
    before = client.get("/api/state").json()["imports"]
    batch = t.queue_companies([{"company": "Gamma"}, {"company": "acme"}, {"company": "Delta"}], label="three",
                              by="Codex")  # fmt: skip
    assert client.get("/api/state").json()["imports"] != before  # the page notices and shows the banner
    [queued] = client.get("/api/imports").json()["imports"]
    assert (queued["id"], queued["status"], queued["rows"], queued["label"]) == (batch, "pending", 3, "three")
    assert client.post(f"/api/imports/{batch}/accept").status_code == 403  # not from the page
    res = client.post(f"/api/imports/{batch}/accept", headers=W).json()["batch"]
    assert (res["status"], res["imported"], res["skipped_duplicates"]) == ("done", 2, 1)
    rows = client.get("/api/rows/companies").json()["rows"]
    assert [(r["company_id"], r["company"], r["updated_by"]) for r in rows[1:]] == [("C002", "Gamma", "Codex"),
                                                                                   ("C003", "Delta", "Codex")]  # fmt: skip
    assert client.post("/api/imports/q404/accept", headers=W).status_code == 400
