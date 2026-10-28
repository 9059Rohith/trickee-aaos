"""Verify credential, alert and daily-plan workflows against an isolated local cloud."""

import os
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
from scripts.verify_ui import offline_tile
from trickee.simulator import demo_profile
from trickee.store import CloudStore
from scripts.verify_application import _stop_tree


def main() -> None:
    with tempfile.TemporaryDirectory(prefix="trickee-fleet-qa-") as directory:
        database = str(Path(directory) / "fleet.db")
        store = CloudStore(database)
        store.register(demo_profile(), "QA-DRIVER")
        store.close()
        del store
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        origin = f"http://127.0.0.1:{port}"
        environment = os.environ.copy()
        environment.update({"TRICKEE_DEMO_MODE": "1", "TRICKEE_DB": database})
        venv_python = ROOT / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        server = subprocess.Popen(
            [str(venv_python if venv_python.exists() else sys.executable), "-m", "uvicorn", "trickee.api:app",
             "--host", "127.0.0.1", "--port", str(port)],
            cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        try:
            for _ in range(80):
                if server.poll() is not None:
                    raise RuntimeError("local fleet QA server exited")
                try:
                    if httpx.get(origin + "/health", timeout=1).status_code == 200:
                        break
                except httpx.TransportError:
                    time.sleep(0.1)
            else:
                raise RuntimeError("local fleet QA server did not become ready")
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = browser.new_context(viewport={"width": 1365, "height": 900})
                context.route("https://tile.openstreetmap.org/**", lambda request: request.fulfill(
                    status=200, content_type="image/png", body=offline_tile()))
                page = context.new_page()
                errors = []
                page.on("pageerror", lambda error: errors.append(str(error)))
                page.goto(origin, wait_until="networkidle")
                page.locator("#operatorKey").fill("local-demo-operator-key")
                page.get_by_role("button", name="Connect", exact=True).click()
                page.get_by_text("CONNECTED", exact=True).wait_for()

                page.get_by_role("button", name="Issue driver credential").click()
                token = page.locator("#driverCredentialResult code").inner_text()
                assert len(token) >= 32
                page.get_by_role("button", name="Revoke D-1").click()
                page.wait_for_function("document.querySelector('#driverCredentialList').textContent.includes('REVOKED')")
                assert page.request.get(origin + "/api/v1/driver/me", headers={"X-Driver-Key": token}).status == 401

                page.wait_for_function("document.querySelector('#fleetAlerts').textContent.includes('telemetry missing')")
                page.get_by_role("button", name="Acknowledge telemetry missing for TRICKEE-DEMO-1").click()
                page.wait_for_function("document.querySelector('#fleetAlerts').textContent.includes('ACKNOWLEDGED')")
                page.get_by_role("button", name="Resolve telemetry missing for TRICKEE-DEMO-1").click()
                page.get_by_text("No active rule alerts.").wait_for()
                page.get_by_text("Incident history", exact=True).click()
                assert "RESOLVED" in page.locator("#fleetAlertHistory").inner_text()

                service_date = (date.today() + timedelta(days=1)).isoformat()
                page.locator("#dailyDate").fill(service_date)
                page.locator("#dailySoc").fill("80")
                page.route("**/api/v1/places/search**", lambda request: request.fulfill(status=200, json=[
                    {"name": "Origin hub", "latitude": 12.9716, "longitude": 77.5946,
                     "source": "PROVIDER", "quality": "REPORTED", "place_id": "1",
                     "osm_ref": {"osm_type": "N", "osm_id": "101", "category": "amenity"}},
                    {"name": "Office", "latitude": 12.98, "longitude": 77.6,
                     "source": "PROVIDER", "quality": "REPORTED", "place_id": "2",
                     "osm_ref": {"osm_type": "W", "osm_id": "202", "category": "office"}},
                ]))
                page.locator("#dailyPlaceQuery").fill("Office")
                page.locator("#dailyPlaceSearch").click()
                page.locator("#dailyPlaceResults .place-result").nth(1).wait_for()
                page.locator("#dailyPlaceResults .place-result").nth(0).get_by_role("button", name="Use as plan origin").click()
                page.locator("#dailyPlaceTime").fill("09:00")
                page.locator("#dailyPlaceResults .place-result").nth(1).get_by_role("button", name="Add as plan stop").click()
                assert page.locator("#dailyOrigin").input_value() == "12.9716, 77.5946"
                assert "09:00 | Office | 12.98, 77.6" in page.locator("#dailyStops").input_value()
                page.get_by_role("button", name="Save draft").click()
                page.get_by_role("button", name="Edit draft").wait_for()
                first_plan = page.request.get(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/daily-plans",
                                              headers={"X-Operator-Key": "local-demo-operator-key"}).json()[0]
                assert first_plan["origin_osm_ref"] == {"osm_type": "N", "osm_id": "101", "category": "amenity"}
                assert first_plan["stops"][0]["osm_ref"] == {"osm_type": "W", "osm_id": "202", "category": "office"}
                page.get_by_role("button", name="Edit draft").click()
                assert page.locator("#cancelDailyPlanEdit").is_visible()
                page.locator("#dailyStops").fill("09:00 | New office | 12.9800, 77.6000")
                page.get_by_role("button", name="Save changes").click()
                page.wait_for_function("document.querySelector('#dailyPlanDetail').textContent.includes('New office')")
                plans = page.request.get(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/daily-plans",
                                         headers={"X-Operator-Key": "local-demo-operator-key"}).json()
                assert plans[0]["revision"] == 2
                assert plans[0]["origin_osm_ref"] == first_plan["origin_osm_ref"]
                assert plans[0]["stops"][0]["osm_ref"] is None, "manual pin edit retained stale place identity"
                page.get_by_role("button", name="Confirm and calculate").click()
                page.wait_for_function("document.querySelector('#dailyPlanDetail').textContent.includes('Road and energy prediction unavailable')")
                page.locator("#nudgeKind").select_option("ROUTE_REVIEW")
                page.locator("#nudgeMessage").fill("Review tomorrow's confirmed route before departure.")
                page.get_by_role("button", name="Send advisory").click()
                page.wait_for_function("document.querySelector('#nudgeList').textContent.includes('Review tomorrow')")
                nudges = page.request.get(origin + "/api/v1/vehicles/TRICKEE-DEMO-1/nudges",
                                          headers={"X-Operator-Key": "local-demo-operator-key"}).json()
                assert len(nudges) == 1 and nudges[0]["plan_id"] == plans[0]["plan_id"]
                assert nudges[0]["status"] == "PENDING"
                page.locator("#decisionPickup").fill("12.98, 77.60")
                page.locator("#decisionDestination").fill("13.0, 77.62")
                def response_during_refresh(request):
                    response = request.fetch()
                    # A routine fleet refresh must not discard a response to
                    # an operator action submitted under the same identity.
                    page.evaluate("void refresh()")
                    request.fulfill(response=response)
                page.route("**/api/v1/fleet/decisions/preview", response_during_refresh)
                page.get_by_role("button", name="Preview assignment").click()
                page.wait_for_function("document.querySelector('#decisionResult').textContent.includes('road router not configured')")
                page.route("**/api/v1/fleet/daily-operations?*", response_during_refresh)
                page.get_by_role("button", name="Load daily operations").click()
                page.wait_for_function("document.querySelector('#dailyOperationsResult').textContent.includes('0/0 trips completed')")
                assert "savings remain unavailable" in page.locator("#dailyOperationsResult").inner_text()
                page.set_viewport_size({"width": 390, "height": 844})
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                page.set_viewport_size({"width": 320, "height": 740})
                assert not page.evaluate("document.documentElement.scrollWidth > innerWidth + 1")
                page.locator("#operatorAuthType").select_option("token")
                assert page.locator("#mVehicles").inner_text() == "—"
                assert page.evaluate("sessionStorage.getItem('trickeeOperatorKey')") is None
                assert page.locator("#operatorKey").input_value() == ""
                assert page.locator("#connection").inner_text() == "DISCONNECTED"
                token_requests = []
                def inspect_token_request(request):
                    token_requests.append(request.request.headers)
                    request.fulfill(status=401, json={"detail": "invalid token"})
                page.route("**/api/v1/operator/me", inspect_token_request)
                page.locator("#operatorKey").fill("synthetic-access-token")
                page.locator("#connectBtn").click()
                page.get_by_text("Access token is invalid or expired.").wait_for()
                assert token_requests and token_requests[0].get("authorization") == "Bearer synthetic-access-token"
                assert "x-operator-key" not in token_requests[0]
                assert page.evaluate("sessionStorage.getItem('trickeeOperatorKey')") is None
                page.locator("#disconnectBtn").click()
                assert page.locator("#connection").inner_text() == "DISCONNECTED"
                assert not errors, errors
                context.close()
                browser.close()
            print("Fleet browser QA passed: credentials, token header choice, alerts, plan place selection/edit/confirm, decision abstention, daily operations")
        finally:
            _stop_tree(server)
            time.sleep(0.5)


if __name__ == "__main__":
    main()
