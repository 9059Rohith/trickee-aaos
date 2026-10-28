"""Local-only OSRM-shaped route fixture for the car template host QA.

This is deterministic synthetic road geometry. It must never be configured for
a real driver or included in a production navigation image.
"""

from __future__ import annotations

import argparse
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from math import cos, hypot, radians
from urllib.parse import urlsplit


def distance_metres(left: tuple[float, float], right: tuple[float, float]) -> float:
    mean_lat = radians((left[1] + right[1]) / 2)
    return hypot((right[0] - left[0]) * 111_320 * cos(mean_lat),
                 (right[1] - left[1]) * 111_320)


def fixture_route(path: str) -> dict:
    prefix = "/route/v1/driving/"
    parsed = urlsplit(path)
    if not parsed.path.startswith(prefix):
        raise ValueError("not a route request")
    pairs = parsed.path[len(prefix):].split(";")
    if len(pairs) != 2:
        raise ValueError("route requires two endpoints")
    points = []
    for pair in pairs:
        numbers = pair.split(",")
        if len(numbers) != 2:
            raise ValueError("coordinate pair missing")
        lon, lat = map(float, numbers)
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("coordinate out of range")
        points.append((lon, lat))
    start, finish = points
    middle = ((start[0] + finish[0]) / 2, (start[1] + finish[1]) / 2)
    shape = [start, middle, finish]
    length = distance_metres(start, middle) + distance_metres(middle, finish)
    return {
        "code": "Ok",
        "routes": [{
            "distance": length,
            "duration": length / 10,
            "geometry": {"type": "LineString", "coordinates": shape},
            "legs": [{"steps": [{
                "name": "Synthetic test road",
                "maneuver": {"type": "continue", "modifier": "straight", "location": middle},
            }]}],
        }],
    }


class Handler(BaseHTTPRequestHandler):
    def _reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        if not self.server.available:
            self._reply(503, b'{"code":"Unavailable","routes":[]}')
            return
        try:
            body = json.dumps(fixture_route(self.path)).encode("utf-8")
            status = 200
        except (ValueError, TypeError):
            body = b'{"code":"InvalidRequest","routes":[]}'
            status = 400
        self._reply(status, body)

    def do_POST(self) -> None:
        length_text = self.headers.get("Content-Length", "")
        if (self.path != "/__test__/availability" or not length_text.isdigit()
                or not 1 <= int(length_text) <= 32):
            self._reply(400, b'{"error":"invalid test control"}')
            return
        try:
            command = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if set(command) != {"available"} or not isinstance(command["available"], bool):
                raise ValueError("invalid availability state")
        except (TypeError, ValueError, json.JSONDecodeError):
            self._reply(400, b'{"error":"invalid test control"}')
            return
        self.server.available = command["available"]
        self._reply(200, b'{"ok":true}')

    def log_message(self, format_string: str, *args: object) -> None:
        return  # Do not log route coordinates in ordinary console output.


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    if not (1024 <= args.port <= 65535):
        raise SystemExit("port must be between 1024 and 65535")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.available = True
    print(f"Synthetic AAOS route fixture listening on 127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
