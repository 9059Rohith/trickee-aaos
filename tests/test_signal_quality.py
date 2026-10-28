"""Vehicle decisions must not treat stale or future telemetry as live."""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from trickee.agent import VehicleAgent
from trickee.api import create_app
from trickee.domain import Quality, Route
from trickee.simulator import demo_profile, scenario_frame
from trickee.store import CloudStore


ROUTE = Route(distance_km=50, baseline_eta_min=60, average_speed_kmh=50)


@pytest.mark.parametrize("age,expected", [
    (timedelta(minutes=2), Quality.STALE),
    (timedelta(minutes=-3), Quality.INVALID),
])
def test_stale_or_future_frame_cannot_drive_local_decisions(tmp_path, age, expected):
    agent = VehicleAgent(demo_profile(), str(tmp_path / "quality.db"))
    frame = scenario_frame("SCENARIO_001", 1, datetime.now(timezone.utc) - age)
    agent.capture(frame)
    assert frame.quality() == expected
    status = agent.local_status(ROUTE)
    assert status["quality"] == expected.value
    assert status["range"] is None
    assert status["eta"] is None
    assert agent.recommend_chargers() == []
    assert "unavailable" in agent.copilot("Can I reach it?", ROUTE)["text"].lower()
    assert "unavailable" in agent.copilot("Find a charger")["text"].lower()
    assert "unavailable" in agent.copilot("battery")["text"].lower()


def test_newer_sequence_with_older_observation_is_rejected(tmp_path):
    agent = VehicleAgent(demo_profile(), str(tmp_path / "chronology.db"))
    now = datetime.now(timezone.utc)
    agent.capture(scenario_frame("SCENARIO_001", 1, now))
    with pytest.raises(ValueError, match="observation time"):
        agent.capture(scenario_frame("SCENARIO_001", 2, now - timedelta(seconds=5)))
    assert agent.outbox.count_pending() == 1


def test_stale_status_does_not_claim_live_vehicle_state(tmp_path):
    agent = VehicleAgent(demo_profile(), str(tmp_path / "stale.db"))
    frame = scenario_frame("SCENARIO_001", 1,
                           datetime.now(timezone.utc) - timedelta(minutes=1))
    agent.capture(frame)
    assert agent.local_status()["state"] == "OFFLINE"


def test_cloud_twin_marks_stale_frame_offline_but_keeps_last_observation(tmp_path):
    cloud = CloudStore(str(tmp_path / "twin.db"))
    profile = demo_profile()
    cloud.register(profile)
    old = scenario_frame("SCENARIO_001", 1,
                         datetime.now(timezone.utc) - timedelta(minutes=1))
    cloud.ingest([old])
    twin = cloud.twin(profile.vehicle_id)
    assert twin["quality"] == "STALE"
    assert twin["state"] == "OFFLINE"
    assert twin["observed_at"] == old.observed_at.isoformat()
    cloud.close()


def test_local_agent_recovers_after_stale_driving_frame(tmp_path):
    agent = VehicleAgent(demo_profile(), str(tmp_path / "recover.db"))
    old = scenario_frame("SCENARIO_001", 1,
                         datetime.now(timezone.utc) - timedelta(minutes=1))
    agent.capture(old)
    assert agent.local_status()["state"] == "OFFLINE"
    fresh = scenario_frame("SCENARIO_008", 2, datetime.now(timezone.utc))
    assert agent.capture(fresh)["state"] == "CHARGING"


def test_future_telemetry_is_offline_and_not_a_live_low_soc_alert(tmp_path):
    app = create_app(str(tmp_path / "future.db"), "operator-test-key-123", "vehicle-test-key-123")
    profile = demo_profile()
    app.state.store.register(profile)
    future = scenario_frame("SCENARIO_001", 1,
                            datetime.now(timezone.utc) + timedelta(minutes=3))
    app.state.store.ingest([future.model_copy(update={"soc_pct": 5})])
    client = TestClient(app)
    auth = {"X-Operator-Key": "operator-test-key-123"}
    summary = client.get("/api/v1/fleet/summary", headers=auth).json()
    assert summary["online"] == 0 and summary["offline"] == 1
    assert summary["data_quality"] == "INVALID"
    assert summary["low_battery"] == 0
    alerts = client.get("/api/v1/fleet/alerts", headers=auth).json()
    assert any(alert["type"] == "telemetry_invalid" for alert in alerts)
    assert all(alert["type"] != "low_soc" for alert in alerts)
