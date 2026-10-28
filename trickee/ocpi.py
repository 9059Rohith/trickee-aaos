"""Read-only OCPI location adapter with explicit freshness and unknown fields.

This module consumes a configured CPO Locations Sender endpoint. It does not
create a charging session, authorize a token, infer a tariff, or establish a
commercial OCPI roaming relationship.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from math import asin, cos, isfinite, radians, sin, sqrt
from urllib.parse import urlsplit

import httpx


class OcpiUnavailable(Exception):
    """The configured OCPI endpoint could not serve a page."""


class OcpiBadResponse(Exception):
    """An OCPI response cannot be safely shown as provider data."""


_STATUSES = {
    "AVAILABLE", "BLOCKED", "CHARGING", "INOPERATIVE", "OUTOFORDER",
    "PLANNED", "REMOVED", "RESERVED", "UNKNOWN",
}
_NEXT = re.compile(r'<([^<>]+)>\s*;\s*rel\s*=\s*"?next"?', re.IGNORECASE)


def _date(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("OCPI timestamp is missing")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    if parsed.utcoffset() != timedelta(0):
        raise ValueError("OCPI timestamp must be UTC")
    return parsed.astimezone(timezone.utc)


def _identifier(value: object, limit: int = 36) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= limit or not value.isascii():
        raise ValueError("invalid OCPI identifier")
    if any(ord(char) < 33 or ord(char) > 126 for char in value):
        raise ValueError("invalid OCPI identifier")
    return value.upper()


def _coordinates(value: object) -> dict[str, float]:
    if not isinstance(value, dict):
        raise ValueError("OCPI coordinates are missing")
    latitude = value.get("latitude")
    longitude = value.get("longitude")
    if not isinstance(latitude, str) or not isinstance(longitude, str):
        raise ValueError("OCPI coordinates must be strings")
    lat, lon = float(latitude), float(longitude)
    if not isfinite(lat) or not isfinite(lon) or not -90 <= lat <= 90 or not -180 <= lon <= 180:
        raise ValueError("invalid OCPI coordinates")
    return {"latitude": lat, "longitude": lon}


def _availability(status: str, observed: datetime, now: datetime,
                  max_status_age: timedelta) -> str:
    if observed < now - max_status_age or observed > now + timedelta(minutes=2):
        return "STALE"
    return status


def _station(raw: object, now: datetime, max_status_age: timedelta) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("OCPI location is not an object")
    country = _identifier(raw["country_code"], 2)
    party = _identifier(raw["party_id"], 3)
    location_id = _identifier(raw["id"])
    if len(country) != 2 or len(party) != 3:
        raise ValueError("invalid OCPI party identity")
    location_updated = _date(raw["last_updated"])
    evses = raw.get("evses", [])
    if not isinstance(evses, list) or len(evses) > 100:
        raise ValueError("invalid OCPI EVSE list")
    parsed_evses = []
    seen_evses = set()
    for raw_evse in evses:
        if not isinstance(raw_evse, dict):
            raise ValueError("invalid OCPI EVSE")
        uid = _identifier(raw_evse["uid"])
        if uid in seen_evses:
            raise ValueError("duplicate OCPI EVSE")
        seen_evses.add(uid)
        status = raw_evse["status"]
        if not isinstance(status, str) or status not in _STATUSES:
            raise ValueError("invalid OCPI status")
        updated = _date(raw_evse["last_updated"])
        connectors = raw_evse["connectors"]
        if not isinstance(connectors, list) or not 1 <= len(connectors) <= 20:
            raise ValueError("invalid OCPI connectors")
        parsed_connectors = []
        seen_connectors = set()
        for connector in connectors:
            if not isinstance(connector, dict):
                raise ValueError("invalid OCPI connector")
            connector_id = _identifier(connector["id"])
            if connector_id in seen_connectors:
                raise ValueError("duplicate OCPI connector")
            seen_connectors.add(connector_id)
            standard = _identifier(connector["standard"], 50)
            watts = connector.get("max_electric_power")
            if watts is not None and (isinstance(watts, bool) or not isinstance(watts, int)
                                      or not 0 < watts <= 1_000_000):
                raise ValueError("invalid OCPI connector power")
            parsed_connectors.append({
                "connector_id": connector_id, "standard": standard,
                "power_kw": watts / 1000 if watts is not None else None,
                "price_per_kwh": None, "queue_min": None,
                "last_updated": _date(connector["last_updated"]).isoformat(),
            })
        parsed_evses.append({
            "evse_uid": uid, "reported_status": status,
            "availability": _availability(status, updated, now, max_status_age),
            "status_updated_at": updated.isoformat(), "connectors": parsed_connectors,
        })
    name = raw.get("name")
    if name is not None and (not isinstance(name, str) or len(name) > 255):
        raise ValueError("invalid OCPI location name")
    return {
        "station_id": f"{country}-{party}-{location_id}", "name": name,
        "coordinates": _coordinates(raw["coordinates"]),
        "last_updated": location_updated.isoformat(),
        "source": "PROVIDER", "evses": parsed_evses,
    }


class OcpiClient:
    """Bounded, read-only OCPI Locations Sender pull client."""

    def __init__(self, locations_url: str, token: str,
                 transport: httpx.BaseTransport | None = None,
                 max_status_age: timedelta = timedelta(minutes=15)):
        parsed = urlsplit(locations_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("OCPI locations endpoint must be an HTTPS URL without credentials or query")
        if not isinstance(token, str) or not token or "\r" in token or "\n" in token:
            raise ValueError("OCPI credentials token is invalid")
        if max_status_age <= timedelta(0):
            raise ValueError("OCPI freshness window must be positive")
        self.locations_url = locations_url
        self.token = token
        self.transport = transport
        self.max_status_age = max_status_age
        self._endpoint = parsed

    def _trusted_page(self, url: str) -> bool:
        parsed = urlsplit(url)
        return (parsed.scheme == self._endpoint.scheme and parsed.netloc == self._endpoint.netloc
                and parsed.path == self._endpoint.path and not parsed.fragment
                and not parsed.username and not parsed.password)

    def _raw_records(self) -> list[dict]:
        next_url: str | None = self.locations_url
        seen_pages = set()
        records = []
        with httpx.Client(timeout=8, follow_redirects=False, transport=self.transport) as client:
            while next_url is not None:
                if next_url in seen_pages or len(seen_pages) >= 20 or not self._trusted_page(next_url):
                    raise OcpiBadResponse("invalid OCPI pagination")
                seen_pages.add(next_url)
                try:
                    with client.stream("GET", next_url, headers={
                            "Authorization": "Token " + self.token,
                            "User-Agent": "TrickeeVehicleOS/0.1 OCPI"}) as response:
                        if response.status_code != 200:
                            raise OcpiUnavailable("OCPI locations are unavailable")
                        chunks = []
                        size = 0
                        for chunk in response.iter_bytes():
                            size += len(chunk)
                            if size > 2 * 1024 * 1024:
                                raise OcpiBadResponse("OCPI page is too large")
                            chunks.append(chunk)
                        payload = b"".join(chunks)
                        link = response.headers.get("Link", "")
                except httpx.HTTPError as exc:
                    raise OcpiUnavailable("OCPI locations request failed") from exc
                try:
                    body = json.loads(payload)
                    if (not isinstance(body, dict) or body.get("status_code") != 1000
                            or not isinstance(body.get("data"), list)
                            or len(body["data"]) > 100):
                        raise ValueError("invalid OCPI envelope")
                    _date(body["timestamp"])
                    for raw in body["data"]:
                        if not isinstance(raw, dict) or len(records) >= 1000:
                            raise ValueError("invalid or excessive OCPI records")
                        records.append(raw)
                    matches = _NEXT.findall(link)
                    if len(matches) > 1:
                        raise ValueError("ambiguous OCPI pagination")
                    next_url = matches[0] if matches else None
                except (KeyError, TypeError, ValueError, OverflowError) as exc:
                    raise OcpiBadResponse("invalid OCPI locations response") from exc
        return records

    def locations(self) -> list[dict]:
        seen_stations = set()
        stations = []
        now = datetime.now(timezone.utc)
        try:
            for raw in self._raw_records():
                station = _station(raw, now, self.max_status_age)
                if station["station_id"] in seen_stations:
                    raise ValueError("duplicate OCPI location")
                seen_stations.add(station["station_id"])
                stations.append(station)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise OcpiBadResponse("invalid OCPI location") from exc
        return stations

    def nearby(self, latitude: float, longitude: float, radius_km: float = 25) -> list[dict]:
        """Return bounded nearby stations, without inferring tariff or wait time."""
        if (any(isinstance(value, bool) or not isinstance(value, (int, float))
                or not isfinite(value) for value in (latitude, longitude, radius_km))
                or not -90 <= latitude <= 90 or not -180 <= longitude <= 180
                or not 0 < radius_km <= 100):
            raise ValueError("invalid OCPI nearby search")
        found = []
        for station in self.locations():
            point = station["coordinates"]
            lat_delta = radians(point["latitude"] - latitude)
            lon_delta = radians(point["longitude"] - longitude)
            area = (sin(lat_delta / 2) ** 2 + cos(radians(latitude))
                    * cos(radians(point["latitude"])) * sin(lon_delta / 2) ** 2)
            distance = 6371.0088 * 2 * asin(min(1, sqrt(area)))
            if distance <= radius_km:
                found.append({**station, "distance_km": round(distance, 2)})
        found.sort(key=lambda item: (item["distance_km"], item["station_id"]))
        return found[:50]


class OcpiCollectionClient(OcpiClient):
    """Read-only paginated OCPI collection for Sessions or CDRs.

    The caller validates record fields with ChargingSessionStore before
    storing them. The configured URL must be the partner's version endpoint.
    """

    def records(self) -> list[dict]:
        return self._raw_records()
