"""Road geometry is provider data and never silently becomes live traffic."""

import httpx
import pytest
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.providers import RoadRouter
from trickee.simulator import demo_profile


KEY = {"X-Operator-Key": "test-operator-key-123"}
POINTS = {"origin": {"latitude": 12.9716, "longitude": 77.5946},
          "destination": {"latitude": 12.7409, "longitude": 77.8253}}


def osrm_response(request):
    assert request.url.path == "/route/v1/driving/77.5946,12.9716;77.8253,12.7409"
    assert request.url.params["geometries"] == "geojson"
    assert request.url.params["alternatives"] == "true"
    assert request.url.params["steps"] == "true"
    assert request.url.params["overview"] == "full"
    return httpx.Response(200, json={"code": "Ok", "routes": [{
        "distance": 48000, "duration": 3600,
        "geometry": {"type": "LineString", "coordinates": [[77.5946, 12.9716], [77.7, 12.85], [77.8253, 12.7409]]},
        "legs": [{"steps": [{"name": "Hosur Road", "distance": 500.0,
                             "maneuver": {"type": "turn", "modifier": "right",
                                          "location": [77.7, 12.85]}}]}],
    }]})


def test_road_route_requires_operator_and_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_OSRM_URL", raising=False)
    client = TestClient(create_app(str(tmp_path / "none.db"), KEY["X-Operator-Key"], "test-vehicle-key-123"))
    assert client.post("/api/v1/routes/road", json=POINTS).status_code == 401
    response = client.post("/api/v1/routes/road", json=POINTS, headers=KEY)
    assert response.status_code == 503
    assert "not configured" in response.json()["detail"]


def test_road_route_geometry_metrics_and_provenance(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_OSRM_URL", "https://router.example.test")
    app = create_app(str(tmp_path / "route.db"), KEY["X-Operator-Key"], "test-vehicle-key-123")
    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(osrm_response))
    response = TestClient(app).post("/api/v1/routes/road", json=POINTS, headers=KEY)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quality"] == "PROVIDER"
    assert body["traffic"] == {"quality": "UNAVAILABLE", "live": False}
    assert body["routes"][0]["route"]["source"] == "PROVIDER"
    assert body["routes"][0]["route"]["distance_km"] == 48
    assert body["routes"][0]["route"]["baseline_eta_min"] == 60
    assert body["routes"][0]["geometry"]["coordinates"][1] == [77.7, 12.85]
    assert body["routes"][0]["steps"] == [{"type": "turn", "modifier": "right",
                                           "name": "Hosur Road", "distance_m": 500.0,
                                           "location": {"latitude": 12.85, "longitude": 77.7}}]


@pytest.mark.parametrize("location", [None, ["77.7", 12.85], [181, 12.85], [77.9, 13.2]])
def test_road_route_rejects_missing_invalid_or_off_route_maneuver_location(location):
    maneuver = {"type": "turn", "modifier": "right"}
    if location is not None:
        maneuver["location"] = location
    response = {"code": "Ok", "routes": [{"distance": 48000, "duration": 3600,
        "geometry": {"type": "LineString", "coordinates": [[77.5946, 12.9716], [77.7, 12.85], [77.8253, 12.7409]]},
        "legs": [{"steps": [{"name": "Hosur Road", "distance": 500.0, "maneuver": maneuver}]}]}]}
    router = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    from trickee.providers import RoadProviderBadResponse
    with pytest.raises(RoadProviderBadResponse):
        router.route((12.9716, 77.5946), (12.7409, 77.8253))


def test_road_route_rejects_invalid_points_and_provider_geometry(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_OSRM_URL", "https://router.example.test")
    app = create_app(str(tmp_path / "bad.db"), KEY["X-Operator-Key"], "test-vehicle-key-123")
    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _:
        httpx.Response(200, json={"code": "Ok", "routes": [{"distance": 48000, "duration": 3600,
            "geometry": {"type": "LineString", "coordinates": [[1000, 12], [77, 13]]}}]})))
    client = TestClient(app)
    assert client.post("/api/v1/routes/road", json={**POINTS, "origin": {"latitude": 91, "longitude": 77}}, headers=KEY).status_code == 422
    assert client.post("/api/v1/routes/road", json=POINTS, headers=KEY).status_code == 502


def test_road_route_rejects_invalid_provider_steps():
    response = {"code": "Ok", "routes": [{"distance": 48000, "duration": 3600,
        "geometry": {"type": "LineString", "coordinates": [[77.5946, 12.9716], [77.8253, 12.7409]]},
        "legs": [{"steps": [{"name": "Road", "distance": -5,
                              "maneuver": {"type": "turn", "modifier": "right",
                                           "location": [77.7, 12.85]}}]}]}]}
    router = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _: httpx.Response(200, json=response)))
    from trickee.providers import RoadProviderBadResponse
    with pytest.raises(RoadProviderBadResponse):
        router.route((12.9716, 77.5946), (12.7409, 77.8253))


def test_road_route_handles_provider_failure_and_requires_https(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="HTTPS"):
        RoadRouter("http://router.example.test")
    monkeypatch.setenv("TRICKEE_OSRM_URL", "https://router.example.test")
    app = create_app(str(tmp_path / "down.db"), KEY["X-Operator-Key"], "test-vehicle-key-123")
    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _:
        httpx.Response(503)))
    response = TestClient(app).post("/api/v1/routes/road", json=POINTS, headers=KEY)
    assert response.status_code == 503


def test_road_route_rejects_non_object_provider_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_OSRM_URL", "https://router.example.test")
    app = create_app(str(tmp_path / "shape.db"), KEY["X-Operator-Key"], "test-vehicle-key-123")
    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _:
        httpx.Response(200, json=["invalid"])))
    assert TestClient(app).post("/api/v1/routes/road", json=POINTS, headers=KEY).status_code == 502


def test_driver_route_preview_requires_credential_and_provider(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_OSRM_URL", raising=False)
    app = create_app(str(tmp_path / "driver-route.db"), KEY["X-Operator-Key"], "test-vehicle-key-123")
    client = TestClient(app)
    assert client.post("/api/v1/vehicles", headers=KEY, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    token = client.post("/api/v1/vehicles/TRICKEE-DEMO-1/driver-credentials", headers=KEY).json()["token"]
    driver = {"X-Driver-Key": token}
    path = "/api/v1/driver/routes/road"
    assert client.post(path, headers=KEY, json=POINTS).status_code == 401
    assert client.get("/api/v1/driver/map/config", headers=KEY).status_code == 401
    assert client.post(path, headers=driver, json=POINTS).status_code == 503
    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(osrm_response))
    response = client.post(path, headers=driver, json=POINTS)
    assert response.status_code == 200 and response.json()["quality"] == "PROVIDER"
    assert response.json()["traffic"]["live"] is False
    assert client.get("/api/v1/driver/map/config", headers=driver).status_code == 200
    assert client.post(path, headers=driver, json={**POINTS, "destination": {"latitude": 91, "longitude": 77}}).status_code == 422


def test_road_table_validates_distance_duration_and_unreachable_cells():
    def good(request):
        assert request.url.path.startswith("/table/v1/driving/")
        assert request.url.params["sources"] == "0;1"
        assert request.url.params["destinations"] == "2"
        assert request.url.params["annotations"] == "duration,distance"
        return httpx.Response(200, json={"code": "Ok", "durations": [[600], [None]],
                                         "distances": [[5000], [None]]})
    router = RoadRouter("https://router.example.test", httpx.MockTransport(good))
    routes = router.table_to_destination([(12.97, 77.59), (12.8, 77.8)], (12.98, 77.6))
    assert routes[0].distance_km == 5 and routes[0].source == "PROVIDER"
    assert routes[1] is None
    bad = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _:
        httpx.Response(200, json={"code": "Ok", "durations": [[0]], "distances": [[5000]]})))
    with pytest.raises(Exception, match="invalid road table"):
        bad.table_to_destination([(12.97, 77.59)], (12.98, 77.6))


def test_road_table_preserves_zero_and_null_cells_without_fabricated_distance():
    router = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _:
        httpx.Response(200, json={"code": "Ok", "durations": [[0], [None]],
                                  "distances": [[0], [None]]})))
    cells = router.table_metrics_to_destination([(12.97, 77.59), (12.8, 77.8)], (12.97, 77.59))
    assert cells[0].distance_km == cells[0].duration_min == 0
    assert cells[1] is None
    assert router.table_to_destination([(12.97, 77.59), (12.8, 77.8)], (12.97, 77.59)) == [None, None]
    with pytest.raises(ValueError, match="coordinates"):
        router.table_metrics_to_destination([(True, 77.59)], (12.97, 77.59))
    with pytest.raises(ValueError, match="coordinates"):
        router.table_metrics_to_destination([(12.97,)], (12.97, 77.59))
    tiny = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _:
        httpx.Response(200, json={"code": "Ok", "durations": [[5e-324]],
                                  "distances": [[5e-324]]})))
    with pytest.raises(Exception, match="invalid road table"):
        tiny.table_metrics_to_destination([(12.97, 77.59)], (12.98, 77.6))
