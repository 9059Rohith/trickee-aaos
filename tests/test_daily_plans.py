"""Operator-supervised daily plans require explicit coordinates and preserve evidence."""

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile


KEY = {"X-Operator-Key": "test-operator-key-123"}
BASE = "/api/v1/vehicles/TRICKEE-DEMO-1/daily-plans"
PLAN = {
    "plan_id": "DAY-1", "idempotency_key": "draft-1",
    "service_date": "2026-10-07", "timezone": "Asia/Kolkata",
    "starting_soc_pct": 80,
    "origin": {"latitude": 12.9716, "longitude": 77.5946},
    "stops": [
        {"label": "Office", "requested_arrival_local": "09:00", "coordinates": {"latitude": 12.98, "longitude": 77.60}},
        {"label": "Client", "requested_arrival_local": "12:30", "coordinates": {"latitude": 13.00, "longitude": 77.62}},
    ],
}


def client(tmp_path):
    app = create_app(str(tmp_path / "plans.db"), KEY["X-Operator-Key"], "test-vehicle-key-123")
    result = TestClient(app)
    assert result.post("/api/v1/vehicles", headers=KEY, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    return result


def test_draft_confirmation_retry_and_degraded_route(tmp_path):
    c = client(tmp_path)
    assert c.post(BASE, json=PLAN).status_code == 401
    created = c.post(BASE, headers=KEY, json=PLAN)
    assert created.status_code == 200, created.text
    assert created.json()["status"] == "DRAFT"
    assert created.json()["starting_soc_source"] == "MANUAL_DASHBOARD"
    assert c.post(BASE, headers=KEY, json=PLAN).json() == created.json()
    assert c.post(BASE, headers=KEY, json={**PLAN, "starting_soc_pct": 79}).status_code == 409
    assert len(c.get(BASE, headers=KEY).json()) == 1
    url = BASE + "/DAY-1/confirm"
    confirmed = c.post(url, headers=KEY, json={"idempotency_key": "confirm-1"})
    assert confirmed.status_code == 200, confirmed.text
    body = confirmed.json()
    assert body["status"] == "CONFIRMED"
    assert len(body["result"]["legs"]) == 2
    assert all(leg["route_source"] == "UNAVAILABLE" and leg["arrival_soc_pct"] is None for leg in body["result"]["legs"])
    assert c.post(url, headers=KEY, json={"idempotency_key": "confirm-1"}).json() == body
    assert c.post(url, headers=KEY, json={"idempotency_key": "confirm-other"}).status_code == 409
    assert c.get(BASE + "/DAY-1", headers=KEY).json() == body
    assert c.get(BASE + "/missing", headers=KEY).status_code == 404
    actions = c.app.state.store.db.execute("SELECT action FROM audit WHERE action LIKE 'daily_plan_%'").fetchall()
    assert [row[0] for row in actions] == ["daily_plan_drafted", "daily_plan_confirmed"]


def test_osm_reference_persists_without_replacing_confirmed_coordinates(tmp_path):
    c = client(tmp_path)
    origin_ref = {"osm_type": "N", "osm_id": "101", "category": "amenity"}
    stop_ref = {"osm_type": "W", "osm_id": "202", "category": "office"}
    content = {**PLAN, "origin_osm_ref": origin_ref,
               "stops": [{**PLAN["stops"][0], "osm_ref": stop_ref}, PLAN["stops"][1]]}
    created = c.post(BASE, headers=KEY, json=content)
    assert created.status_code == 200, created.text
    assert created.json()["origin_osm_ref"] == origin_ref
    assert created.json()["stops"][0]["osm_ref"] == stop_ref
    confirmed = c.post(BASE + "/DAY-1/confirm", headers=KEY,
                       json={"idempotency_key": "confirm-osm"})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["origin"] == PLAN["origin"]
    assert confirmed.json()["origin_osm_ref"] == origin_ref
    assert confirmed.json()["stops"][0]["osm_ref"] == stop_ref
    invalid = {**content, "plan_id": "DAY-2", "idempotency_key": "invalid-osm",
               "origin_osm_ref": {"osm_type": "W", "osm_id": "0", "category": "place"}}
    assert c.post(BASE, headers=KEY, json=invalid).status_code == 422


def test_provider_routes_predict_soc_sequentially(tmp_path):
    c = client(tmp_path)
    class Router:
        def route(self, origin, destination):
            return {"routes": [{"route": {
                "distance_km": 10, "baseline_eta_min": 20, "average_speed_kmh": 30,
                "traffic_factor": 1, "source": "PROVIDER",
            }}]}
    c.app.state.road_router = Router()
    assert c.post(BASE, headers=KEY, json=PLAN).status_code == 200
    body = c.post(BASE + "/DAY-1/confirm", headers=KEY, json={"idempotency_key": "confirm-1"}).json()
    first, second = body["result"]["legs"]
    assert first["route_source"] == "PROVIDER"
    assert first["distance_km"] == 10
    assert first["planned_departure_at"].startswith("2026-10-07T08:40")
    assert first["arrival_soc_pct"] < 80
    assert second["starting_soc_pct"] == first["arrival_soc_pct"]
    assert second["arrival_soc_pct"] < first["arrival_soc_pct"]
    assert body["result"]["quality"] == "ESTIMATED"


def test_route_failure_preserves_unknown_soc_and_schedule_conflict(tmp_path):
    c = client(tmp_path)
    class Router:
        calls = 0
        def route(self, origin, destination):
            self.calls += 1
            if self.calls == 1:
                raise ValueError("no road path")
            return {"routes": [{"route": {
                "distance_km": 10, "baseline_eta_min": 60, "average_speed_kmh": 10,
                "traffic_factor": 1, "source": "PROVIDER",
            }}]}
    c.app.state.road_router = Router()
    body = {**PLAN, "stops": [{**PLAN["stops"][0], "requested_arrival_local": "09:00"},
                               {**PLAN["stops"][1], "requested_arrival_local": "09:30"}]}
    assert c.post(BASE, headers=KEY, json=body).status_code == 200
    legs = c.post(BASE + "/DAY-1/confirm", headers=KEY, json={"idempotency_key": "confirm-1"}).json()["result"]["legs"]
    assert legs[0]["distance_km"] is None and legs[0]["arrival_soc_pct"] is None
    assert legs[1]["distance_km"] == 10 and legs[1]["starting_soc_pct"] is None
    assert legs[1]["arrival_soc_pct"] is None and legs[1]["schedule_conflict"] is True


def test_validation_and_assignment(tmp_path):
    c = client(tmp_path)
    assert c.post(BASE, headers=KEY, json={**PLAN, "timezone": "Mars/Base"}).status_code == 422
    for service_date, local_time in [("2026-03-08", "02:30"), ("2026-11-01", "01:30")]:
        assert c.post(BASE, headers=KEY, json={**PLAN, "service_date": service_date, "timezone": "America/New_York",
            "stops": [{**PLAN["stops"][0], "requested_arrival_local": local_time}]}).status_code == 422
    assert c.post(BASE, headers=KEY, json={**PLAN, "stops": list(reversed(PLAN["stops"]))}).status_code == 422
    assert c.post(BASE, headers=KEY, json={**PLAN, "stops": [{**PLAN["stops"][0], "coordinates": None}]}).status_code == 422
    assert c.post(BASE, headers=KEY, json={**PLAN, "origin": PLAN["stops"][0]["coordinates"]}).status_code == 422
    assert c.post(BASE, headers=KEY, json={**PLAN, "plan_id": "DAY-2", "idempotency_key": "draft-2", "service_date": "2026-10-08"}).status_code == 200
    assert c.post(BASE, headers=KEY, json={**PLAN, "plan_id": "DAY-3", "idempotency_key": "draft-3", "service_date": "2026-10-08"}).status_code == 409


def test_draft_can_be_edited_with_revision_and_confirmed_plan_is_locked(tmp_path):
    c = client(tmp_path)
    created = c.post(BASE, headers=KEY, json=PLAN).json()
    assert created["revision"] == 1
    edited_body = {key: PLAN[key] for key in ("service_date", "timezone", "starting_soc_pct", "origin", "stops")}
    edited_body["stops"] = [{**PLAN["stops"][0], "label": "New office"}]
    edited_body.update({"expected_revision": 1, "idempotency_key": "edit-1"})
    url = BASE + "/DAY-1"
    edited = c.put(url, headers=KEY, json=edited_body)
    assert edited.status_code == 200, edited.text
    assert edited.json()["revision"] == 2
    assert edited.json()["stops"][0]["label"] == "New office"
    assert c.put(url, headers=KEY, json=edited_body).json() == edited.json()
    assert c.put(url, headers=KEY, json={**edited_body, "starting_soc_pct": 70}).status_code == 409
    assert c.put(url, headers=KEY, json={**edited_body, "idempotency_key": "edit-2"}).status_code == 409
    next_edit = {**edited_body, "expected_revision": 2, "idempotency_key": "edit-2", "starting_soc_pct": 75}
    assert c.put(url, headers=KEY, json=next_edit).json()["revision"] == 3
    assert c.put(url, headers=KEY, json=edited_body).json() == edited.json()
    assert c.put(url, headers=KEY, json={**next_edit, "idempotency_key": "edit-1", "expected_revision": 3}).status_code == 409
    assert c.post(url + "/confirm", headers=KEY, json={"idempotency_key": "confirm-1"}).status_code == 200
    assert c.put(url, headers=KEY, json={**edited_body, "expected_revision": 3, "idempotency_key": "edit-3"}).status_code == 409
