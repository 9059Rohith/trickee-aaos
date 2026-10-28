"""Driver-reported stops are durable, idempotent and separate from BMS charging."""

from datetime import datetime, timezone

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile, scenario_frame


OP = {"X-Operator-Key": "operator-secret-123"}


def test_driver_wait_charge_stop_lifecycle_and_trip_guard(tmp_path):
    client = TestClient(create_app(str(tmp_path / "stops.db"), OP["X-Operator-Key"], "vehicle-secret-123"))
    vehicle = "TRICKEE-DEMO-1"
    assert client.post("/api/v1/vehicles", headers=OP, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    token = client.post(f"/api/v1/vehicles/{vehicle}/driver-credentials", headers=OP).json()["token"]
    driver = {"X-Driver-Key": token}
    assert client.post("/api/v1/driver/trips", headers=driver,
                       json={"trip_id": "TRIP-1", "idempotency_key": "start-1"}).status_code == 200
    path = "/api/v1/driver/trips/TRIP-1/stops"
    wait = {"stop_id": "WAIT-1", "idempotency_key": "wait-1", "kind": "WAITING"}
    first = client.post(path, headers=driver, json=wait)
    assert first.status_code == 200 and first.json()["active_stop"]["kind"] == "WAITING"
    assert client.post(path, headers=driver, json=wait).json() == first.json()
    assert client.post(path, headers=driver, json={**wait, "kind": "CHARGING"}).status_code == 409
    assert client.post(path, headers=driver, json={"stop_id": "CHARGE-1", "idempotency_key": "charge-1",
                                                   "kind": "CHARGING"}).status_code == 409
    assert client.post("/api/v1/driver/trips/TRIP-1/complete", headers=driver,
                       json={"idempotency_key": "finish-1"}).status_code == 409
    end_path = path + "/WAIT-1/complete"
    end = {"idempotency_key": "end-wait-1"}
    closed = client.post(end_path, headers=driver, json=end)
    assert closed.status_code == 200 and closed.json()["active_stop"] is None
    assert client.post(end_path, headers=driver, json=end).json() == closed.json()
    assert client.post(end_path, headers=driver, json={"idempotency_key": "different"}).status_code == 409
    charge = client.post(path, headers=driver, json={"stop_id": "CHARGE-1", "idempotency_key": "charge-1",
                                                    "kind": "CHARGING"})
    assert charge.status_code == 200 and charge.json()["active_stop"]["source"] == "DRIVER_REPORTED"
    assert client.post(path + "/CHARGE-1/complete", headers=driver,
                       json={"idempotency_key": "end-charge-1"}).status_code == 200
    complete = client.post("/api/v1/driver/trips/TRIP-1/complete", headers=driver,
                           json={"idempotency_key": "finish-1"})
    assert complete.status_code == 200 and complete.json()["status"] == "COMPLETED"
    assert len(complete.json()["stops"]) == 2
    assert client.post(path, headers=driver, json={"stop_id": "LATE", "idempotency_key": "late",
                                                   "kind": "WAITING"}).status_code == 409
    detail = client.get(f"/api/v1/vehicles/{vehicle}/trips/TRIP-1", headers=OP).json()
    assert len(detail["stops"]) == 2
    assert client.get(f"/api/v1/vehicles/{vehicle}/twin", headers=OP).json()["state"] != "CHARGING"


def test_driver_stops_require_owned_trip_and_valid_kind(tmp_path):
    client = TestClient(create_app(str(tmp_path / "scope.db"), OP["X-Operator-Key"], "vehicle-secret-123"))
    client.post("/api/v1/vehicles", headers=OP, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    })
    token = client.post("/api/v1/vehicles/TRICKEE-DEMO-1/driver-credentials", headers=OP).json()["token"]
    driver = {"X-Driver-Key": token}
    path = "/api/v1/driver/trips/OTHER/stops"
    assert client.post(path, headers=driver, json={"stop_id": "S1", "idempotency_key": "k1",
                                                   "kind": "WAITING"}).status_code == 404
    assert client.post(path, headers=OP, json={"stop_id": "S1", "idempotency_key": "k1",
                                               "kind": "WAITING"}).status_code == 401
    assert client.post(path, headers=driver, json={"stop_id": "S1", "idempotency_key": "k1",
                                                   "kind": "PARKED"}).status_code == 422


def test_charging_stop_requires_car_api_evidence_for_corroboration(tmp_path):
    client = TestClient(create_app(str(tmp_path / "reconcile.db"), OP["X-Operator-Key"], "vehicle-secret-123"))
    vehicle = "TRICKEE-DEMO-1"
    registered = client.post("/api/v1/vehicles", headers=OP, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).json()
    token = client.post(f"/api/v1/vehicles/{vehicle}/driver-credentials", headers=OP).json()["token"]
    driver = {"X-Driver-Key": token}
    vehicle_key = {"X-Vehicle-Key": registered["ingest_key"]}
    assert client.post("/api/v1/driver/trips", headers=driver,
                       json={"trip_id": "TRIP-1", "idempotency_key": "start-1"}).status_code == 200
    path = "/api/v1/driver/trips/TRIP-1/stops"
    assert client.post(path, headers=driver, json={
        "stop_id": "CHARGE-1", "idempotency_key": "charge-1", "kind": "CHARGING",
    }).status_code == 200

    def charging_frame(sequence, source):
        return scenario_frame("SCENARIO_001", sequence).model_copy(update={
            "source": source, "charging": True, "speed_kmh": 0,
            "observed_at": datetime.now(timezone.utc), "trip_id": "TRIP-1",
        }).model_dump(mode="json")

    simulator = charging_frame(1, "SIMULATOR")
    assert client.post("/api/v1/telemetry/batches", headers=vehicle_key,
                       json={"frames": [simulator]}).status_code == 200
    url = f"/api/v1/vehicles/{vehicle}/trips/TRIP-1"
    reported = client.get(url, headers=OP).json()["stops"][0]
    assert reported["reconciliation"] == "UNVERIFIED"
    car_signal = charging_frame(2, "CAR_API")
    assert client.post("/api/v1/telemetry/batches", headers=vehicle_key,
                       json={"frames": [car_signal]}).status_code == 200
    corroborated = client.get(url, headers=OP).json()["stops"][0]
    assert corroborated["reconciliation"] == "CAR_API_CORROBORATED"
    assert corroborated["source"] == "DRIVER_REPORTED"
    assert client.post(path + "/CHARGE-1/complete", headers=driver,
                       json={"idempotency_key": "end-charge-1"}).status_code == 200
    assert client.post(path, headers=driver, json={
        "stop_id": "CHARGE-2", "idempotency_key": "charge-2", "kind": "CHARGING",
    }).status_code == 200
    later = client.get(url, headers=OP).json()["stops"][1]
    assert later["reconciliation"] == "UNVERIFIED"
