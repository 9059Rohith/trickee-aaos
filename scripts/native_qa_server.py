"""Local-only driver API fixture with a bounded synthetic OSRM response for emulator QA."""

import os
from math import cos, hypot, radians
import httpx

from trickee.api import app
from trickee.providers import ChargerProviderStale, RoadRouter
from starlette.responses import JSONResponse


@app.middleware("http")
async def reject_gps_for_qa(request, call_next):
    if (os.getenv("TRICKEE_QA_GPS_REJECT") == "409"
            and request.method == "POST" and request.url.path.endswith("/gps-batches")):
        return JSONResponse({"detail": "synthetic GPS conflict"}, status_code=409)
    return await call_next(request)


def synthetic_route(request: httpx.Request) -> httpx.Response:
    if not request.url.path.startswith("/route/v1/driving/"):
        return httpx.Response(404)
    endpoints = request.url.path.rsplit("/", 1)[-1].split(";")
    if len(endpoints) != 2:
        return httpx.Response(400)
    start, end = ([float(value) for value in endpoint.split(",")] for endpoint in endpoints)
    middle = [(start[0] + end[0]) / 2, (start[1] + end[1]) / 2]
    bend = [middle[0] + 0.002, middle[1] + 0.002]

    def metres(left, right):
        return hypot((right[0] - left[0]) * 111_320 * cos(radians((left[1] + right[1]) / 2)),
                     (right[1] - left[1]) * 111_320)

    direct = metres(start, middle) + metres(middle, end)
    alternative = metres(start, bend) + metres(bend, end)
    return httpx.Response(200, json={
        "code": "Ok",
        "routes": [
            {"distance": direct, "duration": direct / 9,
             "geometry": {"type": "LineString", "coordinates": [start, middle, end]},
             "legs": [{"steps": [{"name": "Hosur Road", "distance": direct / 2,
                                   "maneuver": {"type": "turn", "modifier": "right",
                                                "location": middle}}]}]},
            {"distance": alternative, "duration": alternative / 8,
             "geometry": {"type": "LineString", "coordinates": [start, bend, end]},
             "legs": [{"steps": [{"name": "Outer Ring Road", "distance": alternative / 2,
                                   "maneuver": {"type": "continue",
                                                "location": bend}}]}]},
        ],
    })


if os.getenv("TRICKEE_QA_ROUTER") != "off":
    app.state.road_router = RoadRouter("https://qa-router.invalid", httpx.MockTransport(synthetic_route))
else:
    class StaleFeed:
        def nearby(self, latitude: float, longitude: float):
            raise ChargerProviderStale("synthetic stale charger feed")

    app.state.charger_feed = StaleFeed()
