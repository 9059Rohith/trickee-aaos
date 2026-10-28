"""A rule alert is a durable, tenant-scoped incident until its condition clears."""

import hashlib
import hmac
import json

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile, scenario_frame


def test_alert_acknowledge_resolve_and_recurrence(tmp_path):
    client = TestClient(create_app(str(tmp_path / "alerts.db"), "operator-secret-123", "vehicle-secret-123"))
    operator = {"X-Operator-Key": "operator-secret-123"}
    assert client.post("/api/v1/vehicles", headers=operator, json={"profile": demo_profile().model_dump()}).status_code == 200
    missing = client.get("/api/v1/fleet/alerts", headers=operator).json()
    assert len(missing) == 1
    alert_id = missing[0]["id"]
    assert client.post(f"/api/v1/fleet/alerts/{alert_id}/acknowledge", headers=operator,
                       json={"note": "Investigating telemetry"}).json()["status"] == "ACKNOWLEDGED"
    assert client.get("/api/v1/fleet/alerts", headers=operator).json()[0]["id"] == alert_id
    assert client.post(f"/api/v1/fleet/alerts/{alert_id}/resolve", headers=operator,
                       json={"note": "Checked with driver"}).json()["status"] == "RESOLVED"
    assert client.get("/api/v1/fleet/alerts", headers=operator).json() == []
    history = client.get("/api/v1/fleet/alerts/history", headers=operator).json()
    assert history[0]["id"] == alert_id
    assert history[0]["note"] == "Checked with driver"
    assert history[0]["resolution"] == "OPERATOR"
    assert client.post(f"/api/v1/fleet/alerts/{alert_id}/acknowledge", headers=operator, json={}).status_code == 409

    vehicle = {"X-Vehicle-Key": hmac.new(b"vehicle-secret-123", b"TRICKEE-DEMO-1", hashlib.sha256).hexdigest()}
    frame = scenario_frame("SCENARIO_001", 1).model_copy(update={"soc_pct": 9}).model_dump(mode="json")
    assert client.post("/api/v1/telemetry/batches", headers=vehicle, json={"frames": [frame]}).status_code == 200
    low = next(alert for alert in client.get("/api/v1/fleet/alerts", headers=operator).json() if alert["type"] == "low_soc")
    assert low["severity"] == "CRITICAL"
    assert client.post(f"/api/v1/fleet/alerts/{low['id']}/resolve", headers=operator, json={}).status_code == 200
    assert all(alert["type"] != "low_soc" for alert in client.get("/api/v1/fleet/alerts", headers=operator).json())
    clear = scenario_frame("SCENARIO_001", 2).model_copy(update={"soc_pct": 50}).model_dump(mode="json")
    again = scenario_frame("SCENARIO_001", 3).model_copy(update={"soc_pct": 9}).model_dump(mode="json")
    for data in (clear, again):
        assert client.post("/api/v1/telemetry/batches", headers=vehicle, json={"frames": [data]}).status_code == 200
        assert client.get("/api/v1/fleet/alerts", headers=operator).status_code == 200
    recurring = next(alert for alert in client.get("/api/v1/fleet/alerts", headers=operator).json() if alert["type"] == "low_soc")
    assert recurring["id"] != low["id"]
    assert recurring["status"] == "ACTIVE"


def test_alert_actions_are_tenant_scoped_and_viewer_cannot_write(tmp_path, monkeypatch):
    accounts = [
        {"tenant_id": tenant, "role": role, "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for tenant, role, key in (
            ("north", "admin", "north-admin-secret-123"),
            ("south", "admin", "south-admin-secret-123"),
            ("north", "viewer", "north-viewer-secret-123"),
        )
    ]
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps(accounts))
    client = TestClient(create_app(str(tmp_path / "scoped-alerts.db"), "legacy-secret-123", "vehicle-secret-123"))
    north = {"X-Operator-Key": "north-admin-secret-123"}
    south = {"X-Operator-Key": "south-admin-secret-123"}
    viewer = {"X-Operator-Key": "north-viewer-secret-123"}
    profile = demo_profile().model_dump()
    assert client.post("/api/v1/vehicles", headers=north, json={"profile": profile}).status_code == 200
    assert client.post("/api/v1/vehicles", headers=south,
                       json={"profile": {**profile, "vehicle_id": "SOUTH-1"}}).status_code == 200
    north_id = client.get("/api/v1/fleet/alerts", headers=north).json()[0]["id"]
    assert client.post(f"/api/v1/fleet/alerts/{north_id}/acknowledge", headers=viewer, json={}).status_code == 403
    assert client.post(f"/api/v1/fleet/alerts/{north_id}/resolve", headers=south, json={}).status_code == 404
    assert all(alert["vehicle_id"] == "SOUTH-1" for alert in client.get("/api/v1/fleet/alerts/history", headers=south).json())
