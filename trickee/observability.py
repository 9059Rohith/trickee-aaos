"""Low-cardinality request metrics and secret-safe structured request logs."""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime, timezone


BUCKETS_MS = (10, 25, 50, 100, 250, 500, 1000, 2000, 5000, 10000)
logging.getLogger("trickee.requests").setLevel(logging.INFO)


class RequestMetrics:
    def __init__(self):
        self._lock = threading.Lock()
        self._rows: dict[tuple[str, str, int], dict] = defaultdict(
            lambda: {"requests": 0, "errors": 0, "latency_ms_sum": 0.0,
                     "buckets": [0] * (len(BUCKETS_MS) + 1)})

    def record(self, method: str, route: str, status: int, latency_ms: float) -> None:
        # Callers pass route templates, never URLs, query strings, or user IDs.
        if not route.startswith("/") or len(route) > 256:
            route = "UNMATCHED"
        key = (method, route, status)
        with self._lock:
            row = self._rows[key]
            row["requests"] += 1
            row["errors"] += int(status >= 500)
            row["latency_ms_sum"] += latency_ms
            index = next((index for index, bound in enumerate(BUCKETS_MS)
                          if latency_ms <= bound), len(BUCKETS_MS))
            row["buckets"][index] += 1

    def snapshot(self) -> dict:
        with self._lock:
            routes = [{"method": method, "route": route, "status": status,
                       "requests": row["requests"], "errors": row["errors"],
                       "latency_ms_sum": round(row["latency_ms_sum"], 3),
                       "latency_buckets": {
                           **{f"le_{bound}": count for bound, count in
                              zip(BUCKETS_MS, row["buckets"][:-1])},
                           "over_10000": row["buckets"][-1],
                       }} for (method, route, status), row in
                      sorted(self._rows.items())]
        return {"total_requests": sum(row["requests"] for row in routes),
                "server_errors": sum(row["errors"] for row in routes),
                "routes": routes}


def request_log(method: str, route: str, status: int, latency_ms: float,
                request_id: str) -> None:
    event = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "service": "trickee-cloud", "event": "http_request",
        "request_id": request_id, "severity": "ERROR" if status >= 500 else "INFO",
        "method": method, "route": route, "status": status,
        "latency_ms": round(latency_ms, 3),
    }
    logging.getLogger("trickee.requests").info(json.dumps(event, separators=(",", ":")))


def new_request_id() -> str:
    return uuid.uuid4().hex


def elapsed_ms(started: float) -> float:
    return (time.monotonic() - started) * 1000
