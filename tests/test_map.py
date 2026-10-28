"""Map configuration keeps demo tiles separate from production tiles."""

from fastapi.testclient import TestClient
import pytest

from trickee.api import create_app


OPERATOR = {"X-Operator-Key": "test-operator-key-123"}


def test_map_configuration_requires_auth_and_does_not_default_to_public_tiles(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    monkeypatch.delenv("TRICKEE_MAP_TILE_URL", raising=False)
    app = create_app(str(tmp_path / "cloud.db"), OPERATOR["X-Operator-Key"], "test-vehicle-key-123")
    client = TestClient(app)
    assert client.get("/api/v1/map/config").status_code == 401
    response = client.get("/api/v1/map/config", headers=OPERATOR)
    assert response.status_code == 200
    assert response.json() == {"tile_url": None, "attribution": None, "quality": "UNAVAILABLE"}


def test_map_configuration_allows_demo_tiles_or_explicit_https_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_DEMO_MODE", "1")
    app = create_app(str(tmp_path / "demo.db"), OPERATOR["X-Operator-Key"], "test-vehicle-key-123")
    demo = TestClient(app).get("/api/v1/map/config", headers=OPERATOR).json()
    assert demo["tile_url"] == "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
    assert "OpenStreetMap" in demo["attribution"]
    assert demo["quality"] == "DEMO"

    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    monkeypatch.setenv("TRICKEE_MAP_TILE_URL", "https://tiles.example.test/{z}/{x}/{y}.png")
    monkeypatch.setenv("TRICKEE_MAP_ATTRIBUTION", "Licensed tiles")
    app = create_app(str(tmp_path / "configured.db"), OPERATOR["X-Operator-Key"], "test-vehicle-key-123")
    configured = TestClient(app).get("/api/v1/map/config", headers=OPERATOR).json()
    assert configured == {
        "tile_url": "https://tiles.example.test/{z}/{x}/{y}.png",
        "attribution": "Licensed tiles",
        "quality": "CONFIGURED",
    }


def test_map_configuration_rejects_insecure_or_uncredited_tiles(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    monkeypatch.setenv("TRICKEE_MAP_ATTRIBUTION", "Licensed tiles")
    monkeypatch.setenv("TRICKEE_MAP_TILE_URL", "http://tiles.example.test/{z}/{x}/{y}.png")
    with pytest.raises(ValueError, match="HTTPS"):
        create_app(str(tmp_path / "insecure.db"), OPERATOR["X-Operator-Key"], "test-vehicle-key-123")
    monkeypatch.setenv("TRICKEE_MAP_TILE_URL", "https://tiles.example.test/{z}/{x}/{y}.png")
    monkeypatch.delenv("TRICKEE_MAP_ATTRIBUTION", raising=False)
    with pytest.raises(ValueError, match="attribution"):
        create_app(str(tmp_path / "uncredited.db"), OPERATOR["X-Operator-Key"], "test-vehicle-key-123")
    monkeypatch.setenv("TRICKEE_MAP_ATTRIBUTION", "Licensed tiles")
    monkeypatch.setenv("TRICKEE_MAP_TILE_URL", "https://user:password@tiles.example.test/{z}/{x}/{y}.png")
    with pytest.raises(ValueError, match="credentials"):
        create_app(str(tmp_path / "credentials.db"), OPERATOR["X-Operator-Key"], "test-vehicle-key-123")


def test_console_allows_origin_referrer_for_attributed_demo_tiles(tmp_path, monkeypatch):
    monkeypatch.setenv("TRICKEE_DEMO_MODE", "1")
    app = create_app(str(tmp_path / "demo.db"), OPERATOR["X-Operator-Key"], "test-vehicle-key-123")
    client = TestClient(app)
    assert client.get("/").headers["referrer-policy"] == "strict-origin-when-cross-origin"
    assert client.get("/api/v1/map/config", headers=OPERATOR).headers["referrer-policy"] == "no-referrer"
