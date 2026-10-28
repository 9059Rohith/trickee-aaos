from fastapi.testclient import TestClient
import pytest
from trickee.api import create_app


def test_account_access_honest_unconfigured_and_configured(tmp_path, monkeypatch):
    app = create_app(
        str(tmp_path / "access.db"), "operator-key-12345", "vehicle-key-12345"
    )
    with TestClient(app) as client:
        config = client.get("/api/v1/account-access").json()
        assert config["passwords_managed_by"] == "IDENTITY_PROVIDER"
        assert config["recovery_url"] is None
        assert config["email_delivery_verified"] is False
        assert client.get("/account-access").headers["X-Frame-Options"] == "DENY"
    app.state.store.close()
    monkeypatch.setenv(
        "TRICKEE_ACCOUNT_RECOVERY_URL", "https://accounts.example.test/recover"
    )
    app = create_app(
        str(tmp_path / "configured.db"), "operator-key-12345", "vehicle-key-12345"
    )
    assert (
        TestClient(app).get("/api/v1/account-access").json()["recovery_url"]
        == "https://accounts.example.test/recover"
    )
    app.state.store.close()


@pytest.mark.parametrize(
    "url",
    [
        "http://accounts.example.test/reset",
        "javascript:alert(1)",
        "https://user:pass@accounts.test",
        "https://accounts.test/reset#token",
    ],
)
def test_account_access_rejects_unsafe_urls(tmp_path, monkeypatch, url):
    monkeypatch.setenv("TRICKEE_ACCOUNT_RECOVERY_URL", url)
    with pytest.raises(ValueError, match="account access URL"):
        create_app(
            str(tmp_path / "unsafe.db"), "operator-key-12345", "vehicle-key-12345"
        )
