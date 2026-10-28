"""Order and charging previews abstain when evidence is missing or unsafe."""

import hashlib
import json
from datetime import datetime, timezone

import httpx
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.providers import ChargerFeed, RoadRouter
from trickee.simulator import demo_profile, scenario_frame


KEY = {"X-Operator-Key": "test-operator-key-123"}
ORDER = {"pickup": {"latitude": 12.98, "longitude": 77.60},
         "destination": {"latitude": 13.0, "longitude": 77.62}, "preparation_min": 20}


def router_response(request):
    if request.url.path.startswith("/table/v1/driving/"):
        assert request.url.params["annotations"] == "duration,distance"
        assert request.url.params["destinations"] == "1"
        return httpx.Response(200, json={"code": "Ok", "durations": [[600]], "distances": [[5000]]})
    assert request.url.path.startswith("/route/v1/driving/")
    return httpx.Response(200, json={"code": "Ok", "routes": [{
        "distance": 5000, "duration": 600,
        "geometry": {"type": "LineString", "coordinates": [[77.59, 12.97], [77.62, 13.0]]},
    }]})


def test_preview_requires_auth_and_abstains_without_router(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_OSRM_URL", raising=False)
    client = TestClient(create_app(str(tmp_path / "no-router.db"), KEY["X-Operator-Key"], "vehicle-secret-123"))
    path = "/api/v1/fleet/decisions/preview"
    assert client.post(path, json=ORDER).status_code == 401
    assert client.post(path, headers=KEY, json={**ORDER, "preparation_min": -1}).status_code == 422
    result = client.post(path, headers=KEY, json=ORDER).json()
    assert result["quality"] == "UNAVAILABLE"
    assert result["recommended_vehicle_id"] is None
    assert result["reason"] == "ROAD_ROUTER_NOT_CONFIGURED"
    assert result["automatic_assignment"] is False
    assert result["charger_quality"] == "NOT_QUERIED"


def test_preview_recommends_only_range_safe_driver_with_provenance(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    app = create_app(str(tmp_path / "decision.db"), KEY["X-Operator-Key"], "vehicle-secret-123")
    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(router_response))
    client = TestClient(app)
    registered = client.post("/api/v1/vehicles", headers=KEY, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).json()
    frame = scenario_frame("SCENARIO_001", 1).model_dump(mode="json")
    assert client.post("/api/v1/telemetry/batches", headers={"X-Vehicle-Key": registered["ingest_key"]},
                       json={"frames": [frame]}).status_code == 200
    result = client.post("/api/v1/fleet/decisions/preview", headers=KEY, json=ORDER).json()
    assert result["recommended_vehicle_id"] == "TRICKEE-DEMO-1"
    assert result["automatic_assignment"] is False
    assert result["quality"] == "SIMULATED"
    assert result["candidates"][0]["range_safe"] is True
    assert result["candidates"][0]["road_source"] == "PROVIDER"
    assert result["charging_decision"] == "NO_VERIFIED_CHARGING_OPPORTUNITY"
    assert result["traffic_quality"] == "UNAVAILABLE"
    app.state.charger_feed = ChargerFeed("https://charger.example.test", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={"observed_at": datetime.now(timezone.utc).isoformat(),
            "chargers": [{"charger_id": "PICKUP-1", "name": "Pickup charger", "connector": "CCS2",
                          "power_kw": 90, "distance_km": 0.3, "detour_min": 2, "queue_min": 0,
                          "price_per_kwh": 18, "currency": "INR", "reliability": 0.95,
                          "available": True}]})))
    verified = client.post("/api/v1/fleet/decisions/preview", headers=KEY, json=ORDER).json()
    assert verified["charger_quality"] == "PROVIDER"
    assert verified["charging_decision"] == "REVIEW_AT_PICKUP"
    low = {**frame, "sequence": 2, "soc_pct": 2}
    assert client.post("/api/v1/telemetry/batches", headers={"X-Vehicle-Key": registered["ingest_key"]},
                       json={"frames": [low]}).status_code == 200
    unsafe = client.post("/api/v1/fleet/decisions/preview", headers=KEY, json=ORDER).json()
    assert unsafe["recommended_vehicle_id"] is None
    assert unsafe["candidates"][0]["range_safe"] is False
    assert unsafe["reason"] == "NO_RANGE_SAFE_CANDIDATE"


def test_preview_accepts_zero_length_road_table_cell(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    app = create_app(str(tmp_path / "zero-table.db"), KEY["X-Operator-Key"], "vehicle-secret-123")

    def response(request):
        if request.url.path.startswith("/table/v1/driving/"):
            return httpx.Response(200, json={"code": "Ok", "durations": [[0]], "distances": [[0]]})
        return router_response(request)

    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(response))
    client = TestClient(app)
    registered = client.post("/api/v1/vehicles", headers=KEY, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).json()
    frame = scenario_frame("SCENARIO_001", 1).model_dump(mode="json")
    assert client.post("/api/v1/telemetry/batches", headers={"X-Vehicle-Key": registered["ingest_key"]},
                       json={"frames": [frame]}).status_code == 200
    result = client.post("/api/v1/fleet/decisions/preview", headers=KEY, json=ORDER).json()
    assert result["recommended_vehicle_id"] == "TRICKEE-DEMO-1"
    assert result["candidates"][0]["pickup_distance_km"] == 0
    assert result["candidates"][0]["pickup_eta_min"] == 0


def test_preview_respects_tenant_boundary(tmp_path, monkeypatch):
    keys = {"north": "north-decision-secret-123", "south": "south-decision-secret-123"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": tenant, "role": "viewer", "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for tenant, key in keys.items()
    ]))
    app = create_app(str(tmp_path / "tenants.db"), "legacy-secret-123", "vehicle-secret-123")
    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(router_response))
    client = TestClient(app)
    # Register through a store fixture because viewers intentionally cannot create vehicles.
    for tenant, vehicle_id in (("north", "NORTH-1"), ("south", "SOUTH-1")):
        app.state.store.register(demo_profile(vehicle_id), f"DRIVER-{tenant}", tenant)
        app.state.store.ingest([scenario_frame("SCENARIO_001", 1, vehicle_id=vehicle_id)])
    for tenant, key in keys.items():
        response = client.post("/api/v1/fleet/decisions/preview", headers={"X-Operator-Key": key}, json=ORDER)
        assert response.status_code == 200
        candidate_ids = [row["vehicle_id"] for row in response.json()["candidates"]]
        assert candidate_ids == [f"{tenant.upper()}-1"]


def test_preview_abstains_for_fleet_larger_than_candidate_limit(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    app = create_app(str(tmp_path / "large-fleet.db"), KEY["X-Operator-Key"], "vehicle-secret-123")
    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(
        lambda request: (_ for _ in ()).throw(AssertionError("router must not be called"))))
    for index in range(21):
        app.state.store.register(demo_profile(f"V-{index}"), f"D-{index}")
    result = TestClient(app).post("/api/v1/fleet/decisions/preview", headers=KEY, json=ORDER).json()
    assert result["reason"] == "FLEET_TOO_LARGE_FOR_PREVIEW"
    assert result["recommended_vehicle_id"] is None
    assert result["vehicles_truncated"] is True
