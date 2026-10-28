"""Seeded multi-scenario synthetic evaluation data for local development.

This generator has no access to field outcomes. Every row is permanently marked
SIMULATED and must never be used as evidence of real-world model accuracy.
"""
from __future__ import annotations

import argparse
import random
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .domain import Route, SignalFrame, VehicleProfile, predict_range
from .model_eval import EvaluationRow


VEHICLES = (
    ("CITY-01", 32.0, 1250.0, .58, .011),
    ("CITY-02", 42.0, 1530.0, .64, .012),
    ("FLEET-01", 65.0, 1900.0, .70, .011),
    ("VAN-01", 85.0, 2550.0, .92, .013),
)
ROADS = ("URBAN", "HIGHWAY", "HILL", "MIXED")
WEATHERS = ("CLEAR", "RAIN", "HOT", "COLD")
SEASONS = ("WINTER", "SPRING", "SUMMER", "MONSOON")
CHARGER_CLASSES = ("AC", "DC", "HPC")


def generate(count: int = 1200, seed: int = 2026) -> list[EvaluationRow]:
    if count < 1:
        raise ValueError("trip count must be positive")
    rng = random.Random(seed)
    rows: list[EvaluationRow] = []
    start = datetime(2025, 1, 1, tzinfo=timezone.utc)
    for index in range(count):
        vehicle_id, capacity, mass, drag, rolling = VEHICLES[index % len(VEHICLES)]
        road = ROADS[(index // len(VEHICLES)) % len(ROADS)]
        weather = WEATHERS[(index // 3) % len(WEATHERS)]
        season = SEASONS[(index // 11) % len(SEASONS)]
        charger_class = CHARGER_CLASSES[(index // 5) % len(CHARGER_CLASSES)]
        driver_index = index % 24
        driver_factor = .85 + driver_index / 24 * .35
        trip_id = f"SYN-TRIP-{index:05d}"
        split = "TEST" if index % 10 == 0 else "VALIDATION" if index % 10 == 1 else "TRAIN"
        observed_at = start + timedelta(hours=index * 6)
        temperature = {"CLEAR": 23, "RAIN": 18, "HOT": 39, "COLD": 3}[weather] + rng.uniform(-5, 5)
        soc = rng.uniform(18, 95)
        soh = rng.uniform(75, 100)
        distance = rng.uniform(8, 180)
        speed = {"URBAN": 31, "HIGHWAY": 81, "HILL": 44, "MIXED": 55}[road]
        traffic = rng.uniform(1, 1.8) if road == "URBAN" else rng.uniform(.9, 1.3)
        profile = VehicleProfile(
            vehicle_id=vehicle_id, usable_capacity_kwh=capacity, mass_kg=mass,
            drag_area_m2=drag, rolling_coefficient=rolling,
            max_charge_kw=100, connectors=["CCS2"],
        )
        frame = SignalFrame(
            vehicle_id=vehicle_id, sequence=index + 1, observed_at=observed_at,
            source="SIMULATOR", soc_pct=soc, soh_pct=soh,
            speed_kmh=speed, ignition=True,
            battery_temperature_c=temperature,
            outside_temperature_c=temperature,
            hvac_power_kw=2.5 if weather in ("HOT", "COLD") else .8,
            trip_id=trip_id,
        )
        map_eta = distance / speed * 60
        route = Route(distance_km=distance, baseline_eta_min=map_eta,
                      average_speed_kmh=speed, traffic_factor=traffic,
                      average_grade=.035 if road == "HILL" else 0)
        prediction = predict_range(frame, profile, route)
        assert prediction is not None
        weather_penalty = {"CLEAR": 1.0, "RAIN": .9, "HOT": .94,
                           "COLD": .83}[weather]
        true_range = max(0, prediction.range_km * weather_penalty *
                         rng.gauss(1, .055))
        personal_eta = map_eta * driver_factor
        contextual_eta = personal_eta * traffic
        true_eta = max(1, contextual_eta *
                       (1.08 if weather == "RAIN" else 1) * rng.gauss(1, .08))
        queue_baseline = {"AC": 18, "DC": 9, "HPC": 6}[charger_class]
        queue_actual = max(0, queue_baseline +
                           (6 if weather == "RAIN" else 0) + rng.gauss(0, 4))
        available_probability = {"AC": .79, "DC": .86,
                                 "HPC": .91}[charger_class]
        available_probability -= .08 if weather == "RAIN" else 0
        health_probability = min(.95, .03 + max(0, 85 - soh) * .012 +
                                 max(0, temperature - 42) * .018)
        common = dict(trip_id=trip_id, source="SIMULATED", split=split,
                      observed_at=observed_at, vehicle_id=vehicle_id,
                      driver_id=f"DRIVER-{driver_index:02d}", road_type=road,
                      weather=weather, temperature_c=round(temperature, 2),
                      soc_pct=round(soc, 2), season=season,
                      soh_pct=round(soh, 2),
                      charger_class=charger_class)
        rows.extend((
            EvaluationRow(sample_id=f"{trip_id}-RANGE", task="RANGE",
                          predicted=prediction.range_km,
                          actual=round(true_range, 2),
                          interval_low=max(0, round(prediction.range_km * .78, 2)),
                          interval_high=round(prediction.range_km * 1.22, 2), **common),
            EvaluationRow(sample_id=f"{trip_id}-ETA", task="ETA",
                          predicted=round(contextual_eta, 2),
                          actual=round(true_eta, 2),
                          baseline_predicted=round(map_eta, 2),
                          personal_predicted=round(personal_eta, 2), **common),
            EvaluationRow(sample_id=f"{trip_id}-QUEUE", task="CHARGER_QUEUE",
                          predicted=queue_baseline,
                          actual=round(queue_actual, 2), **common),
            EvaluationRow(sample_id=f"{trip_id}-AVAIL", task="CHARGER_AVAILABILITY",
                          predicted=round(available_probability, 4),
                          actual=int(rng.random() < available_probability), **common),
            EvaluationRow(sample_id=f"{trip_id}-HEALTH", task="HEALTH",
                          predicted=round(health_probability, 4),
                          actual=int(rng.random() < health_probability),
                          lead_time_hours=round(rng.uniform(2, 48), 1), **common),
        ))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate labelled synthetic trips")
    parser.add_argument("--count", type=int, default=1200)
    parser.add_argument("--seed", type=int, default=2026)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        for row in generate(args.count, args.seed):
            stream.write(row.model_dump_json() + "\n")
    temporary.replace(destination)


if __name__ == "__main__":
    main()
