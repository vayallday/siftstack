"""KVS-backed state-file persistence for Apify Actor runs.

The Apify Actor's container file system is wiped between runs, AND each
new Actor run gets its own fresh "default" KeyValueStore — so writing
state to the default KVS does NOT survive across scheduled runs. The
chesterfield + richmond state persistence in the codebase had this latent
bug too; it was only providing within-run resilience (e.g. surviving
mid-run host migration) rather than the across-run delta-only behavior
the operator expected.

Fix: use a NAMED key-value store (`SIFTSTACK_PERSISTENT_KVS_NAME`).
Named KVS instances are stable across runs of the same actor, so writes
made by today's 5am cron are readable by tomorrow's 5am cron.

Each helper opens the named KVS itself, so callers don't have to thread
it through. The named KVS is created on first use.

Usage in actor_main:
    await restore_state_file("chesterfield_aca_state.json")
    ... run puller ...
    await persist_state_file("chesterfield_aca_state.json")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)

# Named KVS shared across all scheduled runs of this actor. Apify
# auto-creates it on first open. Naming convention is per-actor so
# multiple actors in the same account don't collide.
SIFTSTACK_PERSISTENT_KVS_NAME = "siftstack-persistent"


async def _open_persistent_kvs() -> Any:
    """Open the cross-run persistent KVS by name. Cached implicitly by
    the Apify SDK across calls within the same run."""
    from apify import Actor
    return await Actor.open_key_value_store(name=SIFTSTACK_PERSISTENT_KVS_NAME)


def _kvs_key(state_filename: str) -> str:
    """Map a state filename to a stable KVS key.

    e.g. "chesterfield_aca_state.json" -> "state__chesterfield_aca_state.json"
    The `state__` prefix avoids collisions with other KVS keys the Actor uses
    (output.csv, datasift_*, last_run_date, deep_prospecting_*.pdf, etc.).
    """
    return f"state__{state_filename}"


def _state_path(state_filename: str) -> Path:
    """Resolve the local path the puller expects."""
    return config.PROJECT_ROOT / state_filename


async def restore_state_file(kvs_or_filename, state_filename: str | None = None) -> bool:
    """Restore a state file from the persistent KVS to the local filesystem.

    Two call shapes are supported for backward compatibility:
      restore_state_file("chesterfield_aca_state.json")          # new
      restore_state_file(kvs, "chesterfield_aca_state.json")     # legacy

    In both cases the persistent (named) KVS is used regardless of which
    handle was passed; the legacy `kvs` argument is silently ignored
    because passing the run's default KVS was the bug that made cross-
    run persistence fail to begin with.

    Returns True if a state value was found in KVS and written locally.
    Returns False if KVS has nothing for this key (first run).
    """
    # Normalize args
    if state_filename is None:
        state_filename = kvs_or_filename
    kvs = await _open_persistent_kvs()

    key = _kvs_key(state_filename)
    try:
        value = await kvs.get_value(key)
    except Exception as e:
        logger.warning("KVS get_value(%s) failed: %s — treating as missing", key, e)
        return False

    if value is None:
        logger.info("No prior state in KVS for %s — first run", state_filename)
        return False

    # KVS may return str or bytes depending on content type detection
    if isinstance(value, str):
        data = value.encode("utf-8")
    elif isinstance(value, (bytes, bytearray)):
        data = bytes(value)
    else:
        # Apify SDK auto-deserialized JSON — re-serialize for the local file
        import json
        data = json.dumps(value, indent=2).encode("utf-8")

    path = _state_path(state_filename)
    path.write_bytes(data)
    logger.info("Restored state file %s from KVS (%d bytes)", state_filename, len(data))
    return True


async def persist_state_file(kvs_or_filename, state_filename: str | None = None) -> bool:
    """Persist a local state file's contents to the persistent KVS.

    Same backward-compat shape as restore_state_file — the legacy `kvs`
    argument is accepted but ignored; the named persistent KVS is always
    used so cross-run state actually survives.

    Returns True if the file existed and was written to KVS. Returns False
    if the file is missing (puller never created it — typically because the
    run produced no records and bailed early).
    """
    if state_filename is None:
        state_filename = kvs_or_filename
    kvs = await _open_persistent_kvs()

    path = _state_path(state_filename)
    if not path.exists():
        logger.info("No local state file at %s — nothing to persist", path)
        return False

    key = _kvs_key(state_filename)
    try:
        await kvs.set_value(key, path.read_bytes(), content_type="application/json")
        logger.info("Persisted state file %s to KVS", state_filename)
        return True
    except Exception as e:
        logger.warning("KVS set_value(%s) failed: %s", key, e)
        return False


# ── Pending-records checkpoint (mid-pipeline crash recovery) ─────────
#
# The PR puller costs us export quota whenever it pulls a record. If the
# pipeline dies AFTER the export step but BEFORE DataSift upload (e.g.,
# 2026-05-31 Apify container migration during obit Phase A), today's
# exported records are paid-for but never reach the CRM, and the next run
# sees them as "already known" via pr_state.json so they're effectively
# lost.
#
# Solution: serialize the parsed records list to KVS as a checkpoint
# right after PR pull completes, restore it on subsequent runs, and clear
# it only after DataSift upload succeeds. This way an interrupted run
# resumes from the same data without re-burning PR quota.

PENDING_RECORDS_KEY = "pending_records.json"


async def save_pending_records(arg1, arg2=None) -> bool:
    """Checkpoint the parsed NoticeData list to the persistent KVS.

    Accepts either `save_pending_records(notices)` (preferred) or
    `save_pending_records(kvs, notices)` (legacy — the kvs is ignored).
    """
    # Normalize args (legacy shape passed a KVS first)
    if arg2 is not None:
        notices = arg2
    else:
        notices = arg1
    kvs = await _open_persistent_kvs()

    if not notices:
        # Don't overwrite a real checkpoint with an empty one (would lose
        # prior-run records on a zero-delta day).
        return False
    try:
        import json
        from dataclasses import asdict
        payload = {
            "version": 1,
            "count": len(notices),
            "records": [asdict(n) for n in notices],
        }
        body = json.dumps(payload).encode("utf-8")
        await kvs.set_value(PENDING_RECORDS_KEY, body,
                             content_type="application/json")
        logger.info("Checkpointed %d pending records to KVS (%d bytes)",
                    len(notices), len(body))
        return True
    except Exception as e:
        logger.warning("Failed to checkpoint pending records: %s", e)
        return False


async def restore_pending_records(_kvs_legacy=None) -> list:
    """Restore a previously-checkpointed NoticeData list from the persistent KVS.

    Returns an empty list if no checkpoint exists, or if the checkpoint
    can't be deserialized for any reason. The legacy `kvs` arg is ignored.
    """
    kvs = await _open_persistent_kvs()
    try:
        value = await kvs.get_value(PENDING_RECORDS_KEY)
    except Exception as e:
        logger.warning("KVS get_value(%s) failed: %s", PENDING_RECORDS_KEY, e)
        return []
    if value is None:
        return []
    try:
        import json
        from notice_parser import NoticeData
        if isinstance(value, (bytes, bytearray)):
            payload = json.loads(bytes(value).decode("utf-8"))
        elif isinstance(value, str):
            payload = json.loads(value)
        else:
            # SDK auto-deserialized
            payload = value
        records_raw = payload.get("records") or []
        # NoticeData has 100+ fields; the dataclass tolerates missing
        # fields via defaults, but unknown fields would TypeError. Filter
        # to the dataclass's declared fields so a schema migration
        # between checkpoint write + read doesn't crash the restore.
        valid_fields = {f.name for f in NoticeData.__dataclass_fields__.values()}
        restored = []
        for r in records_raw:
            if not isinstance(r, dict):
                continue
            kwargs = {k: v for k, v in r.items() if k in valid_fields}
            try:
                restored.append(NoticeData(**kwargs))
            except Exception:
                # Skip individual record on parse error; keep the rest.
                continue
        logger.info("Restored %d pending records from KVS checkpoint", len(restored))
        return restored
    except Exception as e:
        logger.warning("Failed to deserialize pending records checkpoint: %s", e)
        return []


async def clear_pending_records(_kvs_legacy=None) -> None:
    """Delete the pending-records checkpoint from the persistent KVS. Call
    after the day's records have successfully landed in DataSift so the
    next run doesn't re-process them."""
    kvs = await _open_persistent_kvs()
    try:
        await kvs.set_value(PENDING_RECORDS_KEY, None)
        logger.info("Cleared pending records checkpoint from KVS")
    except Exception as e:
        logger.warning("KVS clear of %s failed: %s", PENDING_RECORDS_KEY, e)
