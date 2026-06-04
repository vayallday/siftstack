"""Virginia Public Notice (VPA) puller — publicnoticevirginia.com.

Drives the authenticated Advanced Search form (one union keyword + a single
County checkbox + date window), paginates the results grid, opens each notice
detail (free Smart Search login + reCAPTCHA solve), classifies the notice type
from its CONTENT (not the search terms — the site's category presets cross-
contaminate), LLM-parses the full text into NoticeData, and diffs against a
cross-run seen-notice cache so a detail is never re-opened (or re-captcha'd).

One search per county (TARGET_LOCALITIES) makes each record's county
unambiguous. The notice_type comes from classify_notice_type() applied to the
preview (to gate captcha spend) and the full body (final).

Public entry point:
    pull_new_records(mode="daily", since=None, headless=True, ...) -> list[NoticeData]

This is the SiftStack puller contract (cf. chesterfield_aca_puller,
richmond_vacant_puller). main.py's `va-public-notice` mode and the Apify Actor
flow both call it.

Platform notes live in va_public_notice_config.py. The flow mirrors the archived
TN twin (src/_legacy_tn/scraper.py) and was verified end-to-end live on
2026-06-03 (login → search → classify → detail → captcha solve → LLM extract).
Note: 2Captcha reCAPTCHA-v2 solves take ~30-90s each, so throughput is roughly
one notice/minute — fine for a daily cron, slow for large historical backfills.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import logging
import random
import re
from datetime import date, datetime, timedelta
from pathlib import Path

from playwright.async_api import Page, TimeoutError as PwTimeout, async_playwright

import config
import llm_client
import va_public_notice_config as vacfg
from captcha_solver import solve_captcha_and_view
from notice_parser import NoticeData

logger = logging.getLogger(__name__)

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

# Cap on records emitted per single run — a runaway (e.g. since_date logic bug
# pulling a year of statewide notices) aborts rather than flooding DataSift.
MAX_RECORDS_PER_PULL = 3000

# Hard ceiling on processing ONE notice detail (open → captcha → extract → back).
# A legit slow 2Captcha solve (incl. retries) fits comfortably; a stuck Playwright
# navigation (observed: go_back hanging with no timeout) is caught and recovered
# so one bad page can't hang the whole unattended cron and lose the run's records.
DETAIL_TIMEOUT_SEC = 300


class _PageStuck(Exception):
    """Raised when a notice detail times out — the page is likely wedged, so we
    abandon the current county and let the next county re-navigate fresh."""


async def _delay() -> None:
    await asyncio.sleep(random.uniform(config.REQUEST_DELAY_MIN, config.REQUEST_DELAY_MAX))


# ── State ──────────────────────────────────────────────────────────────


def load_state() -> dict:
    """Load puller state, returning a fresh shell if missing/empty.

    Shape: {schema_version, last_run_date, seen_ids: {notice_id: "YYYY-MM-DD"}}.
    seen_ids is the cross-run dedup cache; the date is first-seen, used only for
    pruning to bound file size.
    """
    state = config.load_state(vacfg.STATE_FILE)
    if not state:
        return {
            "schema_version": vacfg.STATE_SCHEMA_VERSION,
            "last_run_date": "",
            "seen_ids": {},
        }
    state.setdefault("seen_ids", {})
    state.setdefault("last_run_date", "")
    return state


def save_state(state: dict) -> None:
    config.save_state(vacfg.STATE_FILE, state)


_CHECKPOINT_FILE = config.OUTPUT_DIR / "va_public_notice_checkpoint.jsonl"


def _checkpoint_records(notices: list[NoticeData]) -> None:
    """Persist collected records to a JSONL so a hard crash (kill/OOM) mid-run
    doesn't lose them — the CSV is only written after all counties finish."""
    try:
        import json
        with open(_CHECKPOINT_FILE, "w", encoding="utf-8") as f:
            for n in notices:
                f.write(json.dumps(n.__dict__, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("checkpoint write failed", exc_info=True)


def _prune_seen(seen: dict[str, str]) -> dict[str, str]:
    cutoff = (datetime.now() - timedelta(days=vacfg.SEEN_IDS_PRUNE_DAYS)).strftime("%Y-%m-%d")
    pruned = {nid: d for nid, d in seen.items() if d >= cutoff}
    if len(pruned) < len(seen):
        logger.info("Pruned %d seen IDs older than %d days",
                    len(seen) - len(pruned), vacfg.SEEN_IDS_PRUNE_DAYS)
    return pruned


_ID_PARAM_RE = re.compile(r"[?&](?:n?id|noticeid|nid)=([^&#]+)", re.I)
_TRAILING_NUM_RE = re.compile(r"/(\d{4,})(?:[/?#]|$)")


def _notice_id_from_url(url: str) -> str:
    """Derive a stable notice id from a detail URL, or '' if none found."""
    if not url:
        return ""
    m = _ID_PARAM_RE.search(url)
    if m:
        return m.group(1)
    m = _TRAILING_NUM_RE.search(url)
    if m:
        return m.group(1)
    return ""


def _notice_id_fallback(*parts: str) -> str:
    """Stable hash id from row fields when no URL id is available."""
    raw = "|".join(p.strip() for p in parts if p).lower()
    return "h" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


# ── Cookies / session ──────────────────────────────────────────────────


async def _load_cookies(context) -> bool:
    cookies = config.load_state(vacfg.COOKIES_FILE)
    if not cookies:
        return False
    try:
        await context.add_cookies(cookies)
        return True
    except Exception:
        return False


async def _save_cookies(context) -> None:
    try:
        config.save_state(vacfg.COOKIES_FILE, await context.cookies())
    except Exception:
        logger.debug("Could not save cookies", exc_info=True)


# ── Login ──────────────────────────────────────────────────────────────


async def _login(page: Page, retries: int = 3) -> bool:
    """Log in to the VPA Smart Search. Returns True on success."""
    if not vacfg.VAPN_EMAIL or not vacfg.VAPN_PASSWORD:
        logger.error(
            "VAPN_EMAIL / VAPN_PASSWORD not set — sign up free at %s and add "
            "them to .env", vacfg.SIGNUP_URL,
        )
        return False

    for attempt in range(1, retries + 1):
        try:
            logger.info("Logging in to %s (attempt %d/%d)", vacfg.LOGIN_URL, attempt, retries)
            await page.goto(vacfg.LOGIN_URL, wait_until="domcontentloaded")
            await page.wait_for_load_state("domcontentloaded")
            break
        except Exception as exc:
            logger.warning("Login navigation failed (%d/%d): %s", attempt, retries, exc)
            if attempt < retries:
                await asyncio.sleep(5 * attempt)
                continue
            return False

    try:
        await page.fill(vacfg.SEL_LOGIN_EMAIL, vacfg.VAPN_EMAIL)
        await page.fill(vacfg.SEL_LOGIN_PASSWORD, vacfg.VAPN_PASSWORD)
        await page.click(vacfg.SEL_LOGIN_SUBMIT)
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        logger.exception("Login form interaction failed — selectors may have drifted")
        await _shot(page, "login_form_failed")
        return False
    await _delay()

    if "authenticate" not in page.url.lower():
        logger.info("Login successful — landed on %s", page.url)
        return True

    err = await page.query_selector(".error, .validation-summary-errors")
    if err:
        logger.error("Login failed: %s", (await err.inner_text()).strip())
    else:
        logger.error("Login failed — still on %s", page.url)
    await _shot(page, "login_failed")
    return False


async def _shot(page: Page, name: str) -> None:
    """Best-effort failure screenshot (always inspect the real page state)."""
    try:
        out = config.OUTPUT_DIR / f"va_pn_{name}.png"
        await page.screenshot(path=str(out), full_page=True)
        logger.warning("Saved diagnostic screenshot: %s", out)
    except Exception:
        pass


# ── Search form ────────────────────────────────────────────────────────


async def _check_locality(page: Page, checkbox_label: str,
                          prefix: str = vacfg.SEL_COUNTY_LABEL_PREFIX) -> bool:
    """Tick the CheckBoxList item whose label EXACTLY matches.

    ``prefix`` selects which list to search — the County list (default) or the
    City list (for VA independent cities like Alexandria that aren't in the
    County list). The checkboxes render offscreen (offsetParent null) so
    Playwright's visibility-gated ``check()`` fails — set ``checked`` + dispatch
    events via JS. Returns True if a matching box was found and checked.
    """
    checked_id = await page.evaluate(
        """(args) => {
            const [prefix, label] = args;
            const want = label.trim().toLowerCase();
            for (const b of document.querySelectorAll(`input[id^='${prefix}']`)) {
                const lab = document.querySelector(`label[for='${b.id}']`);
                const t = lab ? lab.textContent.trim().toLowerCase() : '';
                if (t === want) {
                    b.checked = true;
                    b.dispatchEvent(new Event('click', {bubbles: true}));
                    b.dispatchEvent(new Event('change', {bubbles: true}));
                    return b.id;
                }
            }
            return null;
        }""",
        [prefix, checkbox_label],
    )
    if checked_id:
        logger.debug("Checked locality %r (%s)", checkbox_label, checked_id)
        return True
    logger.warning("Locality checkbox label %r not found (prefix=%s)", checkbox_label, prefix)
    return False


async def _set_date_window(page: Page, since_date: str | None, mode: str) -> None:
    """Set the date filter via JS (the inputs render offscreen).

    Best-effort: the radio defaults to 'last 60 days'. We widen for historical
    (12 months) and otherwise set 'last N days' from since_date. A per-row
    publication-date cutoff in the scraper is the real boundary, so failure here
    only changes how much the server pre-filters.
    """
    if mode == "historical":
        await page.evaluate(
            """(ids) => {
                const [rId, tId] = ids;
                const r = document.getElementById(rId);
                if (r) { r.checked = true; r.dispatchEvent(new Event('click', {bubbles:true}));
                         r.dispatchEvent(new Event('change', {bubbles:true})); }
                const t = document.getElementById(tId);
                if (t) { t.value = '12'; t.dispatchEvent(new Event('input', {bubbles:true}));
                         t.dispatchEvent(new Event('change', {bubbles:true})); }
            }""",
            [vacfg.SEL_DATE_LASTMONTHS_RADIO_ID, vacfg.SEL_DATE_LASTMONTHS_TXT_ID],
        )
        return
    days = 60
    if since_date:
        try:
            d = datetime.strptime(since_date, "%Y-%m-%d").date()
            days = max(1, min((date.today() - d).days + 1, 366))
        except ValueError:
            pass
    await page.evaluate(
        """(args) => {
            const [rId, tId, days] = args;
            const r = document.getElementById(rId);
            if (r) { r.checked = true; r.dispatchEvent(new Event('click', {bubbles:true}));
                     r.dispatchEvent(new Event('change', {bubbles:true})); }
            const t = document.getElementById(tId);
            if (t) { t.value = String(days); t.dispatchEvent(new Event('input', {bubbles:true}));
                     t.dispatchEvent(new Event('change', {bubbles:true})); }
        }""",
        [vacfg.SEL_DATE_LASTDAYS_RADIO_ID, vacfg.SEL_DATE_LASTDAYS_TXT_ID, days],
    )


async def _get_page_info(page: Page) -> tuple[int, int]:
    """Parse 'Page X of Y Pages'. Returns (current, total)."""
    try:
        el = await page.query_selector(vacfg.SEL_PAGE_INFO)
        if el:
            m = re.search(r"Page\s+(\d+)\s+of\s+(\d+)", await el.inner_text())
            if m:
                return int(m.group(1)), int(m.group(2))
    except Exception:
        pass
    return 1, 1


async def _set_per_page(page: Page) -> None:
    try:
        dd = await page.query_selector(vacfg.SEL_PER_PAGE_DROPDOWN)
        if dd and (await dd.input_value()) != str(vacfg.RESULTS_PER_PAGE):
            await page.select_option(vacfg.SEL_PER_PAGE_DROPDOWN, str(vacfg.RESULTS_PER_PAGE))
            await page.wait_for_load_state("domcontentloaded")
            await _delay()
    except Exception:
        logger.debug("Per-page dropdown not set", exc_info=True)


# ── Content classification (source of truth for notice_type) ────────────
# The search keyword is broad/OR, so it surfaces a mix. We classify each notice
# from its actual text — both the cheap grid preview (to gate captcha spend) and
# the full body (final). Patterns are VA-notice idioms verified against live
# previews (e.g. "TRUSTEE'S SALE ...", "In execution of the ...").

_TAX_RE = re.compile(
    r"delinquent\s+tax|tax\s+deed|unpaid\s+tax|§?\s*58\.1-39|nonjudicial\s+sale.*tax",
    re.I | re.S,
)
_FORECLOSURE_RE = re.compile(
    r"trustee'?s?\s+sale|deed\s+of\s+trust|substitute\s+trustee|foreclos|"
    r"in\s+execution\s+of\s+the",
    re.I,
)
_PROBATE_RE = re.compile(
    r"estate\s+of|notice\s+to\s+creditors|qualified\s+as|personal\s+representative|"
    r"executor|executrix|administrat(?:or|rix)|decedent|deceased",
    re.I,
)


_PREVIEW_ADDR_RE = re.compile(
    r"\b(\d{1,6}\s+[A-Za-z0-9][A-Za-z0-9 .'\-]{2,38}?"
    r"(?:ST|STREET|AVE|AVENUE|RD|ROAD|DR|DRIVE|LN|LANE|BLVD|BOULEVARD|CT|COURT|"
    r"PL|PLACE|WAY|CIR|CIRCLE|TER|TERRACE|PKWY|HWY|TPKE|TRL|TRAIL|LOOP|RUN|PIKE|"
    r"SQ|ROW|PARK|WALK|CRES|CRESCENT|GREEN|GROVE))\b",
    re.I,
)


def _preview_addr_key(preview: str) -> str:
    """Normalized first street-address in a grid preview, or '' if none.

    Used for within-run dedup: the same notice is republished across multiple
    papers/dates (distinct notice ids), so we collapse by property address
    BEFORE opening a detail to avoid re-fetching/re-parsing the same property.
    """
    # Drop the leading "Publication … Month DD, YYYY" so the year isn't matched
    # as a house number — leaves the notice body (e.g. "TRUSTEE'S SALE OF 301…").
    body = re.sub(r"^.*?\b(?:19|20)\d{2}\b\s*", "", preview or "", count=1)
    m = _PREVIEW_ADDR_RE.search(body)
    if not m:
        return ""
    return re.sub(r"[^a-z0-9]", "", m.group(1).lower())


def classify_notice_type(text: str) -> str | None:
    """Classify a VA notice into foreclosure / probate / tax_sale, or None.

    Tax is checked first (tax judicial sales also say "sale"/"judicial sale"),
    then foreclosure, then probate. Returns None for non-target notices so the
    caller can skip them (and avoid a wasted captcha solve).
    """
    if not text:
        return None
    if _TAX_RE.search(text):
        return "tax_sale"
    if _FORECLOSURE_RE.search(text):
        return "foreclosure"
    if _PROBATE_RE.search(text):
        return "probate"
    return None


# ── LLM extraction (VA-specific; no Tennessee references) ───────────────

_SYSTEM = (
    "You extract structured data from Virginia legal notices. "
    "Return ONLY valid JSON — no markdown, no code fences, no explanation."
)

_FORECLOSURE_PROMPT = """\
Extract these fields from this Virginia trustee's-sale / foreclosure legal notice published in {locality}, Virginia.

Return ONLY a JSON object with these exact keys:
- "address": the property street address being foreclosed/sold (e.g. "123 Main St"). If MULTIPLE properties are listed, use the FIRST street address. NOT the trustee office, courthouse, or auction-location address.
- "city": the city/town where the property is located
- "state": always "VA"
- "zip": the property's 5-digit ZIP code
- "owner_name": the borrower/grantor who EXECUTED the deed of trust (the property owner being foreclosed). Use ALL CAPS as written. VA trustee notices OFTEN do NOT name the borrower — if no borrower/grantor person or entity is named, use "". NEVER use the Trustee, Substitute Trustee, law firm, or lender as the owner.
- "auction_date": the scheduled sale/auction date in YYYY-MM-DD format (NOT the publication date).
- "parcel_id": the Tax Map No. / tax map number if stated (e.g. "Tax Map No. E0000776012" → "E0000776012"; if several, the first), else "".

If a field cannot be determined, use an empty string "".

Notice text:
{raw_text}"""

_PROBATE_PROMPT = """\
Extract these fields from this Virginia estate / "Notice to Creditors" notice published in {locality}, Virginia.

Return ONLY a JSON object with these exact keys:
- "decedent_name": the deceased person's full name (from "Estate of [NAME]"). ALL CAPS as written.
- "owner_name": the Executor / Administrator / Personal Representative appointed to the estate. ALL CAPS. Drop titles (e.g. "Executor", "Administratrix").
- "owner_street": the PR/executor's mailing street address (where claims are sent)
- "owner_city": city of the PR's mailing address
- "owner_state": state of the PR's mailing address (usually "VA")
- "owner_zip": 5-digit ZIP of the PR's mailing address
- "address": "" (estate notices rarely contain the decedent's property address)
- "city": ""
- "state": "VA"
- "zip": ""

If a field cannot be determined, use an empty string "".

Notice text:
{raw_text}"""

_TAXSALE_PROMPT = """\
Extract these fields from this Virginia delinquent-tax / tax-deed sale notice published in {locality}, Virginia.

Return ONLY a JSON object with these exact keys:
- "address": the property street address being sold for delinquent taxes (e.g. "123 Main St")
- "city": the city/town where the property is located
- "state": always "VA"
- "zip": the property's 5-digit ZIP code
- "owner_name": the delinquent owner / defendant named in the notice. ALL CAPS as written.
- "auction_date": the scheduled sale/auction date in YYYY-MM-DD format (NOT the publication date).
- "parcel_id": the tax map / parcel ID if stated, else "".

If a field cannot be determined, use an empty string "".

Notice text:
{raw_text}"""

_PROMPTS = {
    "probate": _PROBATE_PROMPT,
    "foreclosure": _FORECLOSURE_PROMPT,
    "tax_sale": _TAXSALE_PROMPT,
}


async def _llm_extract(raw_text: str, notice_type: str, locality: str) -> dict:
    """LLM-extract structured fields from a VA notice body. {} on failure."""
    text = (raw_text or "").strip()
    if not text:
        return {}
    api_key = config.ANTHROPIC_API_KEY
    if getattr(config, "LLM_BACKEND", "anthropic") == "anthropic" and not api_key:
        logger.warning("ANTHROPIC_API_KEY not set — cannot LLM-parse VA notice")
        return {}
    template = _PROMPTS.get(notice_type, _FORECLOSURE_PROMPT)
    prompt = template.format(locality=locality, raw_text=text[:8000])
    try:
        parsed = await llm_client.chat_json_async(
            prompt, system=_SYSTEM, max_tokens=320, api_key=api_key,
        )
        return parsed or {}
    except Exception:
        logger.exception("LLM extraction failed for %s notice", notice_type)
        return {}


# ── Detail text ────────────────────────────────────────────────────────


async def _detail_text(page: Page) -> str:
    """Return the full notice body text from a (revealed) detail page.

    The platform truncates the web display to ~1,000 chars ("Web display limited
    to …") and serves the FULL notice as an embedded PDF — the property address
    is in the visible headline, but the borrower/grantor name is only in the PDF.
    So we prefer the embedded PDF's text (downloaded via the session cookies and
    parsed with pdfminer, reusing notice_parser._try_extract_pdf_text), and fall
    back to the cleaned web "Notice Content" section.
    """
    from notice_parser import _extract_notice_content, _try_extract_pdf_text

    pdf_text = await _try_extract_pdf_text(page)
    if pdf_text and len(pdf_text.strip()) > 120:
        return pdf_text.strip()

    # Web fallback — isolate the "Notice Content" section so the LLM doesn't see
    # the nav / search form / Google-Translate language list.
    full = (await page.inner_text("body")).replace("\xa0", " ")
    content = _extract_notice_content(full)
    return (content or full).strip()


# ── Build NoticeData ───────────────────────────────────────────────────


def _to_notice(
    fields: dict, *, notice_type: str, county: str, pub_date: str,
    notice_id: str, raw_text: str,
) -> NoticeData:
    g = lambda k: str(fields.get(k, "") or "").strip()  # noqa: E731
    nd = NoticeData(
        date_added=pub_date or datetime.now().strftime("%Y-%m-%d"),
        address=g("address"),
        city=g("city"),
        state=g("state") or "VA",
        zip=g("zip"),
        owner_name=g("owner_name"),
        notice_type=notice_type,
        county=county,
        source_url=f"{vacfg.SOURCE_URL_SCHEME}://{notice_id}",
        raw_text=raw_text[:6000],
    )
    # Foreclosure / tax_sale auction date
    if g("auction_date"):
        nd.auction_date = g("auction_date")
    # Tax parcel
    if g("parcel_id"):
        nd.parcel_id = g("parcel_id")
    # Probate (estate) extras — set the deceased-owner signal so a failed
    # obituary lookup still tags the record, and feed the PR/decedent so the
    # obituary enricher's "DM = named PR" preset fires (see CLAUDE.md).
    if notice_type == "probate":
        nd.owner_deceased = "yes"
        if g("decedent_name"):
            nd.decedent_name = g("decedent_name")
        for src, dst in (("owner_street", "owner_street"), ("owner_city", "owner_city"),
                         ("owner_state", "owner_state"), ("owner_zip", "owner_zip")):
            if g(src):
                setattr(nd, dst, g(src))
    return nd


# ── One per-county union-keyword search ────────────────────────────────


async def _run_search(
    page: Page,
    locality: vacfg.TargetLocality,
    since_date: str | None,
    mode: str,
    seen_ids: dict[str, str],
    seen_addr_keys: set[str],
    today_iso: str,
) -> list[NoticeData]:
    """Run one union-keyword search for a single county and scrape its pages."""
    label = locality.checkbox_label
    logger.info("Search county=%s", locality.county_display)
    try:
        await page.goto(vacfg.SEARCH_URL, wait_until="domcontentloaded")
        await page.wait_for_load_state("domcontentloaded")
    except Exception:
        logger.warning("Could not load search form for %s", label)
        return []
    await _delay()

    # Type the union keyword + select OR match (fields render offscreen → JS).
    await page.evaluate(
        """(args) => {
            const [kwId, orId, keyword] = args;
            const kw = document.getElementById(kwId);
            if (kw) { kw.value = keyword; kw.dispatchEvent(new Event('input', {bubbles:true}));
                      kw.dispatchEvent(new Event('change', {bubbles:true})); }
            const orr = document.getElementById(orId);
            if (orr) { orr.checked = true; orr.dispatchEvent(new Event('click', {bubbles:true}));
                       orr.dispatchEvent(new Event('change', {bubbles:true})); }
        }""",
        [vacfg.SEL_KEYWORD_ID, vacfg.SEL_MATCH_OR_ID, vacfg.SEARCH_KEYWORD],
    )
    prefix = (vacfg.SEL_CITY_CHECKBOX_PREFIX if locality.list_kind == "city"
              else vacfg.SEL_COUNTY_LABEL_PREFIX)
    if not await _check_locality(page, label, prefix):
        return []
    await _set_date_window(page, since_date, mode)
    await _delay()

    try:
        async with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
            await page.click(vacfg.SEL_GO)
    except Exception:
        logger.debug("Go did not trigger navigation; continuing", exc_info=True)
        await page.wait_for_timeout(3000)

    # Detect the login wall (full results require Smart Search auth).
    if await page.query_selector("text='Please login to continue'"):
        logger.error("Hit login wall on results — session not authenticated")
        await _shot(page, "results_login_wall")
        return []

    await _set_per_page(page)

    notices: list[NoticeData] = []
    cur, total = await _get_page_info(page)
    logger.info("  %d result page(s) for %s", total, locality.county_display)
    page_guard = 0
    while True:
        try:
            notices.extend(await _scrape_results_page(
                page, locality, since_date, seen_ids, seen_addr_keys, today_iso,
            ))
        except _PageStuck:
            logger.warning("  %s abandoned after a stuck page — %d records kept",
                           locality.county_display, len(notices))
            break
        if cur >= total:
            break
        page_guard += 1
        if page_guard > 100:
            logger.warning("  Page guard hit for %s — stopping", locality.county_display)
            break
        nxt = await page.query_selector(vacfg.SEL_NEXT_PAGE_BUTTON)
        if not nxt or (await nxt.get_attribute("disabled")):
            break
        try:
            await nxt.click()
            await page.wait_for_load_state("domcontentloaded")
            await _delay()
            cur, total = await _get_page_info(page)
        except Exception:
            logger.warning("Pagination stalled at page %d/%d", cur, total)
            break
    return notices


async def _scrape_results_page(
    page: Page,
    locality: vacfg.TargetLocality,
    since_date: str | None,
    seen_ids: dict[str, str],
    seen_addr_keys: set[str],
    today_iso: str,
) -> list[NoticeData]:
    """Scrape one results page: per notice, classify → dedup → detail → parse."""
    try:
        await page.wait_for_selector(vacfg.SEL_VIEW_BUTTON_PATTERN, state="attached", timeout=20000)
    except PwTimeout:
        logger.info("  No result rows for %s", locality.county_display)
        return []

    # Distinct notices on this page, keyed by the per-page view-button id. A
    # notice spans a publication/date row + a content row, so combine their text.
    items = await page.evaluate(
        """(gridSel) => {
            const byBtn = {};
            for (const tr of document.querySelectorAll(gridSel + ' tr')) {
                const b = tr.querySelector("input[id*='btnView']");
                if (!b) continue;
                const t = (tr.innerText || '').replace(/\\s+/g, ' ').trim();
                byBtn[b.id] = (byBtn[b.id] || '') + (t ? ' ' + t : '');
            }
            return Object.keys(byBtn).map(id => ({btnId: id, txt: byBtn[id].trim().slice(0, 600)}));
        }""",
        vacfg.SEL_RESULTS_GRID,
    )
    logger.info("  %d distinct notices on page", len(items))

    notices: list[NoticeData] = []
    for item in items:
        preview = item["txt"]
        pub_date = _extract_pub_date(preview)
        if since_date and pub_date and pub_date < since_date:
            continue
        # Stable cross-run dedup id from the preview (the real ID= param is only
        # known after clicking; preview is deterministic for the same notice).
        pre_id = _notice_id_fallback(locality.county_display, preview[:200])
        if pre_id in seen_ids:
            continue
        # Pre-classify from the (rich) preview to avoid wasting a captcha solve
        # on non-target notices (meeting schedules, ABC licenses, etc.).
        ptype = classify_notice_type(preview)
        if ptype is None:
            seen_ids[pre_id] = pub_date or today_iso   # remember; don't re-check
            continue
        # Within-run dedup by property address: the same notice is republished in
        # several papers/dates (distinct ids), so collapse by address BEFORE
        # opening a detail. First occurrence wins; the rest are skipped cheaply.
        akey = _preview_addr_key(preview)
        if akey and akey in seen_addr_keys:
            seen_ids[pre_id] = pub_date or today_iso
            continue
        if akey:
            seen_addr_keys.add(akey)

        # Bound the detail work so a stuck navigation can't hang the whole run.
        try:
            nd = await asyncio.wait_for(
                _process_detail(page, item, locality, pre_id, pub_date, ptype,
                                seen_ids, today_iso),
                timeout=DETAIL_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "  Detail >%ds (stuck) for %s — abandoning %s to recover",
                DETAIL_TIMEOUT_SEC, item["btnId"], locality.county_display,
            )
            raise _PageStuck()
        except Exception:
            logger.exception("  Error processing notice %s", item["btnId"])
            nd = None

        if nd is not None:
            notices.append(nd)
            logger.info("  + [%s] %s | owner=%s | %s",
                        nd.notice_type, nd.address or "(no addr)",
                        nd.owner_name or nd.decedent_name or "?",
                        nd.source_url.rsplit("/", 1)[-1])
        await _delay()
    return notices


async def _process_detail(
    page: Page,
    item: dict,
    locality: vacfg.TargetLocality,
    pre_id: str,
    pub_date: str,
    ptype: str,
    seen_ids: dict[str, str],
    today_iso: str,
) -> NoticeData | None:
    """Open one notice detail → captcha → full text → classify → LLM → NoticeData.

    Always returns to the results page in a finally. Returns None for a captcha
    miss (left unmarked so it retries next run), a non-target final type, or an
    empty extraction.
    """
    try:
        if not await _open_detail(page, item["btnId"]):
            return None
        notice_id = _notice_id_from_url(page.url) or pre_id

        if not await solve_captcha_and_view(
            page, vacfg.CAPTCHA_API_KEY,
            view_button_selector=vacfg.SEL_VIEW_NOTICE_BUTTON,
            content_marker=vacfg.DETAIL_CONTENT_MARKER,
        ):
            logger.warning("  Captcha/gate not cleared for notice %s", notice_id)
            return None  # don't mark seen → retried next run

        raw_text = await _detail_text(page)
        # Final classification from full text (more reliable than preview).
        ftype = classify_notice_type(raw_text) or ptype
        seen_ids[pre_id] = pub_date or today_iso
        if ftype not in vacfg.TARGET_NOTICE_TYPES:
            return None
        fields = await _llm_extract(raw_text, ftype, locality.county_display)
        if not fields:
            logger.info("  No fields extracted for notice %s — skipped", notice_id)
            return None
        return _to_notice(
            fields, notice_type=ftype, county=locality.county_display,
            pub_date=pub_date, notice_id=notice_id, raw_text=raw_text,
        )
    finally:
        await _back_to_results(page)


async def _open_detail(page: Page, btn_id: str) -> bool:
    """Click a result's view button (ASP.NET postback) → detail. True on success."""
    try:
        await page.click("#" + btn_id, timeout=25000)
        await page.wait_for_load_state("domcontentloaded", timeout=25000)
    except Exception:
        logger.debug("  open_detail click failed for %s", btn_id, exc_info=True)
        return False
    if "details" in page.url.lower():
        return True
    # Some postbacks render the detail in-place; treat the view-notice button or
    # a reCAPTCHA frame as confirmation we reached a notice detail.
    if await page.query_selector(vacfg.SEL_VIEW_NOTICE_BUTTON) or \
            await page.query_selector("iframe[src*='recaptcha']"):
        return True
    logger.debug("  btnView click did not reach a detail (url=%s)", page.url)
    return False


async def _back_to_results(page: Page) -> None:
    # Explicit per-op timeouts: the observed run-killer was go_back hanging with
    # no timeout. Bound each step so recovery is fast and the loop continues.
    try:
        if "search" not in page.url.lower():
            await page.go_back(timeout=25000, wait_until="domcontentloaded")
            if "details" in page.url.lower():  # captcha interstitial
                await page.go_back(timeout=25000, wait_until="domcontentloaded")
    except Exception:
        logger.debug("  back-to-results failed", exc_info=True)


_PUB_DATE_RE = re.compile(
    r"(?:Published:?\s*)?"
    r"(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,?\s*)?"
    r"([A-Z][a-z]+ \d{1,2},? \d{4}|\d{1,2}/\d{1,2}/\d{4})"
)


def _extract_pub_date(row_text: str) -> str:
    """Pull a publication date (YYYY-MM-DD) from a result row's text."""
    m = _PUB_DATE_RE.search(row_text or "")
    if not m:
        return ""
    raw = m.group(1)
    for fmt in ("%B %d, %Y", "%B %d %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return ""


# ── Top-level async pull ───────────────────────────────────────────────


async def pull_new_records_async(
    *,
    mode: str = "daily",
    since: str | None = None,
    headless: bool = True,
    proxy_url: str | None = None,
    localities: list[vacfg.TargetLocality] | None = None,
    seen_ids: dict[str, str] | None = None,
    on_search_complete=None,
) -> list[NoticeData]:
    """Log in, run one union-keyword search per county, return new NoticeData.

    seen_ids: cross-run dedup cache. If None, loaded from STATE_FILE. Callers
    (e.g. Apify) may pass a KVS-backed dict to share the cache.
    """
    localities = localities or vacfg.TARGET_LOCALITIES

    state = load_state()
    if seen_ids is None:
        seen_ids = _prune_seen(state.get("seen_ids", {}))

    since_date = _resolve_since_date(mode, since, state)
    logger.info("VA public notice pull — mode=%s since=%s (%d seen ids loaded)",
                mode, since_date, len(seen_ids))

    today_iso = datetime.now().strftime("%Y-%m-%d")
    seen_addr_keys: set[str] = set()   # within-run property-address dedup
    all_notices: list[NoticeData] = []

    async with async_playwright() as pw:
        launch_opts: dict = {"headless": headless}
        if proxy_url:
            launch_opts["proxy"] = _parse_proxy(proxy_url)
        browser = await pw.chromium.launch(**launch_opts)
        context = await browser.new_context(user_agent=_USER_AGENT, accept_downloads=True)
        context.set_default_timeout(60_000)
        # Bound navigations explicitly so a wedged page errors fast instead of
        # hanging the unattended run (the per-detail timeout is a further backstop).
        context.set_default_navigation_timeout(30_000)
        await _load_cookies(context)
        page = await context.new_page()

        if not await _login(page):
            logger.error("Login failed — aborting VA public notice pull")
            await browser.close()
            return []
        await _save_cookies(context)

        for locality in localities:
            try:
                all_notices.extend(await _run_search(
                    page, locality, since_date, mode, seen_ids, seen_addr_keys, today_iso,
                ))
            except Exception:
                logger.exception("Search failed for county %s", locality.county_display)
            # Checkpoint RECORDS per-county (crash safety). We deliberately do NOT
            # persist seen_ids mid-run: seen_ids is only written at the very end of
            # a completed run, so a crash/hang re-processes next time rather than
            # marking notices "seen" without ever emitting them (the lost-records
            # bug that bit run #3). Within-run dedup uses the in-memory seen_ids.
            _checkpoint_records(all_notices)
            if len(all_notices) >= MAX_RECORDS_PER_PULL:
                logger.warning("Hit MAX_RECORDS_PER_PULL (%d) — stopping early",
                               MAX_RECORDS_PER_PULL)
                break

        await browser.close()

    # Finalize state — persist seen_ids ONLY now, after the full run completed.
    state["seen_ids"] = _prune_seen(seen_ids)
    if mode == "daily":
        state["last_run_date"] = today_iso
    save_state(state)
    if on_search_complete is not None:
        await on_search_complete(seen_ids)

    logger.info("VA public notice pull complete: %d new records", len(all_notices))
    return all_notices


def _resolve_since_date(mode: str, since: str | None, state: dict) -> str | None:
    if since:
        return since
    if mode == "historical":
        return (datetime.now() - timedelta(days=365)).strftime("%Y-%m-%d")
    # daily
    last = state.get("last_run_date") or ""
    if last:
        return last
    return (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")


def _parse_proxy(proxy_url: str) -> dict:
    from urllib.parse import urlparse
    p = urlparse(proxy_url)
    cfg: dict = {"server": f"{p.scheme}://{p.hostname}:{p.port}"}
    if p.username:
        cfg["username"] = p.username
    if p.password:
        cfg["password"] = p.password
    return cfg


# ── Sync wrapper (SiftStack puller contract) ───────────────────────────


def pull_new_records(
    *,
    mode: str = "daily",
    since: str | None = None,
    headless: bool = True,
    proxy_url: str | None = None,
) -> list[NoticeData]:
    """Synchronous entry point used by main.py and the Apify Actor flow."""
    return asyncio.run(
        pull_new_records_async(
            mode=mode, since=since, headless=headless, proxy_url=proxy_url,
        )
    )


# ── Diagnostic CLI ─────────────────────────────────────────────────────


def _cli() -> None:
    p = argparse.ArgumentParser(description="Virginia Public Notice (VPA) puller")
    p.add_argument("--mode", choices=["daily", "historical"], default="daily")
    p.add_argument("--since", help="Override since-date YYYY-MM-DD")
    p.add_argument("--headed", action="store_true", help="Run with visible browser")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    records = pull_new_records(mode=args.mode, since=args.since, headless=not args.headed)
    print(f"New records: {len(records)}")
    for r in records[:5]:
        print(f"  [{r.notice_type}] {r.county} | {r.owner_name or r.decedent_name} "
              f"| {r.address or '(no addr)'} | {r.source_url}")


if __name__ == "__main__":
    _cli()
