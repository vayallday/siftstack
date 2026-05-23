"""TN-specific configuration — archived from src/config.py.

Everything in this module was previously at the top level of ``config.py``
but is only used by the archived TN public-notice scraper (now under
``src/_legacy_tn/``). It's preserved here so the archive is self-contained
and could be reconstructed if a future market ever needs the same shape of
ASP.NET/reCAPTCHA scrape pipeline as a starting point.

NOT IMPORTED by any active code path. Top-level ``config.py`` no longer
carries any of these symbols.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ── Paths (TNPN scraper state files) ──────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
STATE_FILE = PROJECT_ROOT / "last_run.json"                       # daily-mode cursor
SEEN_IDS_FILE = PROJECT_ROOT / "seen_ids.json"                    # cross-run notice dedup
SEEN_IDS_PRUNE_DAYS = 90
# Notices that exhausted all CAPTCHA retries during scraping.
CAPTCHA_FAILED_IDS_FILE = PROJECT_ROOT / "captcha_failed_ids.json"
CAPTCHA_FAILED_PRUNE_DAYS = 14
COOKIES_FILE = PROJECT_ROOT / "cookies.json"                      # TNPN session

# ── Credentials (TNPN login + 2Captcha) ────────────────────────────────
TNPN_EMAIL = os.getenv("TNPN_EMAIL", "")
TNPN_PASSWORD = os.getenv("TNPN_PASSWORD", "")
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")  # 2Captcha API key

# ── Site URLs (tnpublicnotice.com) ─────────────────────────────────────
BASE_URL = "https://www.tnpublicnotice.com"
LOGIN_URL = f"{BASE_URL}/authenticate.aspx"
SMART_SEARCH_URL = f"{BASE_URL}/Smartsearch/Default.aspx"

# ── ASP.NET Selectors (TNPN-specific) ──────────────────────────────────
# Login form
SEL_LOGIN_EMAIL = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtEmailAddress"
SEL_LOGIN_PASSWORD = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtPassword"
SEL_LOGIN_SUBMIT = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_btnAuth"

# Smart Search dashboard
SEL_SAVED_SEARCHES_DROPDOWN = "#ctl00_ContentPlaceHolder1_as1_ddlSavedSearches"
SEL_PER_PAGE_DROPDOWN = 'select[name$="ddlPerPage"]'

# Search results (authenticated grid)
SEL_RESULTS_GRID = "#ctl00_ContentPlaceHolder1_WSExtendedGrid1_GridView1"
SEL_VIEW_BUTTON_PATTERN = "input[name$='btnView']"
SEL_NEXT_PAGE_BUTTON = "input[title='Next page']"
SEL_PAGE_INFO = "td:has-text('Page ')"

# Notice detail page
SEL_CAPTCHA_IFRAME = "iframe[src*='recaptcha']"
SEL_VIEW_NOTICE_BUTTON = "#ctl00_ContentPlaceHolder1_PublicNoticeDetailsBody1_btnViewNotice"
RECAPTCHA_SITEKEY = "6LdtSg8sAAAAADTdRyZxJ2R2sS82pKALNMvMqSyL"

# ── TNPN pagination ────────────────────────────────────────────────────
RESULTS_PER_PAGE = 50  # max the site allows

# ── Notice Types (subset originally tracked by TN scraper) ─────────────
NOTICE_TYPES = ["foreclosure", "probate"]


@dataclass
class SavedSearch:
    """Represents a saved search on tnpublicnotice.com."""
    county: str
    notice_type: str  # One of NOTICE_TYPES
    saved_search_name: str  # Exact name in the Saved Searches dropdown


# ── Saved Searches ─────────────────────────────────────────────────────
# These names must match exactly what appears in the dropdown on the site.
SAVED_SEARCHES: list[SavedSearch] = [
    SavedSearch("Knox", "foreclosure", "Foreclosure V2 Knox"),
    SavedSearch("Blount", "foreclosure", "Foreclosure V2 Blount"),
]
