"""Tests for src/main.py's daily/historical acquisition path.

The TN public-notice scraper was archived and then deleted, so there is no
source dispatch and no --source flag: daily and historical resolve to
propertyradar_puller.pull_all_lists() and nothing else.

Since 2026-08-21 that call is gated — PropertyRadar is disabled by default
(propertyradar_config.is_enabled). These tests pin both halves of the
contract: the puller is unreachable while disabled, and reachable once
explicitly enabled. They also guard against the deleted TN scraper being
silently reintroduced.
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path

import pytest


SRC = Path(__file__).resolve().parent.parent / "src"


# ── argparse: --source flag is gone ────────────────────────────────

# Spawning `python src/main.py` imports the full dependency tree. On macOS
# the FIRST import of a package in a fresh process can stall 20-60s at 0%
# CPU on first-run binary verification (reportlab measured at 60s cold,
# 0.08s warm), which made these subprocess tests flake at timeout=30.
_CLI_TIMEOUT = 180


def test_help_text_no_source_flag():
    """`python src/main.py --help` MUST NOT mention --source (removed)."""
    result = subprocess.run(
        [sys.executable, str(SRC / "main.py"), "--help"],
        capture_output=True, text=True, timeout=_CLI_TIMEOUT,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert "--source" not in result.stdout, (
        f"--source must not be in help text any more. stdout: {result.stdout[:500]!r}"
    )


def test_argparse_rejects_dropped_source_flag():
    """`python src/main.py daily --source propertyradar` exits non-zero."""
    result = subprocess.run(
        [sys.executable, str(SRC / "main.py"), "daily", "--source", "propertyradar"],
        capture_output=True, text=True, timeout=_CLI_TIMEOUT,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert result.returncode != 0, "--source must be unrecognized after archive"


# ── _preflight_check: PR creds only ───────────────────────────────

def test_preflight_checks_pr_creds_when_enabled(monkeypatch):
    """Missing PR creds only block once PropertyRadar is switched on."""
    sys.path.insert(0, str(SRC))
    import main as main_mod
    import propertyradar_config as prc

    monkeypatch.setattr(prc, "PROPERTYRADAR_ENABLED", True)
    monkeypatch.setattr(prc, "PROPERTYRADAR_EMAIL", "")
    monkeypatch.setattr(prc, "PROPERTYRADAR_PASSWORD", "")
    failures = main_mod._preflight_check("daily")
    joined = " | ".join(failures)
    assert "PROPERTYRADAR" in joined, (
        f"daily-mode preflight must check PROPERTYRADAR_* creds when enabled. "
        f"failures: {failures}"
    )


def test_preflight_ignores_pr_creds_when_disabled(monkeypatch):
    """PropertyRadar is off by default, so its creds are not a blocker —
    otherwise every daily run would fail preflight on an unused source."""
    sys.path.insert(0, str(SRC))
    import main as main_mod
    import propertyradar_config as prc

    monkeypatch.setattr(prc, "PROPERTYRADAR_ENABLED", False)
    monkeypatch.setattr(prc, "PROPERTYRADAR_EMAIL", "")
    monkeypatch.setattr(prc, "PROPERTYRADAR_PASSWORD", "")
    failures = main_mod._preflight_check("daily")
    cred_failures = [f for f in failures if "PROPERTYRADAR" in f]
    assert cred_failures == [], (
        f"disabled PropertyRadar must not block preflight: {cred_failures}"
    )


def test_preflight_passes_with_pr_creds(monkeypatch):
    sys.path.insert(0, str(SRC))
    import main as main_mod
    import propertyradar_config as prc

    monkeypatch.setattr(prc, "PROPERTYRADAR_ENABLED", True)
    monkeypatch.setattr(prc, "PROPERTYRADAR_EMAIL", "x")
    monkeypatch.setattr(prc, "PROPERTYRADAR_PASSWORD", "x")
    failures = main_mod._preflight_check("daily")
    cred_failures = [f for f in failures if "PROPERTYRADAR" in f]
    assert cred_failures == [], (
        f"daily preflight must pass when PR creds are set: {cred_failures}"
    )


# ── _run_scrape_pipeline: PR only when enabled ────────────────────

def _fake_args(mode="daily"):
    a = argparse.Namespace()
    a.mode = mode
    a.split = False
    a.verbose = False
    a.upload_datasift = False
    a.no_enrich = False
    a.no_skip_trace = False
    a.notify_slack = False
    a.run_tracerfy = False
    a.include_vacant = False
    a.include_commercial = False
    a.include_entities = False
    a.research_entities = False
    a.skip_smarty = False
    a.skip_zillow = False
    a.skip_tax = False
    a.skip_geocode = False
    a.skip_obituary = False
    a.skip_ancestry = False
    a.skip_heir_verification = False
    a.max_heir_depth = 2
    a.skip_dm_address = False
    a.tracerfy_tier1 = False
    a.skip_tracerfy = True  # don't hit Tracerfy in test
    a.audit_records = False
    return a


def test_run_scrape_pipeline_calls_pull_all_lists(monkeypatch):
    sys.path.insert(0, str(SRC))
    import main as main_mod
    import propertyradar_config as prc
    import propertyradar_puller

    called = {"pull": False}

    async def fake_pull(**kwargs):
        called["pull"] = True
        return []

    monkeypatch.setattr(propertyradar_puller, "pull_all_lists", fake_pull)

    # PropertyRadar is disabled by default — the puller must NOT be reached.
    monkeypatch.setattr(prc, "PROPERTYRADAR_ENABLED", False)
    args = _fake_args(mode="daily")
    args.enable_propertyradar = False
    try:
        main_mod._run_scrape_pipeline(args)
    except BaseException:
        pass
    assert called["pull"] is False, (
        "daily mode must NOT call pull_all_lists while PropertyRadar is "
        "disabled — the gate is what stops the live PR account being hit "
        "and export quota being consumed"
    )

    # ...and IS reached once explicitly enabled.
    args.enable_propertyradar = True
    try:
        main_mod._run_scrape_pipeline(args)
    except BaseException:
        # Downstream pipeline (enrich → upload → notify → sys.exit(0)) can
        # blow up freely; we only care that the PR puller was the
        # acquisition source. BaseException catches SystemExit too.
        pass
    assert called["pull"] is True, (
        "daily mode with --enable-propertyradar must call pull_all_lists"
    )


def test_main_module_does_not_import_legacy_scraper(monkeypatch):
    """main.py must not import the archived TN scraper at module load."""
    sys.path.insert(0, str(SRC))
    import importlib
    import main as main_mod
    importlib.reload(main_mod)
    assert not hasattr(main_mod, "scrape_all"), (
        "main.py reintroduced scrape_all — the TN scraper is archived; "
        "any new acquisition source must wire its own puller into "
        "_run_scrape_pipeline, not resurrect scrape_all at module level."
    )
