"""Replaceable route provider contract; local data is explicitly simulated."""
from __future__ import annotations

from dataclasses import dataclass
from math import asin, cos, isfinite, radians, sin, sqrt
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from threading import Lock
from time import monotonic
from typing import Callable, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import ValidationError

from .domain import Charger, Route


@dataclass(frozen=True)
class RoadTableCell:
    """One routed OSRM matrix cell; zero is a valid same-road arrival."""

    distance_km: float
    duration_min: float


class MapProvider(Protocol):
    def geocode(self, query: str) -> dict | None: ...
    def reverse_geocode(self, latitude: float, longitude: float) -> dict | None: ...
    def route(self, origin: str, destination: str) -> Route: ...
    def route_matrix(self, origins: list[str], destinations: list[str]) -> list[list[Route]]: ...
    def traffic(self, origin: str, destination: str) -> dict: ...
    def map_tile(self, z: int, x: int, y: int) -> dict: ...
    def search(self, query: str) -> list[dict]: ...


_PLACES = {
    "bengaluru": (12.9716, 77.5946),
    "mysuru": (12.2958, 76.6394),
    "chennai": (13.0827, 80.2707),
    "hosur": (12.7409, 77.8253),
}


class MockMapProvider:
    def geocode(self, query: str) -> dict | None:
        coordinates = _PLACES.get(query.strip().lower())
        return {"name": query.strip().title(), "latitude": coordinates[0], "longitude": coordinates[1], "quality": "SIMULATED"} if coordinates else None

    def reverse_geocode(self, latitude: float, longitude: float) -> dict | None:
        closest = min(_PLACES, key=lambda name: self._distance(latitude, longitude, *_PLACES[name]))
        return self.geocode(closest) if self._distance(latitude, longitude, *_PLACES[closest]) < 10 else None

    @staticmethod
    def _distance(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
        a = sin(radians(lat2 - lat1) / 2) ** 2 + cos(radians(lat1)) * cos(radians(lat2)) * sin(radians(lon2 - lon1) / 2) ** 2
        return 6371 * 2 * asin(sqrt(a))

    def route(self, origin: str, destination: str) -> Route:
        a, b = self.geocode(origin), self.geocode(destination)
        if not a or not b or origin.strip().lower() == destination.strip().lower():
            raise ValueError("mock route requires two distinct supported places")
        distance = self._distance(a["latitude"], a["longitude"], b["latitude"], b["longitude"]) * 1.25
        speed = 55.0
        return Route(distance_km=round(distance, 1), baseline_eta_min=round(distance / speed * 60, 1), average_speed_kmh=speed, traffic_factor=1.0, source="SIMULATED")

    def route_matrix(self, origins: list[str], destinations: list[str]) -> list[list[Route]]:
        return [[self.route(origin, destination) for destination in destinations] for origin in origins]

    def traffic(self, origin: str, destination: str) -> dict:
        self.route(origin, destination)
        return {"factor": 1.0, "quality": "SIMULATED", "live": False}

    def map_tile(self, z: int, x: int, y: int) -> dict:
        return {"status": "NOT_SUPPORTED", "quality": "SIMULATED"}

    def search(self, query: str) -> list[dict]:
        return [self.geocode(name) for name in _PLACES if query.lower() in name]


def map_provider(name: str) -> MapProvider:
    if name == "mock":
        return MockMapProvider()
    raise ValueError(f"unsupported map provider: {name}")


class RoadProviderUnavailable(Exception):
    """Configured road service cannot answer this request."""


class RoadProviderBadResponse(Exception):
    """A road service response failed schema or physical bounds checks."""


class PlaceProviderUnavailable(Exception):
    """Configured place service cannot answer this request."""


class PlaceProviderRateLimited(PlaceProviderUnavailable):
    """Configured place service asked callers to wait before retrying."""

    def __init__(self, retry_seconds: int):
        super().__init__("place provider quota reached; retry later")
        self.retry_seconds = retry_seconds


class PlaceProviderBadResponse(Exception):
    """A place service response failed validation."""


class ChargerProviderUnavailable(Exception):
    """A contracted charger feed cannot answer this request."""


class ChargerProviderBadResponse(Exception):
    """A charger feed returned untrustworthy or incomplete data."""


class ChargerProviderStale(Exception):
    """The charger status is too old to present as live availability."""


class ChargerFeed:
    """Normalize a contracted HTTPS nearby feed without inventing missing status."""

    _FIELDS = {"charger_id", "name", "connector", "power_kw", "distance_km", "detour_min",
               "queue_min", "price_per_kwh", "currency", "reliability", "available"}

    def __init__(self, base_url: str, token: str | None = None, transport: httpx.BaseTransport | None = None):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment):
            raise ValueError("charger feed must be a configured HTTPS URL without embedded credentials")
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.transport = transport

    def nearby(self, latitude: float, longitude: float) -> dict:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("invalid charger search coordinates")
        headers = {"User-Agent": "TrickeeVehicleOS/0.1 configured-charger-feed"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        try:
            with httpx.Client(timeout=5, follow_redirects=False, transport=self.transport) as client:
                response = client.get(self.base_url + "/nearby", params={"lat": latitude, "lon": longitude},
                                      headers=headers)
        except httpx.HTTPError as exc:
            raise ChargerProviderUnavailable("charger feed request failed") from exc
        if response.status_code != 200:
            raise ChargerProviderUnavailable("charger feed is unavailable")
        if len(response.content) > 1024 * 1024:
            raise ChargerProviderBadResponse("charger feed response is too large")
        try:
            body = response.json()
            if not isinstance(body, dict) or set(body) != {"observed_at", "chargers"}:
                raise ValueError("charger feed shape is invalid")
            observed = datetime.fromisoformat(body["observed_at"])
            if observed.tzinfo is None:
                raise ValueError("charger feed timestamp needs timezone")
            age = datetime.now(timezone.utc) - observed.astimezone(timezone.utc)
            if age > timedelta(minutes=5) or age < -timedelta(minutes=2):
                raise ChargerProviderStale("charger status is stale or future-dated")
            items = body["chargers"]
            if not isinstance(items, list) or len(items) > 50:
                raise ValueError("charger list is invalid")
            chargers = []
            seen = set()
            for item in items:
                if not isinstance(item, dict) or set(item) != self._FIELDS or not isinstance(item["available"], bool):
                    raise ValueError("charger fields are incomplete")
                charger = Charger.model_validate({**item, "source": "PROVIDER"})
                if charger.charger_id in seen:
                    raise ValueError("charger IDs are duplicated")
                seen.add(charger.charger_id)
                chargers.append(charger)
            return {"quality": "PROVIDER", "observed_at": observed.isoformat(), "chargers": chargers}
        except ChargerProviderStale:
            raise
        except (TypeError, ValueError, ValidationError) as exc:
            raise ChargerProviderBadResponse("charger feed response failed validation") from exc


class Geocoder:
    """Bounded search/reverse adapter for a configured Nominatim-compatible host."""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None,
                 clock: Callable[[], float] = monotonic):
        parsed = urlsplit(base_url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or parsed.hostname.rstrip(".") == "nominatim.openstreetmap.org"):
            raise ValueError("geocoder must be a configured HTTPS host, not the public OSM Nominatim service")
        self.base_url = base_url.rstrip("/")
        self.transport = transport
        self.clock = clock
        self._cooldown_until = 0.0
        self._cooldown_lock = Lock()

    @staticmethod
    def _retry_after(value: str | None) -> int:
        if value is not None:
            try:
                value = value.strip()
                seconds = int(value) if value.isascii() and value.isdecimal() else None
                if seconds is None:
                    instant = parsedate_to_datetime(value)
                    seconds = int((instant - datetime.now(timezone.utc)).total_seconds())
                return min(300, max(1, seconds))
            except (TypeError, ValueError, OverflowError):
                pass
        return 30

    def _get(self, path: str, params: dict) -> object:
        with self._cooldown_lock:
            remaining = self._cooldown_until - self.clock()
        if remaining > 0:
            raise PlaceProviderRateLimited(min(300, max(1, int(remaining + 0.999))))
        try:
            with httpx.Client(timeout=5, follow_redirects=False, transport=self.transport) as client:
                response = client.get(self.base_url + path, params=params,
                                      headers={"User-Agent": "TrickeeVehicleOS/0.1 configured-geocoder"})
        except httpx.HTTPError as exc:
            raise PlaceProviderUnavailable("place provider request failed") from exc
        if response.status_code == 429:
            seconds = self._retry_after(response.headers.get("Retry-After"))
            with self._cooldown_lock:
                self._cooldown_until = max(self._cooldown_until, self.clock() + seconds)
            raise PlaceProviderRateLimited(seconds)
        if response.status_code != 200:
            raise PlaceProviderUnavailable("place provider is unavailable")
        if len(response.content) > 1024 * 1024:
            raise PlaceProviderBadResponse("place provider response is too large")
        try:
            return response.json()
        except ValueError as exc:
            raise PlaceProviderBadResponse("invalid place provider JSON") from exc

    @staticmethod
    def _place(item: object) -> dict:
        try:
            if not isinstance(item, dict):
                raise ValueError("place is not an object")
            name = item["display_name"]
            place_id = item["place_id"]
            if isinstance(item["lat"], bool) or isinstance(item["lon"], bool):
                raise ValueError("boolean coordinate")
            latitude = float(item["lat"])
            longitude = float(item["lon"])
            if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 300
                    or isinstance(place_id, bool) or not isinstance(place_id, int) or place_id < 1
                    or not isfinite(latitude) or not isfinite(longitude)
                    or not -90 <= latitude <= 90 or not -180 <= longitude <= 180):
                raise ValueError("invalid place fields")
            osm_ref = None
            if "osm_type" in item or "osm_id" in item:
                kind = {"node": "N", "way": "W", "relation": "R"}.get(item.get("osm_type"))
                identifier = item.get("osm_id")
                category = item.get("class", item.get("category"))
                if (kind is None or isinstance(identifier, bool)
                        or not (isinstance(identifier, int) and 0 < identifier < 10**20
                                or isinstance(identifier, str) and 1 <= len(identifier) <= 20
                                and identifier.isascii() and identifier.isdecimal()
                                and not identifier.startswith("0"))
                        or not isinstance(category, str) or not 1 <= len(category) <= 80
                        or not category[0].islower()
                        or not all(character.isascii() and (character.islower() or character.isdigit()
                                    or character in "_:-") for character in category)):
                    raise ValueError("invalid OSM reference")
                osm_ref = {"osm_type": kind, "osm_id": str(identifier), "category": category}
            return {"place_id": str(place_id), "name": name.strip(), "osm_ref": osm_ref,
                    "latitude": latitude, "longitude": longitude,
                    "source": "PROVIDER", "quality": "REPORTED"}
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            raise PlaceProviderBadResponse("invalid place provider result") from exc

    def search(self, query: str) -> list[dict]:
        data = self._get("/search", {"q": query, "format": "jsonv2", "limit": "5", "addressdetails": "0"})
        if not isinstance(data, list) or len(data) > 5:
            raise PlaceProviderBadResponse("invalid place search response")
        return [self._place(item) for item in data]

    def reverse(self, latitude: float, longitude: float) -> dict | None:
        data = self._get("/reverse", {"lat": latitude, "lon": longitude, "format": "jsonv2"})
        if isinstance(data, dict) and isinstance(data.get("error"), str):
            return None
        return self._place(data)


def _route_projection(points: list[list[float]], location: list[float]) -> tuple[float, float]:
    """Return kilometres along and away from the closest route segment."""
    travelled = 0.0
    closest = (0.0, float("inf"))
    for start, end in zip(points, points[1:]):
        mid_latitude = (start[1] + end[1]) / 2
        east_scale = 111.32 * max(0.01, abs(cos(radians(mid_latitude))))
        dx = (end[0] - start[0]) * east_scale
        dy = (end[1] - start[1]) * 111.32
        fx = (location[0] - start[0]) * east_scale
        fy = (location[1] - start[1]) * 111.32
        square = dx * dx + dy * dy
        fraction = min(1.0, max(0.0, (fx * dx + fy * dy) / square)) if square else 0.0
        offset = sqrt((fx - fraction * dx) ** 2 + (fy - fraction * dy) ** 2)
        segment_km = MockMapProvider._distance(start[1], start[0], end[1], end[0])
        if offset < closest[1]:
            closest = (travelled + fraction * segment_km, offset)
        travelled += segment_km
    return closest


class RoadRouter:
    """OSRM-compatible road geometry; traffic and geocoding are separate inputs."""

    def __init__(self, base_url: str, transport: httpx.BaseTransport | None = None):
        parsed = urlsplit(base_url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("road router URL must be an HTTPS origin without credentials or query")
        self.base_url = base_url.rstrip("/")
        self.transport = transport

    def route(self, origin: tuple[float, float], destination: tuple[float, float]) -> dict:
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
               for value in (*origin, *destination)) or any(
                   not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180
                   for point in (origin, destination)):
            raise ValueError("invalid route request coordinates")
        if origin == destination:
            raise ValueError("origin and destination must differ")
        coords = f"{origin[1]},{origin[0]};{destination[1]},{destination[0]}"
        try:
            with httpx.Client(timeout=8, follow_redirects=False, transport=self.transport) as client:
                response = client.get(
                    f"{self.base_url}/route/v1/driving/{coords}",
                    params={"alternatives": "true", "steps": "true", "geometries": "geojson", "overview": "full"},
                )
        except httpx.HTTPError as exc:
            raise RoadProviderUnavailable("road router request failed") from exc
        if response.status_code != 200:
            raise RoadProviderUnavailable("road router is unavailable")
        if len(response.content) > 4 * 1024 * 1024:
            raise RoadProviderBadResponse("road router response is too large")
        try:
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("invalid response object")
            if payload.get("code") != "Ok" or not isinstance(payload.get("routes"), list) or not payload["routes"]:
                raise RoadProviderUnavailable("road router found no route")
            routes = []
            for candidate in payload["routes"][:3]:
                geometry = candidate["geometry"]
                points = geometry["coordinates"]
                if geometry["type"] != "LineString" or not isinstance(points, list) or not 2 <= len(points) <= 20000:
                    raise ValueError("invalid geometry")
                for point in points:
                    if not isinstance(point, list) or len(point) != 2 or any(
                        not isinstance(value, (int, float)) or isinstance(value, bool) or not isfinite(value)
                        for value in point
                    ) or not (-180 <= point[0] <= 180 and -90 <= point[1] <= 90):
                        raise ValueError("invalid coordinate")
                if any(abs(right[0] - left[0]) > 180 for left, right in zip(points, points[1:])):
                    raise ValueError("antimeridian crossing needs split geometry")
                if (MockMapProvider._distance(origin[0], origin[1], points[0][1], points[0][0]) > 2
                        or MockMapProvider._distance(destination[0], destination[1],
                                                     points[-1][1], points[-1][0]) > 2):
                    raise ValueError("route endpoints do not match request")
                drawn_km = sum(MockMapProvider._distance(left[1], left[0], right[1], right[0])
                               for left, right in zip(points, points[1:]))
                if drawn_km < 0.01:
                    raise ValueError("route geometry is degenerate")
                if any(isinstance(candidate[field], bool) or
                       not isinstance(candidate[field], (int, float)) or
                       not isfinite(candidate[field]) for field in ("distance", "duration")):
                    raise ValueError("invalid route metrics")
                distance_km = candidate["distance"] / 1000
                minutes = candidate["duration"] / 60
                if (distance_km <= 0 or minutes <= 0 or drawn_km > distance_km * 3 + 0.5
                        or distance_km > drawn_km * 4 + 2):
                    raise ValueError("route distance contradicts geometry")
                route = Route(
                    distance_km=distance_km,
                    baseline_eta_min=minutes,
                    average_speed_kmh=distance_km / (minutes / 60),
                    traffic_factor=1,
                    source="PROVIDER",
                )
                steps = []
                legs = candidate.get("legs", [])
                if not isinstance(legs, list) or len(legs) > 10:
                    raise ValueError("invalid route legs")
                previous_turn_km = -1.0
                for leg in legs:
                    raw_steps = leg["steps"]
                    if not isinstance(raw_steps, list) or len(raw_steps) > 200 or len(steps) + len(raw_steps) > 200:
                        raise ValueError("invalid route steps")
                    for raw in raw_steps:
                        maneuver = raw["maneuver"]
                        kind = maneuver["type"]
                        modifier = maneuver.get("modifier")
                        location = maneuver["location"]
                        name = raw.get("name", "")
                        metres = raw["distance"]
                        if (not isinstance(location, list) or len(location) != 2 or
                                any(not isinstance(value, (int, float)) or isinstance(value, bool)
                                    or not isfinite(value) for value in location) or
                                not -180 <= location[0] <= 180 or not -90 <= location[1] <= 90):
                            raise ValueError("invalid maneuver location")
                        along_km, offset_km = _route_projection(points, location)
                        if offset_km > 0.1 or along_km + 0.02 < previous_turn_km:
                            raise ValueError("maneuver is off route or out of order")
                        previous_turn_km = along_km
                        if (not isinstance(kind, str) or not 1 <= len(kind) <= 40 or
                                modifier is not None and (not isinstance(modifier, str) or len(modifier) > 40) or
                                not isinstance(name, str) or len(name) > 160 or
                                not isinstance(metres, (int, float)) or isinstance(metres, bool) or
                                not isfinite(metres) or not 0 <= metres <= 1_000_000):
                            raise ValueError("invalid route step")
                        steps.append({"type": kind, "modifier": modifier, "name": name,
                                      "distance_m": round(metres, 1),
                                      "location": {"latitude": location[1], "longitude": location[0]}})
                routes.append({"route": route.model_dump(), "geometry": geometry, "steps": steps})
            return {"routes": routes, "quality": "PROVIDER", "traffic": {"quality": "UNAVAILABLE", "live": False}}
        except RoadProviderUnavailable:
            raise
        except (AttributeError, KeyError, TypeError, ValueError, ZeroDivisionError) as exc:
            raise RoadProviderBadResponse("invalid road router response") from exc

    def match_trace(self, points: list[tuple[float, float, int, float]]) -> dict:
        """Match a short GPS trace while retaining dropped points and split segments."""
        if not 2 <= len(points) <= 50:
            raise ValueError("road match requires 2–50 points")
        for latitude, longitude, timestamp, accuracy in points:
            if (not isfinite(latitude) or not isfinite(longitude)
                    or not -90 <= latitude <= 90 or not -180 <= longitude <= 180
                    or isinstance(timestamp, bool) or not isinstance(timestamp, int) or timestamp < 0
                    or not isfinite(accuracy) or not 0 <= accuracy <= 1000):
                raise ValueError("invalid road match point")
        if any(current[2] <= previous[2] for previous, current in zip(points, points[1:])):
            raise ValueError("road match timestamps must increase by at least one second")
        coordinates = ";".join(f"{longitude},{latitude}" for latitude, longitude, _, _ in points)
        try:
            with httpx.Client(timeout=8, follow_redirects=False, transport=self.transport) as client:
                response = client.get(f"{self.base_url}/match/v1/driving/{coordinates}", params={
                    "timestamps": ";".join(str(timestamp) for _, _, timestamp, _ in points),
                    "radiuses": ";".join(str(accuracy) for _, _, _, accuracy in points),
                    "geometries": "geojson", "overview": "full", "steps": "false", "gaps": "split",
                })
        except httpx.HTTPError as exc:
            raise RoadProviderUnavailable("road matching request failed") from exc
        if len(response.content) > 4 * 1024 * 1024:
            raise RoadProviderBadResponse("road matching response is too large")
        if response.status_code not in (200, 400):
            raise RoadProviderUnavailable("road matching is unavailable")
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError("invalid road matching object")
            if response.status_code == 400 and body.get("code") == "NoMatch":
                return {"quality": "UNMATCHED", "tracepoints": [None] * len(points), "segments": []}
            if response.status_code != 200 or body.get("code") != "Ok":
                raise RoadProviderUnavailable("road matching found no valid trace")
            raw_trace, raw_segments = body["tracepoints"], body["matchings"]
            if (not isinstance(raw_trace, list) or len(raw_trace) != len(points)
                    or not isinstance(raw_segments, list) or not 1 <= len(raw_segments) <= 50):
                raise ValueError("invalid matching shape")

            def location(value: object) -> list[float]:
                if (not isinstance(value, list) or len(value) != 2
                        or any(isinstance(item, bool) or not isinstance(item, (int, float))
                               or not isfinite(item) for item in value)
                        or not -180 <= value[0] <= 180 or not -90 <= value[1] <= 90):
                    raise ValueError("invalid matched coordinate")
                return value

            segments = []
            for segment in raw_segments:
                confidence = segment["confidence"]
                geometry = segment["geometry"]
                coordinates = geometry["coordinates"]
                if (isinstance(confidence, bool) or not isinstance(confidence, (int, float))
                        or not isfinite(confidence) or not 0 <= confidence <= 1
                        or geometry["type"] != "LineString" or not isinstance(coordinates, list)
                        or not 2 <= len(coordinates) <= 20000):
                    raise ValueError("invalid matching segment")
                segments.append({"confidence": confidence,
                                 "geometry": {"type": "LineString", "coordinates": [location(item) for item in coordinates]}})
            tracepoints = []
            for item in raw_trace:
                if item is None:
                    tracepoints.append(None)
                    continue
                index, distance = item["matchings_index"], item["distance"]
                if (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < len(segments)
                        or isinstance(distance, bool) or not isinstance(distance, (int, float))
                        or not isfinite(distance) or not 0 <= distance <= 1000):
                    raise ValueError("invalid matched tracepoint")
                tracepoints.append({"location": location(item["location"]),
                                    "distance_m": distance, "segment_index": index})
            return {"quality": "PROVIDER", "tracepoints": tracepoints, "segments": segments}
        except RoadProviderUnavailable:
            raise
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            raise RoadProviderBadResponse("invalid road matching response") from exc

    def table_to_destination(self, origins: list[tuple[float, float]],
                             destination: tuple[float, float]) -> list[Route | None]:
        """Compatibility view for consumers requiring positive-distance Route objects."""
        cells = self.table_metrics_to_destination(origins, destination)
        return [None if cell is None or cell.distance_km == 0 else Route(
            distance_km=cell.distance_km, baseline_eta_min=cell.duration_min,
            average_speed_kmh=cell.distance_km / (cell.duration_min / 60), source="PROVIDER",
        ) for cell in cells]

    def table_metrics_to_destination(self, origins: list[tuple[float, float]],
                                     destination: tuple[float, float]) -> list[RoadTableCell | None]:
        """One bounded OSRM matrix request; preserve null and zero cells distinctly."""
        if not 1 <= len(origins) <= 20:
            raise ValueError("road table requires 1–20 origins")
        points = [*origins, destination]
        if any(not isinstance(point, (tuple, list)) or len(point) != 2 or any(
            isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value)
            for value in point) or not -90 <= point[0] <= 90 or not -180 <= point[1] <= 180
            for point in points):
            raise ValueError("invalid road table coordinates")
        coordinates = ";".join(f"{longitude},{latitude}" for latitude, longitude in points)
        try:
            with httpx.Client(timeout=8, follow_redirects=False, transport=self.transport) as client:
                response = client.get(f"{self.base_url}/table/v1/driving/{coordinates}", params={
                    "sources": ";".join(str(index) for index in range(len(origins))),
                    "destinations": str(len(origins)), "annotations": "duration,distance",
                })
        except httpx.HTTPError as exc:
            raise RoadProviderUnavailable("road table request failed") from exc
        if response.status_code != 200:
            raise RoadProviderUnavailable("road table is unavailable")
        if len(response.content) > 1024 * 1024:
            raise RoadProviderBadResponse("road table response is too large")
        try:
            body = response.json()
            if not isinstance(body, dict) or body.get("code") != "Ok":
                raise ValueError("invalid road table status")
            durations, distances = body["durations"], body["distances"]
            if (not isinstance(durations, list) or not isinstance(distances, list)
                    or len(durations) != len(origins) or len(distances) != len(origins)):
                raise ValueError("invalid road table size")
            cells = []
            for duration_row, distance_row in zip(durations, distances):
                if (not isinstance(duration_row, list) or not isinstance(distance_row, list)
                        or len(duration_row) != 1 or len(distance_row) != 1):
                    raise ValueError("invalid road table cell")
                seconds, meters = duration_row[0], distance_row[0]
                if seconds is None and meters is None:
                    cells.append(None)
                    continue
                if (isinstance(seconds, bool) or isinstance(meters, bool)
                        or not isinstance(seconds, (int, float)) or not isinstance(meters, (int, float))
                        or not isfinite(seconds) or not isfinite(meters) or seconds < 0 or meters < 0
                        or (seconds == 0) != (meters == 0)):
                    raise ValueError("invalid road table metric")
                distance_km, duration_min = meters / 1000, seconds / 60
                if seconds and (not 0 < distance_km <= 3000 or not 0 < duration_min <= 10000
                                or not 1 < distance_km / (duration_min / 60) <= 160):
                    raise ValueError("road table metric outside supported route bounds")
                cells.append(RoadTableCell(distance_km=distance_km, duration_min=duration_min))
            return cells
        except (KeyError, TypeError, ValueError, ValidationError, ZeroDivisionError) as exc:
            raise RoadProviderBadResponse("invalid road table response") from exc
