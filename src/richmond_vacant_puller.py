"""Richmond City Vacant Building List — vacancy registry feed.

Richmond Property Maintenance & Code Enforcement publishes a Vacant Building
List PDF at a predictable URL pattern under rva.gov/sites/default/files/. This
is a **vacancy registry**, NOT a code violation list — operator-confirmed
distinction (see memory: richmond-code-violations). The actual Richmond code
violation source is the OPP / EnerGov portal (see memory: richmond-opp-energov).

Records here emit as `notice_type="vacant_building"` — a distinct signal type
from `code_violation`. Both are useful distress signals, but they route to
different DataSift lists and serve different lead workflows.

Cadence is bi-annual (~6 months apart), NOT monthly. The puller still uses
content-hash change detection (rather than a calendar schedule) so it stays
robust if Richmond publishes off-cycle.

URL pattern: https://rva.gov/sites/default/files/{YYYY-MM}/Vacant%20Building%20List%20-%20{Month}%20{YYYY}.pdf

Schema (per parsed row):
    Address (with property ZIP), Owner, MailAddress, MailCity, State, MailZip

Output: NoticeData records with notice_type="code_violation" and
source_url="richmond_vacant_building_list://YYYY-MM" so downstream consumers
can distinguish this feed from email-requested code violation imports.
"""

from __future__ import annotations

import hashlib
import logging
import re
import ssl
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError

import config
from notice_parser import NoticeData

logger = logging.getLogger(__name__)


# ── Config ────────────────────────────────────────────────────────────

RICHMOND_VACANT_STATE_FILE: Path = config.PROJECT_ROOT / "richmond_vacant_state.json"
RICHMOND_VACANT_STATE_SCHEMA_VERSION: int = 1

# How many months back to probe when looking for new publications.
# Six is enough to catch a backfill if the puller hasn't run in a while.
MAX_LOOKBACK_MONTHS: int = 6

# Hard cap on records per fetch — protects against a malformed PDF or a
# catastrophic schema change yielding thousands of garbage rows.
MAX_RECORDS_PER_FETCH: int = 5000

# URL template — encoded space (%20) matches the rva.gov filename convention.
URL_TEMPLATE: str = (
    "https://rva.gov/sites/default/files/{ym}/"
    "Vacant%20Building%20List%20-%20{month_name}%20{year}.pdf"
)

USER_AGENT: str = "SiftStack/1.0 (Richmond Vacant Building List puller)"
HTTP_TIMEOUT_SECONDS: int = 30


def _ssl_context() -> ssl.SSLContext:
    """Build an SSL context using certifi if available, falling back to the system store.

    macOS Pythons installed via python.org ship without CA roots wired in, so
    urllib.request defaults to "no trust store" and every HTTPS call fails.
    certifi is a transitive dep of most SiftStack deps (apify, dropbox,
    anthropic, etc.) so it's effectively always present.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        return ssl.create_default_context()


_SSL_CONTEXT: ssl.SSLContext = _ssl_context()


# ── Parsing ───────────────────────────────────────────────────────────

# Splits the Address column "10 E Baker St, 23219" into street + property ZIP.
_ADDR_ZIP_RE = re.compile(r"^(?P<addr>.*?)\s*,\s*(?P<zip>\d{5})\b\s*$")


@dataclass
class VacantRecord:
    """One row parsed from the Vacant Building List PDF."""
    address: str       # Property street + house number
    prop_zip: str      # Property ZIP
    owner_name: str    # Owner of record
    mail_addr: str     # Owner mailing address
    mail_city: str
    mail_state: str
    mail_zip: str


def parse_vacant_pdf(pdf_bytes: bytes) -> list[VacantRecord]:
    """Extract VacantRecord rows via pdfplumber table extraction.

    The Vacant Building List PDF is a six-column table:
        Address | Owner | MailAddress | MailCity | State | MailZip
    pdfplumber's extract_tables() preserves row boundaries even when a single
    cell wraps across multiple physical lines (newlines inside the cell are
    flattened to spaces here).
    """
    try:
        import pdfplumber
    except ImportError as exc:
        raise RuntimeError(
            "pdfplumber required for Richmond Vacant Building List parsing — "
            "add `pdfplumber>=0.11.0` to requirements.txt"
        ) from exc

    records: list[VacantRecord] = []
    with pdfplumber.open(BytesIO(pdf_bytes)) as pdf:
        for page_num, page in enumerate(pdf.pages, 1):
            for table in page.extract_tables() or []:
                for row in table:
                    parsed = _parse_row(row)
                    if parsed is None:
                        continue
                    records.append(parsed)
                    if len(records) >= MAX_RECORDS_PER_FETCH:
                        logger.warning(
                            "MAX_RECORDS_PER_FETCH (%d) hit on page %d — stopping",
                            MAX_RECORDS_PER_FETCH, page_num,
                        )
                        return records
    return records


def _parse_row(row: list[str | None]) -> VacantRecord | None:
    """Convert a raw table row into a VacantRecord, or None for headers/noise."""
    if not row or len(row) < 6:
        return None

    cells = [_clean_cell(c) for c in row[:6]]
    addr_full, owner, mail_addr, mail_city, mail_state, mail_zip = cells

    if not addr_full or addr_full.lower() == "address":
        return None  # header row or empty
    if not mail_state or not mail_zip:
        return None

    m = _ADDR_ZIP_RE.match(addr_full)
    if not m:
        logger.debug("Address column did not match expected pattern: %r", addr_full)
        return None

    address = m.group("addr").strip().rstrip(",")
    prop_zip = m.group("zip")

    if not owner or not mail_addr:
        return None

    return VacantRecord(
        address=address,
        prop_zip=prop_zip,
        owner_name=owner,
        mail_addr=mail_addr,
        mail_city=mail_city,
        mail_state=mail_state.upper(),
        mail_zip=mail_zip,
    )


def _clean_cell(value: str | None) -> str:
    """Flatten multi-line cell contents and trim whitespace/trailing commas."""
    if value is None:
        return ""
    return re.sub(r"\s+", " ", value.replace("\n", " ")).strip().rstrip(",")


# ── State management ─────────────────────────────────────────────────


def _record_key(rec: VacantRecord) -> str:
    """Stable key for a record (address + owner)."""
    h = hashlib.sha256()
    h.update(rec.address.lower().encode("utf-8"))
    h.update(b"|")
    h.update(rec.owner_name.lower().encode("utf-8"))
    return h.hexdigest()[:16]


def _content_hash(pdf_bytes: bytes) -> str:
    return f"sha256:{hashlib.sha256(pdf_bytes).hexdigest()}"


def load_state() -> dict:
    """Load the puller state, returning a fresh shell if missing/empty."""
    state = config.load_state(RICHMOND_VACANT_STATE_FILE)
    if not state:
        return {
            "schema_version": RICHMOND_VACANT_STATE_SCHEMA_VERSION,
            "last_fetched_url": "",
            "last_fetched_month": "",
            "last_fetched_at": "",
            "last_content_hash": "",
            "known_records": {},
        }
    state.setdefault("known_records", {})
    return state


def save_state(state: dict) -> None:
    config.save_state(RICHMOND_VACANT_STATE_FILE, state)


# ── Fetch + URL probing ──────────────────────────────────────────────


def _candidate_urls(today: datetime | None = None) -> list[tuple[str, str]]:
    """Yield (yyyy_mm, url) candidates from current month back MAX_LOOKBACK_MONTHS."""
    today = today or datetime.now(timezone.utc)
    year, month = today.year, today.month
    out: list[tuple[str, str]] = []
    for _ in range(MAX_LOOKBACK_MONTHS):
        ym = f"{year:04d}-{month:02d}"
        month_name = datetime(year, month, 1).strftime("%B")
        url = URL_TEMPLATE.format(ym=ym, month_name=month_name, year=year)
        out.append((ym, url))
        month -= 1
        if month == 0:
            month = 12
            year -= 1
    return out


def _http_head(url: str) -> int:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS, context=_SSL_CONTEXT) as resp:
            return resp.status
    except HTTPError as e:
        return e.code
    except URLError as e:
        logger.warning("HEAD %s failed: %s", url, e)
        return 0


def _http_get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS, context=_SSL_CONTEXT) as resp:
        return resp.read()


def find_newest_published(today: datetime | None = None) -> tuple[str, str] | None:
    """Probe candidate URLs; return (ym, url) of the newest 200 OK, else None."""
    for ym, url in _candidate_urls(today=today):
        status = _http_head(url)
        if status == 200:
            logger.info("Found Vacant Building List for %s at %s", ym, url)
            return ym, url
        if status not in (404, 0):
            logger.warning("Unexpected HEAD status %d for %s", status, url)
    return None


# ── Top-level pull ───────────────────────────────────────────────────


def pull_new_records(today: datetime | None = None) -> list[NoticeData]:
    """Fetch the newest Vacant Building List, diff against state, return new records.

    Returns NoticeData with notice_type='code_violation' for every record
    not previously seen. Records already in state are skipped.
    """
    state = load_state()

    found = find_newest_published(today=today)
    if not found:
        logger.info("No Vacant Building List PDF found in the last %d months", MAX_LOOKBACK_MONTHS)
        return []

    ym, url = found

    # Skip if we already processed this exact URL AND the content hasn't changed.
    # (Same URL with new content is possible if the city re-uploads a corrected file.)
    pdf_bytes = _http_get(url)
    content_hash = _content_hash(pdf_bytes)

    if (
        state.get("last_fetched_url") == url
        and state.get("last_content_hash") == content_hash
    ):
        logger.info("Vacant Building List unchanged since last fetch (%s) — skipping", ym)
        return []

    parsed = parse_vacant_pdf(pdf_bytes)
    if not parsed:
        logger.warning("Vacant Building List parse returned 0 records — refusing to update state")
        return []

    logger.info("Parsed %d records from Vacant Building List %s", len(parsed), ym)

    today_iso = (today or datetime.now(timezone.utc)).strftime("%Y-%m-%d")
    new_records: list[NoticeData] = []
    known = state["known_records"]

    for rec in parsed:
        key = _record_key(rec)
        if key in known:
            continue
        known[key] = {
            "address": rec.address,
            "owner": rec.owner_name,
            "first_seen": ym,
        }
        new_records.append(_to_notice(rec, ym, today_iso, url))

    state["last_fetched_url"] = url
    state["last_fetched_month"] = ym
    state["last_fetched_at"] = datetime.now(timezone.utc).isoformat()
    state["last_content_hash"] = content_hash
    save_state(state)

    logger.info(
        "Vacant Building List delta: %d new / %d total tracked",
        len(new_records),
        len(known),
    )
    return new_records


def _to_notice(rec: VacantRecord, ym: str, today_iso: str, url: str) -> NoticeData:
    """Convert a VacantRecord into a NoticeData with code_violation typing."""
    return NoticeData(
        date_added=today_iso,
        address=rec.address,
        city="Richmond",
        state="VA",
        zip=rec.prop_zip,
        owner_name=rec.owner_name,
        # Vacancy registry — its own signal type, NOT code_violation. The
        # actual Richmond code violation source is the OPP / EnerGov portal
        # (src/richmond_opp_enricher.py). See memory: richmond-code-violations.
        notice_type="vacant_building",
        county="Richmond City",
        source_url=f"richmond_vacant_building_list://{ym}",
        owner_street=rec.mail_addr,
        owner_city=rec.mail_city,
        owner_state=rec.mail_state,
        owner_zip=rec.mail_zip,
    )


# ── Diagnostic helpers ───────────────────────────────────────────────


def parse_local_file(path: Path) -> list[VacantRecord]:
    """Parse a downloaded PDF on disk — used for smoke tests and ad-hoc imports."""
    return parse_vacant_pdf(path.read_bytes())


def iter_known_records(state: dict | None = None) -> Iterable[dict]:
    """Iterate over records currently tracked in state — useful for debugging."""
    s = state if state is not None else load_state()
    for key, entry in s.get("known_records", {}).items():
        yield {"key": key, **entry}
