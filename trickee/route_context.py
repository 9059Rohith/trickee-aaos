"""Optional, strictly attributed route weather and endpoint elevation.

The local request cap protects a single process; provider-side account quotas are
still required when multiple workers or replicas are deployed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from math import isfinite
from threading import Lock
from time import monotonic
from urllib.parse import urlsplit

import httpx


class RouteContext:
    def __init__(self, weather_key: str | None, elevation_url: str | None, *,
                 elevation_token: str | None = None, daily_limit: int = 100,
                 elevation_cache_seconds: int = 0, transport: httpx.BaseTransport | None = None):
        if not 1 <= daily_limit <= 10000:
            raise ValueError("route context daily limit must be between 1 and 10000")
        if not 0 <= elevation_cache_seconds <= 86400:
            raise ValueError("elevation cache duration is invalid")
        if elevation_url:
            parsed = urlsplit(elevation_url)
            if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.query or parsed.fragment):
                raise ValueError("elevation provider URL must be HTTPS without embedded credentials")
        self.weather_key = weather_key or None
        self.elevation_url = elevation_url.rstrip("/") if elevation_url else None
        self.elevation_token = elevation_token or None
        self.elevation_cache_seconds = elevation_cache_seconds
        self.daily_limit = daily_limit
        self.transport = transport
        self._cache: dict[tuple, tuple[float, dict]] = {}
        self._counts: dict[tuple[str, str], int] = {}
        self._lock = Lock()

    @staticmethod
    def _unavailable(reason: str) -> dict:
        return {"quality": "UNAVAILABLE", "reason": reason}

    @staticmethod
    def _number(value: object, low: float, high: float) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not isfinite(value):
            raise ValueError("invalid provider number")
        result = float(value)
        if not low <= result <= high:
            raise ValueError("out-of-range provider number")
        return result

    def _cached(self, key: tuple) -> dict | None:
        with self._lock:
            entry = self._cache.get(key)
            if entry and monotonic() < entry[0]:
                return entry[1]
        return None

    def _remember(self, key: tuple, seconds: int, value: dict) -> dict:
        with self._lock:
            if len(self._cache) >= 512:
                now = monotonic()
                self._cache = {name: entry for name, entry in self._cache.items() if entry[0] > now}
                if len(self._cache) >= 512:
                    oldest = min(self._cache, key=lambda name: self._cache[name][0])
                    del self._cache[oldest]
            self._cache[key] = (monotonic() + seconds, value)
        return value

    def _allow(self, provider: str) -> bool:
        day = datetime.now(timezone.utc).date().isoformat()
        key = (provider, day)
        with self._lock:
            self._counts = {name: count for name, count in self._counts.items() if name[1] == day}
            count = self._counts.get(key, 0)
            if count >= self.daily_limit:
                return False
            self._counts[key] = count + 1
            return True

    def _get(self, url: str, params: dict, headers: dict | None = None) -> dict | None:
        try:
            with httpx.Client(timeout=5, follow_redirects=False, transport=self.transport) as client:
                response = client.get(url, params=params, headers=headers)
            if response.status_code != 200 or len(response.content) > 64 * 1024:
                return None
            data = response.json()
            return data if isinstance(data, dict) else None
        except (httpx.HTTPError, ValueError):
            return None

    def weather(self, point: dict) -> dict:
        if not self.weather_key:
            return self._unavailable("NOT_CONFIGURED")
        key = ("weather", point["latitude"], point["longitude"])
        cached = self._cached(key)
        if cached is not None:
            return cached
        if not self._allow("weather"):
            return self._unavailable("LOCAL_QUOTA_EXHAUSTED")
        data = self._get("https://api.openweathermap.org/data/2.5/weather", {
            "lat": point["latitude"], "lon": point["longitude"],
            "appid": self.weather_key, "units": "metric",
        })
        if data is None:
            return self._unavailable("UPSTREAM_UNAVAILABLE")
        try:
            timestamp = data["dt"]
            if isinstance(timestamp, bool) or not isinstance(timestamp, int):
                raise ValueError("invalid observation timestamp")
            age = datetime.now(timezone.utc).timestamp() - timestamp
            if age < -300 or age > 3600:
                return self._unavailable("STALE_DATA")
            main = data["main"]
            if not isinstance(main, dict):
                raise ValueError("invalid weather main")
            temp = self._number(main["temp"], -90, 65)
            humidity = self._number(main["humidity"], 0, 100) if "humidity" in main else None
            wind = data.get("wind", {})
            if not isinstance(wind, dict):
                raise ValueError("invalid wind")
            speed = self._number(wind["speed"], 0, 200) if "speed" in wind else None
            rain = data.get("rain", {})
            if not isinstance(rain, dict):
                raise ValueError("invalid rain")
            rain_mm = self._number(rain["1h"], 0, 500) if "1h" in rain else None
        except (KeyError, TypeError, ValueError, OverflowError):
            return self._unavailable("BAD_RESPONSE")
        return self._remember(key, 600, {
            "quality": "REPORTED", "source": "OPENWEATHER_CURRENT",
            "observed_at": datetime.fromtimestamp(timestamp, timezone.utc).isoformat(),
            "temperature_c": temp, "humidity_pct": humidity,
            "wind_speed_mps": speed, "rain_mm_1h": rain_mm,
        })

    def elevation(self, origin: dict, destination: dict) -> dict:
        if not self.elevation_url:
            return self._unavailable("NOT_CONFIGURED")
        key = ("elevation", origin["latitude"], origin["longitude"],
               destination["latitude"], destination["longitude"])
        cached = self._cached(key) if self.elevation_cache_seconds else None
        if cached is not None:
            return cached
        if not self._allow("elevation"):
            return self._unavailable("LOCAL_QUOTA_EXHAUSTED")
        headers = {"Authorization": "Bearer " + self.elevation_token} if self.elevation_token else None
        data = self._get(self.elevation_url + "/elevation", {
            "origin_lat": origin["latitude"], "origin_lon": origin["longitude"],
            "destination_lat": destination["latitude"], "destination_lon": destination["longitude"],
        }, headers)
        if data is None:
            return self._unavailable("UPSTREAM_UNAVAILABLE")
        try:
            if set(data) != {"origin", "destination"}:
                raise ValueError("unexpected elevation response fields")
            elevations = []
            for item, requested in zip((data["origin"], data["destination"]), (origin, destination)):
                if not isinstance(item, dict) or set(item) != {"latitude", "longitude", "elevation_m"}:
                    raise ValueError("invalid elevation point")
                latitude = self._number(item["latitude"], -90, 90)
                longitude = self._number(item["longitude"], -180, 180)
                if abs(latitude - requested["latitude"]) > 0.02 or abs(longitude - requested["longitude"]) > 0.02:
                    raise ValueError("elevation result does not match requested location")
                elevations.append(self._number(item["elevation_m"], -500, 9000))
        except (KeyError, TypeError, ValueError):
            return self._unavailable("BAD_RESPONSE")
        result = {
            "quality": "REPORTED", "source": "CONFIGURED_ELEVATION",
            "origin_elevation_m": elevations[0],
            "destination_elevation_m": elevations[1],
            "elevation_delta_m": round(elevations[1] - elevations[0], 2),
        }
        return self._remember(key, self.elevation_cache_seconds, result) if self.elevation_cache_seconds else result

    def for_route(self, origin: dict, destination: dict) -> dict:
        return {
            "weather": self.weather(origin),
            "elevation": self.elevation(origin, destination),
            "traffic": {"quality": "UNAVAILABLE", "live": False},
        }
