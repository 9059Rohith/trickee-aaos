"""A network deployment must fail closed before the API begins serving."""

import os

import pytest

from trickee.runtime_config import prepared_environment, validate_deployment


def staging(**overrides):
    values = {
        "TRICKEE_DEPLOYMENT_MODE": "staging",
        "TRICKEE_PUBLIC_URL": "https://fleet.example.test",
        "TRICKEE_DB": "/app/data/cloud.db",
        "TRICKEE_OPERATOR_KEY": "o" * 40,
        "TRICKEE_VEHICLE_KEY": "v" * 40,
        "TRICKEE_PHONE_GPS_RETENTION_DAYS": "30",
    }
    values.update(overrides)
    return values


def test_staging_rejects_demo_http_weak_keys_and_unset_retention():
    for changed in (
        {"TRICKEE_DEMO_MODE": "1"},
        {"TRICKEE_PUBLIC_URL": "http://fleet.example.test"},
        {"TRICKEE_PUBLIC_URL": "https://user:pass@fleet.example.test"},
        {"TRICKEE_OPERATOR_KEY": "weak"},
        {"TRICKEE_VEHICLE_KEY": "v" * 40, "TRICKEE_OPERATOR_KEY": "v" * 40},
        {"TRICKEE_PHONE_GPS_RETENTION_DAYS": ""},
        {"TRICKEE_DB": "data/cloud.db"},
    ):
        with pytest.raises(ValueError):
            validate_deployment(staging(**changed))


def test_staging_accepts_scoped_accounts_without_global_operator_key():
    values = staging(TRICKEE_OPERATOR_ACCOUNTS_JSON='[{"tenant_id":"north"}]')
    del values["TRICKEE_OPERATOR_KEY"]
    validate_deployment(values)


def test_production_requires_scoped_identity_and_rejects_legacy_global_key():
    with pytest.raises(ValueError, match="scoped"):
        validate_deployment(staging(TRICKEE_DEPLOYMENT_MODE="production"))
    values = staging(TRICKEE_DEPLOYMENT_MODE="production",
                     TRICKEE_OPERATOR_ACCOUNTS_JSON='[{"tenant_id":"north"}]')
    del values["TRICKEE_OPERATOR_KEY"]
    validate_deployment(values)


def test_file_secrets_are_loaded_without_leaking_path_or_value(tmp_path):
    secret = tmp_path / "vehicle_key"
    secret.write_text("v" * 40 + "\n", encoding="utf-8")
    values = staging(TRICKEE_VEHICLE_KEY="", TRICKEE_VEHICLE_KEY_FILE=str(secret))
    prepared = prepared_environment(values)
    assert prepared["TRICKEE_VEHICLE_KEY"] == "v" * 40
    assert "TRICKEE_VEHICLE_KEY_FILE" not in prepared
    assert values["TRICKEE_VEHICLE_KEY"] == ""
    with pytest.raises(ValueError) as error:
        prepared_environment(staging(TRICKEE_VEHICLE_KEY_FILE=str(secret)))
    assert str(secret) not in str(error.value)
    assert "v" * 40 not in str(error.value)


def test_missing_empty_or_multiline_secret_fails_closed(tmp_path):
    secret = tmp_path / "secret"
    for content in ("", "first\nsecond", " " * 40):
        secret.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            prepared_environment(staging(TRICKEE_VEHICLE_KEY="", TRICKEE_VEHICLE_KEY_FILE=str(secret)))
    with pytest.raises(ValueError):
        prepared_environment(staging(TRICKEE_VEHICLE_KEY="", TRICKEE_VEHICLE_KEY_FILE=str(tmp_path / "absent")))


def test_local_development_remains_available():
    validate_deployment({"TRICKEE_DEMO_MODE": "1"})
    assert prepared_environment({"TRICKEE_DEMO_MODE": "1"})["TRICKEE_DEMO_MODE"] == "1"


def test_staging_process_exposes_no_unconfigured_fallback(monkeypatch):
    from trickee.api import app_from_environment
    for key in tuple(os.environ):
        if key.startswith("TRICKEE_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("TRICKEE_DEPLOYMENT_MODE", "staging")
    with pytest.raises(ValueError):
        app_from_environment()


def test_file_backed_keys_initialize_api_without_remaining_in_process_environment(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from trickee.api import app_from_environment
    for key in tuple(os.environ):
        if key.startswith("TRICKEE_"):
            monkeypatch.delenv(key, raising=False)
    operator_file = tmp_path / "operator"
    vehicle_file = tmp_path / "vehicle"
    operator_file.write_text("o" * 40, encoding="utf-8")
    vehicle_file.write_text("v" * 40, encoding="utf-8")
    monkeypatch.setenv("TRICKEE_DB", str(tmp_path / "cloud.db"))
    monkeypatch.setenv("TRICKEE_OPERATOR_KEY_FILE", str(operator_file))
    monkeypatch.setenv("TRICKEE_VEHICLE_KEY_FILE", str(vehicle_file))
    app = app_from_environment()
    assert "TRICKEE_OPERATOR_KEY" not in os.environ
    assert "TRICKEE_VEHICLE_KEY" not in os.environ
    assert TestClient(app).get("/api/v1/operator/me", headers={"X-Operator-Key": "o" * 40}).status_code == 200
