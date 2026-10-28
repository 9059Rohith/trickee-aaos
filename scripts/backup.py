"""Online SQLite backup for the single-instance cloud deployment."""
from __future__ import annotations

import argparse
from contextlib import closing
import os
import sqlite3
import tempfile
from pathlib import Path


def backup_database(source: str, destination: str) -> None:
    source_path, target_path = Path(source), Path(destination)
    if not source_path.is_file():
        raise FileNotFoundError(source)
    if source_path.resolve() == target_path.resolve():
        raise ValueError("backup destination must differ from the live database")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{target_path.name}.", suffix=".tmp",
                                     dir=target_path.parent, delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with closing(sqlite3.connect(source_path)) as live, closing(sqlite3.connect(temporary_path)) as backup:
            live.backup(backup)
            result = backup.execute("PRAGMA integrity_check").fetchone()[0]
            if result != "ok":
                raise RuntimeError(f"backup integrity check failed: {result}")
        os.replace(temporary_path, target_path)
    finally:
        temporary_path.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source")
    parser.add_argument("destination")
    args = parser.parse_args()
    backup_database(args.source, args.destination)
    print(f"Verified backup: {args.destination}")
