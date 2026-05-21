"""Unit tests for src/propertyradar_config.py.

Validates DEC-pr-lists fidelity (locked list config), PR-07 coexistence
guarantees (state files distinct from TN), PR-03 state-file round-trip,
and selector-placeholder discipline (Plan 03 has not yet captured real
selectors).
"""

import re
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import config as tn_config
import propertyradar_config as prc


# ── List config integrity (DEC-pr-lists) ─────────────────────────

def test_lists_length_is_four():
    assert len(prc.PROPERTYRADAR_LISTS) == 4


EXPECTED_NAMES = {
    "MD_Auction in 90 Days_No Pre-Probate_No Vacant",
    "VA_Auction in 90 Days_No Pre-Probate_No Vacant",
    "MD_Pre-Probate_Distress >60_Occupied",
    "VA_Pre-Probate_Distress >60_Occupied",
}


def test_list_names_match_dec_pr_lists():
    actual = {l.name for l in prc.PROPERTYRADAR_LISTS}
    missing = EXPECTED_NAMES - actual
    extra = actual - EXPECTED_NAMES
    assert not missing and not extra, (
        f"List names drifted from DEC-pr-lists.\n"
        f"  missing: {sorted(missing)}\n"
        f"  extra:   {sorted(extra)}"
    )


def test_notice_type_distribution():
    nts = [l.notice_type for l in prc.PROPERTYRADAR_LISTS]
    assert nts.count("foreclosure") == 2
    assert nts.count("pre_probate") == 2


def test_state_distribution():
    states = [l.state for l in prc.PROPERTYRADAR_LISTS]
    assert states.count("MD") == 2
    assert states.count("VA") == 2


def test_slugs_are_filesystem_safe():
    for l in prc.PROPERTYRADAR_LISTS:
        assert re.fullmatch(r"[a-z0-9_]+", l.slug), f"unsafe slug: {l.slug!r}"


def test_slugs_are_unique():
    slugs = [l.slug for l in prc.PROPERTYRADAR_LISTS]
    assert len(set(slugs)) == len(slugs), f"duplicate slugs: {slugs}"


# ── Coexistence (PR-07) ──────────────────────────────────────────

def test_state_files_distinct_from_tn():
    assert prc.PR_STATE_FILE != tn_config.STATE_FILE
    assert prc.PR_COOKIES_FILE != tn_config.COOKIES_FILE


def test_state_files_under_project_root():
    assert prc.PR_STATE_FILE.parent == prc.PROJECT_ROOT
    assert prc.PR_COOKIES_FILE.parent == prc.PROJECT_ROOT


# ── State persistence round-trip (PR-03) ─────────────────────────

def test_load_missing_state_returns_empty(monkeypatch, tmp_path):
    ghost = tmp_path / "does_not_exist.json"
    monkeypatch.setattr(prc, "PR_STATE_FILE", ghost)
    assert prc.load_pr_state() == {}


def test_state_round_trip_stamps_schema_version(monkeypatch, tmp_path):
    state_path = tmp_path / "pr_state.json"
    monkeypatch.setattr(prc, "PR_STATE_FILE", state_path)
    prc.save_pr_state({"some_list": "2026-05-20"})
    loaded = prc.load_pr_state()
    assert loaded == {"some_list": "2026-05-20", "_schema_version": 1}


def test_save_does_not_mutate_caller_dict(monkeypatch, tmp_path):
    monkeypatch.setattr(prc, "PR_STATE_FILE", tmp_path / "pr_state.json")
    caller = {"k": "v"}
    prc.save_pr_state(caller)
    assert caller == {"k": "v"}, (
        f"save_pr_state mutated caller dict: {caller}"
    )


# ── First-run lookback default ───────────────────────────────────

def test_default_lookback_is_seven_days_ago():
    # Allow a 2-day window so a midnight rollover between
    # `default_lookback_date()` and the in-test `datetime.now()` doesn't
    # flake the CI run (or the 05:00 Apify run that scrapes the clock-edge).
    # Cron at 05:00 is not directly at risk, but the two `now()` reads can
    # straddle midnight when running locally late.
    now = datetime.now()
    acceptable = {
        (now - timedelta(days=7)).strftime("%Y-%m-%d"),
        (now - timedelta(days=8)).strftime("%Y-%m-%d"),
        (now - timedelta(days=6)).strftime("%Y-%m-%d"),
    }
    assert prc.default_lookback_date() in acceptable


# ── Selector placeholders are still sentinels (Plan 03 hasn't run) ──

def test_sel_pr_constants_are_sentinels():
    sel_attrs = [a for a in dir(prc) if a.startswith("SEL_PR_")]
    assert sel_attrs, "no SEL_PR_* constants found"
    for attr in sel_attrs:
        val = getattr(prc, attr)
        # Plan 03 will replace these with real DOM selectors and this
        # test will be updated then. Until then, every placeholder must
        # equal the sentinel — surfaces accidental partial-capture.
        assert val == prc._SENTINEL, (
            f"{attr} = {val!r} — expected sentinel until Plan 03 captures it"
        )
