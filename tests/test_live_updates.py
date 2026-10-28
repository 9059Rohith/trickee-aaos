"""Durable, tenant-scoped change hints for the browser inboxes."""

import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile


def scoped_client(tmp_path, monkeypatch):
    keys = {"north": "north-updates-admin-123", "south": "south-updates-admin-123"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": tenant, "role": "admin",
         "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for tenant, key in keys.items()
    ]))
    path = str(tmp_path / "updates.db")
    app = create_app(path, "legacy-operator-123", "vehicle-secret-123")
    client = TestClient(app)
    for tenant, vehicle in (("north", "TRICKEE-DEMO-1"), ("south", "SOUTH-1")):
        app.state.store.register(demo_profile(vehicle), "DRIVER-1", tenant)
    operator = {name: {"X-Operator-Key": key} for name, key in keys.items()}
    drivers = {}
    for tenant, vehicle in (("north", "TRICKEE-DEMO-1"), ("south", "SOUTH-1")):
        response = client.post(f"/api/v1/vehicles/{vehicle}/driver-credentials",
                               headers=operator[tenant])
        assert response.status_code == 200
        drivers[tenant] = {"X-Driver-Key": response.json()["token"]}
    return app, client, path, operator, drivers


def create_case(client, driver):
    return client.post("/api/v1/driver/service-cases", headers=driver, json={
        "case_id": "CASE-1", "idempotency_key": "create-case-1",
        "category": "CHARGER_PROBLEM", "priority": "URGENT",
        "title": "Connector unavailable", "description": "The planned charger will not connect.",
    })


def create_offer(client, operator):
    return client.post("/api/v1/vehicles/TRICKEE-DEMO-1/dispatch-proposals",
                       headers=operator, json={
        "offer_id": "OFFER-1", "idempotency_key": "create-offer-1",
        "pickup": {"latitude": 12.9716, "longitude": 77.5946},
        "destination": {"latitude": 12.9916, "longitude": 77.6146},
        "pickup_label": "Pickup", "destination_label": "Dropoff",
        "message": "Review this pickup", "expires_in_minutes": 30,
    })


def test_change_feed_is_durable_scoped_and_idempotent(tmp_path, monkeypatch):
    app, client, path, operators, drivers = scoped_client(tmp_path, monkeypatch)
    fleet_url = "/api/v1/fleet/updates?wait_seconds=0"
    driver_url = "/api/v1/driver/updates?wait_seconds=0"
    assert client.get(fleet_url).status_code == 401
    assert client.get(driver_url, headers=operators["north"]).status_code == 401
    assert client.get(fleet_url + "&after=-1", headers=operators["north"]).status_code == 422
    assert client.get(driver_url + "&wait_seconds=21", headers=drivers["north"]).status_code == 422
    assert client.get(fleet_url, headers=operators["north"]).json()["events"] == []

    created = create_case(client, drivers["north"])
    assert created.status_code == 200, created.text
    assert create_case(client, drivers["north"]).status_code == 200
    assert create_offer(client, operators["north"]).status_code == 200
    north = client.get(fleet_url, headers=operators["north"]).json()
    assert [event["kind"] for event in north["events"]] == [
        "SERVICE_CASE_CREATED", "DISPATCH_CREATED"]
    assert [event["sequence"] for event in north["events"]] == [1, 2]
    assert north["next_cursor"] == 2 and not north["reset_required"]
    assert client.get(fleet_url, headers=operators["south"]).json()["events"] == []
    driver_batch = client.get(driver_url, headers=drivers["north"]).json()
    assert [event["kind"] for event in driver_batch["events"]] == [
        "SERVICE_CASE_CREATED", "DISPATCH_CREATED"]
    assert client.get(driver_url, headers=drivers["south"]).json()["events"] == []
    assert [event["kind"] for event in app.state.store.changes("operator", "*", 0)["events"]] == [
        "SERVICE_CASE_CREATED", "DISPATCH_CREATED"]

    reply = client.post("/api/v1/driver/dispatch-proposals/OFFER-1/respond",
                        headers=drivers["north"], json={
        "idempotency_key": "reply-1", "outcome": "ACCEPTED"})
    assert reply.status_code == 200, reply.text
    changes = client.get(fleet_url + "&after=2", headers=operators["north"]).json()
    assert [event["kind"] for event in changes["events"]] == ["DISPATCH_SEEN", "DISPATCH_ACCEPTED"]
    assert changes["next_cursor"] == 4
    assert client.post("/api/v1/driver/dispatch-proposals/OFFER-1/respond",
                       headers=drivers["north"], json={
        "idempotency_key": "reply-1", "outcome": "ACCEPTED"}).status_code == 200
    assert client.get(fleet_url + "&after=4", headers=operators["north"]).json()["events"] == []

    restarted = TestClient(create_app(path, "legacy-operator-123", "vehicle-secret-123"))
    assert restarted.get(fleet_url + "&after=2", headers=operators["north"]).json()["next_cursor"] == 4
    assert client.post("/api/v1/driver-credentials/D-1/revoke",
                       headers=operators["north"]).status_code == 200
    assert client.get(driver_url, headers=drivers["north"]).status_code == 401
    assert app.state.store.changes("operator", "south", 0)["events"] == []


def test_wait_returns_after_a_committed_change(tmp_path, monkeypatch):
    _, client, _, operators, drivers = scoped_client(tmp_path, monkeypatch)
    start = time.monotonic()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.get, "/api/v1/fleet/updates?after=0&wait_seconds=3",
                              headers=operators["north"])
        time.sleep(0.3)
        assert create_case(client, drivers["north"]).status_code == 200
        response = pending.result(timeout=4)
    assert response.status_code == 200
    assert response.json()["events"][0]["kind"] == "SERVICE_CASE_CREATED"
    assert time.monotonic() - start < 3


def test_plans_advisories_and_trip_changes_reach_the_assigned_driver(tmp_path, monkeypatch):
    app, client, _, operators, drivers = scoped_client(tmp_path, monkeypatch)
    store = app.state.store
    vehicle = "TRICKEE-DEMO-1"
    store.create_daily_plan(vehicle, "PLAN-1", "draft-1", {"service_date": "2026-10-08"})
    store.edit_daily_plan(vehicle, "PLAN-1", "edit-1", 1, {"service_date": "2026-10-09"})
    store.confirm_daily_plan(vehicle, "PLAN-1", "confirm-1", {"quality": "UNAVAILABLE"})
    store.create_nudge(vehicle, "NUDGE-1", "nudge-1", "ROUTE_REVIEW",
                       "Check the route", "PLAN-1")
    store.respond_nudge(vehicle, "DRIVER-1", "NUDGE-1", "answer-1", "ACKNOWLEDGED")
    store.start_trip(vehicle, "TRIP-1", "start-1", 60, None)
    store.start_trip_stop(vehicle, "DRIVER-1", "TRIP-1", "STOP-1", "stop-1", "WAITING")
    store.complete_trip_stop(vehicle, "DRIVER-1", "TRIP-1", "STOP-1", "stop-end-1")
    store.complete_trip(vehicle, "TRIP-1", "complete-1", 55, None)

    operator_kinds = [event["kind"] for event in client.get(
        "/api/v1/fleet/updates?wait_seconds=0", headers=operators["north"]).json()["events"]]
    assert operator_kinds == ["PLAN_DRAFTED", "PLAN_EDITED", "PLAN_CONFIRMED",
                              "ADVISORY_CREATED", "ADVISORY_ACKNOWLEDGED", "TRIP_STARTED",
                              "TRIP_STOP_STARTED", "TRIP_STOP_COMPLETED", "TRIP_COMPLETED"]
    driver_kinds = [event["kind"] for event in client.get(
        "/api/v1/driver/updates?wait_seconds=0", headers=drivers["north"]).json()["events"]]
    assert driver_kinds == operator_kinds[2:]
    assert client.get("/api/v1/fleet/updates?wait_seconds=0",
                      headers=operators["south"]).json()["events"] == []


def test_expired_cursor_requires_snapshot_refresh(tmp_path, monkeypatch):
    app, client, _, operators, _ = scoped_client(tmp_path, monkeypatch)
    store = app.state.store
    with store._write_lock, store.db:
        for n in range(2050):
            store.publish_change("north", "TRICKEE-DEMO-1", None,
                                 "TEST_CHANGE", str(n), "2026-10-07T00:00:00+00:00")
    response = client.get("/api/v1/fleet/updates?after=1&wait_seconds=0",
                          headers=operators["north"])
    assert response.status_code == 200
    assert response.json() == {"events": [], "next_cursor": 2050, "reset_required": True}
    current = client.get("/api/v1/fleet/updates?after=2050&wait_seconds=0",
                         headers=operators["north"]).json()
    assert current == {"events": [], "next_cursor": 2050, "reset_required": False}
    ahead = client.get("/api/v1/fleet/updates?after=9999&wait_seconds=0",
                       headers=operators["north"]).json()
    assert ahead == {"events": [], "next_cursor": 2050, "reset_required": True}
    empty = client.get("/api/v1/fleet/updates?after=7&wait_seconds=0",
                       headers=operators["south"]).json()
    assert empty == {"events": [], "next_cursor": 0, "reset_required": True}


def test_waiting_driver_is_rejected_after_credential_revocation(tmp_path, monkeypatch):
    _, client, _, operators, drivers = scoped_client(tmp_path, monkeypatch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(client.get, "/api/v1/driver/updates?wait_seconds=3",
                              headers=drivers["north"])
        time.sleep(0.3)
        revoked = client.post("/api/v1/driver-credentials/D-1/revoke",
                              headers=operators["north"])
        assert revoked.status_code == 200
        response = pending.result(timeout=4)
    assert response.status_code == 401
