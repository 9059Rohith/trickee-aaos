"""Simulator rollout rings advance only after reported success."""
import hashlib
import hmac

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.ota import sign_campaign
from trickee.simulator import demo_profile


OP = {"X-Operator-Key": "operator-key-12345"}
MASTER = "vehicle-key-12345"


def vehicle_auth(vehicle_id):
    return {"X-Vehicle-Key": hmac.new(MASTER.encode(), vehicle_id.encode(),
                                       hashlib.sha256).hexdigest()}


def test_canary_then_pilot_then_completion(tmp_path):
    app = create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], MASTER)
    client = TestClient(app)
    for vehicle_id in ("RING-A", "RING-B"):
        app.state.store.register(demo_profile(vehicle_id))
    manifest = sign_campaign(Ed25519PrivateKey.generate(), "sim-2", b"artifact")
    base = "/api/v1/ota/rollouts"
    created = client.post(base, headers=OP, json={
        "rollout_id": "rollout-1", "manifest": manifest,
        "rings": {"CANARY": ["RING-A"], "PILOT": ["RING-B"]},
    })
    assert created.status_code == 200, created.text
    assert created.json()["active_ring"] == "CANARY"
    assert [target["state"] for target in created.json()["targets"]] == [
        "PENDING", "WAITING"]
    assert [item["vehicle_id"] for item in client.get(
        "/api/v1/fleet/notifications", headers=OP).json()] == ["RING-A"]
    assert client.get("/api/v1/vehicles/RING-B/ota", headers=vehicle_auth("RING-B")).json() == []
    assert client.post(base + "/rollout-1/advance", headers=OP).status_code == 409
    first_campaign = created.json()["targets"][0]["campaign_id"]
    assert client.post(f"/api/v1/vehicles/RING-A/ota/{first_campaign}/result",
                       headers=vehicle_auth("RING-A"), json={
                           "result": "INSTALLED", "version_after": "sim-2"}).status_code == 200
    advanced = client.post(base + "/rollout-1/advance", headers=OP)
    assert advanced.status_code == 200
    assert advanced.json()["active_ring"] == "PILOT"
    assert [item["vehicle_id"] for item in client.get(
        "/api/v1/fleet/notifications", headers=OP).json()] == ["RING-B", "RING-A"]
    second_campaign = advanced.json()["targets"][1]["campaign_id"]
    assert len(client.get("/api/v1/vehicles/RING-B/ota",
                          headers=vehicle_auth("RING-B")).json()) == 1
    assert client.post(f"/api/v1/vehicles/RING-B/ota/{second_campaign}/result",
                       headers=vehicle_auth("RING-B"), json={
                           "result": "INSTALLED", "version_after": "sim-2"}).status_code == 200
    complete = client.post(base + "/rollout-1/advance", headers=OP)
    assert complete.status_code == 200
    assert complete.json()["status"] == "COMPLETED"
    assert client.post(base + "/rollout-1/advance", headers=OP).status_code == 409


def test_failure_halts_rollout_and_keeps_later_ring_untouched(tmp_path):
    app = create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], MASTER)
    client = TestClient(app)
    for vehicle_id in ("FAIL-A", "FAIL-B"):
        app.state.store.register(demo_profile(vehicle_id))
    manifest = sign_campaign(Ed25519PrivateKey.generate(), "sim-2", b"artifact")
    base = "/api/v1/ota/rollouts"
    created = client.post(base, headers=OP, json={
        "rollout_id": "failed-rollout", "manifest": manifest,
        "rings": {"CANARY": ["FAIL-A"], "FLEET": ["FAIL-B"]},
    }).json()
    campaign = created["targets"][0]["campaign_id"]
    assert client.post(f"/api/v1/vehicles/FAIL-A/ota/{campaign}/result",
                       headers=vehicle_auth("FAIL-A"), json={
                           "result": "ROLLED_BACK", "version_after": "sim-1"}).status_code == 200
    status = client.get(base + "/failed-rollout", headers=OP).json()
    assert status["status"] == "HALTED"
    assert client.post(base + "/failed-rollout/advance", headers=OP).status_code == 409
    assert client.get("/api/v1/vehicles/FAIL-B/ota", headers=vehicle_auth("FAIL-B")).json() == []


def test_invalid_scope_and_duplicate_targets_leave_no_campaign(tmp_path):
    app = create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"], MASTER)
    client = TestClient(app)
    app.state.store.register(demo_profile("ONE"))
    manifest = sign_campaign(Ed25519PrivateKey.generate(), "sim-2", b"artifact")
    body = {"rollout_id": "bad-rollout", "manifest": manifest,
            "rings": {"CANARY": ["ONE"], "PILOT": ["ONE"]}}
    assert client.post("/api/v1/ota/rollouts", headers=OP, json=body).status_code == 409
    assert client.post("/api/v1/ota/rollouts", headers=OP, json={
        **body, "rings": {"CANARY": ["ONE"], "PILOT": ["UNKNOWN"]},
    }).status_code == 404
    assert client.get("/api/v1/vehicles/ONE/ota", headers=vehicle_auth("ONE")).json() == []
