"""Exercise the driver companion in Chromium with foreground GPS and upload recovery."""

import hashlib
import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import time
from datetime import date, timedelta
from pathlib import Path

import httpx
from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from trickee.simulator import demo_profile
from trickee.store import CloudStore
from scripts.verify_ui import offline_tile
from scripts.verify_application import _stop_tree


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="trickee-driver-qa-") as directory:
        database = str(Path(directory) / "driver.db")
        store = CloudStore(database)
        store.register(demo_profile(), "QA-DRIVER")
        token = secrets.token_urlsafe(32)
        store.issue_driver_credential("TRICKEE-DEMO-1", hashlib.sha256(token.encode()).hexdigest())
        store.register(demo_profile().model_copy(update={"vehicle_id": "TRICKEE-QA-2"}), "QA-OTHER")
        other_token = secrets.token_urlsafe(32)
        store.issue_driver_credential("TRICKEE-QA-2", hashlib.sha256(other_token.encode()).hexdigest())
        store.create_nudge("TRICKEE-DEMO-1", "N-QA-1", "nudge-qa-1", "ROUTE_REVIEW",
                           "Review the confirmed route before departure.", None)
        store.close()
        port = free_port()
        origin = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update({"TRICKEE_DEMO_MODE": "1", "TRICKEE_DB": database})
        server = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "trickee.api:app", "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(80):
                if server.poll() is not None:
                    raise RuntimeError("local QA server exited")
                try:
                    if httpx.get(origin + "/health", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("local QA server did not become ready")

            plan_url = origin + "/api/v1/vehicles/TRICKEE-DEMO-1/daily-plans"
            operator = {"X-Operator-Key": "local-demo-operator-key"}
            plan = {"plan_id": "DAY-QA-1", "idempotency_key": "draft-qa-1",
                    "service_date": (date.today() + timedelta(days=1)).isoformat(),
                    "timezone": "Asia/Kolkata", "starting_soc_pct": 80,
                    "origin": {"latitude": 12.9716, "longitude": 77.5946},
                    "stops": [{"label": "Office", "requested_arrival_local": "09:00",
                               "coordinates": {"latitude": 12.98, "longitude": 77.60}}]}
            assert httpx.post(plan_url, headers=operator, json=plan).status_code == 200
            assert httpx.post(plan_url + "/DAY-QA-1/confirm", headers=operator,
                              json={"idempotency_key": "confirm-qa-1"}).status_code == 200

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(
                    viewport={"width": 390, "height": 844},
                    permissions=["geolocation"],
                    geolocation={"latitude": 12.9716, "longitude": 77.5946, "accuracy": 8},
                )
                context.route("https://tile.openstreetmap.org/**", lambda request: request.fulfill(
                    status=200, content_type="image/png", body=offline_tile()))
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin + "/driver", wait_until="networkidle")
                assert page.locator("h1").inner_text().replace("\n", " ").startswith("Drive with")
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                page.locator("#driverKey").fill(token)
                page.get_by_role("button", name="Connect", exact=True).click()
                page.get_by_text("CONNECTED", exact=True).wait_for()
                assert page.get_by_role("button", name="Start GPS").is_disabled()
                page.get_by_role("button", name="Agree to trip GPS and motion").click()
                page.get_by_text("Trip GPS and motion consent granted", exact=False).wait_for()
                assert page.locator("#vehicleId").inner_text() == "TRICKEE-DEMO-1"
                assert page.locator("#soc").inner_text() == "—"
                assert "Office" in page.locator("#driverPlanList").inner_text()
                page.locator("#nudgeList .nudge-card").wait_for()
                assert "Review the confirmed route" in page.locator("#nudgeList").inner_text()
                page.route("**/api/v1/driver/nudges/N-QA-1/respond", lambda request: request.abort())
                page.locator("#nudgeList").get_by_role("button", name="Acknowledge").click()
                page.wait_for_function("document.querySelector('#nudgeList').textContent.includes('ACKNOWLEDGED QUEUED')")
                assert page.request.get(origin + "/api/v1/driver/nudges", headers={"X-Driver-Key": token}).json()[0]["status"] == "PENDING"
                page.unroute("**/api/v1/driver/nudges/N-QA-1/respond")
                page.get_by_role("button", name="Sync queued responses").click()
                page.wait_for_function("document.querySelector('#nudgeStatus').textContent.includes('0 responses queued')")
                assert "ACKNOWLEDGED" in page.locator("#nudgeList").inner_text()
                assert page.request.get(origin + "/api/v1/driver/nudges", headers={"X-Driver-Key": token}).json()[0]["status"] == "ACKNOWLEDGED"
                assert "0 responses queued" in page.locator("#nudgeStatus").inner_text()
                page.get_by_role("button", name="Find nearby chargers").click()
                page.get_by_text("SIMULATED local demonstration chargers", exact=False).wait_for()
                assert page.locator("#chargerList .charger-card").count() == 3
                page.route("**/api/v1/driver/chargers/ocpi/nearby?*", lambda request: request.fulfill(
                    status=200, json={"quality": "PROVIDER", "stations": [{
                        "station_id": "IN-TKE-QA", "name": "Provider QA site", "distance_km": 1.4,
                        "source": "PROVIDER", "evses": [{"evse_uid": "E-1",
                            "reported_status": "AVAILABLE", "availability": "STALE",
                            "connectors": [{"connector_id": "1", "standard": "IEC_62196_T2_COMBO",
                                            "power_kw": 60, "price_per_kwh": None,
                                            "queue_min": None}]}]}]}))
                page.get_by_role("button", name="Find nearby chargers").click()
                page.get_by_text("OCPI provider stations", exact=False).wait_for()
                assert page.locator("#chargerList .charger-card").count() == 1
                assert "STALE" in page.locator("#chargerList").inner_text()
                assert "Price and queue unknown" in page.locator("#chargerList").inner_text()
                page.route("**/api/v1/driver/routes/road", lambda request: request.fulfill(status=200, json={
                    "quality": "PROVIDER", "traffic": {"quality": "UNAVAILABLE", "live": False},
                    "routes": [
                        {"route": {"distance_km": 48, "baseline_eta_min": 60, "source": "PROVIDER"},
                         "steps": [{"type": "turn", "modifier": "right", "name": "Hosur Road", "distance_m": 500}],
                         "geometry": {"type": "LineString", "coordinates": [[77.5946, 12.9716], [77.7, 12.85], [77.8253, 12.7409]]}},
                        {"route": {"distance_km": 52, "baseline_eta_min": 67, "source": "PROVIDER"},
                         "steps": [{"type": "continue", "modifier": "straight", "name": "Main Road", "distance_m": 1200}],
                         "geometry": {"type": "LineString", "coordinates": [[77.5946, 12.9716], [77.75, 12.82], [77.8253, 12.7409]]}},
                    ],
                }))
                page.route("**/api/v1/driver/routes/context", lambda request: request.fulfill(status=200, json={
                    "weather": {"quality": "UNAVAILABLE", "reason": "NOT_CONFIGURED"},
                    "elevation": {"quality": "REPORTED", "source": "CONFIGURED_ELEVATION", "elevation_delta_m": -90.0},
                    "traffic": {"quality": "UNAVAILABLE", "live": False},
                }))
                page.locator("#routeDestination").fill("12.7409, 77.8253")
                page.get_by_role("button", name="Preview route").click()
                page.wait_for_function("document.querySelector('#routeStatus').textContent.includes('48 km')")
                page.wait_for_function("document.querySelector('#routeContext').textContent.includes('-90 m')")
                assert "Weather unavailable" in page.locator("#routeContext").inner_text()
                assert page.locator("#driverRouteOptions button").count() == 2
                assert page.locator("#driverMap path").count() >= 1
                assert "Hosur Road" in page.locator("#driverRouteSteps").inner_text()
                page.locator("#driverRouteOptions button").nth(1).click()
                page.wait_for_function("document.querySelector('#routeStatus').textContent.includes('52 km')")
                assert "Main Road" in page.locator("#driverRouteSteps").inner_text()
                page.locator("#startSoc").fill("80")
                page.get_by_role("button", name="Start trip").click()
                page.get_by_text("ACTIVE", exact=False).first.wait_for()
                page.get_by_role("button", name="Start wait").click()
                page.wait_for_function("document.querySelector('#stopStatus').textContent.includes('WAITING stop')")
                assert page.locator("#completeTrip").is_disabled()
                page.get_by_role("button", name="End stop").click()
                page.wait_for_function("document.querySelector('#stopStatus').textContent.includes('No driver-reported stop is open')")
                page.get_by_role("button", name="Report charging stop").click()
                page.wait_for_function("document.querySelector('#stopStatus').textContent.includes('CHARGING stop')")
                page.get_by_role("button", name="End stop").click()
                page.wait_for_function("document.querySelector('#stopStatus').textContent.includes('No driver-reported stop is open')")
                page.route("**/api/v1/driver/trips/*/gps-batches", lambda request: request.abort())
                # Playwright fixes its initial geolocation timestamp at context creation;
                # refresh the synthetic fix after the recorded consent grant.
                context.set_geolocation({"latitude": 12.9716, "longitude": 77.5946, "accuracy": 8})
                page.get_by_role("button", name="Start GPS").click()
                page.wait_for_function("document.querySelector('#trackingState').textContent.includes('Foreground GPS active')")
                page.get_by_role("button", name="Sync queued GPS").click()
                page.get_by_text("GPS remains queued", exact=False).wait_for()
                page.get_by_role("button", name="Withdraw phone data consent").click()
                page.get_by_text("Trip GPS and motion consent is not granted", exact=False).wait_for()
                assert page.get_by_role("button", name="Start GPS").is_disabled()
                assert page.get_by_role("button", name="Sync queued GPS").is_disabled()
                assert page.request.get(origin + "/api/v1/driver/consent/phone-trip",
                                        headers={"X-Driver-Key": token}).json()["granted"] is False
                page.unroute("**/api/v1/driver/trips/*/gps-batches")
                page.get_by_role("button", name="Agree to trip GPS and motion").click()
                page.get_by_role("button", name="Sync queued GPS").click()
                # Consent re-grant also starts sync. Verify the durable outcome rather
                # than a transient notice that another refresh may replace.
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    trips = page.request.get(origin + "/api/v1/driver/trips", headers={"X-Driver-Key": token}).json()
                    if len(trips) == 1 and trips[0]["phone_gps_sample_count"] >= 1:
                        break
                    time.sleep(.1)
                assert len(trips) == 1 and trips[0]["phone_gps_sample_count"] >= 1
                assert trips[0]["quality"] == "PHONE_GPS_ONLY"
                page.route("**/api/v1/driver/consent/phone-trip", lambda request:
                           request.abort() if request.request.method == "POST" else request.continue_())
                page.get_by_role("button", name="Withdraw phone data consent").click()
                page.get_by_text("Withdrawal pending server confirmation", exact=False).wait_for()
                assert page.get_by_role("button", name="Start GPS").is_disabled()
                page.reload(wait_until="domcontentloaded")
                page.get_by_text("Withdrawal pending server confirmation", exact=False).wait_for()
                assert page.get_by_role("button", name="Start GPS").is_disabled()
                page.unroute("**/api/v1/driver/consent/phone-trip")
                page.evaluate("""() => {
                    const button = document.querySelector('#withdrawPhoneConsent');
                    if (button.textContent === 'Retry withdrawal' && !button.disabled) button.click();
                }""")
                page.get_by_text("Trip GPS and motion consent is not granted", exact=False).wait_for()
                assert page.request.get(origin + "/api/v1/driver/consent/phone-trip",
                                        headers={"X-Driver-Key": token}).json()["granted"] is False
                page.get_by_role("button", name="Agree to trip GPS and motion").click()
                page.get_by_text("Trip GPS and motion consent granted", exact=False).wait_for()
                page.locator("#endSoc").fill("74")
                page.get_by_role("button", name="Complete trip").click()
                page.get_by_text("No active trip", exact=True).wait_for()
                assert page.locator("#soc").inner_text() == "—", "manual SOC changed the BMS display"
                with page.expect_download() as exported:
                    page.get_by_role("button", name="Export phone data").click()
                archive = json.loads(Path(exported.value.path()).read_text(encoding="utf-8"))
                assert archive["total"] >= 1 and archive["samples"][0]["point"]["provider"] == "BROWSER_GEOLOCATION"
                page.route("**/api/v1/driver/trips/*/gps-batches", lambda request: request.abort())
                page.evaluate("""tripId => queuePoint(tripId, {timestamp: new Date().toISOString(),
                    lat: 12.972, lng: 77.595, accuracy: 8, mocked: false,
                    provider: 'BROWSER_GEOLOCATION'})""", trips[0]["trip_id"])
                assert page.evaluate("tripId => pendingPoints(tripId).then(points => points.length)",
                                     trips[0]["trip_id"]) == 1
                page.route("**/api/v1/driver/phone-data", lambda request: request.abort())
                page.once("dialog", lambda dialog: dialog.accept())
                page.get_by_role("button", name="Erase phone data").click()
                page.wait_for_function("document.querySelector('#phoneDataStatus').textContent.includes('Erasure pending server confirmation')")
                assert page.get_by_role("button", name="Sync queued GPS").is_disabled()
                page.reload(wait_until="domcontentloaded")
                page.wait_for_function("document.querySelector('#phoneDataStatus').textContent.includes('Erasure pending server confirmation')")
                assert page.get_by_role("button", name="Sync queued GPS").is_disabled()
                page.unroute("**/api/v1/driver/phone-data")
                page.evaluate("""() => {
                    const button = document.querySelector('#erasePhoneData');
                    if (button.textContent === 'Retry phone data erasure' && !button.disabled) button.click();
                }""")
                page.get_by_text("Cloud phone data erased", exact=False).wait_for()
                assert page.request.get(origin + "/api/v1/driver/phone-data/export",
                                        headers={"X-Driver-Key": token}).json()["total"] == 0
                assert page.evaluate("tripId => pendingPoints(tripId).then(points => points.length)",
                                     trips[0]["trip_id"]) == 0
                page.unroute("**/api/v1/driver/trips/*/gps-batches")
                second = httpx.post(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/nudges", headers=operator,
                    json={"nudge_id": "N-QA-2", "idempotency_key": "nudge-qa-2", "kind": "ROUTE_REVIEW",
                          "message": "Check the next route before departure."})
                assert second.status_code == 200, second.text
                page.reload(wait_until="domcontentloaded")
                second_card = page.locator("#nudgeList .nudge-card").filter(has_text="Check the next route")
                second_card.wait_for()
                page.route("**/api/v1/driver/nudges/N-QA-2/respond", lambda request: request.abort())
                second_card.get_by_role("button", name="Decline").click()
                page.wait_for_function("document.querySelector('#nudgeList').textContent.includes('DECLINED QUEUED')")
                page.locator("#driverKey").fill(other_token)
                page.get_by_role("button", name="Connect", exact=True).click()
                page.wait_for_function("document.querySelector('#vehicleId').textContent.includes('TRICKEE-QA-2')")
                assert "0 responses queued" in page.locator("#nudgeStatus").inner_text()
                assert page.get_by_role("button", name="Sync queued responses").is_disabled()
                assert next(item for item in page.request.get(origin + "/api/v1/driver/nudges",
                    headers={"X-Driver-Key": token}).json() if item["nudge_id"] == "N-QA-2")["status"] == "PENDING"
                # Keep the first driver's response offline until the identity
                # switch completes. Otherwise its legitimate background retry
                # can win the race before Connect changes credentials.
                page.unroute("**/api/v1/driver/nudges/N-QA-2/respond")
                page.locator("#driverKey").fill(token)
                page.get_by_role("button", name="Connect", exact=True).click()
                page.wait_for_function("document.querySelector('#nudgeList').textContent.includes('ROUTE REVIEW · DECLINED')")
                page.wait_for_function("document.querySelector('#nudgeStatus').textContent.includes('0 responses queued')")
                assert next(item for item in page.request.get(origin + "/api/v1/driver/nudges",
                    headers={"X-Driver-Key": token}).json() if item["nudge_id"] == "N-QA-2")["status"] == "DECLINED"
                page.locator("#driverKey").fill("invalid-driver-credential")
                page.get_by_role("button", name="Connect", exact=True).click()
                page.get_by_text("DISCONNECTED", exact=True).wait_for()
                assert page.locator("#driverPlanList .driver-plan-card").count() == 0
                assert page.locator("#nudgeList .nudge-card").count() == 0
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                assert not errors, errors
                context.close()
                browser.close()
            print("Driver browser QA passed: mobile layout, credential, road preview, advisory offline replay and identity isolation, trip stops, GPS recovery, offline phone-data erasure, charger discovery, provenance, completion")
        finally:
            _stop_tree(server)


if __name__ == "__main__":
    main()
