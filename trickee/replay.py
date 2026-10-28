"""Read-only historical trip timeline from canonical telemetry frames."""
from __future__ import annotations

from .domain import SignalFrame, health, infer_state


def trip_timeline(vehicle_id: str, trip_id: str, raw_frames: list[dict],
                  total_count: int) -> dict:
    if total_count < len(raw_frames):
        raise ValueError("trip frame count is inconsistent")
    snapshots = []
    previous_time = None
    for raw in raw_frames:
        frame = SignalFrame.model_validate(raw)
        if frame.vehicle_id != vehicle_id or frame.trip_id != trip_id:
            raise ValueError("trip replay contains a foreign frame")
        anomaly = previous_time is not None and frame.observed_at < previous_time
        previous_time = frame.observed_at
        snapshots.append({
            "sequence": frame.sequence,
            "observed_at": frame.observed_at.isoformat(),
            "original_source": frame.source,
            "state": infer_state(frame).value,
            "soc_pct": frame.soc_pct,
            "soh_pct": frame.soh_pct,
            "speed_kmh": frame.speed_kmh,
            "charging": frame.charging,
            "charging_power_kw": frame.charging_power_kw,
            "latitude": frame.latitude,
            "longitude": frame.longitude,
            "fault_code": frame.fault_code,
            "health": health(frame),
            "clock_anomaly": anomaly,
        })
    return {
        "vehicle_id": vehicle_id, "trip_id": trip_id,
        "quality": "HISTORICAL_REPLAY", "live": False,
        "total_frame_count": total_count,
        "returned_frame_count": len(snapshots),
        "truncated": total_count > len(snapshots),
        "timeline": snapshots,
    }
