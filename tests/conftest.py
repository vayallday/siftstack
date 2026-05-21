"""Pytest shared configuration for SiftStack.

Adds `src/` to sys.path so tests can `from propertyradar_parser import ...`
without per-file path hacks (the existing script-style tests use their own
`sys.path.insert` calls — those keep working because conftest runs first
and idempotent sys.path inserts are harmless).

Also registers the `live_pr` marker for opt-in integration tests against
the real PropertyRadar account (those cost real billing).
"""

import sys
from pathlib import Path


# ── sys.path bootstrap ───────────────────────────────────────────────
_SRC = Path(__file__).resolve().parent.parent / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))


# ── Pytest marker registration ───────────────────────────────────────
def pytest_configure(config):
    # Defensive: also declare here in case pyproject.toml is ignored
    # (e.g., running pytest from a sub-directory that picks up a different
    # config). Belt-and-suspenders with the markers block in pyproject.toml.
    config.addinivalue_line(
        "markers",
        "live_pr: opt-in integration test against live PropertyRadar account "
        "(costs real billing; run only with -m live_pr)",
    )
