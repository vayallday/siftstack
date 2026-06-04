"""Recon part 2: run a real search, map results grid + detail page + locality filtering.

Questions to answer:
  1. Is there any county/city/publication filter (hidden, or revealed by a
     Popular Search postback)? Grep the page HTML for county/publication/city.
  2. After a keyword+date search, what does the results grid look like
     (selectors, columns, does it show locality/publication/date)?
  3. What does a notice detail page contain (full text, locality, owner)?
  4. Does viewing full notice text require login/captcha?

Run: python test_va_public_notice_recon2.py
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path

from playwright.async_api import async_playwright

OUT = Path("output/va_pn_recon")
OUT.mkdir(parents=True, exist_ok=True)

SEARCH = "https://www.publicnoticevirginia.com/Search.aspx"

SEL_KEYWORD = "#ctl00_ContentPlaceHolder1_as1_txtSearch"
SEL_POPULAR = "#ctl00_ContentPlaceHolder1_as1_ddlPopularSearches"
SEL_LASTDAYS_RADIO = "#ctl00_ContentPlaceHolder1_as1_rbLastNumDays"
SEL_LASTDAYS_TXT = "#ctl00_ContentPlaceHolder1_as1_txtLastNumDays"
SEL_GO = "#ctl00_ContentPlaceHolder1_as1_btnGo"


async def main() -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
            viewport={"width": 1400, "height": 1000},
            accept_downloads=True,
        )
        page = await ctx.new_page()
        page.set_default_timeout(45000)

        await page.goto(SEARCH, wait_until="domcontentloaded")
        await page.wait_for_timeout(2500)

        # (1) Grep raw HTML for any locality/publication filter controls.
        html = await page.content()
        for term in ("county", "publication", "city", "locality", "ddlState", "ddlCounty", "ddlCity", "ddlPub"):
            hits = len(re.findall(term, html, re.I))
            print(f"  HTML mentions {term!r}: {hits}")
        # Dump any <select>/<input> whose id/name hints at locality, even if hidden.
        locality_ctrls = await page.evaluate(
            """() => {
              const out = [];
              for (const el of document.querySelectorAll('select, input')) {
                const key = (el.id + ' ' + el.name).toLowerCase();
                if (/county|city|public|locality|state|town|paper/.test(key)) {
                  out.push({tag: el.tagName, id: el.id, name: el.name, type: el.type,
                            options: el.options ? [...el.options].slice(0,80).map(o=>o.textContent.trim()) : null});
                }
              }
              return out;
            }"""
        )
        (OUT / "03_locality_controls.json").write_text(json.dumps(locality_ctrls, indent=2))
        print(f"  locality-ish controls: {len(locality_ctrls)}")
        for c in locality_ctrls:
            print(f"    {c['tag']} id={c['id']!r} name={c['name']!r} "
                  f"opts={c['options'][:12] if c['options'] else None}")

        # (2) Run a search: Foreclosures preset + last 30 days.
        print("\n→ Selecting 'Foreclosures' popular search")
        try:
            await page.select_option(SEL_POPULAR, label="Foreclosures")
            await page.wait_for_timeout(2500)  # may postback
        except Exception as e:
            print(f"  popular select failed ({e}); typing keyword instead")
            await page.fill(SEL_KEYWORD, "foreclosure")

        kw = await page.input_value(SEL_KEYWORD)
        print(f"  keyword box now: {kw!r}")

        # Set date range = last 30 days
        try:
            await page.check(SEL_LASTDAYS_RADIO)
            await page.fill(SEL_LASTDAYS_TXT, "30")
        except Exception as e:
            print(f"  date set failed: {e}")

        await page.screenshot(path=str(OUT / "03_before_go.png"), full_page=True)
        print("→ Clicking Go")
        try:
            async with page.expect_navigation(wait_until="domcontentloaded", timeout=45000):
                await page.click(SEL_GO)
        except Exception as e:
            print(f"  nav after Go: {e} (may be same-page update)")
        await page.wait_for_timeout(4000)
        print(f"  results url: {page.url}")
        await page.screenshot(path=str(OUT / "04_results.png"), full_page=True)

        # (3) Map results structure. Look for result rows / view links / pagination.
        results = await page.evaluate(
            """() => {
              const txt = document.body.innerText;
              const out = {resultCountText: '', viewLinks: [], tables: [], rowSamples: [], pager: []};
              const m = txt.match(/([\\d,]+)\\s+(results?|notices?|records?|matches)/i)
                     || txt.match(/(results?|showing)[^\\n]{0,40}/i);
              out.resultCountText = m ? m[0] : '';
              // anchors/buttons that open a notice
              for (const a of document.querySelectorAll('a, input[type=submit], input[type=button]')) {
                const t = (a.textContent || a.value || '').trim();
                if (/view|detail|read/i.test(t) || /detail|notice/i.test(a.href||'')) {
                  out.viewLinks.push({text: t.slice(0,40), id: a.id, name: a.name, href: a.href||''});
                }
              }
              out.viewLinks = out.viewLinks.slice(0, 15);
              // tables and a couple sample rows
              for (const tb of document.querySelectorAll('table')) {
                if (tb.id || (tb.className||'')) out.tables.push({id: tb.id, cls: tb.className, rows: tb.rows.length});
              }
              // grab repeating result containers
              const conts = document.querySelectorAll('[id*=Results] tr, [class*=result] , [id*=Grid] tr');
              let n = 0;
              for (const c of conts) {
                const t = (c.innerText||'').trim().replace(/\\s+/g,' ');
                if (t.length > 20 && n < 6) { out.rowSamples.push(t.slice(0,300)); n++; }
              }
              // pagination
              for (const a of document.querySelectorAll('a, input')) {
                const t=(a.textContent||a.value||'').trim();
                if (/^(next|prev|previous|\\d+|>>|<<|»|«)$/i.test(t) && t.length<6) out.pager.push({t, id:a.id, name:a.name});
              }
              out.pager = out.pager.slice(0,12);
              return out;
            }"""
        )
        (OUT / "04_results_structure.json").write_text(json.dumps(results, indent=2))
        print(f"  resultCountText: {results['resultCountText']!r}")
        print(f"  viewLinks ({len(results['viewLinks'])}):")
        for v in results["viewLinks"][:10]:
            print(f"    {v['text']!r} id={v['id']!r} href={v['href'][:80]!r}")
        print(f"  tables: {results['tables']}")
        print(f"  row samples:")
        for rs in results["rowSamples"]:
            print(f"    | {rs[:200]}")
        print(f"  pager: {results['pager']}")

        # (4) Try opening the first notice detail.
        detail_clicked = False
        for v in results["viewLinks"]:
            if v["href"] and ("detail" in v["href"].lower() or "notice" in v["href"].lower()):
                print(f"\n→ Opening detail: {v['href'][:90]}")
                try:
                    await page.goto(v["href"], wait_until="domcontentloaded")
                    detail_clicked = True
                    break
                except Exception as e:
                    print(f"  goto failed: {e}")
        if not detail_clicked and results["viewLinks"]:
            # try clicking a postback view link by id
            for v in results["viewLinks"]:
                if v["id"]:
                    try:
                        print(f"→ Clicking view by id {v['id']!r}")
                        await page.click("#" + v["id"])
                        await page.wait_for_timeout(3000)
                        detail_clicked = True
                        break
                    except Exception as e:
                        print(f"  click failed: {e}")

        if detail_clicked:
            await page.wait_for_timeout(2500)
            print(f"  detail url: {page.url}")
            await page.screenshot(path=str(OUT / "05_detail.png"), full_page=True)
            body = await page.evaluate("() => document.body.innerText")
            (OUT / "05_detail_text.txt").write_text(body)
            print(f"  detail page text length: {len(body)}")
            print("  --- first 1200 chars ---")
            print(body[:1200])
            # captcha check
            has_captcha = await page.query_selector("iframe[src*=recaptcha], .g-recaptcha")
            print(f"  recaptcha present on detail: {bool(has_captcha)}")

        await browser.close()
        print(f"\nArtifacts in {OUT.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
