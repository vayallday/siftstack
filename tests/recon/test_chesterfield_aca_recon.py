"""One-off recon: capture Chesterfield ACA Code Violation report output.

Anonymous flow per memory chesterfield-aca-code-violation-report:
  chesterfield.gov ACA portal → Reports tab → Code Violation
  → enter start date + end date → Submit → results render inline (probably).

Goal: capture artifacts that show the report's actual output schema so the
puller can be designed against real data, not assumptions. Specifically:
  - Field/column names in the result
  - Whether results paginate, infinite-scroll, or render in one shot
  - Whether there's a CSV/Excel export button
  - Date input behavior (text vs widget)
  - Any unexpected gating (popup, captcha, login wall)

Usage:
    python test_chesterfield_aca_recon.py                       # last 30 days
    python test_chesterfield_aca_recon.py --days 7              # custom window
    python test_chesterfield_aca_recon.py --start 2026-04-01 --end 2026-04-30
    python test_chesterfield_aca_recon.py --headless            # run without UI

Artifacts land in output/recon_chesterfield_aca/.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

# Add src/ to path so we can import shared helpers if needed
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from playwright.async_api import async_playwright, Page, TimeoutError as PwTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("recon")


LANDING_URL = "https://aca-prod.accela.com/CHESTERFIELD/Default.aspx"
# Direct URL discovered from landing-page HTML (recon run 1, 2026-05-25):
# the "Reports" tile expands to a tooltip with direct anchor links — no
# click-through chain needed. Code Violation reportID=9735.
CODE_VIOLATION_URL = (
    "https://aca-prod.accela.com/CHESTERFIELD/Report/ReportParameter.aspx"
    "?module=&reportID=9735&reportType=LINK_REPORT_LIST"
)
RECON_DIR = Path(__file__).parent / "output" / "recon_chesterfield_aca"


async def try_click(page: Page, selectors: list[str], label: str, timeout: int = 5000) -> str | None:
    """Try a list of selectors until one clicks; return the one that worked."""
    for sel in selectors:
        try:
            await page.click(sel, timeout=timeout)
            logger.info("[%s] clicked via: %s", label, sel)
            return sel
        except PwTimeout:
            continue
        except Exception as e:
            logger.debug("[%s] selector %s raised: %s", label, sel, e)
    logger.warning("[%s] no selector matched", label)
    return None


async def try_fill(page: Page, selectors: list[str], value: str, label: str) -> str | None:
    """Try selectors until one fills successfully."""
    for sel in selectors:
        try:
            await page.fill(sel, value, timeout=3000)
            logger.info("[%s] filled via: %s -> %s", label, sel, value)
            return sel
        except (PwTimeout, Exception):
            continue
    # Last-ditch: JS-set the value
    for sel in selectors:
        try:
            await page.evaluate(
                """({sel, value}) => {
                    const el = document.querySelector(sel);
                    if (!el) return false;
                    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
                    setter.call(el, value);
                    el.dispatchEvent(new Event('input', {bubbles: true}));
                    el.dispatchEvent(new Event('change', {bubbles: true}));
                    return true;
                }""",
                {"sel": sel, "value": value},
            )
            logger.info("[%s] JS-set via: %s -> %s", label, sel, value)
            return sel
        except Exception:
            continue
    logger.warning("[%s] no fill selector matched", label)
    return None


async def dismiss_popups(page: Page) -> None:
    """Best-effort dismissal of Beamer / cookie / popup overlays."""
    try:
        await page.evaluate(
            """() => {
                // Common overlay containers seen on Accela / municipal portals
                const ids = ['npsIframeContainer', 'beamerPushModal', 'cookieconsent',
                             'cookie-banner', 'onetrust-banner-sdk'];
                for (const id of ids) {
                    const el = document.getElementById(id);
                    if (el) el.remove();
                }
                document.querySelectorAll('[id*="ookie"][role="dialog"]').forEach(e => e.remove());
            }"""
        )
    except Exception:
        pass


async def main() -> None:
    parser = argparse.ArgumentParser(description="Chesterfield ACA Code Violation report recon")
    parser.add_argument("--days", type=int, default=30, help="Date window in days back from today")
    parser.add_argument("--start", type=str, default=None, help="Override start date YYYY-MM-DD")
    parser.add_argument("--end", type=str, default=None, help="Override end date YYYY-MM-DD")
    parser.add_argument("--headless", action="store_true", help="Run headless (default: headed)")
    parser.add_argument("--slow-mo", type=int, default=200, help="Slow-mo delay ms (headed only)")
    args = parser.parse_args()

    end_d = datetime.strptime(args.end, "%Y-%m-%d").date() if args.end else date.today()
    start_d = (
        datetime.strptime(args.start, "%Y-%m-%d").date()
        if args.start else end_d - timedelta(days=args.days)
    )

    logger.info("Date range: %s -> %s", start_d, end_d)

    RECON_DIR.mkdir(parents=True, exist_ok=True)
    (RECON_DIR / "downloads").mkdir(exist_ok=True)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=args.headless,
            slow_mo=0 if args.headless else args.slow_mo,
        )
        context = await browser.new_context(
            accept_downloads=True,
            viewport={"width": 1400, "height": 900},
        )
        page = await context.new_page()

        # Track any file downloads triggered by Submit
        downloads: list = []
        page.on("download", lambda d: downloads.append(d))
        # Track all navigation responses for hidden CSV/JSON endpoints
        responses: list[dict] = []

        def _on_response(resp):
            ct = resp.headers.get("content-type", "")
            if any(t in ct for t in ("csv", "json", "excel", "vnd.ms", "octet-stream")):
                responses.append({"url": resp.url, "status": resp.status, "content_type": ct})

        page.on("response", _on_response)

        # ── Step 1: landing page (establishes session cookies) ─────
        logger.info("[1] Goto landing: %s", LANDING_URL)
        await page.goto(LANDING_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(2000)
        await dismiss_popups(page)
        await page.screenshot(path=str(RECON_DIR / "01_landing.png"), full_page=True)

        # ── Step 2-3: navigate directly to the Code Violation report form ──
        logger.info("[3] Goto report form: %s", CODE_VIOLATION_URL)
        await page.goto(CODE_VIOLATION_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        await dismiss_popups(page)
        await page.screenshot(path=str(RECON_DIR / "03_code_violation_form.png"), full_page=True)
        (RECON_DIR / "03_code_violation_form.html").write_text(await page.content(), encoding="utf-8")
        logger.info("[3] URL on report form: %s", page.url)

        # ── Step 4: fill date range ────────────────────────────────
        # Explicit IDs discovered in recon run 1: Date_11907=Start, Date_11908=End.
        # These inputs use AjaxControlToolkit MaskedEdit — fast page.fill() leaves
        # the widget in an invalid state. Solution: click → clear → type with
        # per-keystroke delay so the mask validator processes each char.
        start_str = start_d.strftime("%m/%d/%Y")
        end_str = end_d.strftime("%m/%d/%Y")

        async def _fill_masked(sel: str, value: str, label: str) -> bool:
            try:
                await page.click(sel, timeout=3000)
                await page.keyboard.press("Control+A")
                await page.keyboard.press("Delete")
                await page.type(sel, value, delay=40)
                await page.keyboard.press("Tab")  # blur → trigger validator
                logger.info("[%s] typed masked: %s -> %s", label, sel, value)
                return True
            except Exception as e:
                logger.warning("[%s] failed to type into %s: %s", label, sel, e)
                return False

        start_ok = await _fill_masked("#Date_11907", start_str, "StartDate")
        end_ok = await _fill_masked("#Date_11908", end_str, "EndDate")

        await page.wait_for_timeout(500)
        await page.screenshot(path=str(RECON_DIR / "04_dates_filled.png"), full_page=True)

        if not (start_ok and end_ok):
            logger.warning("Date inputs not filled — see 03_code_violation_form.html")

        # ── Step 5: Submit (btnSave id confirmed in recon run 1) ───
        # ASP.NET WebForm postback. The redirect target opens in a NEW TAB —
        # use context.expect_page() to capture the popup as it opens.
        logger.info("[5] Submitting via #btnSave, waiting for popup...")
        report_page: Page = page
        try:
            async with context.expect_page(timeout=20000) as popup_info:
                await page.click("#btnSave")
            report_page = await popup_info.value
            logger.info("[5] Popup opened: %s", report_page.url)
            try:
                await report_page.wait_for_load_state("domcontentloaded", timeout=20000)
            except PwTimeout:
                logger.info("[5] Popup DOM-content-loaded timeout")
        except PwTimeout:
            logger.info("[5] No popup detected in 20s — capturing same page")
            await page.screenshot(path=str(RECON_DIR / "05_submit_no_popup.png"), full_page=True)

        # Let the report render
        try:
            await report_page.wait_for_load_state("networkidle", timeout=45000)
        except PwTimeout:
            logger.info("[5] networkidle not reached in 45s — proceeding")
        await report_page.wait_for_timeout(2000)

        await report_page.screenshot(path=str(RECON_DIR / "05_after_submit.png"), full_page=True)
        (RECON_DIR / "05_result.html").write_text(
            await report_page.content(), encoding="utf-8"
        )
        logger.info("[5] Final URL: %s", report_page.url)

        # Re-point page variable so the rest of the recon inspects the result tab
        page = report_page

        # ── Step 6: capture downloads (if Submit triggered one) ────
        for i, d in enumerate(downloads):
            target = RECON_DIR / "downloads" / d.suggested_filename
            await d.save_as(str(target))
            logger.info("[6] saved download: %s", target)

        # ── Step 7: probe for any obvious "Export" / "Download" links on results ──
        try:
            export_links = await page.evaluate(
                """() => Array.from(document.querySelectorAll('a, button, input[type=submit]'))
                    .filter(e => /export|download|csv|excel/i.test(e.textContent + ' ' + (e.value || '')))
                    .map(e => ({ text: (e.textContent || e.value || '').trim().slice(0, 80),
                                 href: e.href || '',
                                 id: e.id || '' }))"""
            )
            if export_links:
                logger.info("[7] Possible export links: %s", export_links)
                (RECON_DIR / "07_export_links.json").write_text(
                    __import__("json").dumps(export_links, indent=2), encoding="utf-8"
                )
        except Exception as e:
            logger.warning("[7] export probe failed: %s", e)

        # ── Step 8: extract any visible table structure ────────────
        try:
            tables = await page.evaluate(
                """() => Array.from(document.querySelectorAll('table')).map(t => ({
                    rows: t.rows.length,
                    headers: t.tHead ? Array.from(t.tHead.querySelectorAll('th, td')).map(c => c.innerText.trim()) : [],
                    firstRow: t.tBodies[0] && t.tBodies[0].rows[0]
                        ? Array.from(t.tBodies[0].rows[0].cells).map(c => c.innerText.trim())
                        : [],
                    id: t.id,
                    className: t.className,
                }))"""
            )
            logger.info("[8] Tables on result page: %d found", len(tables))
            for i, t in enumerate(tables):
                logger.info(
                    "    table %d: id=%r class=%r rows=%d headers=%s",
                    i, t.get("id"), t.get("className"), t.get("rows"), t.get("headers"),
                )
            (RECON_DIR / "08_tables.json").write_text(
                __import__("json").dumps(tables, indent=2), encoding="utf-8"
            )
        except Exception as e:
            logger.warning("[8] table probe failed: %s", e)

        # ── Captured CSV/JSON responses ────────────────────────────
        if responses:
            (RECON_DIR / "09_data_responses.json").write_text(
                __import__("json").dumps(responses, indent=2), encoding="utf-8"
            )
            logger.info("[9] Captured %d data-content responses (see 09_data_responses.json)", len(responses))

        logger.info("Recon complete. Artifacts: %s", RECON_DIR)
        if not args.headless:
            logger.info("Browser staying open 20s for manual inspection...")
            await page.wait_for_timeout(20000)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
