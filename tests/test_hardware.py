"""Read-only hardware adapter, provenance, and signal freshness contracts."""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from trickee.agent import VehicleAgent
from trickee.api import create_app
from trickee.hardware import (
    CanReadOnlyAdapter, CanSignalSpec, FakeVehicleHardwareAdapter,
    HardwareSample, ObdReadOnlyAdapter, ReplayVehicleHardwareAdapter,
    SignalGateway, decode_obd_response,
)
from trickee.hardware_ingest import _cloud_sender, capture_loop, load_can_mapping
from trickee.simulator import demo_profile, scenario_frame
from trickee.store import CloudStore


NOW = datetime.now(timezone.utc)


class FakeCanReceiver:
    def __init__(self, frames):
        self.frames = iter(frames)
        self.closed = False

    def receive(self):
        return next(self.frames)

    def close(self):
        self.closed = True


class FakeElmTransport:
    def __init__(self, responses):
        self.responses = responses
        self.queries = []
        self.closed = False

    def query(self, pid):
        self.queries.append(pid)
        return self.responses[pid]

    def close(self):
        self.closed = True


def test_gateway_merges_only_fresh_fields_and_drops_invalid_samples():
    gateway = SignalGateway("V-1", freshness_seconds=2, now=lambda: NOW)
    first = gateway.accept(HardwareSample("CAN", NOW, {"soc_pct": 64, "speed_kmh": 0}))
    assert first.sequence == 1 and first.quality(NOW).value == "OK"
    second = gateway.accept(HardwareSample("CAN", NOW + timedelta(seconds=1), {"speed_kmh": 38}))
    assert second.soc_pct == 64 and second.speed_kmh == 38
    with pytest.raises(ValueError, match="backwards"):
        gateway.accept(HardwareSample("CAN", NOW - timedelta(seconds=1), {"soc_pct": 70}))
    with pytest.raises(ValueError, match="future"):
        gateway.accept(HardwareSample("CAN", NOW + timedelta(minutes=3), {"soc_pct": 70}))
    with pytest.raises(Exception):
        gateway.accept(HardwareSample("CAN", NOW + timedelta(seconds=2), {"soc_pct": 110}))
    assert gateway.next_sequence == 3
    third = gateway.accept(HardwareSample("CAN", NOW + timedelta(seconds=4), {"speed_kmh": 40}))
    assert third.sequence == 3 and third.soc_pct is None


def test_gateway_does_not_mix_real_and_synthetic_sources():
    gateway = SignalGateway("V-1")
    gateway.accept(HardwareSample("SIMULATOR", NOW, {"soc_pct": 90}))
    actual = gateway.accept(HardwareSample("OBD", NOW, {"speed_kmh": 20}))
    assert actual.soc_pct is None and actual.source == "OBD"
    with pytest.raises(ValueError, match="unrecognized"):
        HardwareSample("CAN", NOW, {"control_motor": 1})


def test_gateway_coalesces_asynchronous_gps_and_charging_safely():
    gateway = SignalGateway("V-1")
    first = gateway.accept(HardwareSample("CAN", NOW, {"latitude": 12.5}))
    assert first.latitude is None and first.longitude is None
    second = gateway.accept(HardwareSample("CAN", NOW + timedelta(seconds=1),
                                           {"longitude": 77.6, "speed_kmh": 30}))
    assert second.latitude == 12.5 and second.longitude == 77.6
    charging = gateway.accept(HardwareSample("CAN", NOW + timedelta(seconds=2),
                                             {"charging": True, "charging_power_kw": 25}))
    assert charging.charging and charging.speed_kmh is None
    moving = gateway.accept(HardwareSample("CAN", NOW + timedelta(seconds=3),
                                           {"speed_kmh": 40}))
    assert moving.charging is False and moving.charging_power_kw is None
    with pytest.raises(ValueError, match="conflict"):
        gateway.accept(HardwareSample("CAN", NOW + timedelta(seconds=4),
                                      {"charging": True, "speed_kmh": 50}))


def test_can_mapping_decodes_approved_bytes_and_rejects_invalid_ranges():
    spec_soc = CanSignalSpec(0x234, "soc_pct", 0, 2, scale=0.01)
    spec_speed = CanSignalSpec(0x234, "speed_kmh", 2, 2, scale=0.1)
    receiver = FakeCanReceiver([
        (0x111, b"\x00", NOW),
        (0x234, b"\x10\x27\xfa\x00", NOW),
    ])
    adapter = CanReadOnlyAdapter(receiver, [spec_soc, spec_speed])
    sample = adapter.read()
    assert sample.values == {"soc_pct": 100.0, "speed_kmh": 25.0}
    assert SignalGateway("V-1").accept(sample).soc_pct == 100
    adapter.close()
    assert receiver.closed
    with pytest.raises(ValueError, match="eight-byte"):
        CanSignalSpec(0x234, "soc_pct", 7, 2)
    with pytest.raises(ValueError, match="numeric"):
        CanSignalSpec(0x234, "fault_code", 0, 1)
    with pytest.raises(ValueError, match="shorter"):
        spec_soc.decode(b"\x00")


def test_obd_uses_only_read_allowlist_and_leaves_missing_soc_unknown():
    assert decode_obd_response("010D", "010D\r41 0D 32\r") == ("speed_kmh", 50)
    with pytest.raises(ValueError, match="allowlist"):
        decode_obd_response("015B", "41 5B 80")
    with pytest.raises(ValueError, match="allowlist"):
        decode_obd_response("2F01", "6F01FF")
    transport = FakeElmTransport({"010D": "41 0D 1E"})
    adapter = ObdReadOnlyAdapter(transport)
    frame = SignalGateway("V-1").accept(adapter.read())
    assert frame.speed_kmh == 30 and frame.soc_pct is None
    assert transport.queries == ["010D"]
    adapter.close()
    assert transport.closed
    with pytest.raises(ValueError, match="read-only"):
        ObdReadOnlyAdapter(FakeElmTransport({}), ("2F01",))


def test_fake_and_replay_are_explicitly_synthetic(tmp_path):
    fake = FakeVehicleHardwareAdapter([{"soc_pct": 55}])
    assert fake.read().source == "SIMULATOR"
    record = scenario_frame("SCENARIO_001", 1)
    path = tmp_path / "frames.jsonl"
    path.write_text(record.model_dump_json() + "\n", encoding="utf-8")
    replay = ReplayVehicleHardwareAdapter(path)
    sample = replay.read()
    frame = SignalGateway(record.vehicle_id).accept(sample)
    assert frame.source == "REPLAY"
    assert frame.quality().value == "SIMULATED"
    with pytest.raises(StopIteration):
        replay.read()
    replay.close()


def test_canonical_hardware_frames_flow_through_agent_and_cloud(tmp_path):
    profile = demo_profile("HARDWARE-1")
    agent = VehicleAgent(profile, str(tmp_path / "outbox.db"))
    gateway = SignalGateway(profile.vehicle_id)
    frame = gateway.accept(HardwareSample("CAN", datetime.now(timezone.utc),
                                          {"soc_pct": 68, "speed_kmh": 22,
                                           "ignition": True}))
    assert agent.capture(frame)["quality"] == "OK"
    cloud = CloudStore(str(tmp_path / "cloud.db"))
    cloud.register(profile)
    assert agent.sync(cloud.ingest)["pending"] == 0
    assert cloud.twin(profile.vehicle_id)["quality"] == "OK"
    report = cloud.fleet_report(1)
    assert report["can_frames"] == 1 and report["quality"] == "REPORTED"
    cloud.close()


def test_capture_loop_keeps_outbox_on_outage_then_replays(tmp_path):
    profile = demo_profile("CAPTURE-1")
    agent = VehicleAgent(profile, str(tmp_path / "outbox.db"))
    adapter = FakeVehicleHardwareAdapter([
        {"soc_pct": 72, "speed_kmh": 0, "ignition": False},
        {"soc_pct": 70, "speed_kmh": 20, "ignition": True},
    ])
    calls = 0

    def sender(frames):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("offline")
        return {"acknowledged": [{"vehicle_id": frame.vehicle_id,
                                  "sequence": frame.sequence} for frame in frames]}

    result = capture_loop(adapter, agent, sender=sender, sync_every=1)
    assert result == {"captured": 2, "synchronized": 2, "pending": 0,
                      "sync_error": None}
    assert calls == 2


def test_mapping_rejects_duplicates_and_cloud_sender_rejects_unsafe_url(tmp_path):
    path = tmp_path / "mapping.json"
    path.write_text('{"signals":[{"arbitration_id":291,"field":"soc_pct",'
                    '"start_byte":0,"width_bytes":1}]}', encoding="utf-8")
    assert load_can_mapping(path)[0].arbitration_id == 291
    path.write_text('{"signals":[{"arbitration_id":291,"field":"soc_pct",'
                    '"start_byte":0,"width_bytes":1},'
                    '{"arbitration_id":291,"field":"soc_pct",'
                    '"start_byte":1,"width_bytes":1}]}', encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate"):
        load_can_mapping(path)
    with pytest.raises(ValueError, match="HTTPS origin"):
        _cloud_sender("http://vehicle.example", "key", None)
    with pytest.raises(ValueError, match="HTTPS origin"):
        _cloud_sender("https://vehicle.example/path", "key", None)


def test_read_only_capture_reaches_authenticated_cloud_and_fleet(tmp_path):
    profile = demo_profile("CAN-FLEET-1")
    app = create_app(str(tmp_path / "cloud.db"), "operator-key-12345",
                     "vehicle-master-key-12345")
    client = TestClient(app)
    assert client.post("/api/v1/vehicles", headers={
        "X-Operator-Key": "operator-key-12345"},
        json={"profile": profile.model_dump()}).status_code == 200
    vehicle_key = hmac.new(b"vehicle-master-key-12345", profile.vehicle_id.encode(),
                           hashlib.sha256).hexdigest()
    agent = VehicleAgent(profile, str(tmp_path / "outbox.db"))
    adapter = CanReadOnlyAdapter(FakeCanReceiver([
        (0x123, bytes([70, 0, 0]), datetime.now(timezone.utc)),
        (0x123, bytes([68, 20, 1]), datetime.now(timezone.utc)),
    ]), [CanSignalSpec(0x123, "soc_pct", 0, 1),
         CanSignalSpec(0x123, "speed_kmh", 1, 1),
         CanSignalSpec(0x123, "ignition", 2, 1)])

    def sender(frames):
        response = client.post("/api/v1/telemetry/batches", headers={
            "X-Vehicle-Key": vehicle_key},
            json={"frames": [frame.model_dump(mode="json") for frame in frames]})
        response.raise_for_status()
        return response.json()

    result = capture_loop(adapter, agent, sender=sender, sync_every=2)
    assert result["captured"] == result["synchronized"] == 2
    assert result["pending"] == 0
    twin = client.get("/api/v1/vehicles/CAN-FLEET-1/twin", headers={
        "X-Operator-Key": "operator-key-12345"}).json()
    assert twin["battery"]["soc_pct"] == 68
    assert twin["quality"] == "OK"
    assert client.get("/api/v1/fleet/report", headers={
        "X-Operator-Key": "operator-key-12345"}).json()["can_frames"] == 2
