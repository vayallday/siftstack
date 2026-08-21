"""Phase 3 regression tests — pre_probate as a first-class notice type.

Covers the validation/scoring paths that previously hardcoded the 7
historical notice types and excluded pre_probate. If any of these regress,
PropertyRadar's pre_probate records silently fall out of:
  - the LLM photo/PDF classification path (rejected as "unknown type")
  - the Dropbox folder auto-routing (rejected at folder resolution)
  - the 4-Pillars-of-Motivation lead score (drops to "cold")
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ── llm_parser.auto_detect_notice_type validation ──────────────────

def test_llm_parser_valid_types_includes_pre_probate():
    """The validation set is local to the function — guard it via import-time
    constant. If the set ever gets refactored to module level, this test
    will need to be updated to import that constant directly."""
    import llm_parser
    src = Path(llm_parser.__file__).read_text(encoding="utf-8")
    # Look for the validation set literal — pre_probate must appear inside
    # the `valid_types = { ... }` block in auto_detect_notice_type.
    assert "\"pre_probate\"" in src, (
        "llm_parser.auto_detect_notice_type's valid_types set must include "
        "pre_probate, otherwise LLM-classified photos with that label are "
        "rejected and silently dropped."
    )


# ── lead_manager._score_reason hot/warm/cold routing ───────────────

def test_pre_probate_scores_hot():
    from lead_manager import _score_reason
    score = _score_reason({"notice_type": "pre_probate"})
    assert score.temperature == "hot", (
        f"pre_probate must score HOT (deceased-owner signal carries same "
        f"motivation as court probate). Got: {score.temperature!r} "
        f"(reason: {score.reason!r})"
    )


def test_pre_probate_hot_reason_mentions_distress_type():
    from lead_manager import _score_reason
    score = _score_reason({"notice_type": "pre_probate"})
    assert "pre_probate" in score.reason, (
        f"reason string must surface the notice_type for downstream "
        f"reporting. Got: {score.reason!r}"
    )


def test_probate_still_scores_hot_regression():
    """Regression: adding pre_probate must not bump probate out of hot."""
    from lead_manager import _score_reason
    score = _score_reason({"notice_type": "probate"})
    assert score.temperature == "hot", (
        f"probate regressed to {score.temperature!r} after pre_probate "
        f"addition. Both must be hot."
    )


def test_unknown_notice_type_still_cold():
    """Regression: pre_probate hot doesn't broaden hot_types semantics."""
    from lead_manager import _score_reason
    score = _score_reason({"notice_type": "garbage_type"})
    assert score.temperature == "cold", (
        f"unknown notice_type must remain cold (no false-positive hot)."
    )
