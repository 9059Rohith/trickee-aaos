"""Run the concurrent fleet read check against a seeded, isolated loopback API."""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trickee.simulator import demo_profile, scenario_frame
from trickee.store import CloudStore
from scripts.verify_application import _stop_tree


@contextmanager
def isolated_directory(prefix: str = "trickee-load-"):
    if not prefix.startswith("trickee-") or any(character in prefix for character in ("/", "\\", ":")):
        raise ValueError("invalid isolated directory prefix")
    directory = Path(tempfile.mkdtemp(prefix=prefix))
    try:
        yield directory
    finally:
        temp_root = Path(tempfile.gettempdir()).resolve()
        target = directory.resolve()
        if not target.is_relative_to(temp_root) or target == temp_root or not target.name.startswith(prefix):
            raise RuntimeError("isolated load cleanup target escaped the temporary root")
        for attempt in range(10):
            try:
                shutil.rmtree(target)
                break
            except OSError:
                if not target.exists():
                    break
                if attempt == 9:
                    raise
                time.sleep(0.25)


def main() -> None:
    with isolated_directory() as directory:
        database = Path(directory) / "cloud.db"
        store = CloudStore(str(database))
        try:
            store.register(demo_profile(), "LOAD-DRIVER")
            store.ingest([scenario_frame("SCENARIO_001", 1)])
        finally:
            store.close()

        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update({"TRICKEE_DB": str(database), "TRICKEE_DEMO_MODE": "1"})
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "trickee.api:app", "--host", "127.0.0.1",
             "--port", str(port)], cwd=ROOT, env=environment,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError("isolated load API exited before readiness")
                try:
                    if httpx.get(origin + "/ready", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError("isolated load API did not become ready")

            os.environ["TRICKEE_LOAD_URL"] = origin
            from scripts import verify_load
            verify_load.BASE = origin
            verify_load.main()
        finally:
            _stop_tree(server)


if __name__ == "__main__":
    main()
