"""Parse PropertyRadar CSV exports into NoticeData.

Pure-function module — no Playwright, no network. Input: a CSV file path
plus a notice_type string ("foreclosure" or "pre_probate"). Output: a list
of NoticeData instances ready for the standard enrichment pipeline.

Mirrors src/data_formatter.py's `read_csv` (canonical CSV → NoticeData
pattern, BOM-tolerant utf-8-sig + name-keyed DictReader) but maps
PR-specific column names instead of Sift's.

Per RESEARCH Pitfall 6: column names are name-keyed via DictReader (never
positional indexes) so a PR app field-set edit will not silently misalign
columns. A startup column-presence sanity check raises ValueError with a
clear message if the field set drifts.

Per RESEARCH A4: RadarID is the stable per-property primary key; we use
it as the `source_url` for dedup (`propertyradar://radarid/{id}`).

Per DEC-pre-probate-owner-name: for `pre_probate` records, owner_name is
the deceased property owner from PR; deceased_indicator is set to
"pr_pre_probate" so Phase 3 (NT-03) can route through heir search.

Per RESEARCH Pitfall 5: entity-named pre_probate records (LLC, TRUST) are
passed through (not dropped); Phase 3 handles heir resolution.

Per DEC-foreclosure-filter: PR-sourced foreclosure records bypass the
existing `foreclosure_filter.py` regex — PR's list-level "Foreclosure
Stage" filter has already done the gating upstream. This parser does
not call into foreclosure_filter; the puller (Plan 04) likewise skips
it for PR-sourced records.
"""

import csv
import logging
import re
from datetime import datetime
from pathlib import Path

# Re-exported so Phase 3 modules can do `from propertyradar_parser import BUSINESS_RE`
# without bouncing through `config` directly. Entity detection on PR records
# (Pitfall 5) uses these regexes — the parser itself does NOT drop entity-named
# records; it merely passes them through for Phase 3 routing.
from config import BUSINESS_RE, TRUST_NAME_RE, ESTATE_OF_RE  # noqa: F401
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Required columns (Pitfall 6 sanity check) ────────────────────
# These columns MUST appear in every PR export. If any are missing the
# "SiftStack Export" field set in the PR app has drifted and the parser
# raises a clear ValueError instead of silently producing empty NoticeData.
#
# Column names verified live 2026-05-23 against the actual export from
# MD_Auction. PR uses abbreviated headers — earlier assumed names like
# "RadarID" / "Mailing Address" / "Estimated Value" don't exist; the
# real headers are below.
REQUIRED_PR_COLUMNS: set[str] = {
    "Radar ID",          # space — not "RadarID"
    "Address",
    "City",
    "State",
    "ZIP",               # not "ZIP Code"
    "County",
    "Owner",             # not "Assessed Owner"
    "Mail Address",      # "Mail" — not "Mailing"
    "Mail City",
    "Mail State",
    "Mail ZIP",
    "Est Value",         # not "Estimated Value"
    "Est Equity %",      # not "Estimated Equity %"
    "Yr Built",          # not "Year Built"
}


# ── Date helpers ──────────────────────────────────────────────────
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

# RadarID shape: 'P' followed by alphanumerics, no whitespace. Verified
# live 2026-05-23 against MD_Auction (e.g. P9959E64, PDCF6328) and across
# the synthetic test fixtures (PR1000001 style). The check exists to drop
# PR's trailing license-disclaimer footer that DictReader maps into the
# first column as text — that text starts with 'The information...'
# and contains spaces, so the regex rejects it without being overly
# specific about the exact RadarID character set.
_RADAR_ID_RE = re.compile(r"^P[A-Z0-9]+$", re.IGNORECASE)


def _parse_pr_date(value: str | None) -> str:
    """Convert PR's date format to ISO (YYYY-MM-DD).

    Accepts: '6/15/2026', '06/15/2026', '2026-06-15', '', None.
    Returns '' for empty/None input. Returns input unchanged if unparseable
    (logged at DEBUG — never raises).

    Mirrors src/data_formatter.py L341-357 `_parse_sift_date`.
    """
    if not value:
        return ""
    value = value.strip()
    if not value:
        return ""
    if _ISO_DATE_RE.match(value):
        return value
    # Try common M/D/YYYY and MM/DD/YYYY variants — strptime's %m and %d
    # both accept 1- or 2-digit values, so a single format string covers both.
    try:
        return datetime.strptime(value, "%m/%d/%Y").strftime("%Y-%m-%d")
    except ValueError:
        pass
    logger.debug("Unrecognized PR date format: %r — passing through", value)
    return value


# ── Row → NoticeData ─────────────────────────────────────────────
def _row_to_noticedata(row: dict, notice_type: str) -> NoticeData:
    """Map one PR CSV row to a NoticeData instance.

    Per DEC-pre-probate-owner-name: owner_name for pre_probate is the
    deceased property owner; deceased_indicator flags it for Phase 3.

    Owner-name resolution prefers "Primary Name" (populated when PR
    enrichment ran on the list — the actual person to contact) and
    falls back to "Owner" (always populated; the legal owner).
    Auction date for foreclosure comes from "Orig Sale Date".
    """
    owner_name = (
        (row.get("Primary Name") or "").strip()
        or (row.get("Owner") or "").strip()
    )

    n = NoticeData(
        date_added=datetime.now().strftime("%Y-%m-%d"),
        auction_date=_parse_pr_date(row.get("Orig Sale Date", "")),
        address=(row.get("Address") or "").strip(),
        city=(row.get("City") or "").strip(),
        state=(row.get("State") or "").strip(),
        zip=(row.get("ZIP") or "").strip(),
        owner_name=owner_name,
        notice_type=notice_type,
        county=(row.get("County") or "").strip(),
        source_url=f"propertyradar://radarid/{(row.get('Radar ID') or '').strip()}",
        owner_street=(row.get("Mail Address") or "").strip(),
        owner_city=(row.get("Mail City") or "").strip(),
        owner_state=(row.get("Mail State") or "").strip(),
        owner_zip=(row.get("Mail ZIP") or "").strip(),
        estimated_value=(row.get("Est Value") or "").strip(),
        equity_percent=(row.get("Est Equity %") or "").strip(),
        year_built=(row.get("Yr Built") or "").strip(),
        raw_text="",  # PR has no notice body
    )

    # Per DEC-pre-probate-owner-name: pre_probate owner_name is the
    # deceased — flag for Phase 3's heir-search routing. Foreclosure
    # records leave deceased_indicator empty.
    if notice_type == "pre_probate":
        n.deceased_indicator = "pr_pre_probate"

    return n


# ── Public entry point ────────────────────────────────────────────
def parse_pr_csv(csv_path: str | Path, notice_type: str) -> list[NoticeData]:
    """Parse a PropertyRadar CSV export into NoticeData instances.

    Args:
        csv_path: Path to a CSV file (utf-8 or utf-8-with-BOM).
        notice_type: "foreclosure" or "pre_probate" — set by the puller
            based on the source PR list (per DEC-pr-lists).

    Returns:
        List of NoticeData. Order preserved from CSV. An empty (header-only)
        CSV returns `[]`.

    Raises:
        ValueError: if any column in REQUIRED_PR_COLUMNS is missing from
            the CSV header. Message names the missing columns and points
            to the "SiftStack Export" field set in the PR app.
    """
    csv_path = Path(csv_path)
    notices: list[NoticeData] = []

    with csv_path.open(encoding="utf-8-sig") as f:  # utf-8-sig = BOM-tolerant
        reader = csv.DictReader(f)
        fieldnames = set(reader.fieldnames or [])
        missing = REQUIRED_PR_COLUMNS - fieldnames
        if missing:
            raise ValueError(
                f"PR CSV {csv_path.name} missing required columns: "
                f"{sorted(missing)}. Check the 'SiftStack Export' field "
                f"set in the PropertyRadar app — it has drifted."
            )
        for row in reader:
            # Skip the trailing license-disclaimer footer that PR appends
            # to every export. DictReader maps its single cell into the
            # first column, so we can't just check for empty — the value
            # is the disclaimer text. Real RadarIDs match _RADAR_ID_RE.
            rid = (row.get("Radar ID") or "").strip()
            if not _RADAR_ID_RE.match(rid):
                if rid:
                    logger.debug("Skipping non-RadarID row: %r", rid[:80])
                continue
            notices.append(_row_to_noticedata(row, notice_type=notice_type))

    logger.info(
        "Parsed %d PR records as notice_type=%r from %s",
        len(notices), notice_type, csv_path,
    )
    return notices
