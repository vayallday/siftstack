"""Phase 3 SC #5 / NT-05 — `14. Pre-Probate → DP` preset + heir-aware SMS templates.

Background: an earlier version of this file asserted a 13-entry PRESETS manifest
with a slot-12 `Pre-Probate Heir Discovery` preset. That manifest was fictional —
the operator's real DataSift `00. NICHE SEQUENTIAL` folder has 14 call-first
presets (00..13), with slot 12 already taken by `Rehash`. The reshape (this file)
asserts the corrected design:

  - PRESET_FOLDER mirrors DataSift's actual folder name `"00. NICHE SEQUENTIAL"`
  - PRESETS contains exactly ONE entry: `14. Pre-Probate → DP` (the SiftStack
    addition; the other 13 presets are operator-owned in DataSift and not mirrored)
  - Filter is simple: `has_tag=pre_probate` + `status_not=Sold` (the `has_dm`
    gate was dropped because that tag was never wired anywhere)
  - `export_sms_list` still selects heir-aware templates for pre_probate records,
    because SMS IS part of the operator's real cycle
    (skip trace → SMS → cold call → mail → DP)

Two failure modes guarded here:
  1. PRESETS drifts away from the single documented preset or the folder constant
     changes without DataSift being updated to match.
  2. `export_sms_list` reverts to property-owner framing ("YOUR property") for
     pre_probate records, sending the wrong message to the obituary-identified heir.
"""

import csv
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ── PRESETS manifest invariants ─────────────────────────────────────

def test_pre_probate_preset_exists():
    from niche_sequential import PRESETS
    pre_probate = [p for p in PRESETS if p["number"] == "14"]
    assert len(pre_probate) == 1, (
        "Exactly one preset 14 must exist (the SiftStack-added "
        "`14. Pre-Probate → DP`). Found: "
        f"{[p['name'] for p in pre_probate]}"
    )
    assert pre_probate[0]["name"] == "14. Pre-Probate → DP", (
        f"Preset 14 name drifted: {pre_probate[0]['name']!r}. Must match "
        f"the operator's `→ DP` convention exactly to align with the real "
        f"DataSift folder entry."
    )


def test_preset_folder_matches_real_datasift_name():
    from niche_sequential import PRESET_FOLDER
    assert PRESET_FOLDER == "00. NICHE SEQUENTIAL", (
        f"PRESET_FOLDER drifted to {PRESET_FOLDER!r}. NT-05 requires "
        f"alignment with DataSift's actual folder name '00. NICHE SEQUENTIAL'."
    )


def test_pre_probate_preset_filter_is_simple():
    """Filter targets every pre_probate record except Sold ones, no gating
    on vapor tags. Refinement (e.g., excluding records already in active
    cycle, or those with a confirmed DM) can layer in later once the
    corresponding tags actually exist in DataSift."""
    from niche_sequential import PRESETS
    preset = next(p for p in PRESETS if p["number"] == "14")
    f = preset["filter"]
    assert f.get("has_tag") == "pre_probate", (
        f"Filter must positively target pre_probate via has_tag. "
        f"Filter: {f}"
    )
    assert f.get("status_not") == "Sold", (
        f"Niche sequential presets exclude Sold status (build 1.0.23 "
        f"invariant). Filter: {f}"
    )
    assert "has_dm" not in str(f), (
        f"Filter must NOT reference the `has_dm` tag — that tag was "
        f"designed but never wired (no code emits it). Keep the filter "
        f"simple until/unless the tag becomes real. Filter: {f}"
    )


def test_pre_probate_preset_action_routes_to_deep_prospector():
    from niche_sequential import PRESETS
    preset = next(p for p in PRESETS if p["number"] == "14")
    assert "deep_prospector" in preset["action"].lower(), (
        f"Action must route to deep_prospector for heir research. "
        f"Action: {preset['action']!r}"
    )


def test_presets_manifest_only_contains_sift_stack_additions():
    """PRESETS mirrors ONLY the SiftStack-added preset(s) — not the 13
    operator-owned presets in DataSift's UI. This boundary keeps PRESETS
    honest: when an entry is here, it's a contract between SiftStack code
    and what we expect to live in DataSift; when an entry isn't here,
    DataSift owns it."""
    from niche_sequential import PRESETS
    numbers = [p["number"] for p in PRESETS]
    assert numbers == ["14"], (
        f"PRESETS should contain exactly the SiftStack-added preset(s). "
        f"Found: {numbers}. If a future SiftStack-owned preset is added, "
        f"update this assertion; if a DataSift-owned operator preset got "
        f"mirrored in here, remove it (DataSift's UI is the source-of-truth "
        f"for those)."
    )


# ── export_sms_list heir-aware template selection ──────────────────

def test_sms_uses_heir_aware_template_for_pre_probate(tmp_path):
    """SMS IS part of the operator's real cycle (skip trace → SMS → cold call
    → mail → DP). When `niche-sequential --channel sms` runs for a pre_probate
    list, the SMS template must address the heir, not the deceased owner."""
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
    assert "passed" in msg.lower() or "family" in msg.lower(), (
        f"pre_probate SMS must use heir-aware template (acknowledges "
        f"passing or addresses family). Got: {msg!r}"
    )
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
