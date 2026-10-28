"""Operator advisories have durable driver-owned outcomes, never silent delivery."""

import hashlib
import json

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile


OP = {"X-Operator-Key": "operator-secret-123"}
VEHICLE = "TRICKEE-DEMO-1"


def test_nudge_create_inbox_outcome_idempotency_and_plan_link(tmp_path):
    client = TestClient(create_app(str(tmp_path / "nudges.db"), OP["X-Operator-Key"], "vehicle-secret-123"))
    client.post("/api/v1/vehicles", headers=OP, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    })
    token = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials", headers=OP).json()["token"]
    driver = {"X-Driver-Key": token}
    url = f"/api/v1/vehicles/{VEHICLE}/nudges"
    body = {"nudge_id": "N-1", "idempotency_key": "create-1", "kind": "ROUTE_REVIEW",
            "message": "Review the confirmed pickup route."}
    assert client.post(url, json=body).status_code == 401
    created = client.post(url, headers=OP, json=body)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "PENDING" and created.json()["driver_id"] == "DRIVER-1"
    assert "create_key" not in created.json() and "fingerprint" not in created.json()
    assert client.post(url, headers=OP, json=body).json() == created.json()
    assert client.post(url, headers=OP, json={**body, "message": "Changed"}).status_code == 409
    assert client.get(url, headers=OP).json()[0]["nudge_id"] == "N-1"
    assert client.get("/api/v1/driver/nudges", headers=driver).json()[0]["status"] == "PENDING"
    outcome_url = "/api/v1/driver/nudges/N-1/respond"
    response = {"idempotency_key": "answer-1", "outcome": "ACKNOWLEDGED"}
    acknowledged = client.post(outcome_url, headers=driver, json=response)
    assert acknowledged.status_code == 200 and acknowledged.json()["status"] == "ACKNOWLEDGED"
    assert client.post(outcome_url, headers=driver, json=response).json() == acknowledged.json()
    assert client.post(outcome_url, headers=driver, json={**response, "outcome": "DECLINED"}).status_code == 409
    assert client.post(outcome_url, headers=OP, json=response).status_code == 401
    assert client.post(url, headers=OP, json={**body, "nudge_id": "N-2", "idempotency_key": "create-2",
                                                   "plan_id": "MISSING"}).status_code == 409
    plan = {"plan_id": "DAY-1", "idempotency_key": "draft-1", "service_date": "2026-10-07",
            "timezone": "Asia/Kolkata", "starting_soc_pct": 80,
            "origin": {"latitude": 12.9716, "longitude": 77.5946},
            "stops": [{"label": "Office", "requested_arrival_local": "09:00",
                       "coordinates": {"latitude": 12.98, "longitude": 77.60}}]}
    plan_url = f"/api/v1/vehicles/{VEHICLE}/daily-plans"
    assert client.post(plan_url, headers=OP, json=plan).status_code == 200
    assert client.get("/api/v1/driver/daily-plans", headers=driver).json() == []
    linked = {**body, "nudge_id": "N-2", "idempotency_key": "create-2", "plan_id": "DAY-1"}
    assert client.post(url, headers=OP, json=linked).status_code == 409
    assert client.post(plan_url + "/DAY-1/confirm", headers=OP,
                       json={"idempotency_key": "confirm-1"}).status_code == 200
    confirmed = client.get("/api/v1/driver/daily-plans", headers=driver)
    assert confirmed.status_code == 200 and [item["plan_id"] for item in confirmed.json()] == ["DAY-1"]
    accepted = client.post(url, headers=OP, json=linked)
    assert accepted.status_code == 200 and accepted.json()["plan_id"] == "DAY-1"


def test_nudge_role_tenant_driver_scope_and_decline(tmp_path, monkeypatch):
    keys = {"north": "north-nudge-admin-123", "south": "south-nudge-admin-123",
            "viewer": "north-nudge-viewer-123"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": "north" if name == "viewer" else name,
         "role": "viewer" if name == "viewer" else "admin",
         "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for name, key in keys.items()
    ]))
    app = create_app(str(tmp_path / "tenant-nudges.db"), "legacy-secret-123", "vehicle-secret-123")
    client = TestClient(app)
    app.state.store.register(demo_profile(VEHICLE), "DRIVER-N", "north")
    app.state.store.register(demo_profile("SOUTH-1"), "DRIVER-S", "south")
    headers = {name: {"X-Operator-Key": key} for name, key in keys.items()}
    url = f"/api/v1/vehicles/{VEHICLE}/nudges"
    body = {"nudge_id": "N-tenant", "idempotency_key": "tenant-create", "kind": "CHARGING",
            "message": "Review charging options."}
    assert client.post(url, headers=headers["viewer"], json=body).status_code == 403
    assert client.post(url, headers=headers["south"], json=body).status_code == 404
    assert client.post(url, headers=headers["north"], json=body).status_code == 200
    assert client.get(url, headers=headers["south"]).status_code == 404
    north_token = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials",
                              headers=headers["north"]).json()["token"]
    south_token = client.post("/api/v1/vehicles/SOUTH-1/driver-credentials",
                              headers=headers["south"]).json()["token"]
    assert client.get("/api/v1/driver/nudges", headers={"X-Driver-Key": south_token}).json() == []
    assert len(client.get("/api/v1/driver/nudges", headers={"X-Driver-Key": north_token}).json()) == 1
    respond = "/api/v1/driver/nudges/N-tenant/respond"
    answer = {"idempotency_key": "decline-1", "outcome": "DECLINED"}
    assert client.post(respond, headers={"X-Driver-Key": south_token}, json=answer).status_code == 404
    declined = client.post(respond, headers={"X-Driver-Key": north_token}, json=answer)
    assert declined.status_code == 200 and declined.json()["status"] == "DECLINED"
