"""KVS-backed state-file persistence for Apify Actor runs.

The Apify Actor's container file system is wiped between runs, but its
KeyValueStore (KVS) is persistent. State files written to the local FS by
pullers (e.g., `chesterfield_aca_state.json`, `richmond_vacant_state.json`)
need to be hydrated from KVS at the start of each run and pushed back to
KVS at the end.

This module is intentionally minimal — no schema, no JSON-aware diffing,
just round-trip the file bytes. Each state file's own loader/saver handles
parsing.

Usage in actor_main:
    kvs = await Actor.open_key_value_store()
    await restore_state_file(kvs, "chesterfield_aca_state.json")
    ... run puller ...
    await persist_state_file(kvs, "chesterfield_aca_state.json")
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import config

logger = logging.getLogger(__name__)


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


async def restore_state_file(kvs: Any, state_filename: str) -> bool:
    """Restore a state file from KVS to the local filesystem.

    Returns True if a state value was found in KVS and written locally.
    Returns False if KVS has nothing for this key (first run) — caller can
    proceed; the puller will treat the missing file as a first-time run.
    """
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


async def persist_state_file(kvs: Any, state_filename: str) -> bool:
    """Persist a local state file's contents to KVS.

    Returns True if the file existed and was written to KVS. Returns False
    if the file is missing (puller never created it — typically because the
    run produced no records and bailed early).
    """
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


async def save_pending_records(kvs: Any, notices: list) -> bool:
    """Checkpoint the parsed NoticeData list to KVS.

    Called immediately after the PR puller returns + after merging any
    restored prior-run records, so the most recent batch of PR-exported
    records survives a mid-pipeline crash. Clear with
    `clear_pending_records` once DataSift upload reports success.
    """
    if not notices:
        # Nothing to save — and we don't want to overwrite a real
        # checkpoint with an empty one (would lose prior-run records on a
        # zero-delta day).
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


async def restore_pending_records(kvs: Any) -> list:
    """Restore a previously-checkpointed NoticeData list from KVS.

    Returns an empty list if no checkpoint exists, or if the checkpoint
    can't be deserialized for any reason (silently skip; the run will
    proceed with a fresh PR pull).
    """
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


async def clear_pending_records(kvs: Any) -> None:
    """Delete the pending-records checkpoint. Call after the day's records
    have successfully landed in DataSift so the next run doesn't re-process
    them (they'd just become duplicates in the CRM, modulo DataSift's
    address-based dedup)."""
    try:
        await kvs.set_value(PENDING_RECORDS_KEY, None)
        logger.info("Cleared pending records checkpoint from KVS")
    except Exception as e:
        logger.warning("KVS clear of %s failed: %s", PENDING_RECORDS_KEY, e)
