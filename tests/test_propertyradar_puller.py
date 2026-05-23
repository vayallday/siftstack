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
    """When current scrape matches previous baseline, no export runs and the
    returned notices list is empty."""
    page = MagicMock()
    page.click = AsyncMock()
    page.evaluate = AsyncMock()
    monkeypatch.setattr(pp, "_dismiss_pr_popups", AsyncMock())
    monkeypatch.setattr(
        pp, "_scrape_list_members", AsyncMock(return_value=["1", "2", "3"]),
    )
    # _export_delta should NEVER be called
    ed = AsyncMock()
    monkeypatch.setattr(pp, "_export_delta", ed)

    notices, current = await pp.run_list(
        page,
        PropertyRadarList("L", "VA", "foreclosure", "L"),
        previous_radar_ids={"1", "2", "3"},
        download_dir=tmp_path,
    )
    assert notices == []
    assert set(current) == {"1", "2", "3"}
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
        return [], ["A", "B"]

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
        return [], []

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
