"""Run the fleet visual QA against an isolated, seeded loopback API."""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trickee.simulator import demo_profile, scenario_frame
from trickee.store import CloudStore
from scripts.verify_application import _stop_tree


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="trickee-fleet-visual-") as directory:
        database = str(Path(directory) / "cloud.db")
        store = CloudStore(database)
        store.register(demo_profile(), "QA-DRIVER")
        store.ingest([scenario_frame("SCENARIO_005", 1)])
        store.close()
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update({"TRICKEE_DEMO_MODE": "1", "TRICKEE_DB": database,
                            "TRICKEE_GEOCODER_URL": "https://places.qa.invalid"})
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "trickee.api:app", "--host", "127.0.0.1",
             "--port", str(port)], cwd=ROOT, env=environment, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            for _ in range(80):
                if server.poll() is not None:
                    raise RuntimeError("isolated fleet UI API exited before health check")
                try:
                    if httpx.get(origin + "/health", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("isolated fleet UI API did not become ready")
            os.environ["TRICKEE_UI_URL"] = origin
            os.environ["TRICKEE_UI_OFFLINE_TILES"] = "1"
            from scripts import verify_ui
            verify_ui.main()
        finally:
            _stop_tree(server)


if __name__ == "__main__":
    main()
