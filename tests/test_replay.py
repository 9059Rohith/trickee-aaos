"""Historical trip replay must preserve provenance and tenant access."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.replay import trip_timeline
from trickee.simulator import demo_profile, scenario_frame


OP = {"X-Operator-Key": "operator-key-12345"}


def test_replay_endpoint_returns_recorded_history_without_new_events(tmp_path):
    app = create_app(str(tmp_path / "cloud.db"), OP["X-Operator-Key"],
                     "vehicle-key-12345")
    profile = demo_profile()
    app.state.store.register(profile)
    now = datetime.now(timezone.utc)
    first = scenario_frame("SCENARIO_001", 1, now - timedelta(minutes=1))
    second = scenario_frame("SCENARIO_007", 2, now)
    app.state.store.ingest([first, second])
    client = TestClient(app)
    before = app.state.store.trip_frame_count(profile.vehicle_id, "DEMO-TRIP-1")
    response = client.get("/api/v1/vehicles/TRICKEE-DEMO-1/trips/DEMO-TRIP-1/replay",
                          headers=OP)
    assert response.status_code == 200
    body = response.json()
    assert body["quality"] == "HISTORICAL_REPLAY" and body["live"] is False
    assert body["total_frame_count"] == before == 2
    assert body["timeline"][0]["original_source"] == "SIMULATOR"
    assert body["timeline"][1]["health"]["status"] == "CRITICAL"
    assert app.state.store.trip_frame_count(profile.vehicle_id, "DEMO-TRIP-1") == before
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/trips/DEMO-TRIP-1/replay").status_code == 401
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/trips/unknown/replay",
                      headers=OP).status_code == 404
    assert any(event["action"] == "trip_replay_read" for event in
               app.state.store.audit_events(profile.vehicle_id)["events"])


def test_replay_marks_clock_anomaly_and_truncation_without_fabricating_location():
    now = datetime.now(timezone.utc)
    first = scenario_frame("SCENARIO_001", 1, now).model_copy(update={
        "latitude": None, "longitude": None})
    second = scenario_frame("SCENARIO_001", 2, now - timedelta(seconds=10)).model_copy(
        update={"latitude": None, "longitude": None})
    result = trip_timeline(first.vehicle_id, first.trip_id,
                           [first.model_dump(mode="json"), second.model_dump(mode="json")], 3)
    assert result["truncated"] is True
    assert result["timeline"][1]["clock_anomaly"] is True
    assert result["timeline"][0]["latitude"] is None
    with pytest.raises(ValueError, match="foreign"):
        trip_timeline(first.vehicle_id, "OTHER", [first.model_dump(mode="json")], 1)
