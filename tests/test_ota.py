from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
import hashlib
import hmac

from trickee.api import create_app
from trickee.domain import VehicleState
from trickee.ota import SimulatorUpdater, sign_campaign
from trickee.simulator import demo_profile


def test_signed_simulator_update_failure_and_rollback():
    private = Ed25519PrivateKey.generate()
    updater = SimulatorUpdater(private.public_key())
    campaign = sign_campaign(private, "sim-2", b"test-artifact")
    assert updater.apply(campaign, VehicleState.DRIVING) == "BLOCKED_VEHICLE_STATE"
    tampered = dict(campaign, sha256="00" * 32)
    assert updater.apply(tampered, VehicleState.PARKED) == "REJECTED_SIGNATURE"
    assert updater.apply(dict(campaign, mode="AAOS_IMAGE"), VehicleState.PARKED) == "REJECTED_SIGNATURE"
    assert updater.apply(dict(campaign, artifact_b64="%%%"), VehicleState.PARKED) == "REJECTED_SIGNATURE"
    assert updater.apply(campaign, VehicleState.PARKED) == "INSTALLED"
    assert updater.version == "sim-2"
    failed = sign_campaign(private, "sim-3", b"new-artifact")
    assert updater.apply(failed, VehicleState.PARKED, fail_after_install=True) == "ROLLED_BACK"
    assert updater.version == "sim-2"


def test_campaign_result_is_version_checked_and_immutable(tmp_path):
    operator_key = "operator-key-12345"
    vehicle_master = "vehicle-key-12345"
    vehicle_id = demo_profile().vehicle_id
    key = hmac.new(vehicle_master.encode(), vehicle_id.encode(), hashlib.sha256).hexdigest()
    operator = {"X-Operator-Key": operator_key}
    vehicle = {"X-Vehicle-Key": key}
    client = TestClient(create_app(str(tmp_path / "cloud.db"), operator_key,
                                   vehicle_master))
    client.post("/api/v1/vehicles", headers=operator,
                json={"profile": demo_profile().model_dump()})
    private = Ed25519PrivateKey.generate()
    manifest = sign_campaign(private, "sim-2", b"artifact")
    url = "/api/v1/ota/campaigns"
    body = {"campaign_id": "OTA-1", "vehicle_id": vehicle_id,
            "manifest": manifest}
    assert client.post(url, headers=operator, json=body).status_code == 200
    assert client.post(url, headers=operator, json=body).status_code == 200
    notices = client.get("/api/v1/fleet/notifications", headers=operator).json()
    assert [notice["kind"] for notice in notices] == ["OTA_AVAILABLE"]
    assert client.post(url, headers=operator, json={**body,
        "manifest": sign_campaign(private, "sim-3", b"other")}).status_code == 409
    result_url = f"/api/v1/vehicles/{vehicle_id}/ota/OTA-1/result"
    assert client.post(result_url, headers=vehicle, json={
        "result": "INSTALLED", "version_after": "sim-999"}).status_code == 409
    assert client.post(result_url, headers=vehicle, json={
        "result": "INSTALLED", "version_after": "sim-2"}).status_code == 200
    assert client.post(result_url, headers=vehicle, json={
        "result": "INSTALLED", "version_after": "sim-2"}).status_code == 200
    assert client.post(result_url, headers=vehicle, json={
        "result": "ROLLED_BACK", "version_after": "sim-1"}).status_code == 409
    assert client.get(f"/api/v1/vehicles/{vehicle_id}/twin",
                      headers=operator).json()["software_version"] == "sim-2"
    assert client.post(url, headers=operator, json={**body, "campaign_id": "BAD-1",
        "manifest": {**manifest, "sha256": "0" * 64}}).status_code == 409


def test_rollback_keeps_prior_version_and_rejects_later_change(tmp_path):
    operator_key, master = "operator-key-12345", "vehicle-key-12345"
    vehicle_id = demo_profile().vehicle_id
    client = TestClient(create_app(str(tmp_path / "cloud.db"), operator_key, master))
    client.post("/api/v1/vehicles", headers={"X-Operator-Key": operator_key},
                json={"profile": demo_profile().model_dump()})
    manifest = sign_campaign(Ed25519PrivateKey.generate(), "sim-2", b"artifact")
    client.post("/api/v1/ota/campaigns", headers={"X-Operator-Key": operator_key},
                json={"campaign_id": "OTA-2", "vehicle_id": vehicle_id,
                      "manifest": manifest})
    key = hmac.new(master.encode(), vehicle_id.encode(), hashlib.sha256).hexdigest()
    url = f"/api/v1/vehicles/{vehicle_id}/ota/OTA-2/result"
    assert client.post(url, headers={"X-Vehicle-Key": key}, json={
        "result": "ROLLED_BACK", "version_after": "sim-1"}).status_code == 200
    notices = client.get("/api/v1/fleet/notifications", headers={"X-Operator-Key": operator_key}).json()
    assert [notice["kind"] for notice in notices] == ["OTA_FAILED", "OTA_AVAILABLE"]
    assert client.post(url, headers={"X-Vehicle-Key": key}, json={
        "result": "INSTALLED", "version_after": "sim-2"}).status_code == 409
    assert client.get(f"/api/v1/vehicles/{vehicle_id}/twin",
                      headers={"X-Operator-Key": operator_key}).json()["software_version"] == "sim-1"
