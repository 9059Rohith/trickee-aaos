from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from trickee.agent import VehicleAgent
from trickee.domain import DriverModel, Route, SignalFrame, VehicleState, infer_state, predict_range, transition
from trickee.simulator import SCENARIOS, demo_profile, scenario_frame


def test_all_scenarios_are_valid_synthetic_frames():
    assert len(SCENARIOS) == 15
    for number, code in enumerate(SCENARIOS, 1):
        frame = scenario_frame(code, number)
        assert frame.source == "SIMULATOR"
        assert frame.quality().value == "SIMULATED"


def test_signal_gateway_rejects_invalid_and_stale():
    frame = scenario_frame("SCENARIO_001", 1)
    with pytest.raises(ValidationError):
        SignalFrame.model_validate({**frame.model_dump(), "soc_pct": 101})
    with pytest.raises(ValidationError):
        SignalFrame.model_validate({**frame.model_dump(), "charging": True})
    stale = scenario_frame("SCENARIO_001", 2, datetime.now(timezone.utc) - timedelta(minutes=1))
    assert stale.quality().value == "STALE"


def test_state_machine_rejects_impossible_transition():
    assert transition(VehicleState.PARKED, VehicleState.DRIVING) == VehicleState.DRIVING
    with pytest.raises(ValueError):
        transition(VehicleState.DRIVING, VehicleState.CHARGING)
    with pytest.raises(ValueError):
        transition(VehicleState.UPDATING, VehicleState.DRIVING)


def test_physics_range_and_personal_eta():
    frame = scenario_frame("SCENARIO_001", 1)
    route = Route(distance_km=91, baseline_eta_min=95, average_speed_kmh=58)
    prediction = predict_range(frame, demo_profile(), route)
    assert 0 < prediction.energy_kwh < 50
    assert 0 < prediction.arrival_soc_pct < 64
    assert prediction.arrival_soc_interval_pct[0] <= prediction.arrival_soc_pct <= prediction.arrival_soc_interval_pct[1]
    assert prediction.quality.value == "ESTIMATED"
    slower = DriverModel(driver_id="D").updated(110, 100)
    assert slower.eta(route)["trickee_eta_min"] > route.baseline_eta_min


def test_offline_agent_local_decisions_and_charger_filter(tmp_path):
    agent = VehicleAgent(demo_profile(), str(tmp_path / "outbox.db"))
    agent.capture(scenario_frame("SCENARIO_001", 1))
    route = Route(distance_km=91, baseline_eta_min=95, average_speed_kmh=58)
    assert agent.local_status(route)["range"]["energy_kwh"] > 0
    assert agent.copilot("Can I reach it?", route)["tool"] == "get_range_prediction"
    assert agent.copilot("steer left", route)["tier"] == "DENIED"
    assert all(c["charger"]["available"] for c in agent.recommend_chargers())
    assert agent.outbox.count_pending() == 1


def test_missing_bms_is_unknown_not_fabricated(tmp_path):
    frame = scenario_frame("SCENARIO_001", 1).model_copy(update={"soc_pct": None, "soh_pct": None})
    agent = VehicleAgent(demo_profile(), str(tmp_path / "outbox.db"))
    agent.capture(frame)
    route = Route(distance_km=50, baseline_eta_min=50, average_speed_kmh=50)
    assert agent.local_status(route)["range"] is None
    assert "unavailable" in agent.copilot("battery")["text"]


def test_missing_ignition_does_not_claim_parked_state():
    frame = scenario_frame("SCENARIO_005", 1).model_copy(update={"ignition": None})
    assert infer_state(frame) == VehicleState.UNKNOWN
