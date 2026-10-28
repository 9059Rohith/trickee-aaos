"""Immutable local model artifacts and auditable registry-stage transitions.

Registry PRODUCTION means selected for a release pipeline; it does not imply
installation on a vehicle or completion of OEM, safety, or field approvals.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


def _digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            hasher.update(chunk)
    return hasher.hexdigest()


class ModelMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    version: str = Field(pattern=r"^[A-Za-z0-9_.-]{1,64}$")
    task: Literal["RANGE", "ETA", "CHARGER_QUEUE", "CHARGER_AVAILABILITY", "HEALTH"]
    training_dataset_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    training_at: datetime
    features: list[str] = Field(min_length=1)
    known_limitations: list[str] = Field(min_length=1)
    evaluation: dict

    @model_validator(mode="after")
    def valid_evaluation(self) -> "ModelMetadata":
        if self.training_at.tzinfo is None:
            raise ValueError("training time requires timezone")
        if any(not feature.strip() for feature in self.features):
            raise ValueError("feature names must not be blank")
        if any(not limitation.strip() for limitation in self.known_limitations):
            raise ValueError("model limitations must not be blank")
        if not isinstance(self.evaluation.get("dataset_sha256"), str):
            raise ValueError("evaluation requires a dataset fingerprint")
        task_report = self.evaluation.get("tasks", {}).get(self.task)
        if not isinstance(task_report, dict):
            raise ValueError("evaluation is missing the model task")
        return self


class ModelRegistry:
    """SQLite metadata plus content-addressed artifacts, with no network upload."""

    def __init__(self, root: str | Path):
        self.root = Path(root)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / "models.db", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS models (
                model_id TEXT NOT NULL, version TEXT NOT NULL, task TEXT NOT NULL,
                artifact_sha256 TEXT NOT NULL, metadata_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN
                    ('DEVELOPMENT','STAGING','PRODUCTION','RETIRED','ROLLED_BACK')),
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(model_id,version)
            );
            CREATE UNIQUE INDEX IF NOT EXISTS one_production_model
                ON models(model_id) WHERE status='PRODUCTION';
            CREATE TABLE IF NOT EXISTS model_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL,
                model_id TEXT NOT NULL, version TEXT NOT NULL,
                action TEXT NOT NULL, before_status TEXT,
                after_status TEXT NOT NULL, actor TEXT NOT NULL,
                reason TEXT NOT NULL
            );
        """)

    def close(self) -> None:
        self.db.close()

    def __enter__(self) -> "ModelRegistry":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    def register(self, artifact: str | Path, metadata: ModelMetadata,
                 actor: str) -> dict:
        if not actor.strip():
            raise ValueError("actor is required")
        source = Path(artifact)
        if not source.is_file() or source.stat().st_size == 0:
            raise ValueError("model artifact must be a nonempty file")
        checksum = _digest(source)
        target = self.artifacts / checksum
        with self._lock:
            existing = self.db.execute(
                "SELECT 1 FROM models WHERE model_id=? AND version=?",
                (metadata.model_id, metadata.version),
            ).fetchone()
            if existing:
                raise ValueError("model version is immutable")
            if target.exists() and _digest(target) != checksum:
                raise ValueError("stored model artifact is corrupt")
            if not target.exists():
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=self.artifacts, prefix=checksum + ".",
                    suffix=".tmp", delete=False,
                ) as output_stream:
                    temporary = Path(output_stream.name)
                try:
                    with source.open("rb") as input_stream, temporary.open("wb") as output_stream:
                        shutil.copyfileobj(input_stream, output_stream, 1024 * 1024)
                    if _digest(temporary) != checksum:
                        raise ValueError("model artifact changed while copying")
                    if target.exists():
                        if _digest(target) != checksum:
                            raise ValueError("stored model artifact is corrupt")
                    else:
                        os.replace(temporary, target)
                finally:
                    temporary.unlink(missing_ok=True)
            now = datetime.now(timezone.utc).isoformat()
            with self.db:
                self.db.execute(
                    "INSERT INTO models VALUES(?,?,?,?,?,?,?,?)",
                    (metadata.model_id, metadata.version, metadata.task, checksum,
                     metadata.model_dump_json(), "DEVELOPMENT", now, now),
                )
                self._audit(metadata.model_id, metadata.version, "REGISTER", None,
                            "DEVELOPMENT", actor, "artifact registered")
        return self.get(metadata.model_id, metadata.version)

    def _audit(self, model_id: str, version: str, action: str,
               before: str | None, after: str, actor: str, reason: str) -> None:
        self.db.execute(
            "INSERT INTO model_audit(at,model_id,version,action,before_status,"
            "after_status,actor,reason) VALUES(?,?,?,?,?,?,?,?)",
            (datetime.now(timezone.utc).isoformat(), model_id, version,
             action, before, after, actor, reason),
        )

    def get(self, model_id: str, version: str) -> dict:
        row = self.db.execute(
            "SELECT * FROM models WHERE model_id=? AND version=?",
            (model_id, version),
        ).fetchone()
        if row is None:
            raise KeyError((model_id, version))
        return {"model_id": row["model_id"], "version": row["version"],
                "task": row["task"], "status": row["status"],
                "artifact_sha256": row["artifact_sha256"],
                "metadata": json.loads(row["metadata_json"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def verify_artifact(self, model_id: str, version: str) -> Path:
        row = self.get(model_id, version)
        path = self.artifacts / row["artifact_sha256"]
        if not path.is_file() or _digest(path) != row["artifact_sha256"]:
            raise ValueError("model artifact missing or hash mismatch")
        return path

    def _transition(self, model_id: str, version: str, expected: str,
                    after: str, actor: str, reason: str, action: str) -> None:
        if not actor.strip() or not reason.strip():
            raise ValueError("actor and reason are required")
        self.verify_artifact(model_id, version)
        current = self.get(model_id, version)
        if current["status"] != expected:
            raise ValueError(f"model must be {expected} before {action}")
        self.db.execute(
            "UPDATE models SET status=?,updated_at=? WHERE model_id=? AND version=?",
            (after, datetime.now(timezone.utc).isoformat(), model_id, version),
        )
        self._audit(model_id, version, action, expected, after, actor, reason)

    def stage(self, model_id: str, version: str, actor: str, reason: str) -> dict:
        with self._lock, self.db:
            row = self.get(model_id, version)
            report = row["metadata"]["evaluation"]["tasks"][row["task"]]
            if report.get("status") != "MEASURED":
                raise ValueError("staging requires a measured held-out evaluation")
            self._transition(model_id, version, "DEVELOPMENT", "STAGING",
                             actor, reason, "STAGE")
        return self.get(model_id, version)

    def activate(self, model_id: str, version: str, actor: str,
                 approval_reference: str, minimum_field_samples: int = 100) -> dict:
        if not approval_reference.strip() or minimum_field_samples < 1:
            raise ValueError("approval reference and valid sample threshold are required")
        with self._lock, self.db:
            row = self.get(model_id, version)
            report = row["metadata"]["evaluation"]["tasks"][row["task"]]
            if (report.get("status") != "MEASURED" or
                    report.get("field_verified") is not True or
                    report.get("sample_count", 0) < minimum_field_samples):
                raise ValueError("production selection requires sufficient field evaluation")
            if row["status"] != "STAGING":
                raise ValueError("model must be STAGING before activation")
            prior = self.db.execute(
                "SELECT version FROM models WHERE model_id=? AND status='PRODUCTION'",
                (model_id,),
            ).fetchone()
            if prior:
                self._transition(model_id, prior["version"], "PRODUCTION",
                                 "RETIRED", actor, approval_reference, "SUPERSEDE")
            self._transition(model_id, version, "STAGING", "PRODUCTION",
                             actor, approval_reference, "ACTIVATE")
        return self.get(model_id, version)

    def rollback(self, model_id: str, version: str, actor: str,
                 reason: str) -> dict:
        with self._lock, self.db:
            target = self.get(model_id, version)
            if target["status"] != "RETIRED":
                raise ValueError("rollback target must be a retired version")
            current = self.db.execute(
                "SELECT version FROM models WHERE model_id=? AND status='PRODUCTION'",
                (model_id,),
            ).fetchone()
            if current is None:
                raise ValueError("no active version to roll back")
            self._transition(model_id, current["version"], "PRODUCTION",
                             "ROLLED_BACK", actor, reason, "ROLLBACK_FROM")
            self._transition(model_id, version, "RETIRED", "PRODUCTION",
                             actor, reason, "ROLLBACK_TO")
        return self.get(model_id, version)

    def history(self, model_id: str) -> list[dict]:
        rows = self.db.execute(
            "SELECT * FROM model_audit WHERE model_id=? ORDER BY id", (model_id,),
        ).fetchall()
        return [dict(row) for row in rows]


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Trickee model artifact registry")
    parser.add_argument("--root", required=True, help="Registry data directory")
    actions = parser.add_subparsers(dest="action", required=True)
    register = actions.add_parser("register")
    register.add_argument("--artifact", required=True)
    register.add_argument("--metadata", required=True, help="ModelMetadata JSON")
    register.add_argument("--actor", required=True)
    for action in ("stage", "activate", "rollback", "status", "verify", "history"):
        sub = actions.add_parser(action)
        sub.add_argument("--model-id", required=True)
        if action != "history":
            sub.add_argument("--version", required=True)
        if action in ("stage", "activate", "rollback"):
            sub.add_argument("--actor", required=True)
        if action in ("stage", "rollback"):
            sub.add_argument("--reason", required=True)
        if action == "activate":
            sub.add_argument("--approval-reference", required=True)
            sub.add_argument("--minimum-field-samples", type=int, default=100)
    args = parser.parse_args()
    with ModelRegistry(args.root) as registry:
        if args.action == "register":
            metadata = ModelMetadata.model_validate_json(
                Path(args.metadata).read_text(encoding="utf-8"))
            result = registry.register(args.artifact, metadata, args.actor)
        elif args.action == "stage":
            result = registry.stage(args.model_id, args.version, args.actor,
                                    args.reason)
        elif args.action == "activate":
            result = registry.activate(args.model_id, args.version, args.actor,
                                       args.approval_reference,
                                       args.minimum_field_samples)
        elif args.action == "rollback":
            result = registry.rollback(args.model_id, args.version, args.actor,
                                       args.reason)
        elif args.action == "status":
            result = registry.get(args.model_id, args.version)
        elif args.action == "verify":
            path = registry.verify_artifact(args.model_id, args.version)
            result = {"status": "VERIFIED", "sha256": path.name}
        else:
            result = registry.history(args.model_id)
    print(json.dumps(result, sort_keys=True, indent=2))


if __name__ == "__main__":
    main()
