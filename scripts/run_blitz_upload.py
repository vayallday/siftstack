#!/usr/bin/env python3
"""Push the 90-day blitz CSV to DataSift (upload → enrich → skip-trace).

One-off operator tool (not part of the automated pipeline). Uploads the
hand-built, DataSift-formatted blitz list into a dated `SiftStack` batch
list, then runs DataSift's own Enrich + Skip Trace on that batch.

Run headed so the operator can watch the known-flaky enrich select-all step.

    python3 scripts/run_blitz_upload.py
    python3 scripts/run_blitz_upload.py --headless
    python3 scripts/run_blitz_upload.py --csv output/90day_blitz_datasift.csv
"""
import argparse
import asyncio
import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import config  # noqa: E402  (loads .env)
from datasift_uploader import upload_to_datasift  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description="Upload 90-day blitz CSV to DataSift")
    ap.add_argument("--csv", default="output/90day_blitz_datasift.csv",
                    help="DataSift-formatted CSV to upload")
    ap.add_argument("--headless", action="store_true",
                    help="Run browser headless (default: headed so you can watch)")
    ap.add_argument("--no-enrich", action="store_true", help="Skip the Enrich step")
    ap.add_argument("--no-skip-trace", action="store_true", help="Skip the Skip Trace step")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    csv_path = (ROOT / args.csv) if not Path(args.csv).is_absolute() else Path(args.csv)
    if not csv_path.exists():
        print(f"ERROR: CSV not found: {csv_path}")
        return 1
    if not config.DATASIFT_EMAIL or not config.DATASIFT_PASSWORD:
        print("ERROR: DATASIFT_EMAIL / DATASIFT_PASSWORD not set in .env")
        return 1

    n = sum(1 for _ in open(csv_path, encoding="utf-8")) - 1
    print(f"Uploading {csv_path.name} ({n} records) as {config.DATASIFT_EMAIL} ...")

    result = asyncio.run(upload_to_datasift(
        csv_path,
        email=config.DATASIFT_EMAIL,
        password=config.DATASIFT_PASSWORD,
        headless=args.headless,
        enrich=not args.no_enrich,
        skip_trace=not args.no_skip_trace,
    ))

    print("\n===== RESULT =====")
    for k, v in result.items():
        print(f"{k}: {v}")
    return 0 if result.get("success") else 2


if __name__ == "__main__":
    raise SystemExit(main())
