import pytest

from jobsearch.boards.common import JobPosting
from jobsearch.config import load_config
from jobsearch.filters import PostingFilter, track_for


@pytest.fixture(scope="module")
def flt():
    return PostingFilter(load_config())


def posting(location="", is_remote=None, description="", title="Senior SDET", work_mode="Unknown"):
    return JobPosting(
        board="greenhouse",
        board_job_id="1",
        company_id="C001",
        title=title,
        location_text=location,
        is_remote=is_remote,
        work_mode=work_mode,
        url="https://example.com/j/1",
        raw_ref="https://example.com/api",
        description_text=description,
    )


# --- plan acceptance table -----------------------------------------------------------------------

KEEP = ["Senior SDET", "Software Engineer in Test II", "QA Manager", "Senior Quality Engineer (Software)",
        "Test Automation Lead"]  # fmt: skip
DROP = ["Quality Inspector", "Supplier Quality Engineer", "Manufacturing Quality Technician", "Test Kitchen Manager",
        "QA Intern"]  # fmt: skip


@pytest.mark.parametrize("title", KEEP)
def test_plan_titles_kept(flt, title):
    assert flt.title_ok(title)


@pytest.mark.parametrize("title", DROP)
def test_plan_titles_dropped(flt, title):
    assert not flt.title_ok(title)


@pytest.mark.parametrize(
    ("title", "ok"),
    [
        ("Staff Software Engineer, Test Infrastructure", True),
        ("Release Manager", True),
        ("Site Reliability Engineer", True),
        ("Verification & Validation Engineer (Medical Device)", True),
        ("Junior QA Analyst", False),
        ("Jr. QA Engineer", False),
        ("Food Safety Quality Manager", False),
        ("Account Executive", False),
        ("Senior Product Designer", False),
        ("", False),
    ],
)
def test_more_titles(flt, title, ok):
    assert flt.title_ok(title) is ok


# --- location ------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "is_remote", "verdict"),
    [
        ("Remote - US", None, "eligible"),
        ("Remote, United States", None, "eligible"),
        ("United States (Remote)", None, "eligible"),
        ("US Remote", None, "eligible"),
        ("Remote (USA)", None, "eligible"),
        ("Remote - North America", None, "eligible"),
        ("Anywhere", None, "eligible"),
        ("Remote", None, "eligible"),
        ("Fully Remote", None, "eligible"),
        ("", True, "eligible"),
        ("Portland, ME", None, "eligible"),
        ("Bangor, Maine", None, "eligible"),
        ("Hybrid - Orono, ME", None, "eligible"),
        ("Bar Harbor", None, "eligible"),
        ("Toronto, Ontario; Remote - US", None, "eligible"),
        ("Belfast ME", None, "eligible"),
        ("Ellsworth, ME (Onsite)", None, "eligible"),
        ("Global", None, "unknown"),
        ("Any Location; Serbia; Poland", None, "unknown"),
        ("Seattle, Washington, United States (Washington-Remote)", None, "unknown"),
        ("Remote - US (Austin, TX)", None, "unknown"),
        ("Remote - US; Seattle, Washington, United States (Washington-Remote)", None, "eligible"),
        ("Remote - US (Portland, ME)", None, "eligible"),
        ("Remote in AMER", None, "unknown"),
        ("Remote - Americas", None, "unknown"),
        ("Remote - Canada", None, "drop"),
        ("Remote in EMEA", None, "drop"),
        ("Remote - UK", None, "drop"),
        ("Brazil, Mexico (Remote)", None, "drop"),
        ("Austin, TX", False, "drop"),
        ("New York, NY", None, "drop"),
        ("US - Atlanta", None, "drop"),
        ("Remote, Mexico", None, "drop"),  # ", me" must not match Mexico
        ("Austin, TX", True, "drop"),  # flagged remote but tied to a city: not stated as US-wide
        # owner prefers too wide over too narrow (2026-10-05): ambiguous locations are kept for the scorer
        ("", None, "unknown"),
        ("United States", None, "unknown"),
        ("USA", False, "unknown"),
        ("United States > Phoenix : 200 E Van Buren St", None, "drop"),
    ],
)
def test_classify_location(flt, location, is_remote, verdict):
    assert flt.classify(posting(location, is_remote)).verdict == verdict


@pytest.mark.parametrize(
    ("location", "description", "verdict"),
    [
        ("Austin, TX", "This position is fully remote for candidates in the US.", "unknown"),
        ("San Francisco, California", "This role is remote. We hire across the U.S.", "unknown"),
        ("Austin, TX", "Join our office team. Hybrid three days a week.", "drop"),
        ("India - Mumbai", "We are a remote-first company.", "drop"),  # not a US location
        ("Remote in EMEA", "Work from anywhere in Europe.", "drop"),
    ],
)
def test_us_city_with_remote_description_is_kept(flt, location, description, verdict):
    assert flt.classify(posting(location, description=description)).verdict == verdict


STATE_LIST_NO_MAINE = """About the role...
This is a remote position. We are currently able to hire employees residing in the following states:
Arizona, Colorado, Florida, Georgia, Texas and Washington.
"""
STATE_LIST_WITH_MAINE = "Remote role. Candidates must reside in one of: CA, CO, ME, NY, TX."
STATE_ABBR_NO_MAINE = "Eligible locations: AZ, CO, FL, GA, NC, TX, WA"
OFFICE_PROSE = "We are located in Austin. Our customers love us."


@pytest.mark.parametrize(
    ("description", "verdict", "reason_part"),
    [
        (STATE_LIST_NO_MAINE, "unknown", "without Maine"),
        (STATE_LIST_WITH_MAINE, "eligible", "Maine is in"),
        (STATE_ABBR_NO_MAINE, "unknown", "without Maine"),
        (OFFICE_PROSE, "eligible", "US remote"),
        ("Remote in the US. Our IN office and OR lab are nearby.", "eligible", "US remote"),
        ("", "eligible", "US remote"),
    ],
)
def test_state_lists_in_description(flt, description, verdict, reason_part):
    result = flt.classify(posting("Remote - US", description=description))
    assert result.verdict == verdict
    assert reason_part in result.reason


def test_state_list_ignored_for_on_site_roles(flt):
    desc = "Candidates must reside in one of: CA, CO, ME, NY, TX."
    assert flt.classify(posting("Denver, CO", description=desc)).verdict == "drop"
    # ...but a description saying the role is remote keeps it for the scorer (owner: too wide beats too narrow)
    assert flt.classify(posting("Denver, CO", description=STATE_LIST_WITH_MAINE)).verdict == "unknown"


# --- track ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "track"),
    [
        ("QA Manager", "QA leadership"),
        ("Test Automation Lead", "QA leadership"),
        ("Director of Quality Engineering", "QA leadership"),
        ("Head of QA", "QA leadership"),
        ("Senior SDET", "Core QA"),
        ("Software Engineer in Test II", "Core QA"),
        ("Senior Quality Engineer (Software)", "Core QA"),
        ("QA Analyst", "Core QA"),
        ("Site Reliability Engineer", "SDE pivot"),
        ("Senior Software Engineer, Release Automation", "SDE pivot"),
    ],
)
def test_track_for(title, track):
    assert track_for(title) == track
