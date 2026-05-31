"""Entry point for SiftStack — full-stack REI operations platform.

Runs as either:
  - Apify Actor (when APIFY_IS_AT_HOME is set — reads input from Actor.get_input())
  - Standalone CLI (python src/main.py daily)
"""

import argparse
import asyncio
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

import config
from config import (
    LOG_DIR,
    OUTPUT_DIR,
)
from data_formatter import write_csv, write_csv_by_type

logger = logging.getLogger(__name__)


# ── Preflight health checks ─────────────────────────────────────────


def _preflight_check(mode: str) -> list[str]:
    """Verify required API keys and service connectivity before running.

    Returns a list of failure descriptions. Empty list = all checks passed.

    The daily/historical acquisition path is always PropertyRadar now —
    the TN public-notice scraper was archived to ``src/_legacy_tn/``.
    """
    failures: list[str] = []

    # ── Credential checks (mode-dependent) ──────────────────────────
    scrape_modes = {"daily", "historical"}
    enrichment_modes = scrape_modes | {
        "pdf-import", "photo-import", "dropbox-watch", "csv-import",
        "richmond-vacant", "chesterfield-code-violation",
    }
    datasift_modes = {"manage-presets", "manage-sold", "phone-validate"}

    if mode in scrape_modes:
        from propertyradar_config import (
            PROPERTYRADAR_EMAIL, PROPERTYRADAR_PASSWORD,
        )
        if not PROPERTYRADAR_EMAIL or not PROPERTYRADAR_PASSWORD:
            failures.append(
                "PROPERTYRADAR_EMAIL / PROPERTYRADAR_PASSWORD not set "
                "(required for daily/historical PropertyRadar pulls)"
            )

    if mode in enrichment_modes:
        # These are warnings, not blockers — pipeline degrades gracefully
        if not config.SMARTY_AUTH_ID or not config.SMARTY_AUTH_TOKEN:
            logger.warning("Preflight: SMARTY credentials missing — address standardization will be skipped")
        if not config.OPENWEBNINJA_API_KEY:
            logger.warning("Preflight: OPENWEBNINJA_API_KEY missing — Zillow enrichment will be skipped")
        if not config.ANTHROPIC_API_KEY:
            logger.warning("Preflight: ANTHROPIC_API_KEY missing — obituary search and LLM parsing will be skipped")

    if mode in datasift_modes:
        if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
            failures.append("DATASIFT_EMAIL / DATASIFT_PASSWORD not set (required for DataSift operations)")

    if mode == "dropbox-watch":
        if not config.DROPBOX_APP_KEY or not config.DROPBOX_APP_SECRET or not config.DROPBOX_REFRESH_TOKEN:
            failures.append("DROPBOX credentials incomplete (need APP_KEY, APP_SECRET, REFRESH_TOKEN)")

    if mode == "phone-validate":
        if not config.TRESTLE_API_KEY:
            failures.append("TRESTLE_API_KEY not set (required for phone validation)")

    return failures


# ── Apify Actor mode ─────────────────────────────────────────────────


async def _apify_run_simple_feed_mode(actor_input: dict, mode: str, pipeline_start: float) -> None:
    """Apify Actor flow for the bulk-feed modes (richmond-vacant, chesterfield-code-violation).

    These modes are simpler than the PR daily/historical flow — no Tracerfy,
    no DP PDF generation, no PR quota tracking. Just acquire → enrich → CSV
    → KVS + optional Drive + DataSift + Slack. State files round-trip
    through the Apify KVS so deltas work across runs.
    """
    from apify import Actor
    from time import time as _time
    import apify_state

    kvs = await Actor.open_key_value_store()

    # Per-mode wiring
    if mode == "chesterfield-code-violation":
        from chesterfield_aca_puller import (
            CHESTERFIELD_ACA_STATE_FILE,
            pull_new_records as _pull_aca,
        )
        from datetime import datetime as _dt
        state_filename = CHESTERFIELD_ACA_STATE_FILE.name
        source_label = "Chesterfield ACA Code Violation report"
        csv_prefix = "chesterfield_code_violation"

        await apify_state.restore_state_file(kvs, state_filename)

        # Optional date-window override from actor input
        aca_start_str = actor_input.get("aca_start") or ""
        aca_end_str = actor_input.get("aca_end") or ""
        aca_first_pull_days = int(actor_input.get("aca_first_pull_days") or 90)

        start_d = _dt.strptime(aca_start_str, "%Y-%m-%d").date() if aca_start_str else None
        end_d = _dt.strptime(aca_end_str, "%Y-%m-%d").date() if aca_end_str else None

        aca_all_violations = bool(actor_input.get("aca_all_violations", False))

        Actor.log.info("Pulling Chesterfield ACA Code Violation report")
        # Runs Playwright; can't be wrapped in asyncio.to_thread cleanly because
        # the puller spins up its own asyncio loop via asyncio.run().
        # Run in a worker thread so we don't conflict with Actor's outer loop.
        import asyncio as _asyncio
        notices = await _asyncio.to_thread(
            _pull_aca,
            start_date=start_d,
            end_date=end_d,
            first_pull_days=aca_first_pull_days,
            headless=True,
            all_violations=aca_all_violations,
        )
    elif mode == "richmond-vacant":
        from richmond_vacant_puller import (
            RICHMOND_VACANT_STATE_FILE,
            pull_new_records as _pull_vacant,
        )
        state_filename = RICHMOND_VACANT_STATE_FILE.name
        source_label = "Richmond Vacant Building List"
        csv_prefix = "richmond_vacant"

        await apify_state.restore_state_file(kvs, state_filename)

        Actor.log.info("Pulling Richmond Vacant Building List")
        notices = _pull_vacant()
    else:
        Actor.log.error("Unknown feed mode: %s", mode)
        await Actor.fail(status_message=f"Unknown feed mode: {mode}")
        return

    if not notices:
        Actor.log.info("No new records — exiting cleanly")
        # Even on zero-result runs, persist state in case the puller updated
        # its last_fetched_at / hash markers.
        await apify_state.persist_state_file(kvs, state_filename)
        return

    Actor.log.info("%s: %d new records", source_label, len(notices))

    # ── Enrichment ───────────────────────────────────────────────────
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    include_vacant = bool(actor_input.get("include_vacant", False))
    include_commercial = bool(actor_input.get("include_commercial", False))
    include_entities = bool(actor_input.get("include_entities", False))

    opts = PipelineOptions(
        skip_parcel_lookup=True,
        # Vacant Building List is by definition vacant — never filter it out.
        # ACA records can include vacant land too; let operator decide.
        skip_vacant_filter=True if mode == "richmond-vacant" else include_vacant,
        skip_commercial_filter=include_commercial,
        skip_entity_filter=include_entities,
        # OPP is Richmond-only — skip on Chesterfield-only batches to avoid log noise.
        skip_opp=(mode == "chesterfield-code-violation"),
        source_label=f"Apify Actor ({source_label})",
    )
    notices = run_enrichment_pipeline(notices, opts)
    if not notices:
        Actor.log.warning("No records remaining after enrichment")
        await apify_state.persist_state_file(kvs, state_filename)
        return

    total = len(notices)

    # ── CSV → KVS ────────────────────────────────────────────────────
    from datetime import datetime as _dt2
    ts = _dt2.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{csv_prefix}_{ts}.csv"
    csv_path = write_csv(notices, filename=filename)
    with open(csv_path, "rb") as f:
        await kvs.set_value("output.csv", f.read(), content_type="text/csv")
    Actor.log.info("CSV saved to KVS as 'output.csv'")

    # ── Push records to Apify Dataset ────────────────────────────────
    for n in notices:
        await Actor.push_data(n.__dict__)

    # ── Optional Google Drive upload ─────────────────────────────────
    drive_folder_id = actor_input.get("google_drive_folder_id", "")
    drive_key_b64 = actor_input.get("google_service_account_key", "")
    if drive_folder_id and drive_key_b64:
        try:
            from drive_uploader import upload_csv
            file_id = upload_csv(csv_path, drive_folder_id, drive_key_b64, total)
            Actor.log.info("CSV uploaded to Drive (file ID: %s)", file_id)
        except Exception as e:
            Actor.log.warning("Drive upload failed: %s — continuing", e)

    # ── Optional DataSift automated upload + KVS audit copy ──────────
    do_upload = bool(actor_input.get("upload_datasift", True))
    do_enrich_ds = bool(actor_input.get("enrich_datasift", True))
    do_skip_trace_ds = bool(actor_input.get("skip_trace_datasift", True))
    datasift_csv_urls: list[dict] = []

    from datasift_formatter import write_datasift_split_csvs
    try:
        csv_infos = write_datasift_split_csvs(notices)
    except Exception as e:
        Actor.log.error("DataSift CSV generation failed: %s", e)
        csv_infos = []

    if csv_infos and do_upload and config.DATASIFT_EMAIL and config.DATASIFT_PASSWORD:
        Actor.log.info("DataSift automated upload — %d CSV(s)", len(csv_infos))
        try:
            from datasift_uploader import upload_datasift_split
            result = await upload_datasift_split(
                csv_infos,
                headless=True,
                enrich=do_enrich_ds,
                skip_trace=do_skip_trace_ds,
            )
            if result.get("success"):
                Actor.log.info("DataSift upload OK: %s", result.get("message", ""))
            else:
                Actor.log.warning("DataSift upload failed: %s", result.get("message"))
        except Exception as e:
            Actor.log.warning("DataSift upload raised: %s — KVS audit copy below", e)

    # Audit copy: always save DataSift CSV(s) to KVS regardless of upload outcome
    if csv_infos:
        try:
            kvs_id = kvs._id if hasattr(kvs, "_id") else ""
            for info in csv_infos:
                key = f"datasift_{info['label'].lower().replace(' ', '_')}.csv"
                with open(info["path"], "rb") as f:
                    await kvs.set_value(key, f.read(), content_type="text/csv")
                url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                datasift_csv_urls.append({
                    "label": info["label"], "url": url,
                    "records": info.get("count", "?"),
                })
                Actor.log.info("DataSift CSV (%s) saved to KVS", info["label"])
        except Exception as e:
            Actor.log.error("KVS audit-copy save failed: %s", e)

    # ── Optional Slack notification ──────────────────────────────────
    elapsed_min = (_time() - pipeline_start) / 60
    do_notify_slack = bool(actor_input.get("notify_slack", True))
    if do_notify_slack and config.SLACK_WEBHOOK_URL:
        try:
            from slack_notifier import send_slack_notification, _send_webhook
            send_slack_notification(notices, elapsed_min=elapsed_min)
            if datasift_csv_urls:
                lines = [f"*DataSift CSV ({source_label}):*"]
                for ci in datasift_csv_urls:
                    lines.append(f"  <{ci['url']}|{ci['label']}> ({ci['records']} records)")
                _send_webhook("\n".join(lines))
        except Exception as e:
            Actor.log.warning("Slack notification failed: %s", e)

    # ── Persist state to KVS for next run ────────────────────────────
    await apify_state.persist_state_file(kvs, state_filename)
    Actor.log.info("Done — %d records exported (%.1f min)", total, elapsed_min)


async def actor_main() -> None:
    """Run as an Apify Actor — full automated pipeline.

    Scrape → Enrich → Tracerfy → DataSift Upload → Slack Notification.
    """
    from apify import Actor
    from time import time as _time

    # Set up Python logging so all modules output at INFO level
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    async with Actor:
        pipeline_start = _time()
        actor_input = await Actor.get_input() or {}

        # Override config credentials from Actor input.
        # Set both config.* AND os.environ so downstream modules that read
        # from either source (e.g., datasift_uploader uses os.environ) pick them up.
        _cred_map = {
            "ANTHROPIC_API_KEY": actor_input.get("anthropic_api_key", ""),
            "SMARTY_AUTH_ID": actor_input.get("smarty_auth_id", ""),
            "SMARTY_AUTH_TOKEN": actor_input.get("smarty_auth_token", ""),
            "OPENWEBNINJA_API_KEY": actor_input.get("openwebninja_api_key", ""),
            "SERPER_API_KEY": actor_input.get("serper_api_key", ""),
            "FIRECRAWL_API_KEY": actor_input.get("firecrawl_api_key", ""),
            "TRACERFY_API_KEY": actor_input.get("tracerfy_api_key", ""),
            "DATASIFT_EMAIL": actor_input.get("datasift_email", ""),
            "DATASIFT_PASSWORD": actor_input.get("datasift_password", ""),
            "SLACK_WEBHOOK_URL": actor_input.get("slack_webhook_url", ""),
            "TRESTLE_API_KEY": actor_input.get("trestle_api_key", ""),
            "PROPERTYRADAR_EMAIL": actor_input.get("pr_username", ""),
            "PROPERTYRADAR_PASSWORD": actor_input.get("pr_password", ""),
        }
        for key, val in _cred_map.items():
            setattr(config, key, val)
            if val:
                os.environ[key] = val

        # One-line credential-shape diagnostic. We've had two losing days on
        # DataSift login because the Actor input's `datasift_password` field
        # is stored as a 401-char ENCR... blob in the schedule body, and it's
        # not 100% clear whether Apify auto-decrypts before passing to the
        # Actor or whether we're sending the encrypted blob as the password.
        # Logging LENGTHS only (never values) lets us diagnose without
        # leaking secrets. Plaintext password is 13 chars; encrypted is 401.
        for _k in ("DATASIFT_PASSWORD", "PROPERTYRADAR_PASSWORD"):
            _v = _cred_map.get(_k, "")
            Actor.log.info(
                "Credential shape: %s length=%d prefix=%r",
                _k, len(_v), _v[:4] if _v else "",
            )

        mode = actor_input.get("mode", "daily")
        drive_folder_id = actor_input.get("google_drive_folder_id", "")
        drive_key_b64 = actor_input.get("google_service_account_key", "")

        # Pipeline toggles
        do_tracerfy = actor_input.get("run_tracerfy", True)
        do_notify_slack = actor_input.get("notify_slack", True)

        # Buy box / filter toggles
        include_vacant = actor_input.get("include_vacant", False)
        include_commercial = actor_input.get("include_commercial", False)
        include_entities = actor_input.get("include_entities", False)

        # ── Dispatch new bulk-feed modes (chesterfield, vacant) ─────────
        # These modes don't need PropertyRadar creds and have a much simpler
        # flow than daily/historical. Handled separately, then return.
        if mode in ("chesterfield-code-violation", "richmond-vacant"):
            await _apify_run_simple_feed_mode(actor_input, mode, pipeline_start)
            return

        # Validate PropertyRadar credentials (required for daily/historical).
        from propertyradar_config import (
            PROPERTYRADAR_EMAIL as _PR_EMAIL,
            PROPERTYRADAR_PASSWORD as _PR_PASS,
        )
        if not _PR_EMAIL or not _PR_PASS:
            Actor.log.error("pr_username and pr_password are required")
            try:
                from slack_notifier import notify_preflight_failure
                notify_preflight_failure(["PropertyRadar credentials missing"])
            except Exception:
                pass
            await Actor.fail(status_message="Missing PropertyRadar credentials")
            return

        # Log LLM parser status
        if config.ANTHROPIC_API_KEY:
            Actor.log.info("LLM fallback enabled (Claude Haiku) for missing fields")
        else:
            Actor.log.info("LLM fallback disabled — set anthropic_api_key to enable")

        try:
            kvs = await Actor.open_key_value_store()

            # ── Acquisition: PropertyRadar puller ─────────────────────
            # PR has no Added-Date filter; delta is a membership-set diff
            # against pr_state.json (see plan 02-04 SUMMARY). `mode` is
            # accepted only for back-compat — PR ignores it.
            #
            # Persist pr_state.json across Apify runs. The container FS is
            # wiped between runs, so without this every run sees ALL 1,477
            # current list members as "new" and re-exports the whole list —
            # burning PR export quota AND running full enrichment on records
            # already in DataSift. With KVS round-trip, only the actual delta
            # (~30-50/day) gets exported and enriched. Same pattern as
            # chesterfield + richmond modes use via apify_state.py.
            import apify_state
            await apify_state.restore_state_file(kvs, "pr_state.json")

            from propertyradar_puller import pull_all_lists
            Actor.log.info("Running PropertyRadar puller (mode=%s)", mode)
            notices = await pull_all_lists(
                download_dir=config.OUTPUT_DIR,
            )

            # Persist updated state immediately after the puller so a downstream
            # failure (Zillow circuit breaker, obit timeout, etc.) doesn't cost
            # us tomorrow's delta math. The puller wrote pr_state.json per-list
            # as it ran, so this is just the round-trip back to KVS.
            await apify_state.persist_state_file(kvs, "pr_state.json")

            # ── Enrichment ────────────────────────────────────────────
            from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

            opts = PipelineOptions(
                # PR exports already include parcel_id (APN); skip the lookup step.
                skip_parcel_lookup=True,
                skip_vacant_filter=include_vacant,
                skip_commercial_filter=include_commercial,
                skip_entity_filter=include_entities,
                source_label="Apify Actor",
            )
            notices = run_enrichment_pipeline(notices, opts)

            if not notices:
                Actor.log.warning("No notices found")
                return

            total = len(notices)

            # ── Tracerfy Skip Trace (DP candidates only) ────────────
            # Only run Tracerfy on records that need deep prospecting
            # (deceased owners, heir maps, decision makers). Basic records
            # get skip traced for free inside DataSift's unlimited plan.
            tracerfy_stats = None
            if do_tracerfy and config.TRACERFY_API_KEY:
                dp_for_tracerfy = [
                    n for n in notices
                    if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
                ]
                if dp_for_tracerfy:
                    Actor.log.info("Running Tracerfy on %d DP candidates (%d basic records skipped)...",
                                   len(dp_for_tracerfy), total - len(dp_for_tracerfy))
                    try:
                        from tracerfy_skip_tracer import batch_skip_trace
                        tracerfy_stats = batch_skip_trace(dp_for_tracerfy)
                        Actor.log.info(
                            "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
                            tracerfy_stats["matched"], tracerfy_stats["submitted"],
                            tracerfy_stats["phones_found"], tracerfy_stats["emails_found"],
                            tracerfy_stats["cost"],
                        )
                    except Exception as e:
                        Actor.log.warning("Tracerfy skip trace failed: %s — continuing", e)
                else:
                    Actor.log.info("No DP candidates — Tracerfy skipped (0 deceased/DM records)")
            elif do_tracerfy:
                Actor.log.info("Tracerfy skipped — no API key configured")

            # ── Generate Deep Prospecting PDFs ────────────────────────
            # Only generate PDFs for records that have deep prospecting data:
            # deceased owners with heir/DM info, or records with signing chains.
            # Basic records (just address + owner) don't need a PDF.
            pdf_urls = []
            dp_candidates = [
                n for n in notices
                if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
            ]

            # Score every phone (DM #1 + all heirs) with Trestle before rendering,
            # so signing-chain phones get tier badges — not just DM #1's.
            phone_tiers: dict = {}
            if dp_candidates and config.TRESTLE_API_KEY:
                try:
                    from phone_validator import score_record_phones
                    phone_tiers = score_record_phones(dp_candidates, config.TRESTLE_API_KEY)
                    Actor.log.info("Trestle scored %d unique phones across DP candidates",
                                   len(phone_tiers))
                except Exception as e:
                    Actor.log.warning("Per-record Trestle scoring failed: %s — continuing", e)

            if dp_candidates:
                try:
                    from report_generator import generate_record_pdf
                    kvs = await Actor.open_key_value_store()
                    kvs_id = kvs._id if hasattr(kvs, '_id') else ''
                    report_dir = Path("output/reports")

                    for n in dp_candidates:
                        pdf_path = generate_record_pdf(
                            n, output_dir=report_dir, phone_tiers=phone_tiers,
                        )
                        key = pdf_path.name
                        with open(pdf_path, "rb") as f:
                            await kvs.set_value(key, f.read(), content_type="application/pdf")
                        url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                        pdf_urls.append({"address": n.address, "url": url})

                    Actor.log.info("Generated %d deep prospecting PDFs (%d records skipped — no DP data)",
                                   len(pdf_urls), total - len(dp_candidates))
                except Exception as e:
                    Actor.log.warning("PDF generation failed: %s — continuing", e)
            else:
                Actor.log.info("No records need deep prospecting PDFs")

            # ── Write CSV ─────────────────────────────────────────────
            csv_path = write_csv(notices)
            if not kvs:
                kvs = await Actor.open_key_value_store()
            with open(csv_path, "rb") as f:
                await kvs.set_value("output.csv", f.read(), content_type="text/csv")
            Actor.log.info("CSV saved to key-value store as 'output.csv'")

            # ── Google Drive Upload ───────────────────────────────────
            if drive_folder_id and drive_key_b64:
                Actor.log.info("Uploading to Google Drive...")
                from drive_uploader import upload_csv, upload_summary

                by_type: dict[str, int] = {}
                by_county: dict[str, int] = {}
                for n in notices:
                    by_type[n.notice_type] = by_type.get(n.notice_type, 0) + 1
                    by_county[n.county] = by_county.get(n.county, 0) + 1

                file_id = upload_csv(csv_path, drive_folder_id, drive_key_b64, total)
                if file_id:
                    Actor.log.info("CSV uploaded to Drive (file ID: %s)", file_id)
                else:
                    Actor.log.error("CSV upload to Drive failed — CSV still in key-value store")

                upload_summary(by_type, by_county, total, drive_folder_id, drive_key_b64)
            elif drive_folder_id:
                Actor.log.warning("google_drive_folder_id set but google_service_account_key missing — skipping Drive upload")

            # ── DataSift: automated upload + KVS-CSV audit copy ───────
            # Phase 5 SCH-01 (2026-05-24): replaced the previous "save to KVS
            # for manual download + upload" flow with full automated headless
            # Playwright upload. Validated against live DataSift in headless
            # mode 2026-05-24 (smoke test in `.planning/phases/04-*/`),
            # which exercises the same path used here. KVS-CSV save still
            # runs as an audit copy + fallback so the operator can recover
            # manually if the headless upload ever regresses on a UI change.
            from datasift_formatter import write_datasift_split_csvs
            from datasift_uploader import upload_datasift_split

            datasift_csv_urls = []
            upload_result = {"success": False, "message": "not attempted"}

            do_upload = actor_input.get("upload_datasift", True)
            do_enrich_ds = actor_input.get("enrich_datasift", True)
            do_skip_trace_ds = actor_input.get("skip_trace_datasift", True)

            try:
                # phone_tiers comes from the Trestle scoring step above —
                # threading it into the formatter lets each row's Tags column
                # carry a `dial_first` / `dial_second` / etc. tag for the
                # record's best-ranked phone. Falls back to {} if Trestle
                # was skipped (no API key) so the formatter is no-op.
                csv_infos = write_datasift_split_csvs(notices, phone_tiers=phone_tiers)
            except Exception as e:
                Actor.log.error("DataSift CSV generation failed: %s — skipping DataSift step entirely", e)
                csv_infos = []

            # KVS audit copy — save the DataSift CSVs to the run's key-value
            # store BEFORE attempting the live upload. Reason: a previous run
            # (svw6EmItcaQfqoEhJ, 2026-05-26) OOM'd when Playwright launched
            # for the DataSift upload, and the CSV-fallback that used to run
            # after the upload attempt never fired — losing the entire
            # pull's record-of-truth. Doing the save first means the
            # operator can always recover manually, regardless of what
            # happens in the live-upload step.
            if csv_infos:
                try:
                    kvs = await Actor.open_key_value_store()
                    kvs_id = kvs._id if hasattr(kvs, '_id') else ''
                    for info in csv_infos:
                        key = f"datasift_{info['label'].lower().replace(' ', '_')}.csv"
                        with open(info["path"], "rb") as f:
                            await kvs.set_value(key, f.read(), content_type="text/csv")
                        url = f"https://api.apify.com/v2/key-value-stores/{kvs_id}/records/{key}"
                        datasift_csv_urls.append({"label": info["label"], "url": url, "records": info.get("count", "?")})
                        Actor.log.info("DataSift CSV (%s) saved to KVS pre-upload: %s", info["label"], key)
                except Exception as e:
                    Actor.log.error("KVS pre-upload save failed: %s", e)

            # Primary path: automated headless upload (only if credentials present + toggled on)
            if csv_infos and do_upload and config.DATASIFT_EMAIL and config.DATASIFT_PASSWORD:
                Actor.log.info(
                    "DataSift automated upload — %d CSV(s), enrich=%s, skip_trace=%s",
                    len(csv_infos), do_enrich_ds, do_skip_trace_ds,
                )
                try:
                    upload_result = await upload_datasift_split(
                        csv_infos,
                        headless=True,
                        enrich=do_enrich_ds,
                        skip_trace=do_skip_trace_ds,
                    )
                    if upload_result.get("success"):
                        Actor.log.info("DataSift upload OK: %s", upload_result.get("message", ""))
                    else:
                        Actor.log.warning(
                            "DataSift upload reported failure: %s — KVS-CSV fallback still saved below",
                            upload_result.get("message"),
                        )
                except Exception as e:
                    Actor.log.warning(
                        "DataSift upload raised: %s — KVS-CSV fallback still saved below", e,
                    )
                    upload_result = {"success": False, "message": str(e)}
            elif csv_infos and not do_upload:
                Actor.log.info("DataSift automated upload skipped (upload_datasift=false in input)")
            elif csv_infos:
                Actor.log.info(
                    "DataSift automated upload skipped — DATASIFT_EMAIL or DATASIFT_PASSWORD not set",
                )

            # (KVS audit copy moved to pre-upload position above so it
            # survives an OOM during the live upload step.)

            # ── Slack Notification ────────────────────────────────────
            elapsed_min = (_time() - pipeline_start) / 60

            # Compute estimated run cost
            # 2Captcha was a per-record charge under the now-archived TN scraper
            # (see src/_legacy_tn/captcha_solver.py). PR is a fixed-monthly Solo
            # plan with no per-record fee; export quota burn is tracked separately
            # via pr_quota.json + the Slack quota summary line.
            cost_breakdown = {}
            # Anthropic Haiku: ~$0.001 per record (LLM parsing + obituary search)
            if config.ANTHROPIC_API_KEY:
                cost_breakdown["Anthropic (Haiku)"] = round(total * 0.001, 3)
            # Tracerfy: actual cost from batch stats
            if tracerfy_stats and tracerfy_stats.get("cost", 0) > 0:
                cost_breakdown["Tracerfy"] = round(tracerfy_stats["cost"], 2)
            # Smarty: free tier 250/month, $0.01 after
            smarty_count = sum(1 for n in notices if n.dpv_match_code)
            if smarty_count > 0:
                cost_breakdown["Smarty"] = round(max(0, smarty_count - 250) * 0.01, 2) if smarty_count > 250 else 0.0
            # Zillow (OpenWeb Ninja): free tier 100/month, $0.01 after
            zillow_count = sum(1 for n in notices if n.estimated_value)
            if zillow_count > 0:
                cost_breakdown["Zillow"] = round(max(0, zillow_count - 100) * 0.01, 2) if zillow_count > 100 else 0.0
            # Remove zero-cost entries for cleaner display
            cost_breakdown = {k: v for k, v in cost_breakdown.items() if v > 0}

            if do_notify_slack and config.SLACK_WEBHOOK_URL:
                try:
                    from slack_notifier import send_slack_notification, _send_webhook

                    # Send standard run summary with cost breakdown
                    send_slack_notification(
                        notices,
                        elapsed_min=elapsed_min,
                        cost_breakdown=cost_breakdown,
                    )

                    # PR-09: append the monthly PropertyRadar quota line so the
                    # operator sees consumed/budget at a glance after every run.
                    try:
                        from propertyradar_quota import format_quota_summary
                        _send_webhook(format_quota_summary())
                    except Exception as quota_exc:
                        Actor.log.debug("Could not append PR quota summary: %s",
                                        quota_exc, exc_info=True)

                    # Send DataSift CSV download links as a follow-up message
                    if datasift_csv_urls:
                        csv_lines = [
                            "*DataSift CSVs ready for manual upload:*",
                        ]
                        for csv_info in datasift_csv_urls:
                            csv_lines.append(f"  <{csv_info['url']}|{csv_info['label']}> ({csv_info['records']} records)")
                        csv_lines.append("_Upload at app.reisift.io → Upload File → Add Data_")
                        _send_webhook("\n".join(csv_lines))

                    # Send PDF download links
                    if pdf_urls:
                        pdf_lines = [
                            f"*Deep Prospecting PDFs ({len(pdf_urls)} records):*",
                        ]
                        for pdf_info in pdf_urls:
                            pdf_lines.append(f"  <{pdf_info['url']}|{pdf_info['address']}>")
                        pdf_lines.append("_Attach to DataSift record → Notes or Files_")
                        _send_webhook("\n".join(pdf_lines))

                    Actor.log.info("Slack notification sent")
                except Exception as e:
                    Actor.log.warning("Slack notification failed: %s", e)

            # ── Save last_run_date to Apify KVS for next run ─────
            # (PropertyRadar dedup is driven by pr_state.json — written by
            # the puller itself — not by a notice-ID cache.)
            await kvs.set_value("last_run_date", datetime.now().strftime("%Y-%m-%d"))
            Actor.log.info("Saved last_run_date to KVS for next daily run")

            Actor.log.info("Done — %d notices exported (%.1f min)", total, elapsed_min)

        except Exception as e:
            Actor.log.error("Pipeline failed: %s", e, exc_info=True)
            try:
                from slack_notifier import notify_error
                notify_error("Apify Actor Pipeline", e, context=f"mode={mode}")
            except Exception:
                pass
            await Actor.fail(status_message=f"Pipeline error: {e}")


# ── CLI mode ──────────────────────────────────────────────────────────


def setup_logging(verbose: bool = False) -> None:
    """Configure logging to both console and date-stamped log file."""
    level = logging.DEBUG if verbose else logging.INFO
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    log_file = LOG_DIR / f"scrape_{timestamp}.log"

    # Force UTF-8 on console output to avoid cp1252 encoding errors on Windows
    console = logging.StreamHandler(
        open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
    )
    handlers: list[logging.Handler] = [
        console,
        logging.FileHandler(log_file, encoding="utf-8"),
    ]

    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )
    logging.info("Logging to %s", log_file)


def _run_pdf_import(args) -> None:
    """Run the PDF import pipeline: OCR → parse → enrich → CSV."""
    from pdf_importer import process_pdf
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.pdf_path:
        logging.error("--pdf-path is required for pdf-import mode")
        sys.exit(1)
    if not args.pdf_county:
        logging.error("--pdf-county is required for pdf-import mode")
        sys.exit(1)

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        logging.error("PDF file not found: %s", pdf_path)
        sys.exit(1)

    county = args.pdf_county.strip().title()

    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_pdf(
        pdf_path=pdf_path,
        county=county,
        api_key=api_key,
        date_added=args.pdf_date,
        regex_only=args.regex_only,
    )

    if not notices:
        logging.warning("No records extracted from PDF")
        sys.exit(0)

    # Run unified enrichment pipeline
    opts = PipelineOptions(
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"PDF import ({pdf_path.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_tax_sale_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_chesterfield_aca(args) -> None:
    """Pull Chesterfield ACA Code Violation report → diff vs state → enrich → CSV.

    Bulk feed (anonymous, public). See memory: chesterfield-aca-code-violation-report.
    """
    from datetime import date, datetime
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    from chesterfield_aca_puller import pull_new_records

    start = None
    end = None
    if getattr(args, "aca_start", None):
        start = datetime.strptime(args.aca_start, "%Y-%m-%d").date()
    if getattr(args, "aca_end", None):
        end = datetime.strptime(args.aca_end, "%Y-%m-%d").date()
    first_pull_days = getattr(args, "aca_first_pull_days", 90)
    headless = not getattr(args, "aca_headed", False)

    all_violations = bool(getattr(args, "aca_all_violations", False))

    logger.info("Pulling Chesterfield ACA Code Violation report")
    notices = pull_new_records(
        start_date=start,
        end_date=end,
        first_pull_days=first_pull_days,
        headless=headless,
        all_violations=all_violations,
    )

    if not notices:
        logging.info("No new Chesterfield ACA records — exiting")
        return

    logging.info("Chesterfield ACA delta: %d new records", len(notices))

    opts = PipelineOptions(
        skip_parcel_lookup=True,        # XLSX has no parcel — Smarty/assessor enrichment handles it
        skip_smarty=getattr(args, "skip_smarty", False),
        skip_zillow=getattr(args, "skip_zillow", False),
        skip_tax=getattr(args, "skip_tax", False),
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=getattr(args, "skip_obituary", False),
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_heir_verification=getattr(args, "skip_heir_verification", False),
        max_heir_depth=getattr(args, "max_heir_depth", 1),
        skip_dm_address=getattr(args, "skip_dm_address", False),
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        # OPP is Richmond-only; the enricher self-filters but skip explicitly
        # to avoid log noise on a Chesterfield-only batch.
        skip_opp=True,
        source_label="Chesterfield ACA Code Violation report",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"chesterfield_code_violation_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_richmond_vacant(args) -> None:
    """Pull Richmond Vacant Building List → diff vs state → enrich → CSV.

    Vacancy registry feed (notice_type='vacant_building'). NOT a code
    violation source — for Richmond code violations see the OPP enricher
    (src/richmond_opp_enricher.py). See memory: richmond-code-violations.
    """
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline
    from richmond_vacant_puller import pull_new_records

    logger.info("Pulling Richmond Vacant Building List")
    notices = pull_new_records()

    if not notices:
        logging.info("No new Vacant Building List records — exiting")
        return

    logging.info("Vacant Building List delta: %d new records", len(notices))

    opts = PipelineOptions(
        skip_parcel_lookup=True,
        skip_smarty=getattr(args, "skip_smarty", False),
        skip_zillow=getattr(args, "skip_zillow", False),
        skip_tax=getattr(args, "skip_tax", False),
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=getattr(args, "skip_obituary", False),
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_vacant_filter=True,    # vacant is the WHOLE POINT of this feed
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_heir_verification=getattr(args, "skip_heir_verification", False),
        max_heir_depth=getattr(args, "max_heir_depth", 1),
        skip_dm_address=getattr(args, "skip_dm_address", False),
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label="Richmond Vacant Building List",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No Vacant Building List records remaining after pipeline")
        return

    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"richmond_vacant_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_photo_import(args) -> None:
    """Run the photo import pipeline: preprocess → OCR → parse → enrich → CSV."""
    from photo_importer import process_photos
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    # Validate required args
    if not args.folder:
        logging.error("--folder is required for photo-import mode")
        sys.exit(1)
    if not args.photo_county:
        logging.error("--photo-county is required for photo-import mode")
        sys.exit(1)
    if not args.photo_type:
        logging.error("--photo-type is required for photo-import mode")
        sys.exit(1)

    folder = Path(args.folder)
    if not folder.exists() or not folder.is_dir():
        logging.error("Folder not found: %s", folder)
        sys.exit(1)

    county = args.photo_county.strip().title()

    notice_type = args.photo_type.strip().lower()
    api_key = config.ANTHROPIC_API_KEY or None

    # OCR + parse
    notices = process_photos(
        folder=folder,
        county=county,
        notice_type=notice_type,
        date_added=args.photo_date,
        api_key=api_key,
        correct_perspective=not getattr(args, "no_perspective_correct", False),
    )

    if not notices:
        logging.warning("No records extracted from photos")
        sys.exit(0)

    # Run unified enrichment pipeline
    # Skip vacant land filter for notice types without property addresses
    # (probate from court terminals never has property address — would filter everything)
    no_address_types = {"probate", "divorce"}
    opts = PipelineOptions(
        skip_vacant_filter=getattr(args, "include_vacant", False) or notice_type in no_address_types,
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_parcel_lookup=args.skip_tax,
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"Photo import ({folder.name})",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{county.lower()}_{notice_type}_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)
    logging.info("Done — %d records exported", len(notices))


def _run_csv_import(args) -> None:
    """Run the CSV re-import pipeline: read CSV → enrich → write new CSV.

    Supports multiple CSV paths (comma-separated) for merging datasets.
    Supports --upload-datasift to format and upload to DataSift after enrichment.
    """
    from data_formatter import read_csv
    from enrichment_pipeline import (
        PipelineOptions,
        detect_existing_enrichment,
        run_enrichment_pipeline,
    )

    # Validate required args
    if not args.csv_path:
        logging.error("--csv-path is required for csv-import mode")
        sys.exit(1)

    # Support multiple CSV paths (comma-separated)
    csv_paths = [Path(p.strip()) for p in args.csv_path.split(",")]
    for cp in csv_paths:
        if not cp.exists():
            logging.error("CSV file not found: %s", cp)
            sys.exit(1)

    county = None
    if args.csv_county:
        county = args.csv_county.strip().title()

    # Read all CSVs → NoticeData, merge
    all_notices = []
    for cp in csv_paths:
        batch = read_csv(cp)
        logging.info("Loaded %d records from %s", len(batch), cp.name)
        all_notices.extend(batch)

    if not all_notices:
        logging.warning("No records found in CSV(s)")
        sys.exit(0)

    # Deduplicate by source_url (notice ID) — keeps most recent
    seen_urls = {}
    for n in all_notices:
        url = getattr(n, "source_url", "") or ""
        if url and url in seen_urls:
            # Keep the one with more enrichment data
            existing = seen_urls[url]
            if (getattr(n, "estimated_value", "") or "") and not (getattr(existing, "estimated_value", "") or ""):
                seen_urls[url] = n
        elif url:
            seen_urls[url] = n
        else:
            # No source_url — keep all (dedup by address later)
            seen_urls[id(n)] = n
    notices = list(seen_urls.values())
    if len(notices) < len(all_notices):
        logging.info("Deduped %d → %d records (by source_url)", len(all_notices), len(notices))

    # Override county if provided (for CSVs without county column)
    if county:
        for n in notices:
            if not n.county.strip():
                n.county = county

    logging.info("Total: %d records from %d CSV(s)", len(notices), len(csv_paths))

    # Build pipeline options
    primary_name = csv_paths[0].name
    opts = PipelineOptions(
        skip_filter_sold=False,
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_smarty=args.skip_smarty,
        skip_zillow=args.skip_zillow,
        skip_tax=args.skip_tax,
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"CSV import ({primary_name})",
    )
    detect_existing_enrichment(notices, opts)
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No records remaining after pipeline")
        return

    # Write output
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    filename = f"{csv_paths[0].stem}_reimport_{timestamp}.csv"
    path = write_csv(notices, filename=filename)
    logging.info("Output: %s", path)

    # DataSift upload (same logic as daily/historical mode)
    if getattr(args, "upload_datasift", False):
        from datasift_formatter import write_datasift_split_csvs
        from datasift_uploader import upload_datasift_split, upload_to_datasift

        do_enrich = not getattr(args, "no_enrich", False)
        do_skip_trace = not getattr(args, "no_skip_trace", False)

        csv_infos = write_datasift_split_csvs(notices)
        for info in csv_infos:
            logging.info("DataSift CSV (%s): %s", info["label"], info["path"])

        if len(csv_infos) > 1:
            upload_result = asyncio.run(
                upload_datasift_split(
                    csv_infos,
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )
        else:
            upload_result = asyncio.run(
                upload_to_datasift(
                    csv_infos[0]["path"],
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )

        if upload_result.get("success"):
            logging.info("DataSift upload: %s", upload_result.get("message", "OK"))
        else:
            logging.error("DataSift upload failed: %s", upload_result.get("message"))

    logging.info("Done — %d records exported", len(notices))


def _run_phone_validate(args) -> None:
    """Run phone validation via Trestle API with DataSift export/upload."""
    import json as _json

    csv_path = getattr(args, "csv_path", None)
    list_name = getattr(args, "list_name", None)
    preset_folder = getattr(args, "preset_folder", None)
    all_records = getattr(args, "all_records", False)

    # Must specify at least one targeting mode
    if not csv_path and not list_name and not preset_folder and not all_records:
        logging.error(
            "phone-validate requires one of: --csv-path, --list-name, --preset-folder, or --all-records"
        )
        sys.exit(1)

    # Parse custom tiers if provided
    tiers = None
    custom_tiers_str = getattr(args, "custom_tiers", None)
    if custom_tiers_str:
        try:
            raw = _json.loads(custom_tiers_str)
            tiers = {k: tuple(v) for k, v in raw.items()}
            logging.info("Using custom tiers: %s", tiers)
        except (_json.JSONDecodeError, ValueError) as e:
            logging.error("Invalid --custom-tiers JSON: %s", e)
            sys.exit(1)

    # Estimate-only mode
    if getattr(args, "estimate", False):
        from phone_validator import estimate_cost, print_estimate

        if csv_path:
            est = estimate_cost(csv_path)
            print_estimate(est)
        else:
            logging.error("--estimate requires --csv-path (export from DataSift first, then estimate)")
            sys.exit(1)
        return

    # Full validation workflow
    from datasift_uploader import run_phone_validation_workflow

    result = asyncio.run(run_phone_validation_workflow(
        list_name=list_name,
        preset_folder=preset_folder,
        all_records=all_records,
        csv_path=csv_path,
        upload_tags=not getattr(args, "no_upload", False),
        api_key=config.TRESTLE_API_KEY or None,
        tiers=tiers,
        add_litigator=getattr(args, "add_litigator", False),
        batch_size=getattr(args, "batch_size", 10),
    ))

    if result.get("success"):
        logging.info("Phone validation: %s", result.get("message", "OK"))
        if result.get("validation_result"):
            vr = result["validation_result"]
            logging.info("  Results: %d scored, %d errors", vr.get("results_count", 0), vr.get("errors_count", 0))
            for tag, count in vr.get("tier_counts", {}).items():
                logging.info("    %s: %d", tag, count)
        if result.get("upload_result"):
            logging.info("  Tag upload: %s", result["upload_result"].get("message", ""))
    else:
        logging.error("Phone validation failed: %s", result.get("message"))
        sys.exit(1)


def _run_manage_presets(args) -> None:
    """Run the DataSift filter preset management workflow."""
    from datasift_uploader import run_manage_presets_workflow

    discover = getattr(args, "discover", False)
    add_sold = getattr(args, "add_sold_exclusion", False)
    create_seq = getattr(args, "create_sold_sequence", False)

    # Default to discover if no flags specified
    if not (discover or add_sold or create_seq):
        discover = True

    preset_folders = None
    if getattr(args, "preset_folders", None):
        preset_folders = [f.strip() for f in args.preset_folders.split(",")]

    result = asyncio.run(run_manage_presets_workflow(
        discover=discover,
        add_sold_exclusion=add_sold,
        create_sequence=create_seq,
        preset_folders=preset_folders,
    ))

    if result.get("success"):
        logging.info("Manage presets: %s", result.get("message", "OK"))
        if result.get("discovery"):
            disc = result["discovery"]
            for folder, presets in disc.get("preset_folders", {}).items():
                logging.info("  Folder '%s': %s", folder, presets)
            logging.info("  Sequences: %s", disc.get("sequences", []))
        if result.get("presets"):
            p = result["presets"]
            logging.info("  Updated: %s", p.get("updated", []))
            logging.info("  Failed: %s", p.get("failed", []))
        if result.get("sequence"):
            logging.info("  Sequence: %s", result["sequence"].get("message"))
    else:
        logging.error("Manage presets failed: %s", result.get("message"))
        sys.exit(1)


def _run_manage_sold(args) -> None:
    """Run the SiftMap sold properties management workflow."""
    from datasift_uploader import run_manage_sold_workflow

    # Parse counties if provided, otherwise let downstream pick the default.
    counties = None
    if args.counties and args.counties.lower() != "all":
        counties = [c.strip().title() for c in args.counties.split(",")]

    result = asyncio.run(run_manage_sold_workflow(
        counties=counties,
        months_back=getattr(args, "months_back", 1),
        min_sale_price=getattr(args, "min_sale_price", 1000),
        sold_tag_date=getattr(args, "sold_tag_date", None),
    ))

    if result.get("success"):
        logging.info("Manage sold: %s", result.get("message", "OK"))
        logging.info("  Counties: %s", ", ".join(result.get("counties_processed", [])))
        logging.info("  Total records: %d", result.get("total_records", 0))
    else:
        logging.error("Manage sold failed: %s", result.get("message"))
        sys.exit(1)


def cli_main() -> None:
    """Run as standalone CLI."""
    parser = argparse.ArgumentParser(
        description="SiftStack — full-stack REI operations platform"
    )
    parser.add_argument(
        "mode",
        choices=[
            "daily", "historical", "pdf-import", "photo-import", "dropbox-watch",
            "csv-import", "phone-validate", "manage-sold", "manage-presets",
            "richmond-vacant", "chesterfield-code-violation",
            # New analysis & workflow modes
            "comp", "rehab", "analyze-deal", "market-analysis", "buyer-prospect",
            "deep-prospect", "lead-manage", "setup-sequences", "niche-sequential",
            "playbook",
        ],
        help=(
            "daily/historical = scrape notices; pdf-import/photo-import = import from files; "
            "dropbox-watch = poll Dropbox; csv-import = re-enrich CSV; "
            "phone-validate = Trestle scoring; manage-sold/manage-presets = DataSift ops; "
            "richmond-vacant = pull Richmond Vacant Building List side feed; "
            "chesterfield-code-violation = pull Chesterfield ACA bulk code violation report; "
            "comp = comparable sales ARV; rehab = rehab cost estimate; "
            "analyze-deal = full deal analysis; market-analysis = zip code scoring; "
            "buyer-prospect = cash buyer lists; deep-prospect = 4-level research; "
            "lead-manage = 4 Pillars qualification; setup-sequences = CRM automation; "
            "niche-sequential = marketing cycle; playbook = SOP generator"
        ),
    )
    parser.add_argument(
        "--counties",
        type=str,
        default=None,
        help='Comma-separated county filter (no-op for PropertyRadar — list registry handles scoping)',
    )
    parser.add_argument(
        "--types",
        type=str,
        default=None,
        help='Comma-separated notice types (e.g. "foreclosure,probate" or "all")',
    )
    parser.add_argument(
        "--split",
        action="store_true",
        help="Output separate CSV files per notice type",
    )
    parser.add_argument(
        "--since",
        type=str,
        default=None,
        help="Override date cutoff (YYYY-MM-DD). Overrides daily/historical mode logic.",
    )
    parser.add_argument(
        "--max-notices",
        type=int,
        default=0,
        help="Stop after scraping this many notices (0 = no limit)",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="Enable debug logging",
    )
    # PDF import arguments
    parser.add_argument(
        "--pdf-path",
        type=str,
        default=None,
        help="Path to scanned tax sale PDF (required for pdf-import mode)",
    )
    parser.add_argument(
        "--pdf-county",
        type=str,
        default=None,
        help='County name for PDF import, e.g. "Henrico" (required for pdf-import mode)',
    )
    parser.add_argument(
        "--pdf-date",
        type=str,
        default=None,
        help="Date for PDF records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--regex-only",
        action="store_true",
        help="Skip LLM parsing and use regex only (pdf-import mode)",
    )
    # Photo import arguments
    parser.add_argument(
        "--folder",
        type=str,
        default=None,
        help="Path to folder of phone photos (required for photo-import mode)",
    )
    parser.add_argument(
        "--photo-county",
        type=str,
        default=None,
        dest="photo_county",
        help='County name for photo import, e.g. "Henrico" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-type",
        type=str,
        default=None,
        dest="photo_type",
        help='Notice type for photo import, e.g. "eviction" (required for photo-import mode)',
    )
    parser.add_argument(
        "--photo-date",
        type=str,
        default=None,
        help="Date for photo records (YYYY-MM-DD). Defaults to today.",
    )
    parser.add_argument(
        "--no-perspective-correct",
        action="store_true",
        dest="no_perspective_correct",
        help="Skip perspective correction in photo preprocessing (photo-import mode)",
    )
    # Chesterfield ACA Code Violation report args
    parser.add_argument(
        "--aca-start",
        type=str,
        default=None,
        dest="aca_start",
        help="Override start date YYYY-MM-DD (chesterfield-code-violation mode)",
    )
    parser.add_argument(
        "--aca-end",
        type=str,
        default=None,
        dest="aca_end",
        help="Override end date YYYY-MM-DD (chesterfield-code-violation mode)",
    )
    parser.add_argument(
        "--aca-first-pull-days",
        type=int,
        default=90,
        dest="aca_first_pull_days",
        help="Window size in days for the first ACA pull (default 90)",
    )
    parser.add_argument(
        "--aca-headed",
        action="store_true",
        dest="aca_headed",
        help="Run ACA puller with visible browser (default: headless)",
    )
    parser.add_argument(
        "--aca-all-violations",
        action="store_true",
        dest="aca_all_violations",
        help=(
            "Bypass the HIGH_MOTIVATION_CODE_SECTIONS filter (default = vacant only) "
            "and emit every Chesterfield code violation case"
        ),
    )
    # Dropbox watcher arguments
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=None,
        dest="poll_interval",
        help="Seconds between Dropbox polls (default: 900 = 15 min)",
    )
    parser.add_argument(
        "--max-polls",
        type=int,
        default=None,
        dest="max_polls",
        help="Maximum number of poll cycles (default: infinite)",
    )
    parser.add_argument(
        "--no-delete",
        action="store_true",
        dest="no_delete",
        help="Don't delete photos from Dropbox after processing",
    )
    # CSV import arguments
    parser.add_argument(
        "--csv-path",
        type=str,
        default=None,
        help="Path to existing CSV file to re-enrich (required for csv-import mode)",
    )
    parser.add_argument(
        "--csv-county",
        type=str,
        default=None,
        help='County name for CSV import, e.g. "Henrico" (sets county for records missing it)',
    )

    parser.add_argument(
        "--skip-smarty",
        action="store_true",
        help="Skip Smarty address standardization",
    )
    parser.add_argument(
        "--skip-zillow",
        action="store_true",
        help="Skip Zillow property enrichment",
    )
    parser.add_argument(
        "--skip-tax",
        action="store_true",
        help="Skip tax delinquency enrichment",
    )
    parser.add_argument(
        "--skip-obituary",
        action="store_true",
        help="Skip obituary search for deceased owner detection",
    )
    parser.add_argument(
        "--skip-ancestry",
        action="store_true",
        help="Skip Ancestry.com lookup (SSDI + obituary collection)",
    )
    parser.add_argument(
        "--skip-geocode",
        action="store_true",
        help="Skip reverse geocode retry for failed Smarty lookups",
    )
    parser.add_argument(
        "--skip-dm-address",
        action="store_true",
        help="Skip decision-maker mailing address lookup",
    )
    parser.add_argument(
        "--skip-heir-verification",
        action="store_true",
        help="Skip heir alive/dead verification loop (still runs obituary search)",
    )
    parser.add_argument(
        "--max-heir-depth",
        type=int,
        default=2,
        help="Max recursion depth for heir verification (default: 2)",
    )
    parser.add_argument(
        "--tracerfy-tier1",
        action="store_true",
        help="Use Tracerfy as primary DM address lookup ($0.02/record)",
    )
    parser.add_argument(
        "--skip-tracerfy",
        action="store_true",
        help="Skip Tracerfy batch skip trace (phones + emails) before DataSift upload",
    )
    parser.add_argument(
        "--llm-backend",
        choices=["anthropic", "ollama", "openrouter"],
        default=os.getenv("LLM_BACKEND", "anthropic"),
        help="LLM backend: 'anthropic' (Claude Haiku, paid) or 'ollama' (local, free)",
    )
    parser.add_argument(
        "--research-entities",
        action="store_true",
        help="Research entity-owned properties to find the person behind LLCs/Corps (web search + LLM)",
    )
    # Buy box / filter toggles — control which property types pass through
    parser.add_argument(
        "--include-vacant",
        action="store_true",
        help="Keep vacant land parcels (default: filtered out). Use if your buy box includes land deals.",
    )
    parser.add_argument(
        "--include-commercial",
        action="store_true",
        help="Keep commercial properties (default: filtered out). Use if your buy box includes commercial.",
    )
    parser.add_argument(
        "--include-entities",
        action="store_true",
        help="Keep entity-owned records (LLC, Corp, etc.) without filtering. Default: removed unless --research-entities finds a person.",
    )
    parser.add_argument(
        "--upload-datasift",
        action="store_true",
        help="Upload results to DataSift.ai via Playwright (requires DATASIFT_EMAIL/PASSWORD)",
    )
    parser.add_argument(
        "--no-enrich",
        action="store_true",
        help="Skip DataSift property enrichment after upload",
    )
    parser.add_argument(
        "--no-skip-trace",
        action="store_true",
        help="Skip DataSift skip trace after upload",
    )
    parser.add_argument(
        "--notify-slack",
        action="store_true",
        help="Send run summary to Slack/Discord webhook (requires SLACK_WEBHOOK_URL)",
    )
    parser.add_argument(
        "--audit-records",
        action="store_true",
        help="Audit DataSift for incomplete records (future: daily check via Playwright)",
    )

    # Phone validation arguments
    parser.add_argument(
        "--list-name",
        type=str,
        default=None,
        help="DataSift list name to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--preset-folder",
        type=str,
        default=None,
        help="DataSift preset folder to export phones from (phone-validate mode)",
    )
    parser.add_argument(
        "--all-records",
        action="store_true",
        help="Export all DataSift records for phone validation (phone-validate mode)",
    )
    parser.add_argument(
        "--estimate",
        action="store_true",
        help="Show phone validation cost estimate only, no API calls (phone-validate mode)",
    )
    parser.add_argument(
        "--no-upload",
        action="store_true",
        help="Skip uploading phone tags back to DataSift (phone-validate mode)",
    )
    parser.add_argument(
        "--custom-tiers",
        type=str,
        default=None,
        help='JSON custom tier boundaries, e.g. \'{"Hot": [80,100], "Cold": [0,79]}\' (phone-validate mode)',
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Concurrent Trestle API requests per batch (phone-validate mode, default: 10)",
    )
    parser.add_argument(
        "--add-litigator",
        action="store_true",
        help="Include litigator risk check in phone validation (phone-validate mode)",
    )

    # Manage sold arguments
    parser.add_argument(
        "--months-back",
        type=int,
        default=1,
        help="Months of sales to pull from SiftMap (manage-sold mode, default: 1)",
    )
    parser.add_argument(
        "--min-sale-price",
        type=int,
        default=1000,
        help="Min sale price to exclude deed transfers (manage-sold mode, default: 1000)",
    )
    parser.add_argument(
        "--sold-tag-date",
        type=str,
        default=None,
        help="Tag date in YYYY-MM format (manage-sold mode, default: current month)",
    )

    # Manage presets arguments
    parser.add_argument(
        "--discover",
        action="store_true",
        help="Discover and list all preset folders, presets, and sequences (manage-presets mode)",
    )
    parser.add_argument(
        "--add-sold-exclusion",
        action="store_true",
        help="Update existing presets to exclude Sold status/tag (manage-presets mode)",
    )
    parser.add_argument(
        "--create-sold-sequence",
        action="store_true",
        help="Create Sold Property Cleanup sequence (manage-presets mode)",
    )
    parser.add_argument(
        "--preset-folders",
        type=str,
        default=None,
        help='Comma-separated preset folder names to target (manage-presets mode, default: all)',
    )

    # ── New analysis & workflow mode arguments ────────────────────────
    # Comp analysis
    parser.add_argument("--address", type=str, default=None,
                        help="Property address (comp/rehab/analyze-deal modes)")
    parser.add_argument("--city", type=str, default=None,
                        help="Property city (comp/rehab/analyze-deal modes)")
    parser.add_argument("--zip-code", type=str, default=None,
                        help="Property ZIP code (comp/rehab/analyze-deal modes)")
    parser.add_argument("--radius", type=float, default=0.5,
                        help="Comp search radius in miles (comp mode, default: 0.5)")
    parser.add_argument("--months", type=int, default=6,
                        help="Comp lookback months (comp mode, default: 6)")

    # Rehab estimation
    parser.add_argument("--tier", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Finish tier 1-4 (rehab mode, default: 2)")
    parser.add_argument("--scope", type=str, default="full", choices=["full", "wholetail"],
                        help="Rehab scope (rehab mode, default: full)")
    parser.add_argument("--region", type=str, default="",
                        help="Regional pricing slug (rehab mode; rehab_estimator's REGIONAL_MULTIPLIERS keys)")
    parser.add_argument("--sqft", type=int, default=0,
                        help="Property sqft override (rehab mode)")
    parser.add_argument("--bedrooms", type=int, default=0,
                        help="Bedrooms override (rehab mode)")
    parser.add_argument("--bathrooms", type=float, default=0,
                        help="Bathrooms override (rehab mode)")

    # Deal analysis
    parser.add_argument("--purchase-price", type=float, default=0,
                        help="Purchase price (analyze-deal mode, default: auto-calculate MAO)")
    parser.add_argument("--rehab-tier", type=int, default=2, choices=[1, 2, 3, 4],
                        help="Rehab tier for deal analysis (default: 2)")
    parser.add_argument("--exit-strategy", type=str, default="flip",
                        choices=["flip", "wholesale", "hold"],
                        help="Exit strategy (analyze-deal mode, default: flip)")

    # Market analysis
    parser.add_argument("--zip-codes", type=str, default=None,
                        help="Comma-separated ZIP codes to analyze (market-analysis mode)")
    parser.add_argument("--monthly-budget", type=float, default=5000,
                        help="Monthly marketing budget for allocation (market-analysis mode)")

    # Buyer prospecting
    parser.add_argument("--min-transactions", type=int, default=2,
                        help="Min transactions to qualify as investor (buyer-prospect mode)")

    # Deep prospecting
    parser.add_argument("--depth", type=int, default=3, choices=[1, 2, 3, 4],
                        help="Research depth level 1-4 (deep-prospect mode, default: 3)")

    # Lead management
    parser.add_argument("--lead-action", type=str, default="qualify",
                        choices=["qualify", "report"],
                        help="Lead management action (lead-manage mode)")

    # Sequence setup
    parser.add_argument("--seq-folder", type=str, default="all",
                        choices=["lead-management", "acquisitions", "transactions",
                                 "deep-prospecting", "default", "all"],
                        help="Sequence folder to create (setup-sequences mode)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Preview changes without creating (setup-sequences/niche-sequential)")

    # Niche sequential
    parser.add_argument("--channel", type=str, default="sms",
                        choices=["sms", "call", "mail", "dp"],
                        help="Marketing channel (niche-sequential mode)")
    parser.add_argument("--day", type=int, default=1, choices=[1, 2, 3],
                        help="Cycle day 1-3 (niche-sequential mode)")
    parser.add_argument("--ns-action", type=str, default="execute",
                        choices=["execute", "setup-presets", "status"],
                        help="Niche sequential action (niche-sequential mode)")

    # Playbook
    parser.add_argument("--blueprint", type=str, default="wholesale",
                        choices=["wholesale", "flip", "hold", "hybrid"],
                        help="Investment blueprint (playbook mode)")
    parser.add_argument("--market", type=str, default="",
                        help="Target market (playbook mode)")
    parser.add_argument("--team-size", type=int, default=1,
                        help="Team size 1/2/5 (playbook mode)")

    args = parser.parse_args()

    # Apply LLM backend override from CLI flag
    if hasattr(args, "llm_backend") and args.llm_backend:
        import config as cfg
        cfg.LLM_BACKEND = args.llm_backend
        if args.llm_backend == "ollama":
            logging.info("LLM backend: Ollama (%s)", cfg.OLLAMA_MODEL)
        elif args.llm_backend == "openrouter":
            logging.info("LLM backend: OpenRouter (%s)", cfg.OPENROUTER_MODEL)

    setup_logging(args.verbose)

    # ── Preflight health checks ──────────────────────────────────────
    preflight_failures = _preflight_check(args.mode)
    if preflight_failures:
        for f in preflight_failures:
            logging.error("Preflight FAILED: %s", f)
        # Send Slack alert so unattended runs are visible
        try:
            from slack_notifier import notify_preflight_failure
            notify_preflight_failure(preflight_failures)
        except Exception:
            pass  # Don't fail on notification failure
        sys.exit(1)
    logging.info("Preflight checks passed")

    # ── New analysis & workflow modes ─────────────────────────────────

    if args.mode == "comp":
        if not args.address:
            print("ERROR: --address is required for comp mode")
            return
        from comp_analyzer import run_comp_analysis
        result = run_comp_analysis(
            address=args.address, city=args.city or "", zip_code=args.zip_code or "",
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Comp analysis failed: %s", result["error"])
        else:
            print(f"Comp report: {result['report_path']}")
            arv = result["arv"]
            print(f"ARV: ${arv.arv_low:,.0f} (low) / ${arv.arv_mid:,.0f} (mid) / ${arv.arv_high:,.0f} (high)")
            print(f"Confidence: {arv.confidence} — {arv.confidence_reason}")
        return

    if args.mode == "rehab":
        if not args.address:
            print("ERROR: --address is required for rehab mode")
            return
        from rehab_estimator import run_rehab_estimate
        result = run_rehab_estimate(
            address=args.address, sqft=args.sqft, bedrooms=args.bedrooms or 3,
            bathrooms=args.bathrooms or 2.0, tier=args.tier, scope=args.scope,
            region=args.region,
        )
        full = result["full_estimate"]
        wt = result["wholetail_estimate"]
        print(f"Rehab report: {result['report_path']}")
        print(f"Full rehab: ${full.grand_total:,.0f} ({full.total_weeks:.0f} weeks)")
        print(f"Wholetail:  ${wt.grand_total:,.0f} ({wt.total_weeks:.0f} weeks)")
        return

    if args.mode == "analyze-deal":
        if not args.address:
            print("ERROR: --address is required for analyze-deal mode")
            return
        from deal_analyzer import run_deal_analysis
        result = run_deal_analysis(
            address=args.address, city=args.city or "", zip_code=args.zip_code or "",
            purchase_price=args.purchase_price, rehab_tier=args.rehab_tier,
            exit_strategy=args.exit_strategy, region=args.region,
            radius=args.radius, months=args.months,
        )
        if "error" in result:
            logger.error("Deal analysis failed: %s", result["error"])
        else:
            pkg = result["package"]
            print(f"Deal report: {result['report_path']}")
            print(f"Recommendation: {pkg.recommendation}")
            print(f"ARV: ${pkg.arv.arv_mid:,.0f} | Rehab: ${pkg.rehab_full.grand_total:,.0f}")
            print(f"Flip MAO: ${pkg.mao.flip_mao:,.0f} | Profit: ${pkg.flip.net_profit:,.0f} ({pkg.flip.roi_pct:.0f}% ROI)")
        return

    if args.mode == "market-analysis":
        from market_analyzer import run_market_analysis
        counties = args.counties.split(",") if args.counties else None
        zip_codes = args.zip_codes.split(",") if args.zip_codes else None
        result = run_market_analysis(
            counties=counties, zip_codes=zip_codes,
            monthly_budget=args.monthly_budget,
        )
        if "error" in result:
            logger.error("Market analysis failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Market report: {result['report_path']}")
            print(f"Analyzed {report.total_zips} zips, {report.total_notices} total notices")
            if report.top_zips:
                top = report.top_zips[0]
                print(f"Top zip: {top.zip_code} (score {top.score:.1f}, grade {top.grade})")
        return

    if args.mode == "buyer-prospect":
        from buyer_prospector import run_buyer_prospecting
        counties = args.counties.split(",") if args.counties else None
        result = run_buyer_prospecting(
            counties=counties,
            months_back=args.months_back,
            min_transactions=args.min_transactions,
        )
        if "error" in result:
            logger.error("Buyer prospecting failed: %s", result["error"])
        else:
            report = result["report"]
            print(f"Buyer report: {result['report_path']}")
            print(f"Found {report.total_investors} investors")
            print(f"CSV: {result.get('csv_path', 'N/A')}")
        return

    if args.mode == "deep-prospect":
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        if not csv_path:
            csvs = sorted(config.OUTPUT_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime, reverse=True)
            csv_path = str(csvs[0]) if csvs else ""
        if not csv_path:
            print("ERROR: --csv-path required or place CSVs in output/")
            return
        import asyncio
        from deep_prospector import run_deep_prospecting
        result = asyncio.run(run_deep_prospecting(
            csv_path=csv_path, depth=args.depth,
            max_records=args.max_notices if hasattr(args, "max_notices") else 0,
        ))
        if "error" in result:
            logger.error("Deep prospecting failed: %s", result["error"])
        else:
            stats = result["stats"]
            print(f"Report: {result['report_path']}")
            print(f"Processed {stats['total']} records at depth {args.depth}")
            print(f"Phones: {stats['phones_found']} | Deceased: {stats['deceased_confirmed']} | DMs: {stats['dms_identified']}")
        return

    if args.mode == "lead-manage":
        from lead_manager import run_lead_management
        csv_path = args.csv_path if hasattr(args, "csv_path") and args.csv_path else ""
        result = run_lead_management(
            action=args.lead_action, csv_path=csv_path,
        )
        if "error" in result:
            logger.error("Lead management failed: %s", result["error"])
        else:
            print(f"STABM report: {result['report_path']}")
            print(f"Total: {result['total']} | Hot: {result['hot']} | Warm: {result['warm']} | Cold: {result['cold']}")
        return

    if args.mode == "setup-sequences":
        from sequence_templates import get_templates, list_templates, preview_sequence
        templates = get_templates(args.seq_folder)
        if args.dry_run:
            print(f"DRY RUN — Would create {len(templates)} sequences in DataSift:")
            for t in templates:
                preview = preview_sequence(t)
                print(f"  [{preview['folder']}] {preview['name']}")
                print(f"    Trigger: {preview['trigger']}")
                print(f"    Actions: {len(preview['actions'])}")
        else:
            print(f"Sequence creation requires Playwright — {len(templates)} templates ready")
            print("Templates defined. DataSift Playwright creation coming in next build.")
            print("\nTemplate list:")
            print(list_templates())
        return

    if args.mode == "niche-sequential":
        from niche_sequential import run_niche_sequential
        result = run_niche_sequential(
            list_name=args.list_name or "",
            channel=args.channel, day=args.day,
            csv_path=args.csv_path if hasattr(args, "csv_path") and args.csv_path else "",
            action=args.ns_action,
        )
        if "error" in result:
            logger.error("Niche sequential failed: %s", result["error"])
        elif "output" in result:
            print(f"Exported: {result['output']}")
            print(f"Channel: {result['channel']}, Day {result['day']}, {result['records']} records")
        elif "presets" in result:
            for p in result["presets"]:
                print(f"  {p['name']}: {p['description']}")
        return

    if args.mode == "playbook":
        from playbook_generator import run_playbook_generator
        result = run_playbook_generator(
            blueprint=args.blueprint, market=args.market,
            team_size=args.team_size,
        )
        print(f"Playbook: {result['playbook_path']}")
        print(f"Blueprint: {result['blueprint'].title()} | Market: {result['market'].title()} | Team: {result['team_size']}")
        return

    # Phone validation mode — separate pipeline
    if args.mode == "phone-validate":
        _run_phone_validate(args)
        return

    # Manage presets mode — filter preset + sequence management
    if args.mode == "manage-presets":
        _run_manage_presets(args)
        return

    # Manage sold properties mode — SiftMap workflow
    if args.mode == "manage-sold":
        _run_manage_sold(args)
        return

    # Richmond Vacant Building List — side feed for code_violation notices
    if args.mode == "richmond-vacant":
        _run_richmond_vacant(args)
        return

    # Chesterfield ACA bulk Code Violation report
    if args.mode == "chesterfield-code-violation":
        _run_chesterfield_aca(args)
        return

    # PDF import mode — separate pipeline
    if args.mode == "pdf-import":
        _run_pdf_import(args)
        return

    # Photo import mode — separate pipeline
    if args.mode == "photo-import":
        _run_photo_import(args)
        return

    # Dropbox watcher mode — polls for new photos
    if args.mode == "dropbox-watch":
        from dropbox_watcher import run_watcher
        run_watcher(
            poll_interval=args.poll_interval,
            delete_after=not getattr(args, "no_delete", False),
            max_polls=args.max_polls,
        )
        return

    # CSV re-import mode — separate pipeline
    if args.mode == "csv-import":
        _run_csv_import(args)
        return

    try:
        _run_scrape_pipeline(args)
    except Exception as e:
        logging.exception("Pipeline failed with unhandled error")
        try:
            from slack_notifier import notify_error
            notify_error("Pipeline (top-level)", e, context=f"mode={args.mode}")
        except Exception:
            pass
        sys.exit(1)


def _run_scrape_pipeline(args) -> None:
    """Run the daily/historical PropertyRadar pull → enrich → export → upload pipeline.

    PropertyRadar is the sole acquisition source. PR has no Added-Date
    filter; delta is a membership-set diff against pr_state.json (see plan
    02-04 SUMMARY), so `mode` (daily vs historical) doesn't affect what
    gets pulled — both produce "new since last successful run".
    """
    from propertyradar_puller import pull_all_lists
    logger.info("Running PropertyRadar puller (mode=%s)", args.mode)
    notices = asyncio.run(pull_all_lists(
        download_dir=config.OUTPUT_DIR,
    ))

    # Run unified enrichment pipeline
    from enrichment_pipeline import PipelineOptions, run_enrichment_pipeline

    opts = PipelineOptions(
        skip_parcel_lookup=True,  # web scrape notices don't have parcel IDs
        skip_vacant_filter=getattr(args, "include_vacant", False),
        skip_commercial_filter=getattr(args, "include_commercial", False),
        skip_entity_filter=getattr(args, "include_entities", False),
        skip_smarty=getattr(args, "skip_smarty", False),
        skip_zillow=getattr(args, "skip_zillow", False),
        skip_tax=getattr(args, "skip_tax", False),
        skip_geocode=getattr(args, "skip_geocode", False),
        skip_obituary=args.skip_obituary,
        skip_ancestry=getattr(args, "skip_ancestry", False),
        skip_entity_research=not getattr(args, "research_entities", False),
        skip_heir_verification=args.skip_heir_verification,
        max_heir_depth=args.max_heir_depth,
        skip_dm_address=args.skip_dm_address,
        tracerfy_tier1=getattr(args, "tracerfy_tier1", False),
        source_label=f"CLI {args.mode}",
    )
    notices = run_enrichment_pipeline(notices, opts)

    if not notices:
        logging.warning("No notices found")
        # Send Slack ping even on empty runs so operators know the job
        # ran successfully (vs silently dying). Previously sys.exit(0)
        # fired before the Slack block at the bottom of this function.
        if getattr(args, "notify_slack", False):
            try:
                from slack_notifier import send_slack_notification
                send_slack_notification([])
            except Exception:
                logging.exception("Slack notification for empty run failed")
        sys.exit(0)

    # Tracerfy batch skip trace (phones + emails for all records)
    tiers_map: dict = {}
    tracerfy_stats: dict = {}
    if not getattr(args, "skip_tracerfy", False):
        import config as cfg
        if cfg.TRACERFY_API_KEY:
            from tracerfy_skip_tracer import batch_skip_trace
            tracerfy_stats = batch_skip_trace(notices)
            if tracerfy_stats.get("credits_exhausted"):
                logging.error(
                    "TRACERFY OUT OF CREDITS — skip trace disabled for this run. "
                    "Add credits at https://tracerfy.com/billing to resume phone/email lookups."
                )
            logging.info(
                "Tracerfy: %d/%d matched, %d phones, %d emails, $%.2f",
                tracerfy_stats.get("matched", 0), tracerfy_stats.get("submitted", 0),
                tracerfy_stats.get("phones_found", 0), tracerfy_stats.get("emails_found", 0),
                tracerfy_stats.get("cost", 0.0),
            )
            # Score every phone (DM #1 + all heirs) — writes per-heir phone_scores
            # into heir_map_json so DataSift Notes and PDFs can surface tier badges.
            if cfg.TRESTLE_API_KEY:
                from phone_validator import score_record_phones
                dp_cands = [
                    n for n in notices
                    if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
                ]
                if dp_cands:
                    try:
                        tiers_map = score_record_phones(dp_cands, cfg.TRESTLE_API_KEY)
                        logging.info("Trestle scored %d unique phones across %d DP records",
                                     len(tiers_map), len(dp_cands))
                    except Exception as e:
                        logging.warning("Per-record Trestle scoring failed: %s", e)

    # Write output
    if args.split:
        paths = write_csv_by_type(notices)
        for p in paths:
            logging.info("Output: %s", p)
    else:
        path = write_csv(notices)
        logging.info("Output: %s", path)

    # Generate deep-prospecting PDFs for deceased/DM/heir records.
    # Matches the Apify branch behavior so CLI runs get the same reports —
    # includes the Case Summary section added for deceased-owner records.
    dp_candidates = [
        n for n in notices
        if n.owner_deceased == "yes" or n.heir_map_json or n.decision_maker_name
    ]
    if dp_candidates:
        try:
            from report_generator import generate_record_pdf
            report_dir = Path("output/reports")
            generated = 0
            for n in dp_candidates:
                try:
                    pdf_path = generate_record_pdf(
                        n, output_dir=report_dir, phone_tiers=tiers_map,
                    )
                    logging.info("Report generated: %s", pdf_path)
                    generated += 1
                except Exception:
                    logging.exception("PDF generation failed for %s", n.address)
            logging.info(
                "Generated %d/%d deep-prospecting PDFs in %s",
                generated, len(dp_candidates), report_dir,
            )
        except Exception:
            logging.exception("Report generator import failed")

    # DataSift upload
    upload_result = None
    if getattr(args, "upload_datasift", False):
        from datasift_formatter import write_datasift_csv, write_datasift_split_csvs
        from datasift_uploader import upload_to_datasift, upload_datasift_split

        do_enrich = not getattr(args, "no_enrich", False)
        do_skip_trace = not getattr(args, "no_skip_trace", False)

        # Use split flow (separate DM + Heir Map Message Board entries)
        csv_infos = write_datasift_split_csvs(notices)
        for info in csv_infos:
            logging.info("DataSift CSV (%s): %s", info["label"], info["path"])

        if len(csv_infos) > 1:
            upload_result = asyncio.run(
                upload_datasift_split(
                    csv_infos,
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )
        else:
            # No deceased-with-heirs — single CSV upload
            upload_result = asyncio.run(
                upload_to_datasift(
                    csv_infos[0]["path"],
                    enrich=do_enrich,
                    skip_trace=do_skip_trace,
                )
            )

        if upload_result.get("success"):
            logging.info("DataSift upload: %s", upload_result.get("message", "OK"))
            if upload_result.get("enrich_result"):
                logging.info("  Enrich: %s", upload_result["enrich_result"].get("message", ""))
            if upload_result.get("skip_trace_result"):
                logging.info("  Skip trace: %s", upload_result["skip_trace_result"].get("message", ""))
        else:
            logging.error("DataSift upload failed: %s", upload_result.get("message"))

    # Slack/Discord notification
    if getattr(args, "notify_slack", False):
        from slack_notifier import send_slack_notification

        send_slack_notification(notices, upload_result=upload_result)

        # PR-09: append the monthly PropertyRadar quota line so the
        # operator sees consumed/budget at a glance after every run.
        try:
            from propertyradar_quota import format_quota_summary
            from slack_notifier import _send_webhook
            _send_webhook(format_quota_summary())
        except Exception:
            logging.debug("Could not append PR quota summary", exc_info=True)

    # Audit DataSift for incomplete records (future daily check)
    if getattr(args, "audit_records", False):
        logging.info("--audit-records: Not yet implemented. "
                      "Will check DataSift Incomplete tab via Playwright in a future build.")

    logging.info("Done — %d notices exported", len(notices))


# ── Entry point ───────────────────────────────────────────────────────


if __name__ == "__main__":
    if os.environ.get("APIFY_IS_AT_HOME") or os.environ.get("APIFY_TOKEN"):
        # Running inside Apify platform or with apify run
        asyncio.run(actor_main())
    else:
        # Standalone CLI
        cli_main()
