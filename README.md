# SiftStack

Full-stack real estate investing operations platform built for [DataSift.ai](https://datasift.ai). Pulls distressed-property leads from four automated sources, standardizes everything through a 10-step enrichment pipeline, and pushes it directly into DataSift ready for niche sequential marketing.

**Requires a DataSift.ai account.** The enrichment and CRM layers work in any market; the acquisition feeds are currently wired for Virginia and Maryland.

## What It Does

**One pipeline, many inputs.** No matter how you get the data, it comes out the same way:

```
  PropertyRadar Lists         ──┐
  VA Public Notices (VPA)     ──┤
  Chesterfield ACA Violations ──┼──→  Enrichment Pipeline  ──→  DataSift Upload  ──→  Niche Sequential
  Richmond Vacant Building    ──┤     (10 steps)                (automated)          Marketing
  CSV Re-Import               ──┘
```

### Data Intake (5 methods)

| Method | How It Works | Use Case |
|--------|-------------|----------|
| **PropertyRadar** | Playwright automation of app.propertyradar.com; membership-diff against stored RadarIDs, then export only the new records | Foreclosure auctions + pre-probate (deceased-owner) signal across VA/MD |
| **VA Public Notice** | Playwright + 2Captcha against publicnoticevirginia.com Smart Search | Court probate (Estate Claims), trustee sales, tax deeds — 16 VA localities |
| **Chesterfield ACA** | Playwright drives the public Accela date-range report, parses the XLSX | Bulk code violations, filtered to high-motivation code sections |
| **Richmond Vacant** | Probes rva.gov for the newest Vacant Building List PDF, parses with pdfplumber | Vacancy registry for Richmond City |
| **CSV Re-Import** | Read existing data, re-enrich with latest APIs | Refresh stale records, merge datasets |

All five methods produce the same `NoticeData` records and flow through the same enrichment pipeline.

### Enrichment Pipeline (10 steps)

Every record gets the same treatment, regardless of source:

1. **Deduplicate** — same property in multiple notices? Keep the most recent
2. **Vacant Land Filter** — remove parcels with no house number
3. **Entity Filter** — flag LLC/Corp owners, research the person behind them
4. **Probate Property Lookup** — people search (Serper/Firecrawl + LLM) with DuckDuckGo fallback
5. **Tax Delinquency** — parcel lookup, delinquent years + amount
6. **Address Standardization** — Smarty USPS validation, ZIP+4, geocoding, vacancy detection
7. **Commercial Filter** — remove commercial properties (RDI check)
8. **Zillow Enrichment** — Zestimate, MLS status, equity estimate, property details
9. **Obituary Search** — deceased owner detection, heir identification, decision-maker ranking
10. **Data Validation** — verify required fields, compute mailable flag

### DataSift.ai Automation

SiftStack is purpose-built for the DataSift CRM. After enrichment, records are automatically:
- Formatted into 41-column DataSift CSV with tags, lists, and custom fields
- Uploaded to DataSift via Playwright browser automation (5-step wizard)
- Enriched with SiftMap property data (beds, baths, Zestimate)
- Skip traced for phone numbers and emails (DataSift unlimited plan)
- Routed into DataSift's niche sequential marketing campaigns (21 filter presets, 26 TCA sequences)

### Deal Analysis Tools

CLI tools for evaluating deals on the fly:

- **Comp Analysis** — Two-Bucket ARV with 7-tab Excel workbook
- **Rehab Estimator** — 4-tier room-by-room cost estimation
- **Deal Analyzer** — MAO calculation, financing scenarios (HML/conventional), ROI projections
- **Market Analysis** — Zip code scoring with 6 weighted factors
- **Buyer Prospecting** — Cash buyer identification from county records
- **Deep Prospecting** — 4-level research depth with PDF reports

## Quick Start

```bash
# Clone and install
git clone https://github.com/YOUR_USERNAME/SiftStack.git
cd SiftStack
pip install -r requirements.txt
playwright install chromium

# Configure credentials
cp .env.example .env
# Edit .env with your API keys (see Configuration below)

# Pull today's new PropertyRadar records
python src/main.py daily

# Or pull Virginia public notices (probate / foreclosure / tax deed)
python src/main.py va-public-notice

# Or pull Chesterfield code violations
python src/main.py chesterfield-code-violation

# Full automated pipeline (pull + enrich + upload to DataSift + notify Slack)
python src/main.py daily --upload-datasift --notify-slack
```

## Adapting to Your Market

SiftStack's acquisition feeds are wired for Virginia and Maryland, but the enrichment and CRM layers are market-agnostic:

1. **PropertyRadar Lists** — Edit `PROPERTYRADAR_LISTS` in `src/propertyradar_config.py`. PropertyRadar covers all 50 states, so retargeting is a matter of building the lists in their UI and registering the names here
2. **Notice Parser** — `src/notice_parser.py` defines the `NoticeData` contract shared by every source, and handles 9 notice types
3. **Public Notice Feeds** — The VA puller targets the Virginia Press Association platform. Many states run the same ASP.NET "Smart Search" software, so `src/va_public_notice_puller.py` is a workable template
4. **County Portals** — The Chesterfield (Accela) and Richmond (Tyler EnerGov) integrations are vendor-specific; other counties on the same vendors need only new tenant IDs and selectors
5. **DataSift Presets** — The 23 filter presets and 26 sequence templates are reusable across markets

The enrichment pipeline (Smarty, Zillow, obituary search, skip trace) works nationwide — no market-specific configuration needed.

## Buy Box Configuration

By default, SiftStack filters out property types that don't fit a typical residential wholesaling buy box. **Your strategy may be different.** Use these flags to match your buy box:

### Property Type Filters

| Flag | Default | What It Does |
|------|---------|-------------|
| `--include-vacant` | OFF (removed) | Keep vacant land parcels. Turn ON for land deals, subdivisions, infill lots. |
| `--include-commercial` | OFF (removed) | Keep commercial properties. Turn ON for commercial investing, mixed-use, multifamily. |
| `--include-entities` | OFF (removed) | Keep LLC/Corp/Trust-owned records. Turn ON if you market to entity owners directly. |

### Examples

```bash
# Default — residential only, no vacant land, no commercial, no entities
python src/main.py daily --upload-datasift

# Land investor — keep vacant parcels
python src/main.py daily --include-vacant --upload-datasift

# Commercial investor — keep everything
python src/main.py daily --include-vacant --include-commercial --include-entities --upload-datasift

# Entity researcher — keep entities AND research the person behind them
python src/main.py daily --include-entities --research-entities --upload-datasift
```

### Apify Actor (Cloud)

The same toggles are available in the Apify Console under your Actor's input configuration:
- **Include Vacant Land** — checkbox
- **Include Commercial Properties** — checkbox
- **Include Entity-Owned Properties** — checkbox

These apply to every scheduled run.

## Configuration

### Required (for web scraping)
| Variable | Service | Cost |
|----------|---------|------|
| `TNPN_EMAIL` / `TNPN_PASSWORD` | Your state's public notice site | Free account |
| `CAPTCHA_API_KEY` | [2Captcha](https://2captcha.com) | ~$3/1,000 solves |

### Enrichment APIs (optional, pipeline degrades gracefully)
| Variable | Service | Cost | What It Adds |
|----------|---------|------|-------------|
| `SMARTY_AUTH_ID` / `TOKEN` | [Smarty](https://smarty.com) | 250 free/month | USPS validation, geocoding, vacancy |
| `OPENWEBNINJA_API_KEY` | [OpenWeb Ninja](https://openwebninja.com) | 100 free/month | Zestimate, MLS, equity |
| `ANTHROPIC_API_KEY` | [Anthropic](https://console.anthropic.com) | ~$0.001/record | LLM parsing, obituary search |
| `TRACERFY_API_KEY` | [Tracerfy](https://tracerfy.com) | $0.02/record | Phones, emails, mailing addresses |
| `TRESTLE_API_KEY` | [Trestle](https://trestleiq.com) | $0.015/phone | Phone scoring (5-tier dial priority) |

### DataSift + Notifications (required for full pipeline)
| Variable | Service | What It Does |
|----------|---------|-------------|
| `DATASIFT_EMAIL` / `PASSWORD` | [DataSift.ai](https://datasift.ai) | Auto-upload + SiftMap enrich + skip trace |
| `SLACK_WEBHOOK_URL` | Slack / Discord | Daily summaries + error alerts |

### All Intake Methods (optional)
| Variable | Service | What It Does |
|----------|---------|-------------|
| `PROPERTYRADAR_EMAIL` / `PASSWORD` | [PropertyRadar](https://propertyradar.com) | Foreclosure + pre-probate list pulls |
| `VAPN_EMAIL` / `VAPN_PASSWORD` | [Virginia Public Notice](https://www.publicnoticevirginia.com) | Free Smart Search account for full notice text |
| `CAPTCHA_API_KEY` | [2Captcha](https://2captcha.com) | reCAPTCHA solving on VA notice detail pages |
| `ANCESTRY_EMAIL` / `PASSWORD` | [Ancestry.com](https://ancestry.com) | SSDI + obituary collection |

Every API is optional. Missing a key? That enrichment step is skipped and the pipeline continues.

## CLI Commands

```bash
# ── Data Acquisition ────────────────────────────────────────────
python src/main.py daily                    # New PropertyRadar records since last run
python src/main.py historical               # Same flow (delta is a membership diff)
python src/main.py va-public-notice         # VA probate / foreclosure / tax deed notices
python src/main.py chesterfield-code-violation   # Chesterfield ACA bulk report
python src/main.py richmond-vacant          # Richmond Vacant Building List
python src/main.py csv-import --csv-path FILE

# ── Deal Analysis ───────────────────────────────────────────────
python src/main.py comp --address "123 Main St"
python src/main.py rehab --address "123 Main St" --tier 2
python src/main.py analyze-deal --address "123 Main St" --purchase-price 150000
python src/main.py market-analysis --counties Henrico
python src/main.py buyer-prospect --counties Henrico
python src/main.py deep-prospect --csv-path output/records.csv --depth 3

# ── CRM Operations ─────────────────────────────────────────────
python src/main.py manage-presets --discover
python src/main.py manage-sold --months-back 12
python src/main.py phone-validate --list-name "Foreclosure"
python src/main.py lead-manage --lead-action qualify

# ── Workflow Tools ──────────────────────────────────────────────
python src/main.py setup-sequences --dry-run
python src/main.py niche-sequential --channel sms --day 1
python src/main.py playbook --blueprint wholesale --market knoxville
```

### Common Flags
```bash
--upload-datasift          # Upload results to DataSift.ai
--notify-slack             # Send run summary to Slack/Discord
--skip-smarty              # Skip address standardization
--skip-zillow              # Skip Zillow enrichment
--skip-obituary            # Skip deceased owner detection
--split                    # Separate CSV per county + notice type
--verbose / -v             # Debug logging
```

## Cloud Deployment (Apify)

SiftStack runs as an [Apify Actor](https://apify.com) for scheduled daily automation:

```bash
# Install Apify CLI
npm install -g apify-cli

# Deploy
apify login
apify push

# Configure schedule + secrets in Apify Console
# The Actor runs the full pipeline: scrape → enrich → skip trace → DataSift upload → Slack notify
```

## Architecture

```
src/
├── main.py                  # CLI entry + Apify Actor entry
├── propertyradar_puller.py  # PropertyRadar lists: scrape → diff → export
├── propertyradar_config.py  # Locked 4-list registry + selectors
├── propertyradar_parser.py  # PR CSV → NoticeData
├── propertyradar_quota.py   # 10K/month export quota tracker
├── va_public_notice_puller.py   # VPA Smart Search + captcha + LLM parse
├── chesterfield_aca_puller.py   # Accela date-range report → XLSX
├── richmond_vacant_puller.py    # rva.gov Vacant Building List PDF
├── richmond_opp_enricher.py     # Tyler EnerGov per-address code cases
├── captcha_solver.py        # 2Captcha reCAPTCHA v2 integration
├── notice_parser.py         # NoticeData contract + 9 notice types
├── llm_parser.py            # Claude Haiku structured extraction
├── llm_client.py            # Multi-backend LLM (Anthropic/Ollama/OpenRouter)
├── enrichment_pipeline.py   # 10-step canonical pipeline
├── address_standardizer.py  # Smarty USPS batch standardization
├── property_enricher.py     # Zillow via OpenWeb Ninja API
├── obituary_enricher.py     # Deceased detection + heir identification
├── ancestry_enricher.py     # Ancestry.com SSDI automation
├── entity_researcher.py     # LLC/Corp person extraction
├── tracerfy_skip_tracer.py  # Batch skip trace (phones + emails)
├── phone_validator.py       # Trestle 5-tier scoring
├── data_formatter.py        # CSV dedup + export
├── datasift_formatter.py    # 41-column DataSift CSV builder
├── datasift_uploader.py     # Full DataSift automation (Playwright)
├── comp_analyzer.py         # Two-Bucket ARV analysis
├── deal_analyzer.py         # MAO/ROI/financing scenarios
├── rehab_estimator.py       # 4-tier room-by-room costs
├── market_analyzer.py       # Zip code scoring (6 weights)
├── buyer_prospector.py      # Cash buyer identification
├── deep_prospector.py       # 4-level research + PDF reports
├── lead_manager.py          # 4 Pillars qualification + STABM
├── sequence_templates.py    # 26 TCA sequence definitions
├── niche_sequential.py      # Filter preset orchestration
├── playbook_generator.py    # SOP/script/checklist generator
├── report_generator.py      # PDF deep prospecting reports
├── excel_exporter.py        # Multi-sheet Excel workbooks
├── drive_uploader.py        # Google Drive upload
├── slack_notifier.py        # Slack/Discord notifications
└── config.py                # Environment config + constants
```

## Notice Types Supported

| Type | Source | What to Look For |
|------|--------|-----------------|
| Foreclosure | PropertyRadar, VA Public Notice | Trustee sale, deed of trust default |
| Pre-Probate | PropertyRadar | Deceased owner per assessor data — no court filing yet |
| Probate | VA Public Notice (Estate Claims) | Estate administration, executor appointment |
| Tax Sale | VA Public Notice | Delinquent property tax auction |
| Code Violation | Chesterfield ACA | Building code, compliance deadline |
| Vacant Building | Richmond Vacant Building List | Registered vacant structure |
| Tax Delinquent | *no active feed* | Tax lien, unpaid property taxes |
| Eviction | *no active feed* | Landlord-tenant, detainer warrant |
| Divorce | *no active feed* | Property division, marital assets |

## API Cost Estimates

Running daily across the VA/MD buy box (~20-40 new records/day):

| Service | Monthly Cost | What It Does |
|---------|-------------|-------------|
| 2Captcha | ~$3 | CAPTCHA solving (~30 notices × 30 days) |
| Smarty | Free | 250 lookups/month covers daily runs |
| OpenWeb Ninja | Free | 100 lookups/month covers most records |
| Anthropic (Haiku) | ~$2 | LLM parsing + obituary search |
| Tracerfy | ~$20 | Skip trace @ $0.02/record |
| Trestle | ~$15 | Phone scoring @ $0.015/phone |
| **Total** | **~$40/month** | Full pipeline, one county |

Requires a [DataSift.ai](https://datasift.ai) subscription ($97/month for unlimited skip trace plan).

## REI Skill Library

13 Claude Co-Work skill files for the DataSift community, distributed at [learn.datasift.ai/claude-skills-rei](https://learn.datasift.ai/claude-skills-rei). Each skill teaches Claude a specific REI workflow:

| Skill | What It Does |
|-------|-------------|
| Market Research | Zip code scoring, Market Finder reports |
| County Data | Notice extraction for all 7 types |
| Buyer Prospector | Cash buyer identification from public records |
| Real Estate Comping | Two-Bucket ARV with adjustment tables |
| Rehab Estimator | 4-tier room-by-room cost estimation |
| Deal Analyzer | Combined comp + rehab + MAO + financing |
| Deep Prospecting | 4-level heir research framework |
| Probate Property Finder | 3-tier property lookup for decedents |
| Phone Validator | Trestle scoring with dial priority tiers |
| Sequential Presets | 21 niche marketing filter presets |
| Sift Sequences | 26 TCA automation templates |
| Sift Operations | CRM operations encyclopedia |
| Playbook Creator | SOP generator from transcripts |

## License

MIT License. See [LICENSE](LICENSE) for details.

## Contributing

1. Fork the repo
2. Create a feature branch (`git checkout -b feature/your-market`)
3. Commit your changes
4. Push to the branch (`git push origin feature/your-market`)
5. Open a Pull Request

Built by [DataSift.ai](https://datasift.ai) for the REI community.
