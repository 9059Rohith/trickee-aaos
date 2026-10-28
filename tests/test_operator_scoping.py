"""Tenant-scoped operator keys cannot read or mutate another fleet."""

import hashlib
import hmac
import json
import sqlite3

from fastapi.testclient import TestClient

from trickee.api import app_from_environment, create_app
from trickee.simulator import demo_profile
from trickee.simulator import scenario_frame
from trickee.store import CloudStore


def scoped_client(tmp_path, monkeypatch):
    accounts = [
        {"tenant_id": "north", "role": "admin", "actor_id": "north-admin-1",
         "key_sha256": hashlib.sha256(b"north-admin-key-123456").hexdigest()},
        {"tenant_id": "north", "role": "admin", "actor_id": "north-admin-2",
         "key_sha256": hashlib.sha256(b"north-admin-two-123456").hexdigest()},
        {"tenant_id": "south", "role": "admin", "key_sha256": hashlib.sha256(b"south-admin-key-123456").hexdigest()},
        {"tenant_id": "north", "role": "viewer", "key_sha256": hashlib.sha256(b"north-viewer-key-123456").hexdigest()},
        {"tenant_id": "north", "role": "fleet_operator", "key_sha256": hashlib.sha256(b"north-operator-key-123456").hexdigest()},
    ]
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps(accounts))
    app = create_app(str(tmp_path / "scoped.db"), "legacy-global-key-123", "vehicle-master-key-123")
    return TestClient(app)


def test_tenant_isolation_and_viewer_permissions(tmp_path, monkeypatch):
    client = scoped_client(tmp_path, monkeypatch)
    north = {"X-Operator-Key": "north-admin-key-123456"}
    south = {"X-Operator-Key": "south-admin-key-123456"}
    viewer = {"X-Operator-Key": "north-viewer-key-123456"}
    operator = {"X-Operator-Key": "north-operator-key-123456"}
    profile = demo_profile().model_dump()
    assert client.post("/api/v1/vehicles", headers=north, json={"profile": profile, "driver_id": "DRIVER-N"}).status_code == 200
    other = {**profile, "vehicle_id": "SOUTH-1"}
    assert client.post("/api/v1/vehicles", headers=south, json={"profile": other, "driver_id": "DRIVER-S"}).status_code == 200
    assert client.get("/api/v1/vehicles", headers=north).json()[0]["vehicle_id"] == profile["vehicle_id"]
    assert client.get("/api/v1/operator/me", headers=viewer).json() == {"tenant_id": "north", "role": "viewer", "scoped": True}
    assert [row["vehicle_id"] for row in client.get("/api/v1/vehicles", headers=south).json()] == ["SOUTH-1"]
    assert client.get("/api/v1/fleet/summary", headers=north).json()["vehicles"] == 1
    assert client.get("/api/v1/fleet/report", headers=north).json()["vehicles"] == []
    assert client.get("/api/v1/vehicles/SOUTH-1/twin", headers=north).status_code == 404
    assert client.get("/api/v1/vehicles/SOUTH-1/trips", headers=north).status_code == 404
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/driver-credentials", headers=south).status_code == 404
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/driver-credentials", headers=viewer).status_code == 403
    assert client.get("/api/v1/vehicles/SOUTH-1/events", headers=viewer).status_code == 404
    assert client.post("/api/v1/copilot", headers=north, json={"vehicle_id": "SOUTH-1", "question": "battery"}).status_code == 404
    assert client.post("/api/v1/ota/campaigns", headers=north,
                       json={"campaign_id": "C1", "vehicle_id": "SOUTH-1", "manifest": {}}).status_code == 404
    assert client.post("/api/v1/vehicles/SOUTH-1/trips", headers=north,
                       json={"trip_id": "T1", "idempotency_key": "start1"}).status_code == 404
    assert client.post("/api/v1/vehicles", headers=north, json={"profile": other}).status_code == 409
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/twin", headers=viewer).status_code == 200
    assert client.post("/api/v1/vehicles/TRICKEE-DEMO-1/trips", headers=viewer,
                       json={"trip_id": "T1", "idempotency_key": "start1"}).status_code == 403
    assert client.post("/api/v1/vehicles", headers=viewer, json={"profile": profile}).status_code == 403
    assert client.post("/api/v1/vehicles", headers=operator, json={"profile": profile}).status_code == 403
    assert client.post("/api/v1/vehicles/TRICKEE-DEMO-1/trips", headers=operator,
                       json={"trip_id": "T1", "idempotency_key": "start1"}).status_code == 200
    for vehicle_id, count in (("TRICKEE-DEMO-1", 1), ("SOUTH-1", 2)):
        frames = [scenario_frame("SCENARIO_001", index).model_copy(update={"vehicle_id": vehicle_id}).model_dump(mode="json")
                  for index in range(1, count + 1)]
        derived = hmac.new(b"vehicle-master-key-123", vehicle_id.encode(), hashlib.sha256).hexdigest()
        assert client.post("/api/v1/telemetry/batches", headers={"X-Vehicle-Key": derived},
                           json={"frames": frames}).status_code == 200
    assert client.get("/api/v1/fleet/report", headers=north).json()["telemetry_frames"] == 1
    assert client.get("/api/v1/fleet/report", headers=south).json()["telemetry_frames"] == 2
    assert all(alert["vehicle_id"] == "TRICKEE-DEMO-1" for alert in client.get("/api/v1/fleet/alerts", headers=north).json())
    assert client.get("/api/v1/vehicles", headers={"X-Operator-Key": "legacy-global-key-123"}).status_code == 401


def test_invalid_scoped_configuration_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", '[{"tenant_id":"x","role":"admin","key_sha256":"short"}]')
    try:
        create_app(str(tmp_path / "db.sqlite"), "legacy-global-key-123", "vehicle-master-key-123")
    except ValueError as exc:
        assert "operator account" in str(exc)
    else:
        raise AssertionError("invalid operator account config was accepted")


def test_scoped_environment_starts_without_legacy_operator_key(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    monkeypatch.delenv("TRICKEE_OPERATOR_KEY", raising=False)
    monkeypatch.setenv("TRICKEE_VEHICLE_KEY", "vehicle-master-key-1234567890123456")
    monkeypatch.setenv("TRICKEE_DB", str(tmp_path / "cloud.db"))
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([{
        "tenant_id": "north", "role": "admin",
        "key_sha256": hashlib.sha256(b"north-admin-key-123456").hexdigest(),
    }]))
    client = TestClient(app_from_environment())
    assert client.get("/health").status_code == 200
    assert client.get("/api/v1/operator/me", headers={"X-Operator-Key": "north-admin-key-123456"}).json()["tenant_id"] == "north"


def test_scoped_operator_actions_have_distinct_non_secret_audit_actors(tmp_path, monkeypatch):
    client = scoped_client(tmp_path, monkeypatch)
    first = {"X-Operator-Key": "north-admin-key-123456"}
    second = {"X-Operator-Key": "north-admin-two-123456"}
    vehicle = "TRICKEE-DEMO-1"
    assert client.post("/api/v1/vehicles", headers=first, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-N",
    }).status_code == 200
    assert client.post(f"/api/v1/vehicles/{vehicle}/driver-credentials", headers=second).status_code == 200
    assert client.get(f"/api/v1/vehicles/{vehicle}/driver-credentials", headers=first).status_code == 200
    rows = client.app.state.store.db.execute(
        "SELECT action,actor_id,detail FROM audit WHERE action IN "
        "('vehicle_registered','driver_credential_issued','driver_credentials_read') "
        "ORDER BY id"
    ).fetchall()
    assert [row["action"] for row in rows] == ["vehicle_registered", "driver_credential_issued", "driver_credentials_read"]
    assert [row["actor_id"] for row in rows] == ["north-admin-1", "north-admin-2", "north-admin-1"]
    assert rows[-1]["detail"] == "count:1"
    assert all("key-123456" not in row["actor_id"] + row["detail"] for row in rows)


def test_sensitive_location_reads_record_actor_after_authorization(tmp_path, monkeypatch):
    client = scoped_client(tmp_path, monkeypatch)
    north = {"X-Operator-Key": "north-admin-key-123456"}
    south = {"X-Operator-Key": "south-admin-key-123456"}
    vehicle = "TRICKEE-DEMO-1"
    assert client.post("/api/v1/vehicles", headers=north, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-N",
    }).status_code == 200
    assert client.post(f"/api/v1/vehicles/{vehicle}/trips", headers=north,
                       json={"trip_id": "T-AUDIT", "idempotency_key": "start-audit"}).status_code == 200
    assert client.get(f"/api/v1/vehicles/{vehicle}/events", headers=south).status_code == 404
    assert client.get(f"/api/v1/vehicles/{vehicle}/trips/T-AUDIT", headers=south).status_code == 404
    assert client.get(f"/api/v1/vehicles/{vehicle}/events", headers=north).status_code == 200
    assert client.get(f"/api/v1/vehicles/{vehicle}/trips/T-AUDIT", headers=north).status_code == 200
    rows = client.app.state.store.db.execute(
        "SELECT action,actor_id FROM audit WHERE action IN ('vehicle_events_read','trip_trace_read') ORDER BY id"
    ).fetchall()
    assert [(row["action"], row["actor_id"]) for row in rows] == [
        ("vehicle_events_read", "north-admin-1"), ("trip_trace_read", "north-admin-1")]


def test_existing_vehicle_rows_migrate_to_default_tenant(tmp_path):
    path = tmp_path / "legacy.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE vehicles (vehicle_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL, driver_id TEXT, software_version TEXT NOT NULL DEFAULT 'sim-1')")
    connection.execute("INSERT INTO vehicles(vehicle_id,profile_json) VALUES(?,?)", (
        "TRICKEE-DEMO-1", demo_profile().model_dump_json(),
    ))
    connection.commit()
    connection.close()
    store = CloudStore(str(path))
    assert store.vehicle_tenant("TRICKEE-DEMO-1") == "default"
    assert store.vehicle_ids("default") == ["TRICKEE-DEMO-1"]


def test_existing_audit_rows_gain_actor_column_without_loss(tmp_path):
    path = tmp_path / "legacy-audit.db"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE audit (id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, "
                       "action TEXT NOT NULL, vehicle_id TEXT, detail TEXT NOT NULL)")
    connection.execute("INSERT INTO audit(at,action,detail) VALUES('2026-01-01T00:00:00Z','old_action','old detail')")
    connection.commit()
    connection.close()
    store = CloudStore(str(path))
    store.audit("new_action", None, "new detail", "operator_123")
    rows = store.db.execute("SELECT action,detail,actor_id FROM audit ORDER BY id").fetchall()
    assert [(row["action"], row["detail"], row["actor_id"]) for row in rows] == [
        ("old_action", "old detail", None), ("new_action", "new detail", "operator_123")]
