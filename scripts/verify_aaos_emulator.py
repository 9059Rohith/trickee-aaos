"""Run a debug-only Car API -> outbox -> cloud smoke on an AAOS emulator."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trickee.simulator import demo_profile


BASE = os.getenv("TRICKEE_EMULATOR_CLOUD_URL", "http://127.0.0.1:8002").rstrip("/")
DEVICE_HOST = os.getenv("TRICKEE_EMULATOR_DEVICE_HOST", "10.0.2.2")
KEY = os.getenv("TRICKEE_EMULATOR_OPERATOR_KEY", "local-demo-operator-key")
ADB = os.getenv("ADB", str(Path.home() / "AppData/Local/Android/Sdk/platform-tools/adb.exe"))
OUT = (Path(os.environ["TRICKEE_QA_OUTPUT"]) / "aaos-evidence"
       if os.getenv("TRICKEE_QA_OUTPUT") else ROOT / "docs" / "aaos-evidence")


def adb(*args: str) -> str:
    return subprocess.run([ADB, *args], check=True, capture_output=True, text=True, timeout=40).stdout.strip()


def hierarchy() -> ET.Element:
    adb("shell", "uiautomator", "dump", "/sdcard/trickee-ui.xml")
    return ET.fromstring(adb("shell", "cat", "/sdcard/trickee-ui.xml"))


def center(bounds: str) -> tuple[str, str]:
    x1, y1, x2, y2 = map(int, re.findall(r"\d+", bounds))
    return str((x1 + x2) // 2), str((y1 + y2) // 2)


def tap_text(label: str) -> bool:
    for node in hierarchy().iter("node"):
        if label in node.attrib.get("text", ""):
            adb("shell", "input", "tap", *center(node.attrib["bounds"]))
            return True
    return False


def fill(label: str, value: str) -> None:
    if not tap_text(label):
        raise AssertionError(f"field not visible: {label}")
    adb("shell", "input", "text", value)
    adb("shell", "input", "keyevent", "KEYCODE_BACK")


def main() -> None:
    parsed = urlparse(BASE)
    if parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("emulator verification accepts loopback cloud services only")
    if DEVICE_HOST not in {"10.0.2.2", "127.0.0.1"}:
        raise ValueError("emulator device host must be the emulator bridge or adb-reversed loopback")
    if DEVICE_HOST == "127.0.0.1":
        adb("reverse", f"tcp:{parsed.port or 80}", f"tcp:{parsed.port or 80}")
    if adb("shell", "getprop", "sys.boot_completed") != "1":
        raise RuntimeError("emulator has not completed boot")
    if "automotive" not in adb("shell", "pm", "list", "features"):
        raise RuntimeError("connected device is not AAOS")
    user = adb("shell", "am", "get-current-user")
    adb("shell", "pm", "clear", "--user", user, "com.trickee.vehicleos")
    for permission in ("android.car.permission.CAR_ENERGY", "android.car.permission.CAR_SPEED"):
        adb("shell", "pm", "grant", "--user", user, "com.trickee.vehicleos", permission)
    adb("shell", "am", "start", "-n", "com.trickee.vehicleos/.MainActivity")

    vehicle_id = f"TRICKEE-EMU-{int(time.time())}"
    profile = demo_profile(vehicle_id)
    with httpx.Client(base_url=BASE, headers={"X-Operator-Key": KEY}, timeout=10) as cloud:
        registered = cloud.post("/api/v1/vehicles", json={"profile": profile.model_dump()})
        registered.raise_for_status()
        ingest_key = registered.json()["ingest_key"]
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if tap_text("Vehicle ID"):
                adb("shell", "input", "text", vehicle_id)
                adb("shell", "input", "keyevent", "KEYCODE_BACK")
                break
            time.sleep(1)
        else:
            raise AssertionError("parked setup did not become available")
        fill("Cloud URL", f"http://{DEVICE_HOST}:{parsed.port or 80}")
        adb("shell", "input", "swipe", "540", "490", "540", "185", "400")
        fill("Vehicle ingest key", ingest_key)
        adb("shell", "input", "swipe", "540", "490", "540", "185", "400")
        assert tap_text("Save connection"), "save button not visible"
        deadline = time.monotonic() + 45
        frames = []
        while time.monotonic() < deadline:
            response = cloud.get(f"/api/v1/vehicles/{vehicle_id}/events")
            response.raise_for_status()
            frames = response.json()
            if frames:
                break
            time.sleep(2)
        assert frames, "no Car API frame reached the cloud"
        assert frames[0]["frame"]["source"] == "CAR_API"
        twin = cloud.get(f"/api/v1/vehicles/{vehicle_id}/twin")
        twin.raise_for_status()
        assert twin.json()["last_sequence"] >= 1
    adb("root")  # AAOS emulator userdebug only; verify storage without printing the secret.
    adb("wait-for-device")
    if DEVICE_HOST == "127.0.0.1":
        # `adb root` restarts adbd and drops the reverse tunnel.
        adb("reverse", f"tcp:{parsed.port or 80}", f"tcp:{parsed.port or 80}")
    prefs = adb("shell", "cat", f"/data/user/{user}/com.trickee.vehicleos/shared_prefs/trickee.xml")
    assert "vehicle_key_sealed" in prefs
    assert "vehicle_key\"" not in prefs
    assert ingest_key not in prefs
    OUT.mkdir(parents=True, exist_ok=True)
    vhal = "android.hardware.automotive.vehicle.IVehicle/default"
    adb("shell", "dumpsys", vhal, "--save-prop", "EV_BATTERY_LEVEL")
    battery_frame = False
    try:
        # The stock AAOS emulator reports 150000 Wh of usable capacity.
        adb("shell", "dumpsys", vhal, "--set", "EV_BATTERY_LEVEL", "-f", "96000")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            labels = [node.attrib.get("text", "") for node in hierarchy().iter("node")]
            if any("BATTERY  64%" in label or "BATTERY 64%" in label for label in labels):
                break
            time.sleep(2)
        else:
            raise AssertionError("battery VHAL update did not reach the UI")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            response = httpx.get(f"{BASE}/api/v1/vehicles/{vehicle_id}/events", headers={"X-Operator-Key": KEY}, timeout=10)
            response.raise_for_status()
            if any(abs(event["frame"].get("soc_pct", -1) - 64) < 0.5 for event in response.json()):
                battery_frame = True
                break
            time.sleep(2)
        assert battery_frame, "battery Car API frame did not reach cloud"
    finally:
        adb("shell", "dumpsys", vhal, "--restore-prop", "EV_BATTERY_LEVEL")
    adb("shell", "dumpsys", vhal, "--save-prop", "PERF_VEHICLE_SPEED")
    moving_frame = False
    try:
        adb("shell", "dumpsys", vhal, "--set", "PERF_VEHICLE_SPEED", "-f", "10")
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            labels = [node.attrib.get("text", "") for node in hierarchy().iter("node")]
            if any("SPEED  36 km/h" in label or "SPEED 36 km/h" in label for label in labels):
                assert any("SETUP LOCKED" in label for label in labels)
                assert not any(label == "PARKED SETUP" for label in labels)
                break
            time.sleep(2)
        else:
            raise AssertionError("moving speed did not lock setup")
        adb("shell", "screencap", "-p", "/sdcard/trickee-moving.png")
        adb("pull", "/sdcard/trickee-moving.png", str(OUT / "moving.png"))
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            response = httpx.get(f"{BASE}/api/v1/vehicles/{vehicle_id}/events", headers={"X-Operator-Key": KEY}, timeout=10)
            response.raise_for_status()
            if any(event["frame"].get("speed_kmh", 0) >= 30 for event in response.json()):
                moving_frame = True
                break
            time.sleep(2)
        assert moving_frame, "moving Car API frame did not reach cloud"
    finally:
        adb("shell", "dumpsys", vhal, "--restore-prop", "PERF_VEHICLE_SPEED")
    report = {"api_level": adb("shell", "getprop", "ro.build.version.sdk"),
              "vehicle_id": vehicle_id, "last_sequence": twin.json()["last_sequence"],
              "source": "CAR_API", "sealed_key": True,
              "battery_64_pct_uploaded": battery_frame,
              "moving_setup_locked": True, "moving_frame_uploaded": moving_frame}
    (OUT / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
