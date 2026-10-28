"""Contract checks for OCPI data that must not become invented charger facts."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.ocpi import OcpiBadResponse, OcpiClient, OcpiCollectionClient, OcpiUnavailable


def location(updated: datetime, *, location_id: str = "SITE-1", status: str = "AVAILABLE") -> dict:
    return {
        "country_code": "IN", "party_id": "TKE", "id": location_id,
        "name": "Trickee test hub", "coordinates": {"latitude": "12.9716", "longitude": "77.5946"},
        "last_updated": updated.isoformat(), "evses": [{
            "uid": "EVSE-1", "status": status, "last_updated": updated.isoformat(),
            "connectors": [{"id": "1", "standard": "IEC_62196_T2_COMBO",
                            "max_electric_power": 120000, "last_updated": updated.isoformat()}],
        }],
    }


def response(items: list[dict], link: str | None = None) -> httpx.Response:
    headers = {"X-Total-Count": "2", "X-Limit": "1"}
    if link:
        headers["Link"] = f'<{link}>; rel="next"'
    return httpx.Response(200, headers=headers, json={
        "status_code": 1000, "timestamp": datetime.now(timezone.utc).isoformat(), "data": items,
    })


def test_ocpi_fetches_bounded_pages_with_token_and_preserves_unknowns():
    now = datetime.now(timezone.utc)
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        assert request.headers["authorization"] == "Token provider-secret"
        assert request.url.path == "/ocpi/2.3.0/locations"
        if request.url.params.get("offset") == "1":
            return response([location(now, location_id="SITE-2", status="CHARGING")])
        return response([location(now)], "https://cpo.example/ocpi/2.3.0/locations?offset=1&limit=1")

    client = OcpiClient("https://cpo.example/ocpi/2.3.0/locations", "provider-secret",
                        transport=httpx.MockTransport(handler))
    stations = client.locations()
    assert len(seen) == 2
    assert [item["station_id"] for item in stations] == ["IN-TKE-SITE-1", "IN-TKE-SITE-2"]
    assert stations[0]["evses"][0]["availability"] == "AVAILABLE"
    assert stations[0]["evses"][0]["connectors"][0]["power_kw"] == 120
    assert stations[0]["evses"][0]["connectors"][0]["price_per_kwh"] is None
    assert stations[0]["evses"][0]["connectors"][0]["queue_min"] is None
    assert stations[1]["evses"][0]["availability"] == "CHARGING"
    assert stations[0]["source"] == "PROVIDER"


def test_ocpi_stale_status_is_not_called_available():
    old = datetime.now(timezone.utc) - timedelta(hours=2)
    client = OcpiClient("https://cpo.example/locations", "token", transport=httpx.MockTransport(
        lambda _: response([location(old)])))
    evse = client.locations()[0]["evses"][0]
    assert evse["reported_status"] == "AVAILABLE"
    assert evse["availability"] == "STALE"


def test_ocpi_accepts_utc_timestamp_without_designator_and_rejects_bad_status():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    payload = location(now)
    payload["last_updated"] = now.strftime("%Y-%m-%dT%H:%M:%S")
    payload["evses"][0]["last_updated"] = payload["last_updated"]
    payload["evses"][0]["connectors"][0]["last_updated"] = payload["last_updated"]
    client = OcpiClient("https://cpo.example/locations", "token", transport=httpx.MockTransport(
        lambda _: response([payload])))
    assert client.locations()[0]["evses"][0]["availability"] == "AVAILABLE"
    payload["evses"][0]["status"] = ["AVAILABLE"]
    with pytest.raises(OcpiBadResponse):
        client.locations()


@pytest.mark.parametrize("bad_link", [
    "http://cpo.example/locations?offset=1", "https://evil.example/locations?offset=1",
    "https://cpo.example/other?offset=1", "https://cpo.example/locations?offset=1#fragment",
])
def test_ocpi_rejects_untrusted_pagination_target(bad_link):
    client = OcpiClient("https://cpo.example/locations", "token", transport=httpx.MockTransport(
        lambda _: response([location(datetime.now(timezone.utc))], bad_link)))
    with pytest.raises(OcpiBadResponse):
        client.locations()


def test_ocpi_rejects_duplicate_or_malformed_location_and_bad_envelope():
    now = datetime.now(timezone.utc)
    for items in ([location(now), location(now)], [dict(location(now), coordinates={"latitude": "100", "longitude": "77"})]):
        client = OcpiClient("https://cpo.example/locations", "token", transport=httpx.MockTransport(
            lambda _: response(items)))
        with pytest.raises(OcpiBadResponse):
            client.locations()
    client = OcpiClient("https://cpo.example/locations", "token", transport=httpx.MockTransport(
        lambda _: httpx.Response(200, json={"status_code": 2001, "data": []})))
    with pytest.raises(OcpiBadResponse):
        client.locations()


def test_ocpi_network_failure_and_endpoint_validation():
    with pytest.raises(ValueError):
        OcpiClient("http://cpo.example/locations", "token")
    with pytest.raises(ValueError):
        OcpiClient("https://cpo.example/locations?secret=bad", "token")
    client = OcpiClient("https://cpo.example/locations", "token", transport=httpx.MockTransport(
        lambda _: httpx.Response(503)))
    with pytest.raises(OcpiUnavailable):
        client.locations()


def test_ocpi_nearby_filters_and_sorts_without_inventing_tariffs():
    now = datetime.now(timezone.utc)
    first = location(now, location_id="NEAR")
    second = location(now, location_id="FAR")
    second["coordinates"] = {"latitude": "13.50", "longitude": "78.50"}
    client = OcpiClient("https://cpo.example/locations", "token", transport=httpx.MockTransport(
        lambda _: response([second, first])))
    stations = client.nearby(12.9716, 77.5946, radius_km=10)
    assert [item["station_id"] for item in stations] == ["IN-TKE-NEAR"]
    assert stations[0]["distance_km"] == 0
    assert stations[0]["evses"][0]["connectors"][0]["price_per_kwh"] is None
    with pytest.raises(ValueError):
        client.nearby(91, 77)


def test_ocpi_operator_endpoint_requires_auth_and_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_OCPI_LOCATIONS_URL", raising=False)
    app = create_app(str(tmp_path / "ocpi.db"), "operator-secret-123", "vehicle-secret-123")
    client = TestClient(app)
    path = "/api/v1/chargers/ocpi/nearby?latitude=12.9716&longitude=77.5946"
    assert client.get(path).status_code == 401
    assert client.get(path, headers={"X-Operator-Key": "operator-secret-123"}).json() == {
        "quality": "UNAVAILABLE", "stations": [], "reason": "OCPI_NOT_CONFIGURED",
    }
    app.state.ocpi_client = OcpiClient("https://cpo.example/locations", "token",
                                      transport=httpx.MockTransport(lambda _: response([
                                          location(datetime.now(timezone.utc))])))
    result = client.get(path, headers={"X-Operator-Key": "operator-secret-123"})
    assert result.status_code == 200
    assert result.json()["quality"] == "PROVIDER"
    assert result.json()["stations"][0]["station_id"] == "IN-TKE-SITE-1"
    assert result.json()["stations"][0]["evses"][0]["connectors"][0]["price_per_kwh"] is None


def test_ocpi_generic_collection_fetches_paginated_sessions_without_cross_host_token():
    now = datetime.now(timezone.utc)
    seen = []

    def handler(request):
        seen.append(request)
        assert request.headers["authorization"] == "Token partner-secret"
        if request.url.params.get("offset") == "1":
            return response([{"id": "S-2", "last_updated": now.isoformat()}])
        return response([{"id": "S-1", "last_updated": now.isoformat()}],
                        "https://cpo.example/ocpi/2.3.0/sessions?offset=1")

    client = OcpiCollectionClient("https://cpo.example/ocpi/2.3.0/sessions", "partner-secret",
                                  transport=httpx.MockTransport(handler))
    assert [item["id"] for item in client.records()] == ["S-1", "S-2"]
    assert len(seen) == 2
    unsafe = OcpiCollectionClient("https://cpo.example/sessions", "partner-secret",
                                  transport=httpx.MockTransport(lambda _:
                                      response([], "https://other.example/sessions?offset=1")))
    with pytest.raises(OcpiBadResponse):
        unsafe.records()


def test_ocpi_rejects_oversized_body_while_streaming():
    class BoundedStream(httpx.SyncByteStream):
        def __iter__(self):
            yield b" " * (1024 * 1024)
            yield b" " * (1024 * 1024 + 1)
            raise AssertionError("client read beyond maximum OCPI page size")

    client = OcpiClient("https://cpo.example/locations", "token",
                        transport=httpx.MockTransport(lambda _:
                            httpx.Response(200, stream=BoundedStream())))
    with pytest.raises(OcpiBadResponse, match="too large"):
        client.locations()
