"""SQLite cloud twin and on-vehicle durable telemetry outbox."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import Quality, SignalFrame, VehicleProfile, health, infer_state
from .ota import validate_simulator_manifest


class ConsentRequiredError(Exception):
    """The driver has not granted the purpose for this phone sample."""


def _connect(path: str) -> sqlite3.Connection:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    connection.execute("PRAGMA secure_delete=ON")
    return connection


class CloudStore:
    def __init__(self, path: str):
        configured_retention = os.getenv("TRICKEE_PHONE_GPS_RETENTION_DAYS")
        self._phone_retention_days = None
        if configured_retention is not None:
            try:
                self._phone_retention_days = int(configured_retention)
            except ValueError as exc:
                raise ValueError("TRICKEE_PHONE_GPS_RETENTION_DAYS must be a positive integer") from exc
            if self._phone_retention_days < 1:
                raise ValueError("TRICKEE_PHONE_GPS_RETENTION_DAYS must be a positive integer")
        self._path = path
        self._local = threading.local()
        self._write_lock = threading.RLock()
        self._connections_lock = threading.Lock()
        self._connections: list[sqlite3.Connection] = []
        self._closed = False
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS vehicles (
                vehicle_id TEXT PRIMARY KEY, profile_json TEXT NOT NULL, driver_id TEXT,
                tenant_id TEXT NOT NULL DEFAULT 'default',
                software_version TEXT NOT NULL DEFAULT 'sim-1'
            );
            CREATE TABLE IF NOT EXISTS events (
                vehicle_id TEXT NOT NULL, sequence INTEGER NOT NULL, observed_at TEXT NOT NULL,
                received_at TEXT NOT NULL, payload_json TEXT NOT NULL,
                PRIMARY KEY(vehicle_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS events_recent ON events(vehicle_id, sequence DESC);
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, action TEXT NOT NULL,
                vehicle_id TEXT, detail TEXT NOT NULL, actor_id TEXT
            );
            CREATE INDEX IF NOT EXISTS audit_by_vehicle ON audit(vehicle_id,id DESC);
            CREATE TABLE IF NOT EXISTS client_changes (
                scope TEXT NOT NULL, sequence INTEGER NOT NULL,
                kind TEXT NOT NULL, vehicle_id TEXT NOT NULL,
                resource_id TEXT NOT NULL, occurred_at TEXT NOT NULL,
                PRIMARY KEY(scope,sequence)
            );
            CREATE TABLE IF NOT EXISTS alert_cases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL, vehicle_id TEXT NOT NULL, type TEXT NOT NULL,
                severity TEXT NOT NULL, message TEXT NOT NULL, observed_at TEXT,
                quality TEXT NOT NULL, evidence_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACTIVE','ACKNOWLEDGED','RESOLVED')),
                current INTEGER NOT NULL CHECK(current IN (0,1)),
                started_at TEXT NOT NULL, updated_at TEXT NOT NULL, closed_at TEXT,
                resolution TEXT, note TEXT, actor_role TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS alert_current_kind
                ON alert_cases(vehicle_id,type) WHERE current=1;
            CREATE INDEX IF NOT EXISTS alert_history_tenant
                ON alert_cases(tenant_id,id DESC);
            CREATE TABLE IF NOT EXISTS driver_credentials (
                id INTEGER PRIMARY KEY AUTOINCREMENT, vehicle_id TEXT NOT NULL,
                driver_id TEXT NOT NULL, tenant_id TEXT NOT NULL, token_hash TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL, revoked_at TEXT
            );
            CREATE INDEX IF NOT EXISTS driver_credentials_vehicle
                ON driver_credentials(vehicle_id,driver_id,revoked_at);
            CREATE TABLE IF NOT EXISTS driver_consent_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tenant_id TEXT NOT NULL, driver_id TEXT NOT NULL, vehicle_id TEXT NOT NULL,
                purpose TEXT NOT NULL CHECK(purpose='PHONE_TRIP_GPS_IMU'),
                granted INTEGER NOT NULL CHECK(granted IN (0,1)),
                occurred_at TEXT NOT NULL, actor_id TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS driver_consent_history
                ON driver_consent_events(tenant_id,driver_id,purpose,id);
            CREATE TABLE IF NOT EXISTS gps_batches (
                trip_id TEXT NOT NULL, batch_id TEXT NOT NULL, fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL, PRIMARY KEY(trip_id,batch_id)
            );
            CREATE TABLE IF NOT EXISTS gps_samples (
                trip_id TEXT NOT NULL, sequence INTEGER NOT NULL, observed_at TEXT NOT NULL,
                received_at TEXT NOT NULL, payload_json TEXT NOT NULL, quality TEXT NOT NULL,
                PRIMARY KEY(trip_id,sequence)
            );
            CREATE INDEX IF NOT EXISTS gps_samples_by_trip
                ON gps_samples(trip_id,sequence);
            CREATE TABLE IF NOT EXISTS erased_phone_trips (
                trip_id TEXT PRIMARY KEY, tenant_id TEXT NOT NULL, driver_id TEXT NOT NULL,
                erased_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS driver_phone_data_erasures (
                id INTEGER PRIMARY KEY AUTOINCREMENT, tenant_id TEXT NOT NULL,
                driver_id TEXT NOT NULL, erased_at TEXT NOT NULL,
                deleted_samples INTEGER NOT NULL, deleted_batches INTEGER NOT NULL,
                actor_id TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS ota_campaigns (
                campaign_id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL,
                manifest_json TEXT NOT NULL, result TEXT NOT NULL DEFAULT 'PENDING',
                version_after TEXT, created_at TEXT NOT NULL,
                prior_version TEXT
            );
            CREATE TABLE IF NOT EXISTS trip_sessions (
                trip_id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, driver_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ACTIVE','COMPLETED')),
                source TEXT NOT NULL DEFAULT 'OPERATOR_SUPERVISED',
                started_at TEXT NOT NULL, ended_at TEXT,
                starting_soc_pct REAL, ending_soc_pct REAL,
                origin_lat REAL, origin_lon REAL, destination_lat REAL, destination_lon REAL,
                start_key TEXT NOT NULL, start_fingerprint TEXT NOT NULL,
                complete_key TEXT, complete_fingerprint TEXT,
                UNIQUE(vehicle_id, start_key)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_active_trip_per_vehicle
                ON trip_sessions(vehicle_id) WHERE status='ACTIVE';
            CREATE TABLE IF NOT EXISTS trip_stops (
                trip_id TEXT NOT NULL, stop_id TEXT NOT NULL, kind TEXT NOT NULL
                    CHECK(kind IN ('WAITING','CHARGING')),
                status TEXT NOT NULL CHECK(status IN ('OPEN','COMPLETED')),
                source TEXT NOT NULL DEFAULT 'DRIVER_REPORTED',
                started_at TEXT NOT NULL, ended_at TEXT,
                start_key TEXT NOT NULL, complete_key TEXT,
                PRIMARY KEY(trip_id,stop_id), UNIQUE(trip_id,start_key)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_open_stop_per_trip
                ON trip_stops(trip_id) WHERE status='OPEN';
            CREATE TABLE IF NOT EXISTS daily_plans (
                plan_id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, driver_id TEXT NOT NULL,
                service_date TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('DRAFT','CONFIRMED')),
                plan_json TEXT NOT NULL, result_json TEXT, created_at TEXT NOT NULL, confirmed_at TEXT,
                draft_key TEXT NOT NULL, draft_fingerprint TEXT NOT NULL, confirmation_key TEXT,
                revision INTEGER NOT NULL DEFAULT 1, edit_key TEXT, edit_fingerprint TEXT,
                UNIQUE(vehicle_id, service_date), UNIQUE(vehicle_id, draft_key)
            );
            CREATE TABLE IF NOT EXISTS daily_plan_edits (
                plan_id TEXT NOT NULL, edit_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                result_json TEXT NOT NULL, PRIMARY KEY(plan_id,edit_key)
            );
            CREATE TABLE IF NOT EXISTS driver_nudges (
                nudge_id TEXT PRIMARY KEY, vehicle_id TEXT NOT NULL, driver_id TEXT NOT NULL,
                tenant_id TEXT NOT NULL, plan_id TEXT,
                kind TEXT NOT NULL CHECK(kind IN ('DEPARTURE','CHARGING','ROUTE_REVIEW')),
                message TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('PENDING','ACKNOWLEDGED','DECLINED')),
                created_at TEXT NOT NULL, responded_at TEXT,
                create_key TEXT NOT NULL, fingerprint TEXT NOT NULL,
                response_key TEXT, response_fingerprint TEXT,
                UNIQUE(vehicle_id,create_key)
            );
            CREATE INDEX IF NOT EXISTS driver_nudges_inbox
                ON driver_nudges(vehicle_id,driver_id,created_at DESC);
        """)
        # Existing local databases predate the denormalized trip ID column.
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(events)")}
        if "trip_id" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE events ADD COLUMN trip_id TEXT")
                self.db.execute("UPDATE events SET trip_id=json_extract(payload_json, '$.trip_id')")
        self.db.execute("CREATE INDEX IF NOT EXISTS events_by_trip ON events(vehicle_id, trip_id, sequence)")
        audit_columns = {row[1] for row in self.db.execute("PRAGMA table_info(audit)")}
        if "actor_id" not in audit_columns:
            with self.db:
                self.db.execute("ALTER TABLE audit ADD COLUMN actor_id TEXT")
        trip_columns = {row[1] for row in self.db.execute("PRAGMA table_info(trip_sessions)")}
        if "source" not in trip_columns:
            with self.db:
                self.db.execute("ALTER TABLE trip_sessions ADD COLUMN source TEXT NOT NULL DEFAULT 'OPERATOR_SUPERVISED'")
        vehicle_columns = {row[1] for row in self.db.execute("PRAGMA table_info(vehicles)")}
        if "tenant_id" not in vehicle_columns:
            with self.db:
                self.db.execute("ALTER TABLE vehicles ADD COLUMN tenant_id TEXT NOT NULL DEFAULT 'default'")
        self.db.execute("CREATE INDEX IF NOT EXISTS vehicles_by_tenant ON vehicles(tenant_id,vehicle_id)")
        campaign_columns = {row[1] for row in self.db.execute("PRAGMA table_info(ota_campaigns)")}
        if "prior_version" not in campaign_columns:
            with self.db:
                self.db.execute("ALTER TABLE ota_campaigns ADD COLUMN prior_version TEXT")
        plan_columns = {row[1] for row in self.db.execute("PRAGMA table_info(daily_plans)")}
        with self.db:
            if "revision" not in plan_columns:
                self.db.execute("ALTER TABLE daily_plans ADD COLUMN revision INTEGER NOT NULL DEFAULT 1")
            if "edit_key" not in plan_columns:
                self.db.execute("ALTER TABLE daily_plans ADD COLUMN edit_key TEXT")
            if "edit_fingerprint" not in plan_columns:
                self.db.execute("ALTER TABLE daily_plans ADD COLUMN edit_fingerprint TEXT")
        from .notifications import NotificationStore
        self.notifications = NotificationStore(self)

    @property
    def db(self) -> sqlite3.Connection:
        if self._closed:
            raise RuntimeError("cloud store is closed")
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = _connect(self._path)
            self._local.connection = connection
            with self._connections_lock:
                if self._closed:
                    connection.close()
                    raise RuntimeError("cloud store is closed")
                self._connections.append(connection)
        return connection

    def close(self) -> None:
        """Close every request-thread connection after callers have stopped."""
        with self._write_lock, self._connections_lock:
            if self._closed:
                return
            self._closed = True
            for connection in self._connections:
                connection.close()
            self._connections.clear()

    def register(self, profile: VehicleProfile, driver_id: str | None = None, tenant_id: str = "default") -> None:
        with self._write_lock, self.db:
            existing = self.db.execute("SELECT tenant_id FROM vehicles WHERE vehicle_id=?", (profile.vehicle_id,)).fetchone()
            if existing and existing["tenant_id"] != tenant_id:
                raise ValueError("vehicle belongs to another tenant")
            self.db.execute("INSERT INTO vehicles(vehicle_id,profile_json,driver_id,tenant_id) VALUES(?,?,?,?) ON CONFLICT(vehicle_id) DO UPDATE SET profile_json=excluded.profile_json, driver_id=excluded.driver_id", (profile.vehicle_id, profile.model_dump_json(), driver_id, tenant_id))

    def vehicle_tenant(self, vehicle_id: str) -> str | None:
        row = self.db.execute("SELECT tenant_id FROM vehicles WHERE vehicle_id=?", (vehicle_id,)).fetchone()
        return row[0] if row else None

    @staticmethod
    def _change_scope(audience: str, tenant_id: str,
                      vehicle_id: str = "", driver_id: str = "") -> str:
        return json.dumps([audience, tenant_id, vehicle_id, driver_id], separators=(",", ":"))

    def publish_change(self, tenant_id: str, vehicle_id: str, driver_id: str | None,
                       kind: str, resource_id: str, occurred_at: str) -> None:
        """Write a bounded, audience-scoped hint inside the caller's transaction.

        Callers hold _write_lock and an open SQLite transaction. The hint carries
        no message body, coordinates, or credentials; clients re-fetch protected
        resources after receiving it.
        """
        scopes = [self._change_scope("operator", tenant_id),
                  self._change_scope("operator", "*")]
        if driver_id:
            scopes.append(self._change_scope("driver", tenant_id, vehicle_id, driver_id))
        for scope in scopes:
            sequence = self.db.execute(
                "SELECT COALESCE(MAX(sequence),0)+1 FROM client_changes WHERE scope=?",
                (scope,),
            ).fetchone()[0]
            self.db.execute(
                "INSERT INTO client_changes(scope,sequence,kind,vehicle_id,resource_id,occurred_at) "
                "VALUES(?,?,?,?,?,?)",
                (scope, sequence, kind, vehicle_id, resource_id, occurred_at),
            )
            self.db.execute(
                "DELETE FROM client_changes WHERE scope=? AND sequence<=?",
                (scope, sequence - 2048),
            )

    def publish_vehicle_change(self, vehicle_id: str, driver_id: str | None,
                               kind: str, resource_id: str, occurred_at: str) -> None:
        vehicle = self.db.execute(
            "SELECT tenant_id FROM vehicles WHERE vehicle_id=?", (vehicle_id,),
        ).fetchone()
        if vehicle is None:
            raise KeyError("vehicle not found")
        self.publish_change(vehicle["tenant_id"], vehicle_id, driver_id,
                            kind, resource_id, occurred_at)

    def changes(self, audience: str, tenant_id: str, after: int,
                vehicle_id: str = "", driver_id: str = "") -> dict:
        scope = self._change_scope(audience, tenant_id, vehicle_id, driver_id)
        bounds = self.db.execute(
            "SELECT MIN(sequence),MAX(sequence) FROM client_changes WHERE scope=?",
            (scope,),
        ).fetchone()
        first, last = bounds[0], bounds[1]
        if after > (last or 0):
            return {"events": [], "next_cursor": last or 0, "reset_required": True}
        if first is not None and after < first - 1:
            return {"events": [], "next_cursor": last, "reset_required": True}
        rows = self.db.execute(
            "SELECT sequence,kind,vehicle_id,resource_id,occurred_at FROM client_changes "
            "WHERE scope=? AND sequence>? ORDER BY sequence LIMIT 100",
            (scope, after),
        ).fetchall()
        return {"events": [dict(row) for row in rows],
                "next_cursor": rows[-1]["sequence"] if rows else after,
                "reset_required": False}

    def ingest(self, frames: list[SignalFrame]) -> dict:
        accepted = duplicate = 0
        with self._write_lock, self.db:
            for frame in frames:
                if frame.observed_at.timestamp() > datetime.now(timezone.utc).timestamp() + 300:
                    raise ValueError("observed_at is too far in the future")
                if not self.db.execute("SELECT 1 FROM vehicles WHERE vehicle_id=?", (frame.vehicle_id,)).fetchone():
                    raise ValueError(f"unregistered vehicle: {frame.vehicle_id}")
                payload = frame.model_dump_json(exclude_unset=True)
                existing = self.db.execute("SELECT payload_json FROM events WHERE vehicle_id=? AND sequence=?", (frame.vehicle_id, frame.sequence)).fetchone()
                if existing:
                    previous = SignalFrame.model_validate_json(existing["payload_json"])
                    if previous.model_dump(mode="json") != frame.model_dump(mode="json"):
                        raise ValueError("sequence conflict")
                    duplicate += 1
                    continue
                previous_latest = self.db.execute(
                    "SELECT sequence,observed_at FROM events WHERE vehicle_id=? "
                    "ORDER BY sequence DESC LIMIT 1", (frame.vehicle_id,),
                ).fetchone()
                self.db.execute(
                    "INSERT INTO events(vehicle_id,sequence,observed_at,received_at,payload_json,trip_id) VALUES(?,?,?,?,?,?)",
                    (frame.vehicle_id, frame.sequence, frame.observed_at.isoformat(), datetime.now(timezone.utc).isoformat(), payload, frame.trip_id),
                )
                if (previous_latest is None or
                        frame.sequence > previous_latest["sequence"] and
                        frame.observed_at >= datetime.fromisoformat(previous_latest["observed_at"])):
                    self.notifications.evaluate_signal(frame, self.vehicle_tenant(frame.vehicle_id))
                accepted += 1
        return {"accepted": accepted, "duplicate": duplicate, "acknowledged": [{"vehicle_id": f.vehicle_id, "sequence": f.sequence} for f in frames]}

    def vehicle_ids(self, tenant_id: str = "*") -> list[str]:
        if tenant_id == "*":
            rows = self.db.execute("SELECT vehicle_id FROM vehicles ORDER BY vehicle_id")
        else:
            rows = self.db.execute("SELECT vehicle_id FROM vehicles WHERE tenant_id=? ORDER BY vehicle_id", (tenant_id,))
        return [row[0] for row in rows]

    def twin(self, vehicle_id: str) -> dict | None:
        vehicle = self.db.execute("SELECT * FROM vehicles WHERE vehicle_id=?", (vehicle_id,)).fetchone()
        if vehicle is None:
            return None
        event = self.db.execute("SELECT * FROM events WHERE vehicle_id=? ORDER BY sequence DESC LIMIT 1", (vehicle_id,)).fetchone()
        frame = SignalFrame.model_validate_json(event["payload_json"]) if event else None
        quality = frame.quality() if frame else Quality.MISSING
        usable = quality in (Quality.OK, Quality.SIMULATED)
        return {
            "vehicle_id": vehicle_id,
            "profile": json.loads(vehicle["profile_json"]),
            "driver_id": vehicle["driver_id"],
            "software_version": vehicle["software_version"],
            "state": infer_state(frame).value if usable else "OFFLINE" if quality == Quality.STALE else "UNKNOWN",
            "quality": quality.value,
            "observed_at": frame.observed_at.isoformat() if frame else None,
            "received_at": event["received_at"] if event else None,
            "battery": {"soc_pct": frame.soc_pct, "soh_pct": frame.soh_pct, "temperature_c": frame.battery_temperature_c} if frame else None,
            "location": {"latitude": frame.latitude, "longitude": frame.longitude} if frame and frame.latitude is not None else None,
            "health": health(frame) if usable else {"status": "UNKNOWN", "evidence": [], "confidence": "NONE"},
            "fault_code": frame.fault_code if frame else None,
            "last_sequence": frame.sequence if frame else None,
        }

    def events(self, vehicle_id: str, limit: int = 100) -> list[dict]:
        rows = self.db.execute("SELECT payload_json,received_at FROM events WHERE vehicle_id=? ORDER BY sequence DESC LIMIT ?", (vehicle_id, limit)).fetchall()
        return [{"frame": json.loads(r["payload_json"]), "received_at": r["received_at"]} for r in rows]

    def trip_ids(self, vehicle_id: str) -> list[str]:
        rows = self.db.execute(
            "SELECT trip_id FROM trip_sessions WHERE vehicle_id=? UNION SELECT trip_id FROM events WHERE vehicle_id=? AND trip_id IS NOT NULL",
            (vehicle_id, vehicle_id),
        ).fetchall()
        return [row[0] for row in rows]

    def trip_frames(self, vehicle_id: str, trip_id: str, limit: int = 5000) -> list[dict]:
        rows = self.db.execute(
            "SELECT payload_json FROM (SELECT payload_json,sequence FROM events WHERE vehicle_id=? AND trip_id=? ORDER BY sequence DESC LIMIT ?) ORDER BY sequence",
            (vehicle_id, trip_id, limit),
        ).fetchall()
        return [json.loads(row[0]) for row in rows]

    def trip_endpoints(self, vehicle_id: str, trip_id: str) -> tuple[dict | None, dict | None]:
        first = self.db.execute(
            "SELECT payload_json FROM events WHERE vehicle_id=? AND trip_id=? ORDER BY sequence LIMIT 1",
            (vehicle_id, trip_id),
        ).fetchone()
        last = self.db.execute(
            "SELECT payload_json FROM events WHERE vehicle_id=? AND trip_id=? ORDER BY sequence DESC LIMIT 1",
            (vehicle_id, trip_id),
        ).fetchone()
        return (json.loads(first[0]) if first else None, json.loads(last[0]) if last else None)

    def trip_frame_count(self, vehicle_id: str, trip_id: str) -> int:
        return self.db.execute(
            "SELECT COUNT(*) FROM events WHERE vehicle_id=? AND trip_id=?", (vehicle_id, trip_id)
        ).fetchone()[0]

    def fleet_report(self, days: int, tenant_id: str = "*") -> dict:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        tenant_clause = "" if tenant_id == "*" else " AND e.vehicle_id IN (SELECT vehicle_id FROM vehicles WHERE tenant_id=?)"
        parameters = (cutoff,) if tenant_id == "*" else (cutoff, tenant_id)
        rows = self.db.execute("""
            SELECT e.vehicle_id, COUNT(*) AS telemetry_frames,
                   ROUND(AVG(CAST(json_extract(payload_json, '$.soc_pct') AS REAL)), 2) AS average_soc_pct,
                   ROUND(AVG(CAST(json_extract(payload_json, '$.speed_kmh') AS REAL)), 2) AS average_speed_kmh,
                   SUM(CASE WHEN CAST(json_extract(payload_json, '$.soc_pct') AS REAL) < 20 THEN 1 ELSE 0 END) AS low_soc_frames,
                   SUM(CASE WHEN json_extract(payload_json, '$.charging') = 1 THEN 1 ELSE 0 END) AS charging_frames,
                   SUM(CASE WHEN json_extract(payload_json, '$.source') = 'SIMULATOR' THEN 1 ELSE 0 END) AS simulated_frames,
                   SUM(CASE WHEN json_extract(payload_json, '$.source') = 'CAR_API' THEN 1 ELSE 0 END) AS car_api_frames,
                   SUM(CASE WHEN json_extract(payload_json, '$.source') = 'REPLAY' THEN 1 ELSE 0 END) AS replay_frames,
                   SUM(CASE WHEN json_extract(payload_json, '$.source') = 'OBD' THEN 1 ELSE 0 END) AS obd_frames,
                   SUM(CASE WHEN json_extract(payload_json, '$.source') = 'CAN' THEN 1 ELSE 0 END) AS can_frames
            FROM events e WHERE e.received_at >= ?""" + tenant_clause + " GROUP BY e.vehicle_id ORDER BY e.vehicle_id", parameters).fetchall()
        vehicles = [dict(row) for row in rows]
        frames = sum(row["telemetry_frames"] for row in vehicles)
        simulated = sum(row["simulated_frames"] for row in vehicles)
        car_api = sum(row["car_api_frames"] for row in vehicles)
        replayed = sum(row["replay_frames"] for row in vehicles)
        obd = sum(row["obd_frames"] for row in vehicles)
        can = sum(row["can_frames"] for row in vehicles)
        overall = self.db.execute("""
            SELECT ROUND(AVG(CAST(json_extract(payload_json, '$.soc_pct') AS REAL)), 2),
                   ROUND(AVG(CAST(json_extract(payload_json, '$.speed_kmh') AS REAL)), 2)
            FROM events e WHERE e.received_at >= ?
        """ + tenant_clause, parameters).fetchone()
        return {
            "window_days": days, "telemetry_frames": frames,
            "average_soc_pct": overall[0], "average_speed_kmh": overall[1],
            "low_soc_frames": sum(row["low_soc_frames"] for row in vehicles),
            "charging_frames": sum(row["charging_frames"] for row in vehicles),
            "simulated_frames": simulated, "car_api_frames": car_api, "replay_frames": replayed,
            "obd_frames": obd, "can_frames": can,
            "quality": "MISSING" if not frames else "SIMULATED" if simulated == frames else "REPLAYED" if replayed == frames else "REPORTED" if car_api + obd + can == frames else "MIXED",
            "driver_score": None, "driver_score_reason": "INSUFFICIENT_BEHAVIOR_SIGNALS",
            "vehicles": vehicles,
        }

    def telemetry_metrics(self, now: datetime | None = None,
                          tenant_id: str = "*") -> dict:
        """Latest-frame connectivity without exposing vehicle identifiers."""
        current = now or datetime.now(timezone.utc)
        rows = self.db.execute("""
            SELECT e.payload_json, e.received_at FROM vehicles v
            LEFT JOIN events e ON e.vehicle_id=v.vehicle_id AND e.sequence=(
                SELECT MAX(inner_event.sequence) FROM events inner_event
                WHERE inner_event.vehicle_id=v.vehicle_id
            )
            WHERE (?='*' OR v.tenant_id=?)
        """, (tenant_id, tenant_id)).fetchall()
        quality_counts = {quality.value: 0 for quality in Quality}
        source_counts = {source: 0 for source in
                         ("SIMULATOR", "REPLAY", "CAR_API", "CAN", "OBD")}
        latencies: list[float] = []
        for row in rows:
            if row["payload_json"] is None:
                quality_counts[Quality.MISSING.value] += 1
                continue
            frame = SignalFrame.model_validate_json(row["payload_json"])
            quality_counts[frame.quality(current).value] += 1
            source_counts[frame.source] += 1
            received_at = datetime.fromisoformat(row["received_at"])
            if received_at.tzinfo is not None:
                lag = (received_at - frame.observed_at).total_seconds() * 1000
                if lag >= 0:
                    latencies.append(lag)
        return {
            "registered_vehicles": len(rows),
            "fresh_vehicles": quality_counts[Quality.OK.value] +
                              quality_counts[Quality.SIMULATED.value],
            "quality_counts": quality_counts,
            "source_counts": source_counts,
            "latest_upload_latency_ms_avg":
                round(sum(latencies) / len(latencies), 3) if latencies else None,
            "latest_upload_latency_sample_count": len(latencies),
        }

    def trip_session(self, vehicle_id: str, trip_id: str) -> dict | None:
        row = self.db.execute(
            "SELECT * FROM trip_sessions WHERE vehicle_id=? AND trip_id=?", (vehicle_id, trip_id)
        ).fetchone()
        return dict(row) if row else None

    def daily_plan(self, vehicle_id: str, plan_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM daily_plans WHERE vehicle_id=? AND plan_id=?", (vehicle_id, plan_id)).fetchone()
        return self._plan_row(row) if row else None

    @staticmethod
    def _plan_row(row: sqlite3.Row) -> dict:
        return {
            "plan_id": row["plan_id"], "vehicle_id": row["vehicle_id"], "driver_id": row["driver_id"],
            "service_date": row["service_date"], "status": row["status"],
            **json.loads(row["plan_json"]), "result": json.loads(row["result_json"]) if row["result_json"] else None,
            "revision": row["revision"],
            "created_at": row["created_at"], "confirmed_at": row["confirmed_at"],
        }

    def daily_plans(self, vehicle_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM daily_plans WHERE vehicle_id=? ORDER BY service_date DESC LIMIT 100", (vehicle_id,)
        ).fetchall()
        return [self._plan_row(row) for row in rows]

    def create_daily_plan(self, vehicle_id: str, plan_id: str, key: str, plan: dict) -> tuple[dict, bool]:
        fingerprint = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        with self._write_lock, self.db:
            vehicle = self.db.execute("SELECT driver_id FROM vehicles WHERE vehicle_id=?", (vehicle_id,)).fetchone()
            if vehicle is None:
                raise KeyError("vehicle not found")
            if not vehicle["driver_id"]:
                raise ValueError("vehicle has no assigned driver")
            existing = self.db.execute(
                "SELECT * FROM daily_plans WHERE plan_id=? OR (vehicle_id=? AND draft_key=?) OR (vehicle_id=? AND service_date=?)",
                (plan_id, vehicle_id, key, vehicle_id, plan["service_date"]),
            ).fetchone()
            if existing:
                if existing["vehicle_id"] == vehicle_id and existing["plan_id"] == plan_id and existing["draft_key"] == key and existing["draft_fingerprint"] == fingerprint:
                    return self._plan_row(existing), False
                raise ValueError("daily plan conflicts with an existing draft or service date")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "INSERT INTO daily_plans(plan_id,vehicle_id,driver_id,service_date,status,plan_json,created_at,draft_key,draft_fingerprint) VALUES(?,?,?,?,?,?,?,?,?)",
                (plan_id, vehicle_id, vehicle["driver_id"], plan["service_date"], "DRAFT", fingerprint, now, key, fingerprint),
            )
            self.publish_vehicle_change(vehicle_id, None, "PLAN_DRAFTED", plan_id, now)
            return self.daily_plan(vehicle_id, plan_id), True

    def edit_daily_plan(self, vehicle_id: str, plan_id: str, key: str, expected_revision: int,
                        plan: dict) -> tuple[dict, bool]:
        fingerprint = json.dumps(plan, sort_keys=True, separators=(",", ":"))
        with self._write_lock, self.db:
            row = self.db.execute(
                "SELECT * FROM daily_plans WHERE vehicle_id=? AND plan_id=?", (vehicle_id, plan_id)
            ).fetchone()
            if row is None:
                raise KeyError("daily plan not found")
            replay = self.db.execute(
                "SELECT fingerprint,result_json FROM daily_plan_edits WHERE plan_id=? AND edit_key=?",
                (plan_id, key),
            ).fetchone()
            if replay:
                if replay["fingerprint"] == fingerprint:
                    return json.loads(replay["result_json"]), False
                raise ValueError("edit key was used with different plan content")
            if row["status"] != "DRAFT":
                raise ValueError("confirmed daily plan cannot be edited")
            if row["revision"] != expected_revision:
                raise ValueError("daily plan revision changed; reload before editing")
            collision = self.db.execute(
                "SELECT 1 FROM daily_plans WHERE vehicle_id=? AND service_date=? AND plan_id!=?",
                (vehicle_id, plan["service_date"], plan_id),
            ).fetchone()
            if collision:
                raise ValueError("another daily plan exists for this service date")
            self.db.execute(
                "UPDATE daily_plans SET service_date=?,plan_json=?,revision=revision+1,edit_key=?,edit_fingerprint=? WHERE plan_id=?",
                (plan["service_date"], fingerprint, key, fingerprint, plan_id),
            )
            updated = self.daily_plan(vehicle_id, plan_id)
            self.db.execute(
                "INSERT INTO daily_plan_edits(plan_id,edit_key,fingerprint,result_json) VALUES(?,?,?,?)",
                (plan_id, key, fingerprint, json.dumps(updated)),
            )
            self.publish_vehicle_change(vehicle_id, None, "PLAN_EDITED", plan_id,
                                        datetime.now(timezone.utc).isoformat())
            return updated, True

    def confirm_daily_plan(self, vehicle_id: str, plan_id: str, key: str, result: dict) -> tuple[dict, bool]:
        with self._write_lock, self.db:
            row = self.db.execute("SELECT * FROM daily_plans WHERE vehicle_id=? AND plan_id=?", (vehicle_id, plan_id)).fetchone()
            if row is None:
                raise KeyError("daily plan not found")
            if row["status"] == "CONFIRMED":
                if row["confirmation_key"] == key:
                    return self._plan_row(row), False
                raise ValueError("daily plan already confirmed")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "UPDATE daily_plans SET status='CONFIRMED',result_json=?,confirmed_at=?,confirmation_key=? WHERE plan_id=?",
                (json.dumps(result), now, key, plan_id),
            )
            self.publish_vehicle_change(vehicle_id, row["driver_id"], "PLAN_CONFIRMED", plan_id, now)
            return self.daily_plan(vehicle_id, plan_id), True

    def start_trip(self, vehicle_id: str, trip_id: str, key: str, soc: float | None, origin: dict | None,
                   source: str = "OPERATOR_SUPERVISED") -> tuple[dict, bool]:
        fingerprint_data = {"trip_id": trip_id, "soc": soc, "origin": origin}
        if source != "OPERATOR_SUPERVISED":
            fingerprint_data["source"] = source
        fingerprint = json.dumps(fingerprint_data, sort_keys=True)
        with self._write_lock, self.db:
            vehicle = self.db.execute("SELECT driver_id FROM vehicles WHERE vehicle_id=?", (vehicle_id,)).fetchone()
            if vehicle is None:
                raise KeyError("vehicle not found")
            if not vehicle["driver_id"]:
                raise ValueError("vehicle has no assigned driver")
            existing = self.db.execute(
                "SELECT * FROM trip_sessions WHERE trip_id=? OR (vehicle_id=? AND start_key=?)",
                (trip_id, vehicle_id, key),
            ).fetchone()
            if existing:
                if existing["vehicle_id"] == vehicle_id and existing["start_key"] == key and existing["start_fingerprint"] == fingerprint:
                    return dict(existing), False
                raise ValueError("trip start conflicts with a previous command")
            if self.db.execute("SELECT 1 FROM trip_sessions WHERE vehicle_id=? AND status='ACTIVE'", (vehicle_id,)).fetchone():
                raise ValueError("vehicle already has an active trip")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "INSERT INTO trip_sessions(trip_id,vehicle_id,driver_id,status,source,started_at,starting_soc_pct,origin_lat,origin_lon,start_key,start_fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (trip_id, vehicle_id, vehicle["driver_id"], "ACTIVE", source, now, soc,
                 origin["latitude"] if origin else None, origin["longitude"] if origin else None, key, fingerprint),
            )
            self.publish_vehicle_change(vehicle_id, vehicle["driver_id"], "TRIP_STARTED", trip_id, now)
            return dict(self.db.execute("SELECT * FROM trip_sessions WHERE trip_id=?", (trip_id,)).fetchone()), True

    def complete_trip(self, vehicle_id: str, trip_id: str, key: str, soc: float | None, destination: dict | None) -> tuple[dict, bool]:
        fingerprint = json.dumps({"soc": soc, "destination": destination}, sort_keys=True)
        with self._write_lock, self.db:
            existing = self.db.execute(
                "SELECT * FROM trip_sessions WHERE vehicle_id=? AND trip_id=?", (vehicle_id, trip_id)
            ).fetchone()
            if existing is None:
                raise KeyError("trip not found")
            if existing["status"] == "COMPLETED":
                if existing["complete_key"] == key and existing["complete_fingerprint"] == fingerprint:
                    return dict(existing), False
                raise ValueError("trip completion conflicts with a previous command")
            if self.db.execute("SELECT 1 FROM trip_stops WHERE trip_id=? AND status='OPEN'", (trip_id,)).fetchone():
                raise ValueError("complete the open trip stop before ending the trip")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "UPDATE trip_sessions SET status='COMPLETED', ended_at=?, ending_soc_pct=?, destination_lat=?, destination_lon=?, complete_key=?, complete_fingerprint=? WHERE trip_id=?",
                (now, soc, destination["latitude"] if destination else None,
                 destination["longitude"] if destination else None, key, fingerprint, trip_id),
            )
            self.publish_vehicle_change(vehicle_id, existing["driver_id"], "TRIP_COMPLETED", trip_id, now)
            return dict(self.db.execute("SELECT * FROM trip_sessions WHERE trip_id=?", (trip_id,)).fetchone()), True

    def trip_stops(self, vehicle_id: str, trip_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT stop_id,kind,status,source,started_at,ended_at FROM trip_stops WHERE trip_id=? ORDER BY started_at,stop_id",
            (trip_id,),
        ).fetchall()
        stops = []
        for row in rows:
            stop = dict(row)
            stop["reconciliation"] = "NOT_APPLICABLE"
            if stop["kind"] == "CHARGING":
                end = stop["ended_at"] or datetime.now(timezone.utc).isoformat()
                corroborated = self.db.execute(
                    "SELECT 1 FROM events WHERE vehicle_id=? AND (trip_id IS NULL OR trip_id=?) "
                    "AND julianday(observed_at)>=julianday(?) AND julianday(observed_at)<=julianday(?) "
                    "AND json_extract(payload_json,'$.source')='CAR_API' "
                    "AND json_extract(payload_json,'$.charging')=1 LIMIT 1",
                    (vehicle_id, trip_id, stop["started_at"], end),
                ).fetchone()
                stop["reconciliation"] = "CAR_API_CORROBORATED" if corroborated else "UNVERIFIED"
            stops.append(stop)
        return stops

    def daily_operations(self, start_utc: datetime, end_utc: datetime,
                         tenant_id: str = "*") -> dict:
        """Bounded trip-start cohort with explicitly driver-reported stop durations."""
        clause = "" if tenant_id == "*" else " AND v.tenant_id=?"
        parameters = (start_utc.isoformat(), end_utc.isoformat())
        if tenant_id != "*":
            parameters += (tenant_id,)
        from_sql = (" FROM trip_sessions t JOIN vehicles v ON v.vehicle_id=t.vehicle_id "
                    "WHERE t.started_at>=? AND t.started_at<?" + clause)
        total = self.db.execute("SELECT COUNT(*)" + from_sql, parameters).fetchone()[0]
        sessions = [dict(row) for row in self.db.execute(
            "SELECT t.trip_id,t.vehicle_id,t.driver_id,t.status,t.source,t.started_at,t.ended_at"
            + from_sql + " ORDER BY t.started_at,t.trip_id LIMIT 500", parameters).fetchall()]
        stops_by_trip: dict[str, list[dict]] = {session["trip_id"]: [] for session in sessions}
        if sessions:
            markers = ",".join("?" for _ in sessions)
            rows = self.db.execute(
                f"SELECT trip_id,kind,status,started_at,ended_at FROM trip_stops WHERE trip_id IN ({markers})",
                tuple(stops_by_trip),
            ).fetchall()
            for row in rows:
                stops_by_trip[row["trip_id"]].append(dict(row))
        drivers: dict[str, dict] = {}
        for session in sessions:
            driver_id = session["driver_id"]
            report = drivers.setdefault(driver_id, {"driver_id": driver_id, "trips_started": 0,
                                                    "trips_completed": 0, "wait_minutes": 0.0,
                                                    "charging_stop_minutes": 0.0, "open_stops": 0})
            report["trips_started"] += 1
            report["trips_completed"] += session["status"] == "COMPLETED"
            for stop in stops_by_trip[session["trip_id"]]:
                if stop["status"] == "OPEN":
                    report["open_stops"] += 1
                    continue
                seconds = max(0, (datetime.fromisoformat(stop["ended_at"])
                                  - datetime.fromisoformat(stop["started_at"])).total_seconds())
                field = "wait_minutes" if stop["kind"] == "WAITING" else "charging_stop_minutes"
                report[field] += seconds / 60
        for report in drivers.values():
            report["wait_minutes"] = round(report["wait_minutes"], 1)
            report["charging_stop_minutes"] = round(report["charging_stop_minutes"], 1)
        driver_reports = sorted(drivers.values(), key=lambda row: row["driver_id"])
        return {"trips_started": len(sessions),
                "trips_completed": sum(row["trips_completed"] for row in driver_reports),
                "wait_minutes": round(sum(row["wait_minutes"] for row in driver_reports), 1),
                "charging_stop_minutes": round(sum(row["charging_stop_minutes"] for row in driver_reports), 1),
                "open_stops": sum(row["open_stops"] for row in driver_reports),
                "driver_reports": driver_reports, "total_trip_records": total,
                "truncated": total > len(sessions),
                "trip_source_counts": {
                    "DRIVER_REPORTED": sum(row["source"] == "DRIVER_REPORTED" for row in sessions),
                    "OPERATOR_SUPERVISED": sum(row["source"] == "OPERATOR_SUPERVISED" for row in sessions),
                },
                "charging_energy_kwh": None, "money_saved_inr": None, "orders_completed": None}

    def create_nudge(self, vehicle_id: str, nudge_id: str, key: str, kind: str,
                     message: str, plan_id: str | None) -> tuple[dict, bool]:
        fingerprint = json.dumps({"kind": kind, "message": message, "plan_id": plan_id}, sort_keys=True)
        with self._write_lock, self.db:
            vehicle = self.db.execute("SELECT driver_id,tenant_id FROM vehicles WHERE vehicle_id=?",
                                      (vehicle_id,)).fetchone()
            if vehicle is None:
                raise KeyError("vehicle not found")
            if not vehicle["driver_id"]:
                raise ValueError("vehicle has no assigned driver")
            prior = self.db.execute("SELECT * FROM driver_nudges WHERE nudge_id=? OR (vehicle_id=? AND create_key=?)",
                                    (nudge_id, vehicle_id, key)).fetchone()
            if prior:
                if (prior["nudge_id"] == nudge_id and prior["vehicle_id"] == vehicle_id
                        and prior["create_key"] == key and prior["fingerprint"] == fingerprint):
                    return dict(prior), False
                raise ValueError("nudge conflicts with a previous command")
            if plan_id:
                plan = self.db.execute("SELECT status,driver_id FROM daily_plans WHERE vehicle_id=? AND plan_id=?",
                                       (vehicle_id, plan_id)).fetchone()
                if plan is None or plan["driver_id"] != vehicle["driver_id"] or plan["status"] != "CONFIRMED":
                    raise ValueError("linked daily plan must be confirmed for the assigned driver")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "INSERT INTO driver_nudges(nudge_id,vehicle_id,driver_id,tenant_id,plan_id,kind,message,status,created_at,create_key,fingerprint) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (nudge_id, vehicle_id, vehicle["driver_id"], vehicle["tenant_id"], plan_id,
                 kind, message, "PENDING", now, key, fingerprint),
            )
            self.publish_change(vehicle["tenant_id"], vehicle_id, vehicle["driver_id"],
                                "ADVISORY_CREATED", nudge_id, now)
            return dict(self.db.execute("SELECT * FROM driver_nudges WHERE nudge_id=?", (nudge_id,)).fetchone()), True

    def nudges(self, vehicle_id: str, driver_id: str | None = None) -> list[dict]:
        clause = " AND driver_id=?" if driver_id else ""
        parameters = (vehicle_id, driver_id) if driver_id else (vehicle_id,)
        rows = self.db.execute(
            "SELECT nudge_id,vehicle_id,driver_id,plan_id,kind,message,status,created_at,responded_at "
            "FROM driver_nudges WHERE vehicle_id=?" + clause + " ORDER BY created_at DESC,nudge_id DESC LIMIT 100",
            parameters,
        ).fetchall()
        return [dict(row) for row in rows]

    def respond_nudge(self, vehicle_id: str, driver_id: str, nudge_id: str,
                      key: str, outcome: str) -> tuple[dict, bool]:
        fingerprint = json.dumps({"outcome": outcome}, sort_keys=True)
        with self._write_lock, self.db:
            row = self.db.execute("SELECT * FROM driver_nudges WHERE vehicle_id=? AND driver_id=? AND nudge_id=?",
                                  (vehicle_id, driver_id, nudge_id)).fetchone()
            if row is None:
                raise KeyError("nudge not found")
            if row["status"] != "PENDING":
                if row["response_key"] == key and row["response_fingerprint"] == fingerprint:
                    return dict(row), False
                raise ValueError("nudge already has an outcome")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "UPDATE driver_nudges SET status=?,responded_at=?,response_key=?,response_fingerprint=? WHERE nudge_id=?",
                (outcome, now, key, fingerprint, nudge_id),
            )
            self.publish_change(row["tenant_id"], vehicle_id, driver_id,
                                "ADVISORY_" + outcome, nudge_id, now)
            return dict(self.db.execute("SELECT * FROM driver_nudges WHERE nudge_id=?", (nudge_id,)).fetchone()), True

    def start_trip_stop(self, vehicle_id: str, driver_id: str, trip_id: str,
                        stop_id: str, key: str, kind: str) -> tuple[dict, bool]:
        with self._write_lock, self.db:
            trip = self.db.execute("SELECT status FROM trip_sessions WHERE vehicle_id=? AND driver_id=? AND trip_id=?",
                                   (vehicle_id, driver_id, trip_id)).fetchone()
            if trip is None:
                raise KeyError("driver trip not found")
            prior = self.db.execute("SELECT * FROM trip_stops WHERE trip_id=? AND (stop_id=? OR start_key=?)",
                                    (trip_id, stop_id, key)).fetchone()
            if prior:
                if prior["stop_id"] == stop_id and prior["start_key"] == key and prior["kind"] == kind:
                    return dict(prior), False
                raise ValueError("trip stop start conflicts with a previous command")
            if trip["status"] != "ACTIVE":
                raise ValueError("trip is not active")
            if self.db.execute("SELECT 1 FROM trip_stops WHERE trip_id=? AND status='OPEN'", (trip_id,)).fetchone():
                raise ValueError("trip already has an open stop")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "INSERT INTO trip_stops(trip_id,stop_id,kind,status,started_at,start_key) VALUES(?,?,?,?,?,?)",
                (trip_id, stop_id, kind, "OPEN", now, key),
            )
            self.publish_vehicle_change(vehicle_id, driver_id, "TRIP_STOP_STARTED", stop_id, now)
            return dict(self.db.execute("SELECT * FROM trip_stops WHERE trip_id=? AND stop_id=?",
                                        (trip_id, stop_id)).fetchone()), True

    def complete_trip_stop(self, vehicle_id: str, driver_id: str, trip_id: str,
                           stop_id: str, key: str) -> tuple[dict, bool]:
        with self._write_lock, self.db:
            trip = self.db.execute("SELECT status FROM trip_sessions WHERE vehicle_id=? AND driver_id=? AND trip_id=?",
                                   (vehicle_id, driver_id, trip_id)).fetchone()
            if trip is None:
                raise KeyError("driver trip not found")
            stop = self.db.execute("SELECT * FROM trip_stops WHERE trip_id=? AND stop_id=?",
                                   (trip_id, stop_id)).fetchone()
            if stop is None:
                raise KeyError("trip stop not found")
            if stop["status"] == "COMPLETED":
                if stop["complete_key"] == key:
                    return dict(stop), False
                raise ValueError("trip stop completion conflicts with a previous command")
            if trip["status"] != "ACTIVE":
                raise ValueError("trip is not active")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute("UPDATE trip_stops SET status='COMPLETED',ended_at=?,complete_key=? WHERE trip_id=? AND stop_id=?",
                            (now, key, trip_id, stop_id))
            self.publish_vehicle_change(vehicle_id, driver_id, "TRIP_STOP_COMPLETED", stop_id, now)
            return dict(self.db.execute("SELECT * FROM trip_stops WHERE trip_id=? AND stop_id=?",
                                        (trip_id, stop_id)).fetchone()), True

    def audit(self, action: str, vehicle_id: str | None, detail: str, actor_id: str | None = None) -> None:
        with self._write_lock, self.db:
            self.db.execute("INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                            (datetime.now(timezone.utc).isoformat(), action, vehicle_id, detail, actor_id))

    def audit_events(self, vehicle_id: str, before_id: int | None = None,
                     limit: int = 50) -> dict:
        """Page a single vehicle's audit trail without exposing other tenants."""
        if not 1 <= limit <= 100 or before_id is not None and before_id < 1:
            raise ValueError("invalid audit page")
        query = ("SELECT id,at,action,detail,actor_id FROM audit "
                 "WHERE vehicle_id=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?")
        rows = self.db.execute(query, (vehicle_id, before_id, before_id, limit + 1)).fetchall()
        more = len(rows) > limit
        page = [dict(row) for row in rows[:limit]]
        return {"events": page, "next_before": page[-1]["id"] if more else None}

    @staticmethod
    def _alert_row(row: sqlite3.Row) -> dict:
        result = dict(row)
        result["id"] = f"A-{row['id']}"
        result["evidence"] = json.loads(row["evidence_json"])
        result["source"] = "RULE_BASED"
        result["current"] = bool(row["current"])
        del result["evidence_json"]
        return result

    def sync_alerts(self, alerts: list[dict], tenant_id: str) -> list[dict]:
        """Preserve one incident per active vehicle/rule until the rule clears."""
        now = datetime.now(timezone.utc).isoformat()
        active = {(alert["vehicle_id"], alert["type"]) for alert in alerts}
        with self._write_lock, self.db:
            for alert in alerts:
                vehicle_id, kind = alert["vehicle_id"], alert["type"]
                current = self.db.execute(
                    "SELECT id FROM alert_cases WHERE vehicle_id=? AND type=? AND current=1",
                    (vehicle_id, kind),
                ).fetchone()
                values = (alert["severity"], alert["message"], alert["observed_at"],
                          alert["quality"], json.dumps(alert["evidence"]), now)
                if current:
                    self.db.execute(
                        "UPDATE alert_cases SET severity=?,message=?,observed_at=?,quality=?,evidence_json=?,updated_at=? WHERE id=?",
                        (*values, current["id"]),
                    )
                else:
                    owner = self.vehicle_tenant(vehicle_id)
                    if owner is None or (tenant_id != "*" and owner != tenant_id):
                        raise ValueError("alert vehicle is outside tenant")
                    self.db.execute(
                        "INSERT INTO alert_cases(tenant_id,vehicle_id,type,severity,message,observed_at,quality,evidence_json,status,current,started_at,updated_at) "
                        "VALUES(?,?,?,?,?,?,?,?, 'ACTIVE',1,?,?)",
                        (owner, vehicle_id, kind, *values[:5], now, now),
                    )
            clause = "" if tenant_id == "*" else " AND tenant_id=?"
            params = () if tenant_id == "*" else (tenant_id,)
            current_rows = self.db.execute(
                "SELECT id,vehicle_id,type,status FROM alert_cases WHERE current=1" + clause, params
            ).fetchall()
            for row in current_rows:
                if (row["vehicle_id"], row["type"]) not in active:
                    self.db.execute(
                        "UPDATE alert_cases SET current=0,status='RESOLVED',closed_at=COALESCE(closed_at,?),"
                        "resolution=COALESCE(resolution,'CONDITION_CLEARED'),updated_at=? WHERE id=?",
                        (now, now, row["id"]),
                    )
            rows = self.db.execute(
                "SELECT * FROM alert_cases WHERE current=1 AND status!='RESOLVED'" + clause +
                " ORDER BY CASE severity WHEN 'CRITICAL' THEN 0 ELSE 1 END, id DESC", params
            ).fetchall()
            return [self._alert_row(row) for row in rows]

    def alert_history(self, tenant_id: str, limit: int = 100) -> list[dict]:
        clause = "" if tenant_id == "*" else " WHERE tenant_id=?"
        params = (limit,) if tenant_id == "*" else (tenant_id, limit)
        rows = self.db.execute(
            "SELECT * FROM alert_cases" + clause + " ORDER BY id DESC LIMIT ?", params
        ).fetchall()
        return [self._alert_row(row) for row in rows]

    def update_alert(self, alert_id: int, tenant_id: str, action: str, note: str | None, actor_role: str) -> dict:
        with self._write_lock, self.db:
            clause = "" if tenant_id == "*" else " AND tenant_id=?"
            params = (alert_id,) if tenant_id == "*" else (alert_id, tenant_id)
            row = self.db.execute("SELECT * FROM alert_cases WHERE id=?" + clause, params).fetchone()
            if row is None:
                raise KeyError("alert not found")
            if row["status"] == "RESOLVED" and action == "acknowledge":
                raise ValueError("resolved alert cannot be acknowledged")
            if (action == "acknowledge" and row["status"] == "ACKNOWLEDGED") or (action == "resolve" and row["status"] == "RESOLVED"):
                return self._alert_row(row)
            status = "ACKNOWLEDGED" if action == "acknowledge" else "RESOLVED"
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "UPDATE alert_cases SET status=?,note=?,actor_role=?,updated_at=?,closed_at=?,resolution=? WHERE id=?",
                (status, note, actor_role, now, now if status == "RESOLVED" else None,
                 "OPERATOR" if status == "RESOLVED" else None, alert_id),
            )
            return self._alert_row(self.db.execute("SELECT * FROM alert_cases WHERE id=?", (alert_id,)).fetchone())

    def issue_driver_credential(self, vehicle_id: str, token_hash: str) -> dict:
        with self._write_lock, self.db:
            vehicle = self.db.execute(
                "SELECT driver_id,tenant_id FROM vehicles WHERE vehicle_id=?", (vehicle_id,)
            ).fetchone()
            if vehicle is None:
                raise KeyError("vehicle not found")
            if not vehicle["driver_id"]:
                raise ValueError("vehicle has no assigned driver")
            now = datetime.now(timezone.utc).isoformat()
            self.db.execute(
                "UPDATE driver_credentials SET revoked_at=? WHERE vehicle_id=? AND revoked_at IS NULL",
                (now, vehicle_id),
            )
            cursor = self.db.execute(
                "INSERT INTO driver_credentials(vehicle_id,driver_id,tenant_id,token_hash,created_at) VALUES(?,?,?,?,?)",
                (vehicle_id, vehicle["driver_id"], vehicle["tenant_id"], token_hash, now),
            )
            return {"credential_id": f"D-{cursor.lastrowid}", "vehicle_id": vehicle_id,
                    "driver_id": vehicle["driver_id"], "created_at": now}

    def driver_by_hash(self, token_hash: str) -> dict | None:
        row = self.db.execute(
            "SELECT c.* FROM driver_credentials c JOIN vehicles v ON v.vehicle_id=c.vehicle_id "
            "WHERE c.token_hash=? AND c.revoked_at IS NULL AND v.driver_id=c.driver_id AND v.tenant_id=c.tenant_id",
            (token_hash,),
        ).fetchone()
        return dict(row) if row else None

    def driver_credentials(self, vehicle_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT id,driver_id,created_at,revoked_at FROM driver_credentials WHERE vehicle_id=? ORDER BY id DESC LIMIT 20",
            (vehicle_id,),
        ).fetchall()
        return [{"credential_id": f"D-{row['id']}", "driver_id": row["driver_id"],
                 "created_at": row["created_at"], "revoked_at": row["revoked_at"]} for row in rows]

    def revoke_driver_credential(self, credential_id: int, tenant_id: str) -> bool:
        with self._write_lock, self.db:
            clause = "" if tenant_id == "*" else " AND tenant_id=?"
            params = (credential_id,) if tenant_id == "*" else (credential_id, tenant_id)
            row = self.db.execute("SELECT id FROM driver_credentials WHERE id=?" + clause, params).fetchone()
            if row is None:
                return False
            self.db.execute("UPDATE driver_credentials SET revoked_at=COALESCE(revoked_at,?) WHERE id=?",
                            (datetime.now(timezone.utc).isoformat(), credential_id))
            return True

    def phone_trip_consent(self, tenant_id: str, driver_id: str) -> dict:
        row = self.db.execute(
            "SELECT granted,occurred_at FROM driver_consent_events "
            "WHERE tenant_id=? AND driver_id=? AND purpose='PHONE_TRIP_GPS_IMU' ORDER BY id DESC LIMIT 1",
            (tenant_id, driver_id),
        ).fetchone()
        return {"purpose": "PHONE_TRIP_GPS_IMU", "granted": bool(row["granted"]) if row else False,
                "updated_at": row["occurred_at"] if row else None}

    def set_phone_trip_consent(self, tenant_id: str, driver_id: str, vehicle_id: str,
                               granted: bool) -> dict:
        with self._write_lock, self.db:
            current = self.phone_trip_consent(tenant_id, driver_id)
            if current["granted"] == granted:
                return current
            now = datetime.now(timezone.utc).isoformat()
            actor = f"driver:{driver_id}"
            self.db.execute(
                "INSERT INTO driver_consent_events(tenant_id,driver_id,vehicle_id,purpose,granted,occurred_at,actor_id) "
                "VALUES(?,?,?,'PHONE_TRIP_GPS_IMU',?,?,?)",
                (tenant_id, driver_id, vehicle_id, int(granted), now, actor),
            )
            self.db.execute("INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                            (now, "driver_phone_consent_granted" if granted else "driver_phone_consent_withdrawn",
                             vehicle_id, "PHONE_TRIP_GPS_IMU", actor))
            return {"purpose": "PHONE_TRIP_GPS_IMU", "granted": granted, "updated_at": now}

    def export_driver_phone_data(self, tenant_id: str, driver_id: str,
                                 limit: int = 1000, offset: int = 0) -> dict:
        scope = (tenant_id, driver_id)
        with self._write_lock, self.db:
            total = self.db.execute(
                "SELECT COUNT(*) FROM gps_samples s JOIN trip_sessions t ON t.trip_id=s.trip_id "
                "JOIN vehicles v ON v.vehicle_id=t.vehicle_id WHERE v.tenant_id=? AND t.driver_id=?",
                scope,
            ).fetchone()[0]
            rows = self.db.execute(
                "SELECT s.trip_id,s.sequence,s.observed_at,s.received_at,s.payload_json,s.quality "
                "FROM gps_samples s JOIN trip_sessions t ON t.trip_id=s.trip_id "
                "JOIN vehicles v ON v.vehicle_id=t.vehicle_id WHERE v.tenant_id=? AND t.driver_id=? "
                "ORDER BY s.trip_id,s.sequence LIMIT ? OFFSET ?",
                (*scope, limit, offset),
            ).fetchall()
            self.db.execute("INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                            (datetime.now(timezone.utc).isoformat(), "driver_phone_data_exported", None,
                             f"tenant={tenant_id};samples={len(rows)};offset={offset}", f"driver:{driver_id}"))
            return {"total": total, "limit": limit, "offset": offset,
                    "next_offset": offset + len(rows) if offset + len(rows) < total else None,
                    "samples": [{"trip_id": row["trip_id"], "sequence": row["sequence"],
                                 "observed_at": row["observed_at"], "received_at": row["received_at"],
                                 "quality": row["quality"], "point": json.loads(row["payload_json"])}
                                for row in rows]}

    def erase_driver_phone_data(self, tenant_id: str, driver_id: str, vehicle_id: str) -> dict:
        with self._write_lock, self.db:
            trips = self.db.execute(
                "SELECT t.trip_id,t.status FROM trip_sessions t JOIN vehicles v ON v.vehicle_id=t.vehicle_id "
                "WHERE v.tenant_id=? AND t.driver_id=?", (tenant_id, driver_id),
            ).fetchall()
            if any(row["status"] == "ACTIVE" for row in trips):
                raise ValueError("complete all active trips before erasing phone data")
            now = datetime.now(timezone.utc).isoformat()
            actor = f"driver:{driver_id}"
            if self.phone_trip_consent(tenant_id, driver_id)["granted"]:
                self.db.execute(
                    "INSERT INTO driver_consent_events(tenant_id,driver_id,vehicle_id,purpose,granted,occurred_at,actor_id) "
                    "VALUES(?,?,?,'PHONE_TRIP_GPS_IMU',0,?,?)", (tenant_id, driver_id, vehicle_id, now, actor),
                )
                self.db.execute("INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                                (now, "driver_phone_consent_withdrawn", vehicle_id, "PHONE_TRIP_GPS_IMU", actor))
            deleted_samples = deleted_batches = 0
            for trip in trips:
                trip_id = trip["trip_id"]
                deleted_samples += self.db.execute("DELETE FROM gps_samples WHERE trip_id=?", (trip_id,)).rowcount
                deleted_batches += self.db.execute("DELETE FROM gps_batches WHERE trip_id=?", (trip_id,)).rowcount
                self.db.execute("INSERT OR IGNORE INTO erased_phone_trips(trip_id,tenant_id,driver_id,erased_at) "
                                "VALUES(?,?,?,?)", (trip_id, tenant_id, driver_id, now))
            self.db.execute("INSERT INTO driver_phone_data_erasures(tenant_id,driver_id,erased_at,"
                            "deleted_samples,deleted_batches,actor_id) VALUES(?,?,?,?,?,?)",
                            (tenant_id, driver_id, now, deleted_samples, deleted_batches, actor))
            self.db.execute("INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                            (now, "driver_phone_data_erased", vehicle_id,
                             f"tenant={tenant_id};samples={deleted_samples};batches={deleted_batches}", actor))
            return {"deleted_samples": deleted_samples, "deleted_batches": deleted_batches,
                    "erased_at": now}

    def purge_expired_phone_data(self, retention_days: int, dry_run: bool = False,
                                 now: datetime | None = None) -> dict:
        if not isinstance(retention_days, int) or isinstance(retention_days, bool) or retention_days < 1:
            raise ValueError("retention_days must be a positive integer")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("now must include a timezone")
        cutoff = current.astimezone(timezone.utc) - timedelta(days=retention_days)
        with self._write_lock, self.db:
            rows = self.db.execute(
                "SELECT t.trip_id,t.driver_id,t.ended_at,v.tenant_id FROM trip_sessions t "
                "JOIN vehicles v ON v.vehicle_id=t.vehicle_id WHERE t.status='COMPLETED' "
                "AND (EXISTS(SELECT 1 FROM gps_samples s WHERE s.trip_id=t.trip_id) "
                "OR EXISTS(SELECT 1 FROM gps_batches b WHERE b.trip_id=t.trip_id))"
            ).fetchall()
            eligible = [row for row in rows if datetime.fromisoformat(row["ended_at"]).astimezone(timezone.utc) < cutoff]
            samples = batches = 0
            if not dry_run:
                for row in eligible:
                    trip_id = row["trip_id"]
                    samples += self.db.execute("DELETE FROM gps_samples WHERE trip_id=?", (trip_id,)).rowcount
                    batches += self.db.execute("DELETE FROM gps_batches WHERE trip_id=?", (trip_id,)).rowcount
                    self.db.execute("INSERT OR IGNORE INTO erased_phone_trips(trip_id,tenant_id,driver_id,erased_at) "
                                    "VALUES(?,?,?,?)", (trip_id, row["tenant_id"], row["driver_id"],
                                                       current.astimezone(timezone.utc).isoformat()))
                if eligible:
                    self.db.execute("INSERT INTO audit(at,action,vehicle_id,detail,actor_id) VALUES(?,?,?,?,?)",
                                    (current.astimezone(timezone.utc).isoformat(), "phone_data_retention_purged",
                                     None, f"trips={len(eligible)};samples={samples};batches={batches}", "system:retention"))
            return {"retention_days": retention_days, "cutoff": cutoff.isoformat(),
                    "dry_run": dry_run, "eligible_trips": len(eligible),
                    "deleted_samples": samples, "deleted_batches": batches}

    def ingest_gps_batch(self, vehicle_id: str, driver_id: str, trip_id: str, batch_id: str,
                         points: list[dict]) -> dict:
        fingerprint = json.dumps(points, sort_keys=True, separators=(",", ":"))
        with self._write_lock, self.db:
            trip = self.db.execute(
                "SELECT t.*,v.tenant_id FROM trip_sessions t JOIN vehicles v ON v.vehicle_id=t.vehicle_id "
                "WHERE t.trip_id=? AND t.vehicle_id=? AND t.driver_id=?",
                (trip_id, vehicle_id, driver_id),
            ).fetchone()
            if trip is None:
                raise KeyError("driver trip not found")
            if self.db.execute("SELECT 1 FROM erased_phone_trips WHERE trip_id=?", (trip_id,)).fetchone():
                raise ConsentRequiredError("phone data for this trip was erased")
            previous = self.db.execute(
                "SELECT fingerprint,result_json FROM gps_batches WHERE trip_id=? AND batch_id=?",
                (trip_id, batch_id),
            ).fetchone()
            if previous:
                if previous["fingerprint"] != fingerprint:
                    raise ValueError("GPS batch ID was reused with different points")
                return json.loads(previous["result_json"])
            if self._phone_retention_days is not None and trip["ended_at"]:
                ended = datetime.fromisoformat(trip["ended_at"]).astimezone(timezone.utc)
                if ended < datetime.now(timezone.utc) - timedelta(days=self._phone_retention_days):
                    raise ConsentRequiredError("phone data retention period for this trip expired")
            events = self.db.execute(
                "SELECT granted,occurred_at FROM driver_consent_events "
                "WHERE tenant_id=? AND driver_id=? AND purpose='PHONE_TRIP_GPS_IMU' ORDER BY id",
                (trip["tenant_id"], driver_id),
            ).fetchall()
            if not events or not events[-1]["granted"]:
                raise ConsentRequiredError("phone trip GPS and motion consent is required")
            intervals = [(datetime.fromisoformat(row["occurred_at"]), bool(row["granted"])) for row in events]
            accepted = duplicate = 0
            now = datetime.now(timezone.utc)
            for point in points:
                observed = datetime.fromisoformat(point["timestamp"])
                observed_utc = observed.astimezone(timezone.utc)
                if not any(granted and start <= observed_utc
                           and (index + 1 == len(intervals) or observed_utc < intervals[index + 1][0])
                           for index, (start, granted) in enumerate(intervals)):
                    raise ConsentRequiredError("GPS sample was observed outside a granted consent interval")
                if observed.astimezone(timezone.utc) > now + timedelta(minutes=5):
                    raise ValueError("GPS timestamp is too far in the future")
                if observed.astimezone(timezone.utc) < datetime.fromisoformat(trip["started_at"]) - timedelta(minutes=5):
                    raise ValueError("GPS timestamp predates the trip")
                if trip["ended_at"] and observed.astimezone(timezone.utc) > datetime.fromisoformat(trip["ended_at"]) + timedelta(minutes=5):
                    raise ValueError("GPS timestamp is after the trip")
                payload = json.dumps(point, sort_keys=True, separators=(",", ":"))
                existing = self.db.execute(
                    "SELECT payload_json FROM gps_samples WHERE trip_id=? AND sequence=?",
                    (trip_id, point["sequence"]),
                ).fetchone()
                if existing:
                    if existing["payload_json"] != payload:
                        raise ValueError("GPS sequence conflicts with an earlier sample")
                    duplicate += 1
                    continue
                quality = "SIMULATED" if point.get("mocked") else "LOW_ACCURACY" if point.get("accuracy") is not None and point["accuracy"] > 100 else "REPORTED"
                self.db.execute(
                    "INSERT INTO gps_samples(trip_id,sequence,observed_at,received_at,payload_json,quality) VALUES(?,?,?,?,?,?)",
                    (trip_id, point["sequence"], point["timestamp"], now.isoformat(), payload, quality),
                )
                accepted += 1
            result = {"batch_id": batch_id, "accepted": accepted, "duplicate": duplicate}
            self.db.execute(
                "INSERT INTO gps_batches(trip_id,batch_id,fingerprint,result_json) VALUES(?,?,?,?)",
                (trip_id, batch_id, fingerprint, json.dumps(result)),
            )
            return result

    def gps_trip_summary(self, trip_id: str, limit: int = 5000) -> dict:
        counts = self.db.execute(
            "SELECT COUNT(*),SUM(CASE WHEN quality='REPORTED' THEN 1 ELSE 0 END),"
            "SUM(CASE WHEN COALESCE(json_extract(payload_json,'$.imu.accelerometer_samples'),0)>0 "
            "OR COALESCE(json_extract(payload_json,'$.imu.gyroscope_samples'),0)>0 THEN 1 ELSE 0 END),"
            "SUM(COALESCE(json_extract(payload_json,'$.imu.accelerometer_samples'),0)),"
            "SUM(COALESCE(json_extract(payload_json,'$.imu.gyroscope_samples'),0)) "
            "FROM gps_samples WHERE trip_id=?",
            (trip_id,),
        ).fetchone()
        rows = self.db.execute(
            "SELECT sequence,observed_at,payload_json FROM gps_samples WHERE trip_id=? AND quality='REPORTED' "
            "ORDER BY sequence DESC LIMIT ?", (trip_id, limit),
        ).fetchall()
        trace = [{"sequence": row["sequence"], "observed_at": row["observed_at"],
                  "latitude": json.loads(row["payload_json"])["lat"],
                  "longitude": json.loads(row["payload_json"])["lng"], "source": "PHONE_GPS"}
                 for row in reversed(rows)]
        return {"sample_count": counts[0], "usable_count": counts[1] or 0,
                "imu_window_count": counts[2] or 0,
                "imu_accelerometer_samples": counts[3] or 0,
                "imu_gyroscope_samples": counts[4] or 0,
                "trace": trace, "trace_truncated": (counts[1] or 0) > len(trace)}

    def create_campaign(self, campaign_id: str, vehicle_id: str, manifest: dict) -> bool:
        validate_simulator_manifest(manifest)
        with self._write_lock, self.db:
            vehicle = self.db.execute("SELECT software_version FROM vehicles WHERE vehicle_id=?",
                                      (vehicle_id,)).fetchone()
            if vehicle is None:
                raise ValueError("unregistered vehicle")
            existing = self.db.execute("SELECT vehicle_id,manifest_json FROM ota_campaigns WHERE campaign_id=?",
                                       (campaign_id,)).fetchone()
            normalized = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
            if existing:
                if existing["vehicle_id"] == vehicle_id and existing["manifest_json"] == normalized:
                    return False
                raise ValueError("campaign ID conflicts with a previous manifest")
            self.db.execute(
                "INSERT INTO ota_campaigns(campaign_id,vehicle_id,manifest_json,created_at,prior_version) "
                "VALUES(?,?,?,?,?)",
                (campaign_id, vehicle_id, normalized, datetime.now(timezone.utc).isoformat(),
                 vehicle["software_version"]),
            )
            self.notifications.record_ota_event(
                vehicle_id, self.vehicle_tenant(vehicle_id), campaign_id, "PENDING")
            return True

    def campaigns(self, vehicle_id: str) -> list[dict]:
        rows = self.db.execute("SELECT * FROM ota_campaigns WHERE vehicle_id=? ORDER BY created_at, campaign_id", (vehicle_id,)).fetchall()
        return [{"campaign_id": r["campaign_id"], "vehicle_id": r["vehicle_id"], "manifest": json.loads(r["manifest_json"]), "result": r["result"], "version_after": r["version_after"], "prior_version": r["prior_version"]} for r in rows]

    def report_campaign(self, campaign_id: str, vehicle_id: str, result: str, version_after: str) -> bool:
        if result not in ("INSTALLED", "ROLLED_BACK", "BLOCKED_VEHICLE_STATE", "REJECTED_SIGNATURE"):
            raise ValueError("invalid simulator OTA result")
        with self._write_lock, self.db:
            campaign = self.db.execute(
                "SELECT result,version_after,prior_version,manifest_json FROM ota_campaigns "
                "WHERE campaign_id=? AND vehicle_id=?", (campaign_id, vehicle_id),
            ).fetchone()
            if campaign is None:
                raise KeyError("campaign not found")
            if campaign["result"] != "PENDING":
                if campaign["result"] == result and campaign["version_after"] == version_after:
                    return False
                raise ValueError("completed simulator OTA campaign cannot be changed")
            if campaign["prior_version"] is None:
                raise ValueError("legacy campaign has no trusted prior version")
            target_version = json.loads(campaign["manifest_json"])["version"]
            expected = target_version if result == "INSTALLED" else campaign["prior_version"]
            if version_after != expected:
                raise ValueError("reported version does not match campaign state")
            self.db.execute("UPDATE ota_campaigns SET result=?, version_after=? "
                            "WHERE campaign_id=? AND vehicle_id=? AND result='PENDING'",
                            (result, version_after, campaign_id, vehicle_id))
            if result == "INSTALLED":
                self.db.execute("UPDATE vehicles SET software_version=? WHERE vehicle_id=?", (version_after, vehicle_id))
            else:
                self.notifications.record_ota_event(
                    vehicle_id, self.vehicle_tenant(vehicle_id), campaign_id, result)
            return True


class Outbox:
    def __init__(self, path: str):
        self.db = _connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS outbox (sequence INTEGER PRIMARY KEY, payload_json TEXT NOT NULL, acknowledged INTEGER NOT NULL DEFAULT 0)")
        self.db.commit()

    def append(self, frame: SignalFrame) -> None:
        with self.db:
            if self.count_pending() >= 100000:
                raise OverflowError("vehicle outbox full; ingestion stopped to prevent silent loss")
            self.db.execute("INSERT INTO outbox(sequence,payload_json) VALUES(?,?)",
                            (frame.sequence, frame.model_dump_json(exclude_unset=True)))

    def pending(self, limit: int = 100) -> list[SignalFrame]:
        rows = self.db.execute("SELECT payload_json FROM outbox WHERE acknowledged=0 ORDER BY sequence LIMIT ?", (limit,)).fetchall()
        return [SignalFrame.model_validate_json(row[0]) for row in rows]

    def acknowledge(self, sequences: list[int]) -> None:
        with self.db:
            self.db.executemany("UPDATE outbox SET acknowledged=1 WHERE sequence=?", [(x,) for x in sequences])
            # Keep the newest frame for restart continuity; cloud-confirmed
            # history otherwise grows the vehicle database without bound.
            self.db.execute("DELETE FROM outbox WHERE acknowledged=1 AND "
                            "sequence < (SELECT MAX(sequence) FROM outbox)")

    def count_pending(self) -> int:
        return self.db.execute("SELECT COUNT(*) FROM outbox WHERE acknowledged=0").fetchone()[0]

    def last_frame(self) -> SignalFrame | None:
        row = self.db.execute("SELECT payload_json FROM outbox ORDER BY sequence DESC LIMIT 1").fetchone()
        return SignalFrame.model_validate_json(row[0]) if row else None
