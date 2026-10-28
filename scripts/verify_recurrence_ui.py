"""Isolated three-browser recurrence CRUD for both authenticated portal roles."""

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from scripts.verify_native_driver import free_port, start_server, stop_server
from scripts.verify_load_isolated import isolated_directory
from trickee.store import CloudStore
from trickee.simulator import demo_profile


def main():
    output = Path(os.environ.get("TRICKEE_QA_OUTPUT", "dist/verification/recurrence"))
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with isolated_directory("trickee-recurrence-") as directory:
        db = Path(directory) / "cloud.db"
        store = CloudStore(str(db))
        store.register(demo_profile(), "QA-RECURRENCE")
        token = "recurrence-browser-driver-token-123456789"
        store.issue_driver_credential(
            "TRICKEE-DEMO-1", hashlib.sha256(token.encode()).hexdigest()
        )
        store.close()
        port = free_port()
        server = start_server(db, port)
        try:
            with sync_playwright() as p:
                for engine in ("chromium", "firefox", "webkit"):
                    browser = getattr(p, engine).launch()
                    for path, role in (("/", "operator"), ("/driver", "driver")):
                        context = browser.new_context(
                            viewport={"width": 390, "height": 844},
                            timezone_id="America/New_York",
                        )
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        try:
                            page.goto(
                                f"http://127.0.0.1:{port}{path}",
                                wait_until="domcontentloaded",
                            )
                            if role == "operator":
                                page.locator("#operatorKey").fill(
                                    "local-demo-operator-key"
                                )
                                page.locator("#connectBtn").click()
                                expect(
                                    page.locator("#connection.connected")
                                ).to_be_visible()
                            else:
                                page.locator("#driverKey").fill(token)
                                page.locator("#connectForm button").click()
                                expect(page.locator("#vehicleId")).to_contain_text(
                                    "TRICKEE-DEMO-1"
                                )
                            page.locator("#personalLauncher").click()
                            expect(page.locator("#personalStatus")).to_contain_text(
                                "Workspace loaded"
                            )
                            title = f"Recur {engine} {role}"
                            page.locator("#personalEventTitle").fill(title)
                            month = datetime.now().strftime("%Y-%m")
                            page.locator("#personalEventStart").fill(
                                month + "-01T09:00"
                            )
                            page.locator("#personalEventEnd").fill(month + "-01T10:00")
                            page.locator("#personalRepeat").select_option("DAILY")
                            page.locator("#personalRepeatCount").fill("3")
                            page.locator(
                                "#personalEventForm button[type=submit]"
                            ).click()
                            expect(page.locator("#personalStatus")).to_contain_text(
                                "Event saved"
                            )
                            cards = page.locator(
                                "#personalEvents .personal-event"
                            ).filter(has_text=title)
                            expect(cards).to_have_count(3)
                            cards.first.get_by_role(
                                "button", name="Edit", exact=True
                            ).click()
                            page.locator("#personalEventTitle").fill(
                                title + " exception"
                            )
                            page.locator(
                                "#personalEventForm button[type=submit]"
                            ).click()
                            expect(page.locator("#personalStatus")).to_contain_text(
                                "Event saved"
                            )
                            expect(
                                page.locator("#personalEvents .personal-event").filter(
                                    has_text=title + " exception"
                                )
                            ).to_have_count(1)
                            cards.first.get_by_role(
                                "button", name="Edit", exact=True
                            ).click()
                            page.locator("#personalEditScope").select_option("series")
                            page.locator("#personalRepeat").select_option("WEEKLY")
                            page.locator("#personalEventTitle").fill(title + " rebuilt")
                            page.locator(
                                "#personalEventForm button[type=submit]"
                            ).click()
                            expect(page.locator("#personalStatus")).to_contain_text(
                                "Event saved"
                            )
                            cards = page.locator(
                                "#personalEvents .personal-event"
                            ).filter(has_text=title + " rebuilt")
                            expect(cards).to_have_count(3)
                            page.once("dialog", lambda dialog: dialog.accept())
                            cards.first.get_by_role(
                                "button", name="Delete", exact=True
                            ).click()
                            expect(page.locator("#personalStatus")).to_contain_text(
                                "Event deleted"
                            )
                            expect(cards).to_have_count(2)
                            cards.first.locator("select").select_option("series")
                            page.screenshot(
                                path=str(output / f"{engine}-{role}.png"),
                                full_page=True,
                            )
                            page.once("dialog", lambda dialog: dialog.accept())
                            cards.first.get_by_role(
                                "button", name="Delete", exact=True
                            ).click()
                            expect(page.locator("#personalStatus")).to_contain_text(
                                "Event deleted"
                            )
                            expect(cards).to_have_count(0)
                            assert not errors, errors
                            results.append(
                                {"engine": engine, "role": role, "passed": True}
                            )
                        except Exception as exc:
                            results.append(
                                {
                                    "engine": engine,
                                    "role": role,
                                    "passed": False,
                                    "error": str(exc),
                                    "page_errors": errors,
                                }
                            )
                            raise
                        finally:
                            (output / "results.json").write_text(
                                json.dumps(results, indent=2), encoding="utf-8"
                            )
                            context.close()
                    browser.close()
        finally:
            stop_server(server)
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
