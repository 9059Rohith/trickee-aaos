from datetime import datetime, timezone

import pytest

from trickee.model_eval import EvaluationRow, evaluate
from trickee.model_registry import ModelMetadata, ModelRegistry
from trickee.residual_model import ResidualModel, evaluate_residual, train
from trickee.synthetic_dataset import generate


def test_range_residual_improves_seeded_held_out_synthetic_baseline():
    rows = generate(400, seed=2026)
    model = train(rows, "RANGE")
    report = evaluate_residual(model, rows)
    assert model.source == "SIMULATED" and model.training_count == 320
    assert report["test_rows"] == 40
    assert report["residual"]["mae"] < report["baseline"]["mae"]
    assert report["residual"]["field_verified"] is False
    restored = ResidualModel.model_validate_json(model.model_dump_json())
    assert restored.predict(100, temperature_c=30, soc_pct=70,
                            road_type="URBAN", weather="RAIN") == model.predict(
                                100, temperature_c=30, soc_pct=70,
                                road_type="URBAN", weather="RAIN")


def test_eta_residual_uses_train_split_and_rejects_bad_baseline():
    rows = generate(400, seed=123)
    model = train(rows, "ETA")
    assert model.training_count == 320
    assert evaluate_residual(model, rows)["residual"]["status"] == "MEASURED"
    with pytest.raises(ValueError, match="positive"):
        model.predict(0)
    with pytest.raises(ValueError, match="30 training"):
        train([row for row in rows if row.split == "TEST"], "ETA")


def test_training_rejects_trip_leakage_and_invalid_regularization():
    rows = generate(40)
    example = next(row for row in rows if row.task == "RANGE" and row.split == "TRAIN")
    leaked = EvaluationRow.model_validate({
        **example.model_dump(), "sample_id": "leaked-test", "split": "TEST",
        "observed_at": datetime.now(timezone.utc),
    })
    with pytest.raises(ValueError, match="multiple dataset splits"):
        train(rows + [leaked], "RANGE")
    with pytest.raises(ValueError, match="positive regularization"):
        train(rows, "RANGE", regularization=0)


def test_experimental_model_artifact_can_be_registered_but_not_field_promoted(tmp_path):
    rows = generate(400)
    model = train(rows, "RANGE")
    test_rows = [row.model_copy(update={"predicted": model.predict(
        row.predicted, temperature_c=row.temperature_c, soc_pct=row.soc_pct,
        road_type=row.road_type, weather=row.weather)})
        for row in rows if row.task == "RANGE" and row.split == "TEST"]
    evaluation = evaluate(test_rows)
    artifact = tmp_path / "range.json"
    artifact.write_text(model.model_dump_json(), encoding="utf-8")
    metadata = ModelMetadata(
        model_id="range-residual", version="synthetic-1", task="RANGE",
        training_dataset_sha256=model.training_dataset_sha256,
        training_at=model.trained_at,
        features=list(model.feature_names),
        known_limitations=["Trained and evaluated on generated trips only"],
        evaluation=evaluation,
    )
    with ModelRegistry(tmp_path / "registry") as registry:
        registry.register(artifact, metadata, "builder")
        registry.stage("range-residual", "synthetic-1", "reviewer", "local QA")
        assert registry.verify_artifact("range-residual", "synthetic-1").exists()
        with pytest.raises(ValueError, match="field evaluation"):
            registry.activate("range-residual", "synthetic-1", "release",
                              "local-only", minimum_field_samples=30)
