from fastapi.testclient import TestClient

from trickee.api import create_app


def test_health_mode_and_database_readiness(tmp_path, monkeypatch):
    monkeypatch.delenv("TRICKEE_DEMO_MODE", raising=False)
    app = create_app(str(tmp_path / "cloud.db"), "test-operator-key-123", "test-vehicle-key-123")
    client = TestClient(app)
    assert client.get("/health").json()["mode"] == "CONFIGURED"
    assert client.get("/ready").json()["status"] == "ready"
    with app.state.store.db as database:
        database.execute("DROP TABLE vehicles")
    assert client.get("/health").status_code == 200
    unavailable = client.get("/ready")
    assert unavailable.status_code == 503
    assert unavailable.json()["detail"] == "database is not ready"

    monkeypatch.setenv("TRICKEE_DEMO_MODE", "1")
    demo = TestClient(create_app(str(tmp_path / "demo.db"),
                                 "test-operator-key-456", "test-vehicle-key-456"))
    assert demo.get("/health").json()["mode"] == "DEMO"
    assert demo.get("/ready").status_code == 200
