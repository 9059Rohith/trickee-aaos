"""Run a live browser smoke test for the operator-to-driver proposal lifecycle."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from trickee.api import create_app
from trickee.simulator import demo_profile
from scripts.verify_load_isolated import isolated_directory
from scripts.verify_application import _stop_tree, verification_environment


OPERATOR_KEY = "qa-dispatch-operator-key-123"
VEHICLE_KEY = "qa-dispatch-vehicle-key-123"
VEHICLE = "TRICKEE-DEMO-1"


def free_port() -> int:
    with socket.socket() as endpoint:
        endpoint.bind(("127.0.0.1", 0))
        return endpoint.getsockname()[1]


def wait_ready(port: int, process: subprocess.Popen, *, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    origin = f"http://127.0.0.1:{port}"
    while time.monotonic() < deadline:
        code = process.poll()
        if code is not None:
            raise RuntimeError(f"local QA server exited with exit code {code}")
        try:
            ready = httpx.get(origin + "/ready", timeout=1)
            if ready.status_code == 200 and httpx.get(
                origin + "/api/v1/vehicles", headers={"X-Operator-Key": OPERATOR_KEY}, timeout=1
            ).status_code == 200:
                return
        except httpx.TransportError:
            pass
        time.sleep(0.1)
    raise TimeoutError(f"local QA server did not become HTTP-ready within {timeout}s")


def start_dispatch_server(database: Path, port: int) -> subprocess.Popen:
    """Keep the browser fixture's Windows event loop out of the test process."""
    environment = verification_environment()
    environment.update({'TRICKEE_DB': str(database), 'TRICKEE_DEMO_MODE': '1',
                        'TRICKEE_OPERATOR_KEY': OPERATOR_KEY, 'TRICKEE_VEHICLE_KEY': VEHICLE_KEY})
    startup_log = database.with_suffix('.startup.log')
    with startup_log.open('w', encoding='utf-8') as log:
        process = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'trickee.api:app',
                                '--host', '127.0.0.1', '--port', str(port)],
                               cwd=Path(__file__).resolve().parent.parent, env=environment,
                               stdout=log, stderr=subprocess.STDOUT)
    try:
        wait_ready(port, process)
    except BaseException as error:
        _stop_tree(process)
        error.add_note(startup_log.read_text(encoding='utf-8', errors='replace')[-6000:])
        raise
    return process


def submit_offer(page, pickup: str, destination: str, message: str) -> None:
    page.locator("#dispatchPickupLabel").fill(pickup)
    page.locator("#dispatchPickup").fill("12.9716, 77.5946")
    page.locator("#dispatchDestinationLabel").fill(destination)
    page.locator("#dispatchDestination").fill("12.9916, 77.6146")
    page.locator("#dispatchMessage").fill(message)
    page.locator("#dispatchReference").fill("BROWSER-QA-1")
    page.locator("#sendDispatch").click()
    page.locator("#dispatchList .nudge-card").filter(has_text=pickup).get_by_text("PENDING", exact=False).wait_for()


def wait_for_case_message(client: TestClient, driver_key: str, fragment: str) -> None:
    deadline = time.monotonic() + 15
    headers = {"X-Driver-Key": driver_key}
    while time.monotonic() < deadline:
        cases = client.get("/api/v1/driver/service-cases", headers=headers).json()
        for case in cases:
            messages = client.get(f"/api/v1/driver/service-cases/{case['case_id']}/messages",
                                  headers=headers).json()
            if any(fragment in message["body"] for message in messages):
                return
        time.sleep(0.1)
    raise AssertionError(f"driver follow-up not stored: {fragment}")


def run() -> None:
    from playwright.sync_api import sync_playwright

    with isolated_directory("trickee-dispatch-browser-") as folder:
        app = create_app(str(Path(folder) / "cloud.db"), OPERATOR_KEY, VEHICLE_KEY)
        client = TestClient(app)
        registration = client.post("/api/v1/vehicles", headers={"X-Operator-Key": OPERATOR_KEY}, json={
            "profile": demo_profile().model_dump(), "driver_id": "DRIVER-QA",
        })
        assert registration.status_code == 200, registration.text
        credential = client.post(f"/api/v1/vehicles/{VEHICLE}/driver-credentials",
                                 headers={"X-Operator-Key": OPERATOR_KEY})
        assert credential.status_code == 200, credential.text
        driver_key = credential.json()["token"]
        observed = datetime.now(timezone.utc).isoformat()
        assert app.state.partner_orders.apply("default", "qa_marketplace", [{
            "id": "QA-PARTNER-1", "updated_at": observed, "status": "CREATED",
            "pickup": {"latitude": 12.9716, "longitude": 77.5946},
            "destination": {"latitude": 12.9916, "longitude": 77.6146},
            "pickup_label": "Partner pickup", "destination_label": "Partner drop",
            "vehicle_id": VEHICLE,
        }])["inserted"] == 1
        charging = app.state.charging_records
        charging.apply_session({"country_code": "IN", "party_id": "TKE", "id": "QA-S-1",
                                "start_date_time": observed, "last_updated": observed,
                                "status": "COMPLETED", "kwh": 4, "currency": "INR",
                                "total_cost": {"before_taxes": 20}})
        charging.apply_cdr({"country_code": "IN", "party_id": "TKE", "id": "QA-C-1",
                            "session_id": "QA-S-1", "start_date_time": observed,
                            "end_date_time": observed, "total_energy": 4,
                            "currency": "INR", "total_cost": {"before_taxes": 20}})
        charging.claim_scope("default", "SESSION", "IN", "TKE", "QA-S-1")
        charging.claim_scope("default", "CDR", "IN", "TKE", "QA-C-1")
        port = free_port()
        server = None
        base = f"http://127.0.0.1:{port}"
        errors: list[str] = []
        try:
            server = start_dispatch_server(Path(folder) / 'cloud.db', port)
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                try:
                    operator = browser.new_context(viewport={"width": 1440, "height": 900},
                                                   reduced_motion="reduce")
                    fleet = operator.new_page()
                    fleet.on("pageerror", lambda error: errors.append(f"fleet: {error}"))
                    fleet.goto(base + "/", wait_until="domcontentloaded")
                    fleet.locator("#operatorKey").fill(OPERATOR_KEY)
                    fleet.locator("#connectBtn").click()
                    try:
                        fleet.locator("#connection.connected").wait_for()
                    except Exception:
                        print("Fleet connection failed:", fleet.locator("#error").inner_text(),
                              "page errors:", errors, flush=True)
                        raise
                    fleet.locator("#dispatchList").get_by_text("No pickup proposals", exact=False).wait_for()
                    fleet.locator("#partnerOrderList .nudge-card").filter(has_text="Partner pickup").get_by_text(
                        "provider mirror unverified", exact=False).wait_for()
                    submit_offer(fleet, "MG Road pickup", "Indiranagar drop", "Check package label with dispatch.")

                    driver = browser.new_context(viewport={"width": 390, "height": 844},
                                                 reduced_motion="reduce")
                    companion = driver.new_page()
                    companion.on("pageerror", lambda error: errors.append(f"driver: {error}"))
                    companion.goto(base + "/driver", wait_until="domcontentloaded")
                    companion.locator("#driverKey").fill(driver_key)
                    companion.locator("#connectForm button[type=submit]").click()
                    companion.locator("#dispatchList").get_by_text("MG Road pickup", exact=False).wait_for()
                    assert companion.locator("#dispatchList").get_by_text("Check package label", exact=False).count() == 1
                    companion.get_by_role("button", name="Accept proposal").click()
                    companion.locator("#dispatchList").get_by_text("ACCEPTED", exact=False).wait_for()
                    fleet.locator("#dispatchList .nudge-card").filter(has_text="MG Road pickup").get_by_text("ACCEPTED", exact=False).wait_for()
                    fleet.get_by_role("button", name="View timeline").first.click()
                    fleet.locator("#dispatchList").get_by_text("CREATED", exact=False).wait_for()
                    fleet.locator("#dispatchList .nudge-card").filter(has_text="MG Road pickup").get_by_role(
                        "button", name="Confirm internal assignment").click()
                    fleet.locator("#orderList .nudge-card").filter(has_text="MG Road pickup").locator(
                        "strong").filter(has_text="ASSIGNED").wait_for()
                    fleet.locator('#orderList .nudge-card').filter(has_text='MG Road pickup').locator('textarea').fill('Cancel because dispatch changed')
                    # Keep the old assignment visible during the driver's state transitions.
                    fleet.route('**/api/v1/vehicles/*/internal-orders', lambda route: route.abort())
                    companion.route("**/api/v1/driver/internal-orders/*/pickup",
                                    lambda route: route.abort())
                    companion.locator("#orderList .nudge-card").filter(has_text="MG Road pickup").get_by_role(
                        "button", name="Report pickup").click()
                    companion.locator("#orderList .nudge-card").filter(has_text="MG Road pickup").locator(
                        "strong").filter(has_text="PHONE QUEUED PICKUP").wait_for()
                    companion.unroute("**/api/v1/driver/internal-orders/*/pickup")
                    companion.locator("#syncOrders").click()
                    companion.locator("#orderList .nudge-card").filter(has_text="MG Road pickup").get_by_text(
                        "PICKED UP", exact=False).wait_for()
                    companion.locator("#orderList .nudge-card").filter(has_text="MG Road pickup").get_by_role(
                        "button", name="Report delivery").click()
                    companion.locator("#orderList .nudge-card").filter(has_text="MG Road pickup").get_by_text(
                        "DELIVERED REPORTED", exact=False).wait_for()
                    fleet.unroute('**/api/v1/vehicles/*/internal-orders')
                    fleet.evaluate('loadDispatch()')
                    fleet_order = fleet.locator("#orderList .nudge-card").filter(has_text="MG Road pickup")
                    fleet_order.get_by_role("button", name="Reconcile driver report").wait_for()
                    assert fleet_order.locator('textarea').input_value() == ''
                    fleet_order.locator("textarea").fill("Confirmed by phone with driver")
                    # A background snapshot must not erase the operator's reconciliation note.
                    fleet_order.locator('textarea').evaluate('async el => {el.focus(); el.setSelectionRange(4, 9); await loadDispatch()}')
                    assert fleet_order.locator('textarea').input_value() == 'Confirmed by phone with driver'
                    # Snapshot the current DOM in one browser turn: background render can
                    # detach a Locator's element between resolution and evaluation.
                    cursor = fleet.evaluate("""() => {
                        const card = [...document.querySelectorAll('#orderList .nudge-card')]
                          .find(card => card.querySelector('strong')?.textContent.includes('MG Road pickup'));
                        const el = card?.querySelector('textarea');
                        return {focused: !!el && document.activeElement === el,
                          start: el?.selectionStart, end: el?.selectionEnd, value: el?.value};
                    }""")
                    assert cursor['focused'] and cursor['start'] == 4 and cursor['end'] == 9, cursor
                    assert cursor['value'] == 'Confirmed by phone with driver', cursor
                    fleet_order.get_by_role("button", name="Reconcile driver report").click()
                    fleet.locator("#orderList .nudge-card").filter(has_text="MG Road pickup").locator(
                        "strong").filter(has_text="RECONCILED").wait_for()
                    companion.locator("#orderList .nudge-card").filter(has_text="MG Road pickup").locator(
                        "strong").filter(has_text="RECONCILED").wait_for()
                    fleet.locator("#loadAudit").click()
                    fleet.locator("#auditList").get_by_text("internal order reconciled", exact=False).wait_for()
                    fleet.locator("#loadChargingRecords").click()
                    fleet.locator("#chargingRecordsList strong").filter(has_text="QA-S-1").wait_for()
                    fleet.locator("#chargingRecordsList").get_by_text("MATCHED", exact=False).first.wait_for()

                    submit_offer(fleet, "Second pickup", "Second drop", "Withdraw after save.")
                    pending_card = fleet.locator("#dispatchList .nudge-card").filter(has_text="Second pickup")
                    pending_card.get_by_role("button", name="Withdraw proposal").click()
                    pending_card.get_by_text("CANCELLED", exact=False).wait_for()
                    companion.locator("#dispatchList").get_by_text("Second pickup", exact=False).wait_for()
                    companion.locator("#dispatchList").get_by_text("CANCELLED", exact=False).wait_for()
                    assert companion.locator("#dispatchList").get_by_text("CANCELLED", exact=False).count() == 1

                    companion.locator("#serviceCategory").select_option("CHARGER_PROBLEM")
                    companion.locator("#servicePriority").select_option("URGENT")
                    companion.locator("#serviceTitleInput").fill("Connector failed at station")
                    companion.locator("#serviceDescription").fill("The connector does not latch; I need another safe charging stop.")
                    companion.locator("#submitServiceCase").click()
                    companion.locator("#serviceList .nudge-card").filter(has_text="Connector failed").get_by_text("OPEN", exact=False).wait_for()
                    fleet.locator("#refreshBtn").click()
                    fleet_case = fleet.locator("#serviceList .nudge-card").filter(has_text="Connector failed")
                    fleet_case.get_by_role("button", name="Acknowledge").click()
                    fleet_case = fleet.locator("#serviceList .nudge-card").filter(has_text="Connector failed")
                    fleet_case.get_by_text("ACKNOWLEDGED", exact=False).wait_for()
                    fleet_case.locator(".service-case-form").first.locator("textarea").fill("Try the charger on the opposite side of the station.")
                    fleet_case.get_by_role("button", name="Send reply").click()
                    wait_for_case_message(client, driver_key, "opposite side")
                    companion.reload(wait_until="domcontentloaded")
                    driver_case = companion.locator("#serviceList .nudge-card").filter(has_text="Connector failed")
                    driver_case.get_by_role("button", name="View replies").click()
                    try:
                        driver_case.get_by_text("opposite side", exact=False).wait_for(timeout=10000)
                    except Exception:
                        cases = client.get("/api/v1/driver/service-cases",
                                           headers={"X-Driver-Key": driver_key}).json()
                        print("case QA diagnostic:", cases)
                        for case in cases:
                            print("messages:", client.get(
                                f"/api/v1/driver/service-cases/{case['case_id']}/messages",
                                headers={"X-Driver-Key": driver_key}).json())
                        print("fleet service status:", fleet.locator("#serviceStatus").inner_text())
                        print("driver service card:", driver_case.inner_text())
                        raise
                    driver_case.locator(".service-reply-form textarea").fill("That connector worked; charging has started.")
                    driver_case.get_by_role("button", name="Send update").click()
                    wait_for_case_message(client, driver_key, "That connector worked")
                    fleet.locator("#refreshBtn").click()
                    fleet_case = fleet.locator("#serviceList .nudge-card").filter(has_text="Connector failed")
                    fleet_case.get_by_role("button", name="View conversation").click()
                    fleet_case.get_by_text("That connector worked", exact=False).wait_for()
                    fleet_case.locator(".service-case-form").last.locator("textarea").fill("Driver confirmed another connector works.")
                    case_id = client.get("/api/v1/driver/service-cases",
                                         headers={"X-Driver-Key": driver_key}).json()[0]["case_id"]
                    refresh_message = client.post(
                        f"/api/v1/driver/service-cases/{case_id}/messages",
                        headers={"X-Driver-Key": driver_key},
                        json={"message_id": "QA-REFRESH", "idempotency_key": "qa-refresh",
                              "body": "Refresh while drafting resolution"},
                    )
                    assert refresh_message.status_code == 200, refresh_message.text
                    fleet_case.get_by_text("Refresh while drafting resolution", exact=False).wait_for()
                    assert fleet_case.locator(".service-case-form").last.locator("textarea").input_value() == "Driver confirmed another connector works."
                    fleet_case.get_by_role("button", name="Resolve case").click()
                    fleet_case = fleet.locator("#serviceList .nudge-card").filter(has_text="Connector failed")
                    fleet_case.locator("strong").filter(has_text="RESOLVED").wait_for()
                    companion.reload(wait_until="domcontentloaded")
                    companion.locator("#serviceList").get_by_text("Driver confirmed another connector works", exact=False).wait_for()
                    assert companion.locator("body").evaluate("element => element.scrollWidth <= window.innerWidth + 1")
                    assert not errors, errors
                    print("browser QA passed: dispatch, internal assignments and service cases, operator and driver, mobile viewport")
                finally:
                    browser.close()
        finally:
            client.close()
            try:
                if server is not None:
                    _stop_tree(server)
            finally:
                app.state.store.close()


if __name__ == "__main__":
    run()
