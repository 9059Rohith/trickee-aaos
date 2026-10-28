"""Road matching retains OSRM's uncertainty, splits and dropped GPS fixes."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.providers import RoadProviderBadResponse, RoadRouter
from trickee.simulator import demo_profile


def points():
    first = datetime(2026, 10, 6, 12, 0, tzinfo=timezone.utc)
    return [
        {"sequence": i + 1, "timestamp": (first + timedelta(seconds=i * 5)).isoformat(),
         "lat": 12.9716 + i * 0.001, "lng": 77.5946 + i * 0.001, "accuracy": 8}
        for i in range(3)
    ]


def matched(request):
    assert request.url.path.startswith("/match/v1/driving/77.5946,12.9716;")
    assert request.url.params["geometries"] == "geojson"
    assert request.url.params["timestamps"] == "1791288000;1791288005;1791288010"
    assert [float(value) for value in request.url.params["radiuses"].split(";")] == [8, 8, 8]
    return httpx.Response(200, json={"code": "Ok", "tracepoints": [
        {"location": [77.5946, 12.9716], "distance": 4, "matchings_index": 0},
        None,
        {"location": [77.5966, 12.9736], "distance": 5, "matchings_index": 1},
    ], "matchings": [
        {"confidence": 0.91, "geometry": {"type": "LineString", "coordinates": [[77.5946, 12.9716], [77.595, 12.972]]}},
        {"confidence": 0.7, "geometry": {"type": "LineString", "coordinates": [[77.596, 12.973], [77.5966, 12.9736]]}},
    ]})


def test_driver_matching_preserves_split_and_outlier(tmp_path):
    key = {"X-Operator-Key": "test-operator-key-123"}
    app = create_app(str(tmp_path / "matching.db"), key["X-Operator-Key"], "test-vehicle-key-123")
    client = TestClient(app)
    path = "/api/v1/driver/routes/match"
    assert client.post(path, json={"points": points()}).status_code == 401
    client.post("/api/v1/vehicles", headers=key, json={"profile": demo_profile().model_dump(), "driver_id": "DRIVER-1"})
    token = client.post("/api/v1/vehicles/TRICKEE-DEMO-1/driver-credentials", headers=key).json()["token"]
    driver = {"X-Driver-Key": token}
    assert client.post(path, headers=driver, json={"points": points()}).status_code == 503
    app.state.road_router = RoadRouter("https://router.example.test", httpx.MockTransport(matched))
    response = client.post(path, headers=driver, json={"points": points()})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["quality"] == "PROVIDER"
    assert body["tracepoints"][1] is None
    assert [segment["confidence"] for segment in body["segments"]] == [0.91, 0.7]
    assert client.post(path, headers=driver, json={"points": [points()[0]]}).status_code == 422
    bad = points()
    bad[1]["mocked"] = True
    assert client.post(path, headers=driver, json={"points": bad}).status_code == 422


def test_matching_rejects_invalid_provider_geometry_and_index():
    for changed in (
        lambda body: body["matchings"][0]["geometry"]["coordinates"].append([999, 12]),
        lambda body: body["tracepoints"][0].update(matchings_index=3),
    ):
        sample = matched(httpx.Request("GET", "https://router.example.test/match/v1/driving/77.5946,12.9716;77.5956,12.9726;77.5966,12.9736",
                                       params={"geometries": "geojson", "timestamps": "1791288000;1791288005;1791288010",
                                               "radiuses": "8;8;8"})).json()
        changed(sample)
        router = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _: httpx.Response(200, json=sample)))
        with pytest.raises(RoadProviderBadResponse):
            router.match_trace([(point["lat"], point["lng"], int(datetime.fromisoformat(point["timestamp"]).timestamp()), 8)
                                for point in points()])


def test_matching_no_match_and_outage_are_distinct():
    trace = [(point["lat"], point["lng"], int(datetime.fromisoformat(point["timestamp"]).timestamp()), 8)
             for point in points()]
    unmatched = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _:
        httpx.Response(400, json={"code": "NoMatch"}))).match_trace(trace)
    assert unmatched == {"quality": "UNMATCHED", "tracepoints": [None, None, None], "segments": []}
    unavailable = RoadRouter("https://router.example.test", httpx.MockTransport(lambda _:
        httpx.Response(503))).match_trace
    from trickee.providers import RoadProviderUnavailable
    with pytest.raises(RoadProviderUnavailable):
        unavailable(trace)
