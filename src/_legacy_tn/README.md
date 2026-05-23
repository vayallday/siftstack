# `_legacy_tn/` — Archived TN Public-Notice Data Acquisition

This directory holds the **data-acquisition code** that previously pulled
public notices from `tnpublicnotice.com` (Knox + Blount counties, TN). It
was retired when SiftStack moved its primary markets to Virginia and
Maryland, where PropertyRadar is the source of truth.

Nothing under `_legacy_tn/` is imported by any active code path. The files
remain as documentation and as a working starting point if a similar
ASP.NET / reCAPTCHA-gated public-notice site ever needs to be wired up
again.

## What's here

| File | What it does |
|---|---|
| `tn_config.py` | TNPN credentials, URLs, selectors, `SAVED_SEARCHES` registry, state-file paths. Self-contained — none of these symbols live in top-level `config.py` anymore. |
| `scraper.py` | Playwright automation of tnpublicnotice.com (login, saved-search navigation, result pagination, view-notice clicks). |
| `captcha_solver.py` | 2Captcha integration. The TNPN reCAPTCHA v2 sitekey is wired in here. |
| `foreclosure_filter.py` | Title/phrase filter for the TN foreclosure saved searches. Most "Foreclosure" results on TNPN are non-foreclosures; this rejects them. |
| `tax_enricher.py` | Knox County tax-record enrichment via `knox-tn.mygovonline.com/api/v2`. Used to fill parcel ID, value, delinquency status. |
| `property_lookup.py` | Knox KGIS (Playwright scrape) + Blount TPAD (HTTP GET) lookups — used to find property addresses for probate decedents who had no address on the court filing. |

## What's NOT here (still at top level, framework-grade)

The framework that processes notices is state-agnostic and stayed at the
top of `src/`:

- `notice_parser.py` — the `NoticeData` dataclass is the core record type used by every puller (PropertyRadar, photo import, PDF import).
- `enrichment_pipeline.py` — orchestrates Smarty / Zillow / obituary / Tracerfy / Trestle / heir-verification steps. State-neutral.
- `obituary_enricher.py`, `ancestry_enricher.py`, `tracerfy_skip_tracer.py`, `phone_validator.py`, `address_standardizer.py`, `property_enricher.py` — generic enrichers.
- `photo_importer.py`, `pdf_importer.py`, `dropbox_watcher.py`, `image_utils.py` — courthouse-terminal photo OCR pipeline (works for any state's terminal).
- `datasift_*.py`, `slack_notifier.py`, `drive_uploader.py` — output side.
- `comp_analyzer.py`, `deal_analyzer.py`, `rehab_estimator.py`, `deep_prospector.py`, `lead_manager.py`, etc. — analysis tools.
- `propertyradar_*.py` — the current acquisition source (VA/MD).

## To revive for another state

You would copy this folder to `src/_legacy_<state>/`, swap the URLs and
selectors in `tn_config.py`, retarget the regex in `foreclosure_filter.py`
to that state's notice phrasing, replace the Knox-specific tax/parcel
lookup endpoints, and wire a new `--source <state>` branch into
`main.py`'s `_run_scrape_pipeline()` (currently only dispatches to
`propertyradar_puller`).
