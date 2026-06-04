"""One-off recon: capture Richmond OPP / EnerGov search behavior.

Portal per memory richmond-opp-energov:
  https://energov.richmondgov.com/EnerGov_Prod/SelfService/richmondvaprod#/home

Goal: understand the address search interface so the enrichment module can
target it cleanly. Specifically capture:
  - Anonymous access (no login required for search)
  - Search input format — operator says: number + direction + street name,
    NO suffix (St/Rd/Ave/etc)
  - Result structure: code cases, permits, inspections — what fields are
    exposed, how to distinguish "code case" from "permit"
  - JSON/XHR responses backing the SPA (likely the cleanest scrape surface)
  - Pagination / "no results" behavior

Test addresses to seed the recon (drawn from the Vacant Building List
April 2026 — these are known to be in Richmond, vacant, often distressed):
  - 10 E Baker St → "10 E Baker"
  - 1102 N 25th St → "1102 N 25"   (operator: omit "ST/N 25th" suffix?)
  - 219 W Broad St → "219 W Broad"

Usage:
    python test_richmond_opp_recon.py
    python test_richmond_opp_recon.py --headless
    python test_richmond_opp_recon.py --address "10 E Baker"

Artifacts in output/recon_richmond_opp/.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from playwright.async_api import async_playwright, Page, TimeoutError as PwTimeout

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("recon-opp")


PORTAL_URL = "https://energov.richmondgov.com/EnerGov_Prod/SelfService/richmondvaprod#/home"
RECON_DIR = Path(__file__).parent / "output" / "recon_richmond_opp"

# Test addresses pulled from the April 2026 Vacant Building List — known to be
# vacant Richmond properties, so OPP should have code cases on at least some.
DEFAULT_ADDRESSES = [
    "10 E Baker",       # 10 E Baker St
    "1102 N 25",        # 1102 N 25th St — Nasser portfolio
    "219 W Broad",      # 219 W Broad St — VCU area
    "102 E Broad",      # 102 E Broad St — Jemals portfolio
]


async def main() -> None:
    parser = argparse.ArgumentParser(description="Richmond OPP recon")
    parser.add_argument("--address", action="append", help="Address to test (repeatable)")
    parser.add_argument("--headless", action="store_true", help="Run headless")
    parser.add_argument("--slow-mo", type=int, default=200, help="Slow-mo ms (headed only)")
    args = parser.parse_args()

    addresses = args.address or DEFAULT_ADDRESSES
    RECON_DIR.mkdir(parents=True, exist_ok=True)

    # Captured XHR responses keyed by URL pattern — useful for finding the
    # backing API endpoint(s) so the enricher can bypass the SPA UI.
    xhr_capture: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=args.headless,
            slow_mo=0 if args.headless else args.slow_mo,
        )
        context = await browser.new_context(
            viewport={"width": 1500, "height": 1000},
        )
        page = await context.new_page()

        async def _on_response(resp):
            ct = resp.headers.get("content-type", "")
            if "json" in ct.lower() and resp.status == 200:
                try:
                    body_preview = await resp.text()
                    xhr_capture.append({
                        "url": resp.url,
                        "status": resp.status,
                        "content_type": ct,
                        "body_size": len(body_preview),
                        "body_preview": body_preview[:1500],
                    })
                except Exception:
                    pass

        page.on("response", _on_response)

        # ── Step 1: landing ────────────────────────────────────────
        logger.info("[1] Goto: %s", PORTAL_URL)
        await page.goto(PORTAL_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(4000)  # SPA hydration
        await page.screenshot(path=str(RECON_DIR / "01_landing.png"), full_page=True)
        (RECON_DIR / "01_landing.html").write_text(await page.content(), encoding="utf-8")

        # Dump visible top-level links so we can find "Property Search" / "Code Cases"
        top_links = await page.evaluate(
            """() => Array.from(document.querySelectorAll('a, button'))
                .filter(e => e.offsetParent !== null)
                .map(e => ({ text: (e.innerText || e.textContent || '').trim().slice(0, 80),
                             href: e.href || '',
                             id: e.id || '' }))
                .filter(o => o.text.length > 0 && o.text.length < 80)"""
        )
        # Only keep meaningful ones (skip empty/footer)
        meaningful = [t for t in top_links if not t["text"].startswith(("©", "·"))]
        (RECON_DIR / "01_top_links.json").write_text(
            json.dumps(meaningful[:60], indent=2), encoding="utf-8"
        )
        logger.info("[1] Captured %d top-level interactive elements", len(meaningful))

        # ── Step 2: find search affordance ─────────────────────────
        # OPP exposes search via the "Search Public Records" link OR direct nav
        # to a search URL. Try a few likely entry points.
        search_link_candidates = [
            "a:has-text('Search Public Records')",
            "a:has-text('Property Search')",
            "a:has-text('Search')",
            "[href*='SearchProperty']",
            "[href*='SearchPermit']",
        ]
        clicked = None
        for sel in search_link_candidates:
            try:
                el = page.locator(sel).first
                if await el.count() > 0:
                    await el.click(timeout=4000)
                    clicked = sel
                    break
            except Exception:
                continue

        if clicked:
            logger.info("[2] Clicked search link via: %s", clicked)
            await page.wait_for_timeout(3000)
            await page.screenshot(path=str(RECON_DIR / "02_after_search_click.png"), full_page=True)
            (RECON_DIR / "02_after_search.html").write_text(await page.content(), encoding="utf-8")
        else:
            logger.warning("[2] No search link found — see 01_top_links.json for available entry points")

        logger.info("[2] URL: %s", page.url)

        # ── Step 3: try each address ───────────────────────────────
        for i, addr in enumerate(addresses):
            logger.info("[3.%d] Searching: %r", i, addr)

            # Find an address input — heuristic
            input_candidates = [
                "input[placeholder*='ddress']",
                "input[placeholder*='Search']",
                "input[id*='Address']",
                "input[id*='Search']",
                "input[type='text']:visible",
            ]
            filled = False
            for sel in input_candidates:
                try:
                    locator = page.locator(sel).first
                    if await locator.count() == 0:
                        continue
                    await locator.click(timeout=3000)
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Delete")
                    await locator.fill(addr, timeout=3000)
                    filled = True
                    logger.info("[3.%d]   filled via: %s", i, sel)
                    break
                except Exception:
                    continue

            if not filled:
                logger.warning("[3.%d] couldn't find address input — skipping", i)
                await page.screenshot(path=str(RECON_DIR / f"03_{i}_no_input.png"), full_page=True)
                continue

            # Submit search
            search_btn_candidates = [
                "button:has-text('Search'):visible",
                "input[value='Search']",
                "button[type='submit']",
                "[id*='SearchButton']",
                "[id*='btnSearch']",
            ]
            submitted = False
            for sel in search_btn_candidates:
                try:
                    locator = page.locator(sel).first
                    if await locator.count() == 0:
                        continue
                    await locator.click(timeout=3000)
                    submitted = True
                    logger.info("[3.%d]   submitted via: %s", i, sel)
                    break
                except Exception:
                    continue

            if not submitted:
                # Try pressing Enter on the input
                try:
                    await page.keyboard.press("Enter")
                    submitted = True
                    logger.info("[3.%d]   submitted via Enter key", i)
                except Exception:
                    pass

            if not submitted:
                logger.warning("[3.%d] couldn't submit search — skipping", i)
                continue

            # Wait for results to render
            try:
                await page.wait_for_load_state("networkidle", timeout=20000)
            except PwTimeout:
                pass
            await page.wait_for_timeout(2500)

            slug = addr.replace(" ", "_").replace("/", "_")
            await page.screenshot(path=str(RECON_DIR / f"03_{i}_{slug}_results.png"), full_page=True)
            (RECON_DIR / f"03_{i}_{slug}_results.html").write_text(
                await page.content(), encoding="utf-8"
            )

            # Try to extract result-row structure
            try:
                tables = await page.evaluate(
                    """() => Array.from(document.querySelectorAll('table')).map(t => ({
                        rows: t.rows.length,
                        headers: t.tHead ? Array.from(t.tHead.querySelectorAll('th, td')).map(c => c.innerText.trim()) : [],
                        firstRow: t.tBodies[0] && t.tBodies[0].rows[0]
                            ? Array.from(t.tBodies[0].rows[0].cells).map(c => c.innerText.trim()) : [],
                        id: t.id, className: t.className,
                    }))"""
                )
                if tables:
                    (RECON_DIR / f"03_{i}_{slug}_tables.json").write_text(
                        json.dumps(tables, indent=2), encoding="utf-8"
                    )
                    logger.info("[3.%d]   tables: %d, headers: %s",
                                i, len(tables), [t["headers"] for t in tables if t["headers"]][:2])
            except Exception as e:
                logger.warning("[3.%d] table probe failed: %s", i, e)

            # Probe for grid/card containers (SPAs often use div lists, not tables)
            try:
                cards = await page.evaluate(
                    """() => {
                        const candidates = document.querySelectorAll('[class*="result"], [class*="row"], [class*="card"], [class*="grid"]');
                        return Array.from(candidates).slice(0, 5).map(el => ({
                            tag: el.tagName.toLowerCase(),
                            class: el.className,
                            text: (el.innerText || '').slice(0, 300),
                        }));
                    }"""
                )
                if cards:
                    (RECON_DIR / f"03_{i}_{slug}_cards.json").write_text(
                        json.dumps(cards, indent=2), encoding="utf-8"
                    )
            except Exception:
                pass

            # Brief pause between addresses so we can distinguish XHR responses
            await page.wait_for_timeout(1500)

        # ── Step 4: persist XHR capture ───────────────────────────
        if xhr_capture:
            (RECON_DIR / "04_xhr_responses.json").write_text(
                json.dumps(xhr_capture, indent=2, default=str), encoding="utf-8"
            )
            logger.info("[4] Captured %d JSON responses", len(xhr_capture))
            # Surface unique endpoint paths
            from urllib.parse import urlparse
            paths = sorted({urlparse(r["url"]).path for r in xhr_capture})
            (RECON_DIR / "04_unique_endpoint_paths.txt").write_text("\n".join(paths), encoding="utf-8")
            logger.info("[4] Unique endpoint paths:")
            for p in paths:
                logger.info("    %s", p)

        logger.info("Recon complete. Artifacts in %s", RECON_DIR)
        if not args.headless:
            await page.wait_for_timeout(15000)

        await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
