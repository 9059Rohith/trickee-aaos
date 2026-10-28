"""Bounded HTTPS partner-order pull and tenant-scoped, read-only mirror.

The feed contract is opt-in. A mirrored record is not a dispatch proposal,
assignment, delivery proof, or payment. Operators must still use the reviewed
internal workflow until a particular provider contract is accepted.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from .runtime_config import prepared_environment
from .store import CloudStore


IDENTIFIER = r"^[A-Za-z0-9_-]{1,64}$"


class PartnerOrderBadResponse(ValueError):
    pass


class PartnerOrderUnavailable(RuntimeError):
    pass


class PartnerOrderConflict(ValueError):
    pass


class Position(BaseModel):
    model_config = ConfigDict(extra="forbid")
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class PartnerOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=IDENTIFIER)
    updated_at: datetime
    status: str = Field(pattern=r"^(CREATED|ASSIGNED|PICKED_UP|COMPLETED|CANCELLED)$")
    pickup: Position
    destination: Position
    pickup_label: str = Field(min_length=1, max_length=120)
    destination_label: str = Field(min_length=1, max_length=120)
    vehicle_id: str | None = Field(default=None, pattern=IDENTIFIER)

    @field_validator("updated_at")
    @classmethod
    def valid_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("partner order timestamp must carry a timezone")
        return value.astimezone(timezone.utc)


class PartnerPage(BaseModel):
    model_config = ConfigDict(extra="forbid")
    orders: list[PartnerOrder] = Field(max_length=100)
    next_cursor: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,128}$")


class PartnerOrderClient:
    def __init__(self, url: str, token: str, transport: httpx.BaseTransport | None = None):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.query or parsed.fragment or not parsed.path):
            raise ValueError("partner order feed requires an HTTPS URL without embedded credentials")
        if not token or token != token.strip() or "\n" in token or "\r" in token:
            raise ValueError("partner order feed token is invalid")
        self.url = url
        self.token = token
        self.transport = transport

    def fetch_all(self, max_pages: int = 10) -> list[PartnerOrder]:
        if not 1 <= max_pages <= 10:
            raise ValueError("partner order page limit is invalid")
        orders: list[PartnerOrder] = []
        seen_ids: set[str] = set()
        seen_cursors: set[str] = set()
        cursor: str | None = None
        with httpx.Client(timeout=8, follow_redirects=False, transport=self.transport) as client:
            for _ in range(max_pages):
                try:
                    response = client.get(self.url, params={"cursor": cursor} if cursor else None,
                                          headers={"Authorization": f"Bearer {self.token}",
                                                   "Accept": "application/json"})
                except httpx.HTTPError as exc:
                    raise PartnerOrderUnavailable("partner order feed unavailable") from None
                if response.status_code != 200:
                    raise PartnerOrderUnavailable("partner order feed unavailable")
                if len(response.content) > 512_000:
                    raise PartnerOrderBadResponse("partner order page exceeds size limit")
                try:
                    page = PartnerPage.model_validate(response.json())
                except (ValueError, ValidationError):
                    raise PartnerOrderBadResponse("partner order page violates the configured contract") from None
                for row in page.orders:
                    if row.id in seen_ids:
                        raise PartnerOrderBadResponse("partner order feed repeated an order")
                    seen_ids.add(row.id)
                    orders.append(row)
                cursor = page.next_cursor
                if cursor is None:
                    return orders
                if cursor in seen_cursors:
                    raise PartnerOrderBadResponse("partner order feed repeated a page cursor")
                seen_cursors.add(cursor)
        raise PartnerOrderBadResponse("partner order feed exceeded the page limit")


class PartnerOrderStore:
    """Mirror provider reports without changing driver-facing internal orders."""

    def __init__(self, cloud: CloudStore):
        self.cloud = cloud
        self.cloud.db.executescript("""
            CREATE TABLE IF NOT EXISTS partner_orders (
                tenant_id TEXT NOT NULL, source TEXT NOT NULL, external_id TEXT NOT NULL,
                updated_at TEXT NOT NULL, status TEXT NOT NULL, payload_json TEXT NOT NULL,
                payload_sha256 TEXT NOT NULL, received_at TEXT NOT NULL,
                PRIMARY KEY(tenant_id,source,external_id)
            );
            CREATE INDEX IF NOT EXISTS partner_orders_recent
                ON partner_orders(tenant_id,updated_at DESC,external_id DESC);
        """)

    @staticmethod
    def _validate_scope(tenant_id: str, source: str) -> None:
        if (re.fullmatch(IDENTIFIER, tenant_id) is None
                or re.fullmatch(IDENTIFIER, source) is None):
            raise ValueError("partner order tenant or source is invalid")

    def apply(self, tenant_id: str, source: str,
              incoming: Sequence[PartnerOrder | dict]) -> dict[str, int]:
        self._validate_scope(tenant_id, source)
        if len(incoming) > 1000:
            raise ValueError("partner order batch exceeds limit")
        try:
            rows = [item if isinstance(item, PartnerOrder) else PartnerOrder.model_validate(item)
                    for item in incoming]
        except ValidationError as exc:
            raise PartnerOrderBadResponse("partner order violates the configured contract") from None
        ids = [row.id for row in rows]
        if len(set(ids)) != len(ids):
            raise PartnerOrderBadResponse("partner order batch repeats an ID")
        counts = {"inserted": 0, "updated": 0, "unchanged": 0, "stale": 0}
        received = datetime.now(timezone.utc).isoformat(timespec="microseconds")
        with self.cloud._write_lock, self.cloud.db:
            for row in rows:
                payload = json.dumps(row.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
                digest = hashlib.sha256(payload.encode()).hexdigest()
                updated_at = row.updated_at.isoformat(timespec="microseconds")
                existing = self.cloud.db.execute(
                    "SELECT updated_at,payload_sha256 FROM partner_orders "
                    "WHERE tenant_id=? AND source=? AND external_id=?",
                    (tenant_id, source, row.id),
                ).fetchone()
                if existing:
                    if updated_at < existing["updated_at"]:
                        counts["stale"] += 1
                        continue
                    if updated_at == existing["updated_at"]:
                        if digest != existing["payload_sha256"]:
                            raise PartnerOrderConflict("partner order changed without a newer timestamp")
                        counts["unchanged"] += 1
                        continue
                    self.cloud.db.execute(
                        "UPDATE partner_orders SET updated_at=?,status=?,payload_json=?,payload_sha256=?,received_at=? "
                        "WHERE tenant_id=? AND source=? AND external_id=?",
                        (updated_at, row.status, payload, digest, received, tenant_id, source, row.id),
                    )
                    counts["updated"] += 1
                else:
                    self.cloud.db.execute(
                        "INSERT INTO partner_orders(tenant_id,source,external_id,updated_at,status,"
                        "payload_json,payload_sha256,received_at) VALUES(?,?,?,?,?,?,?,?)",
                        (tenant_id, source, row.id, updated_at, row.status, payload, digest, received),
                    )
                    counts["inserted"] += 1
                self.cloud.publish_change(tenant_id, row.vehicle_id or "", None,
                                          "PARTNER_ORDER_UPDATED", source + ":" + row.id, received)
        return counts

    def list_for_operator(self, tenant_id: str, limit: int = 100) -> list[dict]:
        if tenant_id != "*" and re.fullmatch(IDENTIFIER, tenant_id) is None:
            raise ValueError("operator tenant is invalid")
        if not 1 <= limit <= 100:
            raise ValueError("partner order limit is invalid")
        clause = "" if tenant_id == "*" else "WHERE tenant_id=?"
        rows = self.cloud.db.execute(
            "SELECT * FROM partner_orders " + clause +
            " ORDER BY updated_at DESC,external_id DESC LIMIT ?",
            (limit,) if tenant_id == "*" else (tenant_id, limit),
        ).fetchall()
        results = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            vehicle_id = payload.get("vehicle_id")
            vehicle = self.cloud.db.execute(
                "SELECT tenant_id FROM vehicles WHERE vehicle_id=?", (vehicle_id,),
            ).fetchone() if vehicle_id else None
            results.append({
                "tenant_id": row["tenant_id"], "source": row["source"],
                "external_id": row["external_id"], "updated_at": row["updated_at"],
                "received_at": row["received_at"], "status": row["status"],
                "pickup": payload["pickup"], "destination": payload["destination"],
                "pickup_label": payload["pickup_label"],
                "destination_label": payload["destination_label"],
                "vehicle_id": vehicle_id,
                "vehicle_match": ("MATCHED" if vehicle and vehicle["tenant_id"] == row["tenant_id"]
                                  else "UNMATCHED" if vehicle_id else "UNASSIGNED"),
                "fulfilment_quality": "PROVIDER_MIRROR_UNVERIFIED",
            })
        return results


def sync_partner_orders(config: Mapping[str, str],
                        transport: httpx.BaseTransport | None = None) -> dict[str, int]:
    values = prepared_environment(config)
    required = ("TRICKEE_DB", "TRICKEE_ORDER_FEED_URL", "TRICKEE_ORDER_FEED_TOKEN",
                "TRICKEE_ORDER_FEED_SOURCE", "TRICKEE_ORDER_FEED_TENANT_ID")
    if any(not values.get(name) for name in required) or values["TRICKEE_DB"] == ":memory:":
        raise ValueError("partner order sync configuration is incomplete")
    client = PartnerOrderClient(values["TRICKEE_ORDER_FEED_URL"], values["TRICKEE_ORDER_FEED_TOKEN"], transport)
    PartnerOrderStore._validate_scope(values["TRICKEE_ORDER_FEED_TENANT_ID"], values["TRICKEE_ORDER_FEED_SOURCE"])
    fetched = client.fetch_all()
    cloud = CloudStore(values["TRICKEE_DB"])
    try:
        return PartnerOrderStore(cloud).apply(values["TRICKEE_ORDER_FEED_TENANT_ID"],
                                              values["TRICKEE_ORDER_FEED_SOURCE"], fetched)
    finally:
        cloud.close()


def main() -> int:
    try:
        print(json.dumps(sync_partner_orders(os.environ), sort_keys=True))
        return 0
    except (ValueError, PartnerOrderUnavailable) as exc:
        print(f"Partner order sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
