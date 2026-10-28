"""Repeatable local Android phone -> driver API integration check (debug APK only)."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
import xml.etree.ElementTree as ET

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trickee.simulator import demo_profile
from trickee.store import CloudStore
from scripts.verify_application import _stop_tree

ADB = os.getenv("ADB", "adb")
SERIAL = os.getenv("TRICKEE_PHONE_SERIAL", "emulator-5556")
APK = ROOT / "android-app/driver/build/outputs/apk/debug/driver-debug.apk"
OUT = (Path(os.environ["TRICKEE_QA_OUTPUT"]) / "native-driver-evidence"
       if os.getenv("TRICKEE_QA_OUTPUT") else ROOT / "docs/native-driver-evidence")
PACKAGE = "com.trickee.driver"


def adb(*args: str, binary: bool = False) -> str | bytes:
    result = subprocess.run([ADB, "-s", SERIAL, *args], check=True, capture_output=True,
                            timeout=150 if args and args[0] == "install" else 45)
    return result.stdout if binary else result.stdout.decode("utf-8", errors="replace").strip()


def install_debug_apk() -> None:
    """Replace a QA emulator install even when its old debug signer differs."""
    try:
        paths = adb("shell", "pm", "path", PACKAGE).splitlines()
        if len(paths) == 1 and paths[0].startswith("package:"):
            installed = adb("exec-out", "cat", paths[0][len("package:"):], binary=True)
            if hashlib.sha256(installed).digest() == hashlib.sha256(APK.read_bytes()).digest():
                return
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        pass
    try:
        adb("install", "-r", str(APK))
    except subprocess.CalledProcessError as error:
        details = (error.stdout or b"") + (error.stderr or b"")
        if b"not enough space" in details:
            paths = adb("shell", "pm", "path", PACKAGE).splitlines()
            if len(paths) == 1 and paths[0].startswith("package:"):
                installed = adb("exec-out", "cat", paths[0][len("package:"):], binary=True)
                if hashlib.sha256(installed).digest() == hashlib.sha256(APK.read_bytes()).digest():
                    return
            raise
        if b"INSTALL_FAILED_UPDATE_INCOMPATIBLE" not in details:
            raise
        adb("uninstall", PACKAGE)
        adb("install", "-r", str(APK))


def hierarchy() -> ET.Element:
    for _ in range(5):
        try:
            result = adb("shell", "uiautomator", "dump", "/sdcard/trickee-driver-ui.xml")
            if "dumped" in result:
                return ET.fromstring(adb("shell", "cat", "/sdcard/trickee-driver-ui.xml"))
        except (subprocess.CalledProcessError, ET.ParseError):
            pass
        time.sleep(1)
    raise RuntimeError("phone UI hierarchy unavailable")


def tap(label: str, *, exact: bool = True) -> None:
    if label == "Sync queued GPS" and "mInputShown=true" in adb("shell", "dumpsys", "input_method"):
        adb("shell", "input", "keyevent", "KEYCODE_BACK")
        time.sleep(0.3)
    # Slow, shorter swipes avoid fling-overshooting a control on the emulator.
    directions = [(1850, 1100)] * 12 + [(1200, 1750)] * 20
    for step in range(len(directions) + 1):
        focused_window = re.search(r"mCurrentFocus=([^\n]+)", adb("shell", "dumpsys", "window"))
        if focused_window and "NotificationShade" in focused_window.group(1):
            adb("shell", "input", "keyevent", "KEYCODE_BACK")
            time.sleep(0.2)
        target_y = None
        for node in hierarchy().iter("node"):
            text = node.attrib.get("text", "") or node.attrib.get("hint", "")
            if (text.casefold() == label.casefold() if exact else label.casefold() in text.casefold()) and node.attrib.get("enabled") == "true":
                x1, y1, x2, y2 = map(int, re.findall(r"\d+", node.attrib["bounds"]))
                if 180 <= (y1 + y2) // 2 <= 2050:
                    time.sleep(0.25)  # Let a just-finished scroll/layout settle before tapping.
                    print(f"TAP {label} at {(x1 + x2) // 2},{(y1 + y2) // 2}", flush=True)
                    adb("shell", "input", "tap", str((x1 + x2) // 2), str((y1 + y2) // 2))
                    return
                target_y = (y1 + y2) // 2
        if step < len(directions):
            start, end = ((1200, 1750) if target_y is not None and target_y < 180
                          else (1850, 1100) if target_y is not None and target_y > 2050
                          else directions[step])
            adb("shell", "input", "swipe", "540", str(start), "540", str(end), "600")
            time.sleep(0.3)
    matches = [(node.attrib.get("text", ""), node.attrib.get("enabled", ""), node.attrib.get("bounds", ""))
               for node in hierarchy().iter("node") if label in node.attrib.get("text", "")]
    raise AssertionError(f"enabled UI control not found: {label}; matching nodes: {matches}")


def fill(label: str, value: str) -> None:
    tap(label)
    wait_until(lambda: any(node.attrib.get("hint") == label and node.attrib.get("focused") == "true"
                           for node in hierarchy().iter("node")),
               f"input not focused: {label}", 10)
    time.sleep(0.4)
    if label == "Driver credential":
        for index in range(0, len(value), 8):
            adb("shell", "input", "text", value[index:index + 8])
    else:
        adb("shell", "input", "text", value)
    if label == "HTTPS server origin":
        def entered():
            return any(node.attrib.get("hint") == label and node.attrib.get("text") == value
                       for node in hierarchy().iter("node"))
        wait_until(entered, "server origin was not entered completely", 15)
    elif label == "Driver credential":
        def entered_secret():
            return any(node.attrib.get("hint") == label
                       and len(node.attrib.get("text", "")) == len(value)
                       for node in hierarchy().iter("node"))
        wait_until(entered_secret, "driver credential was not entered completely", 15)
    else:
        time.sleep(1)
    adb("shell", "input", "keyevent", "KEYCODE_BACK")


def wait_until(check, message: str, seconds: int = 35):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(1)
    raise AssertionError(message)


def queue_count() -> int:
    data = adb("exec-out", "run-as", PACKAGE, "cat", "databases/driver_gps_queue.db", binary=True)
    with tempfile.TemporaryDirectory(prefix="trickee-queue-read-") as directory:
        path = Path(directory) / "queue.db"
        path.write_bytes(data)
        connection = sqlite3.connect(path)
        try:
            return connection.execute("SELECT COUNT(*) FROM points").fetchone()[0]
        finally:
            connection.close()


def advisory_queue_xml() -> str:
    return adb("exec-out", "run-as", PACKAGE, "cat", "shared_prefs/advisory_reply_queue.xml")


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(database: Path, port: int, *, router: bool = True,
                 gps_reject: bool = False) -> subprocess.Popen:
    env = os.environ.copy()
    env.update({"TRICKEE_DB": str(database), "TRICKEE_DEMO_MODE": "1",
                "TRICKEE_QA_ROUTER": "on" if router else "off", "TRICKEE_OSRM_URL": "",
                "TRICKEE_QA_GPS_REJECT": "409" if gps_reject else ""})
    process = subprocess.Popen([sys.executable, "-m", "uvicorn", "scripts.native_qa_server:app", "--host", "127.0.0.1",
                                "--port", str(port)], cwd=ROOT, env=env, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    origin = f"http://127.0.0.1:{port}"
    def ready():
        if process.poll() is not None:
            raise RuntimeError("local driver API exited")
        try:
            return httpx.get(origin + "/health", timeout=1).status_code == 200
        except httpx.TransportError:
            return False
    try:
        wait_until(ready, "local driver API did not start")
    except BaseException:
        _stop_tree(process)
        raise
    return process


def stop_server(process: subprocess.Popen) -> None:
    _stop_tree(process)


def main() -> None:
    if not APK.is_file():
        raise FileNotFoundError(f"build the debug APK first: {APK}")
    if adb("shell", "getprop", "sys.boot_completed") != "1":
        raise RuntimeError("phone emulator is not booted")
    if not SERIAL.startswith("emulator-") or adb("shell", "getprop", "ro.kernel.qemu") != "1":
        raise RuntimeError("native QA clears app data and requires an Android emulator")
    if "automotive" in adb("shell", "pm", "list", "features"):
        raise RuntimeError("this check needs an Android phone emulator")
    with tempfile.TemporaryDirectory(prefix="trickee-native-driver-") as directory:
        database = Path(directory) / "qa.db"
        store = CloudStore(str(database))
        store.register(demo_profile(), "QA-NATIVE-DRIVER")
        token = secrets.token_hex(24)
        store.issue_driver_credential("TRICKEE-DEMO-1", hashlib.sha256(token.encode()).hexdigest())
        other_vehicle = "QA-NATIVE-SECOND"
        store.register(demo_profile().model_copy(update={"vehicle_id": other_vehicle}), "QA-NATIVE-OTHER")
        other_token = secrets.token_hex(24)
        store.issue_driver_credential(other_vehicle, hashlib.sha256(other_token.encode()).hexdigest())
        store.create_nudge("TRICKEE-DEMO-1", "N-NATIVE-QA", "nudge-native-qa", "ROUTE_REVIEW",
                           "Review the confirmed route before departure.", None)
        store.db.close()
        port = free_port()
        origin = f"http://127.0.0.1:{port}"
        emulator_origin = f"http://10.0.2.2:{port}"
        server = start_server(database, port)
        try:
            operator = {"X-Operator-Key": "local-demo-operator-key"}
            plan_url = origin + "/api/v1/vehicles/TRICKEE-DEMO-1/daily-plans"
            plan = {"plan_id": "DAY-NATIVE-QA", "idempotency_key": "draft-native-qa",
                    "service_date": (date.today() + timedelta(days=1)).isoformat(),
                    "timezone": "Asia/Kolkata", "starting_soc_pct": 80,
                    "origin": {"latitude": 12.9716, "longitude": 77.5946},
                    "stops": [{"label": "Office", "requested_arrival_local": "09:00",
                               "coordinates": {"latitude": 12.98, "longitude": 77.60}}]}
            httpx.post(plan_url, headers=operator, json=plan).raise_for_status()
            httpx.post(plan_url + "/DAY-NATIVE-QA/confirm", headers=operator,
                       json={"idempotency_key": "confirm-native-qa"}).raise_for_status()
            adb("shell", "am", "force-stop", PACKAGE)
            install_debug_apk()
            adb("shell", "pm", "clear", PACKAGE)
            adb("shell", "am", "start", "-n", PACKAGE + "/.MainActivity")
            fill("HTTPS server origin", emulator_origin)
            fill("Driver credential", token)
            tap("Connect")
            driver_headers = {"X-Driver-Key": token}
            wait_until(lambda: any(n.attrib.get("text") == "TRICKEE-DEMO-1"
                                   for n in hierarchy().iter("node")),
                       "native driver did not connect", 25)
            assert not httpx.get(origin + "/api/v1/driver/consent/phone-trip",
                                 headers={"X-Driver-Key": token}).json()["granted"]
            tap("Agree to trip GPS and motion")
            wait_until(lambda: httpx.get(origin + "/api/v1/driver/consent/phone-trip",
                                         headers={"X-Driver-Key": token}).json()["granted"],
                       "native trip phone-data consent was not recorded", 20)
            tap("Office", exact=False)
            tap("Preview road route to Office")
            tap("Route 2 ·", exact=False)
            tap("ROAD SHAPE / NORTH UP / NO MAP TILES")
            tap("Outer Ring Road", exact=False)
            OUT.mkdir(parents=True, exist_ok=True)
            adb("shell", "screencap", "-p", "/sdcard/trickee-route-preview.png")
            adb("pull", "/sdcard/trickee-route-preview.png", str(OUT / "route-preview.png"))
            print("PASS: provider route alternative and turn preview", flush=True)
            tap("Check chargers near plan origin")
            tap("SIMULATED chargers near plan origin", exact=False)
            tap("North Hub", exact=False)
            adb("shell", "screencap", "-p", "/sdcard/trickee-chargers.png")
            adb("pull", "/sdcard/trickee-chargers.png", str(OUT / "chargers.png"))
            print("PASS: provenance-labeled plan-origin chargers", flush=True)
            stop_server(server)
            server = None
            tap("Acknowledge")
            wait_until(lambda: "N-NATIVE-QA" in advisory_queue_xml()
                       and "ACKNOWLEDGED" in advisory_queue_xml(),
                       "offline advisory reply was not durable", 15)
            adb("shell", "am", "force-stop", PACKAGE)
            adb("shell", "am", "start", "-n", PACKAGE + "/.MainActivity")
            assert "N-NATIVE-QA" in advisory_queue_xml(), "app restart lost the advisory reply"
            tap("Disconnect")
            wait_until(lambda: any("Disconnected. Queued GPS, advisories, pickup decisions, assignment reports and service cases remain" in n.attrib.get("text", "")
                                   for n in hierarchy().iter("node")),
                       "offline disconnect did not complete before reconnecting a different driver", 25)
            server = start_server(database, port)
            fill("Driver credential", other_token)
            tap("Connect")
            wait_until(lambda: any(n.attrib.get("text") == other_vehicle for n in hierarchy().iter("node")),
                       "second driver did not connect during advisory replay check", 25)
            assert httpx.get(origin + "/api/v1/driver/nudges", headers=driver_headers).json()[0]["status"] == "PENDING", \
                "another driver's session replayed the queued advisory"
            assert "N-NATIVE-QA" in advisory_queue_xml(), "switching driver deleted queued advisory"
            fill("Driver credential", token)
            tap("Connect")
            wait_until(lambda: httpx.get(origin + "/api/v1/driver/nudges", headers=driver_headers).json()[0]["status"]
                       == "ACKNOWLEDGED", "native advisory acknowledgement did not replay", 35)
            wait_until(lambda: "N-NATIVE-QA" not in advisory_queue_xml(),
                       "confirmed advisory remained in local queue", 15)
            print("PASS: durable advisory outage, app restart, driver isolation and replay", flush=True)
            conflict = {"nudge_id": "N-NATIVE-CONFLICT", "idempotency_key": "nudge-native-conflict",
                        "kind": "ROUTE_REVIEW", "message": "Check the alternate route before leaving."}
            httpx.post(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/nudges",
                       headers=operator, json=conflict).raise_for_status()
            tap("Sync advisory replies")
            wait_until(lambda: any("Check the alternate route" in n.attrib.get("text", "")
                                   for n in hierarchy().iter("node")),
                       "second advisory not shown in native inbox", 25)
            stop_server(server)
            server = None
            tap("Decline")
            wait_until(lambda: "N-NATIVE-CONFLICT" in advisory_queue_xml(),
                       "conflicting offline advisory reply was not stored", 15)
            # The status label is above the scrolled inbox and absent from UIAutomator's visible tree.
            # Wait past the transport's 12-second read deadline before reintroducing the server.
            time.sleep(13)
            server = start_server(database, port)
            httpx.post(origin + "/api/v1/driver/nudges/N-NATIVE-CONFLICT/respond",
                       headers=driver_headers,
                       json={"idempotency_key": "server-first-conflict", "outcome": "ACKNOWLEDGED"}).raise_for_status()
            tap("Sync advisory replies")
            wait_until(lambda: any("NEEDS_REVIEW" in n.attrib.get("text", "")
                                   for n in hierarchy().iter("node")),
                       "conflicting server advisory outcome was not visible", 25)
            assert "N-NATIVE-CONFLICT" in advisory_queue_xml(), "server conflict deleted local choice"
            tap("Clear reviewed local reply")
            tap("Clear local choice")
            wait_until(lambda: "N-NATIVE-CONFLICT" not in advisory_queue_xml(),
                       "reviewed advisory choice did not clear", 15)
            assert next(n["status"] for n in httpx.get(origin + "/api/v1/driver/nudges", headers=driver_headers).json()
                        if n["nudge_id"] == "N-NATIVE-CONFLICT") == "ACKNOWLEDGED", \
                "clearing local choice changed server outcome"
            print("PASS: advisory conflict visible, retained and recoverable", flush=True)
            tap("Start trip")
            def trip():
                trips = httpx.get(origin + "/api/v1/driver/trips", headers=driver_headers).json()
                return trips[0] if trips else None
            def close_reported_stop(kind: str):
                tap("End reported stop")
                try:
                    wait_until(lambda: trip()["active_stop"] is None,
                               f"{kind} stop did not close on first action", 12)
                except AssertionError:
                    # The command is idempotent; retry after a transient missed tap or response.
                    print(f"RETRY: {kind} stop completion", flush=True)
                    tap("End reported stop")
                    wait_until(lambda: trip()["active_stop"] is None,
                               f"{kind} stop did not close", 35)
            wait_until(lambda: trip() and trip()["status"] == "ACTIVE", "native trip did not start")
            tap("Start phone GPS")
            wait_until(lambda: "permission" in (re.search(r"mCurrentFocus=([^\n]+)",
                       adb("shell", "dumpsys", "window")).group(1).lower()),
                       "location permission was not requested", 15)
            adb("shell", "input", "keyevent", "KEYCODE_BACK")
            time.sleep(1)
            assert "GpsTrackingService" not in adb("shell", "dumpsys", "activity", "services", PACKAGE), \
                "GPS started without a location grant"
            print("PASS: denied location did not start GPS", flush=True)
            for permission in ("android.permission.ACCESS_FINE_LOCATION", "android.permission.ACCESS_COARSE_LOCATION",
                               "android.permission.POST_NOTIFICATIONS"):
                adb("shell", "pm", "grant", PACKAGE, permission)
            adb("emu", "geo", "fix", "77.602000", "12.974000")
            tap("Preview from my phone to Office")
            tap("PROVIDER ROAD PREVIEW / current phone location", exact=False)
            print("PASS: current phone location route origin", flush=True)
            tap("Start guidance")
            adb("emu", "geo", "fix", "77.602000", "12.974000")
            wait_until(lambda: any("GUIDANCE / following provider route" in node.attrib.get("text", "")
                                   for node in hierarchy().iter("node")),
                       "foreground route guidance did not start", 25)
            tap("Stop guidance")
            wait_until(lambda: any("Guidance stopped" in node.attrib.get("text", "")
                                   for node in hierarchy().iter("node")),
                       "route guidance did not stop", 15)
            print("PASS: foreground route guidance start and stop", flush=True)
            tap("Start guidance")
            stop_server(server)
            server = start_server(database, port, router=False)
            for offset in (0.0, 0.001, 0.002, 0.003, 0.004):
                adb("emu", "geo", "fix", f"{77.620 + offset:.6f}", "12.990000")
                time.sleep(1.5)
                print("GUIDANCE STATE:", [node.attrib.get("text") for node in hierarchy().iter("node")
                      if "GUIDANCE" in node.attrib.get("text", "")], flush=True)
            wait_until(lambda: any("reroute unavailable" in node.attrib.get("text", "").lower()
                                   for node in hierarchy().iter("node")),
                       "provider outage during reroute did not show a safe fallback", 30)
            tap("Stop guidance")
            adb("emu", "geo", "fix", "77.623000", "12.990000")
            time.sleep(1.5)
            assert any("Guidance stopped" in node.attrib.get("text", "")
                       for node in hierarchy().iter("node")), "stopped guidance received a later GPS callback"
            stop_server(server)
            server = start_server(database, port)
            adb("emu", "geo", "fix", "77.602000", "12.974000")
            print("PASS: reroute outage fallback and stopped listener", flush=True)
            tap("Find chargers near my phone")
            tap("SIMULATED chargers near phone location", exact=False)
            print("PASS: one-shot current-phone-location charger search", flush=True)
            tap("Start phone GPS")
            wait_until(lambda: "isForeground=true" in adb("shell", "dumpsys", "activity", "services", PACKAGE),
                       "phone GPS foreground service did not start", 20)
            time.sleep(3)
            adb("shell", "input", "keyevent", "KEYCODE_HOME")
            adb("emu", "sensor", "set", "acceleration", "0:0:9.8")
            offset = (int(time.time()) % 1000) / 100000
            adb("emu", "geo", "fix", f"{77.63 + offset:.6f}", f"{12.96 + offset:.6f}")
            wait_until(lambda: trip()["phone_gps_sample_count"] >= 1
                       and trip()["phone_imu_window_count"] >= 1,
                       "background phone GPS and IMU did not reach cloud", 55)
            print("PASS: background GPS and IMU upload", flush=True)
            adb("shell", "am", "start", "-n", PACKAGE + "/.MainActivity")
            time.sleep(2)  # Give a recreated activity time to read its sealed credential.
            before = trip()["phone_gps_sample_count"]
            stop_server(server)
            server = None
            adb("emu", "geo", "fix", f"{77.65 + offset:.6f}", f"{12.98 + offset:.6f}")
            wait_until(lambda: queue_count() >= 1, "offline GPS point was not durable", 25)
            server = start_server(database, port, gps_reject=True)
            tap("Sync queued GPS")
            wait_until(lambda: any("GPS upload blocked: HTTP 409" in node.attrib.get("text", "")
                                   for node in hierarchy().iter("node")),
                       "GPS conflict was not visible to the driver", 30)
            assert queue_count() >= 1, "GPS conflict deleted queued points"
            print("PASS: HTTP 409 is visible and preserves queued GPS", flush=True)
            tap("Withdraw phone data consent")
            wait_until(lambda: not httpx.get(origin + "/api/v1/driver/consent/phone-trip",
                                             headers={"X-Driver-Key": token}).json()["granted"],
                       "native consent withdrawal was not recorded", 20)
            assert queue_count() >= 1, "withdrawal deleted queued GPS"
            tap("Agree to trip GPS and motion")
            wait_until(lambda: httpx.get(origin + "/api/v1/driver/consent/phone-trip",
                                         headers={"X-Driver-Key": token}).json()["granted"],
                       "native consent regrant was not recorded", 20)
            print("PASS: consent withdrawal retained queued GPS and regrant", flush=True)
            fill("Driver credential", other_token)
            tap("Connect")
            wait_until(lambda: any(n.attrib.get("text") == other_vehicle for n in hierarchy().iter("node")),
                       "second driver did not connect", 25)
            tap("Agree to trip GPS and motion")
            wait_until(lambda: httpx.get(origin + "/api/v1/driver/consent/phone-trip",
                                         headers={"X-Driver-Key": other_token}).json()["granted"],
                       "second driver's consent was not independently recorded", 20)
            tap("Sync queued GPS")
            wait_until(lambda: any("different driver assignment" in node.attrib.get("text", "")
                                   for node in hierarchy().iter("node")),
                       "different-assignment queue warning missing", 30)
            assert queue_count() >= 1, "switching drivers deleted queued GPS"
            print("PASS: different driver assignment is visible and retains queued GPS", flush=True)
            fill("Driver credential", token)
            tap("Connect")
            wait_until(lambda: any(n.attrib.get("text") == "TRICKEE-DEMO-1" for n in hierarchy().iter("node")),
                       "original driver did not reconnect", 25)
            stop_server(server)
            server = start_server(database, port)
            tap("Sync queued GPS")
            wait_until(lambda: trip()["phone_gps_sample_count"] > before and queue_count() == 0,
                       "offline GPS queue did not replay", 55)
            wait_until(lambda: not any("GPS upload blocked" in node.attrib.get("text", "")
                                       for node in hierarchy().iter("node")),
                       "GPS conflict did not clear after successful replay", 15)
            print("PASS: durable offline GPS replay", flush=True)
            tap("Start waiting stop")
            wait_until(lambda: trip()["active_stop"] and trip()["active_stop"]["kind"] == "WAITING",
                       "waiting stop not stored")
            close_reported_stop("waiting")
            print("PASS: waiting stop lifecycle", flush=True)
            tap("Report charging stop")
            wait_until(lambda: trip()["active_stop"] and trip()["active_stop"]["kind"] == "CHARGING",
                       "charging stop not stored")
            close_reported_stop("charging")
            print("PASS: charging stop lifecycle", flush=True)
            fill("Ending dashboard charge % (optional)", "65")
            tap("Complete trip")
            wait_until(lambda: trip()["status"] == "COMPLETED", "native trip did not complete")
            completed = trip()
            assert completed["ending_soc_pct"] == 65
            assert completed["ending_soc_source"] == "MANUAL_DASHBOARD"
            assert completed["phone_imu_window_count"] >= 1
            assert [stop["kind"] for stop in completed["stops"]] == ["WAITING", "CHARGING"]
            assert all(stop["status"] == "COMPLETED" for stop in completed["stops"])
            assert "GpsTrackingService" not in adb("shell", "dumpsys", "activity", "services", PACKAGE)
            tap("RECENT TRIPS", exact=False)
            assert any(completed["trip_id"] in node.attrib.get("text", "")
                       for node in hierarchy().iter("node")), "completed trip missing from native history"
            OUT.mkdir(parents=True, exist_ok=True)
            adb("shell", "screencap", "-p", "/sdcard/trickee-native-driver.png")
            adb("pull", "/sdcard/trickee-native-driver.png", str(OUT / "completed-trip.png"))
            stop_server(server)
            server = start_server(database, port, router=False)
            tap("Preview road route to Office")
            tap("Road routing is unavailable", exact=False)
            print("PASS: unavailable route provider state", flush=True)
            tap("Check chargers near plan origin")
            tap("Charger availability stale", exact=False)
            print("PASS: stale charger feed state", flush=True)
            tap("Erase phone data")
            tap("Erase cloud and local data")
            wait_until(lambda: httpx.get(origin + "/api/v1/driver/phone-data/export",
                                         headers=driver_headers).json()["total"] == 0,
                       "native erasure did not clear cloud phone samples")
            wait_until(lambda: queue_count() == 0,
                       "native erasure did not clear local queued phone samples")
            assert not httpx.get(origin + "/api/v1/driver/consent/phone-trip",
                                 headers=driver_headers).json()["granted"]
            tap("Open directions in Maps")
            def map_handler_focused():
                focused = re.search(r"mCurrentFocus=([^\n]+)", adb("shell", "dumpsys", "window"))
                return focused is not None and PACKAGE not in focused.group(1)
            wait_until(map_handler_focused,
                       "native directions did not hand off to a map handler", 15)
            report = {"api_level": adb("shell", "getprop", "ro.build.version.sdk"),
                      "apk_sha256": hashlib.sha256(APK.read_bytes()).hexdigest(),
                      "trip_status": completed["status"], "phone_gps_samples": completed["phone_gps_sample_count"],
                      "phone_imu_windows": completed["phone_imu_window_count"],
                      "phone_imu_accelerometer_samples": completed["phone_imu_accelerometer_samples"],
                      "phone_imu_gyroscope_samples": completed["phone_imu_gyroscope_samples"],
                      "offline_queue_replayed": True,
                      "advisory_outage_restart_isolation_replay": True,
                      "advisory_conflict_retained_and_recovered": True,
                      "gps_409_visible_and_points_retained": True,
                      "gps_409_cleared_after_replay": True,
                      "driver_assignment_mismatch_visible_and_retained": True,
                      "stops": [stop["kind"] for stop in completed["stops"]],
                      "ending_soc_source": completed["ending_soc_source"], "gps_service_stopped": True,
                      "permission_denial_prevented_tracking": True,
                      "provider_route_alternative_previewed": True,
                      "provider_route_shape_previewed": True,
                      "current_phone_location_route_previewed": True,
                      "foreground_route_guidance_started_and_stopped": True,
                      "foreground_route_reroute_outage_visible": True,
                      "stopped_guidance_ignored_later_fix": True,
                      "trip_phone_consent_granted_withdrawn_regranted": True,
                      "native_phone_data_erasure": True,
                      "withdrawal_kept_queued_gps": True,
                      "second_driver_consent_isolated": True,
                      "route_provider_unavailable_state": True,
                      "plan_origin_chargers_labeled_simulated": True,
                      "phone_location_chargers_labeled_simulated": True,
                      "stale_charger_feed_state": True,
                      "confirmed_schedule_shown": True, "advisory_acknowledged": True,
                      "maps_handoff": True, "recent_history_shown": True}
            (OUT / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(report, indent=2))
        finally:
            if server is not None:
                stop_server(server)
            try:
                adb("shell", "am", "force-stop", PACKAGE)
            except subprocess.CalledProcessError:
                pass


if __name__ == "__main__":
    main()
