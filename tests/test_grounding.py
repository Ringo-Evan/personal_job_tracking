"""Grounding check (plan-models.md §4 step 3, §8 tests): real quotes pass, paraphrases and wrong sources fail."""

import copy

import jsonschema
import pytest

from jobsearch.evidence import Pack, Source
from jobsearch.grounding import facts_schema, ground, grounding_gate, normalize

S1 = Source(
    "S1",
    "https://acme.example/careers",
    "Careers",
    "2026-10-05",
    "Join Acme. We are a remote-first company and hire anywhere in the United States.\n"
    "Open roles: Senior SDET (Remote, US). See https://boards.greenhouse.io/acme for all jobs.\n"
    "Our engineering team is led by Dana Whitfield, VP of Engineering.",
)
S2 = Source(
    "S2",
    "https://news.example/acme-funding",
    "Acme raises Series C",
    "2026-10-05",
    "Acme announced a $40 million Series C round in March 2026 to expand its platform team.",
)
PACK = Pack("C1", 0.0, [S1, S2])


def facts(**over):
    base = {
        "career_url": {"source": "S1", "quote": "Join Acme.", "value": "https://acme.example/careers"},
        "ats": {"source": "S1", "quote": "See https://boards.greenhouse.io/acme for all jobs.", "type": "greenhouse",
                "token": "acme"},
        "remote_policy": {"source": "S1", "quote": "We are a remote-first company and hire anywhere in the United States.",
                          "summary": "Remote-first, hires anywhere in the US.", "maine_eligible": "yes"},
        "stability_signals": [{"source": "S2", "quote": "Acme announced a $40 million Series C round in March 2026",
                               "kind": "funding", "summary": "Series C, $40M", "date": "2026-03"}],
        "size": None,
        "qa_openings": [{"source": "S1", "quote": "Senior SDET (Remote, US)", "title": "Senior SDET",
                         "location": "Remote, US", "work_mode": "Remote", "url": "https://boards.greenhouse.io/acme"}],
        "people": [{"source": "S1", "quote": "led by Dana Whitfield, VP of Engineering", "name": "Dana Whitfield",
                    "title": "VP of Engineering"}],
    }  # fmt: skip
    base.update(over)
    return base


def test_schema_accepts_the_example_and_nulls():
    jsonschema.validate(facts(), facts_schema())
    empty = {k: None for k in ("career_url", "ats", "remote_policy", "size")}
    jsonschema.validate(empty | {"stability_signals": [], "qa_openings": [], "people": []}, facts_schema())


def test_existing_quotes_pass():
    g = ground(facts(), PACK)
    assert g.dropped == []
    assert g.total == 6 and g.passed
    assert g.facts["people"][0]["name"] == "Dana Whitfield"


def test_normalization_is_case_whitespace_and_typographic_only():
    assert normalize("  Remote‑First  COMPANY’s ") == normalize("remote‑first company's")
    f = facts(remote_policy={"source": "S1", "quote": "WE ARE A   remote-first\ncompany", "summary": "x",
                             "maine_eligible": "yes"})  # fmt: skip
    assert ground(f, PACK).dropped == []


def test_paraphrase_fails():
    f = facts(remote_policy={"source": "S1", "quote": "Acme lets employees work remotely from any US state",
                             "summary": "x", "maine_eligible": "yes"})  # fmt: skip
    g = ground(f, PACK)
    assert [d.path for d in g.dropped] == ["remote_policy"]
    assert g.facts["remote_policy"] is None


def test_quote_cited_to_wrong_source_fails():
    f = facts(stability_signals=[{"source": "S1", "quote": "Acme announced a $40 million Series C round",
                                  "kind": "funding", "summary": "x", "date": None}])  # fmt: skip
    g = ground(f, PACK)
    assert g.dropped[0].reason == "quote not found in S1"


def test_unknown_source_fails():
    f = facts(size={"source": "S9", "quote": "Join Acme.", "value": "50-500"})
    assert ground(f, PACK).dropped[0].reason == "cites unknown source 'S9'"


def test_person_name_must_be_in_quote():
    f = facts(people=[{"source": "S1", "quote": "Our engineering team is led by", "name": "Dana Whitfield",
                       "title": "VP"}])  # fmt: skip
    assert ground(f, PACK).dropped[0].reason == "person's name is not in their quote"


def test_people_capped_at_five():
    p = facts()["people"][0]
    g = ground(facts(people=[p] * 7), PACK)
    assert len(g.facts["people"]) == 5
    assert any("people truncated" in n for n in g.notes)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("title", "VP, dana@acme.example", "email"),
        ("title", "VP (207) 555-0134", "phone"),
        ("title", "see linkedin.com/in/dana", "LinkedIn"),
    ],
)
def test_pii_and_linkedin_are_dropped(field, value, reason):
    p = dict(facts()["people"][0], **{field: value})
    g = ground(facts(people=[p]), PACK)
    assert reason in g.dropped[0].reason


def test_long_quote_is_truncated_and_still_grounded():
    text = "one two three four five six seven eight nine ten " * 4
    pack = Pack("C1", 0.0, [Source("S1", "https://a.example", "", "", text)])
    f = {"career_url": None, "ats": None, "size": None, "stability_signals": [], "qa_openings": [], "people": [],
         "remote_policy": {"source": "S1", "quote": text.strip(), "summary": "x", "maine_eligible": "unknown"}}  # fmt: skip
    g = ground(f, pack)
    assert g.dropped == []
    assert len(g.facts["remote_policy"]["quote"].split()) == 25
    assert g.notes


def test_career_url_must_appear_in_evidence():
    f = facts(career_url={"source": "S1", "quote": "Join Acme.", "value": "https://evil.example/jobs"})
    assert ground(f, PACK).dropped[0].reason == "career_url appears nowhere in the evidence"


def test_invented_opening_url_is_replaced_by_its_source_page():
    o = dict(facts()["qa_openings"][0], url="https://acme.example/jobs/12345-made-up")
    g = ground(facts(qa_openings=[o]), PACK)
    assert g.facts["qa_openings"][0]["url"] == S1.url


def test_unknown_ats_is_not_a_fact():
    g = ground(facts(ats={"source": "S1", "quote": "nothing", "type": "unknown", "token": None}), PACK)
    assert g.total == 5 and g.facts["ats"] is None


def test_input_is_not_modified():
    f = facts()
    before = copy.deepcopy(f)
    ground(f, PACK)
    assert f == before


def test_gate_escalates_above_30_percent():
    bad = {"source": "S2", "quote": "made up words here", "kind": "other", "summary": "x", "date": None}
    ok = grounding_gate(PACK)(facts(stability_signals=[bad]))  # 1 of 6 failed: 17%
    assert ok is None
    reason = grounding_gate(PACK)(facts(stability_signals=[bad, bad, bad]))  # 3 of 8: 37.5%
    assert reason and reason.startswith("grounding failed for 3/8 facts")


def test_career_url_that_is_the_cited_page_needs_no_quote():
    f = facts(
        career_url={
            "source": "S1",
            "quote": "Apply at our careers site",
            "value": "https://acme.example/careers/",
        }
    )
    assert ground(f, PACK).dropped == []
    other = facts(
        career_url={
            "source": "S2",
            "quote": "Apply at our careers site",
            "value": "https://acme.example/careers",
        }
    )
    assert ground(other, PACK).dropped[0].reason == "quote not found in S2"


def test_quote_may_come_from_the_page_title():
    o = dict(facts()["qa_openings"][0], source="S2", quote="Acme raises Series C")
    assert ground(facts(qa_openings=[o]), PACK).dropped == []


def test_job_titles_and_first_names_are_not_people():
    p = facts()["people"][0]
    as_title = dict(
        p,
        name="VP of Engineering",
        title="VP of Engineering",
        quote="led by Dana Whitfield, VP of Engineering",
    )
    first_only = dict(p, name="Dana")
    reasons = [d.reason for d in ground(facts(people=[as_title, first_only]), PACK).dropped]
    assert reasons == ["name is a job title, not a person", "not a full name"]


def test_signal_date_kept_only_when_quote_states_the_year():
    sig = facts()["stability_signals"][0]
    g = ground(facts(stability_signals=[dict(sig, date="2026-03")]), PACK)
    assert g.facts["stability_signals"][0]["date"] == "2026-03"  # quote says "March 2026"
    guessed = dict(sig, quote="Acme announced a $40 million Series C round", date="2025-08")
    g = ground(facts(stability_signals=[guessed]), PACK)
    assert g.facts["stability_signals"][0]["date"] is None
    assert any("date 2025-08 dropped" in n for n in g.notes)


def test_gate_escalates_an_empty_extraction_from_a_rich_pack():
    empty = {k: None for k in ("career_url", "ats", "remote_policy", "size")} | {
        "stability_signals": [],
        "qa_openings": [],
        "people": [],
    }
    rich = Pack("C1", 0.0, [Source("S1", "https://a.example", "", "", "word " * 500)])
    assert grounding_gate(rich)(empty).startswith("no grounded facts from 1 sources")
    assert grounding_gate(PACK)(empty) is None  # small pack: nothing to find is believable


@pytest.mark.parametrize(
    "name,title,ok",
    [
        ("Dana Whitfield", "VP of Engineering", True),
        ("Radu Creosteanu", "Head of Driver Platform", True),
        ("Mia Chen", "Director, Quality Engineering", True),
        ("Jeremey S.", "Senior Manager, Software Engineering", False),  # surname initial only
        ("Sumedha Mehta", "Senior Quality Engineer", False),  # individual contributor, not a leader
        ("Arnela Hodzic-Isakovic", "Quality Engineer", False),
    ],
)
def test_people_must_be_named_leaders(name, title, ok):
    from jobsearch.grounding import person_problem

    assert (person_problem(name, title) is None) is ok
