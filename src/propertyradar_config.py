"""Configuration for the PropertyRadar puller.

Mirrors the shape of src/config.py — credentials from .env, state-file
paths at repo root, dataclass + list-of-instances for the 4 locked lists
per DEC-pr-lists, and thin wrappers around config.save_state /
config.load_state for per-list `last_seen` persistence (PR-03).

Coexistence guarantee (PR-07): every state file path here is distinct
from src/config.py's TN paths (last_run.json, cookies.json).

Selector constants (SEL_PR_*) are placeholders — Plan 03 (selector capture
session) will fill them with real PropertyRadar DOM strings before Plan 04
consumes them.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

import config  # for save_state / load_state — REUSE, do not reimplement

load_dotenv()

logger = logging.getLogger(__name__)


# ── Credentials ────────────────────────────────────────────────────
# Mirrors src/config.py L38-39 pattern.
PROPERTYRADAR_EMAIL = os.getenv("PROPERTYRADAR_EMAIL", "")
PROPERTYRADAR_PASSWORD = os.getenv("PROPERTYRADAR_PASSWORD", "")


# ── State File Paths (coexistence per PR-07) ───────────────────────
# Mirrors src/config.py L14-25 pattern. These paths MUST be distinct
# from STATE_FILE / COOKIES_FILE in src/config.py (TN scraper state).
PROJECT_ROOT = Path(__file__).resolve().parent.parent
PR_STATE_FILE = PROJECT_ROOT / "pr_state.json"
PR_COOKIES_FILE = PROJECT_ROOT / "pr_cookies.json"


# ── Site URLs ──────────────────────────────────────────────────────
# Mirrors src/config.py L68-70. PR_LOGIN_URL / PR_LISTS_URL specific
# path components captured during Plan 03 exploration may differ — the
# puller (Plan 04) must navigate by clicking nav elements where possible
# and only fall back to these URLs when needed.
PR_BASE_URL = "https://app.propertyradar.com"
PR_LOGIN_URL = f"{PR_BASE_URL}/login"           # confirmed during Plan 03 exploration
PR_LISTS_URL = f"{PR_BASE_URL}/lists"           # placeholder — verify in Plan 03


# ── DOM Selectors (placeholders — filled in Plan 03) ───────────────
# Mirrors src/config.py L72-91 pattern (selectors as named constants,
# never inline in puller code so UI changes are one find-and-replace away).
# Plan 03 is a checkpoint:human-verify session where the operator runs
# Playwright in headed mode against the live PR app and captures these.
# Until then, sentinel value "__CAPTURE_IN_PLAN_03__" makes any accidental
# use in Plan 04 code fail loudly.
_SENTINEL = "__CAPTURE_IN_PLAN_03__"
SEL_PR_LOGIN_EMAIL = _SENTINEL
SEL_PR_LOGIN_PASSWORD = _SENTINEL
SEL_PR_LOGIN_SUBMIT = _SENTINEL
SEL_PR_DASHBOARD_SENTINEL = _SENTINEL   # element only present when logged in (for _is_session_valid)
SEL_PR_LIST_NAV = _SENTINEL             # link/button that selects a list by name
SEL_PR_NEW_SINCE_FILTER = _SENTINEL     # calendar picker for "Added to List >= date"
SEL_PR_RESULT_COUNT = _SENTINEL         # the "N properties" read-back (Pitfall 1 guard)
SEL_PR_EXPORT_MENU = _SENTINEL          # "..." menu
SEL_PR_EXPORT_TO_FILE = _SENTINEL       # "Export to File" menu item
SEL_PR_FIELD_SET_PICKER = _SENTINEL     # "SiftStack Export" saved field set dropdown
SEL_PR_EXPORT_CONTINUE = _SENTINEL      # Continue button on export wizard
SEL_PR_EXPORT_PURCHASE = _SENTINEL      # final "Purchase" / commit button (BILLED action)
SEL_PR_EXPORT_CSV_RADIO = _SENTINEL     # CSV format selector
SEL_PR_EXPORT_DOWNLOAD = _SENTINEL      # Download button (sync path)
SEL_PR_DOWNLOADS_AREA = _SENTINEL       # in-app downloads/inbox area (async fallback)

# Membership scrape selectors (PR-08 — free pagination read of list RadarIDs,
# no export billing). Captured in Plan 03 alongside the export-wizard selectors.
SEL_PR_LIST_ROW = _SENTINEL             # one row in the list table (will be queried as page.locator(...).all())
SEL_PR_ROW_RADAR_ID = _SENTINEL         # RadarID cell within a row (relative selector)
SEL_PR_PAGINATION_NEXT = _SENTINEL      # "Next page" button in the list pagination
SEL_PR_PAGINATION_INFO = _SENTINEL      # "1-20 of N" pagination info text (used to detect last page)


# ── List Configuration (locked per DEC-pr-lists) ───────────────────
@dataclass
class PropertyRadarList:
    """A configured PropertyRadar list — locked per DEC-pr-lists.

    Mirrors the SavedSearch dataclass in src/config.py L108-113.
    """
    name: str           # Exact PR-app display name (used to select the list in UI)
    state: str          # "MD" | "VA"
    notice_type: str    # "foreclosure" | "pre_probate"
    slug: str           # Filesystem-safe short id for CSV filenames + log prefixes


# Names match DEC-pr-lists VERBATIM — these are the strings the PR UI
# displays. Any deviation breaks list-selection-by-name in Plan 04.
PROPERTYRADAR_LISTS: list[PropertyRadarList] = [
    PropertyRadarList(
        name="MD_Auction in 90 Days_No Pre-Probate_No Vacant",
        state="MD",
        notice_type="foreclosure",
        slug="md_auction",
    ),
    PropertyRadarList(
        name="VA_Auction in 90 Days_No Pre-Probate_No Vacant",
        state="VA",
        notice_type="foreclosure",
        slug="va_auction",
    ),
    PropertyRadarList(
        name="MD_Pre-Probate_Distress >60_Occupied",
        state="MD",
        notice_type="pre_probate",
        slug="md_preprobate",
    ),
    PropertyRadarList(
        name="VA_Pre-Probate_Distress >60_Occupied",
        state="VA",
        notice_type="pre_probate",
        slug="va_preprobate",
    ),
]


# ── Billing-Disaster Guard Thresholds (per RESEARCH Pitfall 1) ─────
# Read by the puller (Plan 04) BEFORE clicking "Purchase". If the filter
# was set correctly and we're pulling a daily delta, the read-back count
# should be well under this. A higher value almost certainly means the
# filter failed silently and we're about to re-export a full list.
PR_DAILY_DELTA_MAX_RECORDS = int(os.getenv("PR_DAILY_DELTA_MAX_RECORDS", "500"))


# ── State File Schema ──────────────────────────────────────────────
PR_STATE_SCHEMA_VERSION = 1
_DEFAULT_LOOKBACK_DAYS = 7   # per RESEARCH L487 — billing-conservative first-run default


def default_lookback_date() -> str:
    """Return YYYY-MM-DD for `_DEFAULT_LOOKBACK_DAYS` days before today.

    Used when a list has no entry in pr_state.json (first run).
    Mirrors src/scraper.py L728-734 "no previous run found, pulling last N days".
    """
    return (datetime.now() - timedelta(days=_DEFAULT_LOOKBACK_DAYS)).strftime("%Y-%m-%d")


# ── State Persistence Wrappers (REUSE config.save_state/load_state) ─
def load_pr_state() -> dict[str, str]:
    """Load per-list last_seen timestamps. Returns {} if file missing or corrupt.

    Schema:
      {
        "<list.name>": "YYYY-MM-DD",
        ...,
        "_schema_version": 1
      }

    Mirrors src/config.py L169-177 fallback behavior (returns {} on missing).
    """
    return config.load_state(PR_STATE_FILE)


def save_pr_state(state: dict[str, str]) -> None:
    """Persist per-list last_seen state atomically. Stamps schema version.

    Delegates to config.save_state (tmp -> rename + .bak backup) so PR state
    gets the same crash-safe write semantics as TN's last_run.json.
    """
    # Stamp the schema version on every save so a later schema bump is
    # detectable by readers without breaking older files.
    state = dict(state)  # defensive copy — don't mutate caller's dict
    state["_schema_version"] = PR_STATE_SCHEMA_VERSION
    config.save_state(PR_STATE_FILE, state)
