"""End-to-end test: single record through full pipeline with real APIs.

Runs Daniel H. Williams through:
  1. Obituary enrichment (signing chain + heir addresses)
  2. Tracerfy batch skip trace (real API, ~$0.06)
  3. Trestle phone validation (real API, ~$0.08-0.23)

Usage:
    python test_e2e_record.py
"""
import json, sys, os, logging

src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
sys.path.insert(0, src_dir)
os.chdir(src_dir)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("e2e_test")

from notice_parser import NoticeData
from obituary_enricher import enrich_obituary_data
from tracerfy_skip_tracer import batch_skip_trace
from phone_validator import clean_phone, process_phones, DEFAULT_TIERS, assign_tier
import config as cfg

# ── Step 0: Create test record ──────────────────────────────────────────
notice = NoticeData(
    owner_name='Daniel H. Williams',
    address='5100 Stokely Ln',
    city='Knoxville',
    state='TN',
    zip='37918',
    county='Knox',
    notice_type='foreclosure',
    source_url='https://www.tnpublicnotice.com/notice/123',
    date_added='2025-11-01',
    parcel_id='082GA007',
    tax_owner_name='WILLIAMS DANIEL H & MARY K',
    estimated_value='285000',
    estimated_equity='142000',
    equity_percent='49.8',
    property_type='Single Family',
    bedrooms='3',
    bathrooms='2',
    year_built='1975',
    sqft='1650',
)

# ── Step 1: Obituary enrichment (signing chain + heir addresses) ────────
print("=" * 70)
print("STEP 1: Obituary Enrichment (DuckDuckGo + Claude Haiku)")
print("=" * 70)
enrich_obituary_data(
    [notice],
    api_key=cfg.ANTHROPIC_API_KEY,
    skip_ancestry=True,
)
print(f"  Owner deceased: {notice.owner_deceased}")
print(f"  Decision maker: {notice.decision_maker_name} ({notice.decision_maker_relationship})")
print(f"  Signing chain:  {notice.signing_chain_count} heirs — {notice.signing_chain_names}")
print()

# ── Step 2: Tracerfy batch skip trace ───────────────────────────────────
print("=" * 70)
print("STEP 2: Tracerfy Batch Skip Trace (real API)")
print("=" * 70)
if not cfg.TRACERFY_API_KEY:
    print("  SKIP — TRACERFY_API_KEY not set")
    tracerfy_stats = {"submitted": 0, "matched": 0, "phones_found": 0, "emails_found": 0, "cost": 0}
else:
    tracerfy_stats = batch_skip_trace([notice], max_signing_traces=5)
    print(f"  Submitted: {tracerfy_stats['submitted']} contacts")
    print(f"  Matched:   {tracerfy_stats['matched']}")
    print(f"  Phones:    {tracerfy_stats['phones_found']}")
    print(f"  Emails:    {tracerfy_stats['emails_found']}")
    print(f"  Cost:      ${tracerfy_stats['cost']:.2f}")
print()

# ── Step 3: Collect all phones for Trestle validation ───────────────────
all_phones = []  # (raw_phone, heir_name)

# DM #1 flat fields
for field in ["primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
              "mobile_5", "landline_1", "landline_2", "landline_3"]:
    val = getattr(notice, field, "")
    if val:
        all_phones.append((val, notice.decision_maker_name or notice.owner_name))

# Heir map phones
if notice.heir_map_json:
    try:
        heirs = json.loads(notice.heir_map_json)
        for h in heirs:
            for ph in h.get("phones", []):
                if ph:
                    all_phones.append((ph, h.get("name", "?")))
    except (json.JSONDecodeError, TypeError):
        pass

# Deduplicate by cleaned number
seen = set()
unique_phones = []
for raw, name in all_phones:
    cleaned = clean_phone(raw)
    if cleaned and cleaned not in seen:
        seen.add(cleaned)
        unique_phones.append((raw, cleaned, name))

print("=" * 70)
print("STEP 3: Trestle Phone Validation (real API)")
print("=" * 70)
print(f"  Unique phones to validate: {len(unique_phones)}")

trestle_results = []
trestle_cost = 0.0
if not cfg.TRESTLE_API_KEY:
    print("  SKIP — TRESTLE_API_KEY not set")
elif unique_phones:
    phone_tuples = [(raw, cleaned) for raw, cleaned, _ in unique_phones]
    results, errors = process_phones(phone_tuples, cfg.TRESTLE_API_KEY, batch_size=5)
    trestle_results = results
    trestle_cost = len(results) * 0.015
    print(f"  Validated: {len(results)} phones")
    print(f"  Errors:    {len(errors)}")
    print(f"  Cost:      ${trestle_cost:.2f}")
else:
    print("  No phones to validate")
print()

# Build phone → tier lookup
phone_tiers = {}
for tr in trestle_results:
    cleaned = tr.get("phone_number", "")
    score = tr.get("activity_score")
    tier = tr.get("assigned_tag", assign_tier(score, DEFAULT_TIERS))
    phone_tiers[cleaned] = {"score": score, "tier": tier, "line_type": tr.get("line_type", "?")}

# ── Final Report ────────────────────────────────────────────────────────
print("=" * 70)
print("COMPLETE E2E RECORD REPORT")
print("=" * 70)
print()

r = notice

sections = {
    "PROPERTY": ["address", "city", "state", "zip", "county", "parcel_id",
                  "property_type", "bedrooms", "bathrooms", "sqft", "year_built"],
    "NOTICE": ["notice_type", "date_added", "source_url", "owner_name"],
    "VALUATION": ["estimated_value", "estimated_equity", "equity_percent"],
    "DECEASED OWNER": ["owner_deceased", "date_of_death", "obituary_url",
                       "obituary_source_type", "deceased_indicator"],
    "DECISION MAKER": ["decision_maker_name", "decision_maker_relationship",
                       "decision_maker_status", "decision_maker_source",
                       "decision_maker_street", "decision_maker_city",
                       "decision_maker_state", "decision_maker_zip"],
    "SIGNING CHAIN": ["signing_chain_count", "signing_chain_names"],
    "CONFIDENCE": ["dm_confidence", "dm_confidence_reason"],
}

for section_name, fields in sections.items():
    print(f"--- {section_name} ---")
    for fname in fields:
        val = getattr(r, fname, "")
        label = f"  {fname:35s}"
        print(f"{label} = {val}" if val else f"{label} = (empty)")
    print()

# ── Signing Chain with Contact Info ─────────────────────────────────────
print("=" * 70)
print("SIGNING CHAIN — CONTACT INFO FOR DEAL CLOSING")
print("=" * 70)

if r.heir_map_json:
    heirs = json.loads(r.heir_map_json)
    signers = [h for h in heirs if h.get("signing_authority") and h.get("status") != "deceased"]
    non_signers = [h for h in heirs if not h.get("signing_authority") or h.get("status") == "deceased"]

    if signers:
        for i, h in enumerate(signers, 1):
            status = "ALIVE" if h.get("status") == "verified_living" else h.get("status", "?").upper()
            phones = h.get("phones", [])
            # DM #1 phones come from flat fields
            if i == 1 and not phones:
                for field in ["primary_phone", "mobile_1", "mobile_2", "mobile_3"]:
                    val = getattr(r, field, "")
                    if val:
                        phones.append(val)

            print(f"\n  #{i} {h.get('name', '?')} ({h.get('relationship', '?')})")
            print(f"     Status:          {status}")
            print(f"     Signing Auth:    YES")

            if h.get("street"):
                addr = f"{h['street']}, {h.get('city','')}, {h.get('state','')} {h.get('zip','')}"
                print(f"     Address:         {addr}")

            if phones:
                for j, ph in enumerate(phones):
                    cleaned = clean_phone(ph)
                    tier_info = phone_tiers.get(cleaned, {})
                    tier = tier_info.get("tier", "—")
                    score = tier_info.get("score", "—")
                    line_type = tier_info.get("line_type", "—")
                    print(f"     Phone {j+1}:         {ph}  [{tier}, score={score}, {line_type}]")
            else:
                print(f"     Phone:           (none found)")

            emails = h.get("emails", [])
            if emails:
                for j, em in enumerate(emails):
                    print(f"     Email {j+1}:         {em}")
    else:
        print("  (no signing chain identified)")

    if non_signers:
        print(f"\n\n--- OTHER FAMILY ({len(non_signers)} — no signing authority) ---")
        for h in non_signers:
            status = "living" if h.get("status") == "verified_living" else h.get("status", "?")
            print(f"  {h.get('name', '?'):30s} ({h.get('relationship', '?'):15s}) [{status}]")
else:
    print("  (no heir map generated)")

# ── Cost Summary ────────────────────────────────────────────────────────
print()
print("=" * 70)
print("COST SUMMARY")
print("=" * 70)
print(f"  Tracerfy skip trace:   ${tracerfy_stats.get('cost', 0):.2f} ({tracerfy_stats.get('submitted', 0)} contacts)")
print(f"  Trestle validation:    ${trestle_cost:.2f} ({len(unique_phones)} phones)")
print(f"  Haiku API (obituary):  ~$0.01")
total = tracerfy_stats.get('cost', 0) + trestle_cost + 0.01
print(f"  TOTAL:                 ~${total:.2f}")
print()

# ── Step 4: Generate PDF Report ─────────────────────────────────────────
from report_generator import generate_record_pdf

print("=" * 70)
print("STEP 4: PDF Report Generation")
print("=" * 70)
pdf_path = generate_record_pdf(notice, phone_tiers=phone_tiers)
print(f"  PDF saved to: {pdf_path}")
print(f"  File size:    {pdf_path.stat().st_size:,} bytes")
print()

# Optional: Google Drive upload (if credentials available via env vars)
drive_folder_id = os.getenv("GOOGLE_DRIVE_FOLDER_ID", "")
drive_key_b64 = os.getenv("GOOGLE_SERVICE_ACCOUNT_KEY", "")
if drive_folder_id and drive_key_b64:
    from drive_uploader import upload_file
    print("  Uploading to Google Drive...")
    link = upload_file(pdf_path, drive_folder_id, drive_key_b64)
    if link:
        notice.report_url = link
        print(f"  Drive link:   {link}")
    else:
        print("  Drive upload failed")
else:
    print("  (GOOGLE_DRIVE_FOLDER_ID / GOOGLE_SERVICE_ACCOUNT_KEY not set — skipping upload)")
print()
