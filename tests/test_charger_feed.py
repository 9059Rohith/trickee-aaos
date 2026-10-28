"""Contracted charger data is fresh, validated and never silently simulated."""

import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.providers import ChargerFeed, ChargerProviderBadResponse, ChargerProviderStale
from trickee.simulator import demo_profile, scenario_frame


OP = {"X-Operator-Key": "operator-secret-123"}
VEHICLE = {"X-Vehicle-Key": hmac.new(b"vehicle-secret-123", b"TRICKEE-DEMO-1", hashlib.sha256).hexdigest()}


def charger_data(observed_at=None):
    return {"observed_at": observed_at or datetime.now(timezone.utc).isoformat(), "chargers": [{
        "charger_id": "NETWORK-1", "name": "City rapid", "connector": "CCS2", "power_kw": 90,
        "distance_km": 8, "detour_min": 5, "queue_min": 2, "price_per_kwh": 18,
        "currency": "INR", "reliability": 0.96, "available": True,
    }]}


def test_feed_requires_https_fresh_timestamp_and_full_fields():
    with pytest.raises(ValueError):
        ChargerFeed("http://example.org/feed")
    captured = []
    def handler(request):
        captured.append(request)
        return httpx.Response(200, json=charger_data())
    feed = ChargerFeed("https://charger.example/feed", "private-secret", httpx.MockTransport(handler))
    result = feed.nearby(12.9716, 77.5946)
    assert result["quality"] == "PROVIDER"
    assert result["chargers"][0].source == "PROVIDER"
    assert result["chargers"][0].currency == "INR"
    assert captured[0].headers["authorization"] == "Bearer private-secret"
    assert "private-secret" not in str(captured[0].url)
    stale = ChargerFeed("https://charger.example/feed", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=charger_data((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()))))
    with pytest.raises(ChargerProviderStale):
        stale.nearby(12, 77)
    malformed = charger_data()
    del malformed["chargers"][0]["currency"]
    bad = ChargerFeed("https://charger.example/feed", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=malformed)))
    with pytest.raises(ChargerProviderBadResponse):
        bad.nearby(12, 77)


def test_plan_does_not_fall_back_to_mock_outside_demo(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    monkeypatch.delenv("TRICKEE_CHARGER_FEED_URL", raising=False)
    app = create_app(str(tmp_path / "charger.db"), OP["X-Operator-Key"], "vehicle-secret-123")
    client = TestClient(app)
    client.post("/api/v1/vehicles", headers=OP, json={"profile": demo_profile().model_dump()})
    frame = scenario_frame("SCENARIO_001", 1).model_dump(mode="json")
    client.post("/api/v1/telemetry/batches", headers=VEHICLE, json={"frames": [frame]})
    route = {"distance_km": 20, "baseline_eta_min": 30, "average_speed_kmh": 40}
    plan = client.post("/api/v1/vehicles/TRICKEE-DEMO-1/plan", headers=OP, json={"route": route}).json()
    assert plan["range"] is not None
    assert plan["chargers"] == []
    assert plan["charger_quality"] == "UNAVAILABLE"
    app.state.charger_feed = ChargerFeed("https://charger.example/feed", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=charger_data())))
    live = client.post("/api/v1/vehicles/TRICKEE-DEMO-1/plan", headers=OP, json={"route": route}).json()
    assert live["charger_quality"] == "PROVIDER"
    assert live["chargers"][0]["charger"]["source"] == "PROVIDER"
    assert live["chargers"][0]["charger"]["currency"] == "INR"
    stale = ChargerFeed("https://charger.example/feed", transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json=charger_data(
            (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()))))
    app.state.charger_feed = stale
    degraded = client.post("/api/v1/vehicles/TRICKEE-DEMO-1/plan", headers=OP, json={"route": route}).json()
    assert degraded["chargers"] == [] and degraded["charger_quality"] == "STALE"
    assert client.get("/api/v1/chargers/nearby?latitude=12.97&longitude=77.59").status_code == 401
    assert client.get("/api/v1/driver/chargers/nearby?latitude=12.97&longitude=77.59", headers=OP).status_code == 401
    assert client.get("/api/v1/chargers/nearby?latitude=12.97&longitude=77.59", headers=OP).json()["quality"] == "STALE"
    assert client.get("/api/v1/chargers/nearby?latitude=91&longitude=77.59", headers=OP).status_code == 422
