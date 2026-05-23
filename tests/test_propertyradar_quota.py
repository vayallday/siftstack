"""Unit tests for src/propertyradar_quota.py (PR-09).

Covers:
- Budget resolution from env (defaults, overrides, garbage values, commas)
- get_quota_status first-run and post-record reads
- record_export persistence, validation, and idempotent increment
- can_export under/at/over budget
- Threshold alert firing (50/80/95/100%) with same-month dedup
- Cross-month boundary: fresh alerts_fired in new month, separate counters
- format_quota_summary under-budget, overage, and zero-consumed shapes
- Schema-version guard refuses unknown on-disk versions

No live network, no live filesystem outside tmp_path, no real Slack.
"""

import sys
import types
from datetime import date

import pytest

import propertyradar_quota as prq


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def tmp_quota_file(monkeypatch, tmp_path):
    """Redirect PR_QUOTA_FILE to a tmp file per-test."""
    quota_path = tmp_path / "pr_quota.json"
    monkeypatch.setattr(prq, "PR_QUOTA_FILE", quota_path)
    return quota_path


@pytest.fixture
def small_budget(monkeypatch):
    """Set budget to 100 for easy threshold math (50/80/95/100 → 50/80/95/100)."""
    monkeypatch.setenv("PR_MONTHLY_RECORD_BUDGET", "100")


@pytest.fixture
def fake_slack(monkeypatch):
    """Replace slack_notifier with a capturing fake.

    _fire_threshold_alerts does a lazy `from slack_notifier import notify_error`,
    so we patch sys.modules["slack_notifier"] which the lazy import resolves.
    """
    calls: list[dict] = []

    def fake_notify(*args, **kwargs):
        calls.append({"args": args, "kwargs": kwargs})
        return True

    fake_module = types.ModuleType("slack_notifier")
    fake_module.notify_error = fake_notify
    monkeypatch.setitem(sys.modules, "slack_notifier", fake_module)
    return calls


# ── get_monthly_budget ───────────────────────────────────────────────


def test_get_monthly_budget_default(monkeypatch):
    monkeypatch.delenv("PR_MONTHLY_RECORD_BUDGET", raising=False)
    assert prq.get_monthly_budget() == prq.DEFAULT_MONTHLY_BUDGET == 10_000


def test_get_monthly_budget_env_override(monkeypatch):
    monkeypatch.setenv("PR_MONTHLY_RECORD_BUDGET", "25000")
    assert prq.get_monthly_budget() == 25_000


def test_get_monthly_budget_handles_garbage_env_values(monkeypatch, caplog):
    caplog.set_level("WARNING")
    for bad in ("abc", "0", "-5", " not a number "):
        monkeypatch.setenv("PR_MONTHLY_RECORD_BUDGET", bad)
        assert prq.get_monthly_budget() == prq.DEFAULT_MONTHLY_BUDGET, (
            f"garbage env {bad!r} should fall back to default"
        )
    # At least one warning should have been logged
    assert any(
        "PR_MONTHLY_RECORD_BUDGET" in rec.message for rec in caplog.records
    ), "expected at least one warning log about bad env value"


def test_get_monthly_budget_handles_comma_separator(monkeypatch):
    monkeypatch.setenv("PR_MONTHLY_RECORD_BUDGET", "10,000")
    assert prq.get_monthly_budget() == 10_000


# ── get_quota_status ─────────────────────────────────────────────────


def test_get_quota_status_first_run(tmp_quota_file, monkeypatch):
    monkeypatch.delenv("PR_MONTHLY_RECORD_BUDGET", raising=False)
    status = prq.get_quota_status()
    assert status.consumed == 0
    assert status.alerts_fired == ()
    assert status.month == date.today().isoformat()[:7]
    assert status.budget == prq.DEFAULT_MONTHLY_BUDGET
    assert status.remaining == prq.DEFAULT_MONTHLY_BUDGET
    assert status.pct_used == 0.0


# ── record_export persistence + validation ───────────────────────────


def test_record_export_writes_atomically(tmp_quota_file, small_budget, fake_slack):
    prq.record_export(50, today="2026-05-15")
    assert tmp_quota_file.exists(), "pr_quota.json should be written on first export"
    # Round-trip via get_quota_status
    status = prq.get_quota_status(today="2026-05-15")
    assert status.consumed == 50
    assert status.month == "2026-05"


def test_record_export_rejects_zero_or_negative(tmp_quota_file, small_budget):
    with pytest.raises(ValueError):
        prq.record_export(0, today="2026-05-15")
    with pytest.raises(ValueError):
        prq.record_export(-5, today="2026-05-15")


def test_record_export_idempotent_within_same_call(
    tmp_quota_file, small_budget, fake_slack
):
    # Two calls with same args → deterministic increment (20 total)
    prq.record_export(10, today="2026-05-15")
    prq.record_export(10, today="2026-05-15")
    assert prq.get_quota_status(today="2026-05-15").consumed == 20


# ── can_export ───────────────────────────────────────────────────────


def test_can_export_allows_under_budget(tmp_quota_file, small_budget, fake_slack):
    prq.record_export(20, today="2026-05-15")
    allowed, reason = prq.can_export(50, today="2026-05-15")
    assert allowed is True
    assert reason == ""


def test_can_export_blocks_at_budget(tmp_quota_file, small_budget, fake_slack):
    prq.record_export(80, today="2026-05-15")
    allowed, reason = prq.can_export(30, today="2026-05-15")
    assert allowed is False
    assert "Would exceed monthly budget" in reason
    assert "PR_MONTHLY_RECORD_BUDGET" in reason  # tells operator how to override


def test_can_export_allows_exact_budget(tmp_quota_file, small_budget, fake_slack):
    prq.record_export(80, today="2026-05-15")
    allowed, _ = prq.can_export(20, today="2026-05-15")  # 80 + 20 == 100, exactly at budget
    assert allowed is True


def test_can_export_treats_nonpositive_count_as_noop(tmp_quota_file, small_budget):
    # No record_export yet — fresh state. Zero/negative count is allowed
    # so callers don't have to special-case empty deltas.
    assert prq.can_export(0, today="2026-05-15") == (True, "")
    assert prq.can_export(-3, today="2026-05-15") == (True, "")


# ── Threshold alert firing + dedup ───────────────────────────────────


def test_threshold_alert_fires_at_50pct(tmp_quota_file, small_budget, fake_slack):
    prq.record_export(50, today="2026-05-15")
    # Exactly one alert: 50pct
    assert len(fake_slack) == 1
    step = fake_slack[0]["kwargs"]["step"]
    assert "50pct" in step


def test_threshold_alert_fires_at_80pct_after_50pct(
    tmp_quota_file, small_budget, fake_slack
):
    prq.record_export(50, today="2026-05-15")  # → 50% (fires 50pct)
    prq.record_export(30, today="2026-05-15")  # → 80% (fires 80pct)
    assert len(fake_slack) == 2
    steps = [c["kwargs"]["step"] for c in fake_slack]
    assert any("50pct" in s for s in steps)
    assert any("80pct" in s for s in steps)


def test_threshold_alert_fires_once_per_threshold_per_month(
    tmp_quota_file, small_budget, fake_slack
):
    # First call: 50 → fires 50pct (1 alert)
    prq.record_export(50, today="2026-05-15")
    # Second call: +50 → consumed=100, crosses 80pct + 95pct + 100pct
    # but NOT 50pct again (already fired)
    prq.record_export(50, today="2026-05-15")
    steps = [c["kwargs"]["step"] for c in fake_slack]
    assert sum("50pct" in s for s in steps) == 1, (
        f"50pct should fire exactly once; got {steps}"
    )
    assert any("80pct" in s for s in steps)
    assert any("95pct" in s for s in steps)
    assert any("100pct" in s for s in steps)
    assert len(fake_slack) == 4, (
        f"expected 4 alerts total (50, 80, 95, 100); got {len(fake_slack)}: {steps}"
    )


def test_threshold_alerts_dedup_across_calls(
    tmp_quota_file, small_budget, fake_slack
):
    # First call crosses 50pct
    prq.record_export(60, today="2026-05-15")
    assert len(fake_slack) == 1
    # Second call (60→70) crosses no new threshold
    prq.record_export(10, today="2026-05-15")
    assert len(fake_slack) == 1, "no new threshold; no additional alert"


# ── Cross-month boundary ─────────────────────────────────────────────


def test_cross_month_resets_alerts_fired(tmp_quota_file, small_budget, fake_slack):
    # May 15: 60 → fires 50pct
    prq.record_export(60, today="2026-05-15")
    assert len(fake_slack) == 1
    # June 1: 60 in a fresh month → fires 50pct AGAIN
    prq.record_export(60, today="2026-06-01")
    assert len(fake_slack) == 2, (
        "new month should reset alerts_fired and fire 50pct again"
    )


def test_cross_month_keeps_separate_counters(
    tmp_quota_file, small_budget, fake_slack
):
    prq.record_export(80, today="2026-05-15")
    prq.record_export(20, today="2026-06-01")
    may = prq.get_quota_status(today="2026-05-15")
    june = prq.get_quota_status(today="2026-06-01")
    assert may.consumed == 80
    assert june.consumed == 20
    assert may.month == "2026-05"
    assert june.month == "2026-06"


# ── format_quota_summary ─────────────────────────────────────────────


def test_format_quota_summary_under_budget(tmp_quota_file, monkeypatch, fake_slack):
    monkeypatch.setenv("PR_MONTHLY_RECORD_BUDGET", "10000")
    prq.record_export(4250, today="2026-05-15")
    summary = prq.format_quota_summary(today="2026-05-15")
    assert "4,250" in summary
    assert "10,000" in summary
    assert "42.5% used" in summary
    assert "over budget" not in summary
    assert summary.startswith("PR quota 2026-05:")


def test_format_quota_summary_overage(tmp_quota_file, monkeypatch, fake_slack):
    monkeypatch.setenv("PR_MONTHLY_RECORD_BUDGET", "100")
    prq.record_export(125, today="2026-05-15")
    summary = prq.format_quota_summary(today="2026-05-15")
    assert "125" in summary
    assert "100" in summary
    assert "125.0% used" in summary
    assert "25 over budget" in summary


def test_format_quota_summary_zero_consumed(tmp_quota_file, monkeypatch):
    monkeypatch.delenv("PR_MONTHLY_RECORD_BUDGET", raising=False)
    summary = prq.format_quota_summary(today="2026-05-15")
    assert "0 / 10,000" in summary
    assert "0.0% used" in summary


# ── Schema version guard ─────────────────────────────────────────────


def test_load_refuses_unknown_schema_version(tmp_quota_file):
    import json
    tmp_quota_file.write_text(
        json.dumps({"_schema_version": 99, "monthly": {}}), encoding="utf-8"
    )
    with pytest.raises(ValueError) as exc_info:
        prq.get_quota_status(today="2026-05-15")
    assert "migration" in str(exc_info.value).lower()


# ── Slack alert content ──────────────────────────────────────────────


def test_slack_alert_content_includes_month_and_count(
    tmp_quota_file, small_budget, fake_slack
):
    prq.record_export(
        50, list_name="MD_Auction in 90 Days_No Pre-Probate_No Vacant",
        today="2026-05-15",
    )
    assert len(fake_slack) == 1
    call = fake_slack[0]
    # step contains token
    assert "50pct" in call["kwargs"]["step"]
    # error (synthetic exception) contains the month + record counts
    error_text = str(call["kwargs"]["error"])
    assert "2026-05" in error_text
    assert "50" in error_text  # consumed count appears
    assert "100" in error_text  # budget appears
    # list name surfaces in either summary or context (we attach it to summary)
    assert "MD_Auction" in error_text or "MD_Auction" in call["kwargs"].get("context", "")
    # context contains the structured numerics for downstream parsing
    context = call["kwargs"]["context"]
    assert "month=2026-05" in context
    assert "consumed=50" in context
    assert "budget=100" in context
