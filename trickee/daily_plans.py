"""Deterministic, evidence-labeled daily plan calculations."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .domain import Route, SignalFrame, VehicleProfile, predict_range
from .providers import RoadProviderBadResponse, RoadProviderUnavailable


def calculate_plan(vehicle_id: str, profile_data: dict, plan: dict, router) -> dict:
    """A provider route is required for numeric route and energy predictions."""
    profile = VehicleProfile.model_validate(profile_data)
    day = date.fromisoformat(plan["service_date"])
    zone = ZoneInfo(plan["timezone"])
    previous = plan["origin"]
    soc = plan["starting_soc_pct"]
    legs = []
    for index, stop in enumerate(plan["stops"], start=1):
        requested = datetime.combine(day, datetime.strptime(stop["requested_arrival_local"], "%H:%M").time(), zone)
        leg = {
            "index": index, "origin": previous, "destination": stop,
            "requested_arrival_at": requested.isoformat(),
            "planned_departure_at": None, "estimated_arrival_at": None,
            "distance_km": None, "duration_min": None,
            "starting_soc_pct": soc, "arrival_soc_pct": None,
            "energy_kwh": None, "route_source": "UNAVAILABLE", "energy_source": "UNAVAILABLE",
            "traffic_source": "UNAVAILABLE", "degraded_reason": "ROAD_ROUTER_NOT_CONFIGURED" if router is None else None,
            "schedule_conflict": None,
        }
        if router is not None:
            try:
                response = router.route(
                    (previous["latitude"], previous["longitude"]),
                    (stop["coordinates"]["latitude"], stop["coordinates"]["longitude"]),
                )
                route = Route.model_validate(response["routes"][0]["route"])
                leg["distance_km"] = round(route.distance_km, 2)
                leg["duration_min"] = round(route.baseline_eta_min, 1)
                leg["route_source"] = "PROVIDER"
                leg["planned_departure_at"] = (requested - timedelta(minutes=route.baseline_eta_min)).isoformat()
                leg["estimated_arrival_at"] = requested.isoformat()
                if index > 1:
                    prior_arrival = datetime.fromisoformat(legs[-1]["requested_arrival_at"])
                    leg["schedule_conflict"] = datetime.fromisoformat(leg["planned_departure_at"]) < prior_arrival
                if soc is not None:
                    frame = SignalFrame(
                        vehicle_id=vehicle_id, sequence=index, observed_at=datetime.now(timezone.utc),
                        source="REPLAY", soc_pct=soc,
                    )
                    prediction = predict_range(frame, profile, route)
                    if prediction:
                        leg["energy_kwh"] = prediction.energy_kwh
                        leg["arrival_soc_pct"] = prediction.arrival_soc_pct
                        leg["energy_source"] = prediction.model_version
                        soc = prediction.arrival_soc_pct
            except (RoadProviderUnavailable, RoadProviderBadResponse, ValueError, KeyError, IndexError, TypeError) as exc:
                leg["degraded_reason"] = type(exc).__name__.upper()
        legs.append(leg)
        previous = stop["coordinates"]
        if leg["arrival_soc_pct"] is None:
            soc = None
    quality = "ESTIMATED" if all(leg["route_source"] == "PROVIDER" for leg in legs) else "PARTIAL" if any(leg["route_source"] == "PROVIDER" for leg in legs) else "UNAVAILABLE"
    return {
        "quality": quality, "legs": legs,
        "traffic_source": "UNAVAILABLE",
        "charger_status": "UNAVAILABLE",
        "energy_assumptions": "Nominal usable capacity; SOH, HVAC, weather, grade and traffic are unmeasured in this plan.",
        "calculated_at": datetime.now(timezone.utc).isoformat(),
    }
