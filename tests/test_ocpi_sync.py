"""Operator OCPI sync command contract and durable integration."""

import json
import hashlib
from datetime import datetime, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from trickee.charging_sessions import ChargingSessionStore
from trickee.api import create_app
from trickee.store import CloudStore


NOW = datetime(2026, 10, 10, 12, tzinfo=timezone.utc).isoformat()


def _session():
    return {"country_code": "IN", "party_id": "TKE", "id": "S-1",
            "start_date_time": NOW, "last_updated": NOW, "status": "COMPLETED",
            "kwh": 4, "currency": "INR", "total_cost": {"before_taxes": 20}}


def _cdr():
    return {"country_code": "IN", "party_id": "TKE", "id": "C-1",
            "session_id": "S-1", "start_date_time": NOW, "end_date_time": NOW,
            "total_energy": 4, "currency": "INR", "total_cost": {"before_taxes": 20}}


def _env(tmp_path):
    return {"TRICKEE_DB": str(tmp_path / "ocpi.db"),
            "TRICKEE_OCPI_SESSIONS_URL": "https://ocpi.example/2.3.0/sessions",
            "TRICKEE_OCPI_CDRS_URL": "https://ocpi.example/2.3.0/cdrs",
            "TRICKEE_OCPI_TOKEN": "secret-cpo-token",
            "TRICKEE_OCPI_TENANT_ID": "north"}


def _response(request):
    assert request.headers["Authorization"] == "Token secret-cpo-token"
    records = [_session()] if request.url.path.endswith("sessions") else [_cdr()]
    return httpx.Response(200, json={"status_code": 1000, "timestamp": NOW,
                                     "data": records})


def test_operator_sync_persists_reconciled_records_and_replay(tmp_path):
    from trickee.ocpi_sync import run_sync

    env = _env(tmp_path)
    transport = httpx.MockTransport(_response)
    first = run_sync(env, transport)
    assert first["sessions_changed"] == first["cdrs_changed"] == 1
    assert first["reconciliation"] == {"MATCHED": 1}
    assert "secret-cpo-token" not in json.dumps(first)
    second = run_sync(env, transport)
    assert second["sessions_changed"] == second["cdrs_changed"] == 0
    cloud = CloudStore(env["TRICKEE_DB"])
    assert ChargingSessionStore(cloud).get("IN", "TKE", "S-1")["reconciliation"] == "MATCHED"
    scoped = ChargingSessionStore(cloud).list_for_tenant("north", now=datetime(2026, 10, 11, tzinfo=timezone.utc))
    assert scoped["sessions"][0]["session_id"] == "S-1"
    assert scoped["cdrs"][0]["cdr_id"] == "C-1"
    assert scoped["vehicle_assignment"] == "UNAVAILABLE"
    assert ChargingSessionStore(cloud).list_for_tenant("south")["sessions"] == []
    cloud.close()


@pytest.mark.parametrize("missing", ["TRICKEE_DB", "TRICKEE_OCPI_SESSIONS_URL",
                                      "TRICKEE_OCPI_CDRS_URL", "TRICKEE_OCPI_TOKEN",
                                      "TRICKEE_OCPI_TENANT_ID"])
def test_operator_sync_rejects_missing_config_before_creating_db(tmp_path, missing):
    from trickee.ocpi_sync import run_sync

    env = _env(tmp_path)
    env.pop(missing)
    with pytest.raises(ValueError):
        run_sync(env, httpx.MockTransport(_response))
    assert not (tmp_path / "ocpi.db").exists()


def test_operator_sync_rejects_insecure_provider_before_creating_db(tmp_path):
    from trickee.ocpi_sync import run_sync

    env = _env(tmp_path)
    env["TRICKEE_OCPI_CDRS_URL"] = "http://ocpi.example/cdrs"
    with pytest.raises(ValueError):
        run_sync(env, httpx.MockTransport(_response))
    assert not (tmp_path / "ocpi.db").exists()


def test_operator_sync_does_not_persist_valid_session_if_cdr_is_invalid(tmp_path):
    from trickee.ocpi_sync import run_sync

    env = _env(tmp_path)

    def invalid_cdr(request):
        result = _response(request)
        if request.url.path.endswith("cdrs"):
            record = _cdr()
            record["total_energy"] = -1
            result = httpx.Response(200, json={"status_code": 1000,
                                               "timestamp": NOW, "data": [record]})
        return result

    with pytest.raises(ValueError):
        run_sync(env, httpx.MockTransport(invalid_cdr))
    cloud = CloudStore(env["TRICKEE_DB"])
    assert ChargingSessionStore(cloud).get("IN", "TKE", "S-1") is None
    cloud.close()


def test_operator_sync_rolls_back_earlier_records_on_late_cdr_conflict(tmp_path):
    from trickee.ocpi_sync import run_sync

    env = _env(tmp_path)
    assert run_sync(env, httpx.MockTransport(_response))["cdrs_changed"] == 1

    def conflicting_replay(request):
        if request.url.path.endswith("sessions"):
            extra = {**_session(), "id": "S-2"}
            records = [_session(), extra]
        else:
            changed = {**_cdr(), "total_cost": {"before_taxes": 21}}
            records = [changed]
        return httpx.Response(200, json={"status_code": 1000,
                                         "timestamp": NOW, "data": records})

    with pytest.raises(ValueError, match="immutable"):
        run_sync(env, httpx.MockTransport(conflicting_replay))
    cloud = CloudStore(env["TRICKEE_DB"])
    mirror = ChargingSessionStore(cloud)
    assert mirror.get("IN", "TKE", "S-2") is None
    assert mirror.list_for_tenant("north")["sessions"][0]["session_id"] == "S-1"
    assert mirror.get_cdr("IN", "TKE", "C-1")["total_cost"] == "20"
    cloud.close()


def test_sync_rejects_other_tenant_claim_before_mutating_records(tmp_path):
    from trickee.ocpi_sync import run_sync

    env = _env(tmp_path)
    transport = httpx.MockTransport(_response)
    assert run_sync(env, transport)["sessions_changed"] == 1
    env["TRICKEE_OCPI_TENANT_ID"] = "south"
    with pytest.raises(ValueError, match="another tenant"):
        run_sync(env, transport)
    cloud = CloudStore(env["TRICKEE_DB"])
    store = ChargingSessionStore(cloud)
    assert store.list_for_tenant("north")["sessions"][0]["session_id"] == "S-1"
    assert store.list_for_tenant("south")["sessions"] == []
    cloud.close()


def test_scoped_operator_api_hides_unclaimed_and_other_tenant_records(tmp_path, monkeypatch):
    keys = {"north": "north-charging-admin-123", "south": "south-charging-admin-123",
            "viewer": "north-charging-viewer-123"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": "north" if name == "viewer" else name,
         "role": "viewer" if name == "viewer" else "admin",
         "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for name, key in keys.items()
    ]))
    app = create_app(str(tmp_path / "scoped-ocpi.db"), "legacy-secret-123", "vehicle-secret-123")
    client = TestClient(app)
    mirror = app.state.charging_records
    mirror.apply_session(_session())
    mirror.apply_cdr(_cdr())
    path = "/api/v1/charging/records"
    north = {"X-Operator-Key": keys["north"]}
    south = {"X-Operator-Key": keys["south"]}
    viewer = {"X-Operator-Key": keys["viewer"]}
    assert client.get(path).status_code == 401
    assert client.get(path, headers=viewer).status_code == 403
    assert client.get(path, headers=north, params={"limit": 0}).status_code == 422
    assert client.get(path, headers=north).json()["sessions"] == []
    mirror.claim_scope("north", "SESSION", "IN", "TKE", "S-1")
    mirror.claim_scope("north", "CDR", "IN", "TKE", "C-1")
    visible = client.get(path, headers=north)
    assert visible.status_code == 200
    assert visible.json()["sessions"][0]["reconciliation"] == "MATCHED"
    assert visible.json()["sessions"][0]["freshness"] == "HISTORICAL"
    assert visible.json()["cdrs"][0]["total_cost"] == "20"
    assert client.get(path, headers=south).json()["sessions"] == []
    assert client.get(path, headers=south).json()["cdrs"] == []
    assert "secret-cpo-token" not in visible.text


def test_cross_tenant_session_reference_does_not_leak_reconciliation(tmp_path):
    mirror = ChargingSessionStore(CloudStore(str(tmp_path / "cross-scope.db")))
    session, _ = mirror.apply_session(_session())
    cdr, _ = mirror.apply_cdr(_cdr())
    mirror.claim_scope("north", "SESSION", "IN", "TKE", "S-1")
    mirror.claim_scope("south", "CDR", "IN", "TKE", "C-1")
    assert mirror.list_for_tenant("north")["sessions"][0]["reconciliation"] == "CDR_MISSING"
    assert mirror.list_for_tenant("south")["cdrs"][0]["reconciliation"] == "SESSION_UNAVAILABLE"
    with pytest.raises(ValueError, match="another tenant"):
        mirror.assert_scope_available("north", [session], [cdr])
    with pytest.raises(ValueError, match="another tenant"):
        mirror.assert_scope_available("south", [session], [cdr])


def test_active_session_freshness_does_not_treat_old_or_future_as_current(tmp_path):
    mirror = ChargingSessionStore(CloudStore(str(tmp_path / "freshness.db")))
    active = _session()
    active["status"] = "ACTIVE"
    mirror.apply_session(active)
    mirror.claim_scope("north", "SESSION", "IN", "TKE", "S-1")
    assert mirror.list_for_tenant("north", now=datetime(2026, 10, 10, 12, 30,
                                                        tzinfo=timezone.utc))["sessions"][0]["freshness"] == "STALE"
    assert mirror.list_for_tenant("north", now=datetime(2026, 10, 10, 11, 50,
                                                        tzinfo=timezone.utc))["sessions"][0]["freshness"] == "FUTURE_TIMESTAMP"
