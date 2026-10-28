"""Prove an AAOS emulator outbox drains after a local cloud outage."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import time
import xml.etree.ElementTree as ET

import httpx


ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "aaos-evidence"
ADB = os.getenv("ADB", str(Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe"))
CONTAINER = "trcikee-aaos-cloud-1"
BASE = "http://127.0.0.1:8002"
KEY = os.getenv("TRICKEE_EMULATOR_OPERATOR_KEY", "local-demo-operator-key")


def command(*args: str) -> str:
    return subprocess.run(args, check=True, capture_output=True, text=True, timeout=40).stdout.strip()


def pending() -> int:
    command(ADB, "shell", "uiautomator", "dump", "/sdcard/trickee-recovery.xml")
    root = ET.fromstring(command(ADB, "shell", "cat", "/sdcard/trickee-recovery.xml"))
    for node in root.iter("node"):
        match = re.search(r"LOCAL OUTBOX\s+(\d+)", node.attrib.get("text", ""))
        if match:
            return int(match.group(1))
    raise AssertionError("outbox status is not visible")


def main() -> None:
    vehicle_id = json.loads((OUT / "verification.json").read_text(encoding="utf-8"))["vehicle_id"]
    with httpx.Client(base_url=BASE, headers={"X-Operator-Key": KEY}, timeout=5) as client:
        before = client.get(f"/api/v1/vehicles/{vehicle_id}/twin")
        before.raise_for_status()
        first_sequence = before.json()["last_sequence"]
        command("docker", "stop", CONTAINER)
        offline_pending = 0
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                offline_pending = pending()
                if offline_pending >= 2:
                    break
                time.sleep(3)
            assert offline_pending >= 2, "offline outbox did not retain frames"
        finally:
            command("docker", "start", CONTAINER)
        deadline = time.monotonic() + 50
        while time.monotonic() < deadline:
            try:
                if client.get("/health").status_code == 200 and pending() == 0:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(3)
        else:
            raise AssertionError("outbox did not drain after the cloud restarted")
        after = client.get(f"/api/v1/vehicles/{vehicle_id}/twin")
        after.raise_for_status()
        last_sequence = after.json()["last_sequence"]
        assert last_sequence > first_sequence
    report = {"offline_pending": offline_pending, "recovered_pending": 0,
              "first_sequence": first_sequence, "last_sequence": last_sequence}
    (OUT / "recovery.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
