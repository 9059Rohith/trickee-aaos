from trickee.model_eval import evaluate
from trickee.synthetic_dataset import generate


def test_1200_seeded_trips_have_diversity_and_explicit_synthetic_provenance():
    rows = generate()
    assert len(rows) == 6000
    assert len({row.trip_id for row in rows}) == 1200
    assert {row.task for row in rows} == {
        "RANGE", "ETA", "CHARGER_QUEUE", "CHARGER_AVAILABILITY", "HEALTH"}
    assert len({row.vehicle_id for row in rows}) == 4
    assert len({row.driver_id for row in rows}) == 24
    assert len({row.weather for row in rows}) == 4
    assert all(row.source == "SIMULATED" for row in rows)
    result = evaluate(rows)
    assert result["test_rows"] == 600
    assert all(task["source"] == "SIMULATED" and not task["field_verified"]
               for task in result["tasks"].values())


def test_seed_makes_dataset_reproducible_and_changes_are_detected():
    first = evaluate(generate(30, seed=7), minimum_samples=2)
    same = evaluate(generate(30, seed=7), minimum_samples=2)
    changed = evaluate(generate(30, seed=8), minimum_samples=2)
    assert first["dataset_sha256"] == same["dataset_sha256"]
    assert first["dataset_sha256"] != changed["dataset_sha256"]
