"""Repeatable API 35 template-host smoke for the separate navigation module.

The host renders templates into a surface, so UIAutomator does not expose their
text. This script captures evidence for visual review and checks host errors,
package focus, and PNG validity. It does not certify a live route.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import struct
import subprocess
import time
import urllib.request
import zlib
from pathlib import Path


PACKAGE = "com.trickee.navigation"
ACTIVITY = "androidx.car.app.activity.CarAppActivity"
HOST = "com.google.android.apps.automotive.templates.host"
ACTION = "androidx.car.app.action.NAVIGATE"
PNG_HEADER = b"\x89PNG\r\n\x1a\n"


def adb(serial: str, *arguments: str, check: bool = True) -> str:
    command = ["adb", "-s", serial, *arguments]
    result = subprocess.run(command, text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(f"ADB failed: {command!r}\n{result.stdout}\n{result.stderr}")
    return result.stdout + result.stderr


def screenshot(serial: str, target: Path) -> dict:
    remote = "/sdcard/trickee-navigation-qa.png"
    adb(serial, "shell", "screencap", "-p", remote)
    adb(serial, "pull", remote, str(target))
    data = target.read_bytes()
    if not data.startswith(PNG_HEADER) or len(data) < 10_000:
        raise AssertionError(f"Missing or implausible host screenshot: {target}")
    return {"path": target.name, "bytes": len(data),
            "width": int.from_bytes(data[16:20], "big"),
            "height": int.from_bytes(data[20:24], "big"),
            "sha256": hashlib.sha256(data).hexdigest()}


def wait_screenshot(serial: str, target: Path, seconds: int = 30) -> dict:
    """Allow the template host to render after the activity takes focus."""
    deadline = time.monotonic() + seconds
    while True:
        try:
            return screenshot(serial, target)
        except AssertionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


def png_rgb_at(path: Path, x: int, y: int) -> tuple[int, int, int]:
    """Read one pixel from the emulator's 8-bit RGBA PNG without image packages."""
    data = path.read_bytes()
    if not data.startswith(PNG_HEADER):
        raise AssertionError("invalid PNG evidence")
    width, height, depth, color, _, _, interlace = struct.unpack(">IIBBBBB", data[16:29])
    if depth != 8 or color != 6 or interlace != 0 or not (0 <= x < width and 0 <= y < height):
        raise AssertionError("unsupported emulator PNG format")
    position = 8
    chunks = []
    while position + 12 <= len(data):
        length = int.from_bytes(data[position:position + 4], "big")
        kind = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        if kind == b"IDAT":
            chunks.append(payload)
        position += 12 + length
        if kind == b"IEND":
            break
    decoded = zlib.decompress(b"".join(chunks))
    stride = width * 4
    previous = bytearray(stride)
    position = 0
    for _ in range(y + 1):
        filter_type = decoded[position]
        position += 1
        row = bytearray(decoded[position:position + stride])
        position += stride
        for index in range(stride):
            left = row[index - 4] if index >= 4 else 0
            above = previous[index]
            upper_left = previous[index - 4] if index >= 4 else 0
            if filter_type == 1:
                predictor = left
            elif filter_type == 2:
                predictor = above
            elif filter_type == 3:
                predictor = (left + above) // 2
            elif filter_type == 4:
                predictor = min((left, above, upper_left),
                                key=lambda value: abs(left + above - upper_left - value))
            elif filter_type == 0:
                predictor = 0
            else:
                raise AssertionError("unsupported PNG filter")
            row[index] = (row[index] + predictor) & 0xff
        previous = row
    return tuple(previous[x * 4:x * 4 + 3])


def color_distance(left: tuple[int, int, int], right: tuple[int, int, int]) -> int:
    return sum(abs(a - b) for a, b in zip(left, right))


def wait_color(serial: str, target: Path, x: int, y: int,
               expected: tuple[int, int, int], maximum_distance: int,
               timeout_seconds: int = 25,
               second: tuple[int, int, tuple[int, int, int], int] | None = None) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_problem = "no screenshot"
    while time.monotonic() < deadline:
        try:
            frame = screenshot(serial, target)
            observed = png_rgb_at(target, x, y)
            if color_distance(observed, expected) <= maximum_distance:
                if second is None:
                    return frame
                other = png_rgb_at(target, second[0], second[1])
                if color_distance(other, second[2]) <= second[3]:
                    return frame
                last_problem = f"second pixel {other}, expected near {second[2]}"
            else:
                last_problem = f"observed {observed}, expected near {expected}"
        except (AssertionError, RuntimeError) as error:
            last_problem = str(error)
        time.sleep(2)
    raise AssertionError(f"Car host did not reach the expected visual state: {last_problem}")


def tap_primary_action(serial: str, frame: dict) -> None:
    """The pinned API 35 template host centers a MessageTemplate primary action here."""
    if frame["width"] != 1080 or frame["height"] != 600:
        raise AssertionError("Unsupported car-host layout for fixed action tap")
    adb(serial, "shell", "input", "tap", "540", "355")


def tap_navigation_stop(serial: str, frame: dict) -> None:
    """The API 35 NavigationTemplate action strip places Stop at top right."""
    if frame["width"] != 1080 or frame["height"] != 600:
        raise AssertionError("Unsupported car-host layout for navigation stop tap")
    adb(serial, "shell", "input", "tap", "1028", "96")
    time.sleep(1)
    adb(serial, "shell", "input", "tap", "1028", "96")


def focus_is_navigation(serial: str) -> bool:
    focus = adb(serial, "shell", "dumpsys", "window", "windows")
    return PACKAGE in focus and ACTIVITY in focus


def wait_for_navigation_focus(serial: str, seconds: int = 30) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if focus_is_navigation(serial):
            return True
        time.sleep(1)
    return False


def host_errors(serial: str) -> list[str]:
    lines = adb(serial, "logcat", "-d", "-s", "CarApp.H.Tem:E").splitlines()
    return [line for line in lines if " E CarApp.H.Tem:" in line]


def guidance_notice(serial: str) -> bool:
    records = adb(serial, "shell", "dumpsys", "notification", "--noredact")
    return any("NotificationRecord(" in line and f"pkg={PACKAGE} " in line
               and "id=17001 " in line and "category=navigation" in line
               and "ONGOING_EVENT" in line for line in records.splitlines())


def fixture_available(port: int, available: bool) -> None:
    body = json.dumps({"available": available}).encode("ascii")
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/__test__/availability", data=body,
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=3) as response:
        if response.status != 200:
            raise AssertionError("local route fixture did not accept availability control")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serial", default="emulator-5554")
    parser.add_argument("--apk", default="android-app/navigation/build/outputs/apk/debug/navigation-debug.apk")
    parser.add_argument("--output", default="docs/aaos-navigation-evidence")
    parser.add_argument("--fixture-route", action="store_true",
                        help="exercise a debug APK built with a loopback OSRM fixture")
    parser.add_argument("--fixture-port", type=int, default=8010)
    parser.add_argument("--reroute-outage", action="store_true",
                        help="exercise the local fixture's fail/recover control during guidance")
    args = parser.parse_args()
    if args.reroute_outage and not args.fixture_route:
        raise SystemExit("--reroute-outage requires --fixture-route")
    serial = args.serial
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    apk = Path(args.apk)
    if not apk.is_file():
        raise SystemExit(f"Build the debug APK first: {apk}")
    if adb(serial, "shell", "getprop", "ro.build.characteristics").strip() != "automotive":
        raise SystemExit("The selected device is not an Android Automotive image")
    packages = adb(serial, "shell", "pm", "list", "packages")
    if f"package:{HOST}" not in packages:
        raise SystemExit("The selected image has no Car App Library template host")
    adb(serial, "install", "-r", str(apk))
    adb(serial, "shell", "am", "force-stop", PACKAGE)
    adb(serial, "logcat", "-c")
    adb(serial, "shell", "am", "start", "-n", f"{PACKAGE}/{ACTIVITY}")
    time.sleep(2)
    if not wait_for_navigation_focus(serial):
        raise AssertionError("Navigation CarAppActivity is not focused after launch")
    launch = wait_screenshot(serial, output / "launch.png")

    adb(serial, "shell", "am", "start", "-a", ACTION,
        "-d", "geo:0,0?q=12.7409,77.8253%28Office%29",
        "-n", f"{PACKAGE}/{ACTIVITY}")
    time.sleep(2)
    if not wait_for_navigation_focus(serial):
        raise AssertionError("Navigation intent did not stay in the host activity")
    valid = wait_screenshot(serial, output / "valid-intent.png")

    adb(serial, "shell", "am", "start", "-a", ACTION,
        "-d", "geo:0,0?q=coffee", "-n", f"{PACKAGE}/{ACTIVITY}")
    time.sleep(2)
    invalid = wait_screenshot(serial, output / "unsupported-intent.png")
    route_preview = None
    route_guidance = None
    location_paused = None
    location_restored = None
    stopped_guidance = None
    replaced_destination = None
    reroute_outage = None
    outage_recovered = None
    notice_during_guidance = None
    notice_after_stop = None
    notice_permission_denied = None
    if args.fixture_route:
        reverse = adb(serial, "reverse", "--list")
        if f"tcp:{args.fixture_port}" not in reverse:
            raise AssertionError(f"ADB reverse tcp:{args.fixture_port} is required for the route fixture")
        if args.reroute_outage:
            fixture_available(args.fixture_port, True)
        current_user = adb(serial, "shell", "am", "get-current-user").strip()
        adb(serial, "shell", "cmd", "location", "set-location-enabled", "true")
        adb(serial, "shell", "pm", "grant", "--user", current_user, PACKAGE,
            "android.permission.ACCESS_COARSE_LOCATION")
        adb(serial, "shell", "pm", "grant", "--user", current_user, PACKAGE,
            "android.permission.ACCESS_FINE_LOCATION")
        if int(adb(serial, "shell", "getprop", "ro.build.version.sdk").strip()) >= 33:
            adb(serial, "shell", "pm", "revoke", "--user", current_user, PACKAGE,
                "android.permission.POST_NOTIFICATIONS")
            adb(serial, "shell", "am", "force-stop", PACKAGE)
            adb(serial, "shell", "am", "start", "-a", ACTION,
                "-d", "geo:0,0?q=12.9720,77.5950%28Office%29",
                "-n", f"{PACKAGE}/{ACTIVITY}")
            adb(serial, "emu", "geo", "fix", "77.5946", "12.9716")
            time.sleep(3)
            notice_permission_denied = wait_color(serial,
                output / "notice-permission-denied.png", 540, 180, (19, 19, 19), 30,
                second=(540, 355, (73, 73, 73), 45))
            if guidance_notice(serial):
                raise AssertionError("Denied notice permission still produced a navigation notice")
        adb(serial, "shell", "pm", "grant", "--user", current_user, PACKAGE,
            "android.permission.POST_NOTIFICATIONS")
        adb(serial, "shell", "am", "force-stop", PACKAGE)
        # The template host tears down its surface asynchronously after a
        # deliberate force-stop. Let that expected teardown finish before
        # clearing logs for the guidance/error checks below.
        time.sleep(3)
        adb(serial, "logcat", "-c")
        adb(serial, "shell", "am", "start", "-a", ACTION,
            "-d", "geo:0,0?q=12.9720,77.5950%28Office%29",
            "-n", f"{PACKAGE}/{ACTIVITY}")
        adb(serial, "emu", "geo", "fix", "77.5946", "12.9716")
        time.sleep(3)
        route_preview = wait_color(serial, output / "route-preview.png", 540, 180,
                                   (19, 19, 19), 30,
                                   second=(540, 355, (73, 73, 73), 45))
        if route_preview["sha256"] == valid["sha256"]:
            raise AssertionError("Route preview did not alter the template surface")
        for attempt in range(3):
            tap_primary_action(serial, route_preview)
            adb(serial, "emu", "geo", "fix", "77.5946", "12.9716")
            try:
                route_guidance = wait_color(serial, output / "active-guidance.png", 540, 180,
                                            (247, 247, 247), 40, timeout_seconds=10)
                break
            except AssertionError:
                if attempt == 2:
                    raise
        if route_guidance["sha256"] == route_preview["sha256"]:
            raise AssertionError("Starting guidance did not alter the template surface")
        preview_background = png_rgb_at(output / "route-preview.png", 540, 180)
        active_background = png_rgb_at(output / "active-guidance.png", 540, 180)
        if color_distance(preview_background, active_background) < 100:
            raise AssertionError("NavigationTemplate surface did not replace the preview")
        notice_during_guidance = guidance_notice(serial)
        if not notice_during_guidance:
            raise AssertionError("Active guidance did not post an ongoing navigation notice")
        adb(serial, "shell", "cmd", "location", "set-location-enabled", "false")
        try:
            time.sleep(16)
            location_paused = wait_color(serial, output / "location-paused.png", 400, 110,
                                         (19, 19, 19), 40,
                                         second=(540, 180, (247, 247, 247), 40))
            if color_distance(png_rgb_at(output / "location-paused.png", 400, 110),
                              png_rgb_at(output / "active-guidance.png", 400, 110)) < 100:
                raise AssertionError("Location loss did not show the guidance pause")
        finally:
            adb(serial, "shell", "cmd", "location", "set-location-enabled", "true")
        adb(serial, "emu", "geo", "fix", "77.5946", "12.9716")
        time.sleep(2)
        location_restored = wait_color(serial, output / "location-restored.png", 400, 110,
                                       (247, 247, 247), 40)
        if color_distance(png_rgb_at(output / "location-restored.png", 400, 110),
                          png_rgb_at(output / "active-guidance.png", 400, 110)) > 80:
            raise AssertionError("Guidance did not recover after location returned")
        tap_navigation_stop(serial, location_restored)
        time.sleep(1)
        stopped_guidance = wait_color(serial, output / "stopped-guidance.png", 540, 180,
                                      preview_background, 30,
                                      second=(540, 355, (73, 73, 73), 45))
        if color_distance(png_rgb_at(output / "stopped-guidance.png", 540, 180),
                          preview_background) > 30:
            raise AssertionError("Stop did not return to the route preview")
        notice_after_stop = guidance_notice(serial)
        if notice_after_stop:
            raise AssertionError("Stop left a navigation notice active")
        for attempt in range(3):
            tap_primary_action(serial, stopped_guidance)
            try:
                wait_color(serial, output / "replacement-guidance.png", 540, 180,
                           (247, 247, 247), 40, timeout_seconds=10)
                break
            except AssertionError:
                if attempt == 2:
                    raise
        adb(serial, "shell", "am", "start", "-a", ACTION,
            "-d", "geo:0,0?q=12.9730,77.5960%28New%20Office%29",
            "-n", f"{PACKAGE}/{ACTIVITY}")
        time.sleep(3)
        replaced_destination = wait_color(serial, output / "replaced-destination.png", 540, 180,
                                          preview_background, 30)
        if not focus_is_navigation(serial):
            raise AssertionError("New destination did not remain in the host activity")
        if args.reroute_outage:
            for attempt in range(3):
                tap_primary_action(serial, replaced_destination)
                try:
                    wait_color(serial, output / "outage-started.png", 540, 180,
                               (247, 247, 247), 40, timeout_seconds=10)
                    break
                except AssertionError:
                    if attempt == 2:
                        raise
            try:
                fixture_available(args.fixture_port, False)
                for offset in range(5):
                    adb(serial, "emu", "geo", "fix", f"{77.5920 + offset * .0001:.4f}",
                        f"{12.9740 + offset * .0001:.4f}")
                    time.sleep(1.5)
                reroute_outage = wait_color(serial, output / "reroute-outage.png", 400, 110,
                                            (19, 19, 19), 40,
                                            second=(540, 180, (247, 247, 247), 40))
                if "Reroute failed" not in adb(serial, "logcat", "-d", "-s", "TrickeeNav:W"):
                    raise AssertionError("Off-route fixes did not exercise provider reroute failure")
            finally:
                fixture_available(args.fixture_port, True)
            adb(serial, "emu", "geo", "fix", "77.5946", "12.9716")
            outage_recovered = wait_color(serial, output / "reroute-outage-recovered.png",
                                          400, 110, (247, 247, 247), 40)
    errors = host_errors(serial)
    if errors:
        raise AssertionError("Template host reported an error:\n" + "\n".join(errors[:10]))
    report = {
        "serial": serial,
        "api_level": adb(serial, "shell", "getprop", "ro.build.version.sdk").strip(),
        "host_package": HOST,
        "apk_sha256": hashlib.sha256(apk.read_bytes()).hexdigest(),
        "launch": launch,
        "valid_coordinate_intent": valid,
        "unsupported_search_intent": invalid,
        "fixture_route_preview": route_preview,
        "fixture_active_guidance": route_guidance,
        "fixture_location_paused": location_paused,
        "fixture_location_restored": location_restored,
        "fixture_stopped_guidance": stopped_guidance,
        "fixture_replaced_destination": replaced_destination,
        "fixture_reroute_outage": reroute_outage,
        "fixture_outage_recovered": outage_recovered,
        "ongoing_navigation_notice_during_guidance": notice_during_guidance,
        "navigation_notice_after_stop": notice_after_stop,
        "notice_permission_denied": notice_permission_denied,
        "template_host_errors": 0,
        "scope": ("host rendering, intents, synthetic route preview, foreground guidance "
                  "start/stop, location pause/recovery and active destination replacement"
                  + (", reroute outage/recovery" if args.reroute_outage else "")
                  + "; no production provider, map tiles or field guidance") if args.fixture_route else (
                      "host rendering and intents with no configured road provider; "
                      "no production provider, map tiles or field guidance"),
    }
    (output / "verification.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
