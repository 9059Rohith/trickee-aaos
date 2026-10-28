"""Verify nonsecret driver drafts survive rotation without retaining a typed credential."""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.verify_native_driver import APK, OUT, PACKAGE, SERIAL, adb, fill, hierarchy, install_debug_apk, wait_until


def field_text(hint: str) -> str:
    directions = ((1850, 350), (350, 2200)) if hint.startswith("Starting") \
        else ((350, 2200), (1850, 350))
    for start, end in directions:
        for _ in range(10):
            for node in hierarchy().iter("node"):
                if node.attrib.get("hint") == hint:
                    return node.attrib.get("text", "")
            adb("shell", "input", "swipe", "540", str(start), "540", str(end), "350")
    raise AssertionError(f"field not found after scrolling: {hint}")


def rotated() -> bool:
    return "mDisplayRotation=ROTATION_90" in adb("shell", "dumpsys", "window")


def restore_setting(name: str, value: str) -> None:
    if value in ("", "null"):
        adb("shell", "settings", "delete", "system", name)
    else:
        adb("shell", "settings", "put", "system", name, value)


def main() -> None:
    if not APK.is_file():
        raise FileNotFoundError(f"build the driver debug APK first: {APK}")
    if not SERIAL.startswith("emulator-") or adb("shell", "getprop", "ro.kernel.qemu") != "1":
        raise RuntimeError("draft restoration QA clears app data and requires an Android emulator")
    if "automotive" in adb("shell", "pm", "list", "features"):
        raise RuntimeError("draft restoration QA requires a phone emulator")
    original_auto = adb("shell", "settings", "get", "system", "accelerometer_rotation")
    original_rotation = adb("shell", "settings", "get", "system", "user_rotation")
    try:
        adb("shell", "am", "force-stop", PACKAGE)
        install_debug_apk()
        adb("shell", "pm", "clear", PACKAGE)
        adb("shell", "am", "start", "-n", PACKAGE + "/.MainActivity")
        fill("HTTPS server origin", "https://example.com")
        fill("Starting dashboard charge % (optional)", "73")
        fill("Driver credential", "unsubmitted-secret")
        adb("shell", "settings", "put", "system", "accelerometer_rotation", "0")
        adb("shell", "settings", "put", "system", "user_rotation", "1")
        wait_until(rotated, "emulator did not rotate to landscape", 20)
        adb("shell", "settings", "put", "system", "user_rotation", "0")
        wait_until(lambda: not rotated(), "emulator did not return to portrait", 20)
        wait_until(lambda: field_text("HTTPS server origin") == "https://example.com",
                   "server draft lost after Activity recreation", 20)
        assert field_text("Starting dashboard charge % (optional)") == "73"
        assert field_text("Driver credential") in ("", "Driver credential"), \
            "typed credential survived Activity recreation"
        result = {"emulator": SERIAL, "nonsecret_origin_restored": True,
                  "nonsecret_soc_restored": True, "unsubmitted_credential_not_restored": True}
        OUT.mkdir(parents=True, exist_ok=True)
        (OUT / "draft-state-verification.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(json.dumps(result, indent=2))
    finally:
        restore_setting("user_rotation", original_rotation)
        restore_setting("accelerometer_rotation", original_auto)
        time.sleep(0.5)


if __name__ == "__main__":
    main()
