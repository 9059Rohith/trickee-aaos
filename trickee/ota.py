"""Simulator-only signed update and rollback lifecycle."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from dataclasses import dataclass

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .domain import VehicleState


def _body(version: str, artifact: bytes) -> bytes:
    return json.dumps({"version": version, "sha256": hashlib.sha256(artifact).hexdigest()}, sort_keys=True, separators=(",", ":")).encode()


def sign_campaign(private_key: Ed25519PrivateKey, version: str, artifact: bytes) -> dict:
    body = _body(version, artifact)
    return {"version": version, "artifact_b64": base64.b64encode(artifact).decode(), "sha256": hashlib.sha256(artifact).hexdigest(), "signature_b64": base64.b64encode(private_key.sign(body)).decode(), "mode": "SIMULATOR_ONLY"}


def validate_simulator_manifest(campaign: dict) -> None:
    """Check envelope integrity before storage; signature trust is vehicle-side."""
    if not isinstance(campaign, dict) or campaign.get("mode") != "SIMULATOR_ONLY":
        raise ValueError("only simulator OTA manifests are supported")
    version = campaign.get("version")
    if not isinstance(version, str) or re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", version) is None:
        raise ValueError("invalid simulator OTA version")
    try:
        artifact = base64.b64decode(campaign["artifact_b64"], validate=True)
        signature = base64.b64decode(campaign["signature_b64"], validate=True)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("invalid simulator OTA encoding") from exc
    if not 0 < len(artifact) <= 5 * 1024 * 1024 or len(signature) != 64:
        raise ValueError("invalid simulator OTA artifact or signature size")
    if hashlib.sha256(artifact).hexdigest() != campaign.get("sha256"):
        raise ValueError("simulator OTA artifact hash mismatch")


@dataclass
class SimulatorUpdater:
    public_key: Ed25519PublicKey
    version: str = "sim-1"
    last_result: str = "IDLE"

    def apply(self, campaign: dict, state: VehicleState, fail_after_install: bool = False) -> str:
        if state not in (VehicleState.PARKED, VehicleState.CHARGING_COMPLETE):
            self.last_result = "BLOCKED_VEHICLE_STATE"
            return self.last_result
        try:
            validate_simulator_manifest(campaign)
            artifact = base64.b64decode(campaign["artifact_b64"], validate=True)
            self.public_key.verify(base64.b64decode(campaign["signature_b64"], validate=True), _body(campaign["version"], artifact))
        except (KeyError, TypeError, ValueError, InvalidSignature):
            self.last_result = "REJECTED_SIGNATURE"
            return self.last_result
        old_version = self.version
        self.version = campaign["version"]
        if fail_after_install:
            self.version = old_version
            self.last_result = "ROLLED_BACK"
        else:
            self.last_result = "INSTALLED"
        return self.last_result
