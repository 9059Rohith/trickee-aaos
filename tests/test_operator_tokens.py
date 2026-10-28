"""Scoped operator access tokens are checked before any tenant role is granted."""

import json
import time

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from trickee.api import app_from_environment, create_app
from trickee.simulator import demo_profile


def token_client(tmp_path, monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))
    jwk["kid"] = "test-key"
    jwk["use"] = "sig"
    monkeypatch.setattr(jwt.PyJWKClient, "fetch_data", lambda _self: {"keys": [jwk]})
    monkeypatch.setenv("TRICKEE_OIDC_ISSUER", "https://id.example.test/")
    monkeypatch.setenv("TRICKEE_OIDC_AUDIENCE", "trickee-api")
    monkeypatch.setenv("TRICKEE_OIDC_JWKS_URL", "https://id.example.test/keys")
    monkeypatch.setenv("TRICKEE_OIDC_SUBJECTS_JSON", json.dumps({
        "alice": {"tenant_id": "north", "role": "admin"},
        "bob": {"tenant_id": "south", "role": "viewer"},
    }))
    client = TestClient(create_app(str(tmp_path / "tokens.db"), "legacy-global-key-123", "vehicle-master-key-123"))

    def issue(subject="alice", *, audience="trickee-api", issuer="https://id.example.test/",
              kind="at+jwt", expires=600, signing_key=key, algorithm="RS256"):
        now = int(time.time())
        return jwt.encode({"iss": issuer, "aud": audience, "sub": subject, "iat": now,
                           "exp": now + expires, "jti": "test-jti", "client_id": "test-client"},
                          signing_key, algorithm=algorithm, headers={"typ": kind, "kid": "test-key"})

    return client, issue


def test_signed_access_token_is_scoped_and_audited(tmp_path, monkeypatch):
    client, issue = token_client(tmp_path, monkeypatch)
    alice = {"Authorization": "Bearer " + issue()}
    bob = {"Authorization": "Bearer " + issue("bob")}
    vehicle = demo_profile().model_dump()
    assert client.post("/api/v1/vehicles", headers=alice, json={"profile": vehicle, "driver_id": "DRIVER-N"}).status_code == 200
    assert client.get("/api/v1/vehicles", headers=bob).json() == []
    assert client.get("/api/v1/vehicles/TRICKEE-DEMO-1/twin", headers=bob).status_code == 404
    assert client.post("/api/v1/vehicles", headers=bob, json={"profile": vehicle}).status_code == 403
    row = client.app.state.store.db.execute("SELECT actor_id FROM audit WHERE action='vehicle_registered'").fetchone()
    assert row["actor_id"] == "oidc:alice"


def test_access_token_rejects_id_token_wrong_audience_expiry_and_unapproved_subject(tmp_path, monkeypatch):
    client, issue = token_client(tmp_path, monkeypatch)
    forged_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    for label, token in (("type", issue(kind="JWT")), ("audience", issue(audience="another-api")),
                         ("issuer", issue(issuer="https://wrong.example.test/")),
                         ("expiry", issue(expires=-100)), ("subject", issue(subject="unknown")),
                         ("signature", issue(signing_key=forged_key)),
                         ("algorithm", issue(signing_key="attacker-secret-with-long-test-key", algorithm="HS256"))):
        assert client.get("/api/v1/vehicles", headers={"Authorization": "Bearer " + token}).status_code == 401, label
    assert client.get("/api/v1/vehicles", headers={"Authorization": "Bearer bad-token"}).status_code == 401
    assert client.get("/api/v1/vehicles", headers={"Authorization": "Bearer " + issue(),
                                                   "X-Operator-Key": "legacy-global-key-123"}).status_code == 400


def test_partial_oidc_configuration_fails_closed(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_OIDC_ISSUER", "https://id.example.test/")
    try:
        create_app(str(tmp_path / "broken.db"), "legacy-global-key-123", "vehicle-master-key-123")
    except ValueError as exc:
        assert "OIDC" in str(exc)
    else:
        raise AssertionError("partial OIDC config was accepted")


def test_token_only_environment_has_no_known_legacy_key(tmp_path, monkeypatch):
    _, issue = token_client(tmp_path, monkeypatch)
    monkeypatch.delenv("TRICKEE_OPERATOR_KEY", raising=False)
    monkeypatch.delenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", raising=False)
    monkeypatch.setenv("TRICKEE_VEHICLE_KEY", "vehicle-master-key-1234567890123456")
    monkeypatch.setenv("TRICKEE_DB", str(tmp_path / "token-only.db"))
    client = TestClient(app_from_environment())
    assert client.get("/api/v1/vehicles", headers={"X-Operator-Key": "scoped-operator-disabled"}).status_code == 401
    assert client.get("/api/v1/vehicles", headers={"Authorization": "Bearer " + issue()}).status_code == 200
