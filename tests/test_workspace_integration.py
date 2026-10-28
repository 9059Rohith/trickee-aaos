import time
import hashlib
import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from trickee.api import create_app
from trickee.simulator import demo_profile


def test_integrated_file_content_retains_sandbox_and_auth(tmp_path):
    app = create_app(
        str(tmp_path / "integration.db"), "integration-operator", "integration-vehicle"
    )
    headers = {"X-Operator-Key": "integration-operator"}
    with TestClient(app) as client:
        base = "/api/v1/operator/workspace/files"
        assert (
            client.post(
                base,
                params={"filename": "notes.txt", "request_id": "integration-file-001"},
                content=b"notes",
            ).status_code
            == 401
        )
        saved = client.post(
            base,
            headers=headers,
            params={"filename": "notes.txt", "request_id": "integration-file-001"},
            content=b"notes",
        )
        assert saved.status_code == 201
        url = base + "/" + saved.json()["file_id"] + "/preview"
        assert client.get(url).status_code == 401
        response = client.get(url, headers=headers)
        assert response.status_code == 200
        assert "sandbox" in response.headers["content-security-policy"]
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
        assert response.headers["cache-control"] == "no-store"
    app.state.store.close()


def test_integrated_files_and_navigation_use_real_tenant_role_and_driver_auth(
    tmp_path, monkeypatch
):
    accounts = [
        dict(
            tenant_id=tenant,
            role=role,
            actor_id="bob" if role == "viewer" else "alice",
            key_sha256=hashlib.sha256(key.encode()).hexdigest(),
        )
        for tenant, role, key in [
            ("north", "admin", "north-admin-key"),
            ("north", "viewer", "north-viewer-key"),
            ("south", "admin", "south-admin-key"),
        ]
    ]
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps(accounts))
    app = create_app(
        str(tmp_path / "scopes.db"), "legacy-operator-key", "vehicle-master-key"
    )
    app.state.store.register(demo_profile(), "driver-alice", "north")
    token = "driver-integration-token-123456789"
    credential = app.state.store.issue_driver_credential(
        "TRICKEE-DEMO-1", hashlib.sha256(token.encode()).hexdigest()
    )
    admin = {"X-Operator-Key": "north-admin-key"}
    with TestClient(app) as client:
        base = "/api/v1/operator/workspace"
        file = client.post(
            base + "/files",
            headers=admin,
            params={"filename": "private.txt", "request_id": "scoped-upload-001"},
            content=b"owner notes",
        ).json()
        payload = {
            "revision": 0,
            "favorites": [{"section_id": "plans", "label": "Plans"}],
        }
        assert (
            client.put(base + "/navigation", headers=admin, json=payload).status_code
            == 200
        )
        for key in ("north-viewer-key", "south-admin-key"):
            headers = {"X-Operator-Key": key}
            assert client.get(base + "/files", headers=headers).json()["total"] == 0
            assert (
                client.get(
                    base + "/files/" + file["file_id"] + "/download", headers=headers
                ).status_code
                == 404
            )
            assert (
                client.get(base + "/navigation", headers=headers).json()["favorites"]
                == []
            )
        driver = {"X-Driver-Key": token}
        assert client.get(base + "/navigation", headers=driver).status_code == 401
        driver_base = "/api/v1/driver/workspace"
        assert client.get(driver_base + "/files", headers=driver).json()["total"] == 0
        assert (
            client.put(
                driver_base + "/navigation", headers=driver, json=payload
            ).status_code
            == 200
        )
        assert app.state.store.revoke_driver_credential(
            int(credential["credential_id"].removeprefix("D-")), "north"
        )
        assert (
            client.get(driver_base + "/navigation", headers=driver).status_code == 401
        )
        assert client.get(driver_base + "/files", headers=driver).status_code == 401
    app.state.store.close()


def test_lifespan_worker_generates_reminders_without_workspace_refresh(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("TRICKEE_WORKSPACE_REMINDER_SECONDS", "1")
    app = create_app(
        str(tmp_path / "worker.db"), "integration-operator", "integration-vehicle"
    )
    start = datetime.now(timezone.utc) - timedelta(minutes=1)
    with TestClient(app) as client:
        result = client.post(
            "/api/v1/operator/workspace/events",
            headers={"X-Operator-Key": "integration-operator"},
            json={
                "title": "Background generation",
                "starts_at": start.isoformat(),
                "ends_at": (start + timedelta(hours=1)).isoformat(),
                "timezone": "UTC",
                "remind_minutes": 0,
            },
        )
        assert result.status_code == 200
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            with app.state.store._write_lock:
                count = app.state.store.db.execute(
                    "SELECT count(*) FROM personal_notices"
                ).fetchone()[0]
            if count:
                break
            time.sleep(0.05)
        assert count == 1
        worker = app.state.workspace_reminder_worker
        assert worker.thread.is_alive()
        worker.stop()
        worker.last_error = "controlled error"
        assert client.get("/ready").status_code == 503
    assert not worker.thread.is_alive()
    app.state.store.close()
