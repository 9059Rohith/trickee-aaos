"""Opt-in operator pull of OCPI 2.3.0 sessions and charge detail records."""

from __future__ import annotations

import json
import os
import re
import sys
from collections.abc import Mapping

import httpx

from .charging_sessions import ChargingSessionStore, sync_ocpi_records
from .ocpi import OcpiBadResponse, OcpiCollectionClient, OcpiUnavailable
from .store import CloudStore


_REQUIRED = ("TRICKEE_DB", "TRICKEE_OCPI_SESSIONS_URL",
             "TRICKEE_OCPI_CDRS_URL", "TRICKEE_OCPI_TOKEN", "TRICKEE_OCPI_TENANT_ID")


def run_sync(config: Mapping[str, str],
             transport: httpx.BaseTransport | None = None) -> dict:
    """Fetch configured partner collections and persist validated records.

    URL and token validation happens before opening the database. This command
    never starts a charge, settles a payment, or exports a provider credential.
    """
    for key in _REQUIRED:
        if not isinstance(config.get(key), str) or not config[key].strip():
            raise ValueError(f"{key} must be configured")
    if config["TRICKEE_DB"] == ":memory:":
        raise ValueError("TRICKEE_DB must be a durable file")
    if re.fullmatch(r"[A-Za-z0-9_-]{1,64}", config["TRICKEE_OCPI_TENANT_ID"]) is None:
        raise ValueError("TRICKEE_OCPI_TENANT_ID is invalid")
    sessions_client = OcpiCollectionClient(config["TRICKEE_OCPI_SESSIONS_URL"],
                                           config["TRICKEE_OCPI_TOKEN"], transport)
    cdrs_client = OcpiCollectionClient(config["TRICKEE_OCPI_CDRS_URL"],
                                       config["TRICKEE_OCPI_TOKEN"], transport)
    cloud = CloudStore(config["TRICKEE_DB"])
    try:
        return sync_ocpi_records(ChargingSessionStore(cloud), sessions_client, cdrs_client,
                                 config["TRICKEE_OCPI_TENANT_ID"])
    finally:
        cloud.close()


def main() -> int:
    try:
        print(json.dumps(run_sync(os.environ), sort_keys=True))
        return 0
    except (ValueError, OcpiBadResponse, OcpiUnavailable) as exc:
        # Exception messages from our adapter contain no URL or token. Avoid
        # printing an underlying HTTP exception, whose URL may be sensitive.
        print(f"OCPI sync failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
