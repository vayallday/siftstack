"""Recon: map the publicnoticevirginia.com public search form DOM.

Throwaway reconnaissance script (mirrors test_chesterfield_aca_recon.py style).
Goal: discover the search page URL, all form controls (selects + their option
values, text inputs, radio buttons), the results grid structure, and the
detail/full-text view — so the puller can be built against real selectors.

Run: python test_va_public_notice_recon.py
Outputs screenshots + a JSON form dump under output/va_pn_recon/.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path("output/va_pn_recon")
OUT.mkdir(parents=True, exist_ok=True)

HOME = "https://www.publicnoticevirginia.com/"


async def dump_form_controls(page) -> dict:
    """Extract every select (with options), text input, and radio on the page."""
    return await page.evaluate(
        """() => {
        const result = {selects: [], texts: [], radios: [], buttons: [], links: []};
        for (const s of document.querySelectorAll('select')) {
            result.selects.push({
                id: s.id, name: s.name,
                options: [...s.options].slice(0, 60).map(o => ({value: o.value, text: o.textContent.trim()})),
                optionCount: s.options.length,
            });
        }
        for (const i of document.querySelectorAll('input[type=text], input:not([type])')) {
            result.texts.push({id: i.id, name: i.name, placeholder: i.placeholder});
        }
        for (const r of document.querySelectorAll('input[type=radio]')) {
            result.radios.push({id: r.id, name: r.name, value: r.value});
        }
        for (const b of document.querySelectorAll('input[type=submit], input[type=button], button')) {
            result.buttons.push({id: b.id, name: b.name, value: b.value, text: (b.textContent||'').trim()});
        }
        for (const a of document.querySelectorAll('a')) {
            const t = (a.textContent||'').trim();
            if (/search|notice|foreclos|estate|legal|advanced/i.test(t)) {
                result.links.push({text: t.slice(0,60), href: a.href});
            }
        }
        return result;
    }"""
    )


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 1000},
        )
        page = await ctx.new_page()

        print(f"→ Loading {HOME}")
        await page.goto(HOME, wait_until="domcontentloaded")
        await page.wait_for_timeout(3000)
        print(f"  landed: {page.url}  title={await page.title()!r}")
        await page.screenshot(path=str(OUT / "01_home.png"), full_page=True)

        home_dump = await dump_form_controls(page)
        (OUT / "01_home_controls.json").write_text(json.dumps(home_dump, indent=2))
        print(f"  home: {len(home_dump['selects'])} selects, "
              f"{len(home_dump['texts'])} text inputs, "
              f"{len(home_dump['links'])} notice-ish links")
        for ln in home_dump["links"][:25]:
            print(f"    link: {ln['text']!r} → {ln['href']}")

        # Try the canonical Search.aspx the WebFetch reported.
        for candidate in ("Search.aspx", "search.aspx"):
            url = HOME + candidate
            print(f"→ Trying {url}")
            try:
                resp = await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await page.wait_for_timeout(3000)
                print(f"  status={resp.status if resp else '??'}  landed: {page.url}")
                if resp and resp.status < 400:
                    await page.screenshot(path=str(OUT / "02_search.png"), full_page=True)
                    dump = await dump_form_controls(page)
                    (OUT / "02_search_controls.json").write_text(json.dumps(dump, indent=2))
                    print(f"  search page: {len(dump['selects'])} selects")
                    for s in dump["selects"]:
                        print(f"    SELECT id={s['id']!r} name={s['name']!r} "
                              f"({s['optionCount']} opts) sample={[o['text'] for o in s['options'][:6]]}")
                    for t in dump["texts"]:
                        print(f"    TEXT id={t['id']!r} name={t['name']!r} ph={t['placeholder']!r}")
                    for r in dump["radios"]:
                        print(f"    RADIO id={r['id']!r} name={r['name']!r} val={r['value']!r}")
                    for b in dump["buttons"]:
                        print(f"    BTN id={b['id']!r} name={b['name']!r} val={b['value']!r} txt={b['text']!r}")
                    break
            except Exception as e:
                print(f"  failed: {e}")

        await browser.close()
        print(f"\nArtifacts in {OUT.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
