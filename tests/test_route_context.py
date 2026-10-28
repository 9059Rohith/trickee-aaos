"""Live route context is bounded, attributed and never invented on failure."""

from datetime import datetime, timedelta, timezone

import httpx
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.route_context import RouteContext
from trickee.simulator import demo_profile


KEY = {"X-Operator-Key": "test-operator-key-123"}
POINTS = {"origin": {"latitude": 12.9716, "longitude": 77.5946},
          "destination": {"latitude": 12.7409, "longitude": 77.8253}}


def test_context_is_authenticated_and_unavailable_without_keys(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_OPENWEATHER_API_KEY", raising=False)
    monkeypatch.delenv("TRICKEE_ELEVATION_URL", raising=False)
    client = TestClient(create_app(str(tmp_path / "context.db"), KEY["X-Operator-Key"], "test-vehicle-key-123"))
    assert client.post("/api/v1/routes/context", json=POINTS).status_code == 401
    result = client.post("/api/v1/routes/context", json=POINTS, headers=KEY)
    assert result.status_code == 200
    assert result.json() == {
        "weather": {"quality": "UNAVAILABLE", "reason": "NOT_CONFIGURED"},
        "elevation": {"quality": "UNAVAILABLE", "reason": "NOT_CONFIGURED"},
        "traffic": {"quality": "UNAVAILABLE", "live": False},
    }


def test_live_context_validated_and_cached(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_OPENWEATHER_API_KEY", "weather-test-key")
    monkeypatch.setenv("TRICKEE_ELEVATION_URL", "https://elevation.example.test")
    calls = []

    def answer(request):
        calls.append(request)
        if request.url.host == "api.openweathermap.org":
            assert request.url.params["units"] == "metric"
            assert request.url.params["appid"] == "weather-test-key"
            return httpx.Response(200, json={
                "dt": int(datetime.now(timezone.utc).timestamp()),
                "main": {"temp": 32.5, "humidity": 68},
                "wind": {"speed": 3.5}, "rain": {"1h": 0.7},
            })
        assert request.url.host == "elevation.example.test"
        assert request.url.params["origin_lat"] == "12.9716"
        assert request.headers["authorization"] == "Bearer elevation-test-token"
        return httpx.Response(200, json={
            "origin": {"elevation_m": 920.1, "latitude": 12.9716, "longitude": 77.5946},
            "destination": {"elevation_m": 830.1, "latitude": 12.7409, "longitude": 77.8253},
        })

    app = create_app(str(tmp_path / "context.db"), KEY["X-Operator-Key"], "test-vehicle-key-123")
    app.state.route_context = RouteContext("weather-test-key", "https://elevation.example.test",
        elevation_token="elevation-test-token", elevation_cache_seconds=600, transport=httpx.MockTransport(answer))
    client = TestClient(app)
    first = client.post("/api/v1/routes/context", json=POINTS, headers=KEY)
    second = client.post("/api/v1/routes/context", json=POINTS, headers=KEY)
    assert first.status_code == 200 and first.json() == second.json()
    assert len(calls) == 2
    assert first.json()["weather"]["temperature_c"] == 32.5
    assert first.json()["weather"]["rain_mm_1h"] == 0.7
    assert first.json()["weather"]["source"] == "OPENWEATHER_CURRENT"
    assert first.json()["elevation"]["elevation_delta_m"] == -90.0
    assert first.json()["elevation"]["source"] == "CONFIGURED_ELEVATION"
    assert first.json()["traffic"]["live"] is False


def test_provider_errors_and_bad_data_do_not_create_fake_measurements():
    old = int((datetime.now(timezone.utc) - timedelta(hours=2)).timestamp())
    def answer(request):
        if request.url.host == "api.openweathermap.org":
            return httpx.Response(200, json={"dt": old, "main": {"temp": 25}})
        return httpx.Response(503)
    context = RouteContext("weather-key", "https://elevation.example.test", transport=httpx.MockTransport(answer))
    result = context.for_route(POINTS["origin"], POINTS["destination"])
    assert result["weather"] == {"quality": "UNAVAILABLE", "reason": "STALE_DATA"}
    assert result["elevation"] == {"quality": "UNAVAILABLE", "reason": "UPSTREAM_UNAVAILABLE"}
    assert "temperature_c" not in result["weather"]
    assert "elevation_delta_m" not in result["elevation"]


def test_context_daily_cap_blocks_extra_calls():
    calls = []
    def answer(request):
        calls.append(request)
        if request.url.host == "api.openweathermap.org":
            return httpx.Response(200, json={"dt": int(datetime.now(timezone.utc).timestamp()), "main": {"temp": 28}})
        return httpx.Response(200, json={
            "origin": {"elevation_m": 100, "latitude": 12.9716, "longitude": 77.5946},
            "destination": {"elevation_m": 110, "latitude": 12.7409, "longitude": 77.8253},
        })
    context = RouteContext("weather-key", "https://elevation.example.test", daily_limit=1,
                           transport=httpx.MockTransport(answer))
    context.for_route(POINTS["origin"], POINTS["destination"])
    changed = {"latitude": 12.9, "longitude": 77.6}
    result = context.for_route(changed, POINTS["destination"])
    assert len(calls) == 2
    assert result["weather"]["reason"] == "LOCAL_QUOTA_EXHAUSTED"
    assert result["elevation"]["reason"] == "LOCAL_QUOTA_EXHAUSTED"


def test_driver_context_requires_driver_credential(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_OPENWEATHER_API_KEY", raising=False)
    monkeypatch.delenv("TRICKEE_ELEVATION_URL", raising=False)
    client = TestClient(create_app(str(tmp_path / "driver-context.db"), KEY["X-Operator-Key"], "test-vehicle-key-123"))
    path = "/api/v1/driver/routes/context"
    assert client.post(path, json=POINTS, headers=KEY).status_code == 401
    assert client.post("/api/v1/vehicles", headers=KEY, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    token = client.post("/api/v1/vehicles/TRICKEE-DEMO-1/driver-credentials", headers=KEY).json()["token"]
    result = client.post(path, json=POINTS, headers={"X-Driver-Key": token})
    assert result.status_code == 200
    assert result.json()["traffic"]["live"] is False


def test_elevation_rejects_unrelated_coordinates_and_weather_missing_rain_is_unknown():
    def answer(request):
        if request.url.host == "api.openweathermap.org":
            return httpx.Response(200, json={"dt": int(datetime.now(timezone.utc).timestamp()),
                                             "main": {"temp": 28}})
        return httpx.Response(200, json={
            "origin": {"elevation_m": 100, "latitude": 12.9716, "longitude": 77.5946},
            "destination": {"elevation_m": 110, "latitude": 20, "longitude": 77.8253},
        })
    context = RouteContext("weather-key", "https://elevation.example.test", transport=httpx.MockTransport(answer))
    result = context.for_route(POINTS["origin"], POINTS["destination"])
    assert result["weather"]["rain_mm_1h"] is None
    assert result["elevation"] == {"quality": "UNAVAILABLE", "reason": "BAD_RESPONSE"}
