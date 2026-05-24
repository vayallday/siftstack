"""Niche sequential marketing helpers — channel exports + a one-entry preset manifest.

Operator's real DataSift cycle (channel order):
  skip trace → SMS → cold call (3 follow-ups) → mail → DP escalations → recycle

Channels escalate by cost: SMS ($0.01) → Call ($0.03-0.06) → Mail ($0.50-2.00) → Deep Prospecting ($1.50-4.00).
`export_sms_list` / `export_call_list` / `export_mail_list` emit channel-ready CSVs
filtered out of DataSift; `export_sms_list` switches to heir-aware templates when
`notice_type == "pre_probate"` so SMS to deceased-owner records addresses the heir.

The live DataSift `00. NICHE SEQUENTIAL` folder is owned by the operator and
contains 14 presets (00..13) that this module does NOT mirror in code — they are
authored and maintained directly in DataSift's UI. PRESETS below is a one-entry
manifest of the SINGLE preset SiftStack adds:

  14. Pre-Probate → DP (NEW — pushes pre_probate-tagged records to deep prospecting
       for heir research; created in Phase 3 of v1.0)

Historical note: an earlier version of this docstring described a 12-preset
SMS-first cycle (Needs Skip Traced / Ready to Text / Needs Called Day 1-3 / ...)
that was never synced to DataSift. That manifest was aspirational and has been
removed. The real cycle (call-first with an SMS step between skip-trace and
calls) is documented in CLAUDE.md and lives in DataSift's UI as the source-of-truth.

Usage:
  python src/main.py niche-sequential --list-name "Pre-Probate" --channel sms --day 1
  python src/main.py niche-sequential --action setup-presets   # informational
"""

import csv
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import config

logger = logging.getLogger(__name__)

# ── Preset definitions ────────────────────────────────────────────────

# Live folder name in DataSift (case + punctuation exact).
PRESET_FOLDER = "00. NICHE SEQUENTIAL"

# PRESETS mirrors ONLY the SiftStack-added preset(s) in DataSift. The other
# 13 presets in the folder (00. Needs Skipped, 01. Skipped No Numbers,
# 02. Ready to Call, 03-05. Follow-Up 1-3, 06. Needs First Mail,
# 07. Mail Monthly, 08-10. * → DP, 11. Not Interested Qrtly, 12. Rehash,
# 13. No Valid Number → DP) are operator-owned in DataSift's UI and are
# NOT represented here — DataSift is the source-of-truth for those.
PRESETS = [
    {
        "number": "14",
        "name": "14. Pre-Probate → DP",
        "description": "Pre-probate records (PR-deceased signal) — owner is dead per "
                       "property records, so route to deep prospecting for heir "
                       "research before any contact channel fires. Matches the "
                       "operator's '→ DP' naming convention used by presets 08-10/13.",
        "filter": {"has_tag": "pre_probate", "status_not": "Sold"},
        "action": "Route to deep_prospector.py for Level 1-3 heir research; "
                  "downstream contact presets re-enter once a DM is identified.",
    },
]


@dataclass
class CycleRecord:
    """A record being processed through the niche sequential cycle."""
    address: str = ""
    owner_name: str = ""
    phone: str = ""
    email: str = ""
    current_preset: str = ""
    cycle_day: int = 0
    cycle_count: int = 0
    tags: list = field(default_factory=list)


# ── Channel execution ─────────────────────────────────────────────────

def export_sms_list(records: list[dict], day: int = 1,
                    output_path: str = "") -> str:
    """Export records for SMS sending via Launch Control / REISimpli.

    Returns path to CSV with: name, phone, message template.
    """
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(config.OUTPUT_DIR / f"sms_list_day{day}_{timestamp}.csv")

    sms_templates = {
        1: "Hi {name}, I noticed your property at {address} and wanted to reach out. "
           "Are you or anyone in the family considering selling? — [Your Name]",
        2: "Hey {name}, just following up on your property at {address}. "
           "If you're interested in a quick, fair cash offer, I'd love to chat. — [Your Name]",
        3: "Last message — {name}, I have a cash offer ready for {address}. "
           "If the timing isn't right, no worries. Let me know! — [Your Name]",
    }
    # Pre-probate records reach SMS only after obituary enrichment named a DM
    # (heir). The property owner is deceased, so "your property" framing is
    # wrong — address the heir directly and acknowledge the passing.
    heir_templates = {
        1: "Hi {name}, I came across the property at {address} and understand the owner "
           "has passed. I work with families navigating these situations — happy to "
           "share options if it'd help. — [Your Name]",
        2: "Hi {name}, following up on {address}. If the family is thinking about next "
           "steps, I can put together a no-obligation cash offer. — [Your Name]",
        3: "Final note — {name}, I'm still able to make a fair cash offer on {address} "
           "if it would help the family. No pressure either way. — [Your Name]",
    }

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["name", "phone", "message", "address"])
        writer.writeheader()
        for rec in records:
            name = rec.get("owner_name") or rec.get("full_name") or "there"
            phone = rec.get("primary_phone") or rec.get("mobile_1") or rec.get("Phone 1") or ""
            address = rec.get("address") or rec.get("Property Street") or ""
            notice = (rec.get("notice_type") or "").lower()
            template_set = heir_templates if notice == "pre_probate" else sms_templates
            template = template_set.get(day, template_set[1])
            if phone:
                writer.writerow({
                    "name": name,
                    "phone": phone,
                    "message": template.format(name=name.split()[0] if name else "there",
                                               address=address),
                    "address": address,
                })

    logger.info("Exported SMS list (Day %d): %s", day, output_path)
    return output_path


def export_call_list(records: list[dict], day: int = 1,
                     output_path: str = "") -> str:
    """Export records for cold calling with dial priority ordering.

    Returns path to CSV ordered by phone tier (Dial First → Second → Third).
    """
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(config.OUTPUT_DIR / f"call_list_day{day}_{timestamp}.csv")

    tier_order = {"Dial First": 1, "Dial Second": 2, "Dial Third": 3,
                  "Dial Fourth": 4, "Drop": 5}

    call_records = []
    for rec in records:
        phone = rec.get("primary_phone") or rec.get("mobile_1") or rec.get("Phone 1") or ""
        if not phone:
            continue
        tier = rec.get("phone_tier_tag") or rec.get("Phone Tag") or "Unscored"
        call_records.append({
            "name": rec.get("owner_name") or rec.get("full_name") or "",
            "phone": phone,
            "phone_2": rec.get("mobile_2") or rec.get("Phone 2") or "",
            "phone_3": rec.get("landline_1") or rec.get("Phone 3") or "",
            "tier": tier,
            "tier_order": tier_order.get(tier, 3),
            "address": rec.get("address") or rec.get("Property Street") or "",
            "notice_type": rec.get("notice_type") or "",
            "notes": rec.get("notes") or "",
        })

    # Sort by tier (best first)
    call_records.sort(key=lambda r: r["tier_order"])

    fields = ["name", "phone", "phone_2", "phone_3", "tier", "address", "notice_type", "notes"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(call_records)

    logger.info("Exported call list (Day %d): %d records, %s", day, len(call_records), output_path)
    return output_path


def export_mail_list(records: list[dict], output_path: str = "") -> str:
    """Export records for direct mail piece.

    Returns path to CSV with mailing-ready address formatting.
    """
    if not output_path:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output_path = str(config.OUTPUT_DIR / f"mail_list_{timestamp}.csv")

    fields = ["first_name", "last_name", "address_line_1", "address_line_2",
              "city", "state", "zip", "property_address", "notice_type"]

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for rec in records:
            name = rec.get("owner_name") or rec.get("full_name") or ""
            parts = name.split(None, 1)
            first = parts[0] if parts else ""
            last = parts[1] if len(parts) > 1 else ""

            # Use mailing address if available, fall back to property
            mail_street = (rec.get("owner_street") or rec.get("decision_maker_street") or
                           rec.get("address") or rec.get("Property Street") or "")
            mail_city = (rec.get("owner_city") or rec.get("decision_maker_city") or
                         rec.get("city") or rec.get("Property City") or "")
            mail_state = (rec.get("owner_state") or rec.get("decision_maker_state") or
                          rec.get("state") or "TN")
            mail_zip = (rec.get("owner_zip") or rec.get("decision_maker_zip") or
                        rec.get("zip") or rec.get("Property ZIP") or "")

            if mail_street:
                writer.writerow({
                    "first_name": first,
                    "last_name": last,
                    "address_line_1": mail_street,
                    "address_line_2": "",
                    "city": mail_city,
                    "state": mail_state,
                    "zip": mail_zip,
                    "property_address": rec.get("address") or "",
                    "notice_type": rec.get("notice_type") or "",
                })

    logger.info("Exported mail list: %s", output_path)
    return output_path


# ── Cycle orchestration ───────────────────────────────────────────────

def run_niche_sequential(list_name: str = "", channel: str = "sms",
                         day: int = 1, csv_path: str = "",
                         action: str = "execute") -> dict:
    """Run niche sequential marketing for a list/channel/day combination.

    Args:
        list_name: DataSift list to filter (e.g., "Foreclosure")
        channel: "sms", "call", "mail", "dp"
        day: 1, 2, or 3 of the 3-day cycle
        csv_path: Direct CSV path (bypasses DataSift filter)
        action: "execute" (run channel), "setup-presets" (create in DataSift),
                "status" (show cycle progress)
    """
    if action == "setup-presets":
        logger.info("Preset creation requires Playwright — use: "
                     "python src/main.py manage-presets --discover")
        return {"presets": PRESETS, "folder": PRESET_FOLDER,
                "note": "Use manage-presets CLI to create/modify in DataSift"}

    if action == "status":
        return {"presets": PRESETS,
                "message": "Preset status requires DataSift connection — "
                           "use manage-presets --discover"}

    # Load records
    records = []
    if csv_path:
        with open(csv_path, "r", encoding="utf-8-sig") as f:
            records = list(csv.DictReader(f))
    else:
        # Find most recent CSV for the list name
        for p in sorted(config.OUTPUT_DIR.glob("*.csv"),
                        key=lambda x: x.stat().st_mtime, reverse=True):
            if list_name.lower().replace(" ", "_") in p.name.lower():
                with open(p, "r", encoding="utf-8-sig") as f:
                    records = list(csv.DictReader(f))
                logger.info("Loaded %d records from %s", len(records), p)
                break

    if not records:
        return {"error": f"No records found for list '{list_name}'"}

    # Execute channel
    result = {"channel": channel, "day": day, "records": len(records)}

    if channel == "sms":
        result["output"] = export_sms_list(records, day)
    elif channel == "call":
        result["output"] = export_call_list(records, day)
    elif channel == "mail":
        result["output"] = export_mail_list(records)
    elif channel == "dp":
        result["note"] = "Route to: python src/main.py deep-prospect --csv-path <path> --depth 3"
    else:
        return {"error": f"Unknown channel: {channel}"}

    logger.info("Niche sequential: %s channel, Day %d, %d records", channel, day, len(records))
    return result
