"""Remote evaluation must keep scoped credentials and preserve existing test data."""

import hashlib
import json

import pytest

from trickee.remote_evaluation import evaluation_environment, seed_evaluation
from trickee.store import CloudStore


def settings(**changes):
    return {
        "TRICKEE_EVALUATION_MODE": "1",
        "RENDER_EXTERNAL_URL": "https://trickee-review.onrender.com",
        "TRICKEE_EVALUATION_OPERATOR_KEY": "operator-" + "aB9kLm27" * 6,
        "TRICKEE_EVALUATION_DRIVER_KEY": "driver-" + "cD8pQr36" * 6,
        "TRICKEE_VEHICLE_KEY": "vehicle-" + "eF7sTu45" * 6,
        **changes,
    }


def test_evaluation_uses_scoped_operator_and_render_https_origin():
    source = settings()
    result = evaluation_environment(source)
    account, = json.loads(result["TRICKEE_OPERATOR_ACCOUNTS_JSON"])
    assert account["tenant_id"] == "evaluation"
    assert account["key_sha256"] == hashlib.sha256(source["TRICKEE_EVALUATION_OPERATOR_KEY"].encode()).hexdigest()
    assert "TRICKEE_OPERATOR_KEY" not in result
    assert result["TRICKEE_PUBLIC_URL"] == source["RENDER_EXTERNAL_URL"]
    assert result["TRICKEE_DEPLOYMENT_MODE"] == "staging"
    assert result["TRICKEE_DEMO_MODE"] == "0"
    assert "TRICKEE_OPERATOR_ACCOUNTS_JSON" not in source


@pytest.mark.parametrize("changes", [
    {"TRICKEE_EVALUATION_MODE": ""},
    {"TRICKEE_DEPLOYMENT_MODE": "production"},
    {"TRICKEE_OPERATOR_KEY": "legacy-" + "x" * 40},
    {"TRICKEE_EVALUATION_DRIVER_KEY": "short"},
    {"TRICKEE_EVALUATION_DRIVER_KEY": settings()["TRICKEE_VEHICLE_KEY"]},
    {"RENDER_EXTERNAL_URL": "http://trickee-review.onrender.com"},
    {"TRICKEE_DB": "/tmp/evaluation.db"},
    {"TRICKEE_PHONE_GPS_RETENTION_DAYS": "0"},
    {"TRICKEE_DEMO_MODE": "1"},
])
def test_evaluation_rejects_unsafe_configuration_without_exposing_keys(changes):
    source = settings(**changes)
    with pytest.raises(ValueError) as error:
        evaluation_environment(source)
    assert source["TRICKEE_EVALUATION_OPERATOR_KEY"] not in str(error.value)


def test_seed_is_idempotent_and_keeps_driver_credentials_and_user_data(tmp_path):
    database = str(tmp_path / "evaluation.db")
    token = settings()["TRICKEE_EVALUATION_DRIVER_KEY"]
    seed_evaluation(database, token)
    store = CloudStore(database)
    initial = store.driver_by_hash(hashlib.sha256(token.encode()).hexdigest())
    assert initial["tenant_id"] == "evaluation"
    store.audit("evaluation-test", initial["vehicle_id"], "keep this record", "test")
    store.close()
    seed_evaluation(database, token)
    store = CloudStore(database)
    try:
        repeated = store.driver_by_hash(hashlib.sha256(token.encode()).hexdigest())
        assert repeated["id"] == initial["id"]
        assert store.db.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        assert store.db.execute("SELECT count(*) FROM audit WHERE action='evaluation-test'").fetchone()[0] == 1
        payload = json.loads(store.db.execute("SELECT payload_json FROM events").fetchone()[0])
        assert payload["source"] == "SIMULATOR"
    finally:
        store.close()


def test_seed_refuses_to_change_an_existing_assignment(tmp_path):
    database = str(tmp_path / "evaluation.db")
    token = settings()["TRICKEE_EVALUATION_DRIVER_KEY"]
    seed_evaluation(database, token)
    store = CloudStore(database)
    with store.db:
        store.db.execute("UPDATE vehicles SET driver_id='different-driver'")
    store.close()
    with pytest.raises(ValueError, match="assignment"):
        seed_evaluation(database, token)


def test_seed_does_not_restore_a_revoked_driver_credential(tmp_path):
    database = str(tmp_path / "evaluation.db")
    token = settings()["TRICKEE_EVALUATION_DRIVER_KEY"]
    seed_evaluation(database, token)
    store = CloudStore(database)
    identity = store.driver_by_hash(hashlib.sha256(token.encode()).hexdigest())
    assert store.revoke_driver_credential(identity["id"], "evaluation")
    store.close()
    with pytest.raises(ValueError, match="revoked"):
        seed_evaluation(database, token)


def test_seed_recovers_first_start_interrupted_before_credential_creation(tmp_path):
    from trickee.simulator import demo_profile

    database = str(tmp_path / "evaluation.db")
    store = CloudStore(database)
    store.register(demo_profile(), "EVALUATION-DRIVER", "evaluation")
    store.close()
    token = settings()["TRICKEE_EVALUATION_DRIVER_KEY"]
    seed_evaluation(database, token)
    store = CloudStore(database)
    try:
        assert store.driver_by_hash(hashlib.sha256(token.encode()).hexdigest()) is not None
        assert store.twin(demo_profile().vehicle_id) is not None
    finally:
        store.close()
