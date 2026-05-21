"""Unit tests for src/propertyradar_parser.py.

Uses the three golden CSV fixtures from Plan 00. No live PR account,
no Playwright, no network — all tests must complete in < 1 second.
"""

import re
from pathlib import Path

import pytest

from config import BUSINESS_RE, TRUST_NAME_RE
from propertyradar_parser import (
    REQUIRED_PR_COLUMNS,
    _parse_pr_date,
    parse_pr_csv,
)

FIXTURES = Path(__file__).parent / "fixtures"
FORECLOSURE_CSV = FIXTURES / "pr_export_foreclosure.csv"
PRE_PROBATE_CSV = FIXTURES / "pr_export_pre_probate.csv"
BROKEN_CSV = FIXTURES / "pr_export_broken.csv"


# ── REQUIRED_PR_COLUMNS surface ─────────────────────────────────────


def test_required_columns_set_includes_radar_id_and_address():
    # Minimum surface — if these drift, the parser will misalign data.
    for col in (
        "RadarID", "Address", "City", "State", "ZIP Code", "County",
        "Assessed Owner", "Mailing Address", "Mailing City",
        "Mailing State", "Mailing ZIP Code", "Estimated Value",
        "Estimated Equity %", "Year Built",
    ):
        assert col in REQUIRED_PR_COLUMNS, f"missing required column: {col}"


def test_required_columns_is_a_set():
    assert isinstance(REQUIRED_PR_COLUMNS, set)


# ── Date helper ─────────────────────────────────────────────────────


@pytest.mark.parametrize("inp,expected", [
    ("6/15/2026", "2026-06-15"),
    ("06/15/2026", "2026-06-15"),
    ("2026-06-15", "2026-06-15"),
    ("", ""),
    ("   ", ""),
])
def test_parse_pr_date_known_formats(inp, expected):
    assert _parse_pr_date(inp) == expected


def test_parse_pr_date_unrecognized_passes_through():
    # Garbage input must not raise — passes through unchanged
    assert _parse_pr_date("not-a-date") == "not-a-date"


def test_parse_pr_date_none_safe():
    # None input must not raise — returns empty string
    assert _parse_pr_date(None) == ""


# ── Foreclosure fixture ─────────────────────────────────────────────


def test_foreclosure_csv_returns_five_records():
    notices = parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure")
    assert len(notices) == 5


def test_foreclosure_records_have_notice_type_foreclosure():
    for n in parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure"):
        assert n.notice_type == "foreclosure"


def test_foreclosure_source_url_uses_radar_id():
    for n in parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure"):
        assert n.source_url.startswith("propertyradar://radarid/")
        radar_id = n.source_url.split("/")[-1]
        assert re.fullmatch(r"PR\d+", radar_id), f"bad RadarID: {radar_id!r}"


def test_foreclosure_state_is_va_or_md_not_default_tn():
    for n in parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure"):
        assert n.state in {"MD", "VA"}, (
            f"state was {n.state!r} — NoticeData TN default leaked through"
        )


def test_foreclosure_owner_name_uses_primary_contact_when_present():
    notices = parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure")
    # Fixture row 0 has Primary Contact "Jane A Doe" — that wins over
    # Assessed Owner "DOE, JANE A"
    assert notices[0].owner_name == "Jane A Doe"


def test_foreclosure_mailing_fields_populate():
    n = parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure")[0]
    # Fixture row 0: Mailing address matches property (owner-occupied)
    assert n.owner_street == "101 Maple Cove Ln"
    assert n.owner_city == "Richmond"
    assert n.owner_state == "VA"
    assert n.owner_zip == "23220"


def test_foreclosure_mailing_fields_diverge_when_absentee():
    notices = parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure")
    # Fixture row 4 (PR1000005) is absentee — mailing differs from property
    n = notices[4]
    assert n.address == "7702 Rockville Pike Apt 3B"
    assert n.owner_street == "9912 Birchwood Way"
    assert n.owner_city == "Gaithersburg"


def test_foreclosure_auction_date_parsed_to_iso():
    notices = parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure")
    for n in notices:
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", n.auction_date), (
            f"auction_date {n.auction_date!r} not ISO"
        )


def test_foreclosure_records_have_no_deceased_indicator():
    for n in parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure"):
        assert n.deceased_indicator == ""


def test_foreclosure_estimated_value_and_equity_populate():
    n = parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure")[0]
    # String passthrough — no float coercion in the parser
    assert n.estimated_value == "425000"
    assert n.equity_percent == "62"
    assert n.year_built == "1958"


def test_foreclosure_county_populates():
    notices = parse_pr_csv(FORECLOSURE_CSV, notice_type="foreclosure")
    # Counties from fixture: Richmond City, Henrico, Chesterfield, Prince George's, Montgomery
    counties = {n.county for n in notices}
    assert "Richmond City" in counties
    assert "Montgomery" in counties


# ── Pre-probate fixture ─────────────────────────────────────────────


def test_pre_probate_records_have_deceased_indicator_pr_pre_probate():
    notices = parse_pr_csv(PRE_PROBATE_CSV, notice_type="pre_probate")
    assert len(notices) == 5
    for n in notices:
        assert n.deceased_indicator == "pr_pre_probate"


def test_pre_probate_records_have_empty_auction_date():
    notices = parse_pr_csv(PRE_PROBATE_CSV, notice_type="pre_probate")
    for n in notices:
        assert n.auction_date == ""


def test_pre_probate_owner_name_falls_back_to_assessed_owner():
    # Fixture rows have empty Primary Contact Full Name; Assessed Owner
    # is the fallback.
    notices = parse_pr_csv(PRE_PROBATE_CSV, notice_type="pre_probate")
    assert notices[0].owner_name == "BROWN, ALICE M"


def test_pre_probate_entity_named_records_pass_through():
    # Per RESEARCH Pitfall 5: entity-named pre_probate records (LLC,
    # TRUST) are NOT dropped — Phase 3 handles them via heir search.
    notices = parse_pr_csv(PRE_PROBATE_CSV, notice_type="pre_probate")
    entity_names = [
        n.owner_name for n in notices
        if BUSINESS_RE.search(n.owner_name) or TRUST_NAME_RE.match(n.owner_name)
    ]
    assert len(entity_names) >= 2, (
        f"expected at least 2 entity-named records to pass through, "
        f"got {len(entity_names)}: {entity_names}"
    )


def test_pre_probate_mailing_fields_populate():
    # Verify mailing fields map to owner_* quartet for pre_probate too
    notices = parse_pr_csv(PRE_PROBATE_CSV, notice_type="pre_probate")
    n = notices[0]
    assert n.owner_street == "3344 Sycamore Hill Rd"
    assert n.owner_city == "Richmond"
    assert n.owner_state == "VA"
    assert n.owner_zip == "23225"


def test_pre_probate_state_is_va_or_md():
    notices = parse_pr_csv(PRE_PROBATE_CSV, notice_type="pre_probate")
    for n in notices:
        assert n.state in {"MD", "VA"}


def test_pre_probate_notice_type_set():
    notices = parse_pr_csv(PRE_PROBATE_CSV, notice_type="pre_probate")
    for n in notices:
        assert n.notice_type == "pre_probate"


# ── Broken fixture (sanity-check failure path) ──────────────────────


def test_missing_required_column_raises_value_error_naming_column_and_field_set():
    with pytest.raises(ValueError) as exc_info:
        parse_pr_csv(BROKEN_CSV, notice_type="foreclosure")
    msg = str(exc_info.value)
    assert "RadarID" in msg, f"error must name missing column: {msg}"
    assert "SiftStack Export" in msg, (
        f"error must point to the field set: {msg}"
    )
