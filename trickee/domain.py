"""Canonical vehicle signals and deterministic local intelligence."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from math import cos, radians
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator


class Quality(StrEnum):
    OK = "OK"
    SIMULATED = "SIMULATED"
    ESTIMATED = "ESTIMATED"
    STALE = "STALE"
    INVALID = "INVALID"
    MISSING = "MISSING"


class VehicleState(StrEnum):
    UNKNOWN = "UNKNOWN"
    PARKED = "PARKED"
    IGNITION_ON = "IGNITION_ON"
    DRIVING = "DRIVING"
    STOPPED = "STOPPED"
    CHARGING = "CHARGING"
    CHARGING_COMPLETE = "CHARGING_COMPLETE"
    FAULT = "FAULT"
    OFFLINE = "OFFLINE"
    UPDATING = "UPDATING"


class SignalFrame(BaseModel):
    schema_version: Literal[1] = 1
    vehicle_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")
    sequence: int = Field(ge=1)
    observed_at: datetime
    source: Literal["SIMULATOR", "CAR_API", "REPLAY", "OBD", "CAN"]
    soc_pct: float | None = Field(default=None, ge=0, le=100)
    soh_pct: float | None = Field(default=None, ge=0, le=100)
    speed_kmh: float | None = Field(default=None, ge=0, le=300)
    odometer_km: float | None = Field(default=None, ge=0)
    latitude: float | None = Field(default=None, ge=-90, le=90)
    longitude: float | None = Field(default=None, ge=-180, le=180)
    heading_deg: float | None = Field(default=None, ge=0, lt=360)
    battery_temperature_c: float | None = Field(default=None, ge=-50, le=100)
    battery_voltage_v: float | None = Field(default=None, ge=0, le=1500)
    battery_current_a: float | None = Field(default=None, ge=-2000, le=2000)
    charging: bool = False
    charging_power_kw: float | None = Field(default=None, ge=0, le=500)
    gear: str | None = None
    ignition: bool | None = None
    outside_temperature_c: float | None = Field(default=None, ge=-80, le=70)
    hvac_power_kw: float | None = Field(default=None, ge=0, le=20)
    fault_code: str | None = Field(default=None, max_length=80)
    trip_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")

    @model_validator(mode="after")
    def coherent(self) -> "SignalFrame":
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at requires timezone")
        if self.charging and (self.speed_kmh or 0) > 1:
            raise ValueError("vehicle cannot charge while driving")
        if self.charging_power_kw and not self.charging:
            raise ValueError("charging power requires charging state")
        if (self.latitude is None) != (self.longitude is None):
            raise ValueError("latitude and longitude must be paired")
        return self

    def quality(self, now: datetime | None = None) -> Quality:
        age = ((now or datetime.now(timezone.utc)) - self.observed_at).total_seconds()
        if age < -120:
            return Quality.INVALID
        if age > 30:
            return Quality.STALE
        return Quality.SIMULATED if self.source in ("SIMULATOR", "REPLAY") else Quality.OK


class VehicleProfile(BaseModel):
    vehicle_id: str
    usable_capacity_kwh: float = Field(gt=0, le=300)
    mass_kg: float = Field(gt=300, le=10000)
    drag_area_m2: float = Field(gt=0.1, le=8)
    rolling_coefficient: float = Field(gt=0.001, le=0.05)
    max_charge_kw: float = Field(gt=0, le=1000)
    connectors: list[str]
    reserve_soc_pct: float = Field(default=10, ge=0, le=50)


class Route(BaseModel):
    distance_km: float = Field(gt=0, le=3000)
    baseline_eta_min: float = Field(gt=0, le=10000)
    average_speed_kmh: float = Field(gt=1, le=160)
    average_grade: float = Field(default=0, ge=-0.15, le=0.15)
    traffic_factor: float = Field(default=1, ge=0.5, le=3)
    source: Literal["SIMULATED", "PROVIDER"] = "SIMULATED"


class RangePrediction(BaseModel):
    quality: Quality
    energy_kwh: float
    consumption_kwh_per_100km: float
    range_km: float
    arrival_soc_pct: float
    arrival_soc_interval_pct: tuple[float, float]
    uncertainty_kwh: float
    risk: Literal["LOW", "MEDIUM", "HIGH"]
    model_version: str = "physics-1"


def predict_range(frame: SignalFrame, profile: VehicleProfile, route: Route) -> RangePrediction | None:
    if frame.soc_pct is None or frame.soh_pct == 0:
        return None
    speed = route.average_speed_kmh / route.traffic_factor
    v_mps = speed / 3.6
    rolling_n = profile.mass_kg * 9.81 * profile.rolling_coefficient
    aero_n = 0.5 * 1.225 * profile.drag_area_m2 * v_mps * v_mps
    grade_n = profile.mass_kg * 9.81 * route.average_grade
    traction_kwh_per_km = (rolling_n + aero_n + grade_n) / 3600 / 0.87
    if traction_kwh_per_km < 0:
        traction_kwh_per_km *= 0.55  # bounded regenerative recovery
    hvac_kw = frame.hvac_power_kw if frame.hvac_power_kw is not None else 0.8
    consumption = max(0.055, traction_kwh_per_km + hvac_kw / speed)
    capacity = profile.usable_capacity_kwh * ((frame.soh_pct if frame.soh_pct is not None else 100) / 100)
    energy = consumption * route.distance_km
    arrival = max(0.0, frame.soc_pct - energy / capacity * 100)
    missing = sum(x is None for x in (frame.soh_pct, frame.hvac_power_kw, frame.battery_temperature_c))
    uncertainty = energy * (0.12 + 0.06 * missing)
    low = max(0.0, frame.soc_pct - (energy + uncertainty) / capacity * 100)
    high = min(100.0, frame.soc_pct - max(0.0, energy - uncertainty) / capacity * 100)
    risk = "HIGH" if low < profile.reserve_soc_pct else "MEDIUM" if arrival < profile.reserve_soc_pct + 5 else "LOW"
    return RangePrediction(
        quality=Quality.ESTIMATED,
        energy_kwh=round(energy, 2),
        consumption_kwh_per_100km=round(consumption * 100, 2),
        range_km=round(max(0, frame.soc_pct - profile.reserve_soc_pct) * capacity / 100 / consumption, 1),
        arrival_soc_pct=round(arrival, 1),
        arrival_soc_interval_pct=(round(low, 1), round(high, 1)),
        uncertainty_kwh=round(uncertainty, 2),
        risk=risk,
    )


class DriverModel(BaseModel):
    driver_id: str
    multiplier: float = Field(default=1, ge=0.5, le=2)
    trips: int = 0

    def updated(self, actual_min: float, baseline_min: float) -> "DriverModel":
        if actual_min <= 0 or baseline_min <= 0:
            raise ValueError("trip duration must be positive")
        measured = max(0.5, min(2, actual_min / baseline_min))
        weight = min(self.trips, 19)
        return self.model_copy(update={"multiplier": (self.multiplier * weight + measured) / (weight + 1), "trips": self.trips + 1})

    def eta(self, route: Route) -> dict:
        return {"baseline_min": route.baseline_eta_min, "personal_multiplier": round(self.multiplier, 3), "trickee_eta_min": round(route.baseline_eta_min * self.multiplier * route.traffic_factor, 1), "quality": "ESTIMATED"}


class Charger(BaseModel):
    model_config = ConfigDict(allow_inf_nan=False, extra="forbid")
    charger_id: str = Field(min_length=1, max_length=80)
    name: str = Field(min_length=1, max_length=160)
    connector: str = Field(min_length=1, max_length=40)
    power_kw: float = Field(gt=0)
    distance_km: float = Field(ge=0)
    detour_min: float = Field(ge=0)
    queue_min: float = Field(ge=0)
    price_per_kwh: float = Field(ge=0)
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    reliability: float = Field(ge=0, le=1)
    available: bool
    source: Literal["SIMULATED", "PROVIDER"] = "SIMULATED"


class ChargerProvider(Protocol):
    def nearby(self, latitude: float | None, longitude: float | None) -> list[Charger]: ...


class MockChargerProvider:
    def nearby(self, latitude: float | None, longitude: float | None) -> list[Charger]:
        return [
            Charger(charger_id="DEMO-A", name="North Hub", connector="CCS2", power_kw=120, distance_km=14, detour_min=8, queue_min=5, price_per_kwh=0.32, reliability=0.96, available=True),
            Charger(charger_id="DEMO-B", name="East Point", connector="CCS2", power_kw=60, distance_km=9, detour_min=4, queue_min=0, price_per_kwh=0.25, reliability=0.90, available=True),
            Charger(charger_id="DEMO-C", name="Unavailable", connector="CCS2", power_kw=180, distance_km=5, detour_min=3, queue_min=20, price_per_kwh=0.40, reliability=0.70, available=False),
        ]


def rank_chargers(chargers: list[Charger], frame: SignalFrame, profile: VehicleProfile, policy: str = "BALANCED") -> list[dict]:
    if frame.soc_pct is None or frame.soh_pct == 0:
        return []
    capacity = profile.usable_capacity_kwh * ((frame.soh_pct if frame.soh_pct is not None else 100) / 100)
    ranked = []
    for station in chargers:
        if station.distance_km == 0:
            arrival = frame.soc_pct
        else:
            leg = Route(distance_km=station.distance_km, baseline_eta_min=station.distance_km / 45 * 60, average_speed_kmh=45)
            prediction = predict_range(frame, profile, leg)
            arrival = prediction.arrival_soc_pct if prediction else 0
        if not station.available or station.connector not in profile.connectors or arrival < profile.reserve_soc_pct:
            continue
        power = min(station.power_kw, profile.max_charge_kw)
        if frame.battery_temperature_c is not None and (frame.battery_temperature_c < 10 or frame.battery_temperature_c > 45):
            power *= 0.5
        start_soc = arrival / 100
        charging_min = 0.0
        for lower, upper, fraction in ((0.0, 0.5, 1.0), (0.5, 0.7, 0.75), (0.7, 0.8, 0.5)):
            segment = max(0.0, min(0.8, upper) - max(start_soc, lower))
            charging_min += segment * capacity / (power * fraction) * 60
        total_min = station.detour_min + station.queue_min + charging_min
        cost = max(0, 0.8 - arrival / 100) * capacity * station.price_per_kwh
        if policy == "TIME_PRIORITY":
            score = total_min + cost * 0.1
        elif policy == "COST_PRIORITY":
            score = cost * 10 + total_min * 0.1
        elif policy == "BATTERY_HEALTH_PRIORITY":
            score = total_min * 0.3 + max(0, power - 80) * 0.5 + cost
        else:
            score = total_min + cost * 2 + (1 - station.reliability) * 60
        ranked.append({"charger": station.model_dump(), "arrival_soc_pct": round(arrival, 1), "charge_time_min": round(charging_min, 1), "estimated_cost": round(cost, 2), "score": round(score, 2), "quality": "ESTIMATED"})
    return sorted(ranked, key=lambda x: x["score"])


_ALLOWED: dict[VehicleState, set[VehicleState]] = {
    VehicleState.UNKNOWN: set(VehicleState),
    VehicleState.PARKED: {VehicleState.PARKED, VehicleState.IGNITION_ON, VehicleState.DRIVING, VehicleState.CHARGING, VehicleState.FAULT, VehicleState.OFFLINE, VehicleState.UPDATING},
    VehicleState.IGNITION_ON: {VehicleState.IGNITION_ON, VehicleState.DRIVING, VehicleState.STOPPED, VehicleState.PARKED, VehicleState.FAULT, VehicleState.OFFLINE},
    VehicleState.DRIVING: {VehicleState.DRIVING, VehicleState.STOPPED, VehicleState.FAULT, VehicleState.OFFLINE},
    VehicleState.STOPPED: {VehicleState.STOPPED, VehicleState.DRIVING, VehicleState.PARKED, VehicleState.CHARGING, VehicleState.FAULT, VehicleState.OFFLINE},
    VehicleState.CHARGING: {VehicleState.CHARGING, VehicleState.CHARGING_COMPLETE, VehicleState.PARKED, VehicleState.FAULT, VehicleState.OFFLINE},
    VehicleState.CHARGING_COMPLETE: {VehicleState.CHARGING_COMPLETE, VehicleState.PARKED, VehicleState.IGNITION_ON, VehicleState.FAULT, VehicleState.OFFLINE},
    VehicleState.FAULT: {VehicleState.FAULT, VehicleState.PARKED, VehicleState.OFFLINE},
    VehicleState.OFFLINE: set(VehicleState),
    VehicleState.UPDATING: {VehicleState.UPDATING, VehicleState.PARKED, VehicleState.FAULT, VehicleState.OFFLINE},
}


def infer_state(frame: SignalFrame) -> VehicleState:
    if frame.fault_code:
        return VehicleState.FAULT
    if frame.charging:
        return VehicleState.CHARGING
    if (frame.speed_kmh or 0) > 1:
        return VehicleState.DRIVING
    if frame.ignition is True:
        return VehicleState.STOPPED
    if frame.ignition is False:
        return VehicleState.PARKED
    return VehicleState.UNKNOWN


def transition(previous: VehicleState, next_state: VehicleState) -> VehicleState:
    if next_state not in _ALLOWED[previous]:
        raise ValueError(f"invalid vehicle transition {previous} -> {next_state}")
    return next_state


def health(frame: SignalFrame) -> dict:
    evidence = []
    severity = "NORMAL"
    if frame.fault_code:
        evidence.append(f"fault:{frame.fault_code}")
        severity = "WARNING"
    if frame.battery_temperature_c is not None and frame.battery_temperature_c >= 55:
        evidence.append("battery_temperature_high")
        severity = "CRITICAL"
    elif frame.battery_temperature_c is not None and frame.battery_temperature_c >= 45:
        evidence.append("battery_temperature_elevated")
        severity = "WARNING"
    if frame.soh_pct is not None and frame.soh_pct < 75 and severity == "NORMAL":
        evidence.append("soh_below_watch_threshold")
        severity = "WATCH"
    return {"status": severity, "evidence": evidence, "confidence": "RULE_BASED", "diagnostic_probability": None, "observed_at": frame.observed_at.isoformat()}
