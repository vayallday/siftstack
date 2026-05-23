"""Unit tests for src/propertyradar_puller.py.

All Playwright interactions are mocked. Live integration tests against the
real PR account are gated behind the `live_pr` pytest marker and excluded
from the default suite.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from playwright.async_api import TimeoutError as PwTimeout

import propertyradar_puller as pp
from propertyradar_config import PropertyRadarList


# ── Exception classes ──────────────────────────────────────────────

def test_quota_guard_error_is_exception():
    assert issubclass(pp.QuotaGuardError, Exception)


def test_async_export_unsupported_error_is_exception():
    assert issubclass(pp.AsyncExportUnsupportedError, Exception)


# ── Cookie helpers (PR-07 coexistence) ─────────────────────────────

@pytest.mark.asyncio
async def test_save_cookies_writes_to_pr_cookies_file(monkeypatch, tmp_path):
    target = tmp_path / "pr_cookies.json"
    monkeypatch.setattr(pp, "PR_COOKIES_FILE", target)
    ctx = MagicMock()
    ctx.cookies = AsyncMock(return_value=[{"name": "session", "value": "abc"}])
    await pp._save_cookies(ctx)
    assert target.exists()


@pytest.mark.asyncio
async def test_load_cookies_returns_false_when_missing(monkeypatch, tmp_path):
    monkeypatch.setattr(pp, "PR_COOKIES_FILE", tmp_path / "absent.json")
    ctx = MagicMock()
    ctx.add_cookies = AsyncMock()
    assert await pp._load_cookies(ctx) is False
    ctx.add_cookies.assert_not_called()


# ── Login: User Agreement tick precedes Login click ────────────────

@pytest.mark.asyncio
async def test_login_ticks_user_agreement_before_clicking_submit(monkeypatch):
    page = MagicMock()
    page.goto = AsyncMock()
    page.fill = AsyncMock()
    page.click = AsyncMock()
    page.evaluate = AsyncMock()

    loc = MagicMock()
    loc.first = MagicMock()
    loc.first.wait_for = AsyncMock()
    loc.first.inner_text = AsyncMock(return_value="Test User")
    page.locator = MagicMock(return_value=loc)

    monkeypatch.setattr(pp, "_dismiss_pr_popups", AsyncMock())
    monkeypatch.setattr(pp, "_is_session_valid", AsyncMock(return_value=True))
    monkeypatch.setattr(pp, "PROPERTYRADAR_EMAIL", "x@y.com")
    monkeypatch.setattr(pp, "PROPERTYRADAR_PASSWORD", "secret")

    order: list[tuple[str, str]] = []
    page.fill.side_effect = lambda *a, **k: order.append(("fill", a[0]))
    page.evaluate.side_effect = lambda *a, **k: order.append(("evaluate", a[0]))
    page.click.side_effect = lambda *a, **k: order.append(("click", a[0]))

    ok = await pp.login(page)
    assert ok is True

    ua_idx = next(
        i for i, x in enumerate(order)
        if x[0] == "evaluate" and "userAgreement" in x[1]
    )
    submit_idx = next(
        i for i, x in enumerate(order)
        if x[0] == "click" and "Login" in x[1]
    )
    assert ua_idx < submit_idx, (
        f"userAgreement evaluate (idx {ua_idx}) must come before "
        f"Login click (idx {submit_idx}); order={order}"
    )


# ── Quota guard ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_quota_guard_passes_under_threshold():
    await pp._quota_guard(
        PropertyRadarList("X", "VA", "foreclosure", "x"),
        delta_radar_ids=[str(i) for i in range(100)],
        max_records=500,
    )


@pytest.mark.asyncio
async def test_quota_guard_raises_over_threshold(monkeypatch):
    import sys
    fake = MagicMock()
    fake.notify_error = MagicMock()
    sys.modules["slack_notifier"] = fake

    with pytest.raises(pp.QuotaGuardError) as exc_info:
        await pp._quota_guard(
            PropertyRadarList("X", "VA", "foreclosure", "x"),
            delta_radar_ids=[str(i) for i in range(600)],
            max_records=500,
        )
    msg = str(exc_info.value)
    assert "600" in msg and "500" in msg
    fake.notify_error.assert_called_once()


# ── Empty-delta short-circuit skips export ─────────────────────────

@pytest.mark.asyncio
async def test_run_list_empty_delta_skips_export(monkeypatch, tmp_path):
    """When current scrape matches previous registry, no export runs and the
    returned notices list contains no real-acquisition NoticeData (and no
    exit/reentry, since membership hasn't changed)."""
    page = MagicMock()
    page.click = AsyncMock()
    page.evaluate = AsyncMock()
    monkeypatch.setattr(pp, "_dismiss_pr_popups", AsyncMock())
    # Post-fold: scrape returns records (dict keyed by RadarID), not just IDs.
    monkeypatch.setattr(
        pp, "_scrape_list_records",
        AsyncMock(return_value={
            "1": {"Address": "1 Main St"},
            "2": {"Address": "2 Main St"},
            "3": {"Address": "3 Main St"},
        }),
    )
    # _export_delta should NEVER be called
    ed = AsyncMock()
    monkeypatch.setattr(pp, "_export_delta", ed)

    prev_registry = {
        rid: {
            "first_seen": "2026-05-22",
            "last_seen": "2026-05-22",
            "exited_at": None,
            "status": "active",
            "data": {"Address": f"{rid} Main St"},
        }
        for rid in ("1", "2", "3")
    }
    notices, new_registry = await pp.run_list(
        page,
        PropertyRadarList("L", "VA", "foreclosure", "L"),
        previous_registry=prev_registry,
        download_dir=tmp_path,
    )
    assert notices == []                       # no real export, no exits, no reentries
    assert set(new_registry) == {"1", "2", "3"}  # all still active in registry
    assert all(r["status"] == "active" for r in new_registry.values())
    ed.assert_not_called()


# ── pull_all_lists: per-iteration persist ──────────────────────────

def _stub_async_playwright(monkeypatch):
    """Replace `async_playwright` with a stub that returns a benign page."""
    page_mock = MagicMock()
    page_mock.goto = AsyncMock()
    page_mock.evaluate = AsyncMock()
    ctx_mock = MagicMock()
    ctx_mock.set_default_timeout = lambda *_: None
    ctx_mock.cookies = AsyncMock(return_value=[])
    ctx_mock.new_page = AsyncMock(return_value=page_mock)
    browser_mock = MagicMock()
    browser_mock.new_context = AsyncMock(return_value=ctx_mock)
    browser_mock.close = AsyncMock()
    chromium = MagicMock()
    chromium.launch = AsyncMock(return_value=browser_mock)

    class FakePW:
        def __init__(self):
            self.chromium = chromium

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            pass

    monkeypatch.setattr(pp, "async_playwright", lambda: FakePW())
    return page_mock


@pytest.mark.asyncio
async def test_pull_all_lists_persists_state_after_each_success(monkeypatch, tmp_path):
    save_calls: list[dict] = []
    monkeypatch.setattr(pp, "save_pr_state", lambda s: save_calls.append(dict(s)))
    monkeypatch.setattr(pp, "load_pr_state", lambda: {})

    async def fake_run_list(page, pr_list, prev, dd, today=None):
        if pr_list.slug == "fails":
            raise RuntimeError("simulated")
        # Post-fold: returns (notices, new_registry) where the registry is a
        # dict[str, dict] keyed by RadarID.
        return [], {"A": {"status": "active"}, "B": {"status": "active"}}

    monkeypatch.setattr(pp, "run_list", fake_run_list)
    monkeypatch.setattr(pp, "_is_session_valid", AsyncMock(return_value=True))
    monkeypatch.setattr(pp, "_try_relogin", AsyncMock(return_value=False))
    _stub_async_playwright(monkeypatch)

    lists = [
        PropertyRadarList("one", "VA", "foreclosure", "ok"),
        PropertyRadarList("two", "MD", "foreclosure", "fails"),
    ]
    await pp.pull_all_lists(lists=lists, download_dir=tmp_path)

    assert any("one" in c for c in save_calls), "first list must persist"
    assert all("two" not in c for c in save_calls), "failed list must NOT persist"


# ── pull_all_lists: quota-guard wholesale abort ─────────────────────

@pytest.mark.asyncio
async def test_pull_all_lists_aborts_on_quota_guard(monkeypatch, tmp_path):
    monkeypatch.setattr(pp, "save_pr_state", lambda _: None)
    monkeypatch.setattr(pp, "load_pr_state", lambda: {})

    called: list[str] = []

    async def fake_run_list(page, pr_list, prev, dd, today=None):
        called.append(pr_list.slug)
        if pr_list.slug == "boom":
            raise pp.QuotaGuardError("simulated")
        return [], {}

    monkeypatch.setattr(pp, "run_list", fake_run_list)
    monkeypatch.setattr(pp, "_is_session_valid", AsyncMock(return_value=True))
    monkeypatch.setattr(pp, "_try_relogin", AsyncMock(return_value=False))
    _stub_async_playwright(monkeypatch)

    lists = [
        PropertyRadarList("one", "VA", "foreclosure", "one"),
        PropertyRadarList("two", "VA", "foreclosure", "boom"),
        PropertyRadarList("three", "MD", "foreclosure", "three"),
    ]
    await pp.pull_all_lists(lists=lists, download_dir=tmp_path)
    assert called == ["one", "boom"], (
        f"after quota-guard on list 2, list 3 must NOT run. called={called}"
    )


# ── Lifecycle (formerly plan 02-07, folded into the puller) ──────

def _active_record(rid: str, address: str = "X") -> dict:
    return {
        "first_seen": "2026-05-22",
        "last_seen": "2026-05-22",
        "exited_at": None,
        "status": "active",
        "data": {"Address": address},
    }


def test_compute_lifecycle_emits_exit_notices():
    """Properties present last run but missing this run produce
    `pr_lifecycle="exited"` synthetic NoticeData."""
    pr_list = PropertyRadarList("L", "VA", "foreclosure", "vaslug")
    prev = {"A": _active_record("A", "1 Main"), "B": _active_record("B", "2 Main")}
    current_records = {"A": {"Address": "1 Main"}}  # B is gone
    next_reg, exits, reentries = pp._compute_lifecycle(
        pr_list, current_records, prev, today="2026-05-23",
    )
    assert [n.pr_lifecycle for n in exits] == ["exited"]
    assert exits[0].pr_list_slug == "vaslug"
    assert exits[0].pr_lifecycle_date == "2026-05-23"
    assert exits[0].address == "2 Main"             # last-known data preserved
    assert exits[0].source_url == "propertyradar://radarid/B"
    assert next_reg["B"]["status"] == "exited"
    assert next_reg["B"]["exited_at"] == "2026-05-23"
    assert reentries == []


def test_compute_lifecycle_emits_reentry_notices():
    """A previously-exited RadarID reappearing produces a `pr_lifecycle=
    "reentered"` synthetic, and the registry clears its `exited_at`."""
    pr_list = PropertyRadarList("L", "VA", "foreclosure", "vaslug")
    prev = {
        "C": {
            "first_seen": "2026-04-01",
            "last_seen": "2026-05-10",
            "exited_at": "2026-05-11",
            "status": "exited",
            "data": {"Address": "3 Oak"},
        },
    }
    current_records = {"C": {"Address": "3 Oak"}}
    next_reg, exits, reentries = pp._compute_lifecycle(
        pr_list, current_records, prev, today="2026-05-23",
    )
    assert exits == []
    assert [n.pr_lifecycle for n in reentries] == ["reentered"]
    assert next_reg["C"]["status"] == "reentered"
    assert next_reg["C"]["exited_at"] is None
    assert next_reg["C"]["first_seen"] == "2026-04-01"  # original preserved


def test_compute_lifecycle_idempotent_on_repeat_exit():
    """Running twice with the same `prev` and an empty current must NOT
    produce a second exit notice for the already-exited record."""
    pr_list = PropertyRadarList("L", "VA", "foreclosure", "vaslug")
    prev = {
        "D": {
            "first_seen": "2026-04-01",
            "last_seen": "2026-05-22",
            "exited_at": "2026-05-22",
            "status": "exited",
            "data": {"Address": "4 Pine"},
        },
    }
    _, exits, reentries = pp._compute_lifecycle(
        pr_list, current_records={}, previous_registry=prev, today="2026-05-23",
    )
    assert exits == [] and reentries == []


def test_compute_lifecycle_first_run_has_no_synthetics():
    """Empty registry first run: no exits, no reentries — all current
    RadarIDs become brand-new (handled by the regular export path)."""
    pr_list = PropertyRadarList("L", "VA", "foreclosure", "vaslug")
    next_reg, exits, reentries = pp._compute_lifecycle(
        pr_list,
        current_records={"E": {"Address": "5 Elm"}, "F": {"Address": "6 Fir"}},
        previous_registry={},
        today="2026-05-23",
    )
    assert exits == [] and reentries == []
    assert set(next_reg) == {"E", "F"}
    assert all(r["status"] == "active" for r in next_reg.values())


def test_coerce_list_registry_migrates_v1_list_of_ids():
    """v1 state files stored `{list: [RadarID1, ...]}`. The coerce helper
    migrates them to v2 dict shape with default metadata."""
    legacy = ["G", "H"]
    out = pp._coerce_list_registry(legacy, today="2026-05-23")
    assert set(out) == {"G", "H"}
    for rec in out.values():
        assert rec["status"] == "active"
        assert rec["first_seen"] == "2026-05-23"
        assert rec["exited_at"] is None


def test_coerce_list_registry_passes_through_v2_dict():
    v2 = {"I": _active_record("I")}
    assert pp._coerce_list_registry(v2, today="2026-05-23") is v2


# ── _download_export: async-path TBD escalation ────────────────────

@pytest.mark.asyncio
async def test_download_export_raises_when_async_needed_but_not_captured(
    monkeypatch, tmp_path,
):
    monkeypatch.setattr(
        pp, "SEL_PR_DOWNLOADS_AREA", "__TBD_ASYNC_PATH_NOT_YET_CAPTURED__",
    )
    page = MagicMock()
    page.locator = MagicMock(return_value=MagicMock(
        first=MagicMock(click=AsyncMock()),
    ))

    class _Bombs:
        async def __aenter__(self):
            raise PwTimeout("sync export timed out")

        async def __aexit__(self, *_):
            pass

    page.expect_download = lambda **_: _Bombs()
    with pytest.raises(pp.AsyncExportUnsupportedError, match="TBD|Plan 02-03"):
        await pp._download_export(
            page,
            PropertyRadarList("X", "VA", "foreclosure", "xslug"),
            tmp_path,
        )
