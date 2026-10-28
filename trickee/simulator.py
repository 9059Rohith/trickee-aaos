"""Reproducible synthetic vehicle scenarios and a five-minute demo runner."""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .agent import VehicleAgent
from .domain import Route, SignalFrame, VehicleProfile, VehicleState
from .ota import SimulatorUpdater, sign_campaign


SCENARIOS: dict[str, dict] = {
    "SCENARIO_001": {"speed_kmh": 50, "ignition": True},
    "SCENARIO_002": {"soc_pct": 12, "speed_kmh": 40, "ignition": True},
    "SCENARIO_003": {"speed_kmh": 35, "ignition": True, "hvac_power_kw": 2.2},
    "SCENARIO_004": {"speed_kmh": 10, "ignition": True, "hvac_power_kw": 2.5},
    "SCENARIO_005": {"speed_kmh": 0, "ignition": False},
    "SCENARIO_006": {"speed_kmh": 48, "ignition": True},
    "SCENARIO_007": {"battery_temperature_c": 60, "speed_kmh": 0, "ignition": True},
    "SCENARIO_008": {"charging": True, "charging_power_kw": 85, "speed_kmh": 0},
    "SCENARIO_009": {"speed_kmh": 35, "ignition": True},
    "SCENARIO_010": {"speed_kmh": 75, "ignition": True},
    "SCENARIO_011": {"speed_kmh": 70, "ignition": True, "hvac_power_kw": 5},
    "SCENARIO_012": {"fault_code": "SIM_BMS_WARN", "speed_kmh": 0},
    "SCENARIO_013": {"speed_kmh": 40, "ignition": True},
    "SCENARIO_014": {"speed_kmh": 0},
    "SCENARIO_015": {"speed_kmh": 0},
}


def demo_profile(vehicle_id: str = "TRICKEE-DEMO-1") -> VehicleProfile:
    return VehicleProfile(vehicle_id=vehicle_id, usable_capacity_kwh=65, mass_kg=1900, drag_area_m2=0.65, rolling_coefficient=0.011, max_charge_kw=130, connectors=["CCS2"])


def scenario_frame(code: str, sequence: int, observed_at: datetime | None = None, vehicle_id: str = "TRICKEE-DEMO-1") -> SignalFrame:
    if code not in SCENARIOS:
        raise KeyError(code)
    values = dict(
        vehicle_id=vehicle_id, sequence=sequence, observed_at=observed_at or datetime.now(timezone.utc),
        source="SIMULATOR", soc_pct=64, soh_pct=94, speed_kmh=0,
        odometer_km=12000 + sequence, latitude=12.9716, longitude=77.5946,
        heading_deg=85, battery_temperature_c=30, battery_voltage_v=380,
        battery_current_a=50, charging=False, ignition=False,
        outside_temperature_c=29, hvac_power_kw=1.2, trip_id="DEMO-TRIP-1",
    )
    values.update(SCENARIOS[code])
    return SignalFrame(**values)


def run_demo(base_url: str, operator_key: str, workdir: str) -> dict:
    profile = demo_profile()
    root = Path(workdir)
    root.mkdir(parents=True, exist_ok=True)
    agent = VehicleAgent(profile, str(root / "vehicle-outbox.db"))
    client = httpx.Client(base_url=base_url, timeout=10)
    response = client.post("/api/v1/vehicles", headers={"X-Operator-Key": operator_key}, json={"profile": profile.model_dump(), "driver_id": "DEMO-DRIVER"})
    response.raise_for_status()
    ingest_key = response.json()["ingest_key"]
    route = Route(distance_km=91, baseline_eta_min=95, average_speed_kmh=58, traffic_factor=1.1)
    next_sequence = agent.frame.sequence + 1 if agent.frame else 1
    start = scenario_frame("SCENARIO_001", next_sequence)
    agent.capture(start)
    initial = agent.local_status(route)
    chargers = agent.recommend_chargers()
    grounded = agent.copilot("Can I reach the charger?", route)
    client_headers = {"X-Vehicle-Key": ingest_key}

    def send(batch: list[SignalFrame]) -> dict:
        result = client.post("/api/v1/telemetry/batches", headers=client_headers, json={"frames": [f.model_dump(mode="json") for f in batch]})
        result.raise_for_status()
        return result.json()

    first_sync = agent.sync(send)
    # Outage: local state and decisions keep running; no cloud call occurs.
    agent.capture(scenario_frame("SCENARIO_011", next_sequence + 1))
    agent.capture(scenario_frame("SCENARIO_007", next_sequence + 2))
    offline = agent.local_status(route)
    recovered = agent.sync(send)
    fleet = client.get("/api/v1/fleet/summary", headers={"X-Operator-Key": operator_key}).json()
    twin = client.get(f"/api/v1/vehicles/{profile.vehicle_id}/twin", headers={"X-Operator-Key": operator_key}).json()
    # A separate parked simulator demonstrates signed update and rollback.
    private_key = Ed25519PrivateKey.generate()
    updater = SimulatorUpdater(private_key.public_key())
    campaign = sign_campaign(private_key, "sim-2", b"synthetic-update-artifact")
    campaign_id_1 = f"DEMO-OTA-{next_sequence}-1"
    campaign_id_2 = f"DEMO-OTA-{next_sequence}-2"
    client.post("/api/v1/ota/campaigns", headers={"X-Operator-Key": operator_key}, json={"campaign_id": campaign_id_1, "vehicle_id": profile.vehicle_id, "manifest": campaign}).raise_for_status()
    pending = client.get(f"/api/v1/vehicles/{profile.vehicle_id}/ota", headers=client_headers).json()
    ota_success = updater.apply(next(item["manifest"] for item in pending if item["campaign_id"] == campaign_id_1), VehicleState.PARKED)
    client.post(f"/api/v1/vehicles/{profile.vehicle_id}/ota/{campaign_id_1}/result", headers=client_headers, json={"result": ota_success, "version_after": updater.version}).raise_for_status()
    campaign2 = sign_campaign(private_key, "sim-3", b"synthetic-update-artifact-v3")
    client.post("/api/v1/ota/campaigns", headers={"X-Operator-Key": operator_key}, json={"campaign_id": campaign_id_2, "vehicle_id": profile.vehicle_id, "manifest": campaign2}).raise_for_status()
    pending = client.get(f"/api/v1/vehicles/{profile.vehicle_id}/ota", headers=client_headers).json()
    ota_rollback = updater.apply(next(item["manifest"] for item in pending if item["campaign_id"] == campaign_id_2), VehicleState.PARKED, fail_after_install=True)
    client.post(f"/api/v1/vehicles/{profile.vehicle_id}/ota/{campaign_id_2}/result", headers=client_headers, json={"result": ota_rollback, "version_after": updater.version}).raise_for_status()
    twin = client.get(f"/api/v1/vehicles/{profile.vehicle_id}/twin", headers={"X-Operator-Key": operator_key}).json()
    client.close()
    return {
        "mode": "SIMULATED", "vehicle_id": profile.vehicle_id,
        "initial": initial, "chargers": chargers, "copilot": grounded,
        "first_sync": first_sync, "offline": offline, "recovered": recovered,
        "fleet": fleet, "twin": twin, "ota": {"success": ota_success, "failure": ota_rollback, "version_after_rollback": updater.version},
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--operator-key", default="local-demo-operator-key")
    parser.add_argument("--workdir", default="data")
    args = parser.parse_args()
    print(json.dumps(run_demo(args.url, args.operator_key, args.workdir), indent=2))


if __name__ == "__main__":
    main()
