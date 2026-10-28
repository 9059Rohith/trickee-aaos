"""Run visual and interaction checks against a local Trickee console."""
from __future__ import annotations

import json
import os
import struct
import zlib
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
OUT = Path(os.environ["TRICKEE_QA_OUTPUT"]) / "ui-parity" if os.getenv("TRICKEE_QA_OUTPUT") else ROOT / "docs" / "ui-parity"
URL = os.getenv("TRICKEE_UI_URL", "http://127.0.0.1:8003").rstrip("/")
KEY = os.getenv("TRICKEE_UI_DEMO_KEY", "local-demo-operator-key")


def offline_tile() -> bytes:
    """A local PNG for hermetic CI; interactive visual QA still uses live tiles."""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    row = b"\0" + b"\x13\x24\x30" * 256
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 256, 256, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * 256)) + chunk(b"IEND", b""))


def overflow(page) -> bool:
    return page.evaluate("document.documentElement.scrollWidth > window.innerWidth + 1")


def main() -> None:
    if urlparse(URL).hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("UI verification accepts loopback services only")
    OUT.mkdir(parents=True, exist_ok=True)
    findings: dict[str, object] = {"url": URL}
    tile = offline_tile() if os.getenv("TRICKEE_UI_OFFLINE_TILES") == "1" else None
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        desktop = browser.new_context(viewport={"width": 1440, "height": 900}, device_scale_factor=1)
        if tile is not None:
            desktop.route("https://tile.openstreetmap.org/**", lambda request: request.fulfill(
                status=200, content_type="image/png", body=tile))
        page = desktop.new_page()
        errors: list[str] = []
        tile_referrers: list[str] = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.on("request", lambda request: tile_referrers.append(request.headers.get("referer", ""))
                if request.url.startswith("https://tile.openstreetmap.org/") else None)
        page.goto(URL, wait_until="networkidle")
        page.evaluate("document.fonts.ready")
        assert page.locator(".hero-image").evaluate("(img) => img.complete && img.naturalWidth > 0")
        assert page.evaluate("document.fonts.check('700 24px Syne') && document.fonts.check('400 14px Manrope') && document.fonts.check('400 9px Michroma')")
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--yellow').trim()") == "#ffe000"
        assert page.locator(".route-core").evaluate("(el) => getComputedStyle(el).animationName") == "route-draw"
        assert page.locator(".route-core").evaluate("(el) => getComputedStyle(el).animationDuration") == "1.18s"
        page.wait_for_function("getComputedStyle(document.querySelector('.route-core')).strokeDashoffset === '0px'")
        page.keyboard.press("Tab")
        assert page.evaluate("document.activeElement.classList.contains('skip-link')")
        page.locator("#operatorKey").fill(KEY)
        page.get_by_role("button", name="Connect", exact=True).click()
        page.get_by_text("CONNECTED", exact=True).wait_for()
        assert page.locator("#mVehicles").inner_text() != "—"
        page.locator("#mapCanvas.leaflet-container").wait_for()
        assert page.locator("#mapCanvas .leaflet-tile-pane").count() == 1
        page.wait_for_function("[...document.querySelectorAll('#mapCanvas .leaflet-tile')].some(tile => tile.complete && tile.naturalWidth > 0)")
        assert tile_referrers and all(value.startswith(URL + "/") for value in tile_referrers)
        vehicle_marker = page.locator('#mapCanvas path[fill="#ffe000"]:not(.trickee-replay-position)')
        vehicle_marker.first.wait_for()
        assert vehicle_marker.count() == 1
        assert page.locator("#mapVehicleList button").count() == 1
        assert page.locator("#mapStatus").inner_text()
        findings["fleet_map"] = {"tiles_loaded": True, "reported_marker": True, "status": page.locator("#mapStatus").inner_text()}
        page.locator("#tripList button").first.wait_for()
        page.wait_for_function("document.querySelector('#tripDetail').textContent.includes('TELEMETRY')")
        assert "TELEMETRY" in page.locator("#tripDetail").inner_text()
        findings["trip_history"] = {"loaded": True, "telemetry_provenance": True}
        replay_slider = page.locator("#replayFrameSlider")
        replay_slider.wait_for()
        assert "historical, not live" in page.locator(".replay-panel .route-hint").inner_text()
        assert "source SIMULATOR" in page.locator(".replay-readout").inner_text()
        if int(replay_slider.get_attribute("max")) > 0:
            latest_readout = page.locator(".replay-readout").inner_text()
            replay_slider.evaluate("element => { element.value = '0'; element.dispatchEvent(new Event('input', { bubbles: true })); }")
            assert page.locator(".replay-readout").inner_text() != latest_readout
        assert page.locator("#mapCanvas .trickee-replay-position").count() == 1
        findings["trip_replay"] = {"historical_label": True, "slider": True,
                                    "map_marker": True}
        page.wait_for_function("document.querySelector('#reportFrames').textContent !== '—'")
        report = page.request.get(URL + "/api/v1/fleet/report?days=7", headers={"X-Operator-Key": KEY}).json()
        alerts = page.request.get(URL + "/api/v1/fleet/alerts", headers={"X-Operator-Key": KEY}).json()
        assert page.locator("#reportQuality").inner_text() == report["quality"]
        assert page.locator("#fleetAlerts article").count() == len(alerts)
        notices = page.request.get(URL + "/api/v1/fleet/notifications?limit=20",
                                   headers={"X-Operator-Key": KEY}).json()
        assert page.locator("#fleetNotifications article").count() == len(notices)
        assert int(page.locator("#notificationCount").inner_text()) == sum(
            not notice["acknowledged_at"] for notice in notices)
        findings["fleet_insights"] = {"stored_frames": True, "source_label": report["quality"],
                                      "rule_alerts": len(alerts), "notifications": len(notices)}
        page.route("**/api/v1/places/search**", lambda request: request.fulfill(status=200, json=[{
            "place_id": "12", "name": "Bengaluru, Karnataka", "latitude": 12.9716,
            "longitude": 77.5946, "source": "PROVIDER", "quality": "REPORTED",
        }]))
        page.locator("#placeQuery").fill("Bengaluru")
        page.locator("#placeSearchBtn").click()
        page.locator("#placeResults .place-result").wait_for()
        page.get_by_role("button", name="Use as origin").click()
        assert page.locator("#origin").input_value() == "12.9716, 77.5946"
        page.locator("#origin").fill("Bengaluru")
        page.unroute("**/api/v1/places/search**")
        findings["place_search_ui"] = {"provider_result_selected": True}
        page.get_by_role("button", name="Find mock route").click()
        page.get_by_text("Mock route loaded:", exact=False).wait_for()
        assert "Mock route loaded" in page.locator("#planResult").inner_text()
        page.get_by_role("button", name="Calculate route").click()
        page.get_by_text("Arrival charge", exact=True).wait_for()
        assert "Arrival charge" in page.locator("#planResult").inner_text()
        page.locator("#question").fill("Can I reach the charger?")
        page.get_by_role("button", name="Ask").click()
        page.get_by_text("Estimated arrival charge:", exact=False).wait_for()
        answer = page.locator("#copilotResult").inner_text()
        assert "Estimated arrival charge" in answer and "undefined" not in answer
        page.wait_for_function("document.getAnimations().every(animation => animation.playState !== 'running')")
        page.evaluate("document.documentElement.style.scrollBehavior = 'auto'; window.scrollTo(0, 0)")
        page.wait_for_function("window.scrollY === 0")
        page.screenshot(path=str(OUT / "desktop-1440-viewport.png"))
        page.screenshot(path=str(OUT / "desktop-1440-full.png"), full_page=True)
        assert not overflow(page)
        findings["desktop"] = {"viewport": "1440x900", "answer": answer, "overflow": False}
        page.route("**/api/v1/places/search**", lambda request: request.fulfill(
            status=503, headers={"Retry-After": "30"},
            json={"detail": "place provider quota reached; retry later"}))
        page.locator("#placeQuery").fill("Quota test")
        page.locator("#placeSearchBtn").click()
        page.get_by_text("place provider quota reached; retry later. Try again in 30 seconds.", exact=True).wait_for()
        page.unroute("**/api/v1/places/search**")
        findings["place_search_ui"]["quota_state_visible"] = True
        page.evaluate("window.TrickeeMap.render([{vehicle_id:'OLD-GPS', quality:'STALE', observed_at:'2026-01-01T00:00:00Z', location:{latitude:12.9716,longitude:77.5946}}], 'OLD-GPS', null)")
        assert "last known position · telemetry stale" in page.locator("#mapStatus").inner_text()
        assert "last known" in page.locator("#mapVehicleList button").inner_text()
        assert page.locator('#mapCanvas path[fill-opacity="0.35"]').count() == 1
        findings["map_stale_location"] = {"labeled": True, "dimmed_pin": True}
        page.evaluate("window.TrickeeMap.render([{vehicle_id:'FUTURE-GPS', quality:'INVALID', observed_at:'2099-01-01T00:00:00Z', location:{latitude:12.9716,longitude:77.5946}}], 'FUTURE-GPS', null)")
        assert "invalid telemetry" in page.locator("#mapStatus").inner_text()
        assert page.locator("#mapVehicleList button").count() == 0
        assert page.locator('#mapCanvas path[fill="#ffe000"]').count() == 0
        findings["map_invalid_location_hidden"] = True
        page.evaluate("window.TrickeeMap.render([{vehicle_id:'NO-GPS', quality:'MISSING', location:null}], 'NO-GPS', null)")
        assert "No vehicle location reported" in page.locator("#mapStatus").inner_text()
        assert page.locator('#mapCanvas path[fill="#ffe000"]').count() == 0
        assert page.locator("#mapVehicleList button").count() == 0
        findings["map_missing_location"] = True
        page.evaluate("window.TrickeeMap.showTripTrace(null)")
        sample_road = {"quality": "PROVIDER", "traffic": {"quality": "UNAVAILABLE", "live": False}, "routes": [
            {"route": {"distance_km": 48, "baseline_eta_min": 60, "average_speed_kmh": 48, "traffic_factor": 1, "source": "PROVIDER"},
             "geometry": {"type": "LineString", "coordinates": [[77.5946, 12.9716], [77.7, 12.85], [77.8253, 12.7409]]}},
            {"route": {"distance_km": 52, "baseline_eta_min": 65, "average_speed_kmh": 48, "traffic_factor": 1, "source": "PROVIDER"},
             "geometry": {"type": "LineString", "coordinates": [[77.5946, 12.9716], [77.6, 12.8], [77.8253, 12.7409]]}},
        ]}
        page.route("**/api/v1/routes/road", lambda request: request.fulfill(status=200, json=sample_road))
        page.route("**/api/v1/routes/context", lambda request: request.fulfill(status=200, json={
            "weather": {"quality": "REPORTED", "source": "OPENWEATHER_CURRENT", "temperature_c": 32.5},
            "elevation": {"quality": "UNAVAILABLE", "reason": "NOT_CONFIGURED"},
            "traffic": {"quality": "UNAVAILABLE", "live": False},
        }))
        page.locator("#origin").fill("12.9716, 77.5946")
        page.locator("#destination").fill("12.7409, 77.8253")
        page.get_by_role("button", name="Find road route").click()
        page.get_by_text("Road route 1 of 2:", exact=False).wait_for()
        page.wait_for_function("document.querySelector('#routeContext').textContent.includes('32.5 °C')")
        assert "Elevation unavailable" in page.locator("#routeContext").inner_text()
        assert page.locator("#mapCanvas path.trickee-road-route").count() == 1
        assert page.evaluate("routeBody().source") == "PROVIDER"
        page.get_by_role("button", name="Option 2").click()
        assert page.locator("#distance").input_value() == "52"
        assert page.get_by_role("button", name="Option 2").get_attribute("aria-pressed") == "true"
        page.locator("#distance").fill("53")
        assert page.evaluate("routeBody().source") == "SIMULATED"
        assert page.locator("#mapCanvas path.trickee-road-route").count() == 0
        page.unroute("**/api/v1/routes/road")
        page.unroute("**/api/v1/routes/context")
        findings["road_route_ui"] = {"alternative_selection": True, "manual_override_provenance": True,
                                      "sourced_context_and_unavailable_state": True}
        trip_fixture: dict[str, object] = {}
        trip_base = "/api/v1/vehicles/TRICKEE-DEMO-1/trips"
        def mock_trip_command(request):
            path = urlparse(request.request.url).path
            if path == trip_base and request.request.method == "POST":
                body = request.request.post_data_json
                trip_fixture.update({"trip_id": body["trip_id"], "vehicle_id": "TRICKEE-DEMO-1", "driver_id": "DEMO-DRIVER",
                    "status": "ACTIVE", "quality": "MISSING", "source": "OPERATOR_SUPERVISED", "started_at": "2026-10-06T10:00:00Z",
                    "ended_at": None, "starting_soc_pct": body["starting_soc_pct"], "starting_soc_source": "MANUAL_DASHBOARD",
                    "ending_soc_pct": None, "ending_soc_source": None, "reported_distance_km": None,
                    "telemetry_frame_count": 0, "trace": [], "trace_truncated": False})
                request.fulfill(status=200, json=trip_fixture)
            elif path == trip_base and request.request.method == "GET":
                request.fulfill(status=200, json=[trip_fixture] if trip_fixture else [])
            elif path.endswith("/complete") and request.request.method == "POST":
                body = request.request.post_data_json
                trip_fixture.update({"status": "COMPLETED", "ended_at": "2026-10-06T10:15:00Z",
                    "ending_soc_pct": body["ending_soc_pct"], "ending_soc_source": "MANUAL_DASHBOARD"})
                request.fulfill(status=200, json=trip_fixture)
            elif path.startswith(trip_base + "/") and request.request.method == "GET":
                request.fulfill(status=200, json=trip_fixture)
            else:
                request.continue_()
        page.route("**/api/v1/vehicles/TRICKEE-DEMO-1/trips**", mock_trip_command)
        page.locator("#startSoc").fill("82")
        page.get_by_role("button", name="Start supervised trip").click()
        page.wait_for_function("document.querySelector('#tripDetail').textContent.includes('82% · MANUAL_DASHBOARD')")
        assert page.locator("#soc").inner_text() == "64"
        page.locator("#endSoc").fill("76")
        page.get_by_role("button", name="Complete selected trip").click()
        page.wait_for_function("document.querySelector('#tripDetail').textContent.includes('76% · MANUAL_DASHBOARD')")
        assert page.get_by_role("button", name="Complete selected trip").is_disabled()
        page.unroute("**/api/v1/vehicles/TRICKEE-DEMO-1/trips**")
        findings["trip_command_ui"] = {"start_complete": True, "manual_soc_separate_from_telemetry": True}
        daily_fixture: dict[str, object] = {}
        daily_base = "/api/v1/vehicles/TRICKEE-DEMO-1/daily-plans"
        def mock_daily_plan(request):
            path = urlparse(request.request.url).path
            if path == daily_base and request.request.method == "POST":
                body = request.request.post_data_json
                daily_fixture.update({**body, "vehicle_id": "TRICKEE-DEMO-1", "driver_id": "DEMO-DRIVER",
                    "status": "DRAFT", "starting_soc_source": "MANUAL_DASHBOARD", "result": None})
                request.fulfill(status=200, json=daily_fixture)
            elif path == daily_base and request.request.method == "GET":
                request.fulfill(status=200, json=[daily_fixture] if daily_fixture else [])
            elif path.endswith("/confirm") and request.request.method == "POST":
                daily_fixture.update({"status": "CONFIRMED", "result": {"quality": "UNAVAILABLE",
                    "energy_assumptions": "Nominal usable capacity only.", "legs": [{
                        "route_source": "UNAVAILABLE", "degraded_reason": "ROAD_ROUTER_NOT_CONFIGURED",
                        "arrival_soc_pct": None,
                    }]}})
                request.fulfill(status=200, json=daily_fixture)
            else:
                request.continue_()
        page.route("**/api/v1/vehicles/TRICKEE-DEMO-1/daily-plans**", mock_daily_plan)
        page.locator("#dailyDate").fill("2026-10-07")
        page.locator("#dailySoc").fill("82")
        page.locator("#dailyOrigin").fill("12.9716, 77.5946")
        page.locator("#dailyStops").fill("09:00 | Office | 12.9800, 77.6000")
        page.get_by_role("button", name="Save draft").click()
        page.get_by_role("button", name="Confirm and calculate").wait_for()
        page.get_by_role("button", name="Confirm and calculate").click()
        page.wait_for_function("document.querySelector('#dailyPlanDetail').textContent.includes('Road and energy prediction unavailable')")
        assert "MANUAL_DASHBOARD" in page.locator("#dailyPlanDetail").inner_text()
        assert page.locator("#soc").inner_text() == "64"
        page.unroute("**/api/v1/vehicles/TRICKEE-DEMO-1/daily-plans**")
        findings["daily_plan_ui"] = {"draft_confirm": True, "degraded_route_labeled": True, "manual_soc_separate_from_telemetry": True}
        page.locator("#question").fill("Steer the car")
        page.get_by_role("button", name="Ask").click()
        page.get_by_text("Vehicle control is unavailable", exact=False).wait_for()
        assert page.locator("#copilotResult").get_attribute("data-tier") == "DENIED"
        findings["copilot_denial"] = True
        page.route("**/api/v1/fleet/summary", lambda request: request.abort())
        page.get_by_role("button", name="Refresh").click()
        page.get_by_text("DISCONNECTED", exact=True).wait_for()
        page.unroute("**/api/v1/fleet/summary")
        page.get_by_role("button", name="Refresh").click()
        page.get_by_text("CONNECTED", exact=True).wait_for()
        findings["connection_recovery"] = True
        page.get_by_role("button", name="Switch to light theme").click()
        assert page.evaluate("document.documentElement.dataset.theme") == "light"
        assert page.locator("#themeToggle").get_attribute("aria-pressed") == "true"
        assert page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--canvas').trim()") == "#ffffff"
        assert page.locator("#brandLogo").evaluate("(img) => img.complete && img.naturalWidth > 0")
        page.wait_for_function("document.getAnimations().every(animation => animation.playState !== 'running')")
        page.screenshot(path=str(OUT / "desktop-light-1440-viewport.png"))
        page.reload(wait_until="domcontentloaded")
        assert page.evaluate("document.documentElement.dataset.theme") == "light"
        findings["theme"] = {"light_palette": True, "persisted_after_reload": True}

        for width, height in [(390, 844), (320, 740)]:
            context = browser.new_context(viewport={"width": width, "height": height}, device_scale_factor=1)
            if tile is not None:
                context.route("https://tile.openstreetmap.org/**", lambda request: request.fulfill(
                    status=200, content_type="image/png", body=tile))
            mobile = context.new_page()
            mobile_errors: list[str] = []
            mobile.on("pageerror", lambda err: mobile_errors.append(str(err)))
            mobile.goto(URL, wait_until="networkidle")
            mobile.locator("#operatorKey").fill(KEY)
            mobile.get_by_role("button", name="Connect", exact=True).click()
            mobile.get_by_text("CONNECTED", exact=True).wait_for()
            assert not overflow(mobile), f"horizontal overflow at {width}px"
            assert mobile.locator(".hero-image").evaluate("(img) => img.naturalWidth > 0")
            if width == 390:
                mobile.wait_for_function("document.getAnimations().every(animation => animation.playState !== 'running')")
                mobile.screenshot(path=str(OUT / "mobile-390-full.png"), full_page=True)
                mobile.get_by_role("button", name="Switch to light theme").click()
                assert not overflow(mobile)
                mobile.wait_for_function("document.getAnimations().every(animation => animation.playState !== 'running')")
                mobile.screenshot(path=str(OUT / "mobile-light-390-full.png"), full_page=True)
            assert not mobile_errors, mobile_errors
            findings[f"mobile_{width}"] = {"viewport": f"{width}x{height}", "overflow": False}
            context.close()

        reduced = browser.new_context(viewport={"width": 390, "height": 844}, reduced_motion="reduce")
        calm = reduced.new_page()
        calm.goto(URL, wait_until="networkidle")
        duration = calm.locator(".route-core").evaluate("(el) => getComputedStyle(el).animationDuration")
        assert float(duration.rstrip("s")) < 0.01, duration
        assert calm.locator(".hero-copy").is_visible()
        assert not overflow(calm)
        findings["reduced_motion"] = {"route_animation_duration": duration, "content_visible": True}
        reduced.close()

        bad = browser.new_context(viewport={"width": 1440, "height": 900})
        invalid = bad.new_page()
        invalid.goto(URL, wait_until="networkidle")
        invalid.locator("#operatorKey").fill("invalid-demo-key")
        invalid.get_by_role("button", name="Connect", exact=True).click()
        invalid.get_by_text("Operator key is invalid.").wait_for()
        assert "invalid" in invalid.locator("#error").inner_text().lower()
        assert invalid.locator("#connection").inner_text() == "DISCONNECTED"
        bad.close()

        viewer_context = browser.new_context(viewport={"width": 390, "height": 844})
        if tile is not None:
            viewer_context.route("https://tile.openstreetmap.org/**", lambda request: request.fulfill(
                status=200, content_type="image/png", body=tile))
        viewer = viewer_context.new_page()
        viewer.route("**/api/v1/operator/me", lambda request: request.fulfill(status=200, json={
            "tenant_id": "test", "role": "viewer", "scoped": True,
        }))
        viewer.goto(URL, wait_until="networkidle")
        viewer.locator("#operatorKey").fill(KEY)
        viewer.get_by_role("button", name="Connect", exact=True).click()
        viewer.get_by_text("CONNECTED", exact=True).wait_for()
        assert viewer.locator("#startTrip").is_disabled()
        assert viewer.locator("#dailyDate").is_disabled()
        assert viewer.locator("#saveDailyPlan").is_disabled()
        assert viewer.locator("#sendNudge").is_disabled()
        findings["viewer_ui"] = {"trip_write_disabled": True, "daily_plan_write_disabled": True,
                                 "advisory_write_disabled": True}
        viewer_context.close()

        page.locator("#operatorKey").fill("invalid-switch-key")
        page.get_by_role("button", name="Connect", exact=True).click()
        page.get_by_text("Operator key is invalid.").wait_for()
        assert page.locator("#mVehicles").inner_text() == "—"
        assert page.locator("#tripList button").count() == 0
        assert page.locator("#dailyPlanList button").count() == 0
        assert page.locator("#nudgeList .nudge-card").count() == 0
        # Reconnect fetches map configuration asynchronously. Exercise the failed
        # configuration through that API so its response cannot replace a manual
        # configure() call made while the request is still in flight.
        page.route("**/missing/**", lambda request: request.abort())
        page.route("**/api/v1/map/config", lambda request: request.fulfill(status=200, json={
            "tile_url": "/missing/{z}/{x}/{y}.png", "attribution": "Test", "quality": "DEMO",
        }))
        page.locator("#operatorKey").fill(KEY)
        page.get_by_role("button", name="Connect", exact=True).click()
        page.get_by_text("CONNECTED", exact=True).wait_for()
        findings["key_switch_clears_data"] = True

        page.locator("#mapVehicleList button").first.wait_for()
        page.get_by_text("map tiles unavailable", exact=False).wait_for()
        findings["map_tile_failure"] = True

        assert not errors, errors
        findings["page_errors"] = 0
        desktop.close()
        browser.close()
    (OUT / "verification.json").write_text(json.dumps(findings, indent=2), encoding="utf-8")
    print(json.dumps(findings, indent=2))


if __name__ == "__main__":
    main()
