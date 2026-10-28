"""OCPI sessions and CDRs are durable and reconciled without billing claims."""

from datetime import datetime, timedelta, timezone

import pytest

from trickee.charging_sessions import ChargingConflict, ChargingSessionStore
from trickee.charging_sessions import sync_ocpi_records
from trickee.store import CloudStore


NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


def session(status="ACTIVE", kwh=4.0, updated=NOW, cost=20.0):
    return {"country_code": "IN", "party_id": "TKE", "id": "S-1",
            "start_date_time": (NOW - timedelta(hours=1)).isoformat(),
            "status": status, "kwh": kwh, "currency": "INR",
            "total_cost": {"before_taxes": cost, "taxes": []}, "last_updated": updated.isoformat()}


def cdr(energy=4.0, cost=20.0, cdr_id="C-1", session_id="S-1"):
    return {"country_code": "IN", "party_id": "TKE", "id": cdr_id,
            "session_id": session_id,
            "start_date_time": (NOW - timedelta(hours=1)).isoformat(),
            "end_date_time": NOW.isoformat(), "currency": "INR",
            "total_energy": energy, "total_cost": {"before_taxes": cost, "taxes": []}}


def test_session_updates_are_idempotent_monotonic_and_durable(tmp_path):
    path = str(tmp_path / "charging.db")
    cloud = CloudStore(path)
    store = ChargingSessionStore(cloud)
    first, changed = store.apply_session(session())
    assert changed and first["status"] == "ACTIVE"
    assert first["reconciliation"] == "SESSION_NOT_COMPLETED"
    assert store.apply_session(session()) == (first, False)
    final, changed = store.apply_session(session("COMPLETED", 5.0, NOW + timedelta(minutes=1), 25))
    assert changed and final["status"] == "COMPLETED"
    with pytest.raises(ChargingConflict):
        store.apply_session(session("ACTIVE", 5.0, NOW + timedelta(minutes=2), 25))
    with pytest.raises(ChargingConflict):
        store.apply_session(session("COMPLETED", 3.0, NOW, 10))
    cloud.close()
    restored = ChargingSessionStore(CloudStore(path))
    assert restored.get("IN", "TKE", "S-1")["status"] == "COMPLETED"


def test_cdr_before_session_reconciles_after_matching_session(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "cdr.db")))
    recorded, changed = store.apply_cdr(cdr())
    assert changed and recorded["reconciliation"] == "SESSION_MISSING"
    store.apply_session(session("COMPLETED", 4.0))
    assert store.get("IN", "TKE", "S-1")["reconciliation"] == "MATCHED"
    assert store.apply_cdr(cdr()) == (store.get_cdr("IN", "TKE", "C-1"), False)
    with pytest.raises(ChargingConflict):
        store.apply_cdr(cdr(cost=30))


@pytest.mark.parametrize("energy,cost,expected", [
    (5.5, 20, "ENERGY_MISMATCH"), (4, 30, "COST_MISMATCH"),
])
def test_reconciliation_flags_mismatches_instead_of_claiming_payment(tmp_path, energy, cost, expected):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "mismatch.db")))
    store.apply_session(session("COMPLETED"))
    result, _ = store.apply_cdr(cdr(energy=energy, cost=cost))
    assert result["reconciliation"] == expected
    assert "payment_status" not in result


def test_unlinked_cdr_and_multiple_cdrs_require_review(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "review.db")))
    unlinked, _ = store.apply_cdr(cdr(cdr_id="C-0", session_id=None))
    assert unlinked["reconciliation"] == "UNLINKED"
    store.apply_session(session("COMPLETED"))
    store.apply_cdr(cdr())
    second, _ = store.apply_cdr(cdr(cdr_id="C-2"))
    assert second["reconciliation"] == "MULTIPLE_CDRS_REVIEW"
    assert store.get("IN", "TKE", "S-1")["reconciliation"] == "MULTIPLE_CDRS_REVIEW"


@pytest.mark.parametrize("invalid", [
    {"kwh": -1}, {"kwh": True}, {"status": "PAID"},
    {"last_updated": "2026-10-10T12:00:00+05:30"},
    {"total_cost": {"before_taxes": -1}},
    {"total_cost": {"incl_vat": 20}},
])
def test_session_rejects_invalid_provider_values(tmp_path, invalid):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "invalid.db")))
    with pytest.raises(ValueError):
        store.apply_session({**session(), **invalid})


def test_ocpi_23_tax_amounts_are_included_in_reconciliation(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "taxes.db")))
    base = session("COMPLETED")
    base["total_cost"] = {"before_taxes": 20, "taxes": [{"name": "GST", "amount": 3.6}]}
    store.apply_session(base)
    detail = cdr()
    detail["total_cost"] = {"before_taxes": 20, "taxes": [{"name": "GST", "amount": 3.6}]}
    matched, _ = store.apply_cdr(detail)
    assert matched["total_cost"] == "23.6"
    assert matched["reconciliation"] == "MATCHED"


def test_ocpi_utc_timestamp_without_designator_and_case_insensitive_ids(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "utc.db")))
    raw = session()
    raw["country_code"] = "in"
    raw["party_id"] = "tke"
    raw["id"] = "s-1"
    raw["last_updated"] = NOW.strftime("%Y-%m-%dT%H:%M:%S")
    record, changed = store.apply_session(raw)
    assert changed and record["session_id"] == "S-1"
    assert store.get("IN", "TKE", "S-1") == record


def test_ocpi_pull_sync_reports_new_and_replayed_records(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "sync.db")))

    class Collection:
        def __init__(self, rows):
            self.rows = rows

        def records(self):
            return self.rows

    sessions = Collection([session("COMPLETED")])
    cdrs = Collection([cdr()])
    assert sync_ocpi_records(store, sessions, cdrs) == {
        "sessions_seen": 1, "sessions_changed": 1, "cdrs_seen": 1,
        "cdrs_changed": 1, "reconciliation": {"MATCHED": 1},
    }
    assert sync_ocpi_records(store, sessions, cdrs)["cdrs_changed"] == 0


def test_ocpi_pull_validates_both_collections_before_writing(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "atomic-invalid.db")))

    class Collection:
        def __init__(self, rows):
            self.rows = rows

        def records(self):
            return self.rows

    with pytest.raises(ValueError):
        sync_ocpi_records(store, Collection([session("COMPLETED")]),
                          Collection([cdr(), {**cdr(cdr_id="C-2"), "total_energy": -1}]))
    assert store.get("IN", "TKE", "S-1") is None
    assert store.get_cdr("IN", "TKE", "C-1") is None


def test_completed_session_accepts_later_cost_correction(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "correction.db")))
    store.apply_session(session("COMPLETED", cost=20))
    store.apply_cdr(cdr(cost=21))
    assert store.get("IN", "TKE", "S-1")["reconciliation"] == "COST_MISMATCH"
    corrected, changed = store.apply_session(session("COMPLETED", cost=21,
                                                    updated=NOW + timedelta(minutes=1)))
    assert changed and corrected["reconciliation"] == "MATCHED"
    with pytest.raises(ChargingConflict):
        store.apply_session(session("COMPLETED", cost=19, updated=NOW))


def test_credit_cdr_is_negative_and_forces_adjustment_review(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "credit.db")))
    store.apply_session(session("COMPLETED"))
    store.apply_cdr(cdr())
    credit = {**cdr(cdr_id="C-1-C"), "credit": True,
              "credit_reference_id": "C-1",
              "total_cost": {"before_taxes": -20, "taxes": []}}
    record, changed = store.apply_cdr(credit)
    assert changed and record["total_cost"] == "-20"
    assert record["credit_reference_id"] == "C-1"
    assert record["reconciliation"] == "CREDIT_ADJUSTMENT_REVIEW"
    assert store.get("IN", "TKE", "S-1")["reconciliation"] == "CREDIT_ADJUSTMENT_REVIEW"


def test_credit_cdr_requires_valid_reference_and_negative_cost(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "invalid-credit.db")))
    for invalid in (
        {"credit": True, "total_cost": {"before_taxes": -20}},
        {"credit": True, "credit_reference_id": "C-1",
         "total_cost": {"before_taxes": 20}},
        {"credit": False, "credit_reference_id": "C-1"},
    ):
        with pytest.raises(ValueError):
            store.apply_cdr({**cdr(cdr_id="C-2"), **invalid})


def test_credit_arriving_before_original_is_flagged_until_original_arrives(tmp_path):
    store = ChargingSessionStore(CloudStore(str(tmp_path / "credit-order.db")))
    store.apply_session(session("COMPLETED"))
    credit = {**cdr(cdr_id="C-1-C"), "credit": True,
              "credit_reference_id": "C-1",
              "total_cost": {"before_taxes": -20}}
    first, _ = store.apply_cdr(credit)
    assert first["reconciliation"] == "CREDIT_REFERENCE_MISSING"
    store.apply_cdr(cdr())
    assert store.get_cdr("IN", "TKE", "C-1-C")["reconciliation"] == "CREDIT_ADJUSTMENT_REVIEW"
