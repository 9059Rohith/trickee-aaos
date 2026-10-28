"""Manual internal assignments confirmed from accepted dispatch proposals.

Driver delivery is a report, not proof of fulfilment by an external order partner.
Only an operator can reconcile that report, and reconciliation remains labelled manual.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .store import CloudStore


class OrderConflict(ValueError):
    """An assignment command conflicts with stored state or another command."""


class OrderNotFound(KeyError):
    """An assignment is absent from the caller's visible scope."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


class OrderStore:
    """Persist a scoped, idempotent local order lifecycle in the cloud database."""

    def __init__(self, cloud: CloudStore):
        self.cloud = cloud
        self.cloud.db.executescript("""
            CREATE TABLE IF NOT EXISTS internal_orders (
                order_id TEXT PRIMARY KEY,
                offer_id TEXT NOT NULL UNIQUE,
                tenant_id TEXT NOT NULL,
                vehicle_id TEXT NOT NULL,
                driver_id TEXT NOT NULL,
                source TEXT NOT NULL CHECK(source='MANUAL_OPERATOR_CONFIRMED'),
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('ASSIGNED','PICKED_UP','DELIVERED_REPORTED','RECONCILED','CANCELLED')),
                create_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                picked_up_at TEXT,
                delivery_reported_at TEXT,
                reconciled_at TEXT,
                cancelled_at TEXT,
                reconciliation_note TEXT,
                cancellation_note TEXT,
                created_by TEXT NOT NULL,
                reconciled_by TEXT,
                cancelled_by TEXT,
                UNIQUE(vehicle_id,create_key),
                FOREIGN KEY(offer_id) REFERENCES dispatch_offers(offer_id)
            );
            CREATE INDEX IF NOT EXISTS orders_by_vehicle
                ON internal_orders(tenant_id,vehicle_id,created_at DESC,order_id DESC);
            CREATE INDEX IF NOT EXISTS orders_by_driver
                ON internal_orders(tenant_id,vehicle_id,driver_id,created_at DESC);
            CREATE TABLE IF NOT EXISTS internal_order_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN
                    ('ASSIGNED','PICKED_UP','DELIVERED_REPORTED','RECONCILED','CANCELLED')),
                command_key TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                UNIQUE(order_id,command_key),
                FOREIGN KEY(order_id) REFERENCES internal_orders(order_id)
            );
            CREATE INDEX IF NOT EXISTS order_events_by_order
                ON internal_order_events(order_id,event_id);
        """)

    @staticmethod
    def _view(row: Any) -> dict[str, Any]:
        payload = json.loads(row["payload_json"])
        return {
            "order_id": row["order_id"], "offer_id": row["offer_id"],
            "tenant_id": row["tenant_id"], "vehicle_id": row["vehicle_id"],
            "driver_id": row["driver_id"], "source": row["source"],
            "status": row["status"], "pickup": payload["pickup"],
            "destination": payload["destination"],
            "pickup_label": payload["pickup_label"],
            "destination_label": payload["destination_label"],
            "external_reference": payload.get("external_reference"),
            "fulfilment_quality": "MANUAL_RECONCILED" if row["status"] == "RECONCILED"
                                   else "DRIVER_REPORTED" if row["status"] == "DELIVERED_REPORTED"
                                   else "UNVERIFIED",
            "created_at": row["created_at"], "picked_up_at": row["picked_up_at"],
            "delivery_reported_at": row["delivery_reported_at"],
            "reconciled_at": row["reconciled_at"], "cancelled_at": row["cancelled_at"],
            "reconciliation_note": row["reconciliation_note"],
            "cancellation_note": row["cancellation_note"],
            "created_by": row["created_by"], "reconciled_by": row["reconciled_by"],
            "cancelled_by": row["cancelled_by"],
        }

    def _operator_row(self, tenant_id: str, vehicle_id: str, order_id: str):
        row = self.cloud.db.execute(
            "SELECT * FROM internal_orders WHERE order_id=? AND vehicle_id=? AND (?='*' OR tenant_id=?)",
            (order_id, vehicle_id, tenant_id, tenant_id),
        ).fetchone()
        if row is None:
            raise OrderNotFound("internal assignment not found")
        return row

    def _driver_row(self, tenant_id: str, vehicle_id: str, driver_id: str, order_id: str):
        row = self.cloud.db.execute(
            "SELECT * FROM internal_orders WHERE order_id=? AND tenant_id=? AND vehicle_id=? AND driver_id=?",
            (order_id, tenant_id, vehicle_id, driver_id),
        ).fetchone()
        if row is None:
            raise OrderNotFound("internal assignment not found")
        return row

    def _record(self, row: Any, event_type: str, command_key: str,
                actor_id: str, at: str, detail: str = "") -> None:
        self.cloud.db.execute(
            "INSERT INTO internal_order_events(order_id,event_type,command_key,occurred_at,actor_id,detail) "
            "VALUES(?,?,?,?,?,?)",
            (row["order_id"], event_type, command_key, at, actor_id, detail),
        )
        self.cloud.publish_change(row["tenant_id"], row["vehicle_id"], row["driver_id"],
                                  "ORDER_" + event_type, row["order_id"], at)
        self.cloud.db.execute(
            "INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
            (at, "internal_order_" + event_type.lower(), row["vehicle_id"], row["order_id"], actor_id),
        )

    def create_from_accepted_offer(self, tenant_id: str, vehicle_id: str, offer_id: str,
                                   order_id: str, command_key: str,
                                   actor_id: str) -> tuple[dict[str, Any], bool]:
        with self.cloud._write_lock, self.cloud.db:
            offer = self.cloud.db.execute(
                "SELECT * FROM dispatch_offers WHERE offer_id=? AND vehicle_id=? AND (?='*' OR tenant_id=?)",
                (offer_id, vehicle_id, tenant_id, tenant_id),
            ).fetchone()
            if offer is None:
                raise OrderNotFound("accepted proposal not found")
            existing = self.cloud.db.execute(
                "SELECT * FROM internal_orders WHERE order_id=? OR offer_id=? OR (vehicle_id=? AND create_key=?)",
                (order_id, offer_id, vehicle_id, command_key),
            ).fetchone()
            if existing is not None:
                if (existing["order_id"] == order_id and existing["offer_id"] == offer_id
                        and existing["vehicle_id"] == vehicle_id and existing["create_key"] == command_key):
                    return self._view(existing), False
                raise OrderConflict("assignment conflicts with a previous command")
            if offer["status"] != "ACCEPTED":
                raise OrderConflict("driver must accept the proposal before assignment")
            vehicle = self.cloud.db.execute(
                "SELECT driver_id,tenant_id FROM vehicles WHERE vehicle_id=?", (vehicle_id,),
            ).fetchone()
            if vehicle is None or vehicle["driver_id"] != offer["driver_id"] or vehicle["tenant_id"] != offer["tenant_id"]:
                raise OrderConflict("vehicle assignment changed after proposal acceptance")
            at = _now()
            payload = json.loads(offer["payload_json"])
            self.cloud.db.execute(
                "INSERT INTO internal_orders(order_id,offer_id,tenant_id,vehicle_id,driver_id,source,"
                "payload_json,status,create_key,created_at,created_by) "
                "VALUES(?,?,?,?,?,'MANUAL_OPERATOR_CONFIRMED',?,'ASSIGNED',?,?,?)",
                (order_id, offer_id, offer["tenant_id"], vehicle_id, offer["driver_id"],
                 json.dumps(payload, sort_keys=True), command_key, at, actor_id),
            )
            row = self._operator_row(offer["tenant_id"], vehicle_id, order_id)
            self._record(row, "ASSIGNED", command_key, actor_id, at)
            return self._view(row), True

    def list_for_operator(self, tenant_id: str, vehicle_id: str,
                          limit: int = 100) -> list[dict[str, Any]]:
        vehicle = self.cloud.db.execute(
            "SELECT tenant_id FROM vehicles WHERE vehicle_id=?", (vehicle_id,),
        ).fetchone()
        if vehicle is None or tenant_id != "*" and vehicle["tenant_id"] != tenant_id:
            raise OrderNotFound("vehicle not found")
        rows = self.cloud.db.execute(
            "SELECT * FROM internal_orders WHERE vehicle_id=? ORDER BY created_at DESC,order_id DESC LIMIT ?",
            (vehicle_id, limit),
        ).fetchall()
        return [self._view(row) for row in rows]

    def list_for_driver(self, tenant_id: str, vehicle_id: str,
                        driver_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.cloud.db.execute(
            "SELECT * FROM internal_orders WHERE tenant_id=? AND vehicle_id=? AND driver_id=? "
            "ORDER BY created_at DESC,order_id DESC LIMIT ?",
            (tenant_id, vehicle_id, driver_id, limit),
        ).fetchall()
        return [self._view(row) for row in rows]

    def _transition(self, row: Any, command_key: str, event_type: str, allowed_status: str,
                    actor_id: str, column: str, note_column: str | None = None,
                    note: str = "") -> tuple[dict[str, Any], bool]:
        prior = self.cloud.db.execute(
            "SELECT event_type,detail FROM internal_order_events WHERE order_id=? AND command_key=?",
            (row["order_id"], command_key),
        ).fetchone()
        if prior:
            if prior["event_type"] == event_type and prior["detail"] == note:
                return self._view(row), False
            raise OrderConflict("command key was used for a different assignment action")
        if row["status"] != allowed_status:
            raise OrderConflict(f"assignment must be {allowed_status} before {event_type.lower()}")
        at = _now()
        if note_column:
            self.cloud.db.execute(
                f"UPDATE internal_orders SET status=?,{column}=?,{note_column}=? WHERE order_id=?",
                (event_type, at, note, row["order_id"]),
            )
        else:
            self.cloud.db.execute(
                f"UPDATE internal_orders SET status=?,{column}=? WHERE order_id=?",
                (event_type, at, row["order_id"]),
            )
        self._record(row, event_type, command_key, actor_id, at, note)
        updated = self.cloud.db.execute(
            "SELECT * FROM internal_orders WHERE order_id=?", (row["order_id"],),
        ).fetchone()
        return self._view(updated), True

    def driver_pickup(self, tenant_id: str, vehicle_id: str, driver_id: str,
                      order_id: str, command_key: str) -> tuple[dict[str, Any], bool]:
        with self.cloud._write_lock, self.cloud.db:
            row = self._driver_row(tenant_id, vehicle_id, driver_id, order_id)
            return self._transition(row, command_key, "PICKED_UP", "ASSIGNED",
                                    f"driver:{driver_id}", "picked_up_at")

    def driver_report_delivery(self, tenant_id: str, vehicle_id: str, driver_id: str,
                               order_id: str, command_key: str) -> tuple[dict[str, Any], bool]:
        with self.cloud._write_lock, self.cloud.db:
            row = self._driver_row(tenant_id, vehicle_id, driver_id, order_id)
            return self._transition(row, command_key, "DELIVERED_REPORTED", "PICKED_UP",
                                    f"driver:{driver_id}", "delivery_reported_at")

    def reconcile(self, tenant_id: str, vehicle_id: str, order_id: str,
                  command_key: str, note: str, actor_id: str) -> tuple[dict[str, Any], bool]:
        with self.cloud._write_lock, self.cloud.db:
            row = self._operator_row(tenant_id, vehicle_id, order_id)
            result, changed = self._transition(row, command_key, "RECONCILED",
                                               "DELIVERED_REPORTED", actor_id, "reconciled_at",
                                               "reconciliation_note", note)
            if changed:
                self.cloud.db.execute("UPDATE internal_orders SET reconciled_by=? WHERE order_id=?",
                                      (actor_id, order_id))
                result["reconciled_by"] = actor_id
            return result, changed

    def cancel(self, tenant_id: str, vehicle_id: str, order_id: str,
               command_key: str, note: str, actor_id: str) -> tuple[dict[str, Any], bool]:
        with self.cloud._write_lock, self.cloud.db:
            row = self._operator_row(tenant_id, vehicle_id, order_id)
            result, changed = self._transition(row, command_key, "CANCELLED", "ASSIGNED",
                                               actor_id, "cancelled_at", "cancellation_note", note)
            if changed:
                self.cloud.db.execute("UPDATE internal_orders SET cancelled_by=? WHERE order_id=?",
                                      (actor_id, order_id))
                result["cancelled_by"] = actor_id
            return result, changed

    def events_for_operator(self, tenant_id: str, vehicle_id: str,
                            order_id: str) -> list[dict[str, Any]]:
        self._operator_row(tenant_id, vehicle_id, order_id)
        rows = self.cloud.db.execute(
            "SELECT event_type,occurred_at,actor_id,detail FROM internal_order_events "
            "WHERE order_id=? ORDER BY event_id", (order_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def daily_summary(self, start_utc: datetime, end_utc: datetime,
                      tenant_id: str) -> dict[str, int]:
        """Count stored manual workflow events, without calling them partner orders."""
        if (start_utc.tzinfo is None or end_utc.tzinfo is None or end_utc <= start_utc):
            raise ValueError("invalid assignment reporting interval")
        clause = "" if tenant_id == "*" else "AND orders.tenant_id=?"
        parameters = (start_utc.isoformat(), end_utc.isoformat())
        if tenant_id != "*":
            parameters += (tenant_id,)
        rows = self.cloud.db.execute(
            "SELECT events.event_type,COUNT(*) AS count FROM internal_order_events events "
            "JOIN internal_orders orders ON orders.order_id=events.order_id "
            "WHERE events.occurred_at>=? AND events.occurred_at<? " + clause +
            " GROUP BY events.event_type", parameters,
        ).fetchall()
        counts = {row["event_type"]: row["count"] for row in rows}
        return {"internal_assignments_created": counts.get("ASSIGNED", 0),
                "internal_pickups_reported": counts.get("PICKED_UP", 0),
                "internal_deliveries_reported": counts.get("DELIVERED_REPORTED", 0),
                "internal_deliveries_manually_reconciled": counts.get("RECONCILED", 0),
                "internal_assignments_cancelled": counts.get("CANCELLED", 0)}
