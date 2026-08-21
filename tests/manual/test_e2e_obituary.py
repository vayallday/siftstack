"""End-to-end test: obituary enrichment on 5100 Stokely Ln (known deceased owner)."""

import os
import sys
import logging

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(message)s")

import config
from notice_parser import NoticeData
from obituary_enricher import enrich_obituary_data

# Build a minimal NoticeData matching the 5100 Stokely Ln record
notice = NoticeData(
    address="5100 Stokely Ln",
    city="Knoxville",
    state="TN",
    zip="37918",
    county="Knox",
    notice_type="tax_sale",
    parcel_id="048DE017",
    tax_owner_name="WILLIAMS DANIEL H",
)

print(f"Testing obituary enrichment for: {notice.tax_owner_name}")
print(f"Address: {notice.address}, {notice.city}, {notice.state} {notice.zip}")
print()

api_key = config.ANTHROPIC_API_KEY
if not api_key:
    print("ERROR: No ANTHROPIC_API_KEY configured in .env")
    sys.exit(1)

enrich_obituary_data([notice], api_key)

print()
print("=" * 60)
print("RESULTS:")
print(f"  owner_deceased:              {notice.owner_deceased!r}")
print(f"  date_of_death:               {notice.date_of_death!r}")
print(f"  obituary_url:                {notice.obituary_url!r}")
print(f"  decision_maker_name:         {notice.decision_maker_name!r}")
print(f"  decision_maker_relationship: {notice.decision_maker_relationship!r}")
print("=" * 60)

# Validate expectations
ok = True
if notice.owner_deceased != "yes":
    print("FAIL: owner_deceased should be 'yes'")
    ok = False
if not notice.date_of_death:
    print("FAIL: date_of_death should not be empty")
    ok = False
if "dignitymemorial" not in (notice.obituary_url or ""):
    print(f"WARN: expected dignitymemorial.com URL, got: {notice.obituary_url}")
# Decision-maker is a bonus — only available when full page is fetched (not snippet fallback)
if notice.decision_maker_name:
    print(f"BONUS: decision_maker identified: {notice.decision_maker_name} ({notice.decision_maker_relationship})")
else:
    print("NOTE: decision_maker empty (expected when using snippet fallback — full page was blocked)")

if ok:
    print("\nPASS — Deceased owner correctly identified")
else:
    print("\nFAIL — Core fields missing")
    sys.exit(1)
