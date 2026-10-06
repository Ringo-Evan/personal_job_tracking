import pytest

from jobsearch.tracker_csv import (
    COMPANIES,
    JOBS,
    PEOPLE,
    TABS,
    detect_tab,
    normalize_label,
    parse_number,
    parse_text,
)


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("Company ID", "company id"),
        ("Company / group", "company group"),
        ("Fit score (1-5, Claude estimate)", "fit score 1 5 claude estimate"),
        ("Fit Score (1–5,  Claude estimate)", "fit score 1 5 claude estimate"),
        ("Screened on (auto)", "screened on"),
        ("Screened on (AUTO)", "screened on"),
        ("  Maine eligible?  ", "maine eligible"),
        ("Connection (cold / warm / referral)", "connection cold warm referral"),
        ("", ""),
    ],
)
def test_normalize_label(label, expected):
    assert normalize_label(label) == expected


@pytest.mark.parametrize("tab", TABS, ids=lambda t: t.name)
def test_labels_are_unambiguous_after_normalizing(tab):
    norms = [normalize_label(label) for label in tab.labels.values()]
    assert len(norms) == len(set(norms))


@pytest.mark.parametrize(
    ("text", "expected"),
    [("4", 4), ("1,000", 1000), ("120,000.50", 120000.5), (" 3 ", 3), ("", None), ("high", None)],
)
def test_parse_number(text, expected):
    assert parse_number(text) == expected


@pytest.mark.parametrize(
    ("label", "tab"), [("Company ID", COMPANIES), ("Job ID", JOBS), ("Person ID", PEOPLE)]
)
def test_detect_tab(label, tab):
    assert detect_tab(f"Title row\n\n{label},Name\nX1,y\n") is tab


def test_detect_tab_unknown():
    assert detect_tab("id,name\n1,a\n") is None


def test_header_must_be_first_cell_exactly():
    # "Company ID" appearing in a later column, or in a title row's second cell, is not a header.
    text = "Report,Company ID\nName,Company ID\nCompany ID,Company / group\nC001,Acme\n"
    result = parse_text(text, COMPANIES)
    assert result.header_row_index == 2
    assert result.rows == [{"company_id": "C001", "company": "Acme"}]


def test_missing_header_raises():
    with pytest.raises(ValueError, match="Company ID"):
        parse_text("a,b\n1,2\n", COMPANIES)


def test_short_rows_leave_trailing_fields_absent():
    result = parse_text("Company ID,Company / group,Sector\nC001,Acme\n", COMPANIES)
    assert result.rows == [{"company_id": "C001", "company": "Acme"}]
