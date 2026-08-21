"""One-off: backfill OPP code-case fields into an existing richmond_vacant CSV.

Use after the 2026-05-25 OPP-fields-missing-from-CSV bug fix. Reads the most
recent (or specified) richmond_vacant_*.csv, runs just the OPP enricher,
writes a new CSV with the OPP columns populated. Skips the full enrichment
pipeline (no Smarty/Zillow/obituary re-runs).

Usage:
    python test_richmond_vacant_opp_backfill.py
    python test_richmond_vacant_opp_backfill.py --csv output/richmond_vacant_X.csv
"""

from __future__ import annotations

import argparse
import csv
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "src"))

from notice_parser import NoticeData
from data_formatter import write_csv, SIFT_COLUMNS
from richmond_opp_enricher import enrich_notices

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("opp-backfill")


# CSV column -> NoticeData field (where they differ)
_CSV_TO_FIELD = {
    "full_name": "owner_name",
    "Date Added": "date_added",
    "Owner Street": "owner_street",
    "Owner City": "owner_city",
    "Owner State": "owner_state",
    "Owner ZIP Code": "owner_zip",
}


def _row_to_notice(row: dict) -> NoticeData:
    n = NoticeData()
    valid = {f.name for f in n.__dataclass_fields__.values()}
    for col, val in row.items():
        field = _CSV_TO_FIELD.get(col, col)
        if field in valid:
            setattr(n, field, val or "")
    return n


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--csv", default=None, help="Specific richmond_vacant_*.csv to backfill")
    args = p.parse_args()

    if args.csv:
        target = Path(args.csv)
    else:
        candidates = sorted(Path("output").glob("richmond_vacant_*.csv"))
        if not candidates:
            logger.error("No richmond_vacant_*.csv found under output/")
            sys.exit(1)
        target = candidates[-1]
    logger.info("Backfilling OPP fields into %s", target)

    with open(target, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    notices = [_row_to_notice(r) for r in rows]
    logger.info("Loaded %d records", len(notices))

    enrich_notices(notices)

    backfilled_name = target.stem + "_with_opp.csv"
    out_path = write_csv(notices, filename=backfilled_name)

    active = sum(1 for n in notices if (n.opp_active_violation_count or "0").isdigit() and int(n.opp_active_violation_count) > 0)
    logger.info("Done. Active violation records: %d/%d -> %s", active, len(notices), out_path)


if __name__ == "__main__":
    main()
