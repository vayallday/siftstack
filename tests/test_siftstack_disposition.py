"""Phase 4 — additive `SiftStack` disposition list in the `Lists` column.

Covers DSP-01 (additive list assignment) + DSP-02 (`SiftStack` list exists in
DataSift — operator-confirmed pre-Phase-4; no auto-create exercise needed).

Each test guards a single contract with DataSift's live state:

  - The `Lists` cell carries BOTH per-type list and `SiftStack`, comma-delimited
  - Casing is `SiftStack` (camel-case, NOT `Siftstack`) — matches the live list name
  - Delimiter is `,` (CSV auto-quotes the cell because the value contains the
    column delimiter; DataSift's upload wizard splits the quoted cell)
  - Records with missing/unmapped notice_type still get the disposition
    assignment — they upload with just `SiftStack` in the Lists column
  - The per-type list (Foreclosure, Pre-Probate, etc.) is the FIRST segment,
    `SiftStack` is the SECOND — order matters for DataSift's wizard behavior

These tests run against `_notice_to_row` (the per-record mapping) and against
`format_for_datasift` (the CSV-writing wrapper) to cover both the in-memory
dict shape and the on-disk CSV escaping behavior.
"""

import csv
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ── Constants in module ─────────────────────────────────────────────

def test_siftstack_list_name_constant_uses_camel_case():
    """Live DataSift list is `SiftStack` (operator-confirmed Phase 4 kickoff).
    CLAUDE.md `My Defaults` originally said `Siftstack` — that was wrong, see
    Phase 4 closure for the reconciliation. If anyone reverts to lowercase,
    DataSift's case-sensitive list lookup will silently route records to a
    list that doesn't exist."""
    from datasift_formatter import SIFTSTACK_LIST_NAME
    assert SIFTSTACK_LIST_NAME == "SiftStack", (
        f"SIFTSTACK_LIST_NAME drifted to {SIFTSTACK_LIST_NAME!r}. "
        f"DataSift's live list is `SiftStack` (two capitals)."
    )


def test_list_delimiter_is_comma():
    """DataSift's CSV upload wizard splits the `Lists` cell on `,` to derive
    multi-list memberships. Changing this would break additive routing."""
    from datasift_formatter import LIST_DELIMITER
    assert LIST_DELIMITER == ",", (
        f"LIST_DELIMITER drifted to {LIST_DELIMITER!r}. DataSift expects comma."
    )


# ── _build_lists_value across all 8 notice types ────────────────────

def _make_notice(notice_type: str | None = None):
    """Minimal NoticeData with just enough to exercise _build_lists_value."""
    from notice_parser import NoticeData
    return NoticeData(notice_type=notice_type or "")


def test_foreclosure_lists_has_both_per_type_and_siftstack():
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("foreclosure")) == "Foreclosure,SiftStack"


def test_probate_lists_has_both_per_type_and_siftstack():
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("probate")) == "Probate,SiftStack"


def test_pre_probate_lists_has_both_per_type_and_siftstack():
    """Phase 3 + Phase 4 interaction: pre_probate's per-type list name is
    `Pre-Probate` (with hyphen), and now additionally lands in `SiftStack`."""
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("pre_probate")) == "Pre-Probate,SiftStack"


def test_tax_sale_lists_has_both_per_type_and_siftstack():
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("tax_sale")) == "Tax Sale,SiftStack"


def test_tax_delinquent_lists_has_both_per_type_and_siftstack():
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("tax_delinquent")) == "Tax Delinquent,SiftStack"


def test_eviction_lists_has_both_per_type_and_siftstack():
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("eviction")) == "Eviction,SiftStack"


def test_code_violation_lists_has_both_per_type_and_siftstack():
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("code_violation")) == "Code Violation,SiftStack"


def test_divorce_lists_has_both_per_type_and_siftstack():
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("divorce")) == "Divorce,SiftStack"


# ── Fallback paths ──────────────────────────────────────────────────

def test_missing_notice_type_still_gets_siftstack_disposition():
    """A NoticeData without a notice_type should still upload to the
    SiftStack disposition list — the operator's defaults make this the
    catch-all destination for every record."""
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("")) == "SiftStack"


def test_unmapped_notice_type_still_gets_siftstack_disposition():
    """If a future notice_type is added to NoticeData but forgotten in
    NOTICE_TYPE_TO_LIST, the per-type lookup returns '' and the cell
    collapses to just `SiftStack` — the record still disposes correctly,
    but the cross-module invariant test in test_propertyradar_config.py
    catches the omission for PR-sourced types."""
    from datasift_formatter import _build_lists_value
    assert _build_lists_value(_make_notice("future_notice_type_2027")) == "SiftStack"


# ── Per-type segment ordering ───────────────────────────────────────

def test_per_type_list_is_first_segment_siftstack_is_second():
    """Ordering convention: per-type → SiftStack. DataSift's wizard may use
    the first segment as the 'primary' list for filter presets; SiftStack is
    the additive disposition. Reversing the order risks subtle filter behavior."""
    from datasift_formatter import _build_lists_value
    value = _build_lists_value(_make_notice("foreclosure"))
    segments = value.split(",")
    assert len(segments) == 2, f"Expected 2 segments, got {len(segments)}: {value!r}"
    assert segments[0] == "Foreclosure"
    assert segments[1] == "SiftStack"


# ── End-to-end: _notice_to_row carries the additive value ──────────

def test_build_row_lists_field_has_additive_value():
    """`_build_row` is the per-record mapping called during CSV
    export. The "Lists" key in its output must contain the additive
    value (not just per-type)."""
    from datasift_formatter import _build_row
    from notice_parser import NoticeData
    notice = NoticeData(
        notice_type="pre_probate",
        address="100 Main St",
        city="Henrico",
        state="VA",
        zip="23223",
        owner_name="Jane Doe",
        county="Henrico",
    )
    row = _build_row(notice)
    assert row["Lists"] == "Pre-Probate,SiftStack", (
        f"`_build_row` Lists field drifted: {row['Lists']!r}"
    )


# ── CSV escaping: DictWriter must auto-quote the multi-list cell ───

def test_csv_writer_auto_quotes_multi_list_cell(tmp_path):
    """The CSV writer must quote the Lists cell because its value contains
    the column delimiter `,`. Without quoting, DataSift's parser would
    split `Foreclosure,SiftStack` into two columns instead of one cell
    carrying two list memberships.

    Uses the same `csv.DictWriter` / `DATASIFT_COLUMNS` setup as
    `write_datasift_csv` (the production CSV writer), so this is a true
    contract test for the on-disk shape DataSift will receive.
    """
    from datasift_formatter import _build_row, DATASIFT_COLUMNS
    from notice_parser import NoticeData
    notice = NoticeData(
        notice_type="foreclosure",
        address="200 Oak Ave",
        city="Richmond",
        state="VA",
        zip="23223",
        owner_name="John Smith",
        county="Henrico",
        date_added="2026-05-23",
    )
    out_path = tmp_path / "datasift_test.csv"
    with open(out_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=DATASIFT_COLUMNS)
        writer.writeheader()
        writer.writerow(_build_row(notice))

    # Read the raw text and confirm the Lists cell is quoted
    raw = out_path.read_text(encoding="utf-8")
    assert '"Foreclosure,SiftStack"' in raw, (
        f"Lists cell `Foreclosure,SiftStack` must be CSV-quoted in the "
        f"written file (because its value contains the column delimiter). "
        f"Raw bytes: {raw[:500]!r}"
    )

    # And confirm a CSV reader correctly parses it back as a single cell
    with open(out_path, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    assert rows[0]["Lists"] == "Foreclosure,SiftStack", (
        f"Round-trip via csv.DictReader didn't recover the additive value. "
        f"Got: {rows[0]['Lists']!r}"
    )
