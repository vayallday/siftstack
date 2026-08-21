"""Deep prospecting: Scott Robert Smyth — full pipeline with real APIs."""
import json, sys, os, logging

src_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'src')
sys.path.insert(0, src_dir)
os.chdir(src_dir)

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("e2e_smyth")

from notice_parser import NoticeData
from obituary_enricher import enrich_obituary_data
from tracerfy_skip_tracer import batch_skip_trace
from phone_validator import clean_phone, process_phones, DEFAULT_TIERS, assign_tier
from report_generator import generate_record_pdf
import config as cfg


def _print_phones(phones, pt):
    """Print phone numbers with Trestle tier info."""
    if phones:
        for j, ph in enumerate(phones, 1):
            cleaned = clean_phone(ph)
            info = pt.get(cleaned, {})
            tier = info.get("tier", "—")
            score = info.get("score", "—")
            line_type = info.get("line_type", "—")
            print(f"     Phone {j}:          {ph}  [{tier}, score={score}, {line_type}]")
    else:
        print(f"     Phone:           (none found)")


# ── Step 0: Create record ───────────────────────────────────────────────
notice = NoticeData(
    owner_name='Scott Robert Smyth',
    address='16255 Allura Cir Unit 3308',
    city='Naples',
    state='FL',
    zip='34110',
    county='Collier',
    notice_type='research',
    source_url='',
    date_added='2026-04-06',
)

# ── Step 1: Obituary / family search (FL — manual DDG + LLM) ───────────
print("=" * 70)
print("STEP 1: Obituary & Family Search — Scott Robert Smyth (Florida)")
print("=" * 70)

from ddgs import DDGS
import anthropic
import requests as req_lib
from bs4 import BeautifulSoup

# Search DDG for obituary/family info across FL
search_queries = [
    '"Scott Robert Smyth" obituary Florida',
    '"Scott Robert Smyth" obituary Naples Florida',
    '"Scott Smyth" obituary Naples FL',
    '"Scott Smyth" Naples Florida',
]

all_results = []
for q in search_queries:
    try:
        hits = DDGS().text(q, max_results=10, backend="google,duckduckgo,brave")
        all_results.extend(hits or [])
        print(f"  Search: {q} -> {len(hits or [])} results")
    except Exception as e:
        print(f"  Search failed: {q} -> {e}")

# Deduplicate by URL
seen_urls = set()
unique_results = []
for r in all_results:
    url = r.get("href", "")
    if url and url not in seen_urls:
        seen_urls.add(url)
        unique_results.append(r)

print(f"  Total unique results: {len(unique_results)}")

# Find obituary-domain results
obit_domains = ["legacy.com", "dignitymemorial.com", "tributearchive.com",
                "echovita.com", "obituaries.com", "findagrave.com",
                "funeralhomes.com", "meaningfulfunerals.net"]
obit_results = [r for r in unique_results
                if any(d in r.get("href", "") for d in obit_domains)]
print(f"  Obituary-domain results: {len(obit_results)}")

# Try to fetch full page from top obituary result
obit_text = ""
obit_url = ""
for r in (obit_results or unique_results[:3]):
    url = r.get("href", "")
    try:
        resp = req_lib.get(url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code == 200:
            soup = BeautifulSoup(resp.text, "html.parser")
            text = soup.get_text(separator="\n", strip=True)
            if len(text) > 200 and ("survived" in text.lower() or "preceded" in text.lower()
                                     or "obituary" in text.lower()):
                obit_text = text[:8000]
                obit_url = url
                print(f"  Fetched obituary page: {url}")
                break
    except Exception:
        continue

# If we got obituary text, send to Claude for family extraction
if obit_text:
    client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
    family_prompt = f"""Analyze this obituary page for "Scott Robert Smyth" (or "Scott Smyth").

Obituary text:
{obit_text}

Return a JSON object with:
- "is_match": true/false — does this obituary match Scott Robert Smyth from Naples, FL?
- "date_of_death": "YYYY-MM-DD" or ""
- "survivors": list of {{"name": "Full Name", "relationship": "wife/son/daughter/etc."}}
- "preceded_by": list of {{"name": "Full Name", "relationship": "..."}} (deceased family)

Be thorough — extract ALL named family members. Return ONLY valid JSON."""

    resp = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2000,
        messages=[{"role": "user", "content": family_prompt}],
    )
    llm_text = resp.content[0].text.strip()

    # Parse JSON from response
    try:
        # Strip markdown code fences if present
        if llm_text.startswith("```"):
            llm_text = llm_text.split("```")[1]
            if llm_text.startswith("json"):
                llm_text = llm_text[4:]
        family_data = json.loads(llm_text)
    except json.JSONDecodeError:
        family_data = {"is_match": False, "survivors": [], "preceded_by": []}

    if family_data.get("is_match"):
        notice.owner_deceased = "yes"
        notice.date_of_death = family_data.get("date_of_death", "")
        notice.obituary_url = obit_url
        notice.obituary_source_type = "full_page"

        # Build heir map from survivors
        survivors = family_data.get("survivors", [])
        preceded = family_data.get("preceded_by", [])

        heir_list = []
        for s in survivors:
            heir_list.append({
                "name": s.get("name", ""),
                "relationship": s.get("relationship", ""),
                "status": "unverified",
                "source": "obituary_survivors",
                "signing_authority": False,  # Will classify below
            })
        for p in preceded:
            heir_list.append({
                "name": p.get("name", ""),
                "relationship": p.get("relationship", ""),
                "status": "deceased",
                "source": "obituary_preceded",
                "signing_authority": False,
            })

        # Classify signing authority using the existing function
        from obituary_enricher import rank_decision_makers
        ranked = rank_decision_makers(
            survivors=survivors,
            executor_name="",
            heir_statuses={h["name"]: h["status"] for h in heir_list},
        )
        notice.heir_map_json = json.dumps(ranked, ensure_ascii=False)

        # Set DM from ranked list
        living_signers = [h for h in ranked
                          if h.get("signing_authority") and h.get("status") != "deceased"]
        if living_signers:
            dm = living_signers[0]
            notice.decision_maker_name = dm["name"]
            notice.decision_maker_relationship = dm["relationship"]
            notice.decision_maker_status = dm.get("status", "unverified")
            notice.decision_maker_source = "obituary_survivors"

        signers = [h for h in ranked if h.get("signing_authority") and h.get("status") != "deceased"]
        notice.signing_chain_count = str(len(signers)) if signers else ""
        notice.signing_chain_names = ", ".join(h["name"] for h in signers) if signers else ""

        notice.dm_confidence = "medium"
        notice.dm_confidence_reason = "Obituary found via web search (FL — outside TN pipeline)"

        print(f"  DECEASED confirmed: DOD {notice.date_of_death}")
        print(f"  Survivors: {len(survivors)}")
        print(f"  Preceded by: {len(preceded)}")
        print(f"  Decision maker: {notice.decision_maker_name} ({notice.decision_maker_relationship})")
        print(f"  Signing chain: {notice.signing_chain_count} heirs — {notice.signing_chain_names}")
    else:
        print("  LLM says no obituary match — treating as LIVING")
else:
    # No obituary found — do a broader people search
    print("  No obituary found — Scott Robert Smyth appears to be LIVING")
    print("  Running people search for family/associates...")

    # Use LLM on search snippets for any family info
    snippets = "\n".join([
        f"- {r.get('title', '')}: {r.get('body', '')}" for r in unique_results[:15]
    ])
    if snippets:
        client = anthropic.Anthropic(api_key=cfg.ANTHROPIC_API_KEY)
        resp = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": f"""From these search results about "Scott Robert Smyth" in Naples, FL,
extract any family members, associates, or relatives mentioned.

Search results:
{snippets}

Return JSON: {{"family": [{{"name": "...", "relationship": "..."}}], "notes": "any relevant info"}}
Return ONLY valid JSON."""}],
        )
        llm_text = resp.content[0].text.strip()
        try:
            if llm_text.startswith("```"):
                llm_text = llm_text.split("```")[1]
                if llm_text.startswith("json"):
                    llm_text = llm_text[4:]
            people_data = json.loads(llm_text)
            family = people_data.get("family", [])
            if family:
                print(f"  Found {len(family)} family/associates from search results:")
                for f in family:
                    print(f"    - {f.get('name', '?')} ({f.get('relationship', '?')})")
            notes = people_data.get("notes", "")
            if notes:
                print(f"  Notes: {notes}")
        except json.JSONDecodeError:
            print("  Could not parse family data from search results")
print()

# ── Step 2: Tracerfy Instant Trace (single lookup, $0.10) ───────────────
print("=" * 70)
print("STEP 2: Tracerfy Instant Trace (real API — $0.10/hit)")
print("=" * 70)
tracerfy_stats = {"submitted": 1, "matched": 0, "phones_found": 0, "emails_found": 0, "cost": 0}

if not cfg.TRACERFY_API_KEY:
    print("  SKIP — TRACERFY_API_KEY not set")
else:
    import requests as http_req
    PHONE_FIELDS_T = [
        "primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
        "mobile_5", "landline_1", "landline_2", "landline_3",
    ]
    EMAIL_FIELDS_T = ["email_1", "email_2", "email_3", "email_4", "email_5"]

    # Also trace signing chain heirs if deceased
    trace_targets = [("Scott", "Smyth", notice.address, notice.city, notice.state, notice.zip, notice.owner_name)]

    if notice.owner_deceased == "yes" and notice.heir_map_json:
        heirs = json.loads(notice.heir_map_json)
        for h in heirs:
            if h.get("signing_authority") and h.get("status") != "deceased":
                parts = h["name"].strip().split()
                if len(parts) >= 2:
                    trace_targets.append((
                        parts[0], parts[-1],
                        h.get("street", notice.address),
                        h.get("city", notice.city),
                        h.get("state", notice.state),
                        h.get("zip", notice.zip),
                        h["name"],
                    ))

    tracerfy_stats["submitted"] = len(trace_targets)
    for first, last, addr, city_t, state_t, zip_t, full_name in trace_targets:
        try:
            resp = http_req.post(
                "https://tracerfy.com/v1/api/trace/lookup/",
                headers={
                    "Authorization": f"Bearer {cfg.TRACERFY_API_KEY}",
                    "Content-Type": "application/json",
                },
                json={
                    "first_name": first,
                    "last_name": last,
                    "address": addr,
                    "city": city_t,
                    "state": state_t,
                    "zip": zip_t,
                    "find_owner": False,
                },
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            if data.get("hit") and data.get("persons"):
                person = data["persons"][0]
                tracerfy_stats["matched"] += 1

                # Extract phones
                phones = []
                for pf in PHONE_FIELDS_T:
                    val = (person.get(pf) or "").strip()
                    if val:
                        phones.append(val)
                tracerfy_stats["phones_found"] += len(phones)

                # Extract emails
                emails = []
                for ef in EMAIL_FIELDS_T:
                    val = (person.get(ef) or "").strip()
                    if val:
                        emails.append(val)
                tracerfy_stats["emails_found"] += len(emails)

                # Is this the main subject or an heir?
                if full_name == notice.owner_name or notice.owner_deceased != "yes":
                    # Populate flat fields
                    for i, pf in enumerate(PHONE_FIELDS_T):
                        if i < len(phones):
                            setattr(notice, pf, phones[i])
                    for i, ef in enumerate(EMAIL_FIELDS_T):
                        if i < len(emails):
                            setattr(notice, ef, emails[i])

                    # Also grab mailing address from Tracerfy
                    mail = person.get("mailing_address") or {}
                    if mail.get("street"):
                        print(f"  Mailing address: {mail.get('street')}, {mail.get('city')}, {mail.get('state')} {mail.get('zip')}")
                else:
                    # Store on heir in heir_map_json
                    if notice.heir_map_json:
                        heirs = json.loads(notice.heir_map_json)
                        for h in heirs:
                            if h.get("name", "").lower() == full_name.lower():
                                h["phones"] = phones
                                h["emails"] = emails
                                break
                        notice.heir_map_json = json.dumps(heirs, ensure_ascii=False)

                print(f"  HIT: {full_name} -> {len(phones)} phones, {len(emails)} emails")
            else:
                print(f"  MISS: {full_name} (no match)")
        except Exception as e:
            print(f"  ERROR tracing {full_name}: {e}")

    tracerfy_stats["cost"] = tracerfy_stats["matched"] * 0.10
    print(f"  Cost: ${tracerfy_stats['cost']:.2f} ({tracerfy_stats['matched']} hits x $0.10)")
print()

# ── Step 3: Collect all phones for Trestle ──────────────────────────────
all_phones = []

# Flat fields (DM #1 or living owner)
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

# Deduplicate
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

# Build phone -> tier lookup
phone_tiers = {}
for tr in trestle_results:
    cleaned = tr.get("phone_number", "")
    score = tr.get("activity_score")
    tier = tr.get("assigned_tag", assign_tier(score, DEFAULT_TIERS))
    phone_tiers[cleaned] = {"score": score, "tier": tier, "line_type": tr.get("line_type", "?")}

# ── Final Report ────────────────────────────────────────────────────────
print("=" * 70)
print("COMPLETE RECORD REPORT — SCOTT ROBERT SMYTH")
print("=" * 70)
print()

r = notice

sections = {
    "PROPERTY": ["address", "city", "state", "zip", "county"],
    "SUBJECT": ["owner_name"],
    "DECEASED OWNER": ["owner_deceased", "date_of_death", "obituary_url",
                       "obituary_source_type"],
}

if notice.owner_deceased == "yes":
    sections["DECISION MAKER"] = [
        "decision_maker_name", "decision_maker_relationship",
        "decision_maker_status", "decision_maker_source",
        "decision_maker_street", "decision_maker_city",
        "decision_maker_state", "decision_maker_zip",
    ]
    sections["SIGNING CHAIN"] = ["signing_chain_count", "signing_chain_names"]
    sections["CONFIDENCE"] = ["dm_confidence", "dm_confidence_reason"]

for section_name, fields in sections.items():
    print(f"--- {section_name} ---")
    for fname in fields:
        val = getattr(r, fname, "")
        label = f"  {fname:35s}"
        print(f"{label} = {val}" if val else f"{label} = (empty)")
    print()

# ── Contact Info ────────────────────────────────────────────────────────
print("=" * 70)
if notice.owner_deceased == "yes":
    print("SIGNING CHAIN — CONTACT INFO")
else:
    print("SKIP TRACE RESULTS — SCOTT ROBERT SMYTH")
print("=" * 70)

if notice.owner_deceased == "yes" and notice.heir_map_json:
    heirs = json.loads(notice.heir_map_json)
    signers = [h for h in heirs if h.get("signing_authority") and h.get("status") != "deceased"]
    non_signers = [h for h in heirs if not h.get("signing_authority") or h.get("status") == "deceased"]

    for i, h in enumerate(signers, 1):
        status = "ALIVE" if h.get("status") == "verified_living" else h.get("status", "?").upper()
        phones = h.get("phones", [])
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
        _print_phones(phones, phone_tiers)
        for j, em in enumerate(h.get("emails", []), 1):
            print(f"     Email {j}:          {em}")

    if non_signers:
        print(f"\n\n--- OTHER FAMILY ({len(non_signers)} — no signing authority) ---")
        for h in non_signers:
            status = "living" if h.get("status") == "verified_living" else h.get("status", "?")
            print(f"  {h.get('name', '?'):30s} ({h.get('relationship', '?'):15s}) [{status}]")
else:
    # Living owner — show flat phone/email fields
    print(f"\n  Scott Robert Smyth (subject)")
    print(f"     Address:         {notice.address}, {notice.city}, {notice.state} {notice.zip}")
    phones = []
    for field in ["primary_phone", "mobile_1", "mobile_2", "mobile_3", "mobile_4",
                  "mobile_5", "landline_1", "landline_2", "landline_3"]:
        val = getattr(notice, field, "")
        if val:
            phones.append(val)
    _print_phones(phones, phone_tiers)
    emails = []
    for field in ["email_1", "email_2", "email_3", "email_4", "email_5"]:
        val = getattr(notice, field, "")
        if val:
            emails.append(val)
    for j, em in enumerate(emails, 1):
        print(f"     Email {j}:          {em}")

    # If deceased, also show heir map
    if notice.heir_map_json:
        heirs = json.loads(notice.heir_map_json)
        print(f"\n--- FAMILY MEMBERS ({len(heirs)}) ---")
        for h in heirs:
            status = "living" if h.get("status") == "verified_living" else h.get("status", "?")
            sa = " [SIGNER]" if h.get("signing_authority") else ""
            print(f"  {h.get('name', '?'):30s} ({h.get('relationship', '?'):15s}) [{status}]{sa}")

# ── Cost Summary ────────────────────────────────────────────────────────
print()
print("=" * 70)
print("COST SUMMARY")
print("=" * 70)
print(f"  Tracerfy skip trace:   ${tracerfy_stats.get('cost', 0):.2f} ({tracerfy_stats.get('submitted', 0)} contacts)")
print(f"  Trestle validation:    ${trestle_cost:.2f} ({len(unique_phones)} phones)")
print(f"  Haiku API (obituary):  ~$0.01")
total_cost = tracerfy_stats.get('cost', 0) + trestle_cost + 0.01
print(f"  TOTAL:                 ~${total_cost:.2f}")
print()

# ── Step 4: PDF Report ──────────────────────────────────────────────────
print("=" * 70)
print("STEP 4: PDF Report Generation")
print("=" * 70)
pdf_path = generate_record_pdf(notice, phone_tiers=phone_tiers)
print(f"  PDF saved to: {pdf_path}")
print(f"  File size:    {pdf_path.stat().st_size:,} bytes")
print()

# ── Raw heir map JSON ───────────────────────────────────────────────────
if notice.heir_map_json:
    print("=" * 70)
    print("RAW HEIR MAP JSON")
    print("=" * 70)
    print(json.dumps(json.loads(notice.heir_map_json), indent=2))
    print()


