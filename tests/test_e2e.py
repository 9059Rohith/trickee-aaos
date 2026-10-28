import hashlib
import hmac
import pytest

from fastapi.testclient import TestClient
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from trickee.agent import VehicleAgent
from trickee.api import app_from_environment, create_app
from trickee.domain import Route
from trickee.simulator import demo_profile, scenario_frame
from trickee.ota import SimulatorUpdater, sign_campaign
from trickee.domain import VehicleState


OP = {"X-Operator-Key": "test-operator-key-123"}
VEHICLE_MASTER = "test-vehicle-key-123"
VEHICLE = {"X-Vehicle-Key": hmac.new(VEHICLE_MASTER.encode(), b"TRICKEE-DEMO-1", hashlib.sha256).hexdigest()}


def test_production_configuration_rejects_demo_and_shared_keys(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="distinct"):
        create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], OP["X-Operator-Key"])
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    monkeypatch.setenv("TRICKEE_OPERATOR_KEY", "local-demo-operator-key")
    monkeypatch.setenv("TRICKEE_VEHICLE_KEY", "local-demo-vehicle-key")
    with pytest.raises(ValueError, match="demo keys"):
        app_from_environment()


def test_non_demo_environment_rejects_short_master_keys(monkeypatch):
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    monkeypatch.setenv("TRICKEE_OPERATOR_KEY", "o" * 32)
    monkeypatch.setenv("TRICKEE_VEHICLE_KEY", "v" * 31)
    with pytest.raises(ValueError, match="vehicle master key"):
        app_from_environment()
    monkeypatch.setenv("TRICKEE_VEHICLE_KEY", "v" * 32)
    monkeypatch.setenv("TRICKEE_OPERATOR_KEY", "o" * 31)
    with pytest.raises(ValueError, match="legacy operator key"):
        app_from_environment()


def test_sensitive_api_response_is_not_cached(tmp_path):
    client = TestClient(create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], VEHICLE_MASTER))
    response = client.get("/api/v1/fleet/summary", headers=OP)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_vehicle_cloud_fleet_copilot_offline_recovery(tmp_path):
    app = create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], VEHICLE_MASTER)
    client = TestClient(app)
    profile = demo_profile()
    assert client.post("/api/v1/vehicles", headers=OP, json={"profile": profile.model_dump(), "driver_id": "DRIVER-1"}).status_code == 200
    agent = VehicleAgent(profile, str(tmp_path / "outbox.db"))
    route = Route(distance_km=91, baseline_eta_min=95, average_speed_kmh=58)
    agent.capture(scenario_frame("SCENARIO_001", 1))
    assert agent.local_status(route)["range"]["arrival_soc_pct"] > 10

    def sender(frames):
        result = client.post("/api/v1/telemetry/batches", headers=VEHICLE, json={"frames": [f.model_dump(mode="json") for f in frames]})
        assert result.status_code == 200, result.text
        return result.json()

    assert agent.sync(sender)["synchronized"] == 1
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/twin", headers=OP).json()["battery"]["soc_pct"] == 64
    agent.capture(scenario_frame("SCENARIO_011", 2))
    agent.capture(scenario_frame("SCENARIO_007", 3))
    assert agent.outbox.count_pending() == 2
    assert agent.local_status(route)["health"]["status"] == "CRITICAL"
    assert agent.sync(sender)["pending"] == 0
    twin = client.get("/api/v1/vehicles/TRICKEE-DEMO-1/twin", headers=OP).json()
    assert twin["last_sequence"] == 3
    assert twin["health"]["status"] == "CRITICAL"
    assert client.get("/api/v1/fleet/summary", headers=OP).json()["alerts"] == 1
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/trips", headers=OP).json()
    plan = client.post("/api/v1/vehicles/TRICKEE-DEMO-1/plan", headers=OP, json={"route": route.model_dump()}).json()
    assert plan["range"]["quality"] == "ESTIMATED"
    assert plan["chargers"] == []
    assert plan["charger_quality"] == "UNAVAILABLE"
    route_lookup = client.post("/api/v1/routes", headers=OP, json={"origin": "Bengaluru", "destination": "Hosur"}).json()
    assert route_lookup["quality"] == "SIMULATED"
    answer = client.post("/api/v1/copilot", headers=OP, json={"vehicle_id": profile.vehicle_id, "question": "Can I reach the charger?", "route": route.model_dump()}).json()
    assert answer["tool"] == "get_range_prediction"
    assert "Estimated arrival charge" in answer["answer"]
    denied = client.post("/api/v1/copilot", headers=OP, json={"vehicle_id": profile.vehicle_id, "question": "steer the car"}).json()
    assert denied["tier"] == "DENIED"
    original = client.get("/api/v1/vehicles/TRICKEE-DEMO-1/events", headers=OP).json()[-1]["frame"]
    retry = client.post("/api/v1/telemetry/batches", headers=VEHICLE, json={"frames": [original]})
    assert retry.json()["duplicate"] == 1
    assert client.get("/api/v1/vehicles", headers=OP).json()[0]["vehicle_id"] == profile.vehicle_id


def test_console_serves_local_brand_assets(tmp_path):
    client = TestClient(create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], VEHICLE_MASTER))
    assert client.get("/").status_code == 200
    assert client.get("/assets/hero-fleet.png").content.startswith(b"\x89PNG")
    assert client.get("/assets/manrope-latin.woff2").status_code == 200


def test_fleet_before_first_telemetry_frame(tmp_path):
    client = TestClient(create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], VEHICLE_MASTER))
    profile = demo_profile()
    assert client.post("/api/v1/vehicles", headers=OP, json={"profile": profile.model_dump()}).status_code == 200
    vehicles = client.get("/api/v1/vehicles", headers=OP)
    summary = client.get("/api/v1/fleet/summary", headers=OP)
    assert vehicles.status_code == summary.status_code == 200
    assert vehicles.json()[0]["quality"] == "MISSING"
    assert vehicles.json()[0]["battery"] is None
    assert summary.json()["vehicles"] == 1
    assert summary.json()["online"] == 0
    assert summary.json()["offline"] == 1


def test_auth_and_sequence_conflict(tmp_path):
    app = create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], VEHICLE_MASTER)
    client = TestClient(app)
    assert client.get("/api/v1/vehicles").status_code == 401
    assert client.post("/api/v1/telemetry/batches", json={"frames": [scenario_frame("SCENARIO_001", 1).model_dump(mode="json")]}).status_code == 401
    client.post("/api/v1/vehicles", headers=OP, json={"profile": demo_profile().model_dump()})
    first = scenario_frame("SCENARIO_001", 1).model_dump(mode="json")
    assert client.post("/api/v1/telemetry/batches", headers=VEHICLE, json={"frames": [first]}).status_code == 200
    conflict = {**first, "soc_pct": 10}
    assert client.post("/api/v1/telemetry/batches", headers=VEHICLE, json={"frames": [conflict]}).status_code == 409


def test_cloud_delivered_signed_simulator_ota(tmp_path):
    app = create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], VEHICLE_MASTER)
    client = TestClient(app)
    client.post("/api/v1/vehicles", headers=OP, json={"profile": demo_profile().model_dump()})
    private = Ed25519PrivateKey.generate()
    campaign = sign_campaign(private, "sim-2", b"test-artifact")
    assert client.post("/api/v1/ota/campaigns", headers=OP, json={"campaign_id": "C1", "vehicle_id": "TRICKEE-DEMO-1", "manifest": campaign}).status_code == 200
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/ota").status_code == 401
    assigned = client.get("/api/v1/vehicles/TRICKEE-DEMO-1/ota", headers=VEHICLE).json()[0]
    updater = SimulatorUpdater(private.public_key())
    result = updater.apply(assigned["manifest"], VehicleState.PARKED)
    assert result == "INSTALLED"
    assert client.post("/api/v1/vehicles/TRICKEE-DEMO-1/ota/C1/result", headers=VEHICLE, json={"result": result, "version_after": updater.version}).status_code == 200
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/twin", headers=OP).json()["software_version"] == "sim-2"
