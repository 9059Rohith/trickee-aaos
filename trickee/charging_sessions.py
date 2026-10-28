"""Durable read-only reconciliation of CPO session and charge detail records.

OCPI data is provider evidence. A matched charge detail record is not a
payment confirmation or a claim that the driver received that energy.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from .store import CloudStore


class ChargingConflict(ValueError):
    """A replay changes an immutable record or regresses a session."""


_STATUS = {"PENDING", "RESERVATION", "ACTIVE", "COMPLETED", "INVALID"}
_NEXT = {
    "PENDING": {"PENDING", "RESERVATION", "ACTIVE", "COMPLETED", "INVALID"},
    "RESERVATION": {"RESERVATION", "ACTIVE", "COMPLETED", "INVALID"},
    "ACTIVE": {"ACTIVE", "COMPLETED", "INVALID"},
    "COMPLETED": {"COMPLETED"},
    "INVALID": {"INVALID"},
}


def _text(value: Any, limit: int) -> str:
    if (not isinstance(value, str) or not 1 <= len(value) <= limit or not value.isascii()
            or any(ord(char) < 33 or ord(char) > 126 for char in value)):
        raise ValueError("invalid OCPI identity")
    return value.upper()


def _time(value: Any) -> datetime:
    if not isinstance(value, str):
        raise ValueError("OCPI timestamp required")
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("invalid OCPI timestamp") from exc
    if instant.tzinfo is None:
        instant = instant.replace(tzinfo=timezone.utc)
    if instant.utcoffset() != timedelta(0):
        raise ValueError("OCPI timestamp must be UTC")
    return instant.astimezone(timezone.utc)


def _amount(value: Any, limit: int = 1_000_000, signed: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("invalid OCPI quantity")
    try:
        number = Decimal(str(value))
    except InvalidOperation as exc:
        raise ValueError("invalid OCPI quantity") from exc
    if not number.is_finite() or not (-limit if signed else 0) <= number <= limit:
        raise ValueError("invalid OCPI quantity")
    return number


def _cost(value: Any, required: bool, credit: bool = False) -> str | None:
    if value is None and not required:
        return None
    if not isinstance(value, dict) or "before_taxes" not in value:
        raise ValueError("OCPI 2.3.0 total cost requires before_taxes")
    taxes = value.get("taxes", [])
    if not isinstance(taxes, list) or len(taxes) > 20:
        raise ValueError("invalid OCPI taxes")
    before_taxes = _amount(value["before_taxes"], signed=credit)
    if credit and before_taxes > 0:
        raise ValueError("credit CDR cost must be non-positive")
    total = before_taxes
    for tax in taxes:
        if (not isinstance(tax, dict) or not isinstance(tax.get("name"), str)
                or not 1 <= len(tax["name"]) <= 80):
            raise ValueError("invalid OCPI tax")
        amount = _amount(tax["amount"], signed=credit)
        if credit and amount > 0:
            raise ValueError("credit CDR tax must be non-positive")
        total += amount
    return str(total)


def _identity(raw: dict, id_limit: int = 36) -> tuple[str, str, str]:
    country = _text(raw["country_code"], 2)
    party = _text(raw["party_id"], 3)
    identifier = _text(raw["id"], id_limit)
    if len(country) != 2 or len(party) != 3:
        raise ValueError("invalid OCPI party")
    return country, party, identifier


def _currency(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 3 or not value.isalpha() or not value.isupper():
        raise ValueError("invalid OCPI currency")
    return value


def _session(raw: dict) -> dict:
    country, party, identifier = _identity(raw)
    status = raw["status"]
    if not isinstance(status, str) or status not in _STATUS:
        raise ValueError("invalid OCPI session status")
    start = _time(raw["start_date_time"])
    updated = _time(raw["last_updated"])
    if updated < start:
        raise ValueError("session update precedes start")
    return {
        "country_code": country, "party_id": party, "session_id": identifier,
        "status": status, "kwh": str(_amount(raw["kwh"], 10000)),
        "currency": _currency(raw["currency"]),
        "total_cost": _cost(raw.get("total_cost"), False),
        "start_date_time": start.isoformat(), "last_updated": updated.isoformat(),
        "source": "PROVIDER",
    }


def _cdr(raw: dict) -> dict:
    country, party, identifier = _identity(raw, 39)
    start = _time(raw["start_date_time"])
    end = _time(raw["end_date_time"])
    if end < start:
        raise ValueError("CDR end precedes start")
    session_id = raw.get("session_id")
    if session_id is not None:
        session_id = _text(session_id, 36)
    credit = raw.get("credit", False)
    if not isinstance(credit, bool):
        raise ValueError("invalid OCPI credit flag")
    credit_reference = raw.get("credit_reference_id")
    if credit:
        credit_reference = _text(credit_reference, 39)
        if credit_reference == identifier:
            raise ValueError("credit CDR cannot reference itself")
    elif credit_reference is not None:
        raise ValueError("non-credit CDR cannot reference another CDR")
    return {
        "country_code": country, "party_id": party, "cdr_id": identifier,
        "session_id": session_id, "total_energy": str(_amount(raw["total_energy"], 10000)),
        "total_cost": _cost(raw["total_cost"], True, credit), "currency": _currency(raw["currency"]),
        "credit": credit, "credit_reference_id": credit_reference,
        "start_date_time": start.isoformat(), "end_date_time": end.isoformat(),
        "source": "PROVIDER",
    }


def _fingerprint(record: dict) -> str:
    return json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


class ChargingSessionStore:
    """SQLite-backed OCPI mirror with idempotent updates and review flags."""

    def __init__(self, cloud: CloudStore):
        self.cloud = cloud
        with cloud._write_lock, cloud.db:
            cloud.db.executescript("""
                CREATE TABLE IF NOT EXISTS ocpi_sessions (
                    country_code TEXT NOT NULL, party_id TEXT NOT NULL, session_id TEXT NOT NULL,
                    last_updated TEXT NOT NULL, status TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(country_code,party_id,session_id)
                );
                CREATE TABLE IF NOT EXISTS ocpi_cdrs (
                    country_code TEXT NOT NULL, party_id TEXT NOT NULL, cdr_id TEXT NOT NULL,
                    session_id TEXT, fingerprint TEXT NOT NULL, payload_json TEXT NOT NULL,
                    PRIMARY KEY(country_code,party_id,cdr_id)
                );
                CREATE INDEX IF NOT EXISTS ocpi_cdrs_by_session
                    ON ocpi_cdrs(country_code,party_id,session_id);
                CREATE TABLE IF NOT EXISTS ocpi_record_scopes (
                    kind TEXT NOT NULL CHECK(kind IN ('SESSION','CDR')),
                    country_code TEXT NOT NULL,
                    party_id TEXT NOT NULL,
                    record_id TEXT NOT NULL,
                    tenant_id TEXT NOT NULL,
                    PRIMARY KEY(kind,country_code,party_id,record_id)
                );
                CREATE INDEX IF NOT EXISTS ocpi_scopes_by_tenant
                    ON ocpi_record_scopes(tenant_id,kind,country_code,party_id,record_id);
            """)

    @contextmanager
    def _write(self):
        """Join a provider batch transaction or commit a standalone update."""
        with self.cloud._write_lock:
            connection = self.cloud.db
            if connection.in_transaction:
                yield
            else:
                with connection:
                    yield

    def assert_scope_available(self, tenant_id: str, sessions: list[dict], cdrs: list[dict]) -> None:
        """Fail before ingest if any provider identity belongs to another tenant."""
        if not isinstance(tenant_id, str) or not 1 <= len(tenant_id) <= 64 or not all(
                char.isascii() and (char.isalnum() or char in "_-") for char in tenant_id):
            raise ValueError("invalid OCPI tenant scope")
        records = [("SESSION", item["country_code"], item["party_id"], item["session_id"])
                   for item in sessions]
        records.extend(("CDR", item["country_code"], item["party_id"], item["cdr_id"])
                       for item in cdrs)
        for identity in records:
            existing = self.cloud.db.execute(
                "SELECT tenant_id FROM ocpi_record_scopes WHERE kind=? AND country_code=? "
                "AND party_id=? AND record_id=?", identity,
            ).fetchone()
            if existing and existing["tenant_id"] != tenant_id:
                raise ChargingConflict("OCPI record is already scoped to another tenant")
        for item in cdrs:
            if item["session_id"] is None:
                continue
            owner = self.cloud.db.execute(
                "SELECT tenant_id FROM ocpi_record_scopes WHERE kind='SESSION' AND country_code=? "
                "AND party_id=? AND record_id=?",
                (item["country_code"], item["party_id"], item["session_id"]),
            ).fetchone()
            if owner and owner["tenant_id"] != tenant_id:
                raise ChargingConflict("OCPI session reference belongs to another tenant")
        for item in sessions:
            other = self.cloud.db.execute(
                "SELECT 1 FROM ocpi_cdrs c JOIN ocpi_record_scopes scope ON "
                "scope.kind='CDR' AND scope.country_code=c.country_code AND "
                "scope.party_id=c.party_id AND scope.record_id=c.cdr_id "
                "WHERE c.country_code=? AND c.party_id=? AND c.session_id=? "
                "AND scope.tenant_id<>? LIMIT 1",
                (item["country_code"], item["party_id"], item["session_id"], tenant_id),
            ).fetchone()
            if other:
                raise ChargingConflict("OCPI CDR reference belongs to another tenant")

    def claim_scope(self, tenant_id: str, kind: str, country: str,
                    party: str, record_id: str) -> None:
        with self._write():
            existing = self.cloud.db.execute(
                "SELECT tenant_id FROM ocpi_record_scopes WHERE kind=? AND country_code=? "
                "AND party_id=? AND record_id=?", (kind, country, party, record_id),
            ).fetchone()
            if existing and existing["tenant_id"] != tenant_id:
                raise ChargingConflict("OCPI record is already scoped to another tenant")
            self.cloud.db.execute(
                "INSERT OR IGNORE INTO ocpi_record_scopes(kind,country_code,party_id,record_id,tenant_id) "
                "VALUES(?,?,?,?,?)", (kind, country, party, record_id, tenant_id),
            )

    def list_for_tenant(self, tenant_id: str, limit: int = 100,
                        now: datetime | None = None) -> dict:
        """Expose only explicitly scoped provider records, never legacy unscoped rows."""
        if not 1 <= limit <= 100:
            raise ValueError("invalid OCPI page size")
        current = now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            raise ValueError("OCPI clock must be timezone-aware")
        restriction = "" if tenant_id == "*" else "AND scope.tenant_id=?"
        params: tuple = () if tenant_id == "*" else (tenant_id,)
        sessions = self.cloud.db.execute(
            "SELECT item.payload_json,scope.tenant_id FROM ocpi_record_scopes scope "
            "JOIN ocpi_sessions item ON item.country_code=scope.country_code "
            "AND item.party_id=scope.party_id AND item.session_id=scope.record_id "
            "WHERE scope.kind='SESSION' " + restriction +
            " ORDER BY item.last_updated DESC,scope.record_id DESC LIMIT ?",
            params + (limit,),
        ).fetchall()
        cdrs = self.cloud.db.execute(
            "SELECT item.payload_json,scope.tenant_id FROM ocpi_record_scopes scope "
            "JOIN ocpi_cdrs item ON item.country_code=scope.country_code "
            "AND item.party_id=scope.party_id AND item.cdr_id=scope.record_id "
            "WHERE scope.kind='CDR' " + restriction +
            " ORDER BY json_extract(item.payload_json,'$.end_date_time') DESC,scope.record_id DESC LIMIT ?",
            params + (limit,),
        ).fetchall()
        visible_sessions = []
        for row in sessions:
            data = json.loads(row["payload_json"])
            updated = datetime.fromisoformat(data["last_updated"])
            freshness = ("FUTURE_TIMESTAMP" if updated > current + timedelta(minutes=5)
                         else "STALE" if data["status"] in ("PENDING", "RESERVATION", "ACTIVE")
                         and current - updated > timedelta(minutes=15)
                         else "CURRENT" if data["status"] in ("PENDING", "RESERVATION", "ACTIVE")
                         else "HISTORICAL")
            visible_sessions.append({**data, "tenant_id": row["tenant_id"], "freshness": freshness,
                                     "reconciliation": self._reconciliation(data["country_code"],
                                                                              data["party_id"], data["session_id"],
                                                                              row["tenant_id"])})
        visible_cdrs = []
        for row in cdrs:
            data = json.loads(row["payload_json"])
            visible_cdrs.append({**data, "tenant_id": row["tenant_id"],
                                 "reconciliation": self._reconciliation(data["country_code"],
                                                                          data["party_id"], data["session_id"],
                                                                          row["tenant_id"])})
        return {"source": "OCPI_PROVIDER", "sessions": visible_sessions, "cdrs": visible_cdrs,
                "limited_to": limit, "vehicle_assignment": "UNAVAILABLE"}

    def _reconciliation(self, country: str, party: str, session_id: str | None,
                        tenant_id: str | None = None) -> str:
        if session_id is None:
            return "UNLINKED"
        if tenant_id is None:
            session = self.cloud.db.execute(
                "SELECT payload_json FROM ocpi_sessions WHERE country_code=? AND party_id=? AND session_id=?",
                (country, party, session_id),
            ).fetchone()
        else:
            session = self.cloud.db.execute(
                "SELECT item.payload_json FROM ocpi_sessions item JOIN ocpi_record_scopes scope ON "
                "scope.kind='SESSION' AND scope.country_code=item.country_code AND "
                "scope.party_id=item.party_id AND scope.record_id=item.session_id "
                "WHERE item.country_code=? AND item.party_id=? AND item.session_id=? AND scope.tenant_id=?",
                (country, party, session_id, tenant_id),
            ).fetchone()
        if session is None:
            return "SESSION_MISSING" if tenant_id is None else "SESSION_UNAVAILABLE"
        data = json.loads(session["payload_json"])
        if data["status"] != "COMPLETED":
            return "SESSION_NOT_COMPLETED"
        if tenant_id is None:
            cdrs = self.cloud.db.execute(
                "SELECT payload_json FROM ocpi_cdrs WHERE country_code=? AND party_id=? AND session_id=?",
                (country, party, session_id),
            ).fetchall()
        else:
            cdrs = self.cloud.db.execute(
                "SELECT item.payload_json FROM ocpi_cdrs item JOIN ocpi_record_scopes scope ON "
                "scope.kind='CDR' AND scope.country_code=item.country_code AND "
                "scope.party_id=item.party_id AND scope.record_id=item.cdr_id "
                "WHERE item.country_code=? AND item.party_id=? AND item.session_id=? AND scope.tenant_id=?",
                (country, party, session_id, tenant_id),
            ).fetchall()
        if not cdrs:
            return "CDR_MISSING"
        parsed_cdrs = [json.loads(row["payload_json"]) for row in cdrs]
        credits = [cdr for cdr in parsed_cdrs if cdr["credit"]]
        if credits:
            identifiers = {cdr["cdr_id"] for cdr in parsed_cdrs}
            if any(cdr["credit_reference_id"] not in identifiers for cdr in credits):
                return "CREDIT_REFERENCE_MISSING"
            return "CREDIT_ADJUSTMENT_REVIEW"
        if len(cdrs) > 1:
            return "MULTIPLE_CDRS_REVIEW"
        cdr = parsed_cdrs[0]
        if data["currency"] != cdr["currency"]:
            return "CURRENCY_MISMATCH"
        if abs(Decimal(data["kwh"]) - Decimal(cdr["total_energy"])) > Decimal("0.05"):
            return "ENERGY_MISMATCH"
        if data["total_cost"] is None:
            return "SESSION_COST_UNKNOWN"
        if abs(Decimal(data["total_cost"]) - Decimal(cdr["total_cost"])) > Decimal("0.01"):
            return "COST_MISMATCH"
        return "MATCHED"

    def get(self, country: str, party: str, session_id: str) -> dict | None:
        row = self.cloud.db.execute(
            "SELECT payload_json FROM ocpi_sessions WHERE country_code=? AND party_id=? AND session_id=?",
            (country, party, session_id),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload_json"])
        return {**data, "reconciliation": self._reconciliation(country, party, session_id)}

    def get_cdr(self, country: str, party: str, cdr_id: str) -> dict | None:
        row = self.cloud.db.execute(
            "SELECT payload_json FROM ocpi_cdrs WHERE country_code=? AND party_id=? AND cdr_id=?",
            (country, party, cdr_id),
        ).fetchone()
        if row is None:
            return None
        data = json.loads(row["payload_json"])
        return {**data, "reconciliation": self._reconciliation(country, party, data["session_id"])}

    def apply_session(self, raw: dict) -> tuple[dict, bool]:
        if not isinstance(raw, dict):
            raise ValueError("OCPI session must be an object")
        data = _session(raw)
        country, party, session_id = data["country_code"], data["party_id"], data["session_id"]
        fingerprint = _fingerprint(data)
        with self._write():
            prior = self.cloud.db.execute(
                "SELECT last_updated,status,fingerprint FROM ocpi_sessions "
                "WHERE country_code=? AND party_id=? AND session_id=?",
                (country, party, session_id),
            ).fetchone()
            if prior:
                if prior["fingerprint"] == fingerprint:
                    return self.get(country, party, session_id), False
                if (data["last_updated"] <= prior["last_updated"]
                        or data["status"] not in _NEXT[prior["status"]]
                        or prior["status"] == "INVALID"):
                    raise ChargingConflict("OCPI session update conflicts with stored state")
            self.cloud.db.execute(
                "INSERT INTO ocpi_sessions(country_code,party_id,session_id,last_updated,status,fingerprint,payload_json) "
                "VALUES(?,?,?,?,?,?,?) ON CONFLICT(country_code,party_id,session_id) DO UPDATE SET "
                "last_updated=excluded.last_updated,status=excluded.status,"
                "fingerprint=excluded.fingerprint,payload_json=excluded.payload_json",
                (country, party, session_id, data["last_updated"], data["status"],
                 fingerprint, _fingerprint(data)),
            )
            return self.get(country, party, session_id), True

    def apply_cdr(self, raw: dict) -> tuple[dict, bool]:
        if not isinstance(raw, dict):
            raise ValueError("OCPI CDR must be an object")
        data = _cdr(raw)
        country, party, cdr_id = data["country_code"], data["party_id"], data["cdr_id"]
        fingerprint = _fingerprint(data)
        with self._write():
            prior = self.cloud.db.execute(
                "SELECT fingerprint FROM ocpi_cdrs WHERE country_code=? AND party_id=? AND cdr_id=?",
                (country, party, cdr_id),
            ).fetchone()
            if prior:
                if prior["fingerprint"] == fingerprint:
                    return self.get_cdr(country, party, cdr_id), False
                raise ChargingConflict("OCPI CDR conflicts with immutable stored record")
            self.cloud.db.execute(
                "INSERT INTO ocpi_cdrs(country_code,party_id,cdr_id,session_id,fingerprint,payload_json) "
                "VALUES(?,?,?,?,?,?)",
                (country, party, cdr_id, data["session_id"], fingerprint, fingerprint),
            )
            return self.get_cdr(country, party, cdr_id), True


def sync_ocpi_records(store: ChargingSessionStore, sessions_client: Any,
                      cdrs_client: Any, tenant_id: str | None = None) -> dict:
    """Fetch bounded partner collections, then idempotently mirror records."""
    sessions = sessions_client.records()
    cdrs = cdrs_client.records()
    # Reject an invalid collection before changing durable state. A later
    # provider page can be malformed even when earlier pages are valid.
    normalized_sessions = []
    for item in sessions:
        if not isinstance(item, dict):
            raise ValueError("OCPI session must be an object")
        normalized_sessions.append(_session(item))
    normalized_cdrs = []
    for item in cdrs:
        if not isinstance(item, dict):
            raise ValueError("OCPI CDR must be an object")
        normalized_cdrs.append(_cdr(item))
    # One provider pull is one durable update. In particular, a late immutable
    # CDR conflict must not leave earlier sessions or ownership claims behind.
    with store.cloud._write_lock, store.cloud.db:
        store.cloud.db.execute("BEGIN IMMEDIATE")
        if tenant_id is not None:
            store.assert_scope_available(tenant_id, normalized_sessions, normalized_cdrs)
        session_changes = 0
        cdr_changes = 0
        for item in sessions:
            record, changed = store.apply_session(item)
            session_changes += int(changed)
            if tenant_id is not None:
                store.claim_scope(tenant_id, "SESSION", record["country_code"],
                                  record["party_id"], record["session_id"])
        for item in cdrs:
            record, changed = store.apply_cdr(item)
            cdr_changes += int(changed)
            if tenant_id is not None:
                store.claim_scope(tenant_id, "CDR", record["country_code"],
                                  record["party_id"], record["cdr_id"])
        counts: dict[str, int] = {}
        for item in cdrs:
            record = store.get_cdr(item["country_code"].upper(), item["party_id"].upper(),
                                   item["id"].upper())
            counts[record["reconciliation"]] = counts.get(record["reconciliation"], 0) + 1
    return {"sessions_seen": len(sessions), "sessions_changed": session_changes,
            "cdrs_seen": len(cdrs), "cdrs_changed": cdr_changes,
            "reconciliation": counts}
