"""Verify an image can make, export and reopen an online SQLite backup.

This uses an isolated demo container with no published port or production data.
"""

from __future__ import annotations

import argparse
import sqlite3
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


def docker(*args: str) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode:
        raise subprocess.CalledProcessError(result.returncode, ["docker", *args],
                                            result.stdout, result.stderr)
    return result.stdout.strip()


def verify(image: str) -> None:
    name = f"trickee-backup-qa-{uuid.uuid4().hex[:12]}"
    container_started = False
    try:
        docker("run", "-d", "--rm", "--name", name, "-e", "TRICKEE_DEMO_MODE=1", image)
        container_started = True
        for attempt in range(30):
            try:
                docker("exec", name, "python", "-c",
                       "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=2)")
                break
            except subprocess.CalledProcessError:
                if attempt == 29:
                    raise RuntimeError("container readiness did not succeed") from None
                time.sleep(1)

        docker("exec", name, "python", "-c",
               "from trickee.store import CloudStore; from trickee.simulator import demo_profile, scenario_frame; "
               "s=CloudStore('/app/data/cloud.db'); s.register(demo_profile()); "
               "s.ingest([scenario_frame('SCENARIO_001', 1)])")
        docker("exec", name, "python", "scripts/backup.py", "/app/data/cloud.db",
               "/app/data/backup.db")
        with tempfile.TemporaryDirectory(prefix="trickee-backup-qa-") as directory:
            exported = Path(directory) / "exported.db"
            docker("cp", f"{name}:/app/data/backup.db", str(exported))
            with sqlite3.connect(exported) as restored:
                integrity = restored.execute("PRAGMA integrity_check").fetchone()[0]
                vehicle = restored.execute(
                    "SELECT vehicle_id FROM vehicles WHERE vehicle_id='TRICKEE-DEMO-1'"
                ).fetchone()
                sequence = restored.execute(
                    "SELECT MAX(sequence) FROM events WHERE vehicle_id='TRICKEE-DEMO-1'"
                ).fetchone()
                events = restored.execute(
                    "SELECT COUNT(*) FROM events WHERE vehicle_id='TRICKEE-DEMO-1'"
                ).fetchone()[0]
            if integrity != "ok" or vehicle is None or sequence[0] != 1 or events != 1:
                raise RuntimeError(f"exported backup did not restore expected data: {integrity}, {vehicle}, {sequence}, {events}")
        print(f"Container backup smoke passed: image={image} integrity=ok sequence=1 events=1 off_volume=yes")
    finally:
        if container_started:
            subprocess.run(["docker", "rm", "-f", name], capture_output=True, text=True, check=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    try:
        verify(parser.parse_args().image)
    except subprocess.CalledProcessError as error:
        parser.exit(1, f"container backup smoke failed: {error.stderr.strip()}\n")
