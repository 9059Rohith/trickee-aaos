"""Contract tests for a partner order mirror that never auto-assigns a driver."""

from datetime import datetime, timedelta, timezone

import httpx
import pytest
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.partner_orders import (PartnerOrderBadResponse, PartnerOrderClient,
                                    PartnerOrderConflict, PartnerOrderStore, sync_partner_orders)
from trickee.simulator import demo_profile
from trickee.store import CloudStore


NOW = datetime(2026, 10, 11, 12, tzinfo=timezone.utc)


def order(order_id="EXT-1", updated_at=NOW.isoformat(), status="CREATED", vehicle_id=None):
    return {"id": order_id, "updated_at": updated_at, "status": status,
            "pickup": {"latitude": 12.9716, "longitude": 77.5946},
            "destination": {"latitude": 12.9916, "longitude": 77.6146},
            "pickup_label": "MG Road", "destination_label": "Indiranagar",
            "vehicle_id": vehicle_id}


def test_partner_feed_pagination_and_authorization():
    requests = []

    def serve(request):
        requests.append(request)
        assert request.headers["Authorization"] == "Bearer private-partner-token"
        if "cursor" not in request.url.params:
            return httpx.Response(200, json={"orders": [order()], "next_cursor": "page-2"})
        assert request.url.params["cursor"] == "page-2"
        return httpx.Response(200, json={"orders": [order("EXT-2")], "next_cursor": None})

    client = PartnerOrderClient("https://orders.example.test/v1/orders", "private-partner-token",
                                httpx.MockTransport(serve))
    assert [row.id for row in client.fetch_all()] == ["EXT-1", "EXT-2"]
    assert len(requests) == 2


def test_partner_feed_rejects_unsafe_or_invalid_data():
    with pytest.raises(ValueError):
        PartnerOrderClient("http://orders.example.test/orders", "token")
    with pytest.raises(ValueError):
        PartnerOrderClient("https://user:pass@orders.example.test/orders", "token")
    with pytest.raises(ValueError):
        PartnerOrderClient("https://orders.example.test/orders?key=secret", "token")
    for body in (
        {"orders": [order(status="UNKNOWN")], "next_cursor": None},
        {"orders": [order(), order()], "next_cursor": None},
        {"orders": [order(updated_at="2026-10-11T12:00:00")], "next_cursor": None},
        {"orders": [], "next_cursor": "bad cursor"},
    ):
        client = PartnerOrderClient("https://orders.example.test/orders", "token",
                                    httpx.MockTransport(lambda _: httpx.Response(200, json=body)))
        with pytest.raises(PartnerOrderBadResponse):
            client.fetch_all()
    looping = PartnerOrderClient("https://orders.example.test/orders", "token",
                                 httpx.MockTransport(lambda _: httpx.Response(200, json={
                                     "orders": [], "next_cursor": "same-page"})))
    with pytest.raises(PartnerOrderBadResponse, match="cursor"):
        looping.fetch_all()


def test_partner_mirror_replay_stale_conflict_and_tenant_scope(tmp_path):
    cloud = CloudStore(str(tmp_path / "partner.db"))
    cloud.register(demo_profile("CAR-N"), "DRIVER-N", "north")
    mirror = PartnerOrderStore(cloud)
    initial = mirror.apply("north", "marketplace", [order(vehicle_id="CAR-N")])
    assert initial == {"inserted": 1, "updated": 0, "unchanged": 0, "stale": 0}
    assert mirror.apply("north", "marketplace", [order(vehicle_id="CAR-N")])["unchanged"] == 1
    newer = order(updated_at=(NOW + timedelta(minutes=1)).isoformat(), status="ASSIGNED", vehicle_id="CAR-N")
    assert mirror.apply("north", "marketplace", [newer])["updated"] == 1
    assert mirror.apply("north", "marketplace", [order(vehicle_id="CAR-N")])["stale"] == 1
    row = mirror.list_for_operator("north")[0]
    assert row["status"] == "ASSIGNED" and row["vehicle_match"] == "MATCHED"
    assert row["fulfilment_quality"] == "PROVIDER_MIRROR_UNVERIFIED"
    assert mirror.list_for_operator("south") == []
    assert cloud.db.execute("SELECT COUNT(*) FROM client_changes WHERE kind='PARTNER_ORDER_UPDATED'").fetchone()[0] == 4
    assert cloud.db.execute("SELECT name FROM sqlite_master WHERE name='internal_orders'").fetchone() is None
    with pytest.raises(PartnerOrderConflict):
        mirror.apply("north", "marketplace", [order(updated_at=newer["updated_at"], status="CANCELLED")])
    assert mirror.list_for_operator("north")[0]["status"] == "ASSIGNED"
    with pytest.raises(PartnerOrderConflict):
        mirror.apply("north", "marketplace", [order("EXT-2"),
                     order(updated_at=newer["updated_at"], status="CANCELLED")])
    assert [row["external_id"] for row in mirror.list_for_operator("north")] == ["EXT-1"]
    assert mirror.list_for_operator("*")[0]["tenant_id"] == "north"
    cloud.close()


def test_partner_sync_and_operator_api_are_scoped(tmp_path, monkeypatch):
    import hashlib
    import json

    accounts = [
        {"tenant_id": tenant, "role": "admin", "key_sha256": hashlib.sha256(key.encode()).hexdigest()}
        for tenant, key in (("north", "north-partner-key-123"), ("south", "south-partner-key-123"))
    ]
    monkeypatch.setenv("TRICKEE_OPERATOR_ACCOUNTS_JSON", json.dumps(accounts))
    db = str(tmp_path / "api.db")
    app = create_app(db, "legacy-secret-123", "vehicle-secret-123")
    client = TestClient(app)
    assert client.get("/api/v1/partner-orders").status_code == 401
    config = {"TRICKEE_DB": db, "TRICKEE_ORDER_FEED_URL": "https://orders.example.test/orders",
              "TRICKEE_ORDER_FEED_TOKEN": "private-partner-token", "TRICKEE_ORDER_FEED_SOURCE": "marketplace",
              "TRICKEE_ORDER_FEED_TENANT_ID": "north"}
    transport = httpx.MockTransport(lambda _: httpx.Response(200, json={"orders": [order()], "next_cursor": None}))
    assert sync_partner_orders(config, transport)["inserted"] == 1
    assert len(client.get("/api/v1/partner-orders", headers={"X-Operator-Key": "north-partner-key-123"}).json()) == 1
    assert client.get("/api/v1/partner-orders", headers={"X-Operator-Key": "south-partner-key-123"}).json() == []


def test_partner_sync_validates_config_before_creating_database(tmp_path):
    db = tmp_path / "absent.db"
    config = {"TRICKEE_DB": str(db), "TRICKEE_ORDER_FEED_URL": "http://insecure.example/orders",
              "TRICKEE_ORDER_FEED_TOKEN": "secret", "TRICKEE_ORDER_FEED_SOURCE": "marketplace",
              "TRICKEE_ORDER_FEED_TENANT_ID": "north"}
    with pytest.raises(ValueError):
        sync_partner_orders(config, httpx.MockTransport(lambda _: httpx.Response(200)))
    assert not db.exists()
    token_file = tmp_path / "partner.token"
    token_file.write_text("secret\n", encoding="utf-8")
    config["TRICKEE_ORDER_FEED_URL"] = "https://orders.example.test/orders"
    config.pop("TRICKEE_ORDER_FEED_TOKEN")
    config["TRICKEE_ORDER_FEED_TOKEN_FILE"] = str(token_file)
    served = httpx.MockTransport(lambda request: httpx.Response(200, json={
        "orders": [order()], "next_cursor": None}))
    assert sync_partner_orders(config, served)["inserted"] == 1
