"""Driver GPS is authenticated, trip-owned and separate from vehicle BMS signals."""

from datetime import datetime, timezone
import hashlib
import json

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile


OP = {"X-Operator-Key": "operator-secret-123"}
VEHICLE = "TRICKEE-DEMO-1"


def test_driver_trip_gps_idempotency_provenance_and_revocation(tmp_path):
    client = TestClient(create_app(str(tmp_path / "driver.db"), OP["X-Operator-Key"], "vehicle-secret-123"))
    assert client.post("/api/v1/vehicles", headers=OP, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    provision = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials", headers=OP)
    assert provision.status_code == 200, provision.text
    credential = provision.json()
    listed = client.get(f"/api/v1/vehicles/{VEHICLE}/driver-credentials", headers=OP).json()
    assert listed[0]["credential_id"] == credential["credential_id"]
    assert "token" not in listed[0]
    driver = {"X-Driver-Key": credential["token"]}
    assert client.get("/api/v1/driver/me", headers=driver).json()["driver_id"] == "DRIVER-1"
    assert client.get("/api/v1/driver/me", headers=OP).status_code == 401
    start_body = {"trip_id": "PHONE-1", "idempotency_key": "start-1", "starting_soc_pct": 80}
    started = client.post("/api/v1/driver/trips", headers=driver, json=start_body)
    assert started.status_code == 200, started.text
    assert started.json()["source"] == "DRIVER_REPORTED"
    assert client.post("/api/v1/driver/trips", headers=driver, json=start_body).json() == started.json()
    assert client.post("/api/v1/driver/consent/phone-trip", headers=driver,
                       json={"granted": True}).status_code == 200
    now = datetime.now(timezone.utc).isoformat()
    imu = {"source": "PHONE_IMU", "window_ms": 1000,
           "accelerometer_present": True, "gyroscope_present": True,
           "accelerometer_samples": 25, "gyroscope_samples": 25,
           "raw_acceleration_rms_mps2": 9.81, "angular_rate_rms_radps": 0.2,
           "accelerometer_accuracy": 3, "gyroscope_accuracy": 3}
    batch = {"batch_id": "batch-1", "points": [
        {"sequence": 1, "timestamp": now, "lat": 12.9716, "lng": 77.5946, "accuracy": 8, "imu": imu},
        {"sequence": 2, "timestamp": now, "lat": 12.975, "lng": 77.598, "accuracy": 9},
    ]}
    url = "/api/v1/driver/trips/PHONE-1/gps-batches"
    first = client.post(url, headers=driver, json=batch)
    assert first.status_code == 200, first.text
    assert first.json()["accepted"] == 2
    assert client.post(url, headers=driver, json=batch).json() == first.json()
    saved_without_imu = client.app.state.store.db.execute(
        "SELECT payload_json FROM gps_samples WHERE trip_id='PHONE-1' AND sequence=2"
    ).fetchone()[0]
    assert "imu" not in json.loads(saved_without_imu)
    assert client.post(url, headers=driver, json={**batch, "batch_id": "batch-2",
        "points": [{**batch["points"][0], "lat": 13.0}]}).status_code == 409
    assert client.post(url, headers=driver, json={**batch, "points": [{**batch["points"][0], "lat": 13.0}]}).status_code == 409
    twin = client.get(f"/api/v1/vehicles/{VEHICLE}/twin", headers=OP).json()
    assert twin["battery"] is None and twin["location"] is None
    detail = client.get(f"/api/v1/vehicles/{VEHICLE}/trips/PHONE-1", headers=OP).json()
    assert detail["phone_gps_sample_count"] == 2
    assert detail["phone_imu_window_count"] == 1
    assert detail["phone_imu_accelerometer_samples"] == 25
    assert detail["phone_imu_gyroscope_samples"] == 25
    assert all(point["source"] == "PHONE_GPS" for point in detail["trace"])
    assert detail["quality"] == "PHONE_GPS_ONLY"
    assert client.post("/api/v1/driver/trips/PHONE-1/complete", headers=driver,
                       json={"idempotency_key": "end-1", "ending_soc_pct": 74}).status_code == 200
    assert client.post(f"/api/v1/driver-credentials/{credential['credential_id']}/revoke", headers=OP).status_code == 200
    assert client.get("/api/v1/driver/me", headers=driver).status_code == 401


def test_driver_gps_rejects_unowned_trip_and_invalid_points(tmp_path):
    client = TestClient(create_app(str(tmp_path / "driver2.db"), OP["X-Operator-Key"], "vehicle-secret-123"))
    assert client.post("/api/v1/vehicles", headers=OP, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    driver = {"X-Driver-Key": client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials", headers=OP).json()["token"]}
    assert client.post("/api/v1/driver/consent/phone-trip", headers=driver,
                       json={"granted": True}).status_code == 200
    now = datetime.now(timezone.utc).isoformat()
    sample = {"sequence": 1, "timestamp": now, "lat": 12.97, "lng": 77.59}
    assert client.post("/api/v1/driver/trips/OTHER/gps-batches", headers=driver,
                       json={"batch_id": "B1", "points": [sample]}).status_code == 404
    assert client.post("/api/v1/driver/trips", headers=driver,
                       json={"trip_id": "PHONE-2", "idempotency_key": "start-2"}).status_code == 200
    assert client.post("/api/v1/driver/trips/PHONE-2/gps-batches", headers=driver,
                       json={"batch_id": "B1", "points": [{**sample, "lat": 91}]}).status_code == 422
    assert client.post("/api/v1/driver/trips/PHONE-2/gps-batches", headers=driver,
                       json={"batch_id": "B-bad-imu", "points": [{**sample, "imu": {
                           "source": "PHONE_IMU", "window_ms": 1000,
                           "accelerometer_present": False, "gyroscope_present": False,
                           "accelerometer_samples": 1, "gyroscope_samples": 0,
                           "raw_acceleration_rms_mps2": 9.8,
                           "accelerometer_accuracy": 0, "gyroscope_accuracy": 0,
                       }}]}).status_code == 422
    assert client.post("/api/v1/driver/trips/PHONE-2/gps-batches", headers=driver,
                       json={"batch_id": "B1", "points": [{**sample, "mocked": True}]}).status_code == 200
    detail = client.get(f"/api/v1/vehicles/{VEHICLE}/trips/PHONE-2", headers=OP).json()
    assert detail["phone_gps_sample_count"] == 1
    assert detail["phone_gps_usable_count"] == 0
    assert detail["trace"] == []


def test_driver_credentials_rotate_and_obey_tenant_roles(tmp_path, monkeypatch):
    keys = {"north": "north-admin-secret-123", "south": "south-admin-secret-123",
            "viewer": "north-viewer-secret-123"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": "north" if name == "viewer" else name,
         "role": "viewer" if name == "viewer" else "admin",
         "key_sha256": hashlib.sha256(value.encode()).hexdigest()}
        for name, value in keys.items()
    ]))
    client = TestClient(create_app(str(tmp_path / "tenant-driver.db"), "legacy-secret-123", "vehicle-secret-123"))
    headers = {name: {"X-Operator-Key": value} for name, value in keys.items()}
    assert client.post("/api/v1/vehicles", headers=headers["north"], json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-N",
    }).status_code == 200
    url = f"/api/v1/vehicles/{VEHICLE}/driver-credentials"
    assert client.post(url, headers=headers["viewer"]).status_code == 403
    assert client.post(url, headers=headers["south"]).status_code == 404
    first = client.post(url, headers=headers["north"]).json()
    second = client.post(url, headers=headers["north"]).json()
    assert client.get("/api/v1/driver/me", headers={"X-Driver-Key": first["token"]}).status_code == 401
    assert client.get("/api/v1/driver/me", headers={"X-Driver-Key": second["token"]}).status_code == 200
    assert client.get(url, headers=headers["south"]).status_code == 404
    assert client.get(url, headers=headers["viewer"]).status_code == 403
    assert client.post(f"/api/v1/driver-credentials/{second['credential_id']}/revoke", headers=headers["south"]).status_code == 404
    assert client.post(f"/api/v1/driver-credentials/{second['credential_id']}/revoke", headers=headers["north"]).status_code == 200
    assert client.get("/api/v1/driver/me", headers={"X-Driver-Key": second["token"]}).status_code == 401
