"""Trip sessions preserve retries and never present manual SOC as a BMS value."""

import hashlib
import hmac
import json
import sqlite3

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile, scenario_frame
from trickee.store import CloudStore


OPERATOR = {"X-Operator-Key": "test-operator-key-123"}
VEHICLE_KEY = hmac.new(b"test-vehicle-key-123", b"TRICKEE-DEMO-1", hashlib.sha256).hexdigest()
VEHICLE = {"X-Vehicle-Key": VEHICLE_KEY}
START = {"trip_id": "PILOT-TRIP-1", "idempotency_key": "start-1", "starting_soc_pct": 82,
         "origin": {"latitude": 12.9716, "longitude": 77.5946}}
END = {"idempotency_key": "end-1", "ending_soc_pct": 76,
       "destination": {"latitude": 12.7409, "longitude": 77.8253}}


def client_with_vehicle(tmp_path, *, driver_id="DRIVER-1"):
    app = create_app(str(tmp_path / "cloud.db"), OPERATOR["X-Operator-Key"], "test-vehicle-key-123")
    client = TestClient(app)
    assert client.post("/api/v1/vehicles", headers=OPERATOR, json={
        "profile": demo_profile().model_dump(), "driver_id": driver_id,
    }).status_code == 200
    return client


def test_trip_start_complete_retries_and_manual_soc_provenance(tmp_path):
    client = client_with_vehicle(tmp_path)
    start_url = "/api/v1/vehicles/TRICKEE-DEMO-1/trips"
    assert client.post(start_url, json=START).status_code == 401
    started = client.post(start_url, headers=OPERATOR, json=START)
    assert started.status_code == 200, started.text
    assert started.json()["status"] == "ACTIVE"
    assert started.json()["driver_id"] == "DRIVER-1"
    assert started.json()["starting_soc_source"] == "MANUAL_DASHBOARD"
    assert client.post(start_url, headers=OPERATOR, json=START).json() == started.json()
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/twin", headers=OPERATOR).json()["battery"] is None

    frame = scenario_frame("SCENARIO_001", 1).model_copy(update={"trip_id": START["trip_id"]})
    assert client.post("/api/v1/telemetry/batches", headers=VEHICLE,
                       json={"frames": [frame.model_dump(mode="json")]}).status_code == 200
    complete_url = start_url + "/PILOT-TRIP-1/complete"
    completed = client.post(complete_url, headers=OPERATOR, json=END)
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["ending_soc_source"] == "MANUAL_DASHBOARD"
    assert client.post(complete_url, headers=OPERATOR, json=END).json() == completed.json()
    twin = client.get("/api/v1/vehicles/TRICKEE-DEMO-1/twin", headers=OPERATOR).json()
    assert twin["battery"]["soc_pct"] == 64
    detail = client.get(start_url + "/PILOT-TRIP-1", headers=OPERATOR).json()
    assert detail["telemetry_frame_count"] == 1
    assert detail["trace"][0]["latitude"] == 12.9716
    assert detail["starting_soc_pct"] == 82
    assert detail["ending_soc_pct"] == 76
    audit = client.app.state.store.db.execute(
        "SELECT action, COUNT(*) FROM audit WHERE action IN ('trip_started','trip_completed') GROUP BY action"
    ).fetchall()
    assert {row[0]: row[1] for row in audit} == {"trip_started": 1, "trip_completed": 1}


def test_trip_conflicts_and_assignment(tmp_path):
    client = client_with_vehicle(tmp_path)
    base = "/api/v1/vehicles/TRICKEE-DEMO-1/trips"
    assert client.post(base, headers=OPERATOR, json=START).status_code == 200
    assert client.post(base, headers=OPERATOR, json={**START, "starting_soc_pct": 81}).status_code == 409
    assert client.post(base, headers=OPERATOR, json={**START, "trip_id": "PILOT-TRIP-2", "idempotency_key": "start-2"}).status_code == 409
    assert client.post(base + "/PILOT-TRIP-1/complete", headers=OPERATOR, json=END).status_code == 200
    assert client.post(base + "/PILOT-TRIP-1/complete", headers=OPERATOR,
                       json={**END, "ending_soc_pct": 75}).status_code == 409
    assert client.post(base, headers=OPERATOR,
                       json={**START, "trip_id": "PILOT-TRIP-2", "idempotency_key": "start-2"}).status_code == 200
    assert client.get(base + "/NO-SUCH-TRIP", headers=OPERATOR).status_code == 404

    without_driver = client_with_vehicle(tmp_path / "other", driver_id=None)
    assert without_driver.post(base, headers=OPERATOR, json=START).status_code == 409


def test_unmanaged_telemetry_trip_is_grouped_with_trace(tmp_path):
    client = client_with_vehicle(tmp_path)
    first = scenario_frame("SCENARIO_001", 1).model_dump(mode="json")
    second = scenario_frame("SCENARIO_005", 2).model_dump(mode="json")
    second["latitude"] = 12.75
    second["longitude"] = 77.82
    assert client.post("/api/v1/telemetry/batches", headers=VEHICLE,
                       json={"frames": [first, second]}).status_code == 200
    rows = client.get("/api/v1/vehicles/TRICKEE-DEMO-1/trips", headers=OPERATOR).json()
    assert len(rows) == 1
    assert rows[0]["trip_id"] == "DEMO-TRIP-1"
    assert rows[0]["status"] == "UNMANAGED"
    assert rows[0]["telemetry_frame_count"] == 2
    assert rows[0]["reported_distance_km"] == 1
    detail = client.get("/api/v1/vehicles/TRICKEE-DEMO-1/trips/DEMO-TRIP-1", headers=OPERATOR).json()
    assert len(detail["trace"]) == 2
    assert detail["trace"][1]["longitude"] == 77.82


def test_existing_event_table_migrates_trip_index(tmp_path):
    db = tmp_path / "legacy.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE events (vehicle_id TEXT NOT NULL, sequence INTEGER NOT NULL, observed_at TEXT NOT NULL, received_at TEXT NOT NULL, payload_json TEXT NOT NULL, PRIMARY KEY(vehicle_id,sequence))")
    frame = scenario_frame("SCENARIO_001", 1)
    connection.execute("INSERT INTO events VALUES(?,?,?,?,?)", (
        frame.vehicle_id, frame.sequence, frame.observed_at.isoformat(), frame.observed_at.isoformat(),
        json.dumps(frame.model_dump(mode="json")),
    ))
    connection.commit()
    connection.close()
    store = CloudStore(str(db))
    assert len(store.trip_frames(frame.vehicle_id, frame.trip_id)) == 1
    store.register(demo_profile())
    store.ingest([scenario_frame("SCENARIO_001", 2), scenario_frame("SCENARIO_005", 3)])
    assert [row["sequence"] for row in store.trip_frames(frame.vehicle_id, frame.trip_id, limit=2)] == [2, 3]
    first, last = store.trip_endpoints(frame.vehicle_id, frame.trip_id)
    assert (first["sequence"], last["sequence"]) == (1, 3)
