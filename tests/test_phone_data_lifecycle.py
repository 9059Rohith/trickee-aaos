"""Driver cloud phone-data export and erasure keep consent and replay consistent."""

from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi.testclient import TestClient

from scripts.backup import backup_database
from trickee.api import create_app
from trickee.simulator import demo_profile


def _ready_driver(tmp_path):
    path = tmp_path / "lifecycle.db"
    client = TestClient(create_app(str(path), "operator-secret-123", "vehicle-secret-123"))
    operator = {"X-Operator-Key": "operator-secret-123"}
    vehicle = "TRICKEE-DEMO-1"
    assert client.post("/api/v1/vehicles", headers=operator, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    token = client.post(f"/api/v1/vehicles/{vehicle}/driver-credentials", headers=operator).json()["token"]
    driver = {"X-Driver-Key": token}
    assert client.post("/api/v1/driver/consent/phone-trip", headers=driver,
                       json={"granted": True}).status_code == 200
    assert client.post("/api/v1/driver/trips", headers=driver,
                       json={"trip_id": "DATA-1", "idempotency_key": "start"}).status_code == 200
    timestamp = datetime.now(timezone.utc).isoformat()
    batch = {"batch_id": "B1", "points": [{"sequence": 1, "timestamp": timestamp,
             "lat": 12.9716, "lng": 77.5946, "accuracy": 8}]}
    gps = "/api/v1/driver/trips/DATA-1/gps-batches"
    assert client.post(gps, headers=driver, json=batch).status_code == 200
    return client, path, driver, gps, batch


def test_export_erasure_and_stale_replay_prevention(tmp_path):
    client, path, driver, gps, batch = _ready_driver(tmp_path)
    export = "/api/v1/driver/phone-data/export"
    erase = "/api/v1/driver/phone-data"
    assert client.get(export).status_code == 401
    before = client.get(export, headers=driver)
    assert before.status_code == 200
    assert before.json()["total"] == 1
    assert before.json()["samples"][0]["point"]["lat"] == 12.9716
    assert client.delete(erase, headers=driver).status_code == 409  # active trip
    assert client.get(export, headers=driver).json()["total"] == 1
    assert client.post("/api/v1/driver/trips/DATA-1/complete", headers=driver,
                       json={"idempotency_key": "end"}).status_code == 200
    deleted = client.delete(erase, headers=driver)
    assert deleted.status_code == 200
    assert deleted.json()["deleted_samples"] == 1
    assert client.get(export, headers=driver).json()["total"] == 0
    assert client.get("/api/v1/driver/consent/phone-trip", headers=driver).json()["granted"] is False
    assert client.post("/api/v1/driver/consent/phone-trip", headers=driver,
                       json={"granted": True}).status_code == 200
    assert client.post(gps, headers=driver, json=batch).status_code == 403
    assert client.app.state.store.db.execute(
        "SELECT COUNT(*) FROM gps_batches WHERE trip_id='DATA-1'").fetchone()[0] == 0
    assert client.app.state.store.db.execute(
        "SELECT COUNT(*) FROM audit WHERE action='driver_phone_data_erased'").fetchone()[0] == 1

    backup = tmp_path / "post-erasure.db"
    backup_database(str(path), str(backup))
    restored = TestClient(create_app(str(backup), "operator-secret-123", "vehicle-secret-123"))
    assert restored.get(export, headers=driver).json()["total"] == 0
    assert restored.post(gps, headers=driver, json=batch).status_code == 403
    assert restored.post("/api/v1/driver/trips", headers=driver,
                         json={"trip_id": "DATA-2", "idempotency_key": "start-2"}).status_code == 200
    fresh = {"batch_id": "B2", "points": [{**batch["points"][0],
             "timestamp": datetime.now(timezone.utc).isoformat()}]}
    assert restored.post("/api/v1/driver/trips/DATA-2/gps-batches",
                         headers=driver, json=fresh).status_code == 200


def test_phone_data_export_pagination_and_tenant_isolation(tmp_path, monkeypatch):
    keys = {"north": "north-secret-123456", "south": "south-secret-123456"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": tenant, "role": "admin", "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for tenant, key in keys.items()
    ]))
    client = TestClient(create_app(str(tmp_path / "tenants.db"), "legacy-secret-123", "vehicle-secret-123"))
    credentials = {}
    for tenant, vehicle in (("north", "CAR-N"), ("south", "CAR-S")):
        operator = {"X-Operator-Key": keys[tenant]}
        profile = demo_profile().model_copy(update={"vehicle_id": vehicle})
        assert client.post("/api/v1/vehicles", headers=operator, json={
            "profile": profile.model_dump(), "driver_id": "SAME-DRIVER"}).status_code == 200
        token = client.post(f"/api/v1/vehicles/{vehicle}/driver-credentials", headers=operator).json()["token"]
        credentials[tenant] = {"X-Driver-Key": token}
        assert client.post("/api/v1/driver/consent/phone-trip", headers=credentials[tenant],
                           json={"granted": True}).status_code == 200
        trip_id = f"TRIP-{tenant}"
        assert client.post("/api/v1/driver/trips", headers=credentials[tenant],
                           json={"trip_id": trip_id, "idempotency_key": "start"}).status_code == 200
        batch = {"batch_id": "B1", "points": [{"sequence": sequence,
                 "timestamp": datetime.now(timezone.utc).isoformat(),
                 "lat": 12.97 + sequence / 1000, "lng": 77.59} for sequence in (1, 2)]}
        assert client.post(f"/api/v1/driver/trips/{trip_id}/gps-batches",
                           headers=credentials[tenant], json=batch).status_code == 200
        assert client.post(f"/api/v1/driver/trips/{trip_id}/complete", headers=credentials[tenant],
                           json={"idempotency_key": "end"}).status_code == 200
    export = "/api/v1/driver/phone-data/export?limit=1"
    first = client.get(export, headers=credentials["north"]).json()
    assert first["total"] == 2 and first["next_offset"] == 1
    assert first["samples"][0]["trip_id"] == "TRIP-north"
    second = client.get(export + "&offset=1", headers=credentials["north"]).json()
    assert second["samples"][0]["sequence"] == 2 and second["next_offset"] is None
    assert client.delete("/api/v1/driver/phone-data", headers=credentials["north"]).json()["deleted_samples"] == 2
    assert client.get(export, headers=credentials["north"]).json()["total"] == 0
    assert client.get(export, headers=credentials["south"]).json()["total"] == 2


def test_completed_trip_retention_dry_run_and_purge(tmp_path):
    client, _, driver, gps, batch = _ready_driver(tmp_path)
    assert client.post("/api/v1/driver/trips/DATA-1/complete", headers=driver,
                       json={"idempotency_key": "end"}).status_code == 200
    store = client.app.state.store
    past = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    store.db.execute("UPDATE trip_sessions SET ended_at=? WHERE trip_id='DATA-1'", (past,))
    store.db.commit()
    report = store.purge_expired_phone_data(30, dry_run=True)
    assert report["eligible_trips"] == 1 and report["deleted_samples"] == 0
    assert client.get("/api/v1/driver/phone-data/export", headers=driver).json()["total"] == 1
    report = store.purge_expired_phone_data(30)
    assert report["eligible_trips"] == 1 and report["deleted_samples"] == 1
    assert client.get("/api/v1/driver/phone-data/export", headers=driver).json()["total"] == 0
    assert client.post(gps, headers=driver, json=batch).status_code == 403


def test_expired_completed_trip_rejects_new_late_batch(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_PHONE_GPS_RETENTION_DAYS", "30")
    client, _, driver, _, _ = _ready_driver(tmp_path)
    assert client.post("/api/v1/driver/trips/DATA-1/complete", headers=driver,
                       json={"idempotency_key": "end"}).status_code == 200
    old_end = (datetime.now(timezone.utc) - timedelta(days=40)).isoformat()
    store = client.app.state.store
    store.db.execute("UPDATE trip_sessions SET ended_at=? WHERE trip_id='DATA-1'", (old_end,))
    store.db.execute("DELETE FROM gps_samples WHERE trip_id='DATA-1'")
    store.db.execute("DELETE FROM gps_batches WHERE trip_id='DATA-1'")
    store.db.commit()
    late = {"batch_id": "late", "points": [{"sequence": 2,
            "timestamp": datetime.now(timezone.utc).isoformat(), "lat": 12.97, "lng": 77.59}]}
    response = client.post("/api/v1/driver/trips/DATA-1/gps-batches", headers=driver, json=late)
    assert response.status_code == 403
