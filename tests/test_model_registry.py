"""Registry transitions preserve artifacts, evidence, and rollback history."""
import json
import subprocess
import sys
from datetime import datetime, timezone

import pytest

from trickee.model_eval import EvaluationRow, evaluate
from trickee.model_registry import ModelMetadata, ModelRegistry


def metadata(version: str, *, source: str = "SIMULATED", count: int = 3):
    rows = [EvaluationRow(
        sample_id=f"{version}-{index}", task="RANGE", source=source,
        split="TEST", observed_at=datetime.now(timezone.utc),
        predicted=100 + index, actual=101 + index,
    ) for index in range(count)]
    report = evaluate(rows, minimum_samples=2)
    return ModelMetadata(
        model_id="range-physics", version=version, task="RANGE",
        training_dataset_sha256="a" * 64,
        training_at=datetime.now(timezone.utc),
        features=["soc_pct", "temperature_c"],
        known_limitations=["Not validated on every vehicle profile"],
        evaluation=report,
    )


def test_registry_immutable_artifact_and_synthetic_staging(tmp_path):
    artifact = tmp_path / "model.json"
    artifact.write_text('{"coefficients":[1,2]}', encoding="utf-8")
    with ModelRegistry(tmp_path / "registry") as registry:
        created = registry.register(artifact, metadata("1"), actor="builder")
        assert created["status"] == "DEVELOPMENT"
        assert registry.verify_artifact("range-physics", "1").read_bytes() == artifact.read_bytes()
        with pytest.raises(ValueError, match="immutable"):
            registry.register(artifact, metadata("1"), actor="builder")
        staged = registry.stage("range-physics", "1", "reviewer", "local evaluation")
        assert staged["status"] == "STAGING"
        with pytest.raises(ValueError, match="field evaluation"):
            registry.activate("range-physics", "1", "reviewer", "ticket-123",
                              minimum_field_samples=2)
        assert [event["action"] for event in registry.history("range-physics")] == [
            "REGISTER", "STAGE"]
        artifact.write_text("mutated source", encoding="utf-8")
        assert registry.verify_artifact("range-physics", "1").read_text() == '{"coefficients":[1,2]}'


def test_field_evidence_selection_and_rollback_are_audited(tmp_path):
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"model-v1")
    with ModelRegistry(tmp_path / "registry") as registry:
        registry.register(artifact, metadata("1", source="FIELD"), "builder")
        registry.stage("range-physics", "1", "reviewer", "held-out check")
        assert registry.activate("range-physics", "1", "release", "approval-1",
                                 minimum_field_samples=3)["status"] == "PRODUCTION"
        artifact.write_bytes(b"model-v2")
        registry.register(artifact, metadata("2", source="FIELD"), "builder")
        registry.stage("range-physics", "2", "reviewer", "held-out check")
        registry.activate("range-physics", "2", "release", "approval-2",
                          minimum_field_samples=3)
        assert registry.get("range-physics", "1")["status"] == "RETIRED"
        assert registry.get("range-physics", "2")["status"] == "PRODUCTION"
        assert registry.rollback("range-physics", "1", "release",
                                 "regression observed")["status"] == "PRODUCTION"
        assert registry.get("range-physics", "2")["status"] == "ROLLED_BACK"
        assert [event["action"] for event in registry.history("range-physics")][-2:] == [
            "ROLLBACK_FROM", "ROLLBACK_TO"]


def test_corrupt_artifact_blocks_promotion(tmp_path):
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"model-v1")
    with ModelRegistry(tmp_path / "registry") as registry:
        registry.register(artifact, metadata("1", source="FIELD"), "builder")
        stored = registry.verify_artifact("range-physics", "1")
        stored.write_bytes(b"tampered")
        with pytest.raises(ValueError, match="hash mismatch"):
            registry.stage("range-physics", "1", "reviewer", "review")
        assert registry.get("range-physics", "1")["status"] == "DEVELOPMENT"


def test_registry_command_line_registers_and_verifies_artifact(tmp_path):
    artifact = tmp_path / "model.bin"
    artifact.write_bytes(b"model-v1")
    details = tmp_path / "metadata.json"
    details.write_text(metadata("cli-1").model_dump_json(), encoding="utf-8")
    root = tmp_path / "registry"
    command = [sys.executable, "-m", "trickee.model_registry", "--root", str(root)]
    registered = subprocess.run(command + ["register", "--artifact", str(artifact),
                                          "--metadata", str(details), "--actor", "builder"],
                                capture_output=True, text=True, check=True)
    assert json.loads(registered.stdout)["status"] == "DEVELOPMENT"
    verified = subprocess.run(command + ["verify", "--model-id", "range-physics",
                                        "--version", "cli-1"],
                              capture_output=True, text=True, check=True)
    assert json.loads(verified.stdout)["status"] == "VERIFIED"
