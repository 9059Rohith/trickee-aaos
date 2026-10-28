"""Exercise concurrent fleet reads against a seeded loopback deployment."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
from time import perf_counter
from urllib.parse import urlparse

import httpx


BASE = os.getenv("TRICKEE_LOAD_URL", "http://127.0.0.1:8002").rstrip("/")
KEY = os.getenv("TRICKEE_LOAD_OPERATOR_KEY", "local-demo-operator-key")
OUTPUT = ((Path(os.environ["TRICKEE_QA_OUTPUT"]) / "load") if os.getenv("TRICKEE_QA_OUTPUT")
          else Path(__file__).resolve().parent.parent / "docs" / "load") / "verification.json"


def main() -> None:
    if urlparse(BASE).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("load verification accepts loopback services only")
    paths = ("/api/v1/fleet/summary", "/api/v1/vehicles")
    with httpx.Client(base_url=BASE, headers={"X-Operator-Key": KEY}, timeout=15) as client:
        def request(index: int) -> tuple[int, float]:
            started = perf_counter()
            response = client.get(paths[index % len(paths)])
            return response.status_code, perf_counter() - started

        with ThreadPoolExecutor(max_workers=24) as pool:
            results = list(pool.map(request, range(400)))
    latencies = sorted(latency for _, latency in results)
    statuses: dict[int, int] = {}
    for status, _ in results:
        statuses[status] = statuses.get(status, 0) + 1
    report = {
        "url": BASE,
        "requests": len(results),
        "concurrency": 24,
        "statuses": statuses,
        "p95_seconds": round(latencies[int(len(latencies) * 0.95) - 1], 3),
        "max_seconds": round(latencies[-1], 3),
    }
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))
    assert statuses == {200: len(results)}, "fleet reads returned an error under load"


if __name__ == "__main__":
    main()
