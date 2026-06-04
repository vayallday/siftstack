"""Configuration for the Virginia Public Notice (VPA) acquisition source.

Site: https://www.publicnoticevirginia.com/ — the Virginia Press Association's
statewide public-notice registry. It runs on the SAME ASP.NET "Smart Search"
platform as the archived tnpublicnotice.com (note the identical
``authenticate.aspx`` / ``Smartsearch/Default.aspx`` / session-``(S(...))`` URLs,
``WSExtendedGrid`` results grid, and ``as1_`` search controls), so the archived
TN scraper under ``src/_legacy_tn/`` is the structural template.

Two differences from the TN integration:
  1. We drive the **public Advanced Search form** directly (category preset +
     county checkboxes + date range + Go) instead of relying on operator-created
     Saved Searches — self-contained, no manual UI setup.
  2. The public results grid is viewable anonymously, but opening a full notice
     **detail requires a free VPA "Smart Search" login** (and the detail page is
     reCAPTCHA-gated, same as the TN twin). Hence VAPN_EMAIL/VAPN_PASSWORD +
     the existing CAPTCHA_API_KEY (2Captcha).

All selectors below were VERIFIED against the live site: the public Search.aspx
on 2026-06-02 and the authenticated results/detail flow on 2026-06-03 (login,
county CheckBoxList, results grid, btnView postback → Details.aspx?...&ID=,
reCAPTCHA sitekey, btnViewNotice). The full path login → search → classify →
detail → captcha solve → LLM extract was confirmed end-to-end.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import config

# ── Credentials ────────────────────────────────────────────────────────
VAPN_EMAIL = os.getenv("VAPN_EMAIL", "")
VAPN_PASSWORD = os.getenv("VAPN_PASSWORD", "")
# 2Captcha key — reused from the existing account (already in .env as
# CAPTCHA_API_KEY for the archived TN scraper).
CAPTCHA_API_KEY = os.getenv("CAPTCHA_API_KEY", "")

# ── Site URLs ──────────────────────────────────────────────────────────
BASE_URL = "https://www.publicnoticevirginia.com"
LOGIN_URL = f"{BASE_URL}/authenticate.aspx"
SEARCH_URL = f"{BASE_URL}/Search.aspx"            # public Advanced Search form
SMART_SEARCH_URL = f"{BASE_URL}/Smartsearch/Default.aspx"
SIGNUP_URL = f"{BASE_URL}/SmartSearchSignup.aspx"  # free account creation

# ── State files ────────────────────────────────────────────────────────
# Single state file holds the daily-mode cursor + the cross-run seen-notice
# cache (so we never re-open/re-captcha a detail we've already parsed).
STATE_FILE = config.PROJECT_ROOT / "va_public_notice_state.json"
STATE_SCHEMA_VERSION = 1
SEEN_IDS_PRUNE_DAYS = 90
COOKIES_FILE = config.PROJECT_ROOT / "va_public_notice_cookies.json"  # session reuse

# ── ASP.NET selectors ──────────────────────────────────────────────────
# Login form — VERIFIED (auth recon 2026-06-03): same control IDs as the TN twin.
SEL_LOGIN_EMAIL = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtEmailAddress"
SEL_LOGIN_PASSWORD = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_txtPassword"
SEL_LOGIN_SUBMIT = "#ctl00_ContentPlaceHolder1_AuthenticateIPA1_btnAuth"

# Advanced Search form — VERIFIED (recon) on public Search.aspx.
SEL_POPULAR_SEARCHES = "#ctl00_ContentPlaceHolder1_as1_ddlPopularSearches"
SEL_KEYWORD = "#ctl00_ContentPlaceHolder1_as1_txtSearch"
SEL_EXCLUDE = "#ctl00_ContentPlaceHolder1_as1_txtExclude"
SEL_MATCH_TYPE_RADIO = "input[name='ctl00$ContentPlaceHolder1$as1$rdoType']"  # AND/OR/EXACT
# County is a CheckBoxList: each item is `..._lstCounty_{i}` with an adjacent
# <label> carrying the locality name. We resolve target localities to indices
# at runtime by reading the label text (robust to ordering changes).
SEL_COUNTY_CHECKBOX_PREFIX = "ctl00_ContentPlaceHolder1_as1_lstCounty_"
SEL_CITY_CHECKBOX_PREFIX = "ctl00_ContentPlaceHolder1_as1_lstCity_"
# Date range — VERIFIED (recon).
SEL_DATE_LASTDAYS_RADIO = "#ctl00_ContentPlaceHolder1_as1_rbLastNumDays"
SEL_DATE_LASTDAYS_TXT = "#ctl00_ContentPlaceHolder1_as1_txtLastNumDays"
SEL_DATE_RANGE_RADIO = "#ctl00_ContentPlaceHolder1_as1_rbRange"
SEL_DATE_FROM = "#ctl00_ContentPlaceHolder1_as1_txtDateFrom"
SEL_DATE_TO = "#ctl00_ContentPlaceHolder1_as1_txtDateTo"
SEL_GO = "#ctl00_ContentPlaceHolder1_as1_btnGo"

# Results grid — VERIFIED (auth recon): grid id is WSExtendedGridNP1_GridView1.
SEL_RESULTS_GRID = "table[id*='WSExtendedGrid'][id$='GridView1']"
SEL_RESULT_ROW = f"{SEL_RESULTS_GRID} tr"
SEL_PER_PAGE_DROPDOWN = "select[name$='ddlPerPage']"
SEL_NEXT_PAGE_BUTTON = "input[title='Next page']"
SEL_PAGE_INFO = "td:has-text('Page ')"
# A result's "open detail" control — VERIFIED (auth recon): each notice row has
# an ASP.NET postback button whose id contains 'btnView' (rendered 'btnView2').
# Clicking it navigates to Details.aspx (NOT an href link).
SEL_VIEW_BUTTON_PATTERN = "input[id*='btnView']"

# Notice detail page — VERIFIED (auth recon): Details.aspx?SID=...&ID=NNNNNN.
# reCAPTCHA v2 gate; submit button id ends with 'btnViewNotice' ("I Agree, View
# Notice"). Sitekey is dynamic (auto-detected by captcha_solver).
SEL_VIEW_NOTICE_BUTTON = "input[id$='btnViewNotice'], input[id$='_btnViewNotice']"
# The notice id is the `ID=` query param on the detail URL.
DETAIL_ID_PARAM = "ID"
# Marker text shown once the notice body is revealed (post-captcha). Verified
# absent pre-solve; the puller also treats a populated body as success.
DETAIL_CONTENT_MARKER = "Notice Content"

# Date-range controls — VERIFIED (auth recon): rbLastNumDays radio is pre-checked
# with a default of 60 days; the text input is rendered offscreen (must set via
# JS). We also enforce a per-row publication-date cutoff, so the exact server
# window is a coarse pre-filter only.
SEL_DATE_LASTDAYS_TXT_ID = "ctl00_ContentPlaceHolder1_as1_txtLastNumDays"
SEL_DATE_LASTDAYS_RADIO_ID = "ctl00_ContentPlaceHolder1_as1_rbLastNumDays"
SEL_DATE_LASTMONTHS_TXT_ID = "ctl00_ContentPlaceHolder1_as1_txtLastNumMonths"
SEL_DATE_LASTMONTHS_RADIO_ID = "ctl00_ContentPlaceHolder1_as1_rbLastNumMonths"
SEL_KEYWORD_ID = "ctl00_ContentPlaceHolder1_as1_txtSearch"
SEL_MATCH_OR_ID = "ctl00_ContentPlaceHolder1_as1_rdoType_1"      # OR match
SEL_COUNTY_LABEL_PREFIX = "ctl00_ContentPlaceHolder1_as1_lstCounty_"

# ── Pagination ─────────────────────────────────────────────────────────
RESULTS_PER_PAGE = 50  # max the platform offers

# ── Search strategy ────────────────────────────────────────────────────
# IMPORTANT (auth recon 2026-06-03): the "Popular Searches" presets only auto-
# fill the keyword box with a BROAD OR-term list, and the terms cross-
# contaminate — e.g. the Estate Claims preset (which includes "estate") matches
# foreclosure "real estate" notices, so a preset does NOT reliably imply a
# notice_type. We therefore (a) drive the form with ONE focused union keyword
# per county and (b) classify each notice's type from its actual CONTENT.
#
# Union keyword: OR match, phrases joined with '+', terms separated by double
# space (mirrors the site's own preset format). Tuned to surface foreclosure +
# probate (estate) + tax-sale notices with minimal noise. Content classification
# (classify_notice_type in the puller) is the source of truth for notice_type.
SEARCH_KEYWORD: str = (
    "trustee  trustee's+sale  substitute+trustee  deed+of+trust  foreclosure  "
    "estate  creditors  decedent  qualified  personal+representative  "
    "delinquent+taxes  tax+deed  judicial+sale  nonjudicial+sale"
)

# The three notice types this source targets. Anything classified outside this
# set is dropped (not emitted, but its id is cached so it isn't re-fetched).
TARGET_NOTICE_TYPES: tuple[str, ...] = ("foreclosure", "probate", "tax_sale")


@dataclass(frozen=True)
class TargetLocality:
    """A buy-box locality → its exact checkbox label + canonical county.

    Most localities are in the County CheckBoxList (``list_kind="county"``).
    A few VA independent cities (e.g. Alexandria) are NOT in the County list and
    are only filterable via the City CheckBoxList (``list_kind="city"``).
    """
    checkbox_label: str   # EXACT <label> text in the lstCounty / lstCity CheckBoxList
    county_display: str   # what SiftStack stamps on NoticeData.county
    list_kind: str = "county"   # "county" → lstCounty, "city" → lstCity


# Operator buy-box: 4 primary VA markets + nearby (2026-06-02). All 8 live in
# the County checkbox list (verified). "Richmond" alone is the rural Richmond
# County (Northern Neck) — the operator wants the independent city, label
# "Richmond City".
TARGET_LOCALITIES: list[TargetLocality] = [
    TargetLocality("Richmond City", "Richmond City"),
    TargetLocality("Henrico", "Henrico County"),
    TargetLocality("Chesterfield", "Chesterfield County"),
    TargetLocality("Prince William", "Prince William County"),
    TargetLocality("Hanover", "Hanover County"),
    TargetLocality("Goochland", "Goochland County"),
    TargetLocality("Powhatan", "Powhatan County"),
    TargetLocality("Fairfax", "Fairfax County"),
    # Hampton Roads independent cities + Alexandria (added per operator 2026-06-04).
    # Labels VERIFIED against the live lstCounty CheckBoxList — note the "City"
    # suffix on Norfolk / Newport News / Suffolk: the bare "Norfolk" etc. are
    # finer-grained lstCity entries, NOT the publication jurisdiction.
    TargetLocality("Norfolk City", "Norfolk"),
    TargetLocality("Virginia Beach", "Virginia Beach"),
    TargetLocality("Portsmouth", "Portsmouth"),
    TargetLocality("Chesapeake", "Chesapeake"),
    TargetLocality("Hampton", "Hampton"),
    TargetLocality("Newport News City", "Newport News"),
    TargetLocality("Suffolk City", "Suffolk"),
    # Alexandria is NOT in the County CheckBoxList — only the City list has it,
    # so it's matched via list_kind="city" (verified present in lstCity).
    # (Prince William is already covered above.)
    TargetLocality("Alexandria", "Alexandria", list_kind="city"),
]

# ── Source URL scheme ──────────────────────────────────────────────────
# Every VA-public-notice record carries this scheme so downstream consumers can
# disambiguate it from PropertyRadar / Chesterfield ACA / Richmond Vacant feeds.
SOURCE_URL_SCHEME = "va_public_notice"
