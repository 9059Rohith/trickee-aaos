"""Fail-closed ring delivery for simulator OTA campaigns.

This coordinates signed simulator manifests only. It never installs an AAOS
image, controls a vehicle, or treats a device report as OEM acceptance.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone

from .ota import validate_simulator_manifest
from .store import CloudStore


RING_ORDER = ("CANARY", "PILOT", "FLEET")
FAILURES = {"ROLLED_BACK", "BLOCKED_VEHICLE_STATE", "REJECTED_SIGNATURE"}


class RolloutConflict(ValueError):
    pass


class RolloutNotFound(KeyError):
    pass


class RolloutStore:
    def __init__(self, cloud: CloudStore):
        self.cloud = cloud
        self.cloud.db.executescript("""
            CREATE TABLE IF NOT EXISTS ota_rollouts (
                rollout_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL,
                manifest_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACTIVE','HALTED','COMPLETED')),
                active_ring TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                created_by TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ota_rollout_targets (
                rollout_id TEXT NOT NULL, vehicle_id TEXT NOT NULL,
                ring TEXT NOT NULL CHECK(ring IN ('CANARY','PILOT','FLEET')),
                campaign_id TEXT NOT NULL UNIQUE,
                queued_at TEXT,
                PRIMARY KEY(rollout_id,vehicle_id),
                FOREIGN KEY(rollout_id) REFERENCES ota_rollouts(rollout_id)
            );
            CREATE INDEX IF NOT EXISTS ota_rollout_targets_ring
                ON ota_rollout_targets(rollout_id,ring,vehicle_id);
        """)

    @staticmethod
    def _campaign_id(rollout_id: str, vehicle_id: str) -> str:
        digest = hashlib.sha256((rollout_id + "\0" + vehicle_id).encode()).hexdigest()[:32]
        return "ROLL-" + digest

    def _row(self, rollout_id: str, tenant_id: str):
        row = self.cloud.db.execute("SELECT * FROM ota_rollouts WHERE rollout_id=? AND "
                                    "(?='*' OR tenant_id=?)",
                                    (rollout_id, tenant_id, tenant_id)).fetchone()
        if row is None:
            raise RolloutNotFound("simulator rollout not found")
        return row

    def create(self, rollout_id: str, tenant_id: str, manifest: dict,
               rings: dict[str, list[str]], actor: str) -> dict:
        if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rollout_id) is None:
            raise RolloutConflict("invalid rollout ID")
        if not actor.strip():
            raise RolloutConflict("actor required")
        validate_simulator_manifest(manifest)
        if not rings or "CANARY" not in rings or any(ring not in RING_ORDER for ring in rings):
            raise RolloutConflict("rollout needs a CANARY ring and recognized ring names")
        targets = [(ring, vehicle_id) for ring in RING_ORDER
                   for vehicle_id in rings.get(ring, [])]
        if (not targets or len(targets) > 500 or any(not isinstance(vehicle_id, str) or
                re.fullmatch(r"[A-Za-z0-9_-]{1,64}", vehicle_id) is None
                for _, vehicle_id in targets) or
                len({vehicle_id for _, vehicle_id in targets}) != len(targets) or
                not rings["CANARY"]):
            raise RolloutConflict("rollout targets must be unique and CANARY nonempty")
        now = datetime.now(timezone.utc).isoformat()
        manifest_json = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
        with self.cloud._write_lock, self.cloud.db:
            if self.cloud.db.execute("SELECT 1 FROM ota_rollouts WHERE rollout_id=?",
                                     (rollout_id,)).fetchone():
                raise RolloutConflict("rollout ID already exists")
            versions = {}
            for _, vehicle_id in targets:
                vehicle = self.cloud.db.execute(
                    "SELECT tenant_id,software_version FROM vehicles WHERE vehicle_id=?",
                    (vehicle_id,),
                ).fetchone()
                if vehicle is None or tenant_id != "*" and vehicle["tenant_id"] != tenant_id:
                    raise RolloutNotFound("vehicle not found in rollout scope")
                if self.cloud.db.execute(
                    "SELECT 1 FROM ota_campaigns WHERE vehicle_id=? AND result='PENDING'",
                    (vehicle_id,),
                ).fetchone():
                    raise RolloutConflict("vehicle has a pending simulator campaign")
                versions[vehicle_id] = vehicle["software_version"]
            scope = tenant_id if tenant_id != "*" else self.cloud.vehicle_tenant(targets[0][1])
            if any(self.cloud.vehicle_tenant(vehicle_id) != scope for _, vehicle_id in targets):
                raise RolloutConflict("rollout targets must belong to one tenant")
            self.cloud.db.execute(
                "INSERT INTO ota_rollouts VALUES(?,?,?,?,?,?,?,?)",
                (rollout_id, scope, manifest_json, "ACTIVE", "CANARY", now, now, actor),
            )
            for ring, vehicle_id in targets:
                campaign_id = self._campaign_id(rollout_id, vehicle_id)
                queued_at = now if ring == "CANARY" else None
                self.cloud.db.execute(
                    "INSERT INTO ota_rollout_targets VALUES(?,?,?,?,?)",
                    (rollout_id, vehicle_id, ring, campaign_id, queued_at),
                )
                if queued_at:
                    self.cloud.db.execute(
                        "INSERT INTO ota_campaigns(campaign_id,vehicle_id,manifest_json,created_at,prior_version) "
                        "VALUES(?,?,?,?,?)",
                        (campaign_id, vehicle_id, manifest_json, now,
                         versions[vehicle_id]),
                    )
                    self.cloud.notifications.record_ota_event(
                        vehicle_id, scope, campaign_id, "PENDING")
            self.cloud.db.execute(
                "INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                (now, "simulator_rollout_created", None, rollout_id, actor),
            )
        return self.status(rollout_id, tenant_id)

    def status(self, rollout_id: str, tenant_id: str) -> dict:
        with self.cloud._write_lock, self.cloud.db:
            return self._status(rollout_id, tenant_id)

    def _status(self, rollout_id: str, tenant_id: str) -> dict:
        row = self._row(rollout_id, tenant_id)
        targets = self.cloud.db.execute("""
            SELECT t.vehicle_id,t.ring,t.campaign_id,t.queued_at,c.result
            FROM ota_rollout_targets t
            LEFT JOIN ota_campaigns c ON c.campaign_id=t.campaign_id
            WHERE t.rollout_id=?
            ORDER BY CASE t.ring WHEN 'CANARY' THEN 0 WHEN 'PILOT' THEN 1 ELSE 2 END,
                     t.vehicle_id
        """, (rollout_id,)).fetchall()
        active = [item for item in targets if item["ring"] == row["active_ring"]]
        status = row["status"]
        if status == "ACTIVE" and any(item["result"] in FAILURES for item in active):
            status = "HALTED"
            self.cloud.db.execute(
                "UPDATE ota_rollouts SET status=?,updated_at=? WHERE rollout_id=?",
                (status, datetime.now(timezone.utc).isoformat(), rollout_id),
            )
        visible = [{"vehicle_id": item["vehicle_id"], "ring": item["ring"],
                    "campaign_id": item["campaign_id"],
                    "state": item["result"] if item["queued_at"] else "WAITING"}
                   for item in targets]
        return {"rollout_id": rollout_id, "tenant_id": row["tenant_id"],
                "mode": "SIMULATOR_ONLY", "status": status,
                "active_ring": row["active_ring"], "targets": visible,
                "created_at": row["created_at"]}

    def advance(self, rollout_id: str, tenant_id: str, actor: str) -> dict:
        if not actor.strip():
            raise RolloutConflict("actor required")
        with self.cloud._write_lock, self.cloud.db:
            current = self._status(rollout_id, tenant_id)
            if current["status"] != "ACTIVE":
                raise RolloutConflict("halted or completed rollout cannot advance")
            ring = current["active_ring"]
            active = [target for target in current["targets"] if target["ring"] == ring]
            if not active or any(target["state"] != "INSTALLED" for target in active):
                raise RolloutConflict("every current-ring simulator install must be reported")
            later = [candidate for candidate in RING_ORDER[RING_ORDER.index(ring) + 1:]
                     if any(target["ring"] == candidate for target in current["targets"])]
            now = datetime.now(timezone.utc).isoformat()
            if not later:
                self.cloud.db.execute("UPDATE ota_rollouts SET status='COMPLETED',updated_at=? "
                                      "WHERE rollout_id=?", (now, rollout_id))
                action = "simulator_rollout_completed"
            else:
                next_ring = later[0]
                manifest_json = self._row(rollout_id, tenant_id)["manifest_json"]
                waiting = [target for target in current["targets"] if target["ring"] == next_ring]
                for target in waiting:
                    vehicle = self.cloud.db.execute(
                        "SELECT software_version FROM vehicles WHERE vehicle_id=?",
                        (target["vehicle_id"],),
                    ).fetchone()
                    if vehicle is None or self.cloud.db.execute(
                        "SELECT 1 FROM ota_campaigns WHERE vehicle_id=? AND result='PENDING'",
                        (target["vehicle_id"],),
                    ).fetchone():
                        raise RolloutConflict("next-ring vehicle is unavailable or has pending OTA")
                    self.cloud.db.execute(
                        "INSERT INTO ota_campaigns(campaign_id,vehicle_id,manifest_json,created_at,prior_version) "
                        "VALUES(?,?,?,?,?)",
                        (target["campaign_id"], target["vehicle_id"], manifest_json,
                         now, vehicle["software_version"]),
                    )
                    self.cloud.notifications.record_ota_event(
                        target["vehicle_id"], current["tenant_id"],
                        target["campaign_id"], "PENDING")
                    self.cloud.db.execute("UPDATE ota_rollout_targets SET queued_at=? "
                                          "WHERE rollout_id=? AND vehicle_id=?",
                                          (now, rollout_id, target["vehicle_id"]))
                self.cloud.db.execute("UPDATE ota_rollouts SET active_ring=?,updated_at=? "
                                      "WHERE rollout_id=?", (next_ring, now, rollout_id))
                action = "simulator_rollout_advanced"
            self.cloud.db.execute(
                "INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                (now, action, None, rollout_id, actor),
            )
        return self.status(rollout_id, tenant_id)
