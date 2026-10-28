"""Vehicle-side local operations and offline synchronization."""
from __future__ import annotations

from typing import Callable

from .domain import (
    ChargerProvider, DriverModel, MockChargerProvider, Quality, Route, SignalFrame,
    VehicleProfile, VehicleState, health, infer_state, predict_range, rank_chargers,
    transition,
)
from .store import Outbox


class VehicleAgent:
    def __init__(self, profile: VehicleProfile, outbox_path: str, chargers: ChargerProvider | None = None):
        self.profile = profile
        self.outbox = Outbox(outbox_path)
        self.frame = self.outbox.last_frame()
        self.state = infer_state(self.frame) if self.frame else VehicleState.UNKNOWN
        self.chargers = chargers or MockChargerProvider()
        self.driver = DriverModel(driver_id="local-driver")
        self.transitions: list[tuple[str, str]] = []

    def capture(self, frame: SignalFrame) -> dict:
        if frame.vehicle_id != self.profile.vehicle_id:
            raise ValueError("frame vehicle identity mismatch")
        if self.frame and frame.sequence <= self.frame.sequence:
            raise ValueError("sequence must increase")
        if self.frame and frame.observed_at < self.frame.observed_at:
            raise ValueError("observation time must not move backwards")
        prior_state = (VehicleState.OFFLINE if self.frame and
                       self.frame.quality() not in (Quality.OK, Quality.SIMULATED)
                       else self.state)
        next_state = transition(prior_state, infer_state(frame))
        self.outbox.append(frame)
        if next_state != prior_state:
            self.transitions.append((prior_state.value, next_state.value))
        self.frame = frame
        self.state = next_state
        return self.local_status()

    def local_status(self, route: Route | None = None) -> dict:
        if not self.frame:
            return {"vehicle_id": self.profile.vehicle_id, "state": "UNKNOWN", "quality": "MISSING", "pending_events": self.outbox.count_pending()}
        quality = self.frame.quality()
        usable = quality in (Quality.OK, Quality.SIMULATED)
        prediction = predict_range(self.frame, self.profile, route) if route and usable else None
        return {
            "vehicle_id": self.profile.vehicle_id,
            "state": self.state.value if usable else "OFFLINE" if quality == Quality.STALE else "UNKNOWN",
            "quality": quality.value,
            "battery": {"soc_pct": self.frame.soc_pct, "soh_pct": self.frame.soh_pct},
            "health": health(self.frame) if usable else {"status": "UNKNOWN", "evidence": [], "confidence": "NONE"},
            "range": prediction.model_dump(mode="json") if prediction else None,
            "eta": self.driver.eta(route) if route and usable else None,
            "pending_events": self.outbox.count_pending(),
        }

    def recommend_chargers(self, policy: str = "BALANCED") -> list[dict]:
        if not self.frame or self.frame.quality() not in (Quality.OK, Quality.SIMULATED):
            return []
        return rank_chargers(self.chargers.nearby(self.frame.latitude, self.frame.longitude), self.frame, self.profile, policy)

    def copilot(self, request: str, route: Route | None = None) -> dict:
        words = request.lower()
        if any(x in words for x in ("brake", "steer", "accelerate", "unlock", "start charging", "set charge")):
            return {"tier": "DENIED", "text": "That vehicle control is not available to the copilot.", "tool": None}
        if any(x in words for x in ("reach", "range", "arrive")):
            if not self.frame or self.frame.quality() not in (Quality.OK, Quality.SIMULATED):
                return {"tier": "T0", "text": "Vehicle telemetry is unavailable; I cannot estimate arrival charge.", "tool": "get_range_prediction"}
            if route is None:
                return {"tier": "T0", "text": "Set a route to estimate arrival charge.", "tool": "get_range_prediction"}
            prediction = self.local_status(route)["range"]
            if prediction is None:
                return {"tier": "T0", "text": "Battery charge is unavailable; I cannot estimate arrival charge.", "tool": "get_range_prediction"}
            return {"tier": "T0", "text": f"Estimated arrival charge is {prediction['arrival_soc_pct']}%, with a {prediction['arrival_soc_interval_pct'][0]}–{prediction['arrival_soc_interval_pct'][1]}% interval. Risk: {prediction['risk']}.", "tool": "get_range_prediction", "data": prediction}
        if "charger" in words:
            if not self.frame or self.frame.quality() not in (Quality.OK, Quality.SIMULATED):
                return {"tier": "T1", "text": "Vehicle telemetry is unavailable; charger reachability cannot be assessed.", "tool": "find_chargers", "data": []}
            stations = self.recommend_chargers()
            return {"tier": "T1", "text": f"The best simulated charger is {stations[0]['charger']['name']}." if stations else "No reachable charger is available in the current data.", "tool": "find_chargers", "data": stations}
        if "battery" in words or "soc" in words:
            soc = self.frame.soc_pct if self.frame and self.frame.quality() in (Quality.OK, Quality.SIMULATED) else None
            return {"tier": "T0", "text": f"Battery charge is {soc}%." if soc is not None else "Battery charge is unavailable.", "tool": "get_battery_state"}
        if "health" in words or "fault" in words:
            status = health(self.frame) if self.frame and self.frame.quality() in (Quality.OK, Quality.SIMULATED) else {"status": "UNKNOWN"}
            return {"tier": "T0", "text": f"Vehicle health status is {status['status']}.", "tool": "get_vehicle_health", "data": status}
        return {"tier": "T0", "text": "I can answer about battery, route range, chargers, and vehicle health.", "tool": None}

    def sync(self, sender: Callable[[list[SignalFrame]], dict]) -> dict:
        total = 0
        while batch := self.outbox.pending(100):
            result = sender(batch)
            expected = {f.sequence for f in batch}
            acknowledged = {item["sequence"] for item in result.get("acknowledged", []) if item.get("vehicle_id") == self.profile.vehicle_id}
            if acknowledged != expected:
                raise ValueError("cloud did not acknowledge complete batch")
            self.outbox.acknowledge(sorted(acknowledged))
            total += len(acknowledged)
        return {"synchronized": total, "pending": self.outbox.count_pending()}
