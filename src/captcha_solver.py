"""State-agnostic 2Captcha reCAPTCHA v2 solver for public-notice detail pages.

Promoted from the archived TN scraper (``src/_legacy_tn/captcha_solver.py``)
so active code paths don't import from ``_legacy_tn/``. The only behavioural
change is that the reCAPTCHA **sitekey is detected dynamically** from the page
(``data-sitekey`` attribute or the ``k=`` param of the reCAPTCHA iframe) instead
of being hardcoded — so the same solver works for any state's instance of the
shared Smart Search / usalegalnotice.com ASP.NET platform.

Flow (mirrors the legacy TN flow):
  1. Bail if the IP is blocked or content is already visible.
  2. Detect the sitekey on the page.
  3. Send websiteURL + sitekey to 2Captcha; get a g-recaptcha-response token.
  4. Inject the token + fire the reCAPTCHA callback.
  5. Click the "View Notice" submit button.
  6. Verify the full notice content is now visible.
"""

from __future__ import annotations

import asyncio
import logging

from playwright.async_api import Page, TimeoutError as PwTimeout
from twocaptcha import TwoCaptcha

from config import MAX_RETRIES

logger = logging.getLogger(__name__)

# Marker text the platform renders once the gate is cleared and the full
# notice body is shown. Same string on the TN twin; override per-site if needed.
DEFAULT_CONTENT_MARKER = "Notice Content"
# Default "reveal the notice" submit button — id suffix is stable across the
# platform's per-state installs (PublicNoticeDetailsBody1_btnViewNotice).
DEFAULT_VIEW_NOTICE_SELECTOR = "input[id$='btnViewNotice'], input[id$='_btnViewNotice']"
# Platform message shown when an IP/region is not allowed to view notices.
_IP_BLOCK_MARKER = "You are not permitted to view public notices"


async def detect_sitekey(page: Page) -> str:
    """Return the reCAPTCHA v2 sitekey on the current page, or '' if none.

    Looks at (in order): a ``[data-sitekey]`` element, then the ``k=`` query
    param of any ``recaptcha`` iframe src. Empty string means no captcha is
    present (caller should treat the detail as ungated).
    """
    try:
        return await page.evaluate(
            """() => {
                const el = document.querySelector('[data-sitekey]');
                if (el) return el.getAttribute('data-sitekey') || '';
                const ifr = document.querySelector("iframe[src*='recaptcha']");
                if (ifr) {
                    const m = (ifr.getAttribute('src') || '').match(/[?&]k=([^&]+)/);
                    if (m) return decodeURIComponent(m[1]);
                }
                return '';
            }"""
        ) or ""
    except Exception:
        return ""


async def solve_captcha_and_view(
    page: Page,
    api_key: str,
    *,
    view_button_selector: str = DEFAULT_VIEW_NOTICE_SELECTOR,
    content_marker: str = DEFAULT_CONTENT_MARKER,
) -> bool:
    """Solve reCAPTCHA v2 (if present) and reveal the full notice text.

    Returns True if the notice content is visible afterwards (including the
    no-captcha-needed case), False if the gate could not be cleared.
    Retries up to ``MAX_RETRIES`` times.
    """
    if not api_key:
        logger.error("CAPTCHA_API_KEY not set — cannot solve CAPTCHA")
        return False

    page_url = page.url

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            # Bail immediately on an IP block — retrying just burns solves.
            if await page.query_selector(f"text='{_IP_BLOCK_MARKER}'"):
                logger.error(
                    "IP BLOCKED: site says 'not permitted to view' — "
                    "need a residential proxy or different IP"
                )
                return False

            # Already revealed? (captcha previously solved this session, or the
            # detail page isn't gated at all.)
            if await page.query_selector(f"text='{content_marker}'"):
                logger.debug("Notice content already visible — no CAPTCHA needed")
                return True

            sitekey = await detect_sitekey(page)
            if not sitekey:
                # No captcha on the page but content marker not found either —
                # the body may render under a different marker; treat the
                # presence of a populated detail as success at the call site.
                logger.debug("No reCAPTCHA sitekey detected on detail page")
                return True

            # Confirm the submit button is present before spending a solve.
            try:
                await page.wait_for_selector(view_button_selector, timeout=15000)
            except PwTimeout:
                logger.warning(
                    "View Notice button not found within 15s on %s (attempt %d/%d)",
                    page_url, attempt, MAX_RETRIES,
                )
                continue

            logger.warning(
                "Solving reCAPTCHA (sitekey=%s…) for %s (attempt %d/%d)",
                sitekey[:12], page_url, attempt, MAX_RETRIES,
            )
            solver = TwoCaptcha(api_key)
            # 2Captcha's solver is blocking; run it off the event loop so we
            # don't stall Playwright's async machinery.
            result = await asyncio.to_thread(
                solver.recaptcha, sitekey=sitekey, url=page_url
            )
            token = result.get("code") if isinstance(result, dict) else str(result)
            if not token:
                logger.warning("2Captcha returned empty token (attempt %d)", attempt)
                continue

            await _inject_token(page, token)
            await asyncio.sleep(1)

            view_btn = await page.query_selector(view_button_selector)
            if not view_btn:
                # Callback may have auto-submitted.
                if await page.query_selector(f"text='{content_marker}'"):
                    logger.warning("CAPTCHA solved — callback auto-submitted form")
                    return True
                logger.warning(
                    "View Notice button gone after token inject (attempt %d)", attempt
                )
                continue

            await view_btn.click()
            await page.wait_for_load_state("domcontentloaded")

            if await page.query_selector(f"text='{content_marker}'"):
                logger.warning("CAPTCHA solved — notice text visible")
                return True
            if not await page.query_selector("text='You must complete the reCAPTCHA'"):
                logger.warning("CAPTCHA solved — gate cleared")
                return True

            logger.warning("CAPTCHA still present after attempt %d", attempt)

        except Exception:
            logger.exception("CAPTCHA solve error (attempt %d/%d)", attempt, MAX_RETRIES)

    logger.error("All %d CAPTCHA attempts failed for %s", MAX_RETRIES, page_url)
    return False


async def _inject_token(page: Page, token: str) -> None:
    """Inject the solved token into the hidden response field + fire callbacks."""
    await page.evaluate(
        """(token) => {
            const el = document.getElementById('g-recaptcha-response');
            if (el) { el.value = token; el.style.display = 'block'; }
            const ta = document.querySelector('textarea[name="g-recaptcha-response"]');
            if (ta) { ta.value = token; ta.style.display = 'block'; }
            if (typeof ___grecaptcha_cfg !== 'undefined') {
                const clients = ___grecaptcha_cfg.clients;
                if (clients) {
                    Object.keys(clients).forEach(key => {
                        const client = clients[key];
                        const findCallback = (obj) => {
                            if (!obj || typeof obj !== 'object') return;
                            Object.values(obj).forEach(v => {
                                if (typeof v === 'object' && v !== null) {
                                    if (typeof v.callback === 'function') v.callback(token);
                                    findCallback(v);
                                }
                            });
                        };
                        findCallback(client);
                    });
                }
            }
        }""",
        token,
    )
