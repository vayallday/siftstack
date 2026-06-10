"""DataSift.ai shared automation primitives — login, cookies, UI helpers.

Self-contained module for use in both the SiftStack pipeline and
distributed .skill packages. Only requires playwright + python-dotenv.

When used inside SiftStack (src/), it loads credentials from config.py.
When used standalone in a skill ZIP, it loads from .env or environment vars.
"""

__version__ = "1.0.0"

import json
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

# ── URLs ──────────────────────────────────────────────────────────────
DATASIFT_LOGIN_URL = "https://app.reisift.io/login"
DATASIFT_DASHBOARD_URL = "https://app.reisift.io/dashboard/general"
DATASIFT_RECORDS_URL = "https://app.reisift.io/records/properties"
DATASIFT_SIFTMAP_URL = "https://app.reisift.io/siftmap"
DATASIFT_MARKET_FINDER_URL = "https://app.reisift.io/market-finder"

# ── Browser Defaults ──────────────────────────────────────────────────
DEFAULT_VIEWPORT = {"width": 1440, "height": 900}
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


# ── Capability Detection ─────────────────────────────────────────────

def has_playwright() -> bool:
    """Check if Playwright is available in this environment."""
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def detect_context() -> str:
    """Detect execution context: 'claude_code', 'co_work', or 'standalone'."""
    if os.getenv("CLAUDE_CODE"):
        return "claude_code"
    if not has_playwright():
        return "co_work"
    return "standalone"


# ── Credentials ───────────────────────────────────────────────────────

def get_credentials() -> tuple[str, str]:
    """Get DataSift email and password from environment or .env file.

    Returns (email, password). Raises ValueError if not found.
    """
    # Try loading from .env (works both in SiftStack and standalone)
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # dotenv not required if env vars are set directly

    email = os.getenv("DATASIFT_EMAIL", "")
    password = os.getenv("DATASIFT_PASSWORD", "")

    if not email or not password:
        raise ValueError(
            "DATASIFT_EMAIL and DATASIFT_PASSWORD must be set in .env or environment"
        )
    return email, password


# ── Cookie / State Persistence ────────────────────────────────────────

def save_state(path: Path, data) -> None:
    """Write JSON state to disk with .bak backup."""
    if path.exists():
        try:
            bak = path.with_suffix(path.suffix + ".bak")
            bak.write_bytes(path.read_bytes())
        except OSError:
            pass
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def load_state(path: Path):
    """Load JSON state from disk, falling back to .bak if corrupt."""
    for candidate in [path, path.with_suffix(path.suffix + ".bak")]:
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as e:
                logger.warning("Failed to read %s: %s", candidate, e)
    return {}


# ── Cookie Management ─────────────────────────────────────────────────

COOKIES_FILE = Path("datasift_cookies.json")


async def save_cookies(page) -> None:
    """Save browser cookies for session reuse."""
    cookies = await page.context.cookies()
    save_state(COOKIES_FILE, cookies)
    logger.debug("Saved %d DataSift cookies", len(cookies))


async def load_cookies(context) -> bool:
    """Load saved cookies into browser context. Returns True if loaded."""
    cookies = load_state(COOKIES_FILE)
    if not cookies:
        return False
    try:
        await context.add_cookies(cookies)
        logger.debug("Loaded %d DataSift cookies", len(cookies))
        return True
    except Exception as e:
        logger.debug("Failed to load cookies: %s", e)
        return False


# ── Authentication ────────────────────────────────────────────────────

async def login(page, email: str = None, password: str = None) -> bool:
    """Log in to DataSift.ai (app.reisift.io). Returns True on success.

    Tries saved cookies first, falls back to fresh login.
    If email/password not provided, loads from environment.
    """
    from playwright.async_api import TimeoutError as PwTimeout

    if not email or not password:
        email, password = get_credentials()

    # Try cookies first
    has_cookies = await load_cookies(page.context)
    if has_cookies:
        await page.goto(DATASIFT_RECORDS_URL, wait_until="domcontentloaded")
        await page.wait_for_timeout(5000)
        current_url = page.url
        if "/login" not in current_url and ("/dashboard" in current_url or "/records" in current_url):
            logger.info("DataSift session restored from cookies")
            return True
        logger.info("DataSift cookies expired (url=%s), doing fresh login", current_url)

    # Fresh login
    await page.goto(DATASIFT_LOGIN_URL, wait_until="domcontentloaded")

    # Fill credentials
    await page.get_by_role("textbox", name="Email").fill(email)
    await page.get_by_role("textbox", name="Password").fill(password)

    # Hidden checkboxes — click labels, not inputs
    remember_label = page.locator('label:has-text("Remember me")')
    if await remember_label.count() > 0:
        await remember_label.first.click()

    terms_label = page.locator('label:has-text("I\'ve read and agree")')
    if await terms_label.count() > 0:
        await terms_label.first.click()

    # Click Sign In. If the button is still disabled (because email/password
    # weren't filled or terms checkbox didn't latch), this click is a no-op
    # and we'd time out below — which is the correct failure path.
    await page.get_by_role("button", name="Sign In").click()

    # Wait for navigation away from login page. The earlier version of this
    # function had a false-positive: if wait_for_url timed out AND the URL
    # didn't happen to contain "/login" at that exact moment (transient
    # state during a slow redirect, for example), it would fall through to
    # save_cookies + return True. That masked a credential-misconfig bug on
    # 2026-05-23.
    #
    # 2026-05-26: an Apify run logged "Final URL: https://app.reisift.io/"
    # (bare root, NOT /login) → click DID navigate but didn't reach
    # /dashboard/general. DataSift has multiple post-login landing routes
    # (/, /dashboard, /dashboard/general, /records/*) depending on account
    # state + role + recent activity. Match any of them — re-validate by
    # negative gate (still seeing the password input means not logged in).
    POST_LOGIN_PATTERNS = (
        "**/dashboard/general**",
        "**/dashboard**",
        "**/records/**",
        "**/siftmap**",
        # The bare root is a valid landing for some accounts post-login —
        # confirm by negative gate (password input absent) before trusting it.
        "https://app.reisift.io/",
    )

    async def _await_post_login() -> str | None:
        # Returns the URL we landed on, or None if all patterns timed out.
        for pat in POST_LOGIN_PATTERNS:
            try:
                await page.wait_for_url(pat, timeout=5000)
                return page.url
            except PwTimeout:
                continue
        return None

    landed_url = await _await_post_login()
    if landed_url is None:
        logger.error(
            "DataSift login failed — no post-login URL matched within 25s. "
            "Final URL: %s. Check credentials in .env (DATASIFT_EMAIL / "
            "DATASIFT_PASSWORD) and the Sign In button state (disabled means "
            "the form's email / password / terms checkbox didn't latch).",
            page.url,
        )
        await screenshot(page, "login_failed_post_signin")
        await _upload_login_diagnostic(page, "no_post_login_url")
        return False

    # Negative gate — if the password field is still visible, we're still on
    # the form (Sign In was a no-op or session got rejected).
    try:
        pw_visible = await page.locator('input[type="password"]').first.is_visible(timeout=1000)
    except Exception:
        pw_visible = False
    if pw_visible:
        logger.error(
            "DataSift login failed — landed on %s but password field is still "
            "visible. Likely a credential or terms-checkbox issue.",
            landed_url,
        )
        await screenshot(page, "login_failed_pw_still_visible")
        await _upload_login_diagnostic(page, "pw_still_visible")
        return False

    logger.info("DataSift login successful — landed on %s", landed_url)
    await save_cookies(page)
    return True


async def _upload_login_diagnostic(page, tag: str) -> None:
    """When running on Apify, push the login-failure screenshot + HTML to KVS
    so we can debug post-mortem (the local screenshot file is ephemeral on
    the Actor container)."""
    import os as _os
    if not (_os.environ.get("APIFY_IS_AT_HOME") or _os.environ.get("APIFY_TOKEN")):
        return
    from datetime import datetime as _dt
    stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
    key_base = f"datasift_login_{tag}_{stamp}"
    try:
        png_bytes = await page.screenshot(full_page=True)
        html = await page.content()
        from apify import Actor
        kvs = await Actor.open_key_value_store()
        await kvs.set_value(key_base, png_bytes, content_type="image/png")
        await kvs.set_value(f"{key_base}_html", html.encode("utf-8"),
                            content_type="text/html")
        logger.error("DataSift login diagnostic uploaded to KVS: %s + _html", key_base)
    except Exception as e:
        logger.debug("Failed to upload login diagnostic: %s", e)


# ── UI Primitives ─────────────────────────────────────────────────────

async def screenshot(page, name: str) -> None:
    """Take a debug screenshot.

    Local: saves to the working directory as `datasift_{name}.png`.

    Apify: ALSO uploads to the run's default KVS as
    `datasift_screenshot_{name}_{YYYYMMDD_HHMMSS}.png` so post-mortem
    debugging is possible after the container is destroyed. Without this,
    every Playwright bug (wizard column-mapping landing on wrong elements,
    login form changes, etc.) had to be guessed at since no diagnostic
    artifact survived the run.

    Failures here are NEVER fatal — diagnostic capture must not break the
    main upload flow.
    """
    import os as _os
    local_path = f"datasift_{name}.png"
    try:
        await page.screenshot(path=local_path)
        logger.debug("Screenshot: %s", local_path)
    except Exception as e:
        logger.debug("Screenshot failed (%s): %s", name, e)
        return

    # On Apify, push to KVS too so we get post-mortem artifacts.
    if not (_os.environ.get("APIFY_IS_AT_HOME") or _os.environ.get("APIFY_TOKEN")):
        return
    try:
        from datetime import datetime as _dt
        from apify import Actor
        stamp = _dt.now().strftime("%Y%m%d_%H%M%S")
        key = f"datasift_screenshot_{name}_{stamp}"
        kvs = await Actor.open_key_value_store()
        with open(local_path, "rb") as f:
            await kvs.set_value(key, f.read(), content_type="image/png")
        logger.debug("Screenshot uploaded to KVS as %s", key)
    except Exception as e:
        # Don't even log at warning — diagnostic save is best-effort.
        logger.debug("Screenshot KVS upload failed (%s): %s", name, e)


async def dismiss_popups(page) -> None:
    """Dismiss notification popups and remove Beamer NPS overlay.

    The Beamer NPS survey iframe (#npsIframeContainer) blocks ALL pointer
    events globally — it MUST be removed before any click interactions.
    """
    try:
        # Try clicking dismiss text elements first
        for text in ["NO, THANKS", "No, thanks", "No Thanks", "NO THANKS", "Not Now", "Dismiss"]:
            el = page.get_by_text(text, exact=True)
            if await el.count() > 0:
                await el.first.click(force=True)
                await page.wait_for_timeout(1000)
                logger.debug("Dismissed popup via '%s'", text)
                return

        # JavaScript fallback: remove popup elements from DOM
        removed = await page.evaluate("""() => {
            let removed = 0;
            // Remove Beamer NPS survey iframe (blocks pointer events globally)
            const nps = document.getElementById('npsIframeContainer');
            if (nps) { nps.remove(); removed++; }
            // Also remove by class
            document.querySelectorAll('[class*="nps-iframe"], [class*="beamer"]').forEach(
                el => { el.remove(); removed++; }
            );
            // Look for the notification popup overlay
            const els = document.querySelectorAll(
                '[class*="notification"], [class*="Notification"], '
                + '[class*="popup"], [class*="Popup"]'
            );
            for (const el of els) {
                if (el.textContent && el.textContent.includes('notifications')) {
                    el.remove();
                    removed++;
                }
            }
            // Also try removing any fixed/absolute overlays
            const overlays = document.querySelectorAll(
                '[style*="position: fixed"], [style*="position:fixed"]'
            );
            for (const o of overlays) {
                if (o.textContent && o.textContent.includes('notifications')) {
                    o.remove();
                    removed++;
                }
            }
            return removed;
        }""")
        if removed:
            logger.debug("Removed %d popup elements via JS", removed)
            await page.wait_for_timeout(500)
    except Exception as e:
        logger.debug("Popup dismissal failed: %s", e)


async def scroll_into_view(page, element) -> None:
    """Scroll an element into view using JS (Playwright scroll fails on DataSift panels).

    DataSift filter panels are scrollable <div>s, NOT the viewport.
    Playwright's scroll_into_view_if_needed() does nothing for these.
    """
    await page.evaluate(
        "el => el.scrollIntoView({behavior: 'instant', block: 'center'})",
        element,
    )
    await page.wait_for_timeout(300)


async def click_styled_dropdown(page, container_selector: str, option_text: str) -> bool:
    """Click a styled-components dropdown and select an option by text.

    DataSift has NO native <select> elements — all dropdowns are
    [class*="Selectstyles__Select"] containers with custom option elements.

    Args:
        page: Playwright page
        container_selector: CSS selector for the dropdown container
        option_text: Text of the option to select

    Returns:
        True if option was selected, False otherwise
    """
    try:
        # Click the dropdown to open it
        dropdown = page.locator(container_selector).first
        await dropdown.click()
        await page.wait_for_timeout(500)

        # Find and click the option
        option = page.locator(f'[class*="SelectOption"]:has-text("{option_text}")').first
        await option.wait_for(state="visible", timeout=5000)
        await option.click()
        await page.wait_for_timeout(500)
        return True
    except Exception as e:
        logger.warning("Failed to select '%s' from dropdown: %s", option_text, e)
        return False


async def wait_for_spa(page, ms: int = 5000) -> None:
    """Wait for DataSift SPA to settle after navigation.

    Use wait_until='domcontentloaded' (NOT 'networkidle') because
    the SPA keeps WebSocket connections open permanently.
    """
    await page.wait_for_timeout(ms)


async def extract_table_data(page, table_selector: str = "table") -> list[list[str]]:
    """Extract all rows from a table or table-like element via JS.

    Handles both standard <table> elements and styled-components tables.
    Returns a list of rows, where each row is a list of cell text values.
    """
    data = await page.evaluate(f"""() => {{
        const rows = [];
        const table = document.querySelector('{table_selector}') ||
                      document.querySelector('[class*="Table"]') ||
                      document.querySelector('[role="table"]');
        if (!table) return rows;

        // Try standard table rows first
        let trs = table.querySelectorAll('tr');
        if (trs.length > 0) {{
            for (const tr of trs) {{
                const cells = tr.querySelectorAll('td, th');
                const row = Array.from(cells).map(c => c.innerText.trim());
                if (row.length > 0) rows.push(row);
            }}
            return rows;
        }}

        // Fallback: div-based table (styled-components)
        const divRows = table.querySelectorAll('[class*="Row"], [class*="row"]');
        for (const dr of divRows) {{
            const cells = dr.querySelectorAll('[class*="Cell"], [class*="cell"], div > span');
            const row = Array.from(cells).map(c => c.innerText.trim());
            if (row.length > 0) rows.push(row);
        }}
        return rows;
    }}""")
    return data


async def scroll_and_extract_all(page, table_selector: str = "table",
                                  scroll_container: str = None,
                                  max_scrolls: int = 50) -> list[list[str]]:
    """Scroll through a lazy-loaded table and extract all rows.

    Handles DataSift's infinite scroll / lazy loading by scrolling the
    container, waiting for new rows, and re-extracting until stable.

    Args:
        page: Playwright page
        table_selector: CSS selector for the table element
        scroll_container: CSS selector for the scrollable container (if different from table)
        max_scrolls: Maximum number of scroll attempts

    Returns:
        All unique rows from the table
    """
    all_rows = []
    seen_keys = set()
    prev_count = 0

    for i in range(max_scrolls):
        data = await extract_table_data(page, table_selector)

        for row in data:
            key = "|".join(row)
            if key not in seen_keys:
                seen_keys.add(key)
                all_rows.append(row)

        if len(all_rows) == prev_count and i > 0:
            logger.debug("No new rows after scroll %d (total: %d)", i, len(all_rows))
            break

        prev_count = len(all_rows)

        # Scroll the container down
        container = scroll_container or table_selector
        await page.evaluate(f"""() => {{
            const el = document.querySelector('{container}') ||
                       document.querySelector('[class*="Table"]') ||
                       document.querySelector('[role="table"]');
            if (el) el.scrollTop = el.scrollHeight;
        }}""")
        await page.wait_for_timeout(1500)

    logger.info("Extracted %d total rows from table", len(all_rows))
    return all_rows


# ── Browser Lifecycle ─────────────────────────────────────────────────

@asynccontextmanager
async def create_browser(headless: bool = False, viewport: dict = None):
    """Create a Playwright browser context. Yields (browser, context, page).

    Usage:
        async with create_browser(headless=False) as (browser, context, page):
            await login(page)
            # ... do work ...
    """
    from playwright.async_api import async_playwright

    vp = viewport or DEFAULT_VIEWPORT

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport=vp,
            user_agent=DEFAULT_USER_AGENT,
        )
        page = await context.new_page()
        try:
            yield browser, context, page
        finally:
            await browser.close()
