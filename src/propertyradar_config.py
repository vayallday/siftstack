"""Configuration for the PropertyRadar puller.

Mirrors the shape of src/config.py — credentials from .env, state-file
paths at repo root, dataclass + list-of-instances for the 4 locked lists
per DEC-pr-lists, and thin wrappers around config.save_state /
config.load_state for per-list `last_seen` persistence (PR-03).

Coexistence guarantee (PR-07): every state file path here is distinct
from src/config.py's TN paths (last_run.json, cookies.json).

Selector constants (SEL_PR_*) were captured from live PropertyRadar HTML
dumps during Plan 02-03 (see .planning/phases/02-propertyradar-puller/
captured-selectors.py for source provenance + the page-*.html captures
each selector was verified against). Each selector pins to stable anchors
only — `name`, `data-qtip`, `role`, `data-ref`, stable `x-*` / `fr-*`
classes, and visible text via :has-text — NEVER ExtJS auto-ids (those
rotate on every page load).

Sentinel values like "__N_A_*__" and "__TBD_*__" mean the constant is
deliberately not a real selector (see comment beside each); any puller
code that tries to use one as a CSS selector will fail loudly with a
helpful message in the call log.
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
# PropertyRadar is a hash-routed ExtJS SPA — there is no real /login or
# /lists path. The root URL serves the login form when unauthenticated,
# and routes to the app via hash fragments after login.
PR_BASE_URL = "https://app.propertyradar.com"
PR_LOGIN_URL = PR_BASE_URL                          # PR auto-redirects to login when unauthed
PR_LISTS_URL = f"{PR_BASE_URL}/#!/myLists"          # hash route — set in-app, never page.reload()
                                                    # (a full reload drops the session)


# ── DOM Selectors (captured live in Plan 02-03) ────────────────────
# See module docstring for the stability rule. Provenance for each
# selector is in .planning/phases/02-propertyradar-puller/captured-selectors.py.

# Login (ExtJS form on the login page)
SEL_PR_LOGIN_EMAIL     = 'input[name="userEmail"]'
SEL_PR_LOGIN_PASSWORD  = 'input[name="userPW"]'
SEL_PR_LOGIN_AGREEMENT = 'input[name="userAgreement"]'      # MUST be ticked
#   before login — the submit button is `x-btn-disabled` until it's set.
#   The native input is visually hidden, so toggle via ExtJS:
#   use JS_PR_TICK_USER_AGREEMENT below, not a Playwright .click().
SEL_PR_LOGIN_SUBMIT    = 'a.x-btn:has-text("Login")'
#   ExtJS <a class="x-btn">, label text is "Login" (one word). No <button>.

# Dashboard sentinel — non-empty inner text means we're logged in. CAUTION:
# ExtJS instantiates this <label> empty on the login page too, so a bare
# .count() gives a false positive — check inner_text() != "".
SEL_PR_DASHBOARD_SENTINEL = 'label.fr-account-name'

# List navigation — templated per list. {name} = exact PR list name.
SEL_PR_LIST_NAV = '.list-name[data-qtip="{name}"]'

# Filter button (toolbar) — opens the in-grid search box (NOT a criteria
# filter panel). Kept because dump_pages.py uses it to capture the toolbar
# state; the puller itself shouldn't need it.
SEL_PR_FILTER_BTN = 'a.x-btn[data-qtip="Filter results"]'

# Added-Date / "New since" filter — DOES NOT EXIST in PR's UI (user-confirmed).
# The puller's delta strategy is full membership scrape + RadarID set diff,
# not filter-then-export. See:
#   .planning memory: propertyradar-no-added-date-filter
SEL_PR_NEW_SINCE_FILTER = "__N_A_PR_HAS_NO_ADDED_DATE_FILTER__"

# Result count — there is no "N properties" element in the list grid (PR
# uses a buffered/infinite-scroll grid). For PR-08 "done scrolling"
# detection, use JS_PR_GRID_STORE_COUNT below instead.
SEL_PR_RESULT_COUNT = "__TBD_USE_JS_PR_GRID_STORE_COUNT_INSTEAD__"

# Export wizard — TWO pages (verified live 2026-05-23 against MD_Auction;
# the older "single-page" assumption was wrong):
#   Page 1: pick field set in the combobox → click Continue
#   Page 2: review purchase summary  → click Purchase
# Then the "Download Export" modal opens with the CSV/XLSX format radio.
# Purchase is NOT the quota-consuming action — the modal's Download click is.
# See memory: propertyradar-export-wizard-two-step
SEL_PR_EXPORT_MENU       = 'a.x-btn.fr-text-icon-button:has(.icon-pr-more):has-text("Actions")'
SEL_PR_EXPORT_TO_FILE    = '[role="menuitem"]:has-text("Export to File")'
SEL_PR_FIELD_SET_PICKER  = (                          # anchored off the label
    'xpath=//span[contains(@class,"labels")]'         # text — the combo has
    '[contains(.,"Export Field Set")]'                # no stable id.
    '/following::input[@role="combobox"][1]'
)
SEL_PR_FIELD_SET_OPTION  = '.x-boundlist-item:has-text("{name}")'   # templated;
#   {name} = the saved field-set's exact display name, default PR_FIELD_SET_NAME.
SEL_PR_EXPORT_CONTINUE   = 'a[role="button"]:has-text("Continue")'
SEL_PR_EXPORT_PURCHASE   = 'a[role="button"]:has-text("Purchase")'   # opens the
#   Download modal — does NOT consume quota. Quota is consumed when the modal's
#   Download button is clicked.

# Download modal (opens after Purchase)
SEL_PR_DOWNLOAD_MODAL    = '[id^="downloadExporFile-"]'              # id prefix is
#   stable; the numeric suffix changes per session.
SEL_PR_EXPORT_CSV_RADIO  = '.x-form-cb-label:has-text("Comma delimited")'
#   ExtJS styled radio — the native input is visually hidden, so click the
#   label to toggle. The other option in the modal is "Excel (.xlsx)".
SEL_PR_EXPORT_DOWNLOAD   = (
    '[id^="downloadExporFile-"] '
    'a[role="button"]:has-text("Download")'
)   # scoped to the modal; renders x-btn-disabled until a format radio is picked.

# In-app downloads / inbox area for the ASYNC export path (large lists
# delivered via email + queue) — not yet captured; the captured sync
# Download button covers small lists. Capture when a list ever hits the
# async threshold.
SEL_PR_DOWNLOADS_AREA = "__TBD_ASYNC_PATH_NOT_YET_CAPTURED__"

# List membership scrape (PR-08)
SEL_PR_LIST_ROW = 'tr.x-grid-row'        # use page.locator(...).all() to enumerate

# RadarID extraction is NOT a CSS selector. PropertyRadar's grid renders the
# "Radar ID" column with display:none even when the user adds it to the view
# (verified May 2026), so no <td> cells are emitted. Read directly from the
# ExtJS grid Store via JS_PR_GRID_STORE_RADARIDS instead. Do NOT use the row
# table's `data-recordid` — values like 3074/3075 are ExtJS internal store
# indexes, not RadarIDs. See memory: propertyradar-radarid-column
SEL_PR_ROW_RADAR_ID = "__USE_JS_PR_GRID_STORE_RADARIDS__"

# Pagination — PR's grid is buffered/infinite-scroll, not paginated. There
# is no Next button and no "1-20 of N" element. Scroll the grid until
# store.getCount() stabilises. See memory: propertyradar-no-added-date-filter
SEL_PR_PAGINATION_NEXT = "__N_A_PR_GRID_IS_INFINITE_SCROLL__"
SEL_PR_PAGINATION_INFO = "__N_A_PR_GRID_IS_INFINITE_SCROLL__"


# ── In-page JS snippets (page.evaluate(), not page.locator()) ──────
# Use these where a DOM selector won't work — clicks on visually-hidden
# ExtJS controls, ExtJS-model writes that bypass styled wrappers, and
# reads from the ExtJS Store independent of column visibility.

JS_PR_TICK_USER_AGREEMENT = (
    "Ext.ComponentQuery.query('[name=userAgreement]')[0].setValue(true)"
)

JS_PR_GRID_STORE_RADARIDS = """
(() => {
    const grids = Ext.ComponentQuery.query('grid');
    const g = grids.find(g => g.isVisible && g.isVisible());
    if (!g) return null;
    // PR's grid is backed by an Ext.data.BufferedStore (PageMap-based).
    // Diagnostic-verified on 2026-05-23:
    //   store.getRange()                 → THROWS on buffered store
    //   store.data.items                 → undefined (PageMap has no items)
    //   store.getRange(0, count-1)       → returns full Array of records
    //   store.getAt(0).data.RadarID      → 'P9959E64' (real ID format)
    // Ext auto-loads any missing pages during getRange(0, count-1).
    const store = g.getStore();
    const count = store.getCount();
    if (!count) return [];
    const records = store.getRange(0, count - 1);
    return records
        .filter(r => r && r.data && r.data.RadarID)
        .map(r => r.data.RadarID);
})()
"""

JS_PR_GRID_STORE_COUNT = """
(() => {
    const grids = Ext.ComponentQuery.query('grid');
    const g = grids.find(g => g.isVisible && g.isVisible());
    return g ? g.getStore().getCount() : null;
})()
"""

# Prime the BufferedStore's page cache + report load progress.
#
# Verified live 2026-05-23: PR uses a buffered/PageMap store. `getCount()`
# returns `totalCount` from the server's metadata response, so it stabilises
# almost immediately. But that's distinct from "the records are loaded" —
# the first `store.getRange(0, count-1)` call returns `[]` and asynchronously
# kicks off the page fetches that the range needs. Subsequent calls return
# the real records as the pages settle.
#
# Returning {ready, loaded, total} lets the Python caller poll until
# loaded === total (with a timeout), then scrape with confidence.
JS_PR_GRID_STORE_LOADED_CHECK = """
(() => {
    const grids = Ext.ComponentQuery.query('grid');
    const g = grids.find(g => g.isVisible && g.isVisible());
    if (!g) return {ready: false, reason: 'no-grid'};
    const store = g.getStore();
    if (!store) return {ready: false, reason: 'no-store'};
    const total = typeof store.getCount === 'function' ? store.getCount() : 0;
    if (!total) return {ready: true, loaded: 0, total: 0};
    // Calling getRange both READS what's cached AND triggers Ext to
    // fetch any uncached pages — so this snippet doubles as the prime.
    const records = store.getRange(0, total - 1);
    let loaded = 0;
    for (let i = 0; i < records.length; i++) {
        const r = records[i];
        if (r && r.data && r.data.RadarID) loaded++;
    }
    return {ready: loaded === total, loaded, total};
})()
"""

# Used by the puller's exit-detection fold (formerly plan 02-07). Returns a
# compact list of last-known property fields per loaded row, so the puller can
# preserve enough metadata to emit synthetic exit/reentry NoticeData when a
# property leaves the list later. Only the fields below are persisted — full
# 250-field records would bloat pr_state.json.
JS_PR_GRID_STORE_RECORDS = """
(() => {
    const grids = Ext.ComponentQuery.query('grid');
    const g = grids.find(g => g.isVisible && g.isVisible());
    if (!g) return null;
    // See JS_PR_GRID_STORE_RADARIDS for the BufferedStore PageMap details.
    const store = g.getStore();
    const count = store.getCount();
    if (!count) return [];
    const records = store.getRange(0, count - 1);
    return records
        .filter(r => r && r.data && r.data.RadarID)
        .map(r => ({
            RadarID:  String(r.data.RadarID),
            Address:  r.data.Address || '',
            City:     r.data.City || '',
            State:    r.data.State || '',
            ZIP:      r.data.ZIP || '',
            Owner:    r.data.Owner || '',
            County:   r.data.County || '',
        }));
})()
"""


# ── Lifecycle state values for pr_state.json v2 schema ────────────
# Each list member is tagged active / exited / reentered in the registry.
# Transitions:
#   absent → active           (first observation)
#   active → exited           (was in last run, not in this run)
#   exited → reentered        (was exited, observed again)
#   reentered → active        (promoted on the NEXT run after re-entry)
LIFECYCLE_ACTIVE    = "active"
LIFECYCLE_EXITED    = "exited"
LIFECYCLE_REENTERED = "reentered"

# Bump when the on-disk schema changes shape. v1 = list[RadarID]. v2 = dict
# keyed by RadarID with {first_seen, last_seen, exited_at, status, data}.
PR_STATE_SCHEMA_VERSION_V2 = 2


# ── Export configuration ───────────────────────────────────────────
# The PR-side field set the puller picks in the export wizard. Must
# exist as a saved User Fieldset in the PropertyRadar account (created
# manually one time — see plan 02-03 task notes for the field list).
PR_FIELD_SET_NAME = "SiftStack Export"


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


# ── Daily-Delta Sanity Guard ───────────────────────────────────────
# Read by the puller BEFORE the Purchase + Download in the export modal.
# Since PR has no Added-Date filter, the delta comes from the membership
# scrape diffing against the previous run's RadarID set. A delta larger
# than this threshold almost certainly means the previous state file is
# stale/missing and the puller is about to re-export an entire list —
# halt and require manual confirmation rather than burn export quota.
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
