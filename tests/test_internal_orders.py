"""Internal assignments must never masquerade as partner-confirmed deliveries."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile


VEHICLE = "TRICKEE-DEMO-1"
OPERATOR = {"X-Operator-Key": "order-operator-key-123"}
PICKUP = {"latitude": 12.9716, "longitude": 77.5946}
DESTINATION = {"latitude": 12.9916, "longitude": 77.6146}
PROPOSALS = f"/api/v1/vehicles/{VEHICLE}/dispatch-proposals"
ORDERS = f"/api/v1/vehicles/{VEHICLE}/internal-orders"
DRIVER_ORDERS = "/api/v1/driver/internal-orders"


def proposal(offer_id="O-1", key="offer-create-1"):
    return {"offer_id": offer_id, "idempotency_key": key, "pickup": PICKUP,
            "destination": DESTINATION, "pickup_label": "MG Road",
            "destination_label": "Indiranagar", "message": "Confirm pickup location",
            "external_reference": "LOCAL-17", "expires_in_minutes": 30}


def setup(tmp_path):
    db_path = str(tmp_path / "orders.db")
    app = create_app(db_path, OPERATOR["X-Operator-Key"], "vehicle-secret-123")
    client = TestClient(app)
    assert client.post("/api/v1/vehicles", headers=OPERATOR, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    token = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials",
                        headers=OPERATOR).json()["token"]
    return app, client, db_path, {"X-Driver-Key": token}


def accepted(client, driver):
    assert client.post(PROPOSALS, headers=OPERATOR, json=proposal()).status_code == 200
    response = client.post("/api/v1/driver/dispatch-proposals/O-1/respond", headers=driver,
                           json={"idempotency_key": "offer-accept-1", "outcome": "ACCEPTED"})
    assert response.status_code == 200, response.text


def assignment(client):
    return client.post(PROPOSALS + "/O-1/assign", headers=OPERATOR,
                       json={"order_id": "ORDER-1", "idempotency_key": "assign-1"})


def test_assignment_driver_report_operator_reconciliation_and_restart(tmp_path):
    app, client, db_path, driver = setup(tmp_path)
    assert client.get(ORDERS).status_code == 401
    assert assignment(client).status_code == 404
    assert client.post(PROPOSALS, headers=OPERATOR, json=proposal()).status_code == 200
    assert assignment(client).status_code == 409
    response = client.post("/api/v1/driver/dispatch-proposals/O-1/respond", headers=driver,
                           json={"idempotency_key": "offer-accept-1", "outcome": "ACCEPTED"})
    assert response.status_code == 200
    assigned = assignment(client)
    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["status"] == "ASSIGNED"
    assert assigned.json()["source"] == "MANUAL_OPERATOR_CONFIRMED"
    assert assigned.json()["fulfilment_quality"] == "UNVERIFIED"
    assert assigned.json()["pickup"] == PICKUP
    assert "create_key" not in assigned.json()
    assert assignment(client).json() == assigned.json()
    assert client.post(PROPOSALS + "/O-1/assign", headers=OPERATOR,
                       json={"order_id": "ORDER-2", "idempotency_key": "assign-2"}).status_code == 409
    assert client.get(DRIVER_ORDERS, headers=driver).json() == [assigned.json()]
    assert client.get(ORDERS, headers=OPERATOR).json() == [assigned.json()]
    assert client.post(ORDERS + "/ORDER-1/reconcile", headers=OPERATOR,
                       json={"idempotency_key": "early", "note": "Checked"}).status_code == 409

    pickup = client.post(DRIVER_ORDERS + "/ORDER-1/pickup", headers=driver,
                         json={"idempotency_key": "pickup-1"})
    assert pickup.status_code == 200 and pickup.json()["status"] == "PICKED_UP"
    assert client.post(DRIVER_ORDERS + "/ORDER-1/pickup", headers=driver,
                       json={"idempotency_key": "pickup-1"}).json() == pickup.json()
    assert client.post(ORDERS + "/ORDER-1/cancel", headers=OPERATOR,
                       json={"idempotency_key": "late-cancel", "note": "Too late"}).status_code == 409
    delivery = client.post(DRIVER_ORDERS + "/ORDER-1/report-delivery", headers=driver,
                           json={"idempotency_key": "delivery-1"})
    assert delivery.status_code == 200 and delivery.json()["status"] == "DELIVERED_REPORTED"
    assert delivery.json()["fulfilment_quality"] == "DRIVER_REPORTED"
    assert client.post(DRIVER_ORDERS + "/ORDER-1/report-delivery", headers=driver,
                       json={"idempotency_key": "delivery-1"}).json() == delivery.json()
    assert client.post(ORDERS + "/ORDER-1/reconcile", headers=OPERATOR,
                       json={"idempotency_key": "reconcile-1", "note": "  "}).status_code == 422
    verified = client.post(ORDERS + "/ORDER-1/reconcile", headers=OPERATOR,
                           json={"idempotency_key": "reconcile-1", "note": "  Called driver  "})
    assert verified.status_code == 200 and verified.json()["status"] == "RECONCILED"
    assert verified.json()["fulfilment_quality"] == "MANUAL_RECONCILED"
    assert verified.json()["reconciliation_note"] == "Called driver"
    assert client.post(ORDERS + "/ORDER-1/reconcile", headers=OPERATOR,
                       json={"idempotency_key": "reconcile-1", "note": "Called driver"}).json() == verified.json()
    assert client.post(ORDERS + "/ORDER-1/reconcile", headers=OPERATOR,
                       json={"idempotency_key": "reconcile-1", "note": "Different note"}).status_code == 409
    assert [event["event_type"] for event in client.get(ORDERS + "/ORDER-1/events",
                                                        headers=OPERATOR).json()] == [
        "ASSIGNED", "PICKED_UP", "DELIVERED_REPORTED", "RECONCILED"]
    assert client.get(f"/api/v1/vehicles/{VEHICLE}/trips", headers=OPERATOR).json() == []
    assert app.state.store.db.execute("SELECT COUNT(*) FROM internal_order_events").fetchone()[0] == 4
    reopened = TestClient(create_app(db_path, OPERATOR["X-Operator-Key"], "vehicle-secret-123"))
    assert reopened.get(ORDERS, headers=OPERATOR).json() == [verified.json()]


def test_cancellation_conflicting_keys_and_scope(tmp_path):
    app, client, _, driver = setup(tmp_path)
    accepted(client, driver)
    assert assignment(client).status_code == 200
    assert client.post(ORDERS + "/ORDER-1/cancel", headers=OPERATOR,
                       json={"idempotency_key": "cancel-1", "note": "  Driver unavailable  "}).json()[
                           "cancellation_note"] == "Driver unavailable"
    assert client.post(ORDERS + "/ORDER-1/cancel", headers=OPERATOR,
                       json={"idempotency_key": "cancel-1", "note": "Driver unavailable"}).status_code == 200
    assert client.post(DRIVER_ORDERS + "/ORDER-1/pickup", headers=driver,
                       json={"idempotency_key": "pickup-1"}).status_code == 409
    assert client.post(ORDERS + "/ORDER-1/cancel", headers=OPERATOR,
                       json={"idempotency_key": "cancel-1", "note": "Other note"}).status_code == 409
    assert client.post(ORDERS + "/ORDER-1/reconcile", headers=OPERATOR,
                       json={"idempotency_key": "cancel-1", "note": "Other note"}).status_code == 409
    assert client.post(DRIVER_ORDERS + "/ORDER-1/pickup", headers=OPERATOR,
                       json={"idempotency_key": "pickup-2"}).status_code == 401
    with app.state.store.db:
        app.state.store.db.execute("UPDATE vehicles SET driver_id='DRIVER-2' WHERE vehicle_id=?", (VEHICLE,))
    assert client.get(DRIVER_ORDERS, headers=driver).status_code == 401
    assert client.get(ORDERS, headers=OPERATOR).json()[0]["status"] == "CANCELLED"


def test_tenant_viewer_and_parallel_transition(tmp_path, monkeypatch):
    keys = {"north": "north-order-admin-123", "south": "south-order-admin-123",
            "viewer": "north-order-viewer-123"}
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
    north = {"X-Operator-Key": keys["north"]}
    south = {"X-Operator-Key": keys["south"]}
    viewer = {"X-Operator-Key": keys["viewer"]}
    token = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials", headers=north).json()["token"]
    driver = {"X-Driver-Key": token}
    assert client.post(PROPOSALS, headers=north, json=proposal()).status_code == 200
    assert client.post("/api/v1/driver/dispatch-proposals/O-1/respond", headers=driver,
                       json={"idempotency_key": "accept", "outcome": "ACCEPTED"}).status_code == 200
    assert client.post(PROPOSALS + "/O-1/assign", headers=viewer,
                       json={"order_id": "ORDER-1", "idempotency_key": "assign-1"}).status_code == 403
    assert client.post(PROPOSALS + "/O-1/assign", headers=south,
                       json={"order_id": "ORDER-1", "idempotency_key": "assign-1"}).status_code == 404
    assert client.post(PROPOSALS + "/O-1/assign", headers=north,
                       json={"order_id": "ORDER-1", "idempotency_key": "assign-1"}).status_code == 200
    assert client.get(ORDERS, headers=south).status_code == 404
    assert client.get(ORDERS + "/ORDER-1/events", headers=south).status_code == 404
    assert client.get(ORDERS, headers=viewer).status_code == 200
    assert client.post(ORDERS + "/ORDER-1/cancel", headers=viewer,
                       json={"idempotency_key": "cancel", "note": "Driver unavailable"}).status_code == 403
    barrier = Barrier(2)

    def pickup():
        barrier.wait()
        return client.post(DRIVER_ORDERS + "/ORDER-1/pickup", headers=driver,
                           json={"idempotency_key": "pickup-race"})

    def cancel():
        barrier.wait()
        return client.post(ORDERS + "/ORDER-1/cancel", headers=north,
                           json={"idempotency_key": "cancel-race", "note": "Vehicle unavailable"})

    with ThreadPoolExecutor(max_workers=2) as executor:
        a = executor.submit(pickup)
        b = executor.submit(cancel)
        statuses = sorted((a.result().status_code, b.result().status_code))
    assert statuses == [200, 409]
    final = client.get(ORDERS, headers=north).json()[0]["status"]
    assert final in ("PICKED_UP", "CANCELLED")
    assert app.state.store.db.execute(
        "SELECT COUNT(*) FROM internal_order_events WHERE event_type IN ('PICKED_UP','CANCELLED')"
    ).fetchone()[0] == 1


def test_daily_metrics_label_manual_activity_without_fabricated_partner_completion(tmp_path):
    _, client, _, driver = setup(tmp_path)
    accepted(client, driver)
    assert assignment(client).status_code == 200
    assert client.post(DRIVER_ORDERS + "/ORDER-1/pickup", headers=driver,
                       json={"idempotency_key": "pickup-1"}).status_code == 200
    assert client.post(DRIVER_ORDERS + "/ORDER-1/report-delivery", headers=driver,
                       json={"idempotency_key": "delivery-1"}).status_code == 200
    assert client.post(ORDERS + "/ORDER-1/reconcile", headers=OPERATOR,
                       json={"idempotency_key": "reconcile-1", "note": "Manual call with driver"}).status_code == 200
    today = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    response = client.get("/api/v1/fleet/daily-operations", headers=OPERATOR,
                          params={"date": today, "timezone": "Asia/Kolkata"})
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["quality"] == "AGGREGATED"
    assert report["trips_started"] == 0
    assert report["internal_assignments_created"] == 1
    assert report["internal_pickups_reported"] == 1
    assert report["internal_deliveries_reported"] == 1
    assert report["internal_deliveries_manually_reconciled"] == 1
    assert report["internal_assignments_cancelled"] == 0
    assert report["orders_completed"] is None
