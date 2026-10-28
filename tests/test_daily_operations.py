"""Daily operations report uses stored trip evidence without invented savings."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile


OP = {"X-Operator-Key": "operator-secret-123"}


def test_daily_operations_counts_completed_stops_without_fabricated_impact(tmp_path):
    app = create_app(str(tmp_path / "operations.db"), OP["X-Operator-Key"], "vehicle-secret-123")
    client = TestClient(app)
    assert client.get("/api/v1/fleet/daily-operations").status_code == 401
    app.state.store.register(demo_profile(), "DRIVER-1")
    store = app.state.store
    store.start_trip("TRICKEE-DEMO-1", "TRIP-1", "start-1", None, None, source="DRIVER_REPORTED")
    store.start_trip_stop("TRICKEE-DEMO-1", "DRIVER-1", "TRIP-1", "WAIT-1", "wait-1", "WAITING")
    store.complete_trip_stop("TRICKEE-DEMO-1", "DRIVER-1", "TRIP-1", "WAIT-1", "end-wait-1")
    ended = datetime.now(timezone.utc)
    with store.db:
        store.db.execute("UPDATE trip_stops SET started_at=?,ended_at=? WHERE trip_id=? AND stop_id=?",
                         ((ended - timedelta(minutes=15)).isoformat(), ended.isoformat(), "TRIP-1", "WAIT-1"))
    store.complete_trip("TRICKEE-DEMO-1", "TRIP-1", "finish-1", None, None)
    selected = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    response = client.get(f"/api/v1/fleet/daily-operations?date={selected}&timezone=Asia/Kolkata", headers=OP)
    assert response.status_code == 200, response.text
    report = response.json()
    assert report["trips_started"] == report["trips_completed"] == 1
    assert report["wait_minutes"] == 15.0
    assert report["charging_stop_minutes"] == 0
    assert report["trip_source_counts"]["DRIVER_REPORTED"] == 1
    assert report["charging_energy_kwh"] is None
    assert report["money_saved_inr"] is None and report["orders_completed"] is None
    assert client.get("/api/v1/fleet/daily-operations?timezone=Unknown/Zone", headers=OP).status_code == 422


def test_daily_operations_tenant_scope_and_daylight_bounds(tmp_path, monkeypatch):
    keys = {"north": "north-report-secret-123", "south": "south-report-secret-123"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": tenant, "role": "viewer", "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for tenant, key in keys.items()
    ]))
    app = create_app(str(tmp_path / "tenant-operations.db"), "legacy-secret-123", "vehicle-secret-123")
    app.state.store.register(demo_profile("NORTH-1"), "DRIVER-N", "north")
    app.state.store.register(demo_profile("SOUTH-1"), "DRIVER-S", "south")
    app.state.store.start_trip("NORTH-1", "TRIP-N", "start-n", None, None)
    selected = datetime.now(ZoneInfo("Asia/Kolkata")).date().isoformat()
    north = TestClient(app).get(f"/api/v1/fleet/daily-operations?date={selected}",
                                headers={"X-Operator-Key": keys["north"]}).json()
    south = TestClient(app).get(f"/api/v1/fleet/daily-operations?date={selected}",
                                headers={"X-Operator-Key": keys["south"]}).json()
    assert north["trips_started"] == 1 and north["driver_reports"][0]["driver_id"] == "DRIVER-N"
    assert south["trips_started"] == 0 and south["driver_reports"] == []
    dst = TestClient(app).get("/api/v1/fleet/daily-operations?date=2026-11-01&timezone=America/New_York",
                              headers={"X-Operator-Key": keys["north"]}).json()
    start = datetime.fromisoformat(dst["period"]["start"])
    end = datetime.fromisoformat(dst["period"]["end"])
    assert (end.astimezone(timezone.utc) - start.astimezone(timezone.utc)).total_seconds() == 25 * 3600
