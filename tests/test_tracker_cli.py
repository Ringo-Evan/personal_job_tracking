"""The agents' tracker commands (plan-tracker.md Phase T4): writes under the agent's own name, refusals exit 1."""

import json

import pytest
from typer.testing import CliRunner

from jobsearch.cli import app
from jobsearch.tracker import OWNER, Tracker

runner = CliRunner()


@pytest.fixture
def t(tmp_path):
    t = Tracker(tmp_path / "tracker")
    t.write("companies", "C001", {"company": "Acme", "sector": "Games"}, by=OWNER, create=True)
    t.write("jobs", "J001", {"company": "Acme", "job_title": "SDET"}, by=OWNER, create=True)
    return t


def run(t, *args):
    return runner.invoke(app, ["tracker", *args, "--data", str(t.root)])


def test_an_agent_adds_and_updates_rows_under_its_own_name(t):
    r = run(t, "add", "companies", "company=Beta Games", "sector=Games",
            "research_notes=From general knowledge – verify", "--by", "Claude Code")  # fmt: skip
    assert r.exit_code == 0 and "added C002" in r.output
    assert t.get("companies", "C002")["updated_by"] == "Claude Code"
    r = run(t, "update", "jobs", "J001", "pay_low=120,000", "fit_notes=Strong match", "--by", "Claude Code")
    assert r.exit_code == 0 and "updated J001: fit_notes, pay_low" in r.output
    assert t.get("jobs", "J001")["pay_low"] == 120000
    r = run(t, "add", "jobs", "company=Beta Games", "job_title=QA Lead", "--by", "Codex")
    assert r.exit_code == 0 and t.get("jobs", "J002")["current_stage"] == "Lead"


def test_owner_fields_the_owners_name_and_bad_input_are_refused_with_exit_1(t):
    for args in (
        ("update", "companies", "C001", "overall_rank=1", "--by", "Claude Code"),  # the owner's field
        ("update", "jobs", "J001", "current_stage=Applied", "--by", "Claude Code"),  # set only when added
        ("add", "activity", "type=Call", "--by", "Claude Code"),  # the owner's tab
        ("update", "jobs", "J001", "fit_notes=x", "--by", "you"),  # the owner edits in the page
        ("update", "jobs", "J001", "fit_notes", "--by", "Claude Code"),  # not key=value
        ("get", "jobs", "J404"),
    ):
        r = run(t, *args)
        assert r.exit_code == 1 and "refused" in r.output, args
    assert "overall_rank" not in t.get("companies", "C001")
    assert t.get("jobs", "J001")["current_stage"] == "Lead" and "fit_notes" not in t.get("jobs", "J001")


def test_list_get_and_next_id(t):
    rows = json.loads(run(t, "list", "jobs", "--where", "company=Acme", "--json").output)
    assert [row["job_id"] for row in rows] == ["J001"]
    assert run(t, "list", "jobs", "--where", "company=Nobody").output.strip() == "0 jobs"
    assert json.loads(run(t, "get", "companies", "C001").output)["company"] == "Acme"
    assert run(t, "next-id", "people").output.strip() == "P001"


def test_queued_companies_wait_for_the_owner(t, tmp_path):
    rows = tmp_path / "rows.json"
    rows.write_text(json.dumps([{"company": "Gamma"}, {"company": "acme"}, {"company": "Delta", "sector": "Travel"}]),
                    encoding="utf-8")  # fmt: skip
    r = run(t, "queue-companies", "--file", str(rows), "--label", "test batch", "--by", "Codex")
    assert r.exit_code == 0 and "queued 3" in r.output
    assert len(t.load("companies")) == 1  # nothing is added until the owner clicks Add them
    [batch] = t.imports()
    assert (batch["label"], batch["added_by"], batch["status"]) == ("test batch", "Codex", "pending")
    rows.write_text(json.dumps([{"company": "Epsilon", "your_notes": "mine"}]), encoding="utf-8")
    r = run(t, "queue-companies", "--file", str(rows), "--label", "bad", "--by", "Codex")
    assert r.exit_code == 1 and "your_notes" in r.output and len(t.imports()) == 1
