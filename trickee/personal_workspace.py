"""Private productivity records. Identity is always derived by the API, never a request body."""

from __future__ import annotations

import json
import hashlib
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from .recurrence import Recurrence, occurrences

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, model_validator


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Preferences(StrictModel):
    timezone: str = "UTC"
    reminders: bool = True
    display_name: str = Field(default="", max_length=80)

    @model_validator(mode="after")
    def valid_zone(self):
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise ValueError("Select an IANA time zone") from None
        return self


class CalendarEvent(StrictModel):
    request_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{8,80}$")
    title: str = Field(min_length=1, max_length=120)
    description: str = Field(default="", max_length=2000)
    starts_at: datetime
    ends_at: datetime
    timezone: str = "UTC"
    category: Literal["TASK", "MAINTENANCE", "CHARGING", "APPOINTMENT"] = "TASK"
    remind_minutes: int = Field(default=15, ge=0, le=10080)
    completed: bool = False
    recurrence: Recurrence | None = None

    @model_validator(mode="after")
    def valid_time(self):
        Preferences(timezone=self.timezone)
        if self.starts_at.tzinfo is None or self.ends_at.tzinfo is None:
            raise ValueError("Dates require an explicit UTC offset")
        if self.ends_at <= self.starts_at:
            raise ValueError("End must follow start")
        if (self.ends_at - self.starts_at).total_seconds() > 366 * 86400:
            raise ValueError("Event cannot exceed one year")
        try:
            occurrences(self.starts_at, self.ends_at, self.timezone, self.recurrence)
        except (OverflowError, OSError):
            raise ValueError(
                "Recurrence extends beyond supported calendar dates"
            ) from None
        return self


class CalendarUpdate(CalendarEvent):
    revision: int = Field(ge=1)
    scope: Literal["occurrence", "series"] = "occurrence"
    series_revision: int | None = Field(default=None, ge=1)


class Conversation(StrictModel):
    title: str = Field(default="Daily review", min_length=1, max_length=120)


class Message(StrictModel):
    message: str = Field(min_length=1, max_length=2000)
    request_id: str = Field(pattern=r"^[A-Za-z0-9_-]{8,80}$")


class NoticeUpdate(StrictModel):
    read: bool = True
    archived: bool = False


class WorkspaceStore:
    def __init__(self, cloud):
        self.cloud = cloud
        cloud.db.executescript("""
        CREATE TABLE IF NOT EXISTS personal_series(
            owner TEXT NOT NULL, series_id TEXT PRIMARY KEY, revision INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS personal_series_events(
            owner TEXT NOT NULL, event_id TEXT PRIMARY KEY, series_id TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS personal_series_members ON personal_series_events(owner,series_id);
        CREATE TABLE IF NOT EXISTS personal_preferences(owner TEXT PRIMARY KEY, payload TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS personal_events(
            owner TEXT NOT NULL, event_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
            starts_at TEXT NOT NULL, ends_at TEXT NOT NULL, reminder_at TEXT NOT NULL,
            revision INTEGER NOT NULL, completed INTEGER NOT NULL, updated_at TEXT NOT NULL,
            deleted_at TEXT);
        CREATE INDEX IF NOT EXISTS personal_event_owner ON personal_events(owner,starts_at,event_id);
        CREATE TABLE IF NOT EXISTS personal_event_commands(
            owner TEXT NOT NULL, request_id TEXT NOT NULL, payload_hash TEXT NOT NULL,
            event_id TEXT NOT NULL, PRIMARY KEY(owner,request_id));
        CREATE TABLE IF NOT EXISTS personal_conversations(
            owner TEXT NOT NULL, conversation_id TEXT PRIMARY KEY, title TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS personal_conversation_owner ON personal_conversations(owner,created_at);
        CREATE TABLE IF NOT EXISTS personal_messages(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, conversation_id TEXT NOT NULL,
            request_id TEXT NOT NULL, speaker TEXT NOT NULL, content TEXT NOT NULL, created_at TEXT NOT NULL,
            UNIQUE(owner,conversation_id,request_id,speaker));
        CREATE INDEX IF NOT EXISTS personal_message_owner ON personal_messages(owner,conversation_id,id);
        CREATE TABLE IF NOT EXISTS personal_notices(
            notice_id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, event_id TEXT NOT NULL,
            revision INTEGER NOT NULL, message TEXT NOT NULL, created_at TEXT NOT NULL,
            read INTEGER NOT NULL DEFAULT 0, archived INTEGER NOT NULL DEFAULT 0,
            UNIQUE(owner,event_id,revision));
        CREATE INDEX IF NOT EXISTS personal_notice_owner ON personal_notices(owner,archived,notice_id);
        CREATE TABLE IF NOT EXISTS personal_workspace_audit(
            id INTEGER PRIMARY KEY AUTOINCREMENT, owner TEXT NOT NULL, action TEXT NOT NULL,
            resource_id TEXT NOT NULL, occurred_at TEXT NOT NULL);
        """)

    @contextmanager
    def transaction(self):
        # Serialize read/modify/write across API workers, not only Python threads.
        with self.cloud._write_lock, self.cloud.db:
            self.cloud.db.execute("BEGIN IMMEDIATE")
            yield

    def audit(self, owner, action, resource):
        self.cloud.db.execute(
            "INSERT INTO personal_workspace_audit(owner,action,resource_id,occurred_at) VALUES(?,?,?,?)",
            (owner, action, resource, now()),
        )

    def preferences(self, owner):
        row = self.cloud.db.execute(
            "SELECT payload FROM personal_preferences WHERE owner=?", (owner,)
        ).fetchone()
        return json.loads(row["payload"]) if row else Preferences().model_dump()

    def save_preferences(self, owner, body):
        with self.transaction():
            self.cloud.db.execute(
                "INSERT INTO personal_preferences VALUES(?,?) ON CONFLICT(owner) DO UPDATE SET payload=excluded.payload",
                (owner, body.model_dump_json()),
            )
            self.audit(owner, "preferences_updated", "preferences")
        return body.model_dump()

    def event_row(self, row):
        link = self.cloud.db.execute(
            "SELECT s.series_id,s.revision FROM personal_series s JOIN personal_series_events e ON s.series_id=e.series_id WHERE e.owner=? AND e.event_id=? AND s.owner=e.owner",
            (row["owner"], row["event_id"]),
        ).fetchone()
        return {
            **json.loads(row["payload"]),
            "event_id": row["event_id"],
            "revision": row["revision"],
            "updated_at": row["updated_at"],
            "series_id": link["series_id"] if link else None,
            "series_revision": link["revision"] if link else None,
        }

    def event(self, owner, identifier):
        row = self.cloud.db.execute(
            "SELECT * FROM personal_events WHERE owner=? AND event_id=? AND deleted_at IS NULL",
            (owner, identifier),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Event not found")
        return row

    def events(self, owner, after=None, before=None, q="", offset=0, limit=100):
        where = "owner=? AND deleted_at IS NULL"
        values = [owner]
        if after:
            where += " AND ends_at>=?"
            values.append(after)
        if before:
            where += " AND starts_at<?"
            values.append(before)
        if q:
            where += " AND lower(json_extract(payload,'$.title') || ' ' || json_extract(payload,'$.description')) LIKE ? ESCAPE '\\'"
            literal = (
                q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            )
            values.append("%" + literal + "%")
        total = self.cloud.db.execute(
            "SELECT count(*) FROM personal_events WHERE " + where, values
        ).fetchone()[0]
        rows = self.cloud.db.execute(
            "SELECT * FROM personal_events WHERE "
            + where
            + " ORDER BY starts_at,event_id LIMIT ? OFFSET ?",
            (*values, limit, offset),
        ).fetchall()
        return {
            "items": [self.event_row(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def save_event(self, owner, body, identifier=None):
        from datetime import timedelta

        payload = body.model_dump(
            mode="json", exclude={"revision", "request_id", "scope", "series_revision"}
        )
        payload.update(
            starts_at=body.starts_at.astimezone(timezone.utc).isoformat(),
            ends_at=body.ends_at.astimezone(timezone.utc).isoformat(),
        )
        fingerprint = hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode()
        ).hexdigest()
        with self.transaction():
            if not identifier and body.request_id:
                previous = self.cloud.db.execute(
                    "SELECT * FROM personal_event_commands WHERE owner=? AND request_id=?",
                    (owner, body.request_id),
                ).fetchone()
                if previous:
                    if previous["payload_hash"] != fingerprint:
                        raise HTTPException(
                            409, "Request ID already used for different event content"
                        )
                    row = self.cloud.db.execute(
                        "SELECT * FROM personal_events WHERE owner=? AND event_id=?",
                        (owner, previous["event_id"]),
                    ).fetchone()
                    if not row:
                        raise HTTPException(
                            404,
                            "Original event was deleted; request cannot recreate it",
                        )
                    return self.event_row(row)
            series_id = None
            revision = 1
            replacing = []
            if identifier:
                previous = self.event(owner, identifier)
                if previous["revision"] != body.revision:
                    raise HTTPException(409, "Event changed; reload before editing")
                meta = self.event_row(previous)
                series_id = meta["series_id"]
                revision = body.revision + 1
                if body.scope == "series":
                    if not series_id or body.series_revision != meta["series_revision"]:
                        raise HTTPException(
                            409, "Series changed; reload before editing the series"
                        )
                    replacing = [
                        r[0]
                        for r in self.cloud.db.execute(
                            "SELECT event_id FROM personal_series_events WHERE owner=? AND series_id=?",
                            (owner, series_id),
                        )
                    ]
                else:
                    replacing = [identifier]
                    if series_id and payload["recurrence"] != json.loads(
                        previous["payload"]
                    ).get("recurrence"):
                        raise HTTPException(
                            422, "Choose entire series to change its recurrence rule"
                        )
                    # Existing recurrence belongs to the series; a single edit is an exception.
                    if series_id:
                        payload["recurrence"] = json.loads(previous["payload"]).get(
                            "recurrence"
                        )
                if series_id:
                    self.cloud.db.execute(
                        "UPDATE personal_series SET revision=revision+1 WHERE owner=? AND series_id=?",
                        (owner, series_id),
                    )
            dates = (
                occurrences(
                    body.starts_at, body.ends_at, body.timezone, body.recurrence
                )
                if not identifier
                or body.scope == "series"
                or (not series_id and body.recurrence)
                else [(body.starts_at, body.ends_at)]
            )
            if (
                self.events(owner, limit=1)["total"] - len(replacing) + len(dates)
                > 2000
            ):
                raise HTTPException(
                    429, "Personal calendar limit reached; remove old events"
                )
            if not identifier:
                identifier = uuid.uuid4().hex
            if not series_id and body.recurrence:
                series_id = uuid.uuid4().hex
                self.cloud.db.execute(
                    "INSERT INTO personal_series VALUES(?,?,1)", (owner, series_id)
                )
            for target in replacing:
                self.cloud.db.execute(
                    "DELETE FROM personal_events WHERE owner=? AND event_id=?",
                    (owner, target),
                )
                self.cloud.db.execute(
                    "DELETE FROM personal_series_events WHERE owner=? AND event_id=?",
                    (owner, target),
                )
                self.cloud.db.execute(
                    "UPDATE personal_notices SET archived=1 WHERE owner=? AND event_id=?",
                    (owner, target),
                )
            for index, (start, end) in enumerate(dates):
                target = identifier if index == 0 else uuid.uuid4().hex
                start, end = (
                    start.astimezone(timezone.utc),
                    end.astimezone(timezone.utc),
                )
                item = dict(
                    payload, starts_at=start.isoformat(), ends_at=end.isoformat()
                )
                self.cloud.db.execute(
                    "INSERT INTO personal_events VALUES(?,?,?,?,?,?,?,?,?,NULL)",
                    (
                        owner,
                        target,
                        json.dumps(item),
                        start.isoformat(),
                        end.isoformat(),
                        (start - timedelta(minutes=body.remind_minutes)).isoformat(),
                        revision,
                        int(body.completed),
                        now(),
                    ),
                )
                if series_id:
                    self.cloud.db.execute(
                        "INSERT INTO personal_series_events VALUES(?,?,?)",
                        (owner, target, series_id),
                    )
            if not hasattr(body, "revision") and body.request_id:
                self.cloud.db.execute(
                    "INSERT INTO personal_event_commands VALUES(?,?,?,?)",
                    (owner, body.request_id, fingerprint, identifier),
                )
            self.audit(
                owner,
                "event_updated" if hasattr(body, "revision") else "event_created",
                identifier,
            )
            return self.event_row(self.event(owner, identifier))

    def delete_event(
        self, owner, identifier, revision, scope="occurrence", series_revision=None
    ):
        with self.transaction():
            row = self.event(owner, identifier)
            if row["revision"] != revision:
                raise HTTPException(409, "Event changed; reload before deleting")
            meta = self.event_row(row)
            targets = [identifier]
            if scope == "series":
                if not meta["series_id"] or series_revision != meta["series_revision"]:
                    raise HTTPException(
                        409, "Series changed; reload before deleting the series"
                    )
                targets = [
                    r[0]
                    for r in self.cloud.db.execute(
                        "SELECT event_id FROM personal_series_events WHERE owner=? AND series_id=?",
                        (owner, meta["series_id"]),
                    )
                ]
            if meta["series_id"]:
                self.cloud.db.execute(
                    "UPDATE personal_series SET revision=revision+1 WHERE owner=? AND series_id=?",
                    (owner, meta["series_id"]),
                )
            for target in targets:
                self.cloud.db.execute(
                    "DELETE FROM personal_events WHERE owner=? AND event_id=?",
                    (owner, target),
                )
                self.cloud.db.execute(
                    "DELETE FROM personal_notices WHERE owner=? AND event_id=?",
                    (owner, target),
                )
                self.cloud.db.execute(
                    "DELETE FROM personal_series_events WHERE owner=? AND event_id=?",
                    (owner, target),
                )
            self.audit(
                owner,
                "series_deleted" if scope == "series" else "event_deleted",
                identifier,
            )
        return {"deleted": True}

    def generate_reminders(self, at=None):
        """Durable all-owner generation; safe across independent SQLite workers."""
        at = at or now()
        if isinstance(at, datetime):
            at = at.astimezone(timezone.utc).isoformat()
        with self.transaction():
            cursor = self.cloud.db.execute(
                """INSERT OR IGNORE INTO personal_notices(owner,event_id,revision,message,created_at)
                SELECT e.owner,e.event_id,e.revision,json_extract(e.payload,'$.title'),?
                FROM personal_events e LEFT JOIN personal_preferences p ON p.owner=e.owner
                WHERE e.completed=0 AND e.deleted_at IS NULL AND e.reminder_at<=?
                AND coalesce(json_extract(p.payload,'$.reminders'),1)=1""",
                (at, at),
            )
            return cursor.rowcount

    def notifications(self, owner, offset=0, limit=100):
        with self.transaction():
            if self.preferences(owner)["reminders"]:
                self.cloud.db.execute(
                    """INSERT OR IGNORE INTO personal_notices(owner,event_id,revision,message,created_at)
                    SELECT owner,event_id,revision,json_extract(payload,'$.title'),? FROM personal_events
                    WHERE owner=? AND completed=0 AND deleted_at IS NULL AND reminder_at<=?""",
                    (now(), owner, now()),
                )
            total = self.cloud.db.execute(
                "SELECT count(*) FROM personal_notices WHERE owner=? AND archived=0",
                (owner,),
            ).fetchone()[0]
            unread = self.cloud.db.execute(
                "SELECT count(*) FROM personal_notices WHERE owner=? AND archived=0 AND read=0",
                (owner,),
            ).fetchone()[0]
            rows = self.cloud.db.execute(
                "SELECT notice_id,event_id,message,created_at,read FROM personal_notices WHERE owner=? AND archived=0 ORDER BY notice_id DESC LIMIT ? OFFSET ?",
                (owner, limit, offset),
            ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "unread": unread,
            "total": total,
            "offset": offset,
            "limit": limit,
            "delivery": "IN_APP_BACKGROUND"
            if getattr(self, "worker_running", False)
            else "IN_APP_ON_REFRESH",
        }

    def conversation(self, owner, identifier):
        row = self.cloud.db.execute(
            "SELECT * FROM personal_conversations WHERE owner=? AND conversation_id=?",
            (owner, identifier),
        ).fetchone()
        if not row:
            raise HTTPException(404, "Conversation not found")
        return row

    def conversations(self, owner):
        return [
            dict(row)
            for row in self.cloud.db.execute(
                "SELECT conversation_id,title,created_at FROM personal_conversations WHERE owner=? ORDER BY created_at DESC LIMIT 50",
                (owner,),
            )
        ]

    def create_conversation(self, owner, title):
        with self.transaction():
            if len(self.conversations(owner)) >= 50:
                raise HTTPException(
                    429, "Conversation limit reached; remove an old conversation"
                )
            identifier = uuid.uuid4().hex
            self.cloud.db.execute(
                "INSERT INTO personal_conversations VALUES(?,?,?,?)",
                (owner, identifier, title, now()),
            )
            self.audit(owner, "conversation_created", identifier)
        row = self.conversation(owner, identifier)
        return {name: row[name] for name in ("conversation_id", "title", "created_at")}

    def messages(self, owner, identifier, offset=0, limit=100):
        self.conversation(owner, identifier)
        total = self.cloud.db.execute(
            "SELECT count(*) FROM personal_messages WHERE owner=? AND conversation_id=?",
            (owner, identifier),
        ).fetchone()[0]
        rows = self.cloud.db.execute(
            "SELECT id,speaker,content,created_at FROM personal_messages WHERE owner=? AND conversation_id=? ORDER BY id LIMIT ? OFFSET ?",
            (owner, identifier, limit, offset),
        ).fetchall()
        return {
            "items": [dict(row) for row in rows],
            "total": total,
            "offset": offset,
            "limit": limit,
        }

    def message(self, owner, identifier, body, answer):
        with self.transaction():
            self.conversation(owner, identifier)
            previous = self.cloud.db.execute(
                "SELECT speaker,content,created_at FROM personal_messages WHERE owner=? AND conversation_id=? AND request_id=? ORDER BY id",
                (owner, identifier, body.request_id),
            ).fetchall()
            if previous:
                if previous[0]["content"] != body.message:
                    raise HTTPException(
                        409, "Request ID already used for different content"
                    )
                return {
                    "user": dict(previous[0]),
                    "assistant": dict(previous[1]) | {"source": "RULE_BASED"},
                }
            from datetime import timedelta

            recent = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
            count = self.cloud.db.execute(
                "SELECT count(*) FROM personal_messages WHERE owner=? AND speaker='user' AND created_at>=?",
                (owner, recent),
            ).fetchone()[0]
            if (
                count >= 20
                or self.messages(owner, identifier, limit=1)["total"] >= 1000
            ):
                raise HTTPException(
                    429,
                    "Assistant limit reached; retry later or start another conversation",
                )
            at = now()
            for speaker, content in (("user", body.message), ("assistant", answer)):
                self.cloud.db.execute(
                    "INSERT INTO personal_messages(owner,conversation_id,request_id,speaker,content,created_at) VALUES(?,?,?,?,?,?)",
                    (owner, identifier, body.request_id, speaker, content, at),
                )
            return {
                "user": {"speaker": "user", "content": body.message, "created_at": at},
                "assistant": {
                    "speaker": "assistant",
                    "content": answer,
                    "created_at": at,
                    "source": "RULE_BASED",
                },
            }


def install_workspace(api, cloud, operator_dependency, driver_dependency):
    store = WorkspaceStore(cloud)
    api.state.workspace = store

    def operator_owner(principal=Depends(operator_dependency)):
        return {
            "key": json.dumps(
                ["operator", principal.tenant_id, principal.actor_id, principal.role]
            ),
            "tenant": principal.tenant_id,
            "role": principal.role,
            "vehicle": None,
            "actor": principal.actor_id,
        }

    def driver_owner(identity=Depends(driver_dependency)):
        return {
            "key": json.dumps(
                [
                    "driver",
                    identity["tenant_id"],
                    identity["driver_id"],
                    identity["vehicle_id"],
                ]
            ),
            "tenant": identity["tenant_id"],
            "role": "driver",
            "vehicle": identity["vehicle_id"],
            "actor": identity["driver_id"],
        }

    def summary(owner):
        key = owner["key"]
        preferences = store.preferences(key)
        local_day = datetime.now(ZoneInfo(preferences["timezone"])).date().isoformat()
        due = cloud.db.execute(
            "SELECT count(*) FROM personal_events WHERE owner=? AND completed=0 AND deleted_at IS NULL AND starts_at<=?",
            (key, now()),
        ).fetchone()[0]
        vehicles = (
            [owner["vehicle"]]
            if owner["vehicle"]
            else cloud.vehicle_ids(owner["tenant"])
        )
        pending = cloud.db.execute(
            "SELECT count(*) FROM personal_events WHERE owner=? AND completed=0 AND deleted_at IS NULL",
            (key,),
        ).fetchone()[0]
        unread = store.notifications(key)["unread"]
        return {
            "date": local_day,
            "role": owner["role"],
            "display_name": preferences["display_name"],
            "authorized_vehicles": len(vehicles),
            "open_calendar_items": pending,
            "due_calendar_items": due,
            "unread_reminders": unread,
            "message": f"{len(vehicles)} authorized vehicle(s). {due} due calendar item(s), {pending} open calendar item(s), {unread} unread reminder(s). Review the calendar and your operational inbox before starting work.",
            "source": "VERIFIED_RECORD_COUNTS",
            "reminder_delivery": "IN_APP_BACKGROUND"
            if getattr(store, "worker_running", False)
            else "IN_APP_ON_REFRESH",
        }

    def assistant(owner, question):
        text = question.lower()
        if any(
            word in text
            for word in (
                "delete",
                "override",
                "ignore permissions",
                "control vehicle",
                "disable safety",
            )
        ):
            return "I am a read-only, rule-based assistant. I cannot change vehicles, permissions, consent or operational records. Use the authorized workflow and its confirmation controls."
        if any(
            word in text
            for word in ("password", "login", "recover", "credential", "register")
        ):
            return "Operator passwords and registration belong to your configured identity provider. For a lost driver credential, contact your fleet administrator for replacement and revocation. Open Account access for configured sign-in and recovery links. Never send credentials in chat."
        if any(word in text for word in ("gps", "consent", "privacy")):
            return "Phone GPS requires explicit trip consent and foreground tracking. Vehicle battery signals are separate. Use Trip control to manage consent and Your phone data to export or erase samples. This assistant does not start tracking."
        if any(
            word in text
            for word in (
                "calendar",
                "schedule",
                "today",
                "daily",
                "attention",
                "remind",
            )
        ):
            return (
                summary(owner)["message"]
                + " These are personal calendar reminders, separate from confirmed fleet daily plans. Reminders are stored in-app by the configured server worker or on workspace refresh. Open Notifications to read them; no external push or email delivery is configured."
            )
        return (
            f"You are using the {owner['role']} workspace. I provide rule-based guidance, not generated AI predictions. Use Calendar for personal tasks, Notifications for due reminders, and the existing trip, planning, advisory and service workflows for operational records. "
            + summary(owner)["message"]
        )

    def routes(dependency):
        router = APIRouter()

        @router.get("/summary")
        def daily(owner=Depends(dependency)):
            return summary(owner)

        @router.get("/preferences")
        def preferences(owner=Depends(dependency)):
            return store.preferences(owner["key"])

        @router.put("/preferences")
        def save_preferences(body: Preferences, owner=Depends(dependency)):
            return store.save_preferences(owner["key"], body)

        @router.get("/events")
        def events(
            after: datetime | None = None,
            before: datetime | None = None,
            q: str = Query(default="", max_length=120),
            offset: int = Query(default=0, ge=0, le=100000),
            limit: int = Query(default=100, ge=1, le=200),
            owner=Depends(dependency),
        ):
            if any(
                value is not None and value.tzinfo is None for value in (after, before)
            ):
                raise HTTPException(422, "Date filters require UTC offsets")
            if after and before and before <= after:
                raise HTTPException(422, "End must follow start")
            return store.events(
                owner["key"],
                after.astimezone(timezone.utc).isoformat() if after else None,
                before.astimezone(timezone.utc).isoformat() if before else None,
                q,
                offset,
                limit,
            )

        @router.post("/events")
        def create_event(body: CalendarEvent, owner=Depends(dependency)):
            return store.save_event(owner["key"], body)

        @router.put("/events/{identifier}")
        def update_event(
            identifier: str, body: CalendarUpdate, owner=Depends(dependency)
        ):
            return store.save_event(owner["key"], body, identifier)

        @router.delete("/events/{identifier}")
        def delete_event(
            identifier: str,
            revision: int = Query(ge=1),
            scope: Literal["occurrence", "series"] = "occurrence",
            series_revision: int | None = Query(default=None, ge=1),
            owner=Depends(dependency),
        ):
            return store.delete_event(
                owner["key"], identifier, revision, scope, series_revision
            )

        @router.get("/notifications")
        def notifications(
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=100, ge=1, le=200),
            owner=Depends(dependency),
        ):
            return store.notifications(owner["key"], offset, limit)

        @router.put("/notifications/{identifier}")
        def notification(
            identifier: int, body: NoticeUpdate, owner=Depends(dependency)
        ):
            with store.transaction():
                cursor = cloud.db.execute(
                    "UPDATE personal_notices SET read=?,archived=? WHERE owner=? AND notice_id=?",
                    (body.read, body.archived, owner["key"], identifier),
                )
                if not cursor.rowcount:
                    raise HTTPException(404, "Notification not found")
            return {"updated": True}

        @router.post("/notifications/read-all")
        def read_all(owner=Depends(dependency)):
            with store.transaction():
                cloud.db.execute(
                    "UPDATE personal_notices SET read=1 WHERE owner=?", (owner["key"],)
                )
            return {"updated": True}

        @router.get("/conversations")
        def conversations(owner=Depends(dependency)):
            return store.conversations(owner["key"])

        @router.post("/conversations")
        def create_conversation(body: Conversation, owner=Depends(dependency)):
            return store.create_conversation(owner["key"], body.title)

        @router.get("/conversations/{identifier}/messages")
        def messages(
            identifier: str,
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=100, ge=1, le=200),
            owner=Depends(dependency),
        ):
            return store.messages(owner["key"], identifier, offset, limit)

        @router.post("/conversations/{identifier}/messages")
        def send_message(identifier: str, body: Message, owner=Depends(dependency)):
            store.conversation(owner["key"], identifier)
            return store.message(
                owner["key"], identifier, body, assistant(owner, body.message)
            )

        @router.delete("/conversations/{identifier}")
        def delete_conversation(identifier: str, owner=Depends(dependency)):
            with store.transaction():
                store.conversation(owner["key"], identifier)
                cloud.db.execute(
                    "DELETE FROM personal_messages WHERE owner=? AND conversation_id=?",
                    (owner["key"], identifier),
                )
                cloud.db.execute(
                    "DELETE FROM personal_conversations WHERE owner=? AND conversation_id=?",
                    (owner["key"], identifier),
                )
                store.audit(owner["key"], "conversation_deleted", identifier)
            return {"deleted": True}

        @router.get("/export")
        def export(owner=Depends(dependency)):
            key = owner["key"]
            events = store.events(key, limit=200)["items"]
            # Complete bounded export; never silently truncate a personal calendar.
            for offset in range(200, store.events(key, limit=1)["total"], 200):
                events.extend(store.events(key, offset=offset, limit=200)["items"])
            chats = []
            for conversation in store.conversations(key):
                identifier = conversation["conversation_id"]
                messages = []
                for offset in range(
                    0, store.messages(key, identifier, limit=1)["total"], 200
                ):
                    messages.extend(
                        store.messages(key, identifier, offset, 200)["items"]
                    )
                chats.append(conversation | {"messages": messages})
            return {
                "exported_at": now(),
                "preferences": store.preferences(key),
                "events": events,
                "conversations": chats,
            }

        return router

    api.include_router(
        routes(operator_owner),
        prefix="/api/v1/operator/workspace",
        tags=["Personal workspace"],
    )
    api.include_router(
        routes(driver_owner),
        prefix="/api/v1/driver/workspace",
        tags=["Personal workspace"],
    )

    return {"operator": operator_owner, "driver": driver_owner, "store": store}
