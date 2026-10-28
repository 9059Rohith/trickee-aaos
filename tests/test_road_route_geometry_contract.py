"""Reject contradictory OSRM route geometry before it reaches driver guidance."""

from copy import deepcopy

import httpx
import pytest
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.providers import RoadProviderBadResponse, RoadRouter


ORIGIN = (12.9716, 77.5946)
DESTINATION = (12.9816, 77.6046)
START = [ORIGIN[1], ORIGIN[0]]
MIDDLE = [77.5996, 12.9766]
END = [DESTINATION[1], DESTINATION[0]]


def payload() -> dict:
    return {"code": "Ok", "routes": [{
        "distance": 1800, "duration": 180,
        "geometry": {"type": "LineString", "coordinates": [START, MIDDLE, END]},
        "legs": [{"steps": [
            {"name": "First road", "distance": 800, "maneuver": {
                "type": "turn", "modifier": "right", "location": MIDDLE}},
        ]}],
    }]}


def router(response: dict) -> RoadRouter:
    return RoadRouter("https://router.example.test", httpx.MockTransport(
        lambda _: httpx.Response(200, json=response)))


@pytest.mark.parametrize("index,coordinate", [(0, [77.7, 13.0]), (2, [77.7, 13.0])])
def test_rejects_route_whose_endpoints_do_not_match_request(index, coordinate):
    response = payload()
    response["routes"][0]["geometry"]["coordinates"][index] = coordinate
    with pytest.raises(RoadProviderBadResponse):
        router(response).route(ORIGIN, DESTINATION)


@pytest.mark.parametrize("metres", [100, 100_000])
def test_rejects_distance_contradicting_geometry(metres):
    response = payload()
    response["routes"][0]["distance"] = metres
    response["routes"][0]["duration"] = max(180, metres / 10)
    with pytest.raises(RoadProviderBadResponse):
        router(response).route(ORIGIN, DESTINATION)


def test_accepts_maneuver_projected_onto_segment_between_shape_vertices():
    response = payload()
    response["routes"][0]["geometry"]["coordinates"] = [START, END]
    result = router(response).route(ORIGIN, DESTINATION)
    assert result["quality"] == "PROVIDER"
    assert result["routes"][0]["steps"][0]["location"]["latitude"] == MIDDLE[1]


def test_rejects_maneuvers_out_of_route_order():
    response = payload()
    response["routes"][0]["legs"][0]["steps"] = [
        {"name": "Late", "distance": 100, "maneuver": {
            "type": "turn", "modifier": "left", "location": END}},
        {"name": "Early", "distance": 100, "maneuver": {
            "type": "turn", "modifier": "right", "location": MIDDLE}},
    ]
    with pytest.raises(RoadProviderBadResponse):
        router(response).route(ORIGIN, DESTINATION)


def test_rejects_boolean_metrics_even_when_python_can_coerce_them():
    response = deepcopy(payload())
    response["routes"][0]["distance"] = True
    with pytest.raises(RoadProviderBadResponse):
        router(response).route(ORIGIN, DESTINATION)


def test_rejects_provider_speed_outside_vehicle_route_model():
    response = payload()
    response["routes"][0]["duration"] = 1
    with pytest.raises(RoadProviderBadResponse):
        router(response).route(ORIGIN, DESTINATION)


def test_rejects_unsplit_antimeridian_linestring():
    response = payload()
    response["routes"][0]["geometry"]["coordinates"] = [[179.99, 0.0], [-179.99, 0.0]]
    response["routes"][0]["distance"] = 2500
    response["routes"][0]["duration"] = 300
    response["routes"][0]["legs"] = []
    with pytest.raises(RoadProviderBadResponse):
        router(response).route((0.0, 179.99), (0.0, -179.99))


def test_route_api_surfaces_bad_provider_as_502_and_valid_midpoint_as_provider(tmp_path):
    app = create_app(str(tmp_path / "route.db"), "test-operator-key-123", "test-vehicle-key-123")
    client = TestClient(app)
    request = {"origin": {"latitude": ORIGIN[0], "longitude": ORIGIN[1]},
               "destination": {"latitude": DESTINATION[0], "longitude": DESTINATION[1]}}
    headers = {"X-Operator-Key": "test-operator-key-123"}
    invalid = payload()
    invalid["routes"][0]["geometry"]["coordinates"][0] = [77.7, 13.0]
    app.state.road_router = router(invalid)
    assert client.post("/api/v1/routes/road", json=request, headers=headers).status_code == 502
    valid = payload()
    valid["routes"][0]["geometry"]["coordinates"] = [START, END]
    app.state.road_router = router(valid)
    result = client.post("/api/v1/routes/road", json=request, headers=headers)
    assert result.status_code == 200
    assert result.json()["quality"] == "PROVIDER"
