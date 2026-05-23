"""Tests for src/main.py's --source dispatch + PR-aware preflight.

Confirms PR-06 (CLI --source flag) and PR-07 (state-file isolation at the
dispatch layer — the PR path never touches scrape_all, and the default path
never touches pull_all_lists).
"""

import argparse
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest


SRC = Path(__file__).resolve().parent.parent / "src"


# ── argparse: --source flag presence and validation ────────────────

def test_help_text_mentions_source_flag():
    """`python src/main.py --help` includes `--source` documentation."""
    result = subprocess.run(
        [sys.executable, str(SRC / "main.py"), "--help"],
        capture_output=True, text=True, timeout=30,
        env={**os.environ, "PYTHONPATH": str(SRC)},
    )
    assert "--source" in result.stdout, (
        f"help text missing --source. stdout: {result.stdout[:500]!r}"
    )
    assert "propertyradar" in result.stdout, (
        f"help text missing propertyradar choice. stdout: {result.stdout[:500]!r}"
    )


def test_argparse_rejects_invalid_source():
    """`python src/main.py daily --source garbage` exits non-zero."""
    result = subprocess.run(
        [sys.executable, str(SRC / "main.py"), "daily", "--source", "garbage"],
        capture_output=True, text=True, timeout=30,
        env={
            **os.environ,
            "PYTHONPATH": str(SRC),
            "TNPN_EMAIL": "x", "TNPN_PASSWORD": "x",
        },
    )
    assert result.returncode != 0
    err = result.stderr.lower()
    assert "invalid choice" in err or "choices" in err, (
        f"stderr did not look like argparse rejection: {result.stderr[:400]!r}"
    )


# ── _preflight_check branching ─────────────────────────────────────

def test_preflight_default_source_checks_tnpn_creds(monkeypatch):
    sys.path.insert(0, str(SRC))
    import main as main_mod
    monkeypatch.setattr(main_mod.config, "TNPN_EMAIL", "")
    monkeypatch.setattr(main_mod.config, "TNPN_PASSWORD", "")
    monkeypatch.setattr(main_mod.config, "CAPTCHA_API_KEY", "")
    failures = main_mod._preflight_check("daily", source=None)
    joined = " | ".join(failures)
    assert "TNPN" in joined, f"default source must check TNPN creds. failures: {failures}"
    assert "PROPERTYRADAR" not in joined, (
        "default source must NOT check PR creds"
    )


def test_preflight_pr_source_checks_pr_creds(monkeypatch):
    sys.path.insert(0, str(SRC))
    import main as main_mod
    import propertyradar_config as prc
    monkeypatch.setattr(prc, "PROPERTYRADAR_EMAIL", "")
    monkeypatch.setattr(prc, "PROPERTYRADAR_PASSWORD", "")
    # TN creds present so the TN check would not have failed
    monkeypatch.setattr(main_mod.config, "TNPN_EMAIL", "x")
    monkeypatch.setattr(main_mod.config, "TNPN_PASSWORD", "x")
    monkeypatch.setattr(main_mod.config, "CAPTCHA_API_KEY", "x")
    failures = main_mod._preflight_check("daily", source="propertyradar")
    joined = " | ".join(failures)
    assert "PROPERTYRADAR" in joined, (
        f"PR source preflight must check PROPERTYRADAR_* creds. failures: {failures}"
    )


def test_preflight_pr_source_skips_tn_and_captcha_checks(monkeypatch):
    """PR source must NOT add TN or CAPTCHA failures even when those are unset."""
    sys.path.insert(0, str(SRC))
    import main as main_mod
    import propertyradar_config as prc
    monkeypatch.setattr(prc, "PROPERTYRADAR_EMAIL", "x")
    monkeypatch.setattr(prc, "PROPERTYRADAR_PASSWORD", "x")
    monkeypatch.setattr(main_mod.config, "TNPN_EMAIL", "")
    monkeypatch.setattr(main_mod.config, "TNPN_PASSWORD", "")
    monkeypatch.setattr(main_mod.config, "CAPTCHA_API_KEY", "")
    failures = main_mod._preflight_check("daily", source="propertyradar")
    joined = " | ".join(failures)
    assert "CAPTCHA" not in joined, (
        f"PR source preflight MUST NOT check CAPTCHA. failures: {failures}"
    )
    assert "TNPN" not in joined, (
        f"PR source preflight MUST NOT check TNPN creds. failures: {failures}"
    )


# ── _run_scrape_pipeline dispatch ──────────────────────────────────

def _fake_args(source=None, mode="daily"):
    a = argparse.Namespace()
    a.mode = mode
    a.source = source
    a.since = None
    a.max_notices = 0
    a.counties = None
    a.types = None
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
    return a


def test_run_scrape_pipeline_pr_branch_calls_pull_all_lists(monkeypatch):
    sys.path.insert(0, str(SRC))
    import main as main_mod
    import propertyradar_puller

    called = {"pull": False, "scrape": False}

    async def fake_pull(**kwargs):
        called["pull"] = True
        return []

    async def fake_scrape(**kwargs):
        called["scrape"] = True
        return []

    monkeypatch.setattr(propertyradar_puller, "pull_all_lists", fake_pull)
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape)

    try:
        main_mod._run_scrape_pipeline(_fake_args(source="propertyradar"), [])
    except Exception:
        # Downstream pipeline (enrich → upload → notify) can fail freely;
        # we only care which acquisition branch ran.
        pass
    assert called["pull"] is True, "PR source must call pull_all_lists"
    assert called["scrape"] is False, "PR source must NOT call scrape_all"


def test_run_scrape_pipeline_default_branch_calls_scrape_all(monkeypatch):
    sys.path.insert(0, str(SRC))
    import main as main_mod
    import propertyradar_puller

    called = {"pull": False, "scrape": False}

    async def fake_pull(**kwargs):
        called["pull"] = True
        return []

    async def fake_scrape(**kwargs):
        called["scrape"] = True
        return []

    monkeypatch.setattr(propertyradar_puller, "pull_all_lists", fake_pull)
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape)

    try:
        main_mod._run_scrape_pipeline(_fake_args(source=None), [])
    except Exception:
        pass
    assert called["scrape"] is True, "default source must call scrape_all"
    assert called["pull"] is False, "default source must NOT call pull_all_lists"


def test_run_scrape_pipeline_tnpn_explicit_calls_scrape_all(monkeypatch):
    """`--source tnpn` (explicit) behaves identically to default."""
    sys.path.insert(0, str(SRC))
    import main as main_mod
    import propertyradar_puller

    called = {"pull": False, "scrape": False}

    async def fake_pull(**kwargs):
        called["pull"] = True
        return []

    async def fake_scrape(**kwargs):
        called["scrape"] = True
        return []

    monkeypatch.setattr(propertyradar_puller, "pull_all_lists", fake_pull)
    monkeypatch.setattr(main_mod, "scrape_all", fake_scrape)

    try:
        main_mod._run_scrape_pipeline(_fake_args(source="tnpn"), [])
    except Exception:
        pass
    assert called["scrape"] is True
    assert called["pull"] is False
