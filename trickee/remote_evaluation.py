"""Opt-in remote evaluation bootstrap; free-host SQLite storage can be reset."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import os

from .runtime_config import validate_deployment
from .simulator import demo_profile, scenario_frame
from .store import CloudStore


def evaluation_environment(values: Mapping[str, str]) -> dict[str, str]:
    if values.get("TRICKEE_EVALUATION_MODE") != "1":
        raise ValueError("remote evaluation requires explicit opt-in")
    if values.get("TRICKEE_DEPLOYMENT_MODE", "staging") != "staging":
        raise ValueError("remote evaluation must use staging mode")
    if values.get("TRICKEE_OPERATOR_KEY") or values.get("TRICKEE_OPERATOR_KEY_FILE"):
        raise ValueError("remote evaluation requires scoped operator access")
    if values.get("TRICKEE_DEMO_MODE", "0") != "0":
        raise ValueError("remote evaluation forbids demo authentication")
    names = ("TRICKEE_EVALUATION_OPERATOR_KEY", "TRICKEE_EVALUATION_DRIVER_KEY", "TRICKEE_VEHICLE_KEY")
    keys = [values.get(name, "") for name in names]
    if any(len(key) < 32 or key != key.strip() or any(char.isspace() for char in key)
           for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("evaluation credentials must be distinct secrets of at least 32 characters")
    result = dict(values)
    result.update({
        "TRICKEE_DEPLOYMENT_MODE": "staging",
        "TRICKEE_DEMO_MODE": "0",
        "TRICKEE_PUBLIC_URL": values.get("TRICKEE_PUBLIC_URL") or values.get("RENDER_EXTERNAL_URL", ""),
        "TRICKEE_DB": values.get("TRICKEE_DB", "/app/data/evaluation.db"),
        "TRICKEE_PHONE_GPS_RETENTION_DAYS": values.get("TRICKEE_PHONE_GPS_RETENTION_DAYS", "7"),
        "TRICKEE_OPERATOR_ACCOUNTS_JSON": json.dumps([{
            "tenant_id": "evaluation", "role": "admin", "actor_id": "evaluation-admin",
            "key_sha256": hashlib.sha256(keys[0].encode()).hexdigest(),
        }]),
    })
    validate_deployment(result)
    return result


def seed_evaluation(database: str, driver_key: str) -> None:
    profile = demo_profile()
    driver_id = "EVALUATION-DRIVER"
    token_hash = hashlib.sha256(driver_key.encode()).hexdigest()
    store = CloudStore(database)
    try:
        vehicle = store.db.execute("SELECT driver_id,tenant_id FROM vehicles WHERE vehicle_id=?",
                                   (profile.vehicle_id,)).fetchone()
        if vehicle is not None:
            if vehicle["driver_id"] != driver_id or vehicle["tenant_id"] != "evaluation":
                raise ValueError("existing evaluation vehicle assignment must not be overwritten")
            identity = store.driver_by_hash(token_hash)
            if identity is not None and identity["vehicle_id"] == profile.vehicle_id:
                return
            if store.driver_credentials(profile.vehicle_id):
                raise ValueError("evaluation driver credential was changed or revoked; review before restarting")
        else:
            store.register(profile, driver_id, "evaluation")
        # Recover an interrupted first bootstrap, without resurrecting credentials
        # that an operator revoked or replacing an existing assignment.
        if store.db.execute("SELECT 1 FROM events WHERE vehicle_id=? LIMIT 1",
                            (profile.vehicle_id,)).fetchone() is None:
            store.ingest([scenario_frame("SCENARIO_005", 1)])
        store.issue_driver_credential(profile.vehicle_id, token_hash)
    finally:
        store.close()


def main() -> None:
    prepared = evaluation_environment(os.environ)
    port = int(os.getenv("PORT", "8000"))
    if not 1 <= port <= 65535:
        raise ValueError("evaluation server port is invalid")
    os.environ.update(prepared)
    seed_evaluation(prepared["TRICKEE_DB"], prepared["TRICKEE_EVALUATION_DRIVER_KEY"])
    import uvicorn
    from .api import app

    print("REMOTE EVALUATION: synthetic vehicle; free-host data can reset after sleep/restart.", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
