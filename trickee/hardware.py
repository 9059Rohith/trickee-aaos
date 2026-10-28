"""Read-only hardware adapters and the canonical vehicle-signal gateway.

CAN identifiers are vehicle-specific. They must come from an approved mapping;
this module deliberately contains no guessed OEM signal IDs or transmit API.
"""
from __future__ import annotations

import json
import socket
import struct
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Iterable, Literal, Protocol

from .domain import SignalFrame


SIGNAL_FIELDS = frozenset({
    "soc_pct", "soh_pct", "speed_kmh", "odometer_km", "latitude", "longitude",
    "heading_deg", "battery_temperature_c", "battery_voltage_v",
    "battery_current_a", "charging", "charging_power_kw", "gear", "ignition",
    "outside_temperature_c", "hvac_power_kw", "fault_code", "trip_id",
})


@dataclass(frozen=True)
class HardwareSample:
    source: Literal["SIMULATOR", "REPLAY", "OBD", "CAN"]
    observed_at: datetime
    values: dict[str, object]

    def __post_init__(self) -> None:
        if self.observed_at.tzinfo is None:
            raise ValueError("hardware observation must have a timezone")
        unknown = set(self.values) - SIGNAL_FIELDS
        if unknown:
            raise ValueError(f"unrecognized canonical signals: {sorted(unknown)}")


class VehicleHardwareAdapter(Protocol):
    def read(self) -> HardwareSample: ...
    def close(self) -> None: ...


class SignalGateway:
    """Merge only fresh fields; stale field values are never silently reused."""

    def __init__(self, vehicle_id: str, next_sequence: int = 1,
                 freshness_seconds: float = 5,
                 now: Callable[[], datetime] | None = None):
        if next_sequence < 1 or freshness_seconds <= 0:
            raise ValueError("invalid sequence or freshness window")
        self.vehicle_id = vehicle_id
        self.next_sequence = next_sequence
        self.freshness = timedelta(seconds=freshness_seconds)
        self._latest: dict[str, tuple[datetime, object]] = {}
        self._observed_at: datetime | None = None
        self._source: str | None = None
        self._now = now or (lambda: datetime.now(timezone.utc))

    def accept(self, sample: HardwareSample) -> SignalFrame:
        now = self._now()
        if sample.observed_at > now + timedelta(seconds=120):
            raise ValueError("hardware observation is in the future")
        if self._observed_at is not None and sample.observed_at < self._observed_at:
            raise ValueError("hardware observation time moved backwards")
        # Do not merge an actual vehicle stream with synthetic or replay data.
        candidate = ({} if self._source is not None and sample.source != self._source
                     else dict(self._latest))
        for name, value in sample.values.items():
            if value is None:
                candidate.pop(name, None)
            else:
                candidate[name] = (sample.observed_at, value)
        current = {name: value for name, (at, value) in candidate.items()
                   if sample.observed_at - at <= self.freshness}
        # Vehicle buses publish related properties on separate messages. Hold
        # incomplete GPS pairs and avoid combining a fresh charging state with
        # an older driving speed (or vice versa).
        if ("latitude" in current) != ("longitude" in current):
            current.pop("latitude", None)
            current.pop("longitude", None)
        if current.get("charging_power_kw") and not current.get("charging"):
            current.pop("charging_power_kw", None)
            candidate.pop("charging_power_kw", None)
        if current.get("charging") and (current.get("speed_kmh") or 0) > 1:
            if "charging" in sample.values and "speed_kmh" in sample.values:
                raise ValueError("simultaneous charging and driving signals conflict")
            if "charging" in sample.values:
                current.pop("speed_kmh", None)
                candidate.pop("speed_kmh", None)
            else:
                current.pop("charging", None)
                current.pop("charging_power_kw", None)
                candidate.pop("charging", None)
                candidate.pop("charging_power_kw", None)
        frame = SignalFrame.model_validate({
            "vehicle_id": self.vehicle_id,
            "sequence": self.next_sequence,
            "observed_at": sample.observed_at,
            "source": sample.source,
            **current,
        })
        self._latest = {name: pair for name, pair in candidate.items()
                        if sample.observed_at - pair[0] <= self.freshness}
        self._observed_at = sample.observed_at
        self._source = sample.source
        self.next_sequence += 1
        return frame


class FakeVehicleHardwareAdapter:
    def __init__(self, samples: Iterable[dict[str, object]]):
        self._samples = iter(samples)

    def read(self) -> HardwareSample:
        return HardwareSample("SIMULATOR", datetime.now(timezone.utc), next(self._samples))

    def close(self) -> None:
        pass


class ReplayVehicleHardwareAdapter:
    """Replay canonical JSONL frames with their original timestamps."""

    def __init__(self, path: str | Path):
        self._file = Path(path).open("r", encoding="utf-8")

    def read(self) -> HardwareSample:
        while line := self._file.readline():
            if not line.strip():
                continue
            record = SignalFrame.model_validate(json.loads(line))
            return HardwareSample("REPLAY", record.observed_at,
                                  record.model_dump(exclude={
                                      "schema_version", "vehicle_id", "sequence",
                                      "observed_at", "source",
                                  }, exclude_none=True))
        raise StopIteration

    def close(self) -> None:
        self._file.close()


@dataclass(frozen=True)
class CanSignalSpec:
    """Approved byte-aligned OEM mapping, with explicit engineering units."""

    arbitration_id: int
    field: str
    start_byte: int
    width_bytes: int
    scale: float = 1.0
    offset: float = 0.0
    byte_order: Literal["little", "big"] = "little"
    signed: bool = False

    def __post_init__(self) -> None:
        if not 0 <= self.arbitration_id <= 0x1FFFFFFF:
            raise ValueError("invalid CAN arbitration ID")
        if self.field not in SIGNAL_FIELDS - {"gear", "fault_code", "trip_id"}:
            raise ValueError("CAN mapping requires a supported numeric signal")
        if not 0 <= self.start_byte < 8 or self.width_bytes not in (1, 2, 4) or self.start_byte + self.width_bytes > 8:
            raise ValueError("CAN mapping must fit in an eight-byte frame")
        if not all(map(_finite, (self.scale, self.offset))) or self.scale == 0:
            raise ValueError("CAN conversion must be finite and nonzero")

    def decode(self, payload: bytes) -> float | bool:
        end = self.start_byte + self.width_bytes
        if len(payload) < end:
            raise ValueError("CAN payload is shorter than the approved mapping")
        raw = int.from_bytes(payload[self.start_byte:end], self.byte_order, signed=self.signed)
        value = raw * self.scale + self.offset
        if self.field in ("charging", "ignition"):
            if value not in (0, 1):
                raise ValueError("boolean CAN signal must decode to zero or one")
            return bool(value)
        return value


def _finite(value: float) -> bool:
    return value == value and value not in (float("inf"), float("-inf"))


class CanReadOnlyAdapter:
    """Decode injected SocketCAN frames; this interface has no send operation."""

    def __init__(self, receiver: ProtocolCanReceiver, mappings: Iterable[CanSignalSpec]):
        self.receiver = receiver
        self.mappings: dict[int, list[CanSignalSpec]] = {}
        for mapping in mappings:
            self.mappings.setdefault(mapping.arbitration_id, []).append(mapping)
        if not self.mappings:
            raise ValueError("an approved CAN mapping is required")

    def read(self) -> HardwareSample:
        while True:
            arbitration_id, payload, observed_at = self.receiver.receive()
            mappings = self.mappings.get(arbitration_id)
            if mappings is None:
                continue
            values = {mapping.field: mapping.decode(payload) for mapping in mappings}
            return HardwareSample("CAN", observed_at, values)

    def close(self) -> None:
        self.receiver.close()


class ProtocolCanReceiver(Protocol):
    def receive(self) -> tuple[int, bytes, datetime]: ...
    def close(self) -> None: ...


class SocketCanReceiver:
    """Linux SocketCAN receiver. Opens a raw socket and never transmits frames."""

    def __init__(self, interface: str):
        if not interface or len(interface) > 15 or not interface.replace("_", "").isalnum():
            raise ValueError("invalid CAN interface")
        if not hasattr(socket, "AF_CAN"):
            raise OSError("SocketCAN is unavailable on this host")
        self.socket = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
        try:
            self.socket.bind((interface,))
        except Exception:
            self.socket.close()
            raise

    def receive(self) -> tuple[int, bytes, datetime]:
        raw = self.socket.recv(16)
        if len(raw) != 16:
            raise ValueError("invalid SocketCAN frame length")
        can_id, data_length, payload = struct.unpack("=IB3x8s", raw)
        if can_id & 0x20000000:  # CAN_ERR_FLAG
            raise ValueError("SocketCAN error frame")
        if can_id & 0x40000000:  # CAN_RTR_FLAG
            raise ValueError("SocketCAN remote request is not telemetry")
        mask = 0x1FFFFFFF if can_id & 0x80000000 else 0x7FF
        if data_length > 8:
            raise ValueError("invalid classic CAN data length")
        return can_id & mask, payload[:data_length], datetime.now(timezone.utc)

    def close(self) -> None:
        self.socket.close()


_OBD_PIDS = {"010D": ("speed_kmh", lambda value: float(value))}


def decode_obd_response(pid: str, response: str) -> tuple[str, float]:
    """Decode standard read-only PIDs with unambiguous canonical meaning."""
    if pid not in _OBD_PIDS:
        raise ValueError("OBD PID is not on the read-only allowlist")
    normalized = response.upper().replace(" ", "").replace("\r", "").replace("\n", "")
    # ELM adapters may echo commands; search for the actual mode-01 response.
    marker = "41" + pid[2:]
    index = normalized.find(marker)
    if index < 0 or len(normalized) < index + len(marker) + 2:
        raise ValueError("OBD PID unavailable or malformed response")
    try:
        value = int(normalized[index + len(marker):index + len(marker) + 2], 16)
    except ValueError as exc:
        raise ValueError("OBD response is not hexadecimal") from exc
    field, convert = _OBD_PIDS[pid]
    return field, convert(value)


class ObdReadOnlyAdapter:
    """Poll only fixed mode-01 read PIDs through an injected ELM transport."""

    def __init__(self, transport: ProtocolObdTransport,
                 pids: tuple[str, ...] = ("010D",)):
        if not pids or any(pid not in _OBD_PIDS for pid in pids):
            raise ValueError("OBD polling supports only read-only allowlisted PIDs")
        self.transport = transport
        self.pids = pids

    def read(self) -> HardwareSample:
        values: dict[str, object] = {}
        for pid in self.pids:
            try:
                field, value = decode_obd_response(pid, self.transport.query(pid))
                values[field] = value
            except (OSError, TimeoutError, ValueError):
                continue  # unsupported PID must stay unknown
        if not values:
            raise ValueError("no supported OBD signals available")
        return HardwareSample("OBD", datetime.now(timezone.utc), values)

    def close(self) -> None:
        self.transport.close()


class ProtocolObdTransport(Protocol):
    def query(self, pid: str) -> str: ...
    def close(self) -> None: ...


class Elm327TcpTransport:
    """TCP ELM327 transport restricted to the PIDs allowed above."""

    def __init__(self, host: str, port: int, timeout: float = 2):
        if not host or not 1 <= port <= 65535 or not 0 < timeout <= 10:
            raise ValueError("invalid ELM327 connection")
        self.socket = socket.create_connection((host, port), timeout=timeout)
        self.socket.settimeout(timeout)

    def query(self, pid: str) -> str:
        if pid not in _OBD_PIDS:
            raise ValueError("OBD PID is not on the read-only allowlist")
        self.socket.sendall((pid + "\r").encode("ascii"))
        reply = bytearray()
        while len(reply) < 1024:
            chunk = self.socket.recv(1)
            if not chunk:
                raise ConnectionError("ELM327 disconnected")
            if chunk == b">":
                return reply.decode("ascii", errors="replace")
            reply.extend(chunk)
        raise ValueError("ELM327 response too large")

    def close(self) -> None:
        self.socket.close()
