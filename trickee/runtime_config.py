"""Fail-closed network deployment configuration and file-backed secrets."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit


FILE_SECRETS = (
    "TRICKEE_OPERATOR_KEY",
    "TRICKEE_VEHICLE_KEY",
    "TRICKEE_OPERATOR_ACCOUNTS_JSON",
    "TRICKEE_OIDC_SUBJECTS_JSON",
    "TRICKEE_OCPI_TOKEN",
    "TRICKEE_CHARGER_FEED_TOKEN",
    "TRICKEE_OPENWEATHER_API_KEY",
    "TRICKEE_ELEVATION_TOKEN",
    "TRICKEE_ORDER_FEED_TOKEN",
)


def prepared_environment(values: Mapping[str, str]) -> dict[str, str]:
    """Return a copy with *_FILE secrets resolved; never include secret data in errors."""
    result = dict(values)
    for name in FILE_SECRETS:
        path = result.pop(name + "_FILE", None)
        if path is None:
            continue
        if result.get(name):
            raise ValueError(f"{name} and {name}_FILE cannot both be set")
        if not path:
            raise ValueError(f"{name}_FILE must reference a readable file")
        try:
            raw = Path(path).read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"{name}_FILE must reference a readable UTF-8 file") from None
        secret = raw.removesuffix("\n").removesuffix("\r")
        if not secret or secret != secret.strip() or "\n" in secret or "\r" in secret:
            raise ValueError(f"{name}_FILE contains an invalid secret")
        result[name] = secret
    return result


def validate_deployment(values: Mapping[str, str]) -> None:
    """Reject staging/production configurations that would expose a demo or partial API."""
    mode = values.get("TRICKEE_DEPLOYMENT_MODE", "local")
    if mode not in ("local", "staging", "production"):
        raise ValueError("TRICKEE_DEPLOYMENT_MODE is invalid")
    if mode == "local":
        return
    if values.get("TRICKEE_DEMO_MODE") == "1":
        raise ValueError("demo mode is forbidden in network deployments")
    public_url = values.get("TRICKEE_PUBLIC_URL", "")
    parsed = urlsplit(public_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or parsed.path not in ("", "/")
            or parsed.hostname in ("localhost", "127.0.0.1", "::1")):
        raise ValueError("network deployment requires a public HTTPS origin")
    db_path = PurePosixPath(values.get("TRICKEE_DB", ""))
    if (not db_path.is_absolute() or ".." in db_path.parts
            or not db_path.is_relative_to(PurePosixPath("/app/data"))
            or db_path == PurePosixPath("/app/data")):
        raise ValueError("network database must use the persistent /app/data volume")
    vehicle_key = values.get("TRICKEE_VEHICLE_KEY", "")
    operator_key = values.get("TRICKEE_OPERATOR_KEY", "")
    if len(vehicle_key) < 32 or vehicle_key == operator_key or vehicle_key == "local-demo-vehicle-key":
        raise ValueError("network vehicle key is missing, weak or duplicated")
    if operator_key:
        if len(operator_key) < 32 or operator_key == "local-demo-operator-key":
            raise ValueError("network operator key is weak")
    elif not values.get("TRICKEE_OPERATOR_ACCOUNTS_JSON") and not values.get("TRICKEE_OIDC_ISSUER"):
        raise ValueError("network operator identity is not configured")
    if mode == "production" and (operator_key or not (
            values.get("TRICKEE_OPERATOR_ACCOUNTS_JSON") or values.get("TRICKEE_OIDC_ISSUER"))):
        raise ValueError("production requires scoped operator identity without a legacy global key")
    retention = values.get("TRICKEE_PHONE_GPS_RETENTION_DAYS", "")
    if not retention.isdecimal() or int(retention) < 1:
        raise ValueError("network phone GPS retention must be a positive number of days")
