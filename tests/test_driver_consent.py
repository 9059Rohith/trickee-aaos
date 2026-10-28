"""Driver trip-phone consent is explicit, scoped and enforced at ingestion."""

from datetime import datetime, timezone
import hashlib
import json

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile


def _client(tmp_path):
    client = TestClient(create_app(str(tmp_path / "consent.db"), "operator-secret-123", "vehicle-secret-123"))
    operator = {"X-Operator-Key": "operator-secret-123"}
    assert client.post("/api/v1/vehicles", headers=operator, json={
        "profile": demo_profile().model_dump(), "driver_id": "DRIVER-1",
    }).status_code == 200
    token = client.post("/api/v1/vehicles/TRICKEE-DEMO-1/driver-credentials", headers=operator).json()["token"]
    return client, {"X-Driver-Key": token}


def _batch(batch_id, timestamp):
    return {"batch_id": batch_id, "points": [{"sequence": int(batch_id[-1]),
            "timestamp": timestamp, "lat": 12.9716, "lng": 77.5946, "accuracy": 8}]}


def test_trip_phone_consent_default_deny_withdrawal_and_regrant(tmp_path):
    client, driver = _client(tmp_path)
    url = "/api/v1/driver/consent/phone-trip"
    gps = "/api/v1/driver/trips/CONSENT-1/gps-batches"
    assert client.post("/api/v1/driver/trips", headers=driver,
                       json={"trip_id": "CONSENT-1", "idempotency_key": "start"}).status_code == 200
    assert client.get(url, headers=driver).json()["granted"] is False
    assert client.get(url).status_code == 401
    assert client.post(url, headers=driver, json={"granted": "yes"}).status_code == 422
    assert client.post(gps, headers=driver, json=_batch("B1", datetime.now(timezone.utc).isoformat())).status_code == 403

    before_grant = datetime.now(timezone.utc).isoformat()
    granted = client.post(url, headers=driver, json={"granted": True})
    assert granted.status_code == 200 and granted.json()["granted"] is True
    assert client.post(gps, headers=driver, json=_batch("B4", before_grant)).status_code == 403
    first_event_count = client.app.state.store.db.execute("SELECT COUNT(*) FROM driver_consent_events").fetchone()[0]
    assert client.post(url, headers=driver, json={"granted": True}).status_code == 200
    assert client.app.state.store.db.execute("SELECT COUNT(*) FROM driver_consent_events").fetchone()[0] == first_event_count
    queued_before_withdrawal = datetime.now(timezone.utc).isoformat()
    assert client.post(gps, headers=driver, json=_batch("B1", queued_before_withdrawal)).status_code == 200
    assert client.post(url, headers=driver, json={"granted": False}).json()["granted"] is False
    while_withdrawn = datetime.now(timezone.utc).isoformat()
    assert client.post(gps, headers=driver, json=_batch("B2", while_withdrawn)).status_code == 403
    # A duplicate acknowledged before withdrawal has no new data to process.
    assert client.post(gps, headers=driver, json=_batch("B1", queued_before_withdrawal)).status_code == 200
    assert client.post(url, headers=driver, json={"granted": True}).status_code == 200
    assert client.post(gps, headers=driver, json=_batch("B2", while_withdrawn)).status_code == 403
    assert client.post(gps, headers=driver, json=_batch("B3", queued_before_withdrawal)).status_code == 200
    actions = [row[0] for row in client.app.state.store.db.execute(
        "SELECT action FROM audit WHERE action LIKE 'driver_phone_consent_%' ORDER BY id")]
    assert actions == ["driver_phone_consent_granted", "driver_phone_consent_withdrawn",
                       "driver_phone_consent_granted"]


def test_consent_is_scoped_by_tenant_and_driver(tmp_path, monkeypatch):
    keys = {"north": "north-secret-123456", "south": "south-secret-123456"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": tenant, "role": "admin", "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for tenant, key in keys.items()
    ]))
    client = TestClient(create_app(str(tmp_path / "tenants.db"), "legacy-secret-123", "vehicle-secret-123"))
    tokens = {}
    for tenant, vehicle_id in (("north", "CAR-N"), ("south", "CAR-S")):
        operator = {"X-Operator-Key": keys[tenant]}
        profile = demo_profile().model_copy(update={"vehicle_id": vehicle_id})
        assert client.post("/api/v1/vehicles", headers=operator,
                           json={"profile": profile.model_dump(), "driver_id": "SAME-DRIVER"}).status_code == 200
        tokens[tenant] = {"X-Driver-Key": client.post(
            f"/api/v1/vehicles/{vehicle_id}/driver-credentials", headers=operator).json()["token"]}
    url = "/api/v1/driver/consent/phone-trip"
    assert client.post(url, headers=tokens["north"], json={"granted": True}).status_code == 200
    assert client.get(url, headers=tokens["north"]).json()["granted"] is True
    assert client.get(url, headers=tokens["south"]).json()["granted"] is False
