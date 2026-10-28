"""Dispatch proposals must stay scoped, durable, and free of implicit trip state."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.dispatch import DispatchConflict, DispatchStore
from trickee.simulator import demo_profile


OP = {"X-Operator-Key": "operator-secret-123"}
VEHICLE = "TRICKEE-DEMO-1"
PICKUP = {"latitude": 12.9716, "longitude": 77.5946}
DESTINATION = {"latitude": 12.9916, "longitude": 77.6146}


def proposal(offer_id="O-1", key="create-1"):
    return {
        "offer_id": offer_id,
        "idempotency_key": key,
        "pickup": PICKUP,
        "destination": DESTINATION,
        "pickup_label": "Pickup at MG Road",
        "destination_label": "Drop at Indiranagar",
        "message": "Please review this pickup proposal before accepting.",
        "external_reference": "MANUAL-17",
        "expires_in_minutes": 30,
    }


def setup_client(tmp_path):
    db_path = str(tmp_path / "dispatch.db")
    app = create_app(db_path, OP["X-Operator-Key"], "vehicle-secret-123")
    client = TestClient(app)
    registered = client.post("/api/v1/vehicles", headers=OP, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    })
    assert registered.status_code == 200, registered.text
    token = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials", headers=OP).json()["token"]
    return app, client, db_path, {"X-Driver-Key": token}


def test_dispatch_end_to_end_idempotency_events_and_no_implicit_trip(tmp_path):
    app, client, db_path, driver = setup_client(tmp_path)
    operator_url = f"/api/v1/vehicles/{VEHICLE}/dispatch-proposals"
    driver_url = "/api/v1/driver/dispatch-proposals"
    assert client.post(operator_url, json=proposal()).status_code == 401
    created = client.post(operator_url, headers=OP, json=proposal())
    assert created.status_code == 200, created.text
    offer = created.json()
    assert offer["status"] == "PENDING"
    assert offer["source"] == "MANUAL_OPERATOR_PROPOSAL"
    assert offer["driver_id"] == "DRIVER-1"
    assert offer["pickup"] == PICKUP
    assert "fingerprint" not in offer and "create_key" not in offer
    assert client.post(operator_url, headers=OP, json=proposal()).json() == offer
    assert client.post(operator_url, headers=OP,
                       json={**proposal(), "message": "Changed"}).status_code == 409
    assert client.post(operator_url, headers=OP,
                       json={**proposal(), "offer_id": "O-2"}).status_code == 409
    assert client.get(operator_url, headers=OP).json() == [offer]
    assert client.get(driver_url, headers=driver).json() == [offer]
    assert client.post(driver_url + "/O-1/seen", headers=OP).status_code == 401
    seen = client.post(driver_url + "/O-1/seen", headers=driver)
    assert seen.status_code == 200 and seen.json()["seen_at"]
    assert client.post(driver_url + "/O-1/seen", headers=driver).json() == seen.json()
    response = {"idempotency_key": "respond-1", "outcome": "ACCEPTED"}
    accepted = client.post(driver_url + "/O-1/respond", headers=driver, json=response)
    assert accepted.status_code == 200 and accepted.json()["status"] == "ACCEPTED"
    assert client.post(driver_url + "/O-1/respond", headers=driver, json=response).json() == accepted.json()
    assert client.post(driver_url + "/O-1/respond", headers=driver,
                       json={**response, "outcome": "DECLINED"}).status_code == 409
    assert client.post(operator_url + "/O-1/cancel", headers=OP,
                       json={"idempotency_key": "cancel-1"}).status_code == 409
    events = client.get(operator_url + "/O-1/events", headers=OP).json()
    assert [event["event_type"] for event in events] == ["CREATED", "SEEN", "ACCEPTED"]
    assert client.get(f"/api/v1/vehicles/{VEHICLE}/trips", headers=OP).json() == []
    assert client.get("/api/v1/driver/trips", headers=driver).json() == []
    restarted = TestClient(create_app(db_path, OP["X-Operator-Key"], "vehicle-secret-123"))
    assert restarted.get(operator_url, headers=OP).json()[0]["status"] == "ACCEPTED"
    assert app.state.store.db.execute("SELECT COUNT(*) FROM dispatch_events").fetchone()[0] == 3


def test_dispatch_decline_cancel_expiry_and_validation(tmp_path):
    app, client, _, driver = setup_client(tmp_path)
    operator_url = f"/api/v1/vehicles/{VEHICLE}/dispatch-proposals"
    driver_url = "/api/v1/driver/dispatch-proposals"
    for bad in ({**proposal(), "pickup": DESTINATION},
                {**proposal(), "pickup_label": "   "},
                {**proposal(), "expires_in_minutes": 1},
                {**proposal(), "message": "x" * 501}):
        assert client.post(operator_url, headers=OP, json=bad).status_code == 422
    assert client.post(operator_url, headers=OP, json=proposal()).status_code == 200
    declined = client.post(driver_url + "/O-1/respond", headers=driver,
                           json={"idempotency_key": "decline-1", "outcome": "DECLINED"})
    assert declined.status_code == 200 and declined.json()["status"] == "DECLINED"
    assert declined.json()["seen_at"] == declined.json()["responded_at"]
    assert [event["event_type"] for event in client.get(operator_url + "/O-1/events", headers=OP).json()] == [
        "CREATED", "SEEN", "DECLINED"]
    assert client.post(operator_url, headers=OP, json=proposal("O-2", "create-2")).status_code == 200
    cancel_body = {"idempotency_key": "cancel-2", "note": "Operator withdrew the proposal"}
    cancelled = client.post(operator_url + "/O-2/cancel", headers=OP, json=cancel_body)
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "CANCELLED"
    assert client.post(operator_url + "/O-2/cancel", headers=OP, json=cancel_body).json() == cancelled.json()
    assert client.post(driver_url + "/O-2/respond", headers=driver,
                       json={"idempotency_key": "respond-2", "outcome": "ACCEPTED"}).status_code == 409
    assert client.post(operator_url, headers=OP, json=proposal("O-3", "create-3")).status_code == 200
    with app.state.store.db:
        app.state.store.db.execute(
            "UPDATE dispatch_offers SET expires_at=? WHERE offer_id='O-3'",
            ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
        )
    assert next(row for row in client.get(driver_url, headers=driver).json()
                if row["offer_id"] == "O-3")["status"] == "EXPIRED"
    assert client.post(driver_url + "/O-3/respond", headers=driver,
                       json={"idempotency_key": "respond-3", "outcome": "ACCEPTED"}).status_code == 409
    assert client.post(operator_url + "/O-3/cancel", headers=OP,
                       json={"idempotency_key": "cancel-3"}).status_code == 409


def test_dispatch_tenant_role_driver_scope_and_reassignment(tmp_path, monkeypatch):
    keys = {"north": "north-dispatch-admin-123", "south": "south-dispatch-admin-123",
            "viewer": "north-dispatch-viewer-123"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": "north" if name == "viewer" else name,
         "role": "viewer" if name == "viewer" else "admin",
         "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for name, key in keys.items()
    ]))
    app = create_app(str(tmp_path / "scoped.db"), "legacy-secret-123", "vehicle-secret-123")
    client = TestClient(app)
    app.state.store.register(demo_profile(VEHICLE), "DRIVER-N", "north")
    app.state.store.register(demo_profile("SOUTH-1"), "DRIVER-S", "south")
    headers = {name: {"X-Operator-Key": key} for name, key in keys.items()}
    operator_url = f"/api/v1/vehicles/{VEHICLE}/dispatch-proposals"
    assert client.post(operator_url, headers=headers["viewer"], json=proposal()).status_code == 403
    assert client.post(operator_url, headers=headers["south"], json=proposal()).status_code == 404
    assert client.post(operator_url, headers=headers["north"], json=proposal()).status_code == 200
    assert client.get(operator_url, headers=headers["south"]).status_code == 404
    assert client.get(operator_url + "/O-1/events", headers=headers["south"]).status_code == 404
    assert client.post(operator_url + "/O-1/cancel", headers=headers["viewer"],
                       json={"idempotency_key": "cancel-1"}).status_code == 403
    north_token = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials",
                              headers=headers["north"]).json()["token"]
    south_token = client.post("/api/v1/vehicles/SOUTH-1/driver-credentials",
                              headers=headers["south"]).json()["token"]
    north_driver = {"X-Driver-Key": north_token}
    south_driver = {"X-Driver-Key": south_token}
    driver_url = "/api/v1/driver/dispatch-proposals"
    assert client.get(driver_url, headers=south_driver).json() == []
    assert client.post(driver_url + "/O-1/seen", headers=south_driver).status_code == 404
    assert client.post(driver_url + "/O-1/respond", headers=south_driver,
                       json={"idempotency_key": "respond-s", "outcome": "ACCEPTED"}).status_code == 404
    assert client.get(driver_url, headers=north_driver).json()[0]["driver_id"] == "DRIVER-N"
    with app.state.store.db:
        app.state.store.db.execute("UPDATE vehicles SET driver_id='DRIVER-NEW' WHERE vehicle_id=?", (VEHICLE,))
    assert client.get(driver_url, headers=north_driver).status_code == 401
    assert app.state.dispatch.list_for_driver("north", VEHICLE, "DRIVER-NEW") == []
    assert client.post(operator_url, headers=headers["north"], json=proposal("O-2", "create-2")).json()[
        "driver_id"] == "DRIVER-NEW"


def test_dispatch_store_deterministic_clock_and_duplicate_after_expiry(tmp_path):
    app, _, _, _ = setup_client(tmp_path)
    store: DispatchStore = app.state.dispatch
    start = datetime(2026, 10, 7, 10, tzinfo=timezone.utc)
    payload = {k: v for k, v in proposal().items() if k not in ("offer_id", "idempotency_key")}
    created, changed = store.create("default", VEHICLE, "CLOCK-1", "clock-create", payload, "operator", start)
    assert changed and created["status"] == "PENDING"
    assert store.list_for_driver("default", VEHICLE, "DRIVER-1", now=start + timedelta(minutes=30))[0][
        "status"] == "EXPIRED"
    try:
        store.respond("default", VEHICLE, "DRIVER-1", "CLOCK-1", "clock-response", "ACCEPTED",
                      start + timedelta(minutes=30))
    except DispatchConflict:
        pass
    else:
        raise AssertionError("expired proposal was accepted")
    replay, changed = store.create("default", VEHICLE, "CLOCK-1", "clock-create", payload, "operator",
                                   start + timedelta(minutes=31))
    assert not changed and replay["status"] == "EXPIRED"
    accepted, changed = store.create("default", VEHICLE, "CLOCK-2", "clock-create-2", payload, "operator", start)
    assert changed and accepted["status"] == "PENDING"
    accepted, changed = store.respond("default", VEHICLE, "DRIVER-1", "CLOCK-2", "clock-answer-2",
                                      "ACCEPTED", start + timedelta(minutes=1))
    assert changed and accepted["status"] == "ACCEPTED"
    replay, changed = store.respond("default", VEHICLE, "DRIVER-1", "CLOCK-2", "clock-answer-2",
                                    "ACCEPTED", start + timedelta(days=1))
    assert not changed and replay["status"] == "ACCEPTED"


def test_dispatch_accept_and_cancel_race_has_one_final_outcome(tmp_path):
    app, client, _, driver = setup_client(tmp_path)
    operator_url = f"/api/v1/vehicles/{VEHICLE}/dispatch-proposals"
    driver_url = "/api/v1/driver/dispatch-proposals"
    assert client.post(operator_url, headers=OP, json=proposal()).status_code == 200
    barrier = Barrier(2)

    def accept():
        barrier.wait()
        return client.post(driver_url + "/O-1/respond", headers=driver,
                           json={"idempotency_key": "race-accept", "outcome": "ACCEPTED"})

    def cancel():
        barrier.wait()
        return client.post(operator_url + "/O-1/cancel", headers=OP,
                           json={"idempotency_key": "race-cancel", "note": "Driver unavailable"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        accepted_future = executor.submit(accept)
        cancelled_future = executor.submit(cancel)
        accepted, cancelled = accepted_future.result(), cancelled_future.result()
    assert sorted((accepted.status_code, cancelled.status_code)) == [200, 409]
    final = client.get(operator_url, headers=OP).json()[0]["status"]
    assert final in ("ACCEPTED", "CANCELLED")
    assert final == ("ACCEPTED" if accepted.status_code == 200 else "CANCELLED")
    events = client.get(operator_url + "/O-1/events", headers=OP).json()
    assert sum(event["event_type"] in ("ACCEPTED", "CANCELLED") for event in events) == 1
    assert app.state.store.db.execute("SELECT COUNT(*) FROM dispatch_offers").fetchone()[0] == 1
