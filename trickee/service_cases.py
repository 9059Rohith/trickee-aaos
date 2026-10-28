"""Durable driver-reported service cases and operator triage."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from .store import CloudStore


class ServiceCaseNotFound(KeyError):
    pass


class ServiceCaseConflict(ValueError):
    pass


def _now(value: datetime | None = None) -> str:
    instant = value or datetime.now(timezone.utc)
    if instant.tzinfo is None:
        raise ValueError("service case clock must be timezone-aware")
    return instant.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _fingerprint(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


class ServiceCaseStore:
    def __init__(self, cloud: CloudStore):
        self.cloud = cloud
        self.cloud.db.executescript("""
            CREATE TABLE IF NOT EXISTS service_cases (
                case_id TEXT PRIMARY KEY,
                tenant_id TEXT NOT NULL,
                vehicle_id TEXT NOT NULL,
                driver_id TEXT NOT NULL,
                category TEXT NOT NULL,
                priority TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('OPEN','ACKNOWLEDGED','RESOLVED')),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                create_key TEXT NOT NULL,
                create_fingerprint TEXT NOT NULL,
                acknowledged_at TEXT,
                acknowledged_by TEXT,
                acknowledge_key TEXT,
                resolved_at TEXT,
                resolved_by TEXT,
                resolve_key TEXT,
                resolve_fingerprint TEXT,
                resolution_code TEXT,
                resolution_note TEXT,
                UNIQUE(tenant_id,driver_id,create_key)
            );
            CREATE INDEX IF NOT EXISTS service_cases_by_tenant
                ON service_cases(tenant_id,status,updated_at DESC);
            CREATE INDEX IF NOT EXISTS service_cases_by_driver
                ON service_cases(tenant_id,vehicle_id,driver_id,updated_at DESC);
            CREATE TABLE IF NOT EXISTS service_case_messages (
                message_id TEXT PRIMARY KEY,
                case_id TEXT NOT NULL,
                command_key TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                actor_kind TEXT NOT NULL CHECK(actor_kind IN ('DRIVER','OPERATOR')),
                actor_id TEXT NOT NULL,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(case_id,command_key),
                FOREIGN KEY(case_id) REFERENCES service_cases(case_id)
            );
            CREATE INDEX IF NOT EXISTS service_case_messages_by_case
                ON service_case_messages(case_id,created_at,message_id);
            CREATE TABLE IF NOT EXISTS service_case_events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                case_id TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK(event_type IN
                    ('CREATED','ACKNOWLEDGED','MESSAGE','RESOLVED')),
                actor_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(case_id) REFERENCES service_cases(case_id)
            );
            CREATE INDEX IF NOT EXISTS service_case_events_by_case
                ON service_case_events(case_id,event_id);
        """)

    @staticmethod
    def _visible(row: Any) -> dict[str, Any]:
        return {key: row[key] for key in (
            "case_id", "tenant_id", "vehicle_id", "driver_id", "category", "priority",
            "title", "description", "status", "created_at", "updated_at",
            "acknowledged_at", "acknowledged_by", "resolved_at", "resolved_by",
            "resolution_code", "resolution_note",
        )}

    def _event(self, case_id: str, kind: str, actor: str, at: str, detail: str = "") -> None:
        self.cloud.db.execute(
            "INSERT INTO service_case_events(case_id,event_type,actor_id,occurred_at,detail) "
            "VALUES(?,?,?,?,?)", (case_id, kind, actor, at, detail),
        )
        row = self.cloud.db.execute(
            "SELECT tenant_id,vehicle_id,driver_id FROM service_cases WHERE case_id=?",
            (case_id,),
        ).fetchone()
        self.cloud.publish_change(row["tenant_id"], row["vehicle_id"], row["driver_id"],
                                  "SERVICE_CASE_" + kind, case_id, at)

    def _audit(self, action: str, vehicle_id: str, case_id: str, actor: str, at: str) -> None:
        self.cloud.db.execute(
            "INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
            (at, action, vehicle_id, case_id, actor),
        )

    def _operator_row(self, tenant: str, vehicle: str, case_id: str):
        row = self.cloud.db.execute(
            "SELECT * FROM service_cases WHERE case_id=? AND vehicle_id=? "
            "AND (?='*' OR tenant_id=?)", (case_id, vehicle, tenant, tenant),
        ).fetchone()
        if row is None:
            raise ServiceCaseNotFound("service case not found")
        return row

    def _driver_row(self, tenant: str, vehicle: str, driver: str, case_id: str):
        row = self.cloud.db.execute(
            "SELECT * FROM service_cases WHERE case_id=? AND tenant_id=? "
            "AND vehicle_id=? AND driver_id=?", (case_id, tenant, vehicle, driver),
        ).fetchone()
        if row is None:
            raise ServiceCaseNotFound("service case not found")
        return row

    def create(self, tenant: str, vehicle: str, driver: str, case_id: str,
               command_key: str, content: dict[str, Any],
               now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        at = _now(now)
        fingerprint = _fingerprint(content)
        with self.cloud._write_lock, self.cloud.db:
            assignment = self.cloud.db.execute(
                "SELECT tenant_id,driver_id FROM vehicles WHERE vehicle_id=?", (vehicle,),
            ).fetchone()
            if assignment is None or assignment["tenant_id"] != tenant or assignment["driver_id"] != driver:
                raise ServiceCaseNotFound("assigned vehicle not found")
            prior = self.cloud.db.execute(
                "SELECT * FROM service_cases WHERE case_id=? OR "
                "(tenant_id=? AND driver_id=? AND create_key=?)",
                (case_id, tenant, driver, command_key),
            ).fetchone()
            if prior is not None:
                if (prior["case_id"] == case_id and prior["tenant_id"] == tenant
                        and prior["vehicle_id"] == vehicle and prior["driver_id"] == driver
                        and prior["create_key"] == command_key
                        and prior["create_fingerprint"] == fingerprint):
                    return self._visible(prior), False
                raise ServiceCaseConflict("service case conflicts with a previous command")
            self.cloud.db.execute(
                "INSERT INTO service_cases(case_id,tenant_id,vehicle_id,driver_id,category,priority,"
                "title,description,status,created_at,updated_at,create_key,create_fingerprint) "
                "VALUES(?,?,?,?,?,?,?,?, 'OPEN',?,?,?,?)",
                (case_id, tenant, vehicle, driver, content["category"], content["priority"],
                 content["title"], content["description"], at, at, command_key, fingerprint),
            )
            actor = f"driver:{driver}"
            self._event(case_id, "CREATED", actor, at)
            self._audit("service_case_created", vehicle, case_id, actor, at)
            return self._visible(self._driver_row(tenant, vehicle, driver, case_id)), True

    def list_for_driver(self, tenant: str, vehicle: str, driver: str,
                        limit: int = 100) -> list[dict[str, Any]]:
        rows = self.cloud.db.execute(
            "SELECT * FROM service_cases WHERE tenant_id=? AND vehicle_id=? AND driver_id=? "
            "ORDER BY updated_at DESC,case_id DESC LIMIT ?", (tenant, vehicle, driver, limit),
        ).fetchall()
        return [self._visible(row) for row in rows]

    def list_for_operator(self, tenant: str, vehicle: str,
                          limit: int = 100) -> list[dict[str, Any]]:
        assignment = self.cloud.db.execute(
            "SELECT tenant_id FROM vehicles WHERE vehicle_id=?", (vehicle,),
        ).fetchone()
        if assignment is None or tenant != "*" and assignment["tenant_id"] != tenant:
            raise ServiceCaseNotFound("vehicle not found")
        rows = self.cloud.db.execute(
            "SELECT * FROM service_cases WHERE vehicle_id=? "
            "ORDER BY CASE status WHEN 'OPEN' THEN 0 WHEN 'ACKNOWLEDGED' THEN 1 ELSE 2 END, "
            "CASE priority WHEN 'URGENT' THEN 0 ELSE 1 END, updated_at DESC,case_id DESC LIMIT ?",
            (vehicle, limit),
        ).fetchall()
        return [self._visible(row) for row in rows]

    def summary(self, tenant: str) -> dict[str, int]:
        clause = "" if tenant == "*" else " WHERE tenant_id=?"
        params = () if tenant == "*" else (tenant,)
        rows = self.cloud.db.execute(
            "SELECT status,priority,COUNT(*) AS count FROM service_cases" + clause +
            " GROUP BY status,priority", params,
        ).fetchall()
        result = {"open": 0, "urgent": 0, "resolved": 0}
        for row in rows:
            if row["status"] == "RESOLVED":
                result["resolved"] += row["count"]
            else:
                result["open"] += row["count"]
                if row["priority"] == "URGENT":
                    result["urgent"] += row["count"]
        return result

    def messages_for_driver(self, tenant: str, vehicle: str, driver: str,
                            case_id: str) -> list[dict[str, Any]]:
        self._driver_row(tenant, vehicle, driver, case_id)
        return self._messages(case_id)

    def messages_for_operator(self, tenant: str, vehicle: str,
                              case_id: str) -> list[dict[str, Any]]:
        self._operator_row(tenant, vehicle, case_id)
        return self._messages(case_id)

    def _messages(self, case_id: str) -> list[dict[str, Any]]:
        rows = self.cloud.db.execute(
            "SELECT message_id,actor_kind,actor_id,body,created_at FROM service_case_messages "
            "WHERE case_id=? ORDER BY rowid LIMIT 200", (case_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def add_driver_message(self, tenant: str, vehicle: str, driver: str, case_id: str,
                           message_id: str, command_key: str, body: str,
                           now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        return self._add_message(tenant, vehicle, driver, case_id, message_id, command_key,
                                 body, "DRIVER", now)

    def add_operator_message(self, tenant: str, vehicle: str, actor: str, case_id: str,
                             message_id: str, command_key: str, body: str,
                             now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        return self._add_message(tenant, vehicle, actor, case_id, message_id, command_key,
                                 body, "OPERATOR", now)

    def _add_message(self, tenant: str, vehicle: str, actor: str, case_id: str,
                     message_id: str, command_key: str, body: str, kind: str,
                     now: datetime | None) -> tuple[dict[str, Any], bool]:
        at = _now(now)
        fingerprint = _fingerprint({"body": body, "actor_kind": kind, "actor_id": actor})
        with self.cloud._write_lock, self.cloud.db:
            row = (self._driver_row(tenant, vehicle, actor, case_id) if kind == "DRIVER"
                   else self._operator_row(tenant, vehicle, case_id))
            prior = self.cloud.db.execute(
                "SELECT * FROM service_case_messages WHERE message_id=? OR "
                "(case_id=? AND command_key=?)", (message_id, case_id, command_key),
            ).fetchone()
            if prior is not None:
                if (prior["message_id"] == message_id and prior["case_id"] == case_id
                        and prior["command_key"] == command_key and prior["fingerprint"] == fingerprint):
                    return self._visible(row), False
                raise ServiceCaseConflict("message conflicts with a previous command")
            if row["status"] == "RESOLVED":
                raise ServiceCaseConflict("resolved case cannot receive new messages")
            self.cloud.db.execute(
                "INSERT INTO service_case_messages(message_id,case_id,command_key,fingerprint,"
                "actor_kind,actor_id,body,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (message_id, case_id, command_key, fingerprint, kind, actor, body, at),
            )
            self.cloud.db.execute(
                "UPDATE service_cases SET updated_at=? WHERE case_id=?", (at, case_id),
            )
            event_actor = f"driver:{actor}" if kind == "DRIVER" else actor
            self._event(case_id, "MESSAGE", event_actor, at, message_id)
            self._audit("service_case_message", vehicle, case_id, event_actor, at)
            return self._visible(self._driver_row(tenant, vehicle, actor, case_id)
                                 if kind == "DRIVER" else self._operator_row(tenant, vehicle, case_id)), True

    def acknowledge(self, tenant: str, vehicle: str, case_id: str, command_key: str,
                    actor: str, now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        at = _now(now)
        with self.cloud._write_lock, self.cloud.db:
            row = self._operator_row(tenant, vehicle, case_id)
            if row["status"] == "ACKNOWLEDGED" and row["acknowledge_key"] == command_key:
                return self._visible(row), False
            if row["status"] != "OPEN":
                raise ServiceCaseConflict("only an open case can be acknowledged")
            self.cloud.db.execute(
                "UPDATE service_cases SET status='ACKNOWLEDGED',acknowledged_at=?,"
                "acknowledged_by=?,acknowledge_key=?,updated_at=? WHERE case_id=?",
                (at, actor, command_key, at, case_id),
            )
            self._event(case_id, "ACKNOWLEDGED", actor, at)
            self._audit("service_case_acknowledged", vehicle, case_id, actor, at)
            return self._visible(self._operator_row(tenant, vehicle, case_id)), True

    def resolve(self, tenant: str, vehicle: str, case_id: str, command_key: str,
                actor: str, resolution_code: str, note: str,
                now: datetime | None = None) -> tuple[dict[str, Any], bool]:
        at = _now(now)
        fingerprint = _fingerprint({"resolution_code": resolution_code, "note": note})
        with self.cloud._write_lock, self.cloud.db:
            row = self._operator_row(tenant, vehicle, case_id)
            if row["status"] == "RESOLVED":
                if row["resolve_key"] == command_key and row["resolve_fingerprint"] == fingerprint:
                    return self._visible(row), False
                raise ServiceCaseConflict("case already resolved")
            self.cloud.db.execute(
                "UPDATE service_cases SET status='RESOLVED',resolved_at=?,resolved_by=?,"
                "resolve_key=?,resolve_fingerprint=?,resolution_code=?,resolution_note=?,"
                "updated_at=? WHERE case_id=?",
                (at, actor, command_key, fingerprint, resolution_code, note, at, case_id),
            )
            self._event(case_id, "RESOLVED", actor, at, resolution_code)
            self._audit("service_case_resolved", vehicle, case_id, actor, at)
            return self._visible(self._operator_row(tenant, vehicle, case_id)), True

    def events_for_operator(self, tenant: str, vehicle: str, case_id: str) -> list[dict[str, Any]]:
        self._operator_row(tenant, vehicle, case_id)
        rows = self.cloud.db.execute(
            "SELECT event_type,actor_id,occurred_at,detail FROM service_case_events "
            "WHERE case_id=? ORDER BY event_id", (case_id,),
        ).fetchall()
        return [dict(row) for row in rows]
