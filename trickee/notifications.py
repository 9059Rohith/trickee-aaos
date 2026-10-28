"""Durable, tenant-scoped vehicle notices with transition-based deduplication.

Only fresh current telemetry creates notices. Historical replay and missing
signals never create an alert or silently clear an active condition.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from .domain import Quality, SignalFrame

if TYPE_CHECKING:
    from .store import CloudStore


class NotificationStore:
    def __init__(self, cloud: CloudStore):
        self.cloud = cloud
        self.cloud.db.executescript("""
            CREATE TABLE IF NOT EXISTS notification_state (
                vehicle_id TEXT NOT NULL, kind TEXT NOT NULL,
                active_key TEXT, updated_at TEXT NOT NULL,
                PRIMARY KEY(vehicle_id,kind)
            );
            CREATE TABLE IF NOT EXISTS notifications (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL, vehicle_id TEXT NOT NULL,
                kind TEXT NOT NULL, priority TEXT NOT NULL,
                message TEXT NOT NULL, evidence TEXT NOT NULL,
                observed_at TEXT NOT NULL, created_at TEXT NOT NULL,
                acknowledged_at TEXT, acknowledged_by TEXT,
                dedupe_key TEXT NOT NULL UNIQUE
            );
            CREATE INDEX IF NOT EXISTS notifications_tenant_recent
                ON notifications(tenant_id,id DESC);
        """)

    def _transition(self, frame: SignalFrame, tenant_id: str, kind: str,
                    active_key: str | None, priority: str = "INFO",
                    message: str = "", evidence: str = "",
                    emit: bool = True) -> None:
        now = datetime.now(timezone.utc).isoformat()
        previous = self.cloud.db.execute(
            "SELECT active_key FROM notification_state WHERE vehicle_id=? AND kind=?",
            (frame.vehicle_id, kind),
        ).fetchone()
        old_key = previous["active_key"] if previous else None
        if old_key == active_key:
            return
        self.cloud.db.execute("""
            INSERT INTO notification_state(vehicle_id,kind,active_key,updated_at)
            VALUES(?,?,?,?) ON CONFLICT(vehicle_id,kind) DO UPDATE SET
            active_key=excluded.active_key,updated_at=excluded.updated_at
        """, (frame.vehicle_id, kind, active_key, now))
        if active_key is not None and emit:
            self.cloud.db.execute("""
                INSERT OR IGNORE INTO notifications(
                    tenant_id,vehicle_id,kind,priority,message,evidence,
                    observed_at,created_at,dedupe_key)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (tenant_id, frame.vehicle_id, kind, priority, message,
                  evidence, frame.observed_at.isoformat(), now,
                  f"SIGNAL:{frame.vehicle_id}:{frame.sequence}:{kind}"))

    def evaluate_signal(self, frame: SignalFrame, tenant_id: str) -> None:
        if frame.source == "REPLAY" or frame.quality() not in (Quality.OK, Quality.SIMULATED):
            return
        supplied = frame.model_fields_set
        if "soc_pct" in supplied and frame.soc_pct is not None:
            critical = frame.soc_pct < 10
            low = frame.soc_pct < 20
            self._transition(frame, tenant_id, "LOW_BATTERY",
                             "CRITICAL" if critical else "WARNING" if low else None,
                             "CRITICAL" if critical else "WARNING",
                             f"Reported battery charge is {frame.soc_pct:.0f}%.",
                             "reported_soc_pct")
        if "fault_code" in supplied:
            code = frame.fault_code.strip() if frame.fault_code else None
            self._transition(frame, tenant_id, "VEHICLE_FAULT", code,
                             "CRITICAL", "A reported vehicle fault needs review.",
                             "reported_fault_code")
        if "soh_pct" in supplied and frame.soh_pct is not None:
            self._transition(frame, tenant_id, "MAINTENANCE",
                             "LOW_SOH" if frame.soh_pct < 75 else None,
                             "WARNING", "Battery health is below the review threshold.",
                             "reported_soh_pct_below_75")
        if "charging" in supplied:
            old = self.cloud.db.execute(
                "SELECT active_key FROM notification_state WHERE vehicle_id=? AND kind='CHARGING_ACTIVE'",
                (frame.vehicle_id,),
            ).fetchone()
            was_charging = old is not None and old["active_key"] == "YES"
            self._transition(frame, tenant_id, "CHARGING_ACTIVE",
                             "YES" if frame.charging else None, emit=False)
            if was_charging and not frame.charging and frame.soc_pct is not None and frame.soc_pct >= 95:
                self._transition(frame, tenant_id, "CHARGING_COMPLETE", "COMPLETE",
                                 "INFO", "Reported charging completed.",
                                 "charging_transition_and_soc_at_least_95")
            elif frame.charging:
                self._transition(frame, tenant_id, "CHARGING_COMPLETE", None)

    def record_ota_event(self, vehicle_id: str, tenant_id: str,
                         campaign_id: str, result: str) -> None:
        if result == "PENDING":
            kind, priority, message = (
                "OTA_AVAILABLE", "INFO", "A simulator update is available for this vehicle.")
        elif result in ("ROLLED_BACK", "BLOCKED_VEHICLE_STATE", "REJECTED_SIGNATURE"):
            kind, priority, message = (
                "OTA_FAILED", "CRITICAL", "Simulator update failed and needs review.")
        else:
            return
        now = datetime.now(timezone.utc).isoformat()
        self.cloud.db.execute("""
            INSERT OR IGNORE INTO notifications(
                tenant_id,vehicle_id,kind,priority,message,evidence,
                observed_at,created_at,dedupe_key)
            VALUES(?,?,?,?,?,?,?,?,?)
        """, (tenant_id, vehicle_id, kind, priority, message,
              f"{campaign_id}:{result}", now, now,
              f"OTA:{kind}:{vehicle_id}:{campaign_id}"))

    def list_for_tenant(self, tenant_id: str, limit: int = 50) -> list[dict]:
        if not 1 <= limit <= 100:
            raise ValueError("notification page size is invalid")
        clause = "" if tenant_id == "*" else "WHERE tenant_id=?"
        params = (limit,) if tenant_id == "*" else (tenant_id, limit)
        rows = self.cloud.db.execute(
            f"SELECT * FROM notifications {clause} ORDER BY id DESC LIMIT ?", params,
        ).fetchall()
        return [dict(row) for row in rows]

    def acknowledge(self, notice_id: int, tenant_id: str, actor: str) -> dict:
        with self.cloud._write_lock, self.cloud.db:
            row = self.cloud.db.execute(
                "SELECT * FROM notifications WHERE id=? AND (?='*' OR tenant_id=?)",
                (notice_id, tenant_id, tenant_id),
            ).fetchone()
            if row is None:
                raise KeyError("notification not found")
            if row["acknowledged_at"] is None:
                self.cloud.db.execute(
                    "UPDATE notifications SET acknowledged_at=?,acknowledged_by=? WHERE id=?",
                    (datetime.now(timezone.utc).isoformat(), actor, notice_id),
                )
            return dict(self.cloud.db.execute(
                "SELECT * FROM notifications WHERE id=?", (notice_id,),
            ).fetchone())
