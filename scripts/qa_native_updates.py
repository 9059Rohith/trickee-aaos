"""Verify foreground native inbox updates and credential revocation on a phone emulator."""

from __future__ import annotations

import hashlib
import json
import secrets
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from scripts import verify_native_driver as phone
from trickee.simulator import demo_profile
from trickee.store import CloudStore


def visible(fragment: str) -> bool:
    return any(fragment in node.attrib.get("text", "") for node in phone.hierarchy().iter("node"))


def scroll_to(fragment: str) -> None:
    for start, end in ((1820, 630), (630, 1820)):
        for _ in range(14):
            if visible(fragment):
                return
            phone.adb("shell", "input", "swipe", "540", str(start), "540", str(end), "450")
            time.sleep(0.2)
    raise AssertionError(f"native section not visible: {fragment}")


def main() -> None:
    if not phone.APK.is_file():
        raise FileNotFoundError("build the native driver debug APK first")
    if not phone.SERIAL.startswith("emulator-") or phone.adb("shell", "getprop", "ro.kernel.qemu") != "1":
        raise RuntimeError("native update QA clears app data and requires a phone emulator")
    if "automotive" in phone.adb("shell", "pm", "list", "features"):
        raise RuntimeError("native update QA requires a phone emulator")
    with tempfile.TemporaryDirectory(prefix="trickee-native-updates-") as directory:
        database = Path(directory) / "cloud.db"
        store = CloudStore(str(database))
        store.register(demo_profile(), "QA-NATIVE-UPDATES")
        token = secrets.token_hex(24)
        store.issue_driver_credential("TRICKEE-DEMO-1", hashlib.sha256(token.encode()).hexdigest())
        store.close()
        port = phone.free_port()
        origin = f"http://127.0.0.1:{port}"
        server = phone.start_server(database, port)
        try:
            phone.adb("shell", "am", "force-stop", phone.PACKAGE)
            phone.install_debug_apk()
            phone.adb("shell", "pm", "clear", phone.PACKAGE)
            phone.adb("shell", "am", "start", "-n", phone.PACKAGE + "/.MainActivity")
            phone.fill("HTTPS server origin", f"http://10.0.2.2:{port}")
            phone.fill("Driver credential", token)
            phone.tap("Connect")
            phone.wait_until(lambda: visible("TRICKEE-DEMO-1"), "native driver did not connect", 30)

            scroll_to("0 stored advisories")
            response = httpx.post(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/nudges",
                                  headers={"X-Operator-Key": "local-demo-operator-key"},
                                  json={"nudge_id": "NUDGE-LIVE-1", "idempotency_key": "nudge-live-1",
                                        "kind": "ROUTE_REVIEW", "message": "Review the new pickup route."})
            response.raise_for_status()
            phone.wait_until(lambda: visible("1 stored advisories"),
                             "native advisory did not update without manual refresh", 30)

            scroll_to("0 stored proposals")
            response = httpx.post(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/dispatch-proposals",
                                  headers={"X-Operator-Key": "local-demo-operator-key"},
                                  json={"offer_id": "OFFER-LIVE-1", "idempotency_key": "offer-live-1",
                                        "pickup": {"latitude": 12.9716, "longitude": 77.5946},
                                        "destination": {"latitude": 12.9916, "longitude": 77.6146},
                                        "pickup_label": "QA pickup", "destination_label": "QA destination",
                                        "message": "Review this test pickup.", "expires_in_minutes": 30})
            response.raise_for_status()
            phone.wait_until(lambda: visible("1 stored proposals"),
                             "native dispatch did not update without manual refresh", 30)

            operator_headers = {"X-Operator-Key": "local-demo-operator-key"}
            driver_headers = {"X-Driver-Key": token}
            response = httpx.post(origin + "/api/v1/driver/dispatch-proposals/OFFER-LIVE-1/respond",
                                  headers=driver_headers,
                                  json={"idempotency_key": "offer-live-accept", "outcome": "ACCEPTED"})
            response.raise_for_status()
            response = httpx.post(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/dispatch-proposals/OFFER-LIVE-1/assign",
                                  headers=operator_headers,
                                  json={"order_id": "ORDER-LIVE-1", "idempotency_key": "assign-live-1"})
            response.raise_for_status()
            scroll_to("stored assignments")
            phone.wait_until(lambda: visible("1 stored assignments"),
                             "native internal assignment did not update", 30)
            scroll_to("Report assignment pickup")
            phone.tap("Report assignment pickup")
            phone.wait_until(lambda: httpx.get(origin + "/api/v1/driver/internal-orders",
                                               headers=driver_headers).json()[0]["status"] == "PICKED_UP",
                             "native pickup report did not reach server", 30)
            scroll_to("Report assignment delivery")
            phone.tap("Report assignment delivery")
            phone.wait_until(lambda: httpx.get(origin + "/api/v1/driver/internal-orders",
                                               headers=driver_headers).json()[0]["status"] == "DELIVERED_REPORTED",
                             "native delivery report did not reach server", 30)
            response = httpx.post(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/internal-orders/ORDER-LIVE-1/reconcile",
                                  headers=operator_headers,
                                  json={"idempotency_key": "reconcile-live-1",
                                        "note": "Manually confirmed with QA driver"})
            response.raise_for_status()
            phone.wait_until(lambda: visible("RECONCILED"),
                             "native assignment reconciliation did not update", 30)

            scroll_to("0 stored cases")
            response = httpx.post(origin + "/api/v1/driver/service-cases",
                                  headers={"X-Driver-Key": token},
                                  json={"case_id": "CASE-LIVE-1", "idempotency_key": "case-live-1",
                                        "category": "CHARGER_PROBLEM", "priority": "URGENT",
                                        "title": "Connector unavailable",
                                        "description": "The test connector did not start charging."})
            response.raise_for_status()
            phone.wait_until(lambda: visible("1 stored cases"),
                             "native service case did not update without manual refresh", 30)

            phone.adb("shell", "input", "keyevent", "KEYCODE_HOME")
            response = httpx.post(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/nudges",
                                  headers={"X-Operator-Key": "local-demo-operator-key"},
                                  json={"nudge_id": "NUDGE-LIVE-2", "idempotency_key": "nudge-live-2",
                                        "kind": "ROUTE_REVIEW", "message": "Review the resumed route."})
            response.raise_for_status()
            phone.adb("shell", "am", "start", "-n", phone.PACKAGE + "/.MainActivity")
            scroll_to("stored advisories")
            phone.wait_until(lambda: visible("2 stored advisories"),
                             "native inbox did not catch up after app resume", 30)

            response = httpx.post(origin + "/api/v1/driver-credentials/D-1/revoke",
                                  headers={"X-Operator-Key": "local-demo-operator-key"})
            response.raise_for_status()

            def credential_cleared() -> bool:
                settings = phone.adb("exec-out", "run-as", phone.PACKAGE, "cat",
                                     "shared_prefs/driver_settings.xml")
                return "driver_credential_sealed" not in settings

            phone.wait_until(credential_cleared, "revoked native credential remained stored", 30)
            report = {
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "api_level": phone.adb("shell", "getprop", "ro.build.version.sdk"),
                "apk_sha256": hashlib.sha256(phone.APK.read_bytes()).hexdigest(),
                "advisory_updated_in_foreground": True,
                "dispatch_updated_in_foreground": True,
                "internal_assignment_pickup_delivery_reconciliation": True,
                "service_case_updated_in_foreground": True,
                "inbox_caught_up_after_resume": True,
                "revoked_credential_cleared": True,
            }
            phone.OUT.mkdir(parents=True, exist_ok=True)
            (phone.OUT / "updates-verification.json").write_text(
                json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2))
        finally:
            phone.stop_server(server)


if __name__ == "__main__":
    main()
