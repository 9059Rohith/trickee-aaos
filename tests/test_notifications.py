"""Vehicle notices are fresh, durable, deduplicated, and tenant isolated."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.domain import SignalFrame
from trickee.simulator import demo_profile
from trickee.store import CloudStore, Outbox


def frame(vehicle: str, sequence: int, **values) -> SignalFrame:
    return SignalFrame(vehicle_id=vehicle, sequence=sequence,
                       observed_at=datetime.now(timezone.utc), source="CAN",
                       **values)


def test_low_battery_escalates_once_per_episode_and_recovers(tmp_path):
    cloud = CloudStore(str(tmp_path / "cloud.db"))
    cloud.register(demo_profile("NOTICE-1"), tenant_id="north")
    for item in (frame("NOTICE-1", 1, soc_pct=18),
                 frame("NOTICE-1", 2, soc_pct=17),
                 frame("NOTICE-1", 3, soc_pct=9),
                 frame("NOTICE-1", 4, soc_pct=8),
                 frame("NOTICE-1", 5, soc_pct=30),
                 frame("NOTICE-1", 6, soc_pct=15)):
        cloud.ingest([item])
    notices = list(reversed(cloud.notifications.list_for_tenant("north")))
    assert [notice["priority"] for notice in notices] == [
        "WARNING", "CRITICAL", "WARNING"]
    assert {notice["kind"] for notice in notices} == {"LOW_BATTERY"}
    assert cloud.notifications.list_for_tenant("south") == []
    cloud.close()
    reopened = CloudStore(str(tmp_path / "cloud.db"))
    assert len(reopened.notifications.list_for_tenant("north")) == 3


def test_partial_stale_and_replay_frames_do_not_make_or_clear_notices(tmp_path):
    cloud = CloudStore(str(tmp_path / "cloud.db"))
    cloud.register(demo_profile("NOTICE-2"))
    cloud.ingest([frame("NOTICE-2", 1, soc_pct=8)])
    cloud.ingest([frame("NOTICE-2", 2, speed_kmh=30)])
    cloud.ingest([frame("NOTICE-2", 3, soc_pct=7)])
    stale = frame("NOTICE-2", 4, soc_pct=3).model_copy(update={
        "observed_at": datetime.now(timezone.utc) - timedelta(hours=1)})
    cloud.ingest([stale])
    replay = frame("NOTICE-2", 5, soc_pct=2).model_copy(update={"source": "REPLAY"})
    cloud.ingest([replay])
    assert len(cloud.notifications.list_for_tenant("*")) == 1


def test_higher_sequence_with_older_observation_cannot_trigger_notice(tmp_path):
    cloud = CloudStore(str(tmp_path / "cloud.db"))
    cloud.register(demo_profile("NOTICE-CLOCK"))
    first = frame("NOTICE-CLOCK", 1, soc_pct=65)
    cloud.ingest([first])
    reversed_clock = frame("NOTICE-CLOCK", 2, soc_pct=4).model_copy(update={
        "observed_at": first.observed_at - timedelta(seconds=10)})
    assert cloud.ingest([reversed_clock])["accepted"] == 1
    assert cloud.notifications.list_for_tenant("*") == []


def test_fault_maintenance_and_charging_completion_transition(tmp_path):
    cloud = CloudStore(str(tmp_path / "cloud.db"))
    cloud.register(demo_profile("NOTICE-3"))
    cloud.ingest([frame("NOTICE-3", 1, fault_code="BMS_WARN", soh_pct=72,
                        charging=True, soc_pct=90)])
    cloud.ingest([frame("NOTICE-3", 2, charging=False, soc_pct=97)])
    cloud.ingest([frame("NOTICE-3", 3, charging=False, soc_pct=97)])
    notices = cloud.notifications.list_for_tenant("*")
    assert {notice["kind"] for notice in notices} == {
        "VEHICLE_FAULT", "MAINTENANCE", "CHARGING_COMPLETE"}
    assert len(notices) == 3


def test_outbox_preserves_sparse_signal_presence(tmp_path):
    outbox = Outbox(str(tmp_path / "outbox.db"))
    outbox.append(frame("NOTICE-4", 1, speed_kmh=20))
    payload = outbox.pending()[0]
    assert "soc_pct" not in payload.model_fields_set
    assert "charging" not in payload.model_fields_set
    assert payload.speed_kmh == 20


def test_duplicate_frame_with_expanded_defaults_is_idempotent(tmp_path):
    cloud = CloudStore(str(tmp_path / "cloud.db"))
    cloud.register(demo_profile("NOTICE-6"))
    sparse = frame("NOTICE-6", 1, speed_kmh=20)
    assert cloud.ingest([sparse])["accepted"] == 1
    expanded = SignalFrame.model_validate(sparse.model_dump(mode="json"))
    assert cloud.ingest([expanded])["duplicate"] == 1


def test_notification_api_tenant_scope_acknowledgement_and_roles(tmp_path, monkeypatch):
    keys = (("north", "admin", "north-admin-secret-123"),
            ("south", "admin", "south-admin-secret-123"),
            ("north", "viewer", "north-viewer-secret-123"))
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": tenant, "role": role,
         "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for tenant, role, key in keys]))
    app = create_app(str(tmp_path / "cloud.db"), "unused-operator-key", "vehicle-key-12345")
    app.state.store.register(demo_profile("NOTICE-5"), tenant_id="north")
    app.state.store.ingest([frame("NOTICE-5", 1, soc_pct=7)])
    client = TestClient(app)
    north = {"X-Operator-Key": "north-admin-secret-123"}
    south = {"X-Operator-Key": "south-admin-secret-123"}
    viewer = {"X-Operator-Key": "north-viewer-secret-123"}
    notice = client.get("/api/v1/fleet/notifications", headers=north).json()[0]
    assert notice["tenant_id"] == "north"
    assert client.get("/api/v1/fleet/notifications", headers=south).json() == []
    path = f"/api/v1/fleet/notifications/{notice['id']}/acknowledge"
    assert client.post(path, headers=south).status_code == 404
    assert client.post(path, headers=viewer).status_code == 403
    acknowledged = client.post(path, headers=north)
    assert acknowledged.status_code == 200
    assert acknowledged.json()["acknowledged_at"] is not None
    assert client.post(path, headers=north).json()["acknowledged_at"] == acknowledged.json()["acknowledged_at"]
