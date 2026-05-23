"""PropertyRadar monthly export quota tracking (PR-09).

Tracks records consumed per calendar month in pr_quota.json. Fires Slack
alerts at 50%/80%/95%/100% of the configured monthly budget the first
time each threshold is crossed within a month. Refuses exports that
would push the running total above budget.

Solo plan default: 10,000 records/month at $119/mo base.
Override via PR_MONTHLY_RECORD_BUDGET env (e.g., "25000" for Team plan).

Used by Plan 04's puller around the Purchase click:
    if not can_export(expected_count)[0]: raise QuotaExceededError(...)
    await page.click(SEL_PR_EXPORT_PURCHASE)
    ...
    record_export(expected_count, list_name=pr_list.name)

Used by Plan 05's main.py daily-summary block:
    slack_summary += "\n" + format_quota_summary()

Storage: pr_quota.json at project root, gitignored by Plan 06. Schema:
    {
      "_schema_version": 1,
      "monthly": {
        "YYYY-MM": {"consumed": int, "alerts_fired": [str, ...]}
      }
    }

Orthogonal to the per-run `_quota_guard` in propertyradar_puller — that
catches "stale state file dumps 5,000 records on you"; this catches
"you've consumed 9,500 of 10,000 records this month across many runs".
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import date
from typing import Optional

import config  # for save_state / load_state — REUSE, do not reimplement
from propertyradar_config import PROJECT_ROOT

logger = logging.getLogger(__name__)


# ── Constants ────────────────────────────────────────────────────────
PR_QUOTA_FILE = PROJECT_ROOT / "pr_quota.json"
PR_QUOTA_SCHEMA_VERSION = 1
DEFAULT_MONTHLY_BUDGET = 10_000

# Locked ascending order — threshold-crossing detection walks this list
# in order, and `_alert_sort_key` reconstructs this same order when
# persisting the alerts_fired array. The puller's daily-summary uses the
# same tokens (Plan 04 / Plan 05 cross-reference).
ALERT_THRESHOLDS: list[tuple[float, str]] = [
    (0.50, "50pct"),
    (0.80, "80pct"),
    (0.95, "95pct"),
    (1.00, "100pct"),
]


# ── Errors ───────────────────────────────────────────────────────────
class QuotaExceededError(Exception):
    """Raised by callers when `can_export` returns False and the caller
    decides to wholesale-abort remaining exports (per Plan 04's call
    contract: can_export pre-Purchase, raise to halt the run)."""


# ── Status type ──────────────────────────────────────────────────────
@dataclass(frozen=True)
class QuotaStatus:
    month: str                       # YYYY-MM
    consumed: int
    budget: int
    remaining: int                   # budget - consumed (negative on overage)
    pct_used: float                  # 0.0 to 1.0+
    alerts_fired: tuple[str, ...]


# ── Budget resolution ────────────────────────────────────────────────
def get_monthly_budget() -> int:
    """Resolve monthly budget from env at call-time (so tests can
    monkeypatch via setenv between calls within the same import).

    Defensive parsing: strips commas, drops decimals, ignores whitespace.
    Falls back to DEFAULT_MONTHLY_BUDGET on any parse failure or
    non-positive value, and warns so the operator sees the misconfig.
    """
    raw = os.environ.get("PR_MONTHLY_RECORD_BUDGET", "").strip()
    if not raw:
        return DEFAULT_MONTHLY_BUDGET
    try:
        cleaned = raw.replace(",", "").split(".")[0]
        value = int(cleaned)
        if value <= 0:
            raise ValueError("budget must be positive")
        return value
    except (ValueError, TypeError):
        logger.warning(
            "PR_MONTHLY_RECORD_BUDGET=%r is not a positive integer — "
            "falling back to default %d",
            raw, DEFAULT_MONTHLY_BUDGET,
        )
        return DEFAULT_MONTHLY_BUDGET


# ── Registry I/O ─────────────────────────────────────────────────────
def _load_raw() -> dict:
    """Load pr_quota.json or return a fresh empty registry.

    Raises ValueError if the on-disk schema version doesn't match
    PR_QUOTA_SCHEMA_VERSION — we never auto-migrate; the operator
    must decide what to do with old data.
    """
    data = config.load_state(PR_QUOTA_FILE)
    if not data:
        return {"_schema_version": PR_QUOTA_SCHEMA_VERSION, "monthly": {}}
    version = data.get("_schema_version", 0)
    if version != PR_QUOTA_SCHEMA_VERSION:
        raise ValueError(
            f"pr_quota.json schema version {version} != expected "
            f"{PR_QUOTA_SCHEMA_VERSION} — migration required"
        )
    data.setdefault("monthly", {})
    return data


def _save_raw(data: dict) -> None:
    """Persist registry atomically (config.save_state writes tmp → rename)."""
    data["_schema_version"] = PR_QUOTA_SCHEMA_VERSION
    data.setdefault("monthly", {})
    config.save_state(PR_QUOTA_FILE, data)


def _month_key(today: Optional[str]) -> str:
    """Normalize a today value (or default to date.today) to YYYY-MM.

    Accepts YYYY-MM or YYYY-MM-DD; returns the YYYY-MM prefix.
    """
    if today:
        return today[:7]
    return date.today().isoformat()[:7]


# ── Public API ───────────────────────────────────────────────────────
def get_quota_status(today: Optional[str] = None) -> QuotaStatus:
    """Return current quota status for the given month (default: today's)."""
    month = _month_key(today)
    budget = get_monthly_budget()
    data = _load_raw()
    record = data["monthly"].get(month, {"consumed": 0, "alerts_fired": []})
    consumed = int(record.get("consumed", 0))
    alerts = tuple(record.get("alerts_fired", []))
    remaining = budget - consumed
    pct = consumed / budget if budget > 0 else 0.0
    return QuotaStatus(
        month=month,
        consumed=consumed,
        budget=budget,
        remaining=remaining,
        pct_used=pct,
        alerts_fired=alerts,
    )


def can_export(count: int, today: Optional[str] = None) -> tuple[bool, str]:
    """Check if exporting `count` more records would exceed budget.

    Returns (True, "") if allowed; (False, reason) if it would exceed.
    Zero or negative counts are treated as no-ops (allowed) so callers
    don't have to special-case them at every guard point. (record_export
    is stricter — it rejects non-positive counts to catch real bugs.)
    """
    if count <= 0:
        return True, ""
    status = get_quota_status(today)
    if status.consumed + count > status.budget:
        reason = (
            f"Would exceed monthly budget: "
            f"{status.consumed} consumed + {count} requested = "
            f"{status.consumed + count} > {status.budget} for {status.month}. "
            f"Override via PR_MONTHLY_RECORD_BUDGET env."
        )
        return False, reason
    return True, ""


def record_export(
    count: int,
    list_name: str = "",
    today: Optional[str] = None,
) -> QuotaStatus:
    """Increment current month's consumed count by `count`. Persist atomically.

    Fires a Slack alert if a new threshold (50/80/95/100% of budget) is
    crossed — first time only per month. Same threshold within the same
    month is deduped via the persisted `alerts_fired` array. A new month
    starts with an empty `alerts_fired` so the same thresholds will fire
    again in the new month.

    `list_name` is for log/alert context only — not persisted in the
    per-month record, to keep the schema compact.
    """
    if count <= 0:
        raise ValueError(f"record_export count must be positive, got {count}")

    month = _month_key(today)
    budget = get_monthly_budget()
    data = _load_raw()
    record = data["monthly"].setdefault(month, {"consumed": 0, "alerts_fired": []})
    previous_consumed = int(record.get("consumed", 0))
    new_consumed = previous_consumed + count
    record["consumed"] = new_consumed
    previous_alerts = set(record.get("alerts_fired", []))

    # Detect newly-crossed thresholds. We walk ALERT_THRESHOLDS in order
    # so a single big jump from 0 → 100% fires all four alerts on one call.
    newly_crossed: list[tuple[float, str]] = []
    for pct, token in ALERT_THRESHOLDS:
        threshold_count = int(pct * budget)
        if new_consumed >= threshold_count and token not in previous_alerts:
            newly_crossed.append((pct, token))
            previous_alerts.add(token)
    record["alerts_fired"] = sorted(previous_alerts, key=_alert_sort_key)

    _save_raw(data)

    # Slack alerts fire AFTER persistence — if the Slack webhook is down,
    # the registry is still durable and the alerts won't re-fire on the
    # next call (they're already recorded in alerts_fired).
    if newly_crossed:
        _fire_threshold_alerts(month, new_consumed, budget, newly_crossed, list_name)

    remaining = budget - new_consumed
    pct = new_consumed / budget if budget > 0 else 0.0
    status = QuotaStatus(
        month=month,
        consumed=new_consumed,
        budget=budget,
        remaining=remaining,
        pct_used=pct,
        alerts_fired=tuple(record["alerts_fired"]),
    )
    logger.info(
        "PR quota %s: %d/%d (%.1f%%) after +%d from %s",
        month, new_consumed, budget, pct * 100, count, list_name or "?",
    )
    return status


def format_quota_summary(today: Optional[str] = None) -> str:
    """One-line summary for daily Slack roll-up.

    Below budget: 'PR quota 2026-05: 4,250 / 10,000 records (42.5% used)'
    Over budget:  'PR quota 2026-05: 10,245 / 10,000 records (102.5% used — 245 over budget)'
    """
    status = get_quota_status(today)
    base = (
        f"PR quota {status.month}: {status.consumed:,} / {status.budget:,} "
        f"records ({status.pct_used * 100:.1f}% used"
    )
    if status.remaining < 0:
        base += f" — {-status.remaining:,} over budget"
    base += ")"
    return base


# ── Internal helpers ─────────────────────────────────────────────────
def _alert_sort_key(token: str) -> int:
    """Sort 50pct < 80pct < 95pct < 100pct by numeric prefix."""
    try:
        return int(token.rstrip("pct"))
    except ValueError:
        return 999  # unknown tokens sort last


def _fire_threshold_alerts(
    month: str,
    consumed: int,
    budget: int,
    newly_crossed: list[tuple[float, str]],
    list_name: str,
) -> None:
    """Send a Slack alert per newly-crossed threshold.

    Lazy import of slack_notifier so quota tests don't need to stub the
    Slack module at every test boundary — the fixture patches
    sys.modules["slack_notifier"] which this import will pick up.

    All alert failures are swallowed (logged at DEBUG) — quota tracking
    must never break the puller.
    """
    try:
        from slack_notifier import notify_error
    except ImportError:
        logger.warning("slack_notifier unavailable — quota threshold alert not sent")
        return

    for pct, token in newly_crossed:
        pct_int = int(pct * 100)
        summary = (
            f"PR quota crossed {pct_int}%: {consumed:,}/{budget:,} records "
            f"({consumed / budget * 100:.1f}%) consumed in {month}"
        )
        if list_name:
            summary += f" (most recent list: {list_name})"
        try:
            # Plan 04's T-02-04-01 pattern: operational alerts route
            # through notify_error with a synthetic exception. A future
            # notify_run_summary helper could replace this; the call
            # site is intentionally narrow so the swap is one line.
            notify_error(
                step=f"propertyradar_puller quota {token}",
                error=Exception(summary),
                context=f"month={month}, consumed={consumed}, budget={budget}",
            )
        except Exception:
            logger.debug("Could not send Slack quota alert", exc_info=True)
