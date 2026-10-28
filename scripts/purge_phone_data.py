"""One-shot operator retention sweep for completed-trip phone GPS/IMU."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from trickee.store import CloudStore


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", help="path to the existing cloud SQLite database")
    parser.add_argument("--days", type=int, default=None,
                        help="completed-trip retention days; defaults to TRICKEE_PHONE_GPS_RETENTION_DAYS")
    parser.add_argument("--apply", action="store_true", help="delete eligible data; otherwise dry run")
    args = parser.parse_args()
    configured = args.days if args.days is not None else os.getenv("TRICKEE_PHONE_GPS_RETENTION_DAYS")
    if configured is None:
        parser.error("set --days or TRICKEE_PHONE_GPS_RETENTION_DAYS")
    try:
        days = int(configured)
    except ValueError:
        parser.error("retention days must be an integer")
    if days < 1:
        parser.error("retention days must be positive")
    if not os.path.isfile(args.database):
        parser.error("database file does not exist")
    result = CloudStore(args.database).purge_expired_phone_data(days, dry_run=not args.apply)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
