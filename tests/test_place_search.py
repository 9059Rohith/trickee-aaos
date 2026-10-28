"""A configured geocoder supplies validated places; no public service is implicit."""

from datetime import datetime, timedelta, timezone
from email.utils import format_datetime

import httpx
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.providers import Geocoder, PlaceProviderBadResponse, PlaceProviderRateLimited, PlaceProviderUnavailable


KEY = {"X-Operator-Key": "test-operator-key-123"}


def test_geocoder_bounds_and_public_service_rejection():
    for url in ("http://places.example", "https://nominatim.openstreetmap.org",
                "https://nominatim.openstreetmap.org.", "https://user:pass@places.example"):
        try:
            Geocoder(url)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted forbidden geocoder URL: {url}")

    seen = []
    def answer(request):
        seen.append(request)
        if request.url.path == "/search":
            assert request.url.params["q"] == "Bengaluru"
            assert request.url.params["limit"] == "5"
            return httpx.Response(200, json=[{"place_id": 12, "display_name": "Bengaluru, Karnataka, India",
                "lat": "12.9716", "lon": "77.5946", "type": "city", "osm_type": "way",
                "osm_id": 90394480, "class": "place"}])
        return httpx.Response(200, json={"place_id": 12, "display_name": "Bengaluru, Karnataka, India",
            "lat": "12.9716", "lon": "77.5946", "type": "city"})
    geocoder = Geocoder("https://places.example", transport=httpx.MockTransport(answer))
    searched = geocoder.search("Bengaluru")[0]
    assert searched["latitude"] == 12.9716
    assert searched["osm_ref"] == {"osm_type": "W", "osm_id": "90394480", "category": "place"}
    reversed_place = geocoder.reverse(12.9716, 77.5946)
    assert reversed_place["source"] == "PROVIDER"
    assert reversed_place["osm_ref"] is None
    assert len(seen) == 2
    assert seen[0].headers["user-agent"].startswith("TrickeeVehicleOS/")


def test_geocoder_bad_response_and_outage():
    def bad(_request):
        return httpx.Response(200, json=[{"place_id": 1, "display_name": "Bad", "lat": "91", "lon": "20"}])
    geocoder = Geocoder("https://places.example", transport=httpx.MockTransport(bad))
    try:
        geocoder.search("Bad place")
    except PlaceProviderBadResponse:
        pass
    else:
        raise AssertionError("accepted out-of-range latitude")
    malformed_osm = Geocoder("https://places.example", transport=httpx.MockTransport(lambda _:
        httpx.Response(200, json=[{"place_id": 1, "display_name": "Bad reference", "lat": "12", "lon": "77",
                                  "osm_type": "way", "osm_id": True, "class": "place"}])))
    try:
        malformed_osm.search("Bad reference")
    except PlaceProviderBadResponse:
        pass
    else:
        raise AssertionError("accepted malformed OSM object reference")
    down = Geocoder("https://places.example", transport=httpx.MockTransport(lambda _: httpx.Response(503)))
    try:
        down.search("Unavailable")
    except PlaceProviderUnavailable:
        pass
    else:
        raise AssertionError("accepted provider outage")


def test_place_api_requires_auth_and_config(tmp_path):
    app = create_app(str(tmp_path / "cloud.db"), KEY["X-Operator-Key"], "test-vehicle-key-123")
    client = TestClient(app)
    assert client.get("/api/v1/places/search?q=Bengaluru").status_code == 401
    assert client.get("/api/v1/places/search?q=Bengaluru", headers=KEY).status_code == 503
    assert client.get("/api/v1/places/search?q=ab", headers=KEY).status_code == 422
    app.state.geocoder = Geocoder("https://places.example", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json=[{"place_id": 12, "display_name": "Bengaluru",
            "lat": "12.9716", "lon": "77.5946"}])))
    result = client.get("/api/v1/places/search?q=Bengaluru", headers=KEY)
    assert result.status_code == 200
    assert result.json()[0]["source"] == "PROVIDER"


def test_place_provider_quota_cools_down_and_recovers(tmp_path):
    now = [100.0]
    calls = []
    def answer(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": "600"})
        return httpx.Response(200, json=[{"place_id": 12, "display_name": "Bengaluru",
                                          "lat": "12.9716", "lon": "77.5946"}])
    geocoder = Geocoder("https://places.example", transport=httpx.MockTransport(answer),
                        clock=lambda: now[0])
    app = create_app(str(tmp_path / "cloud.db"), KEY["X-Operator-Key"], "test-vehicle-key-123")
    app.state.geocoder = geocoder
    client = TestClient(app)
    first = client.get("/api/v1/places/search?q=Bengaluru", headers=KEY)
    assert first.status_code == 503 and first.headers["Retry-After"] == "300"
    assert "quota reached" in first.json()["detail"]
    blocked = client.get("/api/v1/places/reverse?latitude=12.97&longitude=77.59", headers=KEY)
    assert blocked.status_code == 503 and blocked.headers["Retry-After"] == "300"
    assert len(calls) == 1, "cooldown sent another request to a rate-limited provider"
    now[0] = 401.0
    recovered = client.get("/api/v1/places/search?q=Bengaluru", headers=KEY)
    assert recovered.status_code == 200 and len(calls) == 2
    assert Geocoder._retry_after("not a date") == 30
    later = format_datetime(datetime.now(timezone.utc) + timedelta(seconds=120), usegmt=True)
    assert 110 <= Geocoder._retry_after(later) <= 120
