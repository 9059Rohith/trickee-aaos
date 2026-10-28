"""Durable, manually supervised dispatch proposals.

These records are operator-to-driver requests. They are not partner orders, vehicle
commands, trip starts, or evidence that a pickup or delivery took place.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import CloudStore


class DispatchConflict(ValueError):
    """A replay or state transition conflicts with a stored proposal."""


class DispatchNotFound(KeyError):
    """No proposal is visible in the caller's scope."""


def _utc(now: datetime | None = None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None:
        raise ValueError("dispatch clock must be timezone-aware")
    return value.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


def _fingerprint(payload: dict[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class DispatchStore:
    """SQLite proposal state machine attached to a CloudStore connection.

    The CloudStore write lock and SQLite transaction are shared with vehicle
    assignment reads so each command sees one coherent local assignment.
    """

    def __init__(self, cloud: CloudStore):
        self.cloud = cloud
        self.cloud.db.executescript("""
            CREATE TABLE IF NOT EXISTS dispatch_offers (
                offer_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                vehicle_id TEXT NOT NULL,
                driver_id TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                create_key TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('PENDING','ACCEPTED','DECLINED','CANCELLED')),
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                seen_at TEXT,
                responded_at TEXT,
                response_key TEXT,
                response_fingerprint TEXT,
                cancelled_at TEXT,
                cancel_key TEXT,
                cancellation_note TEXT,
                created_by TEXT NOT NULL,
                response_actor TEXT,
                cancelled_by TEXT,
                UNIQUE(vehicle_id,create_key)
            );
            CREATE INDEX IF NOT EXISTS dispatch_by_tenant
                ON dispatch_offers(tenant_id,created_at DESC,offer_id DESC);
            CREATE INDEX IF NOT EXISTS dispatch_by_driver
                ON dispatch_offers(tenant_id,vehicle_id,driver_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS dispatch_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                offer_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN
                    ('CREATED','SEEN','ACCEPTED','DECLINED','CANCELLED')),
                occurred_at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(offer_id) REFERENCES dispatch_offers(offer_id)
            );
            CREATE INDEX IF NOT EXISTS dispatch_events_by_offer
                ON dispatch_events(offer_id,event_id);
        """)

    @staticmethod
    def _visible(row: Any, now: datetime | None = None) -> dict[str, Any]:
        current = _utc(now)
        status = row["status"]
        if status == "PENDING" and datetime.fromisoformat(row["expires_at"]) <= current:
            status = "EXPIRED"
        payload = json.loads(row["payload_json"])
        return {
            "offer_id": row["offer_id"],
            "tenant_id": row["tenant_id"],
            "vehicle_id": row["vehicle_id"],
            "driver_id": row["driver_id"],
            "source": "MANUAL_OPERATOR_PROPOSAL",
            "status": status,
            "pickup": payload["pickup"],
            "destination": payload["destination"],
            "pickup_label": payload["pickup_label"],
            "destination_label": payload["destination_label"],
            "message": payload["message"],
            "external_reference": payload.get("external_reference"),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "seen_at": row["seen_at"],
            "responded_at": row["responded_at"],
            "cancelled_at": row["cancelled_at"],
            "cancellation_note": row["cancellation_note"],
            "created_by": row["created_by"],
            "response_actor": row["response_actor"],
            "cancelled_by": row["cancelled_by"],
        }

    def _event(self, offer_id: str, event_type: str, at: str, actor: str,
               detail: str = "") -> None:
        self.cloud.db.execute(
            "INSERT INTO dispatch_events(offer_id,event_type,occurred_at,actor_id,detail) "
            "VALUES(?,?,?,?,?)", (offer_id, event_type, at, actor, detail),
        )
        row = self.cloud.db.execute(
            "SELECT tenant_id,vehicle_id,driver_id FROM dispatch_offers WHERE offer_id=?",
            (offer_id,),
        ).fetchone()
        self.cloud.publish_change(row["tenant_id"], row["vehicle_id"], row["driver_id"],
                                  "DISPATCH_" + event_type, offer_id, at)

    def _operator_row(self, tenant_id: str, vehicle_id: str, offer_id: str):
        row = self.cloud.db.execute(
            "SELECT * FROM dispatch_offers WHERE offer_id=? AND vehicle_id=? AND "
            "(?='*' OR tenant_id=?)",
            (offer_id, vehicle_id, tenant_id, tenant_id),
        ).fetchone()
        if row is None:
            raise DispatchNotFound("dispatch proposal not found")
        return row

    def _driver_row(self, tenant_id: str, vehicle_id: str, driver_id: str, offer_id: str):
        row = self.cloud.db.execute(
            "SELECT * FROM dispatch_offers WHERE offer_id=? AND tenant_id=? "
            "AND vehicle_id=? AND driver_id=?",
            (offer_id, tenant_id, vehicle_id, driver_id),
        ).fetchone()
        if row is None:
            raise DispatchNotFound("dispatch proposal not found")
        return row

    def create(self, tenant_id: str, vehicle_id: str, offer_id: str,
               create_key: str, payload: dict[str, Any], actor_id: str,
               now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        current = _utc(now)
        expiry_minutes = payload["expires_in_minutes"]
        expires_at = _iso(current + timedelta(minutes=expiry_minutes))
        stored_payload = {key: value for key, value in payload.items()
                          if key != "expires_in_minutes"}
        fingerprint = _fingerprint(payload)
        with self.cloud._write_lock, self.cloud.db:
            vehicle = self.cloud.db.execute(
                "SELECT tenant_id,driver_id FROM vehicles WHERE vehicle_id=?",
                (vehicle_id,),
            ).fetchone()
            if vehicle is None or tenant_id != "*" and vehicle["tenant_id"] != tenant_id:
                raise DispatchNotFound("vehicle not found")
            if not vehicle["driver_id"]:
                raise DispatchConflict("vehicle has no assigned driver")
            prior = self.cloud.db.execute(
                "SELECT * FROM dispatch_offers WHERE offer_id=? OR "
                "(vehicle_id=? AND create_key=?)",
                (offer_id, vehicle_id, create_key),
            ).fetchone()
            if prior:
                if (prior["offer_id"] == offer_id and prior["vehicle_id"] == vehicle_id
                        and prior["create_key"] == create_key
                        and prior["fingerprint"] == fingerprint):
                    return self._visible(prior, current), False
                raise DispatchConflict("proposal conflicts with a previous command")
            at = _iso(current)
            self.cloud.db.execute(
                "INSERT INTO dispatch_offers(offer_id,tenant_id,vehicle_id,driver_id,"
                "payload_json,fingerprint,create_key,status,created_at,expires_at,created_by) "
                "VALUES(?,?,?,?,?,?,?,'PENDING',?,?,?)",
                (offer_id, vehicle["tenant_id"], vehicle_id, vehicle["driver_id"],
                 _fingerprint(stored_payload), fingerprint, create_key, at, expires_at, actor_id),
            )
            self._event(offer_id, "CREATED", at, actor_id)
            self.cloud.db.execute(
                "INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                (at, "dispatch_proposal_created", vehicle_id, offer_id, actor_id),
            )
            row = self._operator_row(vehicle["tenant_id"], vehicle_id, offer_id)
            return self._visible(row, current), True

    def list_for_operator(self, tenant_id: str, vehicle_id: str,
                          limit: int = 100, now: datetime | None = None) -> list[dict[str, Any]]:
        vehicle = self.cloud.db.execute(
            "SELECT tenant_id FROM vehicles WHERE vehicle_id=?", (vehicle_id,),
        ).fetchone()
        if vehicle is None or tenant_id != "*" and vehicle["tenant_id"] != tenant_id:
            raise DispatchNotFound("vehicle not found")
        rows = self.cloud.db.execute(
            "SELECT * FROM dispatch_offers WHERE vehicle_id=? ORDER BY created_at DESC,offer_id DESC LIMIT ?",
            (vehicle_id, limit),
        ).fetchall()
        return [self._visible(row, now) for row in rows]

    def list_for_driver(self, tenant_id: str, vehicle_id: str, driver_id: str,
                        limit: int = 100, now: datetime | None = None) -> list[dict[str, Any]]:
        rows = self.cloud.db.execute(
            "SELECT * FROM dispatch_offers WHERE tenant_id=? AND vehicle_id=? AND driver_id=? "
            "ORDER BY created_at DESC,offer_id DESC LIMIT ?",
            (tenant_id, vehicle_id, driver_id, limit),
        ).fetchall()
        return [self._visible(row, now) for row in rows]

    def mark_seen(self, tenant_id: str, vehicle_id: str, driver_id: str,
                  offer_id: str, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        current = _utc(now)
        with self.cloud._write_lock, self.cloud.db:
            row = self._driver_row(tenant_id, vehicle_id, driver_id, offer_id)
            if row["seen_at"] is not None:
                return self._visible(row, current), False
            at = _iso(current)
            actor = f"driver:{driver_id}"
            self.cloud.db.execute("UPDATE dispatch_offers SET seen_at=? WHERE offer_id=?", (at, offer_id))
            self._event(offer_id, "SEEN", at, actor)
            self.cloud.db.execute(
                "INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                (at, "dispatch_proposal_seen", vehicle_id, offer_id, actor),
            )
            return self._visible(self._driver_row(tenant_id, vehicle_id, driver_id, offer_id), current), True

    def respond(self, tenant_id: str, vehicle_id: str, driver_id: str,
                offer_id: str, response_key: str, outcome: str,
                now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        if outcome not in ("ACCEPTED", "DECLINED"):
            raise ValueError("invalid dispatch outcome")
        current = _utc(now)
        response_fingerprint = _fingerprint({"outcome": outcome})
        with self.cloud._write_lock, self.cloud.db:
            row = self._driver_row(tenant_id, vehicle_id, driver_id, offer_id)
            if row["status"] != "PENDING":
                if (row["status"] == outcome and row["response_key"] == response_key
                        and row["response_fingerprint"] == response_fingerprint):
                    return self._visible(row, current), False
                raise DispatchConflict("proposal already has a final outcome")
            if datetime.fromisoformat(row["expires_at"]) <= current:
                raise DispatchConflict("proposal has expired")
            at = _iso(current)
            actor = f"driver:{driver_id}"
            if row["seen_at"] is None:
                self.cloud.db.execute("UPDATE dispatch_offers SET seen_at=? WHERE offer_id=?", (at, offer_id))
                self._event(offer_id, "SEEN", at, actor)
            self.cloud.db.execute(
                "UPDATE dispatch_offers SET status=?,responded_at=?,response_key=?,"
                "response_fingerprint=?,response_actor=? WHERE offer_id=?",
                (outcome, at, response_key, response_fingerprint, actor, offer_id),
            )
            self._event(offer_id, outcome, at, actor)
            self.cloud.db.execute(
                "INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                (at, "dispatch_proposal_" + outcome.lower(), vehicle_id, offer_id, actor),
            )
            return self._visible(self._driver_row(tenant_id, vehicle_id, driver_id, offer_id), current), True

    def cancel(self, tenant_id: str, vehicle_id: str, offer_id: str,
               cancel_key: str, note: str, actor_id: str,
               now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        current = _utc(now)
        with self.cloud._write_lock, self.cloud.db:
            row = self._operator_row(tenant_id, vehicle_id, offer_id)
            if row["status"] == "CANCELLED" and row["cancel_key"] == cancel_key and row["cancellation_note"] == note:
                return self._visible(row, current), False
            if row["status"] != "PENDING":
                raise DispatchConflict("only a pending proposal can be cancelled")
            if datetime.fromisoformat(row["expires_at"]) <= current:
                raise DispatchConflict("proposal has expired")
            at = _iso(current)
            self.cloud.db.execute(
                "UPDATE dispatch_offers SET status='CANCELLED',cancelled_at=?,cancel_key=?,"
                "cancellation_note=?,cancelled_by=? WHERE offer_id=?",
                (at, cancel_key, note, actor_id, offer_id),
            )
            self._event(offer_id, "CANCELLED", at, actor_id, note)
            self.cloud.db.execute(
                "INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                (at, "dispatch_proposal_cancelled", vehicle_id, offer_id, actor_id),
            )
            return self._visible(self._operator_row(tenant_id, vehicle_id, offer_id), current), True

    def events_for_operator(self, tenant_id: str, vehicle_id: str, offer_id: str) -> list[dict[str, Any]]:
        self._operator_row(tenant_id, vehicle_id, offer_id)
        rows = self.cloud.db.execute(
            "SELECT event_type,occurred_at,actor_id,detail FROM dispatch_events "
            "WHERE offer_id=? ORDER BY event_id", (offer_id,),
        ).fetchall()
        return [dict(row) for row in rows]
