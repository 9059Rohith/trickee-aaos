"""Operational metrics must be useful without leaking credentials or IDs."""
import hashlib
import json
import logging

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.domain import Quality, SignalFrame
from trickee.observability import RequestMetrics
from trickee.simulator import demo_profile, scenario_frame


def test_request_metrics_are_low_cardinality_and_authenticated(tmp_path, caplog):
    client = TestClient(create_app(str(tmp_path / "cloud.db"),
                                   "operator-key-12345", "vehicle-key-12345"))
    caplog.set_level(logging.INFO, logger="trickee.requests")
    first = client.get("/api/v1/vehicles/private-vehicle-id/twin?secret=never-log",
                       headers={"X-Operator-Key": "operator-key-12345"})
    second = client.get("/health", headers={"X-Operator-Key": "wrong-secret"})
    assert first.status_code == 404 and second.status_code == 200
    assert len(first.headers["x-request-id"]) == 32
    assert first.headers["x-request-id"] != second.headers["x-request-id"]
    assert client.get("/api/v1/ops/metrics").status_code == 401
    observed = client.get("/api/v1/ops/metrics",
                          headers={"X-Operator-Key": "operator-key-12345"})
    assert observed.status_code == 200
    metrics = observed.json()
    assert metrics["total_requests"] >= 3
    assert any(row["route"] == "/api/v1/vehicles/{vehicle_id}/twin"
               for row in metrics["routes"])
    assert metrics["telemetry"]["registered_vehicles"] == 0
    body = json.dumps(metrics) + "\n".join(record.message for record in caplog.records)
    assert "private-vehicle-id" not in body
    assert "never-log" not in body
    assert "operator-key-12345" not in body
    assert "wrong-secret" not in body


def test_scoped_administrator_cannot_read_global_traffic(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([{
        "tenant_id": "north", "role": "admin",
        "key_sha256": hashlib.sha256(b"north-key-123456").hexdigest(),
    }]))
    client = TestClient(create_app(str(tmp_path / "cloud.db"),
                                   "unused-operator-key", "vehicle-key-12345"))
    response = client.get("/api/v1/ops/metrics",
                          headers={"X-Operator-Key": "north-key-123456"})
    assert response.status_code == 403


def test_scoped_administrator_sees_only_tenant_telemetry(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([{
        "tenant_id": "north", "role": "admin",
        "key_sha256": hashlib.sha256(b"north-key-123456").hexdigest(),
    }, {
        "tenant_id": "south", "role": "viewer",
        "key_sha256": hashlib.sha256(b"south-key-123456").hexdigest(),
    }]))
    app = create_app(str(tmp_path / "cloud.db"),
                     "unused-operator-key", "vehicle-key-12345")
    app.state.store.register(demo_profile("NORTH-1"), tenant_id="north")
    app.state.store.register(demo_profile("SOUTH-1"), tenant_id="south")
    app.state.store.ingest([SignalFrame.model_validate({
        **scenario_frame("SCENARIO_001", 1, vehicle_id="SOUTH-1").model_dump(),
        "source": "CAN",
    })])
    client = TestClient(app)
    response = client.get("/api/v1/ops/tenant-metrics",
                          headers={"X-Operator-Key": "north-key-123456"})
    assert response.status_code == 200
    assert response.json() == {
        "tenant_id": "north",
        "telemetry": {
            "registered_vehicles": 1, "fresh_vehicles": 0,
            "quality_counts": {quality.value: int(quality == Quality.MISSING)
                               for quality in Quality},
            "source_counts": {"SIMULATOR": 0, "REPLAY": 0, "CAR_API": 0,
                              "CAN": 0, "OBD": 0},
            "latest_upload_latency_ms_avg": None,
            "latest_upload_latency_sample_count": 0,
        },
    }
    assert client.get("/api/v1/ops/tenant-metrics",
                      headers={"X-Operator-Key": "south-key-123456"}).status_code == 403


def test_histogram_counts_errors_and_bounds():
    metrics = RequestMetrics()
    metrics.record("GET", "/ready", 200, 7)
    metrics.record("GET", "/ready", 500, 55)
    metrics.record("POST", "/api/v1/vehicles/{vehicle_id}", 503, 11000)
    result = metrics.snapshot()
    assert result["total_requests"] == 3
    assert result["server_errors"] == 2
    assert sum(sum(row["latency_buckets"].values()) for row in result["routes"]) == 3


def test_telemetry_metrics_count_missing_and_live_provenance(tmp_path):
    app = create_app(str(tmp_path / "cloud.db"),
                     "operator-key-12345", "vehicle-key-12345")
    app.state.store.register(demo_profile("METRIC-1"))
    app.state.store.register(demo_profile("METRIC-2"))
    frame = scenario_frame("SCENARIO_001", 1, vehicle_id="METRIC-1")
    actual = SignalFrame.model_validate({**frame.model_dump(), "source": "CAN"})
    app.state.store.ingest([actual])
    client = TestClient(app)
    result = client.get("/api/v1/ops/metrics",
                        headers={"X-Operator-Key": "operator-key-12345"}).json()
    telemetry = result["telemetry"]
    assert telemetry["registered_vehicles"] == 2
    assert telemetry["fresh_vehicles"] == 1
    assert telemetry["quality_counts"]["OK"] == 1
    assert telemetry["quality_counts"]["MISSING"] == 1
    assert telemetry["source_counts"]["CAN"] == 1
    assert telemetry["latest_upload_latency_sample_count"] == 1
