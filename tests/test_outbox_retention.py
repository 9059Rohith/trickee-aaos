"""Vehicle offline data survives failure, while acknowledged history is pruned."""
from trickee.agent import VehicleAgent
from trickee.simulator import demo_profile, scenario_frame


def test_acknowledged_frames_pruned_without_losing_last_sequence(tmp_path):
    path = str(tmp_path / "vehicle.db")
    profile = demo_profile()
    agent = VehicleAgent(profile, path)
    for sequence in range(1, 11):
        agent.capture(scenario_frame("SCENARIO_001", sequence))

    def sender(frames):
        return {"acknowledged": [{"vehicle_id": profile.vehicle_id,
                                  "sequence": frame.sequence} for frame in frames]}

    assert agent.sync(sender) == {"synchronized": 10, "pending": 0}
    assert agent.outbox.db.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] == 1
    restarted = VehicleAgent(profile, path)
    assert restarted.frame.sequence == 10
    restarted.capture(scenario_frame("SCENARIO_001", 11))
    assert restarted.outbox.count_pending() == 1
    assert restarted.outbox.pending()[0].sequence == 11


def test_partial_ack_only_prunes_acknowledged_rows(tmp_path):
    path = str(tmp_path / "vehicle.db")
    agent = VehicleAgent(demo_profile(), path)
    for sequence in range(1, 5):
        agent.capture(scenario_frame("SCENARIO_001", sequence))
    agent.outbox.acknowledge([1, 2])
    assert [frame.sequence for frame in agent.outbox.pending()] == [3, 4]
    assert [row[0] for row in agent.outbox.db.execute(
        "SELECT sequence FROM outbox ORDER BY sequence")] == [3, 4]
