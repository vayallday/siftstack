"""Second recon: capture FULL request + response bodies for OPP search API.

Goal: understand the JSON API contract well enough to bypass the SPA entirely
and call the backing endpoints directly from a stateless enricher.

Specifically need:
  - POST body shape for /energov/search/search (filters? case-type filter?)
  - Full result shape — what record types appear, what fields are populated
  - Whether there's a code-case-specific endpoint that returns ONLY code cases

Test address: "1102 N 25" (Nasser portfolio, vacant per April 2026 list)
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

from playwright.async_api import async_playwright

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger("recon-opp-api")


RECON_DIR = Path(__file__).parent / "output" / "recon_richmond_opp_api"
PORTAL_URL = "https://energov.richmondgov.com/EnerGov_Prod/SelfService/richmondvaprod#/home"


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--address", default="1102 N 25")
    args = parser.parse_args()

    RECON_DIR.mkdir(parents=True, exist_ok=True)

    captured: list[dict] = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        context = await browser.new_context(viewport={"width": 1500, "height": 1000})
        page = await context.new_page()

        async def _on_request(req):
            if "/api/energov/" in req.url or "/api/codecases" in req.url.lower():
                try:
                    post_data = req.post_data
                except Exception:
                    post_data = None
                captured.append({
                    "kind": "request",
                    "method": req.method,
                    "url": req.url,
                    "post_data": post_data,
                })

        async def _on_response(resp):
            if "/api/energov/" not in resp.url and "/codecases" not in resp.url.lower():
                return
            try:
                body = await resp.text()
            except Exception:
                body = ""
            captured.append({
                "kind": "response",
                "status": resp.status,
                "url": resp.url,
                "content_type": resp.headers.get("content-type", ""),
                "body": body,
            })

        page.on("request", lambda r: asyncio.create_task(_on_request(r)))
        page.on("response", lambda r: asyncio.create_task(_on_response(r)))

        # Landing
        await page.goto(PORTAL_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)

        # Click main Search nav (instead of home search bar) so we land on
        # the dedicated SearchProperty/SearchPermit page which may have a
        # CodeCase filter tab.
        try:
            await page.click("a:has-text('Search')", timeout=5000)
            logger.info("Clicked Search nav")
        except Exception:
            logger.info("No Search nav — trying URL hash directly")
            await page.goto(
                "https://energov.richmondgov.com/EnerGov_Prod/SelfService/richmondvaprod#/search",
                wait_until="domcontentloaded",
            )
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(RECON_DIR / "01_search_page.png"), full_page=True)
        (RECON_DIR / "01_search_page.html").write_text(await page.content(), encoding="utf-8")

        # Look for radio/tab buttons that switch the search to "Code Cases" only.
        try:
            radios = await page.evaluate(
                """() => Array.from(document.querySelectorAll('input[type="radio"], button, a, [role="tab"]'))
                    .filter(e => e.offsetParent !== null)
                    .map(e => ({
                        tag: e.tagName.toLowerCase(),
                        type: e.type || '',
                        text: (e.innerText || e.textContent || e.value || '').trim().slice(0, 60),
                        name: e.name || '',
                        value: e.value || '',
                        id: e.id || '',
                    }))
                    .filter(o => o.text && o.text.length < 60)"""
            )
            relevant = [r for r in radios if any(
                k in (r.get("text") or "").lower() for k in
                ["code case", "violation", "permit", "plan", "inspect"]
            )]
            (RECON_DIR / "01_filter_controls.json").write_text(
                json.dumps(relevant, indent=2), encoding="utf-8"
            )
            logger.info("Filter controls found: %d relevant", len(relevant))
            for r in relevant[:10]:
                logger.info("  %s", r)
        except Exception as e:
            logger.warning("filter probe failed: %s", e)

        # Try toggling to Code Case search if there's a radio
        for tag_text in ["Code Case", "Code Cases", "Code Violation"]:
            try:
                await page.click(f"text='{tag_text}'", timeout=3000)
                logger.info("Clicked %r", tag_text)
                await page.wait_for_timeout(2000)
                await page.screenshot(
                    path=str(RECON_DIR / f"02_after_{tag_text.replace(' ', '_')}.png"),
                    full_page=True,
                )
                break
            except Exception:
                continue

        # Fill the search input
        addr_input_candidates = [
            "input[placeholder*='ddress']",
            "input[name*='Address']",
            "input[id*='Address']",
            "input[id*='Search']",
            "input[type='text']:visible",
        ]
        for sel in addr_input_candidates:
            try:
                loc = page.locator(sel).first
                if await loc.count() == 0:
                    continue
                await loc.click()
                await loc.fill(args.address)
                logger.info("Filled address via %s -> %r", sel, args.address)
                break
            except Exception:
                continue

        await page.wait_for_timeout(500)
        # Submit
        for sel in ["button:has-text('Search'):visible", "[id*='btnSearch']", "button[type='submit']"]:
            try:
                await page.click(sel, timeout=3000)
                logger.info("Submitted via %s", sel)
                break
            except Exception:
                continue
        else:
            await page.keyboard.press("Enter")
            logger.info("Submitted via Enter")

        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(3000)
        await page.screenshot(path=str(RECON_DIR / "03_results.png"), full_page=True)
        (RECON_DIR / "03_results.html").write_text(await page.content(), encoding="utf-8")

        # Persist full capture
        (RECON_DIR / "captured_xhr.json").write_text(
            json.dumps(captured, indent=2, default=str), encoding="utf-8"
        )

        # Pull out search/search request payloads + responses specifically
        search_requests = [c for c in captured if c.get("kind") == "request" and "/search/search" in c.get("url", "")]
        search_responses = [c for c in captured if c.get("kind") == "response" and "/search/search" in c.get("url", "")]
        logger.info("Search requests: %d  responses: %d", len(search_requests), len(search_responses))

        for i, r in enumerate(search_requests):
            (RECON_DIR / f"search_request_{i}.json").write_text(
                json.dumps(r, indent=2), encoding="utf-8"
            )
        for i, r in enumerate(search_responses):
            (RECON_DIR / f"search_response_{i}.json").write_text(
                r.get("body", ""), encoding="utf-8"
            )

        await browser.close()
        logger.info("Done. Artifacts: %s", RECON_DIR)


if __name__ == "__main__":
    asyncio.run(main())
