"""Operator audit reads are bounded and isolated by vehicle and tenant."""

import hashlib
import json

from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile


def test_scoped_audit_pagination_and_roles(tmp_path, monkeypatch):
    keys = {"north": "north-audit-admin-123", "south": "south-audit-admin-123",
            "viewer": "north-audit-viewer-123"}
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps([
        {"tenant_id": "north" if name == "viewer" else name,
         "role": "viewer" if name == "viewer" else "admin",
         "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for name, key in keys.items()
    ]))
    app = create_app(str(tmp_path / "audit.db"), "legacy-secret-123", "vehicle-secret-123")
    client = TestClient(app)
    app.state.store.register(demo_profile("NORTH-1"), "DRIVER-N", "north")
    app.state.store.register(demo_profile("SOUTH-1"), "DRIVER-S", "south")
    app.state.store.audit("first", "NORTH-1", "one", "operator:north")
    app.state.store.audit("south-secret", "SOUTH-1", "private", "operator:south")
    app.state.store.audit("second", "NORTH-1", "two", "operator:north")
    app.state.store.audit("global-event", None, "not vehicle scoped", "system")
    north = {"X-Operator-Key": keys["north"]}
    south = {"X-Operator-Key": keys["south"]}
    viewer = {"X-Operator-Key": keys["viewer"]}
    path = "/api/v1/vehicles/NORTH-1/audit"
    assert client.get(path).status_code == 401
    assert client.get(path, headers=south).status_code == 404
    assert client.get(path, headers=viewer).status_code == 403
    assert client.get(path, headers=north, params={"limit": 0}).status_code == 422
    assert client.get(path, headers=north, params={"limit": 101}).status_code == 422
    assert client.get(path, headers=north, params={"before_id": 0}).status_code == 422
    first = client.get(path, headers=north, params={"limit": 1})
    assert first.status_code == 200, first.text
    assert [item["action"] for item in first.json()["events"]] == ["second"]
    assert first.json()["next_before"] is not None
    second = client.get(path, headers=north, params={
        "limit": 1, "before_id": first.json()["next_before"]})
    assert second.status_code == 200
    assert [item["action"] for item in second.json()["events"]] == ["first"]
    assert second.json()["next_before"] is None
    all_events = client.get(path, headers=north).json()["events"]
    assert all(item["action"] not in {"south-secret", "global-event"} for item in all_events)
    assert client.get("/api/v1/vehicles/SOUTH-1/audit", headers=south).json()[
        "events"][0]["action"] == "south-secret"
