"""PropertyRadar puller — Playwright web-UI automation against app.propertyradar.com.

Designed around the PropertyRadar UI behaviour captured in Plan 02-03,
not the (incorrect) filter-before-export model originally assumed:

  * No "Added to List" filter exists — delta comes from membership-set diff
    (scrape current RadarIDs → compare to last run's set → new IDs = today's delta).
  * Export wizard is a single page — Purchase is clicked directly after the
    field-set is picked; CSV format radio lives in the post-Purchase modal.
  * Navigation is hash-routed; page.reload() drops the session, so we never
    reload or .goto() a different URL after login.

Per PR-07: PR state files (`pr_state.json` / `pr_cookies.json`) are isolated
from TN scraper state files.
"""

import asyncio
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from playwright.async_api import (
    Page,
    TimeoutError as PwTimeout,
    async_playwright,
)

import config
from datasift_core import dismiss_popups
from notice_parser import NoticeData
from propertyradar_config import (
    JS_PR_GRID_STORE_COUNT,
    JS_PR_GRID_STORE_RECORDS,
    JS_PR_TICK_USER_AGREEMENT,
    LIFECYCLE_ACTIVE,
    LIFECYCLE_EXITED,
    LIFECYCLE_REENTERED,
    PR_BASE_URL,
    PR_COOKIES_FILE,
    PR_DAILY_DELTA_MAX_RECORDS,
    PR_FIELD_SET_NAME,
    PR_LOGIN_URL,
    PR_STATE_SCHEMA_VERSION_V2,
    PROPERTYRADAR_EMAIL,
    PROPERTYRADAR_LISTS,
    PROPERTYRADAR_PASSWORD,
    PropertyRadarList,
    SEL_PR_DASHBOARD_SENTINEL,
    SEL_PR_DOWNLOAD_MODAL,
    SEL_PR_DOWNLOADS_AREA,
    SEL_PR_EXPORT_CSV_RADIO,
    SEL_PR_EXPORT_DOWNLOAD,
    SEL_PR_EXPORT_MENU,
    SEL_PR_EXPORT_PURCHASE,
    SEL_PR_EXPORT_TO_FILE,
    SEL_PR_FIELD_SET_OPTION,
    SEL_PR_FIELD_SET_PICKER,
    SEL_PR_LIST_NAV,
    SEL_PR_LOGIN_EMAIL,
    SEL_PR_LOGIN_PASSWORD,
    SEL_PR_LOGIN_SUBMIT,
    load_pr_state,
    save_pr_state,
)
from propertyradar_parser import parse_pr_csv
from propertyradar_quota import (
    QuotaExceededError,
    can_export,
    record_export,
)

logger = logging.getLogger(__name__)


# ── Custom exceptions ───────────────────────────────────────────────

class QuotaGuardError(Exception):
    """Raised when the per-list delta size exceeds PR_DAILY_DELTA_MAX_RECORDS.

    Almost certainly indicates a stale or missing state file (every list
    member would be treated as "new"). Aborts all remaining lists wholesale
    so a single bad state file doesn't drain the monthly export quota.
    """


class AsyncExportUnsupportedError(Exception):
    """Raised when an export routes to the async/email path AND
    SEL_PR_DOWNLOADS_AREA is the TBD sentinel (not yet captured).
    Surfaces as a clear escalation rather than silently waiting forever.
    """


# ── Cookie persistence (mirrors scraper.py L485-506; swaps file path) ─

async def _save_cookies(context) -> None:
    """Save PR session cookies to pr_cookies.json (NOT cookies.json — PR-07)."""
    try:
        cookies = await context.cookies()
        config.save_state(PR_COOKIES_FILE, cookies)
        logger.debug("Saved %d PR cookies to %s", len(cookies), PR_COOKIES_FILE)
    except Exception:
        logger.debug("Could not save PR cookies", exc_info=True)


async def _load_cookies(context) -> bool:
    """Load PR session cookies from pr_cookies.json. Returns True if loaded."""
    cookies = config.load_state(PR_COOKIES_FILE)
    if not cookies:
        return False
    try:
        await context.add_cookies(cookies)
        logger.debug("Loaded %d PR cookies from %s", len(cookies), PR_COOKIES_FILE)
        return True
    except Exception:
        logger.debug("Could not load PR cookies", exc_info=True)
        return False


# ── Popup dismissal (REUSE datasift_core) ─────────────────────────────

async def _dismiss_pr_popups(page: Page) -> None:
    """Dismiss SaaS popups before any click interaction.

    Calls datasift_core.dismiss_popups (Beamer NPS, Beamer push modal,
    generic fixed/absolute overlays). Already battle-hardened against PR's
    overlay zoo by Plan 02-03's dump_pages.py captures.
    """
    try:
        await dismiss_popups(page)
    except Exception:
        logger.debug("dismiss_popups raised — continuing", exc_info=True)


# ── Session validity (visible login form == not logged in) ──────────

async def _is_session_valid(page: Page) -> bool:
    """Check whether we're authenticated.

    PR's login URL is the bare base URL, so URL-substring checks like
    `"/login" in url` give false negatives. Reliable signals:
      - if `input[type="password"]` is visible → we're on the login form
      - if `label.fr-account-name` has non-empty text → we're logged in
        (the label is also rendered EMPTY on the login page, so a bare
        .count() gives a false positive).
    """
    try:
        await page.locator('input[type="password"]').first.wait_for(
            state="visible", timeout=1500,
        )
        return False  # password field showing — definitely not logged in
    except Exception:
        pass

    try:
        text = (await page.locator(SEL_PR_DASHBOARD_SENTINEL).first.inner_text(
            timeout=2000,
        )).strip()
        if text:
            logger.debug("Session valid — fr-account-name = %r", text)
            return True
    except Exception:
        pass
    return False


# ── Login (ExtJS form, requires User Agreement tick) ────────────────

async def login(page: Page, _retries: int = 3) -> bool:
    """Log in to app.propertyradar.com.

    PropertyRadar's login is an ExtJS form (NOT plain HTML):
      1. Fill email + password (`name="userEmail"` / `name="userPW"`).
      2. Tick the User Agreement checkbox via ExtJS — the native input is
         visually hidden, so a normal click doesn't work; the Login button
         stays `x-btn-disabled` until the ExtJS model is set.
      3. Click the Login button (`<a class="x-btn">`, label "Login").
      4. Wait for the password field to become `hidden` — that's the
         reliable "login completed" signal.
    """
    if not PROPERTYRADAR_EMAIL or not PROPERTYRADAR_PASSWORD:
        logger.error("PROPERTYRADAR_EMAIL / PROPERTYRADAR_PASSWORD not set")
        return False

    for attempt in range(1, _retries + 1):
        try:
            logger.info(
                "Logging in to %s (attempt %d/%d)",
                PR_LOGIN_URL, attempt, _retries,
            )
            await page.goto(
                PR_LOGIN_URL, wait_until="domcontentloaded", timeout=30_000,
            )
            break
        except Exception as exc:
            logger.warning(
                "PR login navigation failed (attempt %d/%d): %s",
                attempt, _retries, exc,
            )
            if attempt < _retries:
                await asyncio.sleep(5 * attempt)
                continue
            return False

    try:
        await _dismiss_pr_popups(page)
        # Wait for the form to render before filling — PR's login is SPA,
        # the form draws asynchronously.
        await page.locator(SEL_PR_LOGIN_EMAIL).first.wait_for(
            state="visible", timeout=25_000,
        )
        await page.fill(SEL_PR_LOGIN_EMAIL, PROPERTYRADAR_EMAIL)
        await page.fill(SEL_PR_LOGIN_PASSWORD, PROPERTYRADAR_PASSWORD)
        # Tick the User Agreement checkbox — required to enable the Login button.
        await page.evaluate(JS_PR_TICK_USER_AGREEMENT)
        await asyncio.sleep(1)  # let ExtJS recompute button-enabled state
        await page.click(SEL_PR_LOGIN_SUBMIT)
        # Login complete when the password field is gone (SPA navigates away).
        await page.locator(SEL_PR_LOGIN_PASSWORD).first.wait_for(
            state="hidden", timeout=30_000,
        )
    except Exception:
        logger.exception("PR login form interaction failed")
        return False

    if await _is_session_valid(page):
        logger.info("PR login successful — on %s", page.url)
        return True
    logger.error("PR login uncertain — URL %s but session-validity check failed",
                 page.url)
    return False


# ── Membership scrape (the canonical delta source) ──────────────────

async def _scroll_until_store_stable(page: Page) -> int | None:
    """PageDown the buffered grid until the ExtJS Store count stops growing.

    Returns the stable count, or `None` if no visible grid was found.
    """
    previous_count = -1
    stable_iterations = 0
    max_iters = 200  # safety cap; ~10K rows at ~50/page
    for _ in range(max_iters):
        count = await page.evaluate(JS_PR_GRID_STORE_COUNT)
        if count is None:
            logger.warning("No visible grid found — store count is None")
            return None
        if count == previous_count:
            stable_iterations += 1
            if stable_iterations >= 2:
                return count
        else:
            stable_iterations = 0
            previous_count = count
        await page.keyboard.press("PageDown")
        await asyncio.sleep(0.4)  # let the store fetch + render
    return previous_count


async def _scrape_list_records(page: Page) -> dict[str, dict]:
    """Scrape the full set of records (keyed by RadarID) from the open list view.

    Returns a dict like:
        {"P12345": {"Address": "...", "City": "...", ..., "Owner": "..."}, ...}

    The full record (not just the RadarID) is needed so the puller can emit
    synthetic exit/reentry NoticeData with the last-known address + owner
    when a property leaves the list later. See JS_PR_GRID_STORE_RECORDS for
    the exact field list — only essentials are persisted (full 250-field
    records would bloat pr_state.json).
    """
    if await _scroll_until_store_stable(page) is None:
        return {}
    records = await page.evaluate(JS_PR_GRID_STORE_RECORDS)
    if not records:
        return {}
    out: dict[str, dict] = {}
    for r in records:
        rid = r.get("RadarID")
        if not rid:
            continue
        out[str(rid)] = {
            k: v for k, v in r.items() if k != "RadarID"
        }
    return out


# Back-compat thin wrapper — older callers / tests that just want RadarIDs.
async def _scrape_list_members(page: Page) -> list[str]:
    """Scrape the set of currently-loaded RadarIDs. Thin wrapper around
    _scrape_list_records for callers that don't need the full records."""
    records = await _scrape_list_records(page)
    return sorted(records.keys())


# ── Lifecycle (formerly plan 02-07, folded into the puller) ─────────

def _migrate_list_registry_v1(legacy: list[str], today: str) -> dict[str, dict]:
    """Migrate a v1 entry (list[RadarID]) to the v2 dict-of-records shape.

    v1 had no per-RadarID metadata, so first_seen / last_seen default to
    today and last-known data is empty. A subsequent scrape populates the
    real values.
    """
    return {
        str(rid): {
            "first_seen": today,
            "last_seen": today,
            "exited_at": None,
            "status": LIFECYCLE_ACTIVE,
            "data": {},
        }
        for rid in legacy
        if rid
    }


def _coerce_list_registry(entry, today: str) -> dict[str, dict]:
    """Return a v2 registry dict whatever the on-disk shape was.

    Accepts:
      - list[RadarID]                 (v1 schema) — migrated to v2
      - dict[RadarID, record-dict]    (v2 schema) — passed through
      - None / anything else          → empty dict
    """
    if isinstance(entry, list):
        return _migrate_list_registry_v1(entry, today)
    if isinstance(entry, dict):
        return entry
    return {}


def _build_lifecycle_notice(
    pr_list: PropertyRadarList,
    radar_id: str,
    record: dict,
    lifecycle: str,
    today: str,
) -> NoticeData:
    """Synthesise a NoticeData from a registry record + lifecycle event.

    `record["data"]` is whatever last-known fields the previous scrape
    captured (Address/City/State/ZIP/Owner/County). Missing fields
    degrade gracefully — the downstream pipeline tolerates empty strings.
    """
    data = record.get("data") or {}
    return NoticeData(
        date_added=today,
        address=data.get("Address", ""),
        city=data.get("City", ""),
        state=data.get("State", "") or pr_list.state,
        zip=data.get("ZIP", ""),
        owner_name=data.get("Owner", ""),
        notice_type=pr_list.notice_type,
        county=data.get("County", ""),
        source_url=f"propertyradar://radarid/{radar_id}",
        pr_lifecycle=lifecycle,
        pr_list_slug=pr_list.slug,
        pr_lifecycle_date=today,
    )


def _compute_lifecycle(
    pr_list: PropertyRadarList,
    current_records: dict[str, dict],
    previous_registry: dict[str, dict],
    today: str,
) -> tuple[dict[str, dict], list[NoticeData], list[NoticeData]]:
    """Diff `current_records` against `previous_registry`; return the
    next-run registry plus synthetic exit/reentry NoticeData lists.

    Transitions:
      active   → exited      (was active last run, missing this run)
      exited   → reentered   (was exited, present again)
      absent   → active      (brand-new RadarID — gets a real export, not a synthetic)
      active   → active      (no change; last_seen bumped)
      reentered→ active      (promoted on the next run after re-entry)
    """
    next_registry: dict[str, dict] = {}
    exit_notices: list[NoticeData] = []
    reentry_notices: list[NoticeData] = []

    current_ids = set(current_records.keys())
    prev_ids = set(previous_registry.keys())

    for rid in current_ids:
        prev = previous_registry.get(rid)
        if prev is None:
            # Brand-new — no synthetic, it'll flow through the regular export.
            next_registry[rid] = {
                "first_seen": today,
                "last_seen": today,
                "exited_at": None,
                "status": LIFECYCLE_ACTIVE,
                "data": current_records[rid],
            }
        elif prev.get("status") == LIFECYCLE_EXITED:
            # Re-entry — was gone, back again.
            next_registry[rid] = {
                "first_seen": prev.get("first_seen") or today,
                "last_seen": today,
                "exited_at": None,
                "status": LIFECYCLE_REENTERED,
                "data": current_records[rid],
            }
            reentry_notices.append(_build_lifecycle_notice(
                pr_list, rid, next_registry[rid], LIFECYCLE_REENTERED, today,
            ))
        else:
            # Continuing presence — bump last_seen, refresh last-known data,
            # promote `reentered → active` so the next run treats it normally.
            next_registry[rid] = {
                "first_seen": prev.get("first_seen") or today,
                "last_seen": today,
                "exited_at": None,
                "status": LIFECYCLE_ACTIVE,
                "data": current_records[rid],
            }

    for rid in prev_ids - current_ids:
        prev = previous_registry[rid]
        prev_status = prev.get("status", LIFECYCLE_ACTIVE)
        if prev_status == LIFECYCLE_EXITED:
            # Already exited and still missing — keep the record but don't
            # emit a new notice (avoid duplicate `pr_exited_*` tags).
            next_registry[rid] = prev
        else:
            # Newly exited.
            next_registry[rid] = {
                **prev,
                "status": LIFECYCLE_EXITED,
                "exited_at": today,
            }
            exit_notices.append(_build_lifecycle_notice(
                pr_list, rid, next_registry[rid], LIFECYCLE_EXITED, today,
            ))

    return next_registry, exit_notices, reentry_notices


# ── Quota guard ──────────────────────────────────────────────────────

async def _quota_guard(
    pr_list: PropertyRadarList,
    delta_radar_ids: list[str],
    max_records: int = PR_DAILY_DELTA_MAX_RECORDS,
) -> None:
    """Refuse to proceed if the delta size suggests a stale state file.

    Without an in-UI date filter, every run scrapes the FULL list and
    diffs against the previous run. If the previous-run set is empty
    (first run, or state file lost/corrupt), every member is "new" — for
    a list with 200 members that's a 200-record export that wasn't
    intended. This guard catches that case BEFORE Purchase consumes quota.

    Default threshold: 500 records (`PR_DAILY_DELTA_MAX_RECORDS`,
    configurable via env). Healthy daily deltas should be in the low tens.
    """
    n = len(delta_radar_ids)
    if n <= max_records:
        logger.info("Quota guard PASSED for %s: %d new RadarIDs (≤ %d)",
                    pr_list.name, n, max_records)
        return
    msg = (
        f"QUOTA GUARD: list {pr_list.name!r} delta is {n} RadarIDs — "
        f"exceeds threshold of {max_records}. Almost certainly the previous-"
        f"run state file is stale or missing; exporting this many records "
        f"would waste a chunk of the 10K/month Solo-plan quota. Aborting "
        f"BEFORE Purchase. Restore the previous state file or, if this is "
        f"intentional, raise PR_DAILY_DELTA_MAX_RECORDS and re-run."
    )
    logger.error(msg)
    try:
        from slack_notifier import notify_error
        notify_error(
            step="propertyradar_puller quota guard",
            error=QuotaGuardError(msg),
            context=f"list={pr_list.name}, delta_count={n}, threshold={max_records}",
        )
    except Exception:
        logger.debug("Could not send Slack quota-guard alert", exc_info=True)
    raise QuotaGuardError(msg)


# ── Export the delta (only called when delta is non-empty) ──────────

async def _export_delta(
    page: Page,
    pr_list: PropertyRadarList,
    delta_radar_ids: list[str],
    download_dir: Path,
) -> Path:
    """Run the PR export wizard end-to-end and return the downloaded CSV path.

    The wizard order discovered in Plan 02-03 (single-page wizard, CSV in
    post-Purchase modal) is the inverse of what the original plan assumed:

      Actions → Export to File → (wait for wizard) → open field-set picker
      → click 'SiftStack Export' option → click Purchase (NOT Continue)
      → wait for the 'Download Export' modal → click CSV radio label
      → click Download.

    Continue is never used in this flow.
    """
    await _dismiss_pr_popups(page)
    await page.click(SEL_PR_EXPORT_MENU)
    await asyncio.sleep(0.5)
    await page.click(SEL_PR_EXPORT_TO_FILE)
    await asyncio.sleep(3)  # wizard render
    await _dismiss_pr_popups(page)

    # Open the field-set picker and select PR_FIELD_SET_NAME.
    await page.click(SEL_PR_FIELD_SET_PICKER)
    await asyncio.sleep(1)
    await page.click(SEL_PR_FIELD_SET_OPTION.replace("{name}", PR_FIELD_SET_NAME))
    await asyncio.sleep(1)

    # Click Purchase directly — the wizard's Continue button is unused.
    # This opens the "Download Export" modal; quota is NOT consumed yet
    # (it's consumed on the modal's Download click).
    logger.info("Clicking Purchase for list %s — opens the Download Export modal",
                pr_list.name)
    await page.click(SEL_PR_EXPORT_PURCHASE)
    await asyncio.sleep(3)

    # In the modal: pick the CSV format radio (label-click — the native
    # radio is visually hidden), then click Download via _download_export.
    await page.locator(SEL_PR_DOWNLOAD_MODAL).locator(
        SEL_PR_EXPORT_CSV_RADIO,
    ).first.click()
    await asyncio.sleep(0.5)

    return await _download_export(page, pr_list, download_dir)


# ── Per-list runner ──────────────────────────────────────────────────

async def run_list(
    page: Page,
    pr_list: PropertyRadarList,
    previous_registry: dict[str, dict],
    download_dir: Path,
    today: str | None = None,
) -> tuple[list[NoticeData], dict[str, dict]]:
    """Process one list. Returns (notices, updated-registry).

    `previous_registry` is the v2-shape entry for THIS list from the last
    run — `{RadarID: {first_seen, last_seen, exited_at, status, data}}`.
    The returned registry is the new baseline to persist for next run.

    Steps:
      1. Click into the list from the My Lists view.
      2. Scrape the FULL current record set via the ExtJS Store.
      3. Compute lifecycle: brand-new IDs → delta to export;
         disappeared IDs → synthetic `pr_exited_*` notices;
         re-appearing previously-exited IDs → synthetic `pr_reentered_*`.
      4. Quota guard on the brand-new delta (refuses huge "everything is new"
         scenarios that signal a stale state file).
      5. If delta is empty → skip the PR export entirely (saves quota).
      6. Otherwise → run the export wizard, parse the CSV, local-filter to
         the delta RadarIDs.
      7. Return the regular new notices + exit + reentry synthetic notices.
    """
    today = today or datetime.now().strftime("%Y-%m-%d")
    logger.info("=== Starting list: %s ===", pr_list.name)

    # 1. Click into the list.
    await _dismiss_pr_popups(page)
    list_selector = SEL_PR_LIST_NAV.replace("{name}", pr_list.name)
    await page.click(list_selector)
    await asyncio.sleep(5)  # list view loads
    await _dismiss_pr_popups(page)

    # 2. Scrape current records (free — no quota).
    current_records = await _scrape_list_records(page)
    current_ids = set(current_records.keys())
    logger.info("List %s currently has %d members", pr_list.name, len(current_ids))

    # 3. Compute lifecycle.
    new_registry, exit_notices, reentry_notices = _compute_lifecycle(
        pr_list, current_records, previous_registry or {}, today,
    )
    # "Brand-new" = present in current scrape AND NOT in previous registry.
    delta = sorted(current_ids - set(previous_registry or {}))
    logger.info(
        "List %s: %d new RadarIDs (delta), %d exits, %d re-entries",
        pr_list.name, len(delta), len(exit_notices), len(reentry_notices),
    )

    # 4a. Per-run quota guard — catches the "stale state file dumps the full
    # list" regression. May raise QuotaGuardError. Counts only the brand-new
    # delta (exits/reentries don't consume export quota — they're synthetic).
    await _quota_guard(pr_list, delta)

    # 4b. Monthly-cumulative quota guard (PR-09). Orthogonal to _quota_guard:
    # that one catches "this run wants to do something insane"; this one
    # catches "this run is fine but you've spent 9,500 of 10,000 records
    # this month already". `can_export` reads pr_quota.json at call-time.
    allowed, reason = can_export(len(delta), today=today)
    if not allowed:
        raise QuotaExceededError(reason)

    new_notices: list[NoticeData] = []
    if delta:
        # 6. Run the export wizard and parse.
        csv_path = await _export_delta(page, pr_list, delta, download_dir)
        all_notices = parse_pr_csv(csv_path, notice_type=pr_list.notice_type)

        # Local-filter the parsed CSV to ONLY the delta RadarIDs. PR exports the
        # full list (no in-UI delta filter exists), so we filter here. The parser
        # sets source_url = "propertyradar://radarid/{RadarID}" per RESEARCH A4.
        delta_set = set(delta)
        new_notices = [
            n for n in all_notices
            if (n.source_url or "").rsplit("/", 1)[-1] in delta_set
        ]
        logger.info(
            "List %s: parsed %d records from export, kept %d matching delta",
            pr_list.name, len(all_notices), len(new_notices),
        )
        # Record the actual delivered count AFTER the download succeeds, so
        # the cumulative-month counter tracks delivery and not intent (T-02-08-01).
        # `len(delta)` is the same value `can_export` saw above, and matches
        # what PR billed against (filtering on RadarID was local; PR delivered
        # the full delta — that's what counts).
        record_export(len(delta), list_name=pr_list.name, today=today)
    else:
        logger.info("List %s: empty new-RadarID delta — skipping export (saves quota)",
                    pr_list.name)

    # 7. Combine regular notices with synthetic lifecycle notices.
    return new_notices + exit_notices + reentry_notices, new_registry


# ── Session-expiry recovery ──────────────────────────────────────────

async def _try_relogin(page: Page) -> bool:
    """Detect session expiry and attempt re-login. Returns True on success."""
    if await _is_session_valid(page):
        return False  # session is fine, the failure was something else
    logger.warning("PR session expired — attempting re-login")
    if await login(page):
        logger.info("PR re-login successful")
        # Re-navigate to My Lists in-app.
        await page.evaluate("window.location.hash = '#!/myLists'")
        await asyncio.sleep(4)
        return True
    logger.error("PR re-login failed")
    return False


# ── Top-level orchestrator ──────────────────────────────────────────

async def pull_all_lists(
    lists: Optional[list[PropertyRadarList]] = None,
    download_dir: Optional[Path] = None,
) -> list[NoticeData]:
    """Pull all configured PR lists; return combined NoticeData.

    State file shape (pr_state.json):
        {
          "<list.name>": ["RadarID1", "RadarID2", ...],
          ...,
          "_schema_version": 1
        }

    Per-list state is updated AFTER successful scrape + (optional) export.
    Quota guard fires → wholesale abort. Per-list non-quota failure →
    skip-and-continue (state for that list NOT updated).
    """
    if lists is None:
        lists = PROPERTYRADAR_LISTS
    if download_dir is None:
        download_dir = config.OUTPUT_DIR
    download_dir = Path(download_dir)
    download_dir.mkdir(parents=True, exist_ok=True)

    pr_state = load_pr_state()
    all_notices: list[NoticeData] = []
    quota_disaster = False

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        )
        ctx.set_default_timeout(60_000)
        await _load_cookies(ctx)
        page = await ctx.new_page()

        # Land on the app root; SPA redirects to the login form if not authed.
        await page.goto(PR_BASE_URL, wait_until="domcontentloaded", timeout=30_000)
        await asyncio.sleep(3)
        if not await _is_session_valid(page):
            if not await login(page):
                logger.error("PR login failed — aborting")
                await browser.close()
                return []
            await _save_cookies(ctx)

        # Navigate to My Lists (in-app, NEVER reload).
        await page.evaluate("window.location.hash = '#!/myLists'")
        await asyncio.sleep(6)

        today_iso = datetime.now().strftime("%Y-%m-%d")

        for idx, pr_list in enumerate(lists, start=1):
            logger.info("[%d/%d] Processing list: %s",
                        idx, len(lists), pr_list.name)

            # Migrate v1 list-of-IDs (or accept v2 dict-of-records) → v2.
            previous_registry = _coerce_list_registry(
                pr_state.get(pr_list.name), today_iso,
            )
            if not await _is_session_valid(page):
                if not await _try_relogin(page):
                    logger.error("Cannot recover PR session — aborting remaining lists")
                    break

            try:
                list_notices, new_registry = await run_list(
                    page, pr_list, previous_registry, download_dir, today=today_iso,
                )
                all_notices.extend(list_notices)
            except (QuotaGuardError, QuotaExceededError) as exc:
                # Wholesale-abort on either guard:
                #   QuotaGuardError    = per-run sanity (stale state file)
                #   QuotaExceededError = cumulative-month budget hit (PR-09)
                # Both signal "stop spending quota on this run" — continuing
                # to the next list would only consume more.
                logger.error("Quota guard fired (%s) — aborting all remaining lists: %s",
                             type(exc).__name__, exc)
                quota_disaster = True
                break
            except Exception:
                logger.exception("List %s failed — attempting one-shot recovery",
                                 pr_list.name)
                if await _try_relogin(page):
                    try:
                        list_notices, new_registry = await run_list(
                            page, pr_list, previous_registry, download_dir, today=today_iso,
                        )
                        all_notices.extend(list_notices)
                    except Exception:
                        logger.exception("List %s STILL failed — skipping",
                                         pr_list.name)
                        continue  # state NOT updated for this list
                else:
                    continue

            # Per-iteration persist. Bump _schema_version to v2 so future
            # readers know the per-RadarID dict shape (see _coerce_list_registry).
            pr_state[pr_list.name] = new_registry
            pr_state["_schema_version"] = PR_STATE_SCHEMA_VERSION_V2
            try:
                save_pr_state(pr_state)
            except Exception:
                logger.exception("Failed to persist pr_state after %s — continuing",
                                 pr_list.name)

            # Return to My Lists between iterations (in-app, no reload).
            await page.evaluate("window.location.hash = '#!/myLists'")
            await asyncio.sleep(4)

        await browser.close()

    if quota_disaster:
        logger.error("Run ended in quota-guard abort. Records pulled before "
                     "abort: %d", len(all_notices))
    logger.info("PR puller complete — %d total records across %d lists",
                len(all_notices), len(lists))
    return all_notices


# ── Download (sync + async fallback) ────────────────────────────────

_SYNC_DOWNLOAD_TIMEOUT_MS = 300_000   # 5 min
_ASYNC_POLL_TIMEOUT_MS = 600_000      # 10 min cap on async polling
_ASYNC_PATH_TBD_SENTINEL = "__TBD_ASYNC_PATH_NOT_YET_CAPTURED__"


async def _download_export(
    page: Page,
    pr_list: PropertyRadarList,
    download_dir: Path,
) -> Path:
    """Download the export CSV from the post-Purchase Download Export modal.

    Tries the sync `expect_download` path first (5-min cap). On PwTimeout,
    falls back to PR's in-app downloads area IF SEL_PR_DOWNLOADS_AREA has
    been captured. If the sentinel is still in place, raises
    AsyncExportUnsupportedError with a clear escalation.
    """
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    save_path = download_dir / f"pr_{pr_list.slug}_{ts}.csv"

    try:
        async with page.expect_download(
            timeout=_SYNC_DOWNLOAD_TIMEOUT_MS,
        ) as dl_info:
            # Download button lives inside the modal; SEL_PR_EXPORT_DOWNLOAD
            # is already scoped to it.
            await page.locator(SEL_PR_EXPORT_DOWNLOAD).first.click()
            logger.info("Clicked Download for %s — awaiting sync download",
                        pr_list.name)
        download = await dl_info.value
        await download.save_as(save_path)
        logger.info("Saved PR export sync: %s", save_path)
        return save_path
    except PwTimeout:
        logger.info(
            "Sync download timed out for %s after %ds — trying async fallback",
            pr_list.name, _SYNC_DOWNLOAD_TIMEOUT_MS / 1000,
        )

    if SEL_PR_DOWNLOADS_AREA == _ASYNC_PATH_TBD_SENTINEL:
        raise AsyncExportUnsupportedError(
            f"List {pr_list.name!r} routed to async/email export, but "
            f"SEL_PR_DOWNLOADS_AREA is the TBD sentinel — the async path "
            f"isn't captured yet. Re-run Plan 02-03's dump_pages.py with a "
            f"list large enough to push the export to the async path, "
            f"capture the in-app downloads area selector, and update "
            f"propertyradar_config.py."
        )

    await page.click(SEL_PR_DOWNLOADS_AREA)
    await asyncio.sleep(2)
    await _dismiss_pr_popups(page)

    deadline = asyncio.get_event_loop().time() + (_ASYNC_POLL_TIMEOUT_MS / 1000)
    while asyncio.get_event_loop().time() < deadline:
        try:
            async with page.expect_download(timeout=30_000) as dl_info:
                await page.locator(
                    f'{SEL_PR_DOWNLOADS_AREA} a:has-text("Download")'
                ).first.click(timeout=5_000)
            download = await dl_info.value
            await download.save_as(save_path)
            logger.info("Saved PR export async: %s", save_path)
            return save_path
        except PwTimeout:
            logger.debug("Async export not yet ready for %s — polling again",
                         pr_list.name)
            await asyncio.sleep(15)

    raise AsyncExportUnsupportedError(
        f"List {pr_list.name!r}: async download polling exceeded "
        f"{_ASYNC_POLL_TIMEOUT_MS / 1000}s with no ready link. Possibly "
        f"emailed-only delivery — check the PR account email."
    )
