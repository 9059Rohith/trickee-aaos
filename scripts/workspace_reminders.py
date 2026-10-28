"""Generate durable in-app reminders without an open browser.

Run from the repository root: python -m scripts.workspace_reminders DATABASE
Add --interval 30 --runs 120 for a bounded standalone worker.
"""

import argparse
import time
from pathlib import Path
from trickee.store import CloudStore
from trickee.personal_workspace import WorkspaceStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("--interval", type=float, default=30)
    parser.add_argument("--runs", type=int, default=1)
    args = parser.parse_args()
    if not Path(args.database).is_file():
        parser.error("database must already exist")
    if not 1 <= args.interval <= 3600 or not 1 <= args.runs <= 100000:
        parser.error("interval must be 1..3600 seconds and runs 1..100000")
    cloud = CloudStore(args.database)
    try:
        store = WorkspaceStore(cloud)
        for index in range(args.runs):
            print(
                f"Generated {store.generate_reminders()} in-app reminder(s)", flush=True
            )
            if index + 1 < args.runs:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        pass
    finally:
        cloud.close()


if __name__ == "__main__":
    main()
