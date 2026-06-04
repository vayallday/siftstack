# CLAUDE.md — SiftStack

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

**SiftStack** — Full-stack real estate investing operations platform built around DataSift.ai CRM. Covers the entire REI business lifecycle:

1. **Data Acquisition:** PropertyRadar list pulls (foreclosures, pre-probate) via Playwright against app.propertyradar.com, scanned PDF import, courthouse terminal photo import (probate, eviction, code violations, divorce), Dropbox auto-polling
2. **Enrichment Pipeline:** 10+ steps — Smarty address standardization, Zillow property data, obituary/heir research, Ancestry.com SSDI, Tracerfy skip trace, Trestle phone scoring, entity research
3. **Deal Analysis:** Comparable sales (Two-Bucket ARV), rehab estimation (4-tier room-by-room), deal analyzer (MAO/ROI/financing scenarios)
4. **Market Intelligence:** Zip code scoring, Market Finder reports, cash buyer list building, investor portfolio analysis
5. **CRM Automation:** DataSift upload, 26 TCA sequence templates, 12 niche sequential marketing presets, filter preset management, SiftMap sold property tagging
6. **Lead Management:** 4 Pillars of Motivation auto-qualification, STABM daily routine, pipeline reporting, deep prospecting (4-level framework)
7. **Operations:** Acquisition playbook generator (SOPs, scripts, checklists), Slack/Discord notifications, Google Drive upload, Apify Actor deployment

Currently focused on Virginia (Richmond, Henrico, Chesterfield, Prince William) and Maryland (Prince George's, Montgomery) markets via PropertyRadar.

8. **REI Skill Library:** 13 Claude Co-Work skill files (`.skill`/`.plugin` ZIPs) for distribution to DataSift community via [learn.datasift.ai/claude-skills-rei](https://learn.datasift.ai/claude-skills-rei). Skills teach Claude specific REI workflows when uploaded to Co-Work sessions or Projects.

### Archived: Tennessee public-notice acquisition

The original data source was `tnpublicnotice.com` for Knox + Blount, TN.
Those acquisition files (the Playwright scraper, the 2Captcha integration,
the TN trustee-sale filter, the Knox County tax API client, the Knox
KGIS + Blount TPAD property-lookup helpers, and the TN-specific config)
moved to [src/_legacy_tn/](src/_legacy_tn/) when the focus shifted to
VA/MD. Nothing under that folder is imported by any active code path —
it's preserved as documentation and as a starting point for any future
state that needs a similar ASP.NET / reCAPTCHA-gated scrape pipeline.
See [src/_legacy_tn/README.md](src/_legacy_tn/README.md).

## Commands

```bash
# Setup
pip install -r requirements.txt
playwright install chromium
cp .env.example .env  # then fill in credentials

# Run — PropertyRadar pulls (the only acquisition source)
python src/main.py daily                          # pull deltas across all configured PR lists
python src/main.py historical                     # same flow (PR has no Added-Date filter; delta is membership-diff)
python src/main.py daily --split                  # separate CSV per list
python src/main.py daily -v                       # verbose/debug logging

# Richmond Vacant Building List — vacancy registry feed (notice_type=vacant_building, NOT code_violation)
python src/main.py richmond-vacant                # probe rva.gov for newest PDF, diff vs state, enrich, CSV

# Chesterfield ACA bulk Code Violation report (anonymous, public Accela Citizen Access)
python src/main.py chesterfield-code-violation                                  # delta since last pull (90-day window first time)
python src/main.py chesterfield-code-violation --aca-start 2026-05-01           # custom window start
python src/main.py chesterfield-code-violation --aca-start 2026-05-01 --aca-end 2026-05-25
python src/main.py chesterfield-code-violation --aca-headed                     # visible browser for debugging

# Virginia Public Notice (VPA) — publicnoticevirginia.com (Estate Claims/Foreclosures/Tax Deeds)
# Needs a FREE VPA Smart Search account (VAPN_EMAIL/VAPN_PASSWORD) + CAPTCHA_API_KEY (2Captcha)
python src/main.py va-public-notice                        # daily delta since last run, all 3 categories × 8 VA localities
python src/main.py va-public-notice --va-mode historical   # last 12 months
python src/main.py va-public-notice --va-since 2026-05-01   # custom since-date
python src/main.py va-public-notice --va-headed             # visible browser for debugging
python -m va_public_notice_puller --headed --since 2026-05-25   # standalone diagnostic

# DataSift preset/sequence management
python src/main.py manage-presets --discover                      # list all presets and sequences
python src/main.py manage-presets --add-sold-exclusion            # add Sold exclusion to all presets
python src/main.py manage-presets --create-sold-sequence          # create Sold cleanup sequence
python src/main.py manage-presets --all                           # discovery + update + sequence

# SiftMap sold property tagging
python src/main.py manage-sold --months-back 12                   # tag sold properties (last 12 months)
python src/main.py manage-sold --counties Henrico --min-sale-price 5000

# Courthouse photo import (build 1.0.28+)
python src/main.py photo-import --folder ./photos --photo-county Henrico --photo-type probate
python src/main.py photo-import --folder ./photos --photo-county Henrico --photo-type eviction --skip-obituary
python src/main.py dropbox-watch                                  # auto-poll Dropbox for new photos
python src/main.py dropbox-watch --poll-interval 300 --max-polls 5  # 5-min interval, 5 cycles
python src/main.py dropbox-watch --no-delete                      # keep photos in Dropbox after processing
```

All source files are in `src/` and imports assume `src/` is the working directory. Run from project root with `python src/main.py` or set `PYTHONPATH=src`.

## Architecture

**Data flows:**
- **PropertyRadar pull:** `main.py` → `propertyradar_puller.py` (Playwright against app.propertyradar.com) → membership-diff vs `pr_state.json` → CSV export wizard → `propertyradar_parser.py` → enrichment → CSV
- **PDF import:** `main.py` → `pdf_importer.py` (pypdfium2 → `image_utils.py` OCR) → enrichment → CSV
- **Photo import:** `main.py` → `photo_importer.py` (OpenCV → `image_utils.py` OCR → `llm_parser.py`) → enrichment → CSV
- **Dropbox watch:** `dropbox_watcher.py` → `photo_importer.py` → enrichment → CSV (auto-polling loop)
- **Market Finder:** `extract_market_finder.py` → DataSift Market Finder (Playwright) → paginate all ZIP + neighborhood data → JSON

- **main.py** — CLI entry point. Modes: `daily`/`historical` (PropertyRadar pulls), `pdf-import`, `photo-import`, `dropbox-watch`, `csv-import`, `phone-validate`, `manage-presets`, `manage-sold`, plus analysis modes (`comp`, `rehab`, `analyze-deal`, `market-analysis`, `buyer-prospect`, `deep-prospect`, `lead-manage`, `setup-sequences`, `niche-sequential`, `playbook`).
- **propertyradar_puller.py** — Playwright automation of `app.propertyradar.com` lists. Two-phase BufferedStore scrape (prime → poll → read RadarIDs), membership-diff vs `pr_state.json`, two-step export wizard (Continue → Purchase → Download CSV), quota guard.
- **propertyradar_config.py** — Locked 4-list registry (`PROPERTYRADAR_LISTS`), JS snippets for the ExtJS grid Store API, selectors, state-file paths, schema versioning. See `src/_legacy_tn/README.md` for the previous TN equivalents.
- **propertyradar_parser.py** — PR CSV → `NoticeData`. Handles PR's abbreviated column names ("Radar ID", "Mail Address", etc.) and filters PR's license-disclaimer footer rows via `_RADAR_ID_RE`.
- **propertyradar_quota.py** — Monthly export quota tracker (10K Solo plan), per-month dedup of 50/80/95/100% threshold alerts, `format_quota_summary()` for Slack appendix.
- **notice_parser.py** — Defines the `NoticeData` dataclass used by every puller. Also contains regex-based parsers for free-text notice bodies (legacy TN web-scrape pipeline; PR pulls don't need them).
- **data_formatter.py** — Deduplicates by address (keeps most recent), converts `NoticeData` list to upload CSV. Split mode produces `{county}_{type}_{timestamp}.csv` files.
- **config.py** — State-agnostic config: credentials for Smarty/Zillow/Anthropic/Tracerfy/Trestle/DataSift/Slack/Dropbox/etc., paths, image-processing thresholds, entity-detection regexes, JSON state-file utilities. No acquisition-source-specific config — those live next to the puller (`propertyradar_config.py`, `_legacy_tn/tn_config.py`).
- **image_utils.py** — Shared OCR utilities used by both `pdf_importer.py` and `photo_importer.py`. Exports `fix_rotation()` (Tesseract OSD) and `ocr_page(image, psm)` with configurable page segmentation mode. Handles Tesseract binary detection.
- **photo_importer.py** — Courthouse phone photo import. OpenCV preprocessing chain (EXIF transpose → blur check → bilateral filter → perspective correction → Otsu threshold) → Tesseract OCR (PSM 4) → LLM parsing → NoticeData. Supports all 7 notice types.
- **dropbox_watcher.py** — Cursor-based Dropbox folder polling. Downloads new photos, resolves county + notice_type from folder path (`/Henrico/eviction/photo.jpg`), processes through photo_importer, deletes from Dropbox after success. State persisted to `dropbox_state.json` + `photo_state.json`.
- **report_generator.py** — Generates per-record PDF deep prospecting reports using reportlab. Includes property summary, signing chain with phone tiers, valuation, deceased owner detection. Output to `output/reports/`.
- **extract_market_finder.py** — Playwright automation to extract ALL ZIP code + neighborhood data from DataSift Market Finder. Handles styled-component dropdowns, pagination (20 rows/page), Beamer popup dismissal. Outputs JSON. See "Market Finder Extraction Patterns" below.
- **market_analyzer.py** — ZIP code scoring engine. 6-factor weighted composite (Distress 30%, Value 20%, Equity 15%, Tax Delinquency 15%, Competition 10%, DOM 10%). Grades A/B/C/D, budget allocation across top ZIPs. Reads from scraped notice CSVs in `output/`.
- **drive_uploader.py** — Google Drive upload via service account. `upload_file()` (generic, returns webViewLink) and `upload_csv()` (CSV-specific, returns file ID).

## PropertyRadar Lists (current acquisition source)

Four lists are pre-configured on the PR account and locked into `propertyradar_config.PROPERTYRADAR_LISTS`:

| Slug | List name | Notice type |
|---|---|---|
| `md_auction` | `MD_Auction in 90 Days_No Pre-Probate_No Vacant` | `foreclosure` |
| `va_auction` | `VA_Auction in 90 Days_No Pre-Probate_No Vacant` | `foreclosure` |
| `md_pre_probate` | `MD_Pre-Probate_Distress >60_Occupied` | `pre_probate` |
| `va_pre_probate` | `VA_Pre-Probate_Distress >60_Occupied` | `pre_probate` |

PR's web UI has **no Added-Date filter** so we can't filter exports server-side. The puller therefore reads each list's full membership via the ExtJS grid Store API (`Ext.data.BufferedStore`), diffs RadarIDs against `pr_state.json`, and only exports the new IDs. Re-exports of the same list still bill, so the diff happens *before* the export wizard runs. See [propertyradar-buffered-store](memory) for the API quirks.

## Key Domain Rules

- **PropertyRadar quota:** 10,000 record exports per month on the Solo plan. `propertyradar_quota.py` tracks consumption in `pr_quota.json` and fires Slack alerts at 50/80/95/100% (per-month dedup). The puller refuses to export beyond budget.
- **Probate owner_name** should be the Personal Representative/Executor/Administrator — not the deceased.
- **PropertyRadar `pre_probate` is NOT court probate.** It's a property-records signal (deceased owner per assessor data); no PR/executor is named. Distinct from court-filed `probate`, which doesn't exist for VA/MD until a photo-import pipeline is wired to those courthouses.
- **Rate limiting:** 2-3 second random delays between requests, 3 retries per page.
- **Address dedup:** Same property can appear in multiple lists; `data_formatter.deduplicate()` keeps the most recent.

## Output

CSV files land in `output/` (gitignored). Logs go to `logs/` with timestamped filenames. Sift columns: `date_added, address, city, state, zip, owner_name, notice_type, county, source_url`.

## Apify Deployment

The project runs as an **Apify Actor** in the cloud. When `APIFY_IS_AT_HOME` or `APIFY_TOKEN` is set, `main.py` uses the Actor SDK instead of CLI args.

```bash
# Install Apify CLI
npm install -g apify-cli

# Local test (reads input.json, simulates Actor environment)
apify run --purge

# Deploy to Apify platform
apify login
apify push

# On Apify Console: set up daily schedule and configure secrets in Actor input
```

### Actor Input (configured in Apify Console or `input.json`)
- `mode` (required, enum): one of
  - `daily` / `historical` — PropertyRadar list pulls. Requires `pr_username` + `pr_password`. Delta is membership-diff; the two modes behave identically (PR has no Added-Date filter).
  - `chesterfield-code-violation` — Chesterfield ACA bulk code violation report (anonymous, no creds). Daily-cadence recommended.
  - `richmond-vacant` — Richmond Vacant Building List vacancy registry (anonymous, no creds). Bi-annual cadence; safe to schedule monthly (no-ops if no new publication).
  - `va-public-notice` — Virginia Press Association notices (Estate Claims→probate, Foreclosures→foreclosure, Tax Deeds→tax_sale). Requires `vapn_username` + `vapn_password` + `captcha_api_key`. Daily-cadence recommended.
- `pr_username`, `pr_password`: PropertyRadar login secrets — required ONLY for `daily` / `historical`.
- `vapn_username`, `vapn_password`, `captcha_api_key`: VPA Smart Search login + 2Captcha key — required ONLY for `va-public-notice`. Optional `va_mode` (daily/historical), `va_since` (YYYY-MM-DD).
- `aca_start`, `aca_end`, `aca_first_pull_days`: optional date-window overrides for `chesterfield-code-violation` mode.
- `google_drive_folder_id`, `google_service_account_key`: optional Google Drive upload (all modes).
- `upload_datasift`, `enrich_datasift`, `skip_trace_datasift`: DataSift upload toggles (all modes).

### Actor Output
- **Dataset**: structured records pushed via `Actor.push_data()`
- **Key-value store**: `output.csv` backup
- **Google Drive** (optional): CSV + summary text file uploaded via service account
- **KVS state files** (new): `state__chesterfield_aca_state.json`, `state__richmond_vacant_state.json`, and `state__va_public_notice_state.json` — round-tripped at start/end of each Actor run so deltas persist across the ephemeral Actor file system. See `src/apify_state.py`.

### Setting up scheduled runs in the Apify Console
1. Apify Console → Actors → `siftstack` → **Schedules** tab → New schedule.
2. **Daily Chesterfield code violation pull** (recommended):
   - Cron: `0 9 * * *` (or operator's preferred time)
   - Input: `{"mode": "chesterfield-code-violation"}` plus any DataSift/Drive secrets — schedule input does NOT inherit Actor's Default Input (see memory: apify-schedule-input-independent).
3. **Daily PropertyRadar pull** (existing):
   - Cron: `0 10 * * *` (one hour after Chesterfield, or operator's preferred 5am)
   - Input: `{"mode": "daily", "pr_username": "...", "pr_password": "..."}` plus all enrichment / DataSift secrets.
4. **Richmond Vacant Building List** (manual on-demand): no schedule. Run from Apify Console → Run Actor → set `mode: "richmond-vacant"` → Start when you hear a new publication has dropped (~every 6 months).
5. **Daily Virginia public-notice pull**:
   - Cron: `0 8 * * *` (or operator's preferred time)
   - Input: `{"mode": "va-public-notice", "vapn_username": "...", "vapn_password": "...", "captcha_api_key": "..."}` plus enrichment / DataSift secrets — schedule input does NOT inherit Default Input.

### Key Files
- `.actor/actor.json` — Actor manifest (name, version, Dockerfile path)
- `.actor/input_schema.json` — Input fields + validation for Apify Console UI
- `Dockerfile` — Based on `apify/actor-python-playwright:3.12`
- `src/apify_state.py` — KVS state-file persistence helper (NEW — used by the chesterfield + vacant feed modes; PR pull still uses local `pr_state.json` which has a latent persistence gap on Apify worth fixing in a separate phase)
- `src/drive_uploader.py` — Google Drive upload via base64-encoded service account key
- `input.json` — Local test input (gitignored, contains credentials)

## Courthouse Photo Pipeline (build 1.0.28+)

Courthouse terminal photos → OCR → LLM parse → enrichment → DataSift. Runner takes phone photos at county courthouse terminals, uploads to Dropbox organized as `{county}/{notice_type}/`, system auto-processes. The OCR + parse layer is state-agnostic; only the obituary enricher's domain whitelist and the address standardizer's default state would need to be retargeted for non-TN courts.

### Notice Types (8 total)
- `foreclosure`, `tax_sale`, `tax_delinquent`, `probate` — court-filed (originally from TN web scraper; foreclosure now also from PropertyRadar for VA/MD)
- `eviction` — plaintiff = landlord (target contact), defendant = tenant
- `code_violation` — owner of record, violation type, compliance deadline
- `divorce` — petitioner + respondent, property from schedule page
- `pre_probate` — **PropertyRadar-only**, property-records deceased signal (owner is dead per assessor data; no executor named, no court filing). Distinct from `probate` because DM identification depends on obituary search rather than a named PR/executor on the filing. `owner_deceased="yes"` is set upfront so a failed obituary lookup still tags the record correctly.

### Critical OCR Patterns (hard-won from live testing)

**Moire pattern from terminal screens is the #1 OCR killer.** Standard Tesseract preprocessing (adaptive threshold, CLAHE) produces garbage on courthouse terminal photos. The fix:
- **Bilateral filter** (`cv2.bilateralFilter(gray, 15, 75, 75)`) removes moire while preserving text edges
- **Otsu threshold** (`cv2.THRESH_BINARY + cv2.THRESH_OTSU`) after bilateral — auto-determines optimal binary threshold
- **PSM 4** (single column variable text) for terminal screens — NOT PSM 6 (single uniform block) which was the research recommendation but fails in practice
- **Do NOT use `fix_rotation()` (Tesseract OSD) on phone photos** — EXIF transpose handles rotation. OSD on raw phone images often fails and the 270° fallback rotates correct images sideways

### Probate Deep Prospecting (from courthouse terminals)

Courthouse probate records have decedent name + PR/executor name but NO property address. Property-address lookup originally went through a 3-tier waterfall (Knox Tax API → executor family search → people search). The Knox Tax API tier was archived to `src/_legacy_tn/tax_enricher.py` when TN was retired. The remaining generic tiers (people search via Serper/Firecrawl + LLM extraction; DuckDuckGo fallback) are state-neutral and live in `obituary_enricher._lookup_dm_address()`.

**Probate Preset** (obituary enricher):
- Triggers when court record has PR name + decedent name (no address required) — prevents wrong obituary from overriding court-named executor
- Sets DM = the named PR/executor directly, skips obituary search entirely
- Then runs DM address lookup (People Search → Tracerfy)

**DOD Sanity Check** (obituary enricher):
- Rejects obituary matches where DOD is > 3 years before the notice filing date (`MAX_DOD_GAP_YEARS = 3`)
- Prevents matching a 2014 obituary to a 2025 court filing (wrong person with same name)
- Applied to both full-page and snippet matches

### Dropbox Folder Structure
```
{DROPBOX_ROOT_FOLDER}/
├── {County}/
│   ├── eviction/
│   ├── code_violation/
│   ├── divorce/
│   ├── foreclosure/
│   ├── tax_sale/
│   └── probate/
└── {OtherCounty}/
    └── (same subfolders)
```

### Environment Variables
- `DROPBOX_APP_KEY` — Dropbox OAuth2 app key
- `DROPBOX_APP_SECRET` — Dropbox OAuth2 app secret
- `DROPBOX_REFRESH_TOKEN` — Dropbox offline refresh token (auto-rotates access tokens)
- `DROPBOX_POLL_INTERVAL` — seconds between polls (default 900 = 15 min)
- `DROPBOX_ROOT_FOLDER` — root folder path in Dropbox (e.g., "SiftStack")

### Dependencies (added to requirements.txt)
- `opencv-python-headless>=4.13.0` — image preprocessing (headless = no GUI, saves 26MB in Docker)
- `numpy>=1.26.0` — required by OpenCV
- `dropbox>=12.0.2` — Dropbox SDK (minimum for post-Jan-2026 API compatibility)

## Richmond Vacant Building List — Vacancy Registry Feed (build 1.0.30+)

Richmond Property Maintenance & Code Enforcement publishes a Vacant Building
List PDF on `rva.gov`. **This is a vacancy registry, NOT a code violation source.**
Operator-confirmed taxonomy (2026-05-25):
- **Richmond code violations** → OPP / EnerGov portal (see "Richmond OPP" section)
- **Richmond vacancy registry** → this PDF
- **Richmond full code violation caseload bulk** → email request to
  `PropertyMaintenance@rva.gov`, then drop response into
  `{DROPBOX_ROOT_FOLDER}/Richmond/code_violation/` for the existing Dropbox pipeline.

### Key Files
- `src/richmond_vacant_puller.py` — probes URL pattern, fetches PDF, parses via
  pdfplumber (clean 6-column extraction), diffs against `richmond_vacant_state.json`,
  returns `NoticeData` records typed as `notice_type="vacant_building"` with
  `source_url="richmond_vacant_building_list://YYYY-MM"`.
- `richmond_vacant_state.json` — at PROJECT_ROOT. Tracks last fetched URL,
  content hash, and a `known_records` index keyed by `sha256(address|owner)[:16]`.

### URL pattern
```
https://rva.gov/sites/default/files/{YYYY-MM}/Vacant%20Building%20List%20-%20{Month}%20{YYYY}.pdf
```

### Cadence — IMPORTANT
**Cadence is bi-annual** (~6 months apart), NOT monthly. The puller still
probes the last 6 months and uses content-hash change detection so it stays
robust to off-cycle publications. See memory: [richmond-code-violations](memory)
for operator confirmation.

### Schema (per parsed row)
| PDF column | NoticeData field |
|---|---|
| Address (with property ZIP) | `address` + `zip` (split on `, NNNNN$`) |
| Owner | `owner_name` |
| MailAddress | `owner_street` |
| MailCity | `owner_city` |
| State | `owner_state` |
| MailZip | `owner_zip` |

`city="Richmond"`, `state="VA"`, `county="Richmond City"` are constants.

### CLI
```bash
python src/main.py richmond-vacant                # fetch + enrich + write CSV to output/
python src/main.py richmond-vacant --upload-datasift  # also push to DataSift
```

### Disposition routing
- Vacant Building List records → `notice_type="vacant_building"` → DataSift
  list `"Vacant Building"` (auto-created from CSV on first upload if absent).
- Richmond code violations (OPP enricher fields OR email-requested caseload)
  → `notice_type="code_violation"` → DataSift list `"Code Violation"`.
- `source_url="richmond_vacant_building_list://YYYY-MM"` always identifies
  records from this specific PDF.

### Dependencies (added to requirements.txt)
- `pdfplumber>=0.11.0` — clean table extraction for digital PDFs (no OCR needed)

## Richmond OPP / EnerGov Per-Address Code-Case Enricher (build 1.0.31+)

Richmond City's [Tyler EnerGov Self-Service portal](https://energov.richmondgov.com/EnerGov_Prod/SelfService/richmondvaprod#/home)
exposes permits + code cases per-address. The SPA is backed by a JSON API
that accepts **anonymous direct HTTP calls** with three Tyler tenant
headers — no Playwright needed at runtime.

The pipeline calls this as **Step 7b** for every Richmond City record,
attaching active code violations, recent permits, and the Richmond parcel ID
as new `NoticeData` fields. Non-Richmond records are self-filtered (no API
call). Records in Henrico/Chesterfield/Hanover are explicitly excluded even
when their `county` field contains "Richmond" (e.g., "North Richmond" in
N. Chesterfield).

### Key Files
- `src/richmond_opp_enricher.py` — `enrich_notices(notices)` is the public entry
  point. Pipeline integration in [src/enrichment_pipeline.py](src/enrichment_pipeline.py).
- Recon scripts (one-off, kept for reference): `test_richmond_opp_recon.py`,
  `test_richmond_opp_api.py`.

### API contract (verified 2026-05-25)
- Endpoint: `POST https://energov.richmondgov.com/energov_prod/selfservice/api/energov/search/search`
- Auth: anonymous. Required headers:
  - `tenantid: 1`
  - `tenantname: richmondvaprod`
  - `tyler-tenanturl: richmondvaprod`
  - `tyler-tenant-culture: en-US`
  - `referer: https://energov.richmondgov.com/EnerGov_Prod/SelfService/richmondvaprod`
  - `Content-Type: application/json;charset=UTF-8`
- POST body: see `_BASE_PAYLOAD` in the enricher module — all nested
  `*Criteria` blocks default to null; only `Keyword` is swapped per call.
- Response shape: `Result.EntityResults[]` — unified array across permits,
  code cases, inspections, plans. Filter by `CaseType` substring.

### Address handling
- Operator instruction (per the Richmond Property Maintenance & Code Enforcement
  office): strip the street suffix from the search input. `"10 E Baker St"` →
  `"10 E Baker"`. Suffixes: St/Ave/Rd/Dr/Ln/Blvd/Way/Cir/Ct/Pl/Pkwy/Ter/Tpke/Trl/Hwy/Loop/Run/Pike/Sq/Pt/Cv/Bnd/Mews/Walk/Park/Row.
- API uses **fuzzy keyword matching, NOT exact address**. `"102 E Broad"` returns
  both `102 E Broad St` AND `102 E Broad Rock Rd`. Enricher post-filters results
  by matching house number + direction + first street word against the
  decomposed `Address` fields in each response entity.

### Detection rules
- **Active code violation**: `CaseType` contains one of `code`, `maintenance`,
  `compliance`, `code violation`; AND `CaseStatus` contains one of
  `in violation`, `open`, `re-inspection`, `notice issued`.
- **Recent permit**: NOT a code case; AND `ApplyDate` within last 2 years.

### NoticeData fields added (see [src/notice_parser.py](src/notice_parser.py))
- `opp_checked` — `yes` | `skip_bad_address` | `http_error` | `network_error` | ``
- `opp_active_violation_count`
- `opp_total_code_case_count`
- `opp_recent_permit_count`
- `opp_active_violation_cases` — pipe-separated case numbers (top 5)
- `opp_latest_violation_status`
- `opp_latest_violation_date` (YYYY-MM-DD)
- `opp_parcel_id` — Richmond `MainParcel` (assessor parcel ID)

### Pipeline integration
- Step 7b in [src/enrichment_pipeline.py](src/enrichment_pipeline.py), between Smarty/geocode and Zillow.
- Default ON. Skip with `PipelineOptions(skip_opp=True)`.
- Chesterfield ACA puller passes `skip_opp=True` explicitly (no Richmond records).
- Rate limit: 0.4s between API calls (~150 req/min — well below any plausible
  public-API threshold).

### Smoke test
```bash
python -m richmond_opp_enricher "10 E Baker St"   # known violation property (12 active cases)
```

## Chesterfield ACA Code Violation Bulk Feed (build 1.0.31+)

Chesterfield County exposes a public **date-range Code Violation report** on its
Accela Citizen Access (ACA) portal. Anonymous access — no login required for
the Reports section (the case-search Enforcement module IS login-walled, but
that's a separate path). Newly-launched feature confirmed by the Chesterfield
code enforcement office 2026-05-25.

### Key Files
- `src/chesterfield_aca_puller.py` — Playwright drives the report form, downloads
  XLSX, parses via openpyxl, diffs against `chesterfield_aca_state.json`.
- `chesterfield_aca_state.json` — at PROJECT_ROOT. Tracks `known_record_ids` for
  dedup + last window for incremental pulls.

### URL contract (verified 2026-05-25)
- Form URL: `https://aca-prod.accela.com/CHESTERFIELD/Report/ReportParameter.aspx?module=&reportID=9735&reportType=LINK_REPORT_LIST`
- Date inputs: `#Date_11907` (Start), `#Date_11908` (End) — AjaxControlToolkit
  MaskedEdit widgets. `page.fill()` is rejected by the validator; use
  click → Ctrl+A → Delete → `page.type()` with 40ms per-keystroke delay → Tab.
- Submit: `#btnSave`. Triggers a **same-page XLSX download** (NOT a popup).
- Wait ~1.2s after typing dates before submit so validators finish.
- Server-side report gen takes 30-120s — `page.expect_download(timeout=180000)`.

### XLSX schema (header at row 3, 0-indexed)
| Column | Sample |
|---|---|
| Record Type | `Property Maintenance` (PM*) or `Zoning Code Compliance` (CE*) |
| Record ID | `PM26-0029`, `CE26-0877` — perfect dedup key |
| Submittal Date | datetime |
| Record Status Date | datetime |
| Record Status | `In Violation`, `Closed`, `Abated`, `Submitted`, `Pending NOV` |
| Code Section | `304.12 - Handrails and guards`, `11-32(a) - Tall Grass/Occupied` |
| Property Address | UPPERCASE street, no city/zip — e.g. `219 HAZELMERE DR` |

### High-motivation Code Section filter
By default the puller emits ONLY records whose case cites a Code Section
matching one of the patterns in
`src/chesterfield_aca_puller.py::HIGH_MOTIVATION_CODE_SECTIONS`. Default = `["vacant"]`,
which catches `11-32(b) - Tall Grass/Vacant` and any future Vacant-named
section. Cases that don't match are still added to
`chesterfield_aca_state.json` (so dedup works) but not emitted as leads.

To bypass the filter:
- CLI: `python src/main.py chesterfield-code-violation --aca-all-violations`
- Apify input: `"aca_all_violations": true`

To widen the filter, edit `HIGH_MOTIVATION_CODE_SECTIONS` in
`src/chesterfield_aca_puller.py`. Candidate extensions (from May 2026 30-day recon):

| Pattern | Records (per month) | Signal strength |
|---|---:|---|
| `"vacant"` (default) | 76 | Strong — explicit vacant |
| `"Discarded Materials"` | 41 | Strong — abandoned junk |
| `"Inoperable Vehicle"` | 39 | Strong — dead car / neglect |
| `"Trash/Litter"` | 7 | Moderate |
| `"Tall Grass/Occupied"` | 132 | Weak — owner present |

**Widening the filter requires deleting state to re-emit historical matches.**
Filtered-out records added to state on prior runs won't be re-evaluated.

A single Record ID can cite multiple Code Sections; the puller groups rows
by Record ID and emits one NoticeData per case with all sections pipe-joined
in `raw_text`. Cases that combine vacant + other distress signals (e.g.
`11-32(b) - Tall Grass/Vacant | 13-62 - Inoperable Vehicle | 11-31 - Trash/Litter`)
are the richest leads in the dataset.

### Notes
- XLSX has no owner/city/zip. `city=""`, `state="VA"`, `county="Chesterfield County"`.
  Smarty fills the city/zip downstream from the property address.
- Records emit as `notice_type="code_violation"` with
  `source_url="chesterfield_aca_report://{record_id}"` for downstream
  disambiguation from the Richmond Vacant Building List feed.
- Volume per recon: ~12 cases/day, ~370/month total. After default filter
  (`vacant` only), ~76/month = ~2.5 emitted leads/day.

### Dependencies (already in requirements.txt)
- `playwright>=1.40.0` — drives the ASP.NET WebForms flow
- `openpyxl>=3.1.0` — parses the XLSX download

## Virginia Public Notice (VPA) Acquisition Source (build 1.0.32+)

The Virginia Press Association publishes a statewide public-notice registry at
**publicnoticevirginia.com** (NOT `vapublicnotice.com`, which refuses
connections). The `va-public-notice` mode scrapes it for the notice types
PropertyRadar can't reach in VA — chiefly **court probate** (Estate Claims).

**This is the SAME ASP.NET "Smart Search" platform as the archived
tnpublicnotice.com** (identical `authenticate.aspx`, `Smartsearch/Default.aspx`,
session `(S(...))` URLs, `WSExtendedGrid`, `as1_` controls), so the archived TN
scraper under `src/_legacy_tn/` is the structural template. See memory:
[va-public-notice](memory).

### Auth + captcha (the key constraint)
The public Advanced Search form and results grid render anonymously, but the
**preview snippet doesn't contain the property address** — the full notice text
(address, owner/borrower, trustee, sale date) is behind a **FREE VPA "Smart
Search" account**, and the notice **detail page is reCAPTCHA-gated** (same as the
TN twin).
- Sign up free: `https://www.publicnoticevirginia.com/SmartSearchSignup.aspx`
- Env: `VAPN_EMAIL`, `VAPN_PASSWORD`, and the existing `CAPTCHA_API_KEY` (2Captcha,
  reused). Detail reCAPTCHA sitekey is detected dynamically (no hardcode).

### Design — one union search per county, classify by CONTENT
The "Popular Searches" presets only auto-fill the keyword box with a broad
**OR-term list**, and the terms cross-contaminate (the Estate Claims preset's
"estate" matches foreclosure "**real estate**" notices) — so a preset does NOT
reliably imply a notice_type. Instead the puller (verified live 2026-06-03):
1. Runs **one search per county** — a single focused union keyword
   (`SEARCH_KEYWORD`, OR match) + one County checkbox + date window. County is
   unambiguous because we know which checkbox produced the results.
2. **Classifies each notice from its content** via `classify_notice_type()` —
   on the cheap grid **preview** (to skip non-targets BEFORE spending a captcha
   solve) and again on the **full body** (final). Non-target notices (meeting
   schedules, ABC licenses, etc.) are dropped but their preview-hash is cached.
3. Full text is **LLM-parsed with VA-specific prompts inside the puller** (NOT
   the TN-hardcoded `llm_parser.py`, which would inject "TN").

### Notice types (classified from content, 2026-06-02 scope)
| notice_type | Detected by | Why it matters |
|---|---|---|
| `probate` | "estate of", "notice to creditors", "qualified as", PR/executor | Fills the VA court-probate gap (PR/executor named → triggers obituary enricher's "DM = named PR" preset). Sets `owner_deceased="yes"`, `decedent_name`, PR mailing address. |
| `foreclosure` | "trustee's sale", "deed of trust", "in execution of" | Overlaps PropertyRadar VA lists; dedup downstream by address. |
| `tax_sale` | "delinquent tax", "tax deed", "§58.1-39…" | Not sourced elsewhere for VA. |

Divorce was dropped (no clean signal, low value). Notice taxonomy unchanged —
reuses existing `probate`/`foreclosure`/`tax_sale` types + DataSift list maps.

### Target localities (VA buy-box + nearby)
Exact County-checkbox labels: **Richmond City** (NOT "Richmond", which is the
rural Richmond County on the Northern Neck), Henrico, Chesterfield, Prince
William, Hanover, Goochland, Powhatan, Fairfax. Checkboxes render offscreen, so
they're ticked via JS (`checked` + dispatched events), matched on exact label.

### Throughput note
Each notice detail requires a 2Captcha reCAPTCHA-v2 solve (~30-90s). Daily
deltas are small (fine for a cron); a full historical backfill is slow. Balance
on the 2Captcha account funds it (~$0.003/solve).

### Key Files
- `src/va_public_notice_puller.py` — `pull_new_records(mode="daily", since=None,
  headless=True) -> list[NoticeData]` (sync) + `pull_new_records_async(...)`.
  Login → per-county union search → paginate → preview-classify → detail+captcha
  → full-text classify → LLM-parse → NoticeData. `classify_notice_type()` is the
  notice_type source of truth.
- `src/va_public_notice_config.py` — URLs, selectors (ALL verified live),
  `SEARCH_KEYWORD`, `TARGET_LOCALITIES`, `TARGET_NOTICE_TYPES`, state paths.
- `src/captcha_solver.py` — shared, state-agnostic 2Captcha helper (sitekey
  auto-detected). Promoted from `_legacy_tn/captcha_solver.py` so active code
  doesn't import from the archive.
- `va_public_notice_state.json` (PROJECT_ROOT) — `{last_run_date, seen_ids}`.
  `seen_ids` dedups BEFORE opening a detail, so a notice is never re-captcha'd.
- Recon scripts (one-off, kept for reference): `test_va_public_notice_recon.py`,
  `test_va_public_notice_recon2.py`.

### Pipeline integration
- `notice_type="probate"|"foreclosure"|"tax_sale"`,
  `source_url="va_public_notice://{notice_id}"`.
- Enrichment: OPP stays ON (Richmond City records get code-case enrichment;
  the enricher self-filters non-Richmond). `skip_parcel_lookup=True`. Vacant land
  kept (operator buy-box).
- `source_url` scheme disambiguates from PR / Chesterfield ACA / Richmond Vacant feeds.

### Dependencies (already in requirements.txt)
- `playwright`, `2captcha-python`, `anthropic` (LLM parse) — no new deps.

## DataSift.ai (REISift) Integration

DataSift.ai (formerly REISift) is the CRM where scraped records land for niche sequential marketing campaigns. There is **no REST API** — upload is via Playwright browser automation of the web UI.

**Domain:** `app.reisift.io` (NOT `app.datasift.ai`). API at `apiv2.reisift.io`.

### Key Files
- `src/datasift_formatter.py` — Transforms `NoticeData` → DataSift CSV (41 columns)
- `src/datasift_uploader.py` — Playwright login + upload wizard + enrich + skip trace + preset management + sequence builder + SiftMap sold workflow
- `test_datasift_upload.py` — Headed browser test (upload + enrich + skip trace)
- `test_manage_presets.py` — Headed browser test (preset discovery + sold exclusion + sequence creation)
- `test_manage_sold.py` — Headed browser test (SiftMap sold property tagging)

### CSV Column Structure (41 columns)
- **Core auto-mapped (11):** Property Street/City/State/ZIP, Owner First/Last Name, Mailing Street/City/State/ZIP, Tags
- **Lists + Notes (2):** Lists (for niche sequential), Notes (contextual per notice type)
- **Built-in fields (13):** Estimated Value, MSL Status, Last Sale Date/Price, Equity Percentage, Tax Deliquent Value, Tax Delinquent Year, Tax Auction Date, Foreclosure Date, Probate Open Date, Personal Representative, Parcel ID, Structure Type, Year Built, Living SqFt, Bedrooms, Bathrooms, Lot (Acres)
- **Custom fields (15):** Notice Type, County, Date Added, Owner Deceased, Date of Death, Decedent Name, Decision Maker, DM Relationship, DM Confidence, DM 2/3 Name/Relationship, Obituary URL, Source URL

### Niche Sequential Marketing
DataSift's niche sequential system uses filter presets to guide records through skip-trace → SMS → Call (3 follow-ups) → Mail → Deep Prospecting phases. Two preset folders: "00. NICHE SEQUENTIAL" (14 presets, courthouse data) and "01. Bulk Sequential Marketing" (9 presets, bulk data). The "00. NICHE SEQUENTIAL" folder is owned by the operator in DataSift's UI and is the source-of-truth for those 14 presets — `src/niche_sequential.py` PRESETS only mirrors SiftStack-added presets (currently just `14. Pre-Probate → DP`). All presets exclude Sold status (build 1.0.23+). Preset 14 (`Pre-Probate → DP`) routes PR-sourced pre_probate records to deep_prospector for heir research before any contact channel fires, since the property owner is dead. A "Sold Property Cleanup" sequence in the Transactions folder auto-fires on "Sold" tag to change status, remove from lists, clear tasks, and clear assignee.

- **"Courthouse Data" tag:** Every record gets this tag — signals first-to-market county data (prioritized over bulk data in filter presets)
- **Lists column (additive, DSP-01):** Every record carries TWO list memberships, comma-delimited: its per-notice-type list AND the `SiftStack` disposition list. Per-type map: `foreclosure` → `"Foreclosure"`, `probate` → `"Probate"`, `pre_probate` → `"Pre-Probate"`, `tax_sale` → `"Tax Sale"`, `tax_delinquent` → `"Tax Delinquent"`, `eviction` → `"Eviction"`, `code_violation` → `"Code Violation"`, `divorce` → `"Divorce"`, `vacant_building` → `"Vacant Building"`. Cell value example: `"Foreclosure,SiftStack"` (CSV-auto-quoted because the value contains the column delimiter `,`). Records without a notice_type still get `SiftStack` so the disposition always lands. Per-type list is FIRST segment, `SiftStack` is SECOND — DataSift auto-creates per-type lists from CSV; `SiftStack` pre-exists in the operator's account. Source-of-truth constants: `SIFTSTACK_LIST_NAME = "SiftStack"`, `LIST_DELIMITER = ","` in `src/datasift_formatter.py`.
- **Tags:** Courthouse Data, notice_type, county, YYYY-MM date, deceased/living, DM confidence level, has_auction, tax_delinquent, photo_import (for photo-sourced records)

### Upload Wizard (6 Steps — DataSift added Enrichment step post-Phase-1)
1. **Setup:** Click "Upload File" sidebar → "Add Data" → dropdown "Uploading a new list not in DataSift yet" → enter list name → organization questions
2. **Enrichment:** Accept defaults via Next Step. New step DataSift inserted (discovered 2026-05-23 Phase 5 smoke test); previously the wizard was 5 steps. The script just clicks Next here — no per-record enrich options needed since we drive enrichment from the SiftStack pipeline.
3. **Tags:** Add "Courthouse Data" custom tag (other tags ride along in CSV column)
4. **Upload File:** Set file on `input[type="file"]`
5. **Map Columns:** Core address fields auto-map; Tags, Lists, and enrichment columns may need manual mapping
6. **Review + Finish Upload:** Click "Finish Upload" — processing happens in background

### Column Mapping Notes
- Only core address fields (Property Street, City, State, ZIP) reliably auto-map
- Tags, Lists, Estimated Value, and enrichment columns often stay unmapped in step 5
- Notes and MSL Status sometimes auto-map
- Custom fields (SiftStack custom-field group) require drag-and-drop mapping

### Contact Logic
- **Deceased owners:** Contact = decision maker (first/last name + mailing address from DM)
- **Living owners:** Contact = property owner (owner mailing address, falls back to property address)

### Post-Upload: Enrich + Skip Trace

After CSV upload, the pipeline automatically runs two DataSift actions via Playwright:

1. **Enrich Property Information** (Manage → Enrich Data): Adds SiftMap property data (beds, baths, Zestimate, sqft, sale history) to uploaded records. "Enrich Owners" and "Swap Owners" are OFF — protects our PR/DM contact mapping.
2. **Skip Trace** (Send To → Skip Trace): Pulls phone numbers (up to 5 per owner) + emails via unlimited plan ($97/mo). Adds auto-tag `skip_traced_YYYY-MM`.

Both run in background — tracked in Activity tab. Both are ON by default when `--upload-datasift` is set.

### CLI Flags
```bash
python src/main.py daily --upload-datasift        # upload + enrich + skip trace
python src/main.py daily --upload-datasift --no-enrich       # upload only, skip enrichment
python src/main.py daily --upload-datasift --no-skip-trace   # upload + enrich, skip skip trace
python src/main.py daily --notify-slack            # send run summary to Slack/Discord
```

### Environment Variables
- `DATASIFT_EMAIL` — DataSift login email
- `DATASIFT_PASSWORD` — DataSift login password
- `SLACK_WEBHOOK_URL` — Slack/Discord webhook for run summaries

### Login Selectors (SPA quirks)
- Hidden checkboxes (Remember me, Terms) — click `<label>` elements, not `<input>`
- Use `wait_until="domcontentloaded"` (not `networkidle` — SPA keeps WebSocket connections open)
- Cookie validation: check for `/dashboard` or `/records` in URL (5s wait for SPA redirect)

### DataSift UI Automation Patterns

Hard-won patterns from build 1.0.22-1.0.23 (SiftMap, preset management, sequence builder). Follow these to avoid repeating past mistakes.

**Styled-Components (no native HTML controls)**
- No native `<select>` elements — all dropdowns are `[class*="Selectstyles__Select"]` containers
- `[class*="SelectValue"]` = current value display; `[class*="SelectOptionContainer"]` = dropdown options
- Multiple Select dropdowns exist per panel (Lists, Tags, Property Status) — always target the **LAST visible one**
- Use `x > 450` bounds check in all JS queries to avoid matching sidebar elements (sidebar is 0-400px)
- React state updates require native setter + event dispatch, not just `.value = ...`:
  ```js
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  setter.call(input, 'new value');
  input.dispatchEvent(new Event('input', {bubbles: true}));
  input.dispatchEvent(new Event('change', {bubbles: true}));
  ```

**Panel Scrolling (Playwright scroll fails)**
- Filter panel is a scrollable `<div>`, NOT the viewport — `scroll_into_view_if_needed()` does nothing
- Use JS: `el.scrollIntoView({behavior: 'instant', block: 'center'})` instead
- Filter Presets section is at the BOTTOM of the filter panel — must scroll container down to reveal
- After scrollIntoView, element y-positions may be negative — don't filter by `y > 0` for the target element

**React DnD (Sequence Builder)**
- Cards have `draggable="false"` — Playwright's native drag won't work
- Must use slow mouse drag: `mouse.move()` → `mouse.down()` → 20 incremental steps (50ms each) → `mouse.up()`
- Add 500ms pauses between down/move/up phases
- "Add new Action +" button required for 2nd+ actions; first action uses initial drop zone
- Sidebar cards can scroll out of view when main area scrolls — scroll BOTH source and target into view before drag

**Pointer Interception (common blockers)**
- Beamer NPS survey iframe (`#npsIframeContainer`) blocks ALL pointer events globally — remove from DOM via `_dismiss_popups()`
- `RecordsFiltersstyles__RecordsFiltersSection` elements intercept clicks — use `page.evaluate()` JS click or `force=True`
- When Playwright click fails with "outside of viewport" or "intercept": switch to `page.evaluate(el => el.click())`
- SiftMap PropertyDetails panel blocks sidebar checkboxes — remove from DOM before interactions

**Preset Management Workflow**
- Flow: open filter panel → scroll to bottom → expand "Filter Presets" → expand folder → click preset → modify → Save (not Save New) → confirm overwrite
- Folder names have case variations ("00 Niche" vs "00 NICHE") — use `.toUpperCase()` comparison
- Preset names follow pattern `^\d{2}\.` (e.g., "00. Needs Skipped")
- 2 folders: "00. NICHE SEQUENTIAL" (14 presets), "01. Bulk Sequential Marketing" (9 presets)
- All 23 presets have Property Status "Do not include" → "Sold" (build 1.0.23+)
- The "00. NICHE SEQUENTIAL" cycle is call-first with an SMS step between skip-trace and calls: `00. Needs Skipped → 01. Skipped No Numbers → SMS step (via sequence) → 02. Ready to Call → 03-05. Follow-Up 1/2/3 → 06. Needs First Mail → 07. Mail Monthly → 08-10. * → DP → 11. Not Interested Qrtly → 12. Rehash → 13. No Valid Number → DP → 14. Pre-Probate → DP`. The 13 base presets (00-13) are operator-owned in DataSift's UI; code mirrors only the SiftStack-added preset 14.

**Sequence Builder Workflow**
- Flow: `/sequences` → Create → title + folder → drag trigger → condition → actions tab → drag actions → configure → save
- Duplicate name handling: detect error toast "different sequence title", retry with " V2" suffix
- Actions tab: navigate via "Set the Following Actions" button or URL (`/sequences/new/actions`)
- Autocomplete inputs: after each selection, `fill("")` + Escape to dismiss dropdown before next entry
- "Sold Property Cleanup" sequence exists in Transactions folder (build 1.0.23): Trigger (Property Tags Added) → Condition (Sold) → Actions (Status→Sold, Remove Lists, Clear Tasks, Clear Assignee)

**SiftMap Automation**
- Search by city (NOT county) — e.g., Henrico → "Richmond, VA", Prince George's → "Upper Marlboro, MD"
- PropertyDetails panel auto-opens on search — remove from DOM before other interactions
- "Add Records to Account" modal: toggle OFF "Do not replace owners", add tags, dismiss dropdown by clicking heading (NOT Escape — clears tags)
- Known limitation: SiftMap filters (price, date) set values visually but don't trigger React re-query. Only sidebar-visible properties (~3-5) get added per run

**Market Finder Extraction Patterns (build 1.0.29+)**

Hard-won patterns from building `extract_market_finder.py`. The Market Finder UI differs significantly from the rest of DataSift.

- **NO HTML `<table>` element** — data table is entirely div-based: `Tablestyles__TableContainer` → `TableRow` → `TableCell` (styled-components). Searching for `<table>` or `<tr>/<td>` finds nothing.
- **PAGINATION, not infinite scroll** — table shows 20 rows per page with "1-20 of N" text and `PaginationInnerContainer` with prev/next `<button>` elements. Must click through ALL pages to get complete data. (Mid-size county example: ~50 ZIPs across 3 pages, ~120 neighborhoods across 7 pages.)
- **State/County selection uses `InputMultiSearch`** — NOT styled-component Select dropdowns. Inputs have placeholders: `"Select States"`, `"Select Counties"`, `"Select ZIP Codes"`. Click input → type name → click dropdown result item (`[class*="Item"]:has-text("...")`).
- **ZIP/Neighborhood toggle is a styled Select dropdown** — at the top bar with `Selectstyles__SelectValue` showing current view. Check the displayed text BEFORE clicking — if already on the correct view, clicking toggles AWAY from it. Only click to switch if the displayed text doesn't match the desired view.
- **Beamer push modal (`#beamerPushModal`)** — appears on fresh login, blocks ALL pointer events. Different from the NPS survey (`#npsIframeContainer`). Both must be removed from DOM before any click interactions. Always call dismiss with `force=True` as fallback.
- **Page body scrolling required** — pagination controls are at `y=1867`, below the viewport (`clientH=824`). Must scroll `AdminPage__AdminPageBody` container down before pagination buttons are accessible.
- **Summary panel on right side** — shows county-level aggregates: Median Home Value, Homes on Market, Mo. Investor Transactions, Homes Sold Last Month, Market Rent, Gross Rental Yield, Homeownership Rate. Extract via regex on page text.

```bash
# Extract all Market Finder data for a county
python src/extract_market_finder.py --state "Virginia" --county "Henrico" -v
python src/extract_market_finder.py --state "Virginia" --county "Henrico,Chesterfield" --headless

# Output: JSON file in output/market_finder_{state}_{county}_{timestamp}.json
```

## REI Skill Library (13 Skills)

Distribution-ready Claude Co-Work skill files at `Skills for REI/improved/`. Each `.skill` is a ZIP containing `SKILL.md` + `references/` folder. Plugins (`.plugin`) also include `commands/` and `.claude-plugin/plugin.json`.

### Skill Inventory

| # | File | Division | Score | What It Does |
|---|------|----------|-------|-------------|
| 1 | `sift-market-research.skill` | Market Intel | 9.6 | Market Finder reports, zip code scoring (6 weights verified against `market_analyzer.py`), 7-sheet Excel output |
| 2 | `first-market-county-data.skill` | Market Intel | 9.7 | County clerk data extraction for all 7 notice types, FOIA templates, marketing windows |
| 3 | `buyer-prospector.skill` | Market Intel | 9.6 | Cash buyer list from 84K+ records, LLC/trust/corp research, 50-state SOS URLs |
| 4 | `real-estate-comping.skill` | Deal Analysis | 9.7 | Two-Bucket ARV, disclosure/non-disclosure routing (12 states), adjustments verified against `comp_analyzer.py` |
| 5 | `rehab-estimator.skill` | Deal Analysis | 9.8 | 912-line skill, complete Repair Cheat Sheet verified against real contractor SOW, 4-tier system |
| 6 | `deal-analyzer.plugin` | Deal Analysis | 9.6 | Combined comp+rehab pipeline, MAO (75%/70% rules), multi-loan financing, exit strategy comparison |
| 7 | `deep-prospecting.skill` | Deal Analysis | 9.6 | 4-level research depth (L1-L4), heir verification loop, DOD sanity check (3yr), 3-site skip trace waterfall |
| 8 | `probate-property-finder.skill` | Deal Analysis | 9.7 | Property lookup for probate decedents, 3-tier search (Tax API→Executor→People search), confidence scoring |
| 9 | `phone-validator.skill` | Operations | 9.8 | Trestle API scoring, 5-tier dial priority, 3 tier strategies, litigator risk check, 4.75x connect rate |
| 10 | `sequential-presets.skill` | Operations | 9.5 | 12 niche + 9 bulk filter presets, Pendulum Theory (SMS→Call→Mail→DP), DataSift UI implementation steps |
| 11 | `sift-sequences.skill` | CRM | 9.5 | 26 TCA sequence templates (verified against `sequence_templates.py`), UI walkthrough, HOT A01-A16 chains |
| 12 | `sift-operations.plugin` | CRM | 9.3 | CRM operations encyclopedia, STABM routine, lead pipeline (9 statuses), task presets, team roles |
| 13 | `playbook-creator.skill` | Operations | 9.5 | Playbook/SOP generator from transcripts, 7-node chart limit, 5th grade reading level, Word doc output |

### Cross-Skill Verified Consistency

These values are identical across all skills that reference them:
- **Phone tiers:** 81-100 (Dial First), 61-80 (Dial Second), 41-60 (Dial Third), 21-40 (Dial Fourth), 0-20 (Drop)
- **Preset folders:** "00. NICHE SEQUENTIAL" (14 presets, operator-owned in DataSift UI; SiftStack code only mirrors `14. Pre-Probate → DP`), "01. Bulk Sequential Marketing" (9 presets)
- **Sequence count:** 26 TCA templates across 5 folders (Lead Management 6, Acquisitions 6, Transactions 6, Deep Prospecting 4, Default 4)
- **Comp adjustments:** Bedroom $5,000, Bathroom $7,500, $/sqft $85, Age $500/yr (from `comp_analyzer.py`)
- **Financing defaults:** HML 12%, conventional 7%, 2 points, 2.5% closing (from `deal_analyzer.py`)
- **DOD sanity:** MAX_DOD_GAP_YEARS = 3 (from `obituary_enricher.py`)
- **Notice types:** 9 total (foreclosure, tax_sale, tax_delinquent, probate, pre_probate, eviction, code_violation, divorce, vacant_building)

### Key Corrections Made During Optimization (April 2026)
- **Hardcoded credentials removed** from sift-market-research (had email/password in SKILL.md)
- **Bedroom adjustment corrected** from $10K to $5K in real-estate-comping (matched to `comp_analyzer.py`)
- **HML points corrected** from 0% to 2% in deal-analyzer (matched to `deal_analyzer.py DEFAULT_HARD_MONEY_POINTS`)
- **Linux paths fixed** in sequential-presets (was `/home/ubuntu/skills/...`, now relative)
- **Preset names aligned** across 3 skills to match `niche_sequential.py` source code
- **Transfer tax labeled** as Tennessee-specific in deal-analyzer with state reference table for top 10 states
- **"Substantial renovation" defined** in real-estate-comping: kitchen + 1 bath minimum (~$15K spend)

### Skill File Structure
```
skill-name.skill (ZIP containing):
├── SKILL.md              # Main skill instructions
├── references/            # Domain knowledge files
│   ├── *.md              # Reference documents
│   └── *.pdf             # SOPs, guides
└── scripts/              # Optional automation scripts
    └── *.py / *.js

plugin-name.plugin (ZIP containing):
├── .claude-plugin/
│   └── plugin.json       # Plugin manifest
├── commands/             # Slash commands
│   └── *.md
├── skills/
│   └── skill-name/
│       ├── SKILL.md
│       └── references/
└── README.md
```

## My Defaults

- **Primary markets:**
  - Richmond, Virginia
  - Henrico, Virginia
  - Chesterfield, Virginia
  - Prince William County, Virginia
  - Prince George's County, Maryland
  - Montgomery County, Maryland
- **Data source:** `app.propertyradar.com` (nationwide property data platform — replaces tnpublicnotice.com for VA/MD markets)
- **Notifications:** Send daily summaries to Slack
- **Preferred run time:** 5:00 AM
- **Dispositions:** Every record additively lands in DataSift List `SiftStack` (camel-case; the list pre-exists in DataSift) AND its per-notice-type list (Foreclosure / Pre-Probate / Probate / Tax Sale / Tax Delinquent / Eviction / Code Violation / Divorce). See `src/datasift_formatter.py::_build_lists_value` for the construction logic.
- **Buy-box filters:**
  - `include_vacant`: **true** (operator buys vacant land — do NOT default to excluding it)
  - `include_commercial`: `false` (residential-only)
  - `include_entities`: `false` (filter out LLC/Corp/Trust-owned records)

### PropertyRadar Architecture Notes

PropertyRadar is the active acquisition source. The four pre-configured
lists (see "PropertyRadar Lists" section above) auto-refresh daily on the
PR side; the puller's job is to pull **new records added since the last
successful run** and feed them through the existing enrichment + DataSift
upload pipeline.

**Account constraints:**
- We're on the Solo plan (10K monthly export quota). Tracked in `pr_quota.json` with 50/80/95/100% Slack alerts.
- No REST API access (that's the $599/mo Business plan). Transport is Playwright against the web UI.
- PR's web UI has **no Added-Date filter** server-side. Delta has to be computed client-side: read full list membership, diff RadarIDs vs `pr_state.json`, only export the new IDs.
- "Purchase" / "Export" both count against quota even for re-exports of the same list — never export the full list when the diff is empty.

**Coverage gaps in VA/MD:**
- Foreclosure ✓, Divorce ✓, Assessor/Recorder (incl. deceased-owner signal for `pre_probate`) ✓.
- Probate, Eviction, Code Violation — **NOT available** in VA/MD. Those are still photo-import notice types (`src/photo_importer.py`) sourced from county courthouse terminals.

**`pre_probate` vs `probate`:**
PR's deceased-owner signal is property-records based (no executor named, no court filing). The pipeline tags this as `pre_probate` to distinguish it from court-filed `probate` — the latter has a named PR/executor and triggers the obituary-enricher's "DM = named PR" preset; the former needs heir research because we only know the owner is dead.

**Enrichment overlap (post-migration):**
PR exports already include owner name, mailing address, equity, estimated value, parcel ID, foreclosure stage, and trustee details. That eliminates the need for Smarty/Zillow/county-tax bridges for VA/MD records (those steps still run but find the data already present and skip themselves). Trestle phone scoring still adds value; obituary/heir research stays in the loop for `pre_probate` records (since PR doesn't name the heirs).
