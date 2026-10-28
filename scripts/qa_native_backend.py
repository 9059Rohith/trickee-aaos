"""Serve an isolated backend for local Android emulator interaction testing."""

from __future__ import annotations

import argparse
import hashlib
import secrets
import tempfile
from pathlib import Path

import uvicorn

from trickee.api import create_app
from trickee.simulator import demo_profile


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8769)
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    with tempfile.TemporaryDirectory(prefix="trickee-native-qa-") as folder:
        app = create_app(str(Path(folder) / "cloud.db"),
                         "native-qa-operator-key-123", "native-qa-vehicle-key-123")
        store = app.state.store
        store.register(demo_profile(), "DRIVER-NATIVE-QA")
        driver_key = secrets.token_urlsafe(32)
        store.issue_driver_credential("TRICKEE-DEMO-1",
                                      hashlib.sha256(driver_key.encode()).hexdigest())
        print(f"origin=http://127.0.0.1:{args.port}", flush=True)
        print(f"driver_credential={driver_key}", flush=True)
        try:
            uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
        finally:
            store.close()


if __name__ == "__main__":
    main()
