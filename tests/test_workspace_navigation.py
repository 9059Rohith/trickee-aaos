import json

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from trickee.store import CloudStore


@pytest.fixture
def navigation(tmp_path):
    from trickee.workspace_navigation import install_workspace_navigation

    cloud = CloudStore(str(tmp_path / "navigation.db"))
    api = FastAPI()

    def alice():
        return {"key": json.dumps(["operator", "north", "alice", "admin"])}

    def bob():
        return {"key": json.dumps(["driver", "north", "bob", "vehicle-2"])}

    install_workspace_navigation(api, cloud, alice, bob)
    with TestClient(api) as client:
        yield client, cloud
    cloud.close()


def state(**changes):
    return {
        "revision": 0,
        "favorites": [],
        "saved_views": [],
        "onboarding_step": 0,
        "onboarding_completed": False,
        **changes,
    }


def test_navigation_persistence_optimistic_revision_and_owner_isolation(navigation):
    client, cloud = navigation
    base = "/api/v1/operator/workspace/navigation"
    assert client.get(base).json() == state()
    payload = state(
        favorites=[{"section_id": "workspace-section-4", "label": "Trips"}],
        saved_views=[
            {
                "view_id": "view-one",
                "name": "Maintenance",
                "view": "week",
                "query": "connector",
            }
        ],
        onboarding_step=4,
        onboarding_completed=True,
    )
    saved = client.put(base, json=payload)
    assert saved.status_code == 200
    assert saved.json() == {**payload, "revision": 1}
    assert client.get(base).json() == saved.json()
    assert client.get("/api/v1/driver/workspace/navigation").json() == state()
    assert client.put(base, json=payload).status_code == 409
    assert (
        cloud.db.execute("SELECT count(*) FROM personal_navigation").fetchone()[0] == 1
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"owner": "bob"},
        {"favorites": [{"section_id": "javascript:alert(1)", "label": "Bad"}]},
        {"favorites": [{"section_id": "../admin", "label": "Bad"}]},
        {"favorites": [{"section_id": "trips", "label": "Trips"}] * 2},
        {
            "saved_views": [
                {"view_id": "view-one", "name": "Bad", "view": "unknown", "query": ""}
            ]
        },
        {"onboarding_step": 100},
        {"onboarding_completed": True},
    ],
)
def test_navigation_rejects_unsafe_duplicate_and_inconsistent_state(
    navigation, changes
):
    client, _ = navigation
    assert (
        client.put(
            "/api/v1/operator/workspace/navigation", json=state(**changes)
        ).status_code
        == 422
    )


def test_navigation_retains_saved_views_after_database_reopen(tmp_path):
    from trickee.workspace_navigation import NavigationState, NavigationStore

    path = str(tmp_path / "durable.db")
    first = CloudStore(path)
    nav = NavigationStore(first)
    nav.save(
        "owner",
        NavigationState(**state(favorites=[{"section_id": "plans", "label": "Plans"}])),
    )
    first.close()
    second = CloudStore(path)
    assert NavigationStore(second).read("owner")["favorites"][0]["label"] == "Plans"
    second.close()
