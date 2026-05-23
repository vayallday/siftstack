"""Phase 3 SC #5 / NT-05 — pre_probate records flow through a documented
preset in the `00 Niche Sequential Marketing` folder, and the SMS template
used for those records addresses the heir rather than the deceased owner.

Two failure modes these tests guard against:
  1. PRESETS list drifts and the pre_probate preset disappears (no
     designated landing-zone for PR's deceased-owner records).
  2. export_sms_list keeps using the property-owner template ("I noticed
     YOUR property") for pre_probate records — wrong audience: the
     property owner is dead and the message goes to the obituary-identified
     heir.
"""

import csv
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ── PRESETS list invariants ─────────────────────────────────────────

def test_pre_probate_preset_exists():
    from niche_sequential import PRESETS
    pre_probate = [p for p in PRESETS if p["number"] == "12"]
    assert len(pre_probate) == 1, (
        "Exactly one preset 12 must exist as the pre_probate designated "
        "landing zone (NT-05). Found: "
        f"{[p['name'] for p in pre_probate]}"
    )
    assert pre_probate[0]["name"] == "12. Pre-Probate Heir Discovery", (
        f"Preset 12 name drifted: {pre_probate[0]['name']!r}. The Phase 3 "
        f"requirement docs the preset by name."
    )


def test_pre_probate_preset_in_canonical_folder():
    from niche_sequential import PRESET_FOLDER
    assert PRESET_FOLDER == "00 Niche Sequential Marketing", (
        f"PRESET_FOLDER drifted to {PRESET_FOLDER!r}. NT-05 requires the "
        f"pre_probate preset live in `00 Niche Sequential Marketing`."
    )


def test_pre_probate_preset_filter_targets_deceased_without_dm():
    """Filter must match pre_probate records that DON'T have a confirmed
    DM — the population that needs heir research before any contact
    attempt. Records WITH a confirmed DM should fall through to the
    standard 01-05 contact presets."""
    from niche_sequential import PRESETS
    preset = next(p for p in PRESETS if p["number"] == "12")
    f = preset["filter"]
    assert f.get("has_tag") == "pre_probate", (
        f"Filter must positively target pre_probate via has_tag. "
        f"Filter: {f}"
    )
    assert f.get("not_tag") == "has_dm", (
        f"Filter must exclude records with a confirmed DM (those flow "
        f"through 01-05). Filter: {f}"
    )
    assert f.get("status_not") == "Sold", (
        f"All niche-sequential presets exclude Sold status (build 1.0.23 "
        f"invariant). Filter: {f}"
    )


def test_pre_probate_preset_action_routes_to_deep_prospector():
    from niche_sequential import PRESETS
    preset = next(p for p in PRESETS if p["number"] == "12")
    assert "deep_prospector" in preset["action"].lower(), (
        f"Action must route to deep_prospector for heir research, since "
        f"contact channels are pointless until a living DM is identified. "
        f"Action: {preset['action']!r}"
    )


def test_baseline_13_presets_present():
    """Regression: adding preset 12 must not drop any of the existing 12."""
    from niche_sequential import PRESETS
    numbers = [p["number"] for p in PRESETS]
    assert numbers == ["00", "01", "02", "03", "04", "05", "06", "07",
                       "08", "09", "10", "11", "12"], (
        f"PRESETS numbering drifted: {numbers}"
    )


# ── export_sms_list heir-aware template selection ──────────────────

def test_sms_uses_heir_aware_template_for_pre_probate(tmp_path):
    from niche_sequential import export_sms_list
    out = tmp_path / "sms.csv"
    records = [{
        "owner_name": "Jane Doe",
        "primary_phone": "8045550100",
        "address": "100 Main St",
        "notice_type": "pre_probate",
    }]
    export_sms_list(records, day=1, output_path=str(out))
    with open(out, encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    msg = row["message"]
    # Heir-aware template acknowledges the passing
    assert "passed" in msg.lower() or "family" in msg.lower(), (
        f"pre_probate SMS must use heir-aware template (acknowledges "
        f"passing or addresses family). Got: {msg!r}"
    )
    # Property-owner template's "your property" framing is wrong for a
    # deceased owner — must not appear.
    assert "your property" not in msg.lower(), (
        f"pre_probate SMS leaked property-owner framing 'your property' "
        f"into the heir audience. Got: {msg!r}"
    )


def test_sms_uses_owner_template_for_foreclosure(tmp_path):
    """Regression: heir-aware routing must be opt-in by notice_type, not
    accidentally applied to living-owner notice types."""
    from niche_sequential import export_sms_list
    out = tmp_path / "sms.csv"
    records = [{
        "owner_name": "John Smith",
        "primary_phone": "8045550101",
        "address": "200 Oak Ave",
        "notice_type": "foreclosure",
    }]
    export_sms_list(records, day=1, output_path=str(out))
    with open(out, encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    msg = row["message"]
    assert "your property" in msg.lower(), (
        f"foreclosure (living owner) must use the standard property-owner "
        f"template. Got: {msg!r}"
    )


def test_sms_falls_back_to_owner_template_when_notice_type_missing(tmp_path):
    """Records without notice_type (legacy CSVs, manual entries) should
    default to the standard template — not the heir-aware one."""
    from niche_sequential import export_sms_list
    out = tmp_path / "sms.csv"
    records = [{
        "owner_name": "Pat Jones",
        "primary_phone": "8045550102",
        "address": "300 Elm Pl",
    }]
    export_sms_list(records, day=1, output_path=str(out))
    with open(out, encoding="utf-8") as f:
        row = next(csv.DictReader(f))
    assert "your property" in row["message"].lower()
