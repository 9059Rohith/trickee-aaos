"""Driver service cases preserve scope, decisions, messages, and audit history."""

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile


OP = {"X-Operator-Key": "operator-secret-123"}
VEHICLE = "TRICKEE-DEMO-1"


def setup_client(tmp_path):
    db_path = str(tmp_path / "cases.db")
    app = create_app(db_path, OP["X-Operator-Key"], "vehicle-secret-123")
    client = TestClient(app)
    assert client.post("/api/v1/vehicles", headers=OP, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    key = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials", headers=OP).json()["token"]
    return app, client, db_path, {"X-Driver-Key": key}


def case_body(case_id="CASE-1", key="create-1"):
    return {"case_id": case_id, "idempotency_key": key,
            "category": "CHARGER_PROBLEM", "priority": "URGENT",
            "title": "Charger connector is unavailable",
            "description": "The connector will not latch at the planned station."}


def test_driver_case_creation_operator_triage_messages_resolution_and_restart(tmp_path):
    app, client, db_path, driver = setup_client(tmp_path)
    driver_url = "/api/v1/driver/service-cases"
    operator_url = f"/api/v1/vehicles/{VEHICLE}/service-cases"
    assert client.post(driver_url, headers=OP, json=case_body()).status_code == 401
    created = client.post(driver_url, headers=driver, json=case_body())
    assert created.status_code == 200, created.text
    row = created.json()
    assert row["status"] == "OPEN" and row["priority"] == "URGENT"
    assert "create_key" not in row and "create_fingerprint" not in row
    assert client.post(driver_url, headers=driver, json=case_body()).json() == row
    assert client.post(driver_url, headers=driver,
                       json={**case_body(), "description": "Changed"}).status_code == 409
    assert client.post(driver_url, headers=driver,
                       json={**case_body(), "case_id": "CASE-2"}).status_code == 409
    assert client.get(driver_url, headers=driver).json() == [row]
    assert client.get(operator_url, headers=OP).json() == [row]
    ack_body = {"idempotency_key": "ack-1"}
    acknowledged = client.post(operator_url + "/CASE-1/acknowledge", headers=OP, json=ack_body)
    assert acknowledged.status_code == 200 and acknowledged.json()["status"] == "ACKNOWLEDGED"
    assert client.post(operator_url + "/CASE-1/acknowledge", headers=OP,
                       json=ack_body).json() == acknowledged.json()
    assert client.post(operator_url + "/CASE-1/acknowledge", headers=OP,
                       json={"idempotency_key": "ack-other"}).status_code == 409
    operator_message = {"message_id": "MSG-1", "idempotency_key": "message-1",
                        "body": "Please move to another charger and keep the vehicle parked safely."}
    posted = client.post(operator_url + "/CASE-1/messages", headers=OP, json=operator_message)
    assert posted.status_code == 200
    assert client.post(operator_url + "/CASE-1/messages", headers=OP,
                       json=operator_message).json() == posted.json()
    assert client.post(operator_url + "/CASE-1/messages", headers=OP,
                       json={**operator_message, "body": "Different"}).status_code == 409
    driver_message = {"message_id": "MSG-2", "idempotency_key": "message-2",
                      "body": "I have reached a second charger."}
    assert client.post(driver_url + "/CASE-1/messages", headers=driver,
                       json=driver_message).status_code == 200
    messages = client.get(driver_url + "/CASE-1/messages", headers=driver).json()
    assert [item["body"] for item in messages] == [operator_message["body"], driver_message["body"]]
    assert [item["actor_kind"] for item in messages] == ["OPERATOR", "DRIVER"]
    resolve_body = {"idempotency_key": "resolve-1", "resolution_code": "ADVICE_GIVEN",
                    "note": "Driver found another charger; no vehicle fault confirmed."}
    resolved = client.post(operator_url + "/CASE-1/resolve", headers=OP, json=resolve_body)
    assert resolved.status_code == 200 and resolved.json()["status"] == "RESOLVED"
    assert client.post(operator_url + "/CASE-1/resolve", headers=OP,
                       json=resolve_body).json() == resolved.json()
    assert client.post(operator_url + "/CASE-1/resolve", headers=OP,
                       json={**resolve_body, "note": "Changed"}).status_code == 409
    assert client.post(driver_url + "/CASE-1/messages", headers=driver,
                       json={"message_id": "MSG-3", "idempotency_key": "message-3",
                             "body": "Late reply"}).status_code == 409
    events = client.get(operator_url + "/CASE-1/events", headers=OP).json()
    assert [event["event_type"] for event in events] == [
        "CREATED", "ACKNOWLEDGED", "MESSAGE", "MESSAGE", "RESOLVED"]
    assert client.get(f"/api/v1/vehicles/{VEHICLE}/trips", headers=OP).json() == []
    restarted = TestClient(create_app(db_path, OP["X-Operator-Key"], "vehicle-secret-123"))
    assert restarted.get(operator_url, headers=OP).json()[0]["status"] == "RESOLVED"
    assert app.state.store.db.execute("SELECT COUNT(*) FROM service_case_events").fetchone()[0] == 5


def test_service_case_validation_and_missing_records(tmp_path):
    _, client, _, driver = setup_client(tmp_path)
    driver_url = "/api/v1/driver/service-cases"
    operator_url = f"/api/v1/vehicles/{VEHICLE}/service-cases"
    for invalid in ({**case_body(), "title": "   "},
                    {**case_body(), "description": " "},
                    {**case_body(), "category": "UNKNOWN"},
                    {**case_body(), "priority": "CRITICAL"},
                    {**case_body(), "description": "x" * 1001}):
        assert client.post(driver_url, headers=driver, json=invalid).status_code == 422
    assert client.post(driver_url, headers=driver, json=case_body()).status_code == 200
    assert client.post(driver_url + "/CASE-1/messages", headers=driver,
                       json={"message_id": "MSG-1", "idempotency_key": "msg-1",
                             "body": "   "}).status_code == 422
    assert client.post(operator_url + "/CASE-1/resolve", headers=OP,
                       json={"idempotency_key": "resolve-1", "resolution_code": "OTHER",
                             "note": "   "}).status_code == 422
    assert client.get(driver_url + "/MISSING/messages", headers=driver).status_code == 404
    assert client.get(operator_url + "/MISSING/events", headers=OP).status_code == 404
    assert client.post(operator_url + "/MISSING/acknowledge", headers=OP,
                       json={"idempotency_key": "missing"}).status_code == 404


def test_service_case_tenant_role_and_driver_scope(tmp_path, monkeypatch):
    keys = {"north": "north-case-admin-123", "south": "south-case-admin-123",
            "viewer": "north-case-viewer-123"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": "north" if name == "viewer" else name,
         "role": "viewer" if name == "viewer" else "admin",
         "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for name, key in keys.items()
    ]))
    app = create_app(str(tmp_path / "scoped-cases.db"), "legacy-secret-123", "vehicle-secret-123")
    client = TestClient(app)
    app.state.store.register(demo_profile(VEHICLE), "DRIVER-N", "north")
    app.state.store.register(demo_profile("SOUTH-1"), "DRIVER-S", "south")
    headers = {name: {"X-Operator-Key": key} for name, key in keys.items()}
    north_key = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials",
                            headers=headers["north"]).json()["token"]
    south_key = client.post("/api/v1/vehicles/SOUTH-1/driver-credentials",
                            headers=headers["south"]).json()["token"]
    north_driver = {"X-Driver-Key": north_key}
    south_driver = {"X-Driver-Key": south_key}
    driver_url = "/api/v1/driver/service-cases"
    operator_url = f"/api/v1/vehicles/{VEHICLE}/service-cases"
    assert client.post(driver_url, headers=north_driver, json=case_body()).status_code == 200
    assert client.get(driver_url, headers=south_driver).json() == []
    assert client.get(driver_url + "/CASE-1/messages", headers=south_driver).status_code == 404
    assert client.post(driver_url + "/CASE-1/messages", headers=south_driver,
                       json={"message_id": "MSG-S", "idempotency_key": "south-message",
                             "body": "Cross-tenant"}).status_code == 404
    assert client.get(operator_url, headers=headers["south"]).status_code == 404
    assert client.get(operator_url + "/CASE-1/messages", headers=headers["south"]).status_code == 404
    assert client.get(operator_url + "/CASE-1/events", headers=headers["south"]).status_code == 404
    assert client.get(operator_url, headers=headers["viewer"]).status_code == 200
    assert client.post(operator_url + "/CASE-1/acknowledge", headers=headers["viewer"],
                       json={"idempotency_key": "ack-viewer"}).status_code == 403
    assert client.post(operator_url + "/CASE-1/messages", headers=headers["viewer"],
                       json={"message_id": "MSG-V", "idempotency_key": "viewer-message",
                             "body": "Forbidden"}).status_code == 403
    assert client.post(operator_url + "/CASE-1/resolve", headers=headers["south"],
                       json={"idempotency_key": "resolve-s", "resolution_code": "OTHER",
                             "note": "No access"}).status_code == 404
    assert client.post(operator_url + "/CASE-1/acknowledge", headers=headers["north"],
                       json={"idempotency_key": "ack-n"}).status_code == 200
    with app.state.store.db:
        app.state.store.db.execute("UPDATE vehicles SET driver_id='DRIVER-NEW' WHERE vehicle_id=?", (VEHICLE,))
    assert client.get(driver_url, headers=north_driver).status_code == 401
    assert app.state.service_cases.list_for_driver("north", VEHICLE, "DRIVER-NEW") == []


def test_service_case_message_replay_after_resolution_and_ordering(tmp_path):
    app, client, _, driver = setup_client(tmp_path)
    driver_url = "/api/v1/driver/service-cases"
    operator_url = f"/api/v1/vehicles/{VEHICLE}/service-cases"
    for case_id, key, priority in [("CASE-1", "key-1", "ROUTINE"),
                                   ("CASE-2", "key-2", "URGENT")]:
        assert client.post(driver_url, headers=driver,
                           json={**case_body(case_id, key), "priority": priority}).status_code == 200
    cases = client.get(operator_url, headers=OP).json()
    assert [item["case_id"] for item in cases] == ["CASE-2", "CASE-1"]
    msg = {"message_id": "MSG-1", "idempotency_key": "msg-1", "body": "Follow-up detail"}
    assert client.post(driver_url + "/CASE-1/messages", headers=driver, json=msg).status_code == 200
    resolution = {"idempotency_key": "resolve-1", "resolution_code": "SERVICE_SCHEDULED",
                  "note": "Workshop slot arranged."}
    assert client.post(operator_url + "/CASE-1/resolve", headers=OP, json=resolution).status_code == 200
    assert client.post(driver_url + "/CASE-1/messages", headers=driver, json=msg).status_code == 200
    assert client.post(driver_url + "/CASE-1/messages", headers=driver,
                       json={**msg, "message_id": "MSG-2"}).status_code == 409
    assert len(client.get(driver_url + "/CASE-1/messages", headers=driver).json()) == 1
    assert app.state.store.db.execute("SELECT COUNT(*) FROM service_case_messages").fetchone()[0] == 1


def test_service_case_fleet_summary_counts_open_urgent_and_resolved(tmp_path):
    _, client, _, driver = setup_client(tmp_path)
    driver_url = "/api/v1/driver/service-cases"
    operator_url = f"/api/v1/vehicles/{VEHICLE}/service-cases"
    summary_url = "/api/v1/fleet/summary"
    initial = client.get(summary_url, headers=OP)
    assert initial.status_code == 200
    assert initial.json()["service_cases_open"] == 0
    assert initial.json()["service_cases_urgent"] == 0
    assert client.post(driver_url, headers=driver,
                       json={**case_body("CASE-1", "create-1"), "priority": "ROUTINE"}).status_code == 200
    assert client.post(driver_url, headers=driver,
                       json=case_body("CASE-2", "create-2")).status_code == 200
    active = client.get(summary_url, headers=OP).json()
    assert active["service_cases_open"] == 2
    assert active["service_cases_urgent"] == 1
    assert client.post(operator_url + "/CASE-2/acknowledge", headers=OP,
                       json={"idempotency_key": "ack-2"}).status_code == 200
    acknowledged = client.get(summary_url, headers=OP).json()
    assert acknowledged["service_cases_open"] == 2
    assert acknowledged["service_cases_urgent"] == 1
    assert client.post(operator_url + "/CASE-2/resolve", headers=OP,
                       json={"idempotency_key": "resolve-2", "resolution_code": "ISSUE_CLEARED",
                             "note": "Driver confirmed the connector works."}).status_code == 200
    resolved = client.get(summary_url, headers=OP).json()
    assert resolved["service_cases_open"] == 1
    assert resolved["service_cases_urgent"] == 0


def test_retried_case_creation_from_parallel_requests_records_one_case(tmp_path):
    app, client, _, driver = setup_client(tmp_path)
    url = "/api/v1/driver/service-cases"
    command = case_body()

    def submit(_):
        return client.post(url, headers=driver, json=command)

    with ThreadPoolExecutor(max_workers=8) as executor:
        responses = list(executor.map(submit, range(16)))
    assert all(response.status_code == 200 for response in responses)
    assert all(response.json() == responses[0].json() for response in responses)
    assert client.get(url, headers=driver).json() == [responses[0].json()]
    assert app.state.store.db.execute("SELECT COUNT(*) FROM service_cases").fetchone()[0] == 1
    assert app.state.store.db.execute("SELECT COUNT(*) FROM service_case_events").fetchone()[0] == 1
