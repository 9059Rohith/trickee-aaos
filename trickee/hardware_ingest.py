"""Capture read-only vehicle data into the durable outbox and optional cloud.

Usage: python -m trickee.hardware_ingest --adapter replay --profile profile.json
       --replay-file frames.jsonl --outbox data/vehicle-outbox.db
The replay and fake adapters are explicitly labelled synthetic downstream.
"""
from __future__ import annotations

import argparse
import json
import os
import time
from contextlib import closing
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

import httpx

from .agent import VehicleAgent
from .domain import SignalFrame, VehicleProfile
from .hardware import (
    CanReadOnlyAdapter, CanSignalSpec, Elm327TcpTransport,
    FakeVehicleHardwareAdapter, ObdReadOnlyAdapter,
    ReplayVehicleHardwareAdapter, SignalGateway, SocketCanReceiver,
    VehicleHardwareAdapter,
)
from .simulator import SCENARIOS, scenario_frame


def load_can_mapping(path: str | Path) -> list[CanSignalSpec]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(document, dict) or not isinstance(document.get("signals"), list):
        raise ValueError("CAN mapping needs a signals array")
    mappings = [CanSignalSpec(**entry) for entry in document["signals"]]
    if not mappings:
        raise ValueError("CAN mapping must not be empty")
    keys = [(item.arbitration_id, item.field) for item in mappings]
    if len(keys) != len(set(keys)):
        raise ValueError("duplicate CAN signal mapping")
    return mappings


def capture_loop(adapter: VehicleHardwareAdapter, agent: VehicleAgent, *,
                 limit: int = 0, sync_every: int = 10,
                 sender: Callable[[list[SignalFrame]], dict] | None = None,
                 poll_seconds: float = 0) -> dict:
    if limit < 0 or sync_every < 1 or poll_seconds < 0:
        raise ValueError("invalid capture settings")
    gateway = SignalGateway(agent.profile.vehicle_id,
                            next_sequence=agent.frame.sequence + 1 if agent.frame else 1)
    captured = synchronized = 0
    last_sync_error: str | None = None

    def sync() -> None:
        nonlocal synchronized, last_sync_error
        if sender is None:
            return
        try:
            synchronized += agent.sync(sender)["synchronized"]
            last_sync_error = None
        except (httpx.HTTPError, ValueError, OSError) as exc:
            # The outbox is durable; a later pass can retry this same batch.
            last_sync_error = type(exc).__name__

    while not limit or captured < limit:
        try:
            sample = adapter.read()
        except StopIteration:
            break
        frame = gateway.accept(sample)
        agent.capture(frame)
        captured += 1
        if captured % sync_every == 0:
            sync()
        if poll_seconds:
            time.sleep(poll_seconds)
    sync()
    return {"captured": captured, "synchronized": synchronized,
            "pending": agent.outbox.count_pending(), "sync_error": last_sync_error}


def _cloud_sender(url: str, key: str, client: httpx.Client):
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or not parsed.netloc or parsed.username or
            parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
        raise ValueError("cloud URL must be an HTTPS origin without credentials")
    if not key:
        raise ValueError("TRICKEE_VEHICLE_INGEST_KEY is required for cloud sync")

    def send(frames: list[SignalFrame]) -> dict:
        response = client.post(
            url.rstrip("/") + "/api/v1/telemetry/batches",
            headers={"X-Vehicle-Key": key},
            json={"frames": [frame.model_dump(mode="json", exclude_unset=True)
                             for frame in frames]},
        )
        response.raise_for_status()
        return response.json()

    return send


def _adapter(args, vehicle_id: str) -> VehicleHardwareAdapter:
    if args.adapter == "replay":
        if not args.replay_file:
            raise ValueError("--replay-file is required")
        return ReplayVehicleHardwareAdapter(args.replay_file)
    if args.adapter == "fake":
        if args.fake_scenario not in SCENARIOS:
            raise ValueError("unknown fake scenario")

        def samples():
            while True:
                frame = scenario_frame(args.fake_scenario, 1, vehicle_id=vehicle_id)
                yield frame.model_dump(exclude={"schema_version", "vehicle_id", "sequence",
                                                "observed_at", "source"}, exclude_none=True)

        return FakeVehicleHardwareAdapter(samples())
    if args.adapter == "can":
        if not args.can_interface or not args.can_mapping:
            raise ValueError("--can-interface and --can-mapping are required")
        mappings = load_can_mapping(args.can_mapping)
        return CanReadOnlyAdapter(SocketCanReceiver(args.can_interface), mappings)
    if args.adapter == "obd":
        if not args.obd_host:
            raise ValueError("--obd-host is required")
        return ObdReadOnlyAdapter(Elm327TcpTransport(args.obd_host, args.obd_port))
    raise ValueError("unknown hardware adapter")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Trickee vehicle capture")
    parser.add_argument("--adapter", choices=("fake", "replay", "can", "obd"), required=True)
    parser.add_argument("--profile", required=True, help="VehicleProfile JSON file")
    parser.add_argument("--outbox", required=True, help="Persistent SQLite outbox")
    parser.add_argument("--replay-file")
    parser.add_argument("--fake-scenario", default="SCENARIO_001")
    parser.add_argument("--can-interface")
    parser.add_argument("--can-mapping", help="Approved OEM mapping JSON")
    parser.add_argument("--obd-host")
    parser.add_argument("--obd-port", type=int, default=35000)
    parser.add_argument("--cloud-url", help="Optional HTTPS Trickee backend")
    parser.add_argument("--limit", type=int, default=0, help="Zero means continuous capture")
    parser.add_argument("--sync-every", type=int, default=10)
    args = parser.parse_args()
    profile = VehicleProfile.model_validate_json(Path(args.profile).read_text(encoding="utf-8"))
    agent = VehicleAgent(profile, args.outbox)
    with closing(_adapter(args, profile.vehicle_id)) as adapter, httpx.Client(timeout=10) as client:
        sender = (_cloud_sender(args.cloud_url,
                                os.getenv("TRICKEE_VEHICLE_INGEST_KEY", ""), client)
                  if args.cloud_url else None)
        # ELM polling is bounded; CAN and replay are driven by incoming frames.
        result = capture_loop(adapter, agent, limit=args.limit,
                              sync_every=args.sync_every, sender=sender,
                              poll_seconds=1 if args.adapter in ("obd", "fake") else 0)
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
