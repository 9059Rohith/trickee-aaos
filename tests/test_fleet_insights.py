"""Fleet insights are derived from stored frames with visible provenance."""

import hashlib
import hmac
from datetime import datetime

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile, scenario_frame


OP = {"X-Operator-Key": "test-operator-key-123"}
VEHICLE = {"X-Vehicle-Key": hmac.new(b"test-vehicle-key-123", b"TRICKEE-DEMO-1", hashlib.sha256).hexdigest()}


def test_empty_report_has_unknown_averages_and_missing_telemetry_alert(tmp_path):
    client = TestClient(create_app(str(tmp_path / "empty.db"), OP["X-Operator-Key"], "test-vehicle-key-123"))
    assert client.post("/api/v1/vehicles", headers=OP, json={"profile": demo_profile().model_dump()}).status_code == 200
    assert client.get("/api/v1/fleet/report").status_code == 401
    report = client.get("/api/v1/fleet/report?days=7", headers=OP).json()
    assert report["telemetry_frames"] == 0
    assert report["average_soc_pct"] is None
    assert report["average_speed_kmh"] is None
    assert report["quality"] == "MISSING"
    assert report["driver_score"] is None
    alerts = client.get("/api/v1/fleet/alerts", headers=OP).json()
    assert len(alerts) == 1
    assert alerts[0]["type"] == "telemetry_missing"
    assert alerts[0]["quality"] == "MISSING"


def test_report_and_alerts_use_real_stored_signal_values(tmp_path):
    client = TestClient(create_app(str(tmp_path / "signals.db"), OP["X-Operator-Key"], "test-vehicle-key-123"))
    client.post("/api/v1/vehicles", headers=OP, json={"profile": demo_profile().model_dump(), "driver_id": "DRIVER-1"})
    first = scenario_frame("SCENARIO_002", 1).model_dump(mode="json")
    second = scenario_frame("SCENARIO_007", 2).model_dump(mode="json")
    assert client.post("/api/v1/telemetry/batches", headers=VEHICLE, json={"frames": [first, second]}).status_code == 200
    report = client.get("/api/v1/fleet/report?days=7", headers=OP).json()
    assert report["telemetry_frames"] == 2
    assert report["average_soc_pct"] == 38
    assert report["low_soc_frames"] == 1
    assert report["simulated_frames"] == 2
    assert report["quality"] == "SIMULATED"
    assert report["driver_score"] is None
    assert report["vehicles"][0]["vehicle_id"] == "TRICKEE-DEMO-1"
    alerts = client.get("/api/v1/fleet/alerts", headers=OP).json()
    assert {alert["type"] for alert in alerts} >= {"health_critical"}
    assert all(alert["vehicle_id"] == "TRICKEE-DEMO-1" for alert in alerts)
    expected_at = datetime.fromisoformat(second["observed_at"].replace("Z", "+00:00"))
    assert all(datetime.fromisoformat(alert["observed_at"]) == expected_at for alert in alerts)
    assert client.get("/api/v1/fleet/report?days=0", headers=OP).status_code == 422
