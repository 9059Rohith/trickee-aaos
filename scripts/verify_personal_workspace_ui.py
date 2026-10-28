"""Exercise actual persisted calendar, reminders, assistant and identity boundaries."""

import hashlib
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from scripts.verify_native_driver import free_port, start_server, stop_server
from trickee.store import CloudStore
from trickee.simulator import demo_profile
from scripts.verify_load_isolated import isolated_directory


def main():
    output = Path(
        os.environ.get("TRICKEE_QA_OUTPUT", "dist/verification/personal-workspace")
    )
    output.mkdir(parents=True, exist_ok=True)
    with isolated_directory("trickee-personal-") as directory:
        database = Path(directory) / "cloud.db"
        store = CloudStore(str(database))
        store.register(demo_profile(), "QA-PERSONAL")
        token = "personal-browser-driver-token-123456789"
        store.issue_driver_credential(
            "TRICKEE-DEMO-1", hashlib.sha256(token.encode()).hexdigest()
        )
        store.close()
        port = free_port()
        server = start_server(database, port)
        results = []
        try:
            with sync_playwright() as p:
                for engine in ("chromium", "firefox", "webkit"):
                    browser = getattr(p, engine).launch()
                    for path, audience in (("/", "operator"), ("/driver", "driver")):
                        context = browser.new_context(
                            viewport={"width": 390, "height": 844},
                            reduced_motion="reduce",
                        )
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(
                            f"http://127.0.0.1:{port}/account-access",
                            wait_until="domcontentloaded",
                        )
                        expect(page.locator("#accountAccessStatus")).to_contain_text(
                            "No reset email has been sent"
                        )
                        page.goto(
                            f"http://127.0.0.1:{port}{path}",
                            wait_until="domcontentloaded",
                        )
                        page.locator("#personalLauncher").click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Connect"
                        )
                        page.locator("#personalClose").click()
                        if audience == "operator":
                            page.locator("#operatorKey").fill("local-demo-operator-key")
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
                        try:
                            expect(page.locator("#personalDaily")).to_contain_text(
                                "authorized vehicle"
                            )
                        except AssertionError:
                            print(
                                "Workspace failure:",
                                page.locator("#personalStatus").inner_text(),
                                errors,
                                page.evaluate("window.trickeeWorkspaceContext?.()"),
                                flush=True,
                            )
                            raise
                        page.locator("#personalEventTitle").fill(
                            f"QA reminder {engine} {audience}"
                        )
                        page.locator("#personalEventForm button[type=submit]").click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Event saved"
                        )
                        expect(page.locator("#personalEvents")).to_contain_text(
                            f"QA reminder {engine} {audience}"
                        )
                        for view in ("month", "week", "day", "agenda"):
                            page.locator("#personalView").select_option(view)
                            expect(page.locator("#personalEvents")).to_contain_text(
                                f"QA reminder {engine} {audience}"
                            )
                        page.locator("#personalSearch").fill(
                            f"QA reminder {engine} {audience}"
                        )
                        page.locator("#personalSearchApply").click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Event search updated"
                        )
                        expect(
                            page.locator("#personalEvents .personal-event")
                        ).to_have_count(1)
                        card = (
                            page.locator("#personalEvents .personal-event")
                            .filter(has_text=f"QA reminder {engine} {audience}")
                            .first
                        )
                        card.get_by_role("button", name="Edit", exact=True).click()
                        page.locator("#personalEventTitle").fill(
                            f"QA reminder {engine} {audience} updated"
                        )
                        page.locator("#personalEventForm button[type=submit]").click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Event saved"
                        )
                        card.get_by_role("button", name="Complete", exact=True).click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Event updated"
                        )
                        card.get_by_role("button", name="Reopen", exact=True).click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Event updated"
                        )
                        page.locator("[data-personal-tab=notifications]").click()
                        expect(page.locator("#personalNotices")).to_contain_text(
                            f"QA reminder {engine} {audience} updated"
                        )
                        page.locator("#personalReadAll").click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "All personal reminders marked read"
                        )
                        notice = (
                            page.locator("#personalNotices .personal-event")
                            .filter(has_text=f"QA reminder {engine} {audience} updated")
                            .first
                        )
                        notice.get_by_role("button", name="Archive", exact=True).click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Reminder archived"
                        )
                        page.locator("[data-personal-tab=assistant]").click()
                        page.locator("#personalNewChat").click()
                        page.locator("#personalQuestion").fill(
                            "What needs attention today?"
                        )
                        if engine == "chromium" and audience == "operator":

                            def lost_reply(route):
                                if route.request.method == "POST":
                                    route.fetch()
                                    route.fulfill(
                                        status=503,
                                        content_type="application/json",
                                        body=json.dumps(
                                            {"detail": "Controlled lost response"}
                                        ),
                                    )
                                else:
                                    route.continue_()

                            page.route(
                                "**/workspace/conversations/*/messages", lost_reply
                            )
                            page.locator("#personalChatForm button").click()
                            expect(page.locator("#personalStatus")).to_contain_text(
                                "Unable to complete"
                            )
                            expect(page.locator("#personalQuestion")).to_have_value(
                                "What needs attention today?"
                            )
                            page.unroute("**/workspace/conversations/*/messages")
                        page.locator("#personalChatForm button").click()
                        expect(page.locator("#personalMessages")).to_contain_text(
                            "calendar item"
                        )
                        expect(
                            page.locator("#personalMessages .personal-message")
                        ).to_have_count(2)
                        page.locator("#personalClose").click()
                        page.reload(wait_until="domcontentloaded")
                        expect(page.locator("#connection.connected")).to_be_visible()
                        page.locator("#personalLauncher").click()
                        page.locator("[data-personal-tab=assistant]").click()
                        expect(
                            page.locator("#personalConversations option")
                        ).not_to_have_count(0)
                        expect(page.locator("#personalMessages")).to_contain_text(
                            "What needs attention today?"
                        )
                        page.locator("[data-personal-tab=settings]").click()
                        page.locator("#personalName").fill("QA profile")
                        page.locator("#personalSettingsForm button").click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Preferences saved"
                        )
                        page.locator("#personalRefresh").click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Workspace loaded"
                        )
                        expect(page.locator("#personalName")).to_have_value(
                            "QA profile"
                        )
                        with page.expect_download() as download:
                            page.locator("#personalExport").click()
                        exported = json.loads(
                            Path(download.value.path()).read_text(encoding="utf-8")
                        )
                        assert exported["events"] and exported["conversations"]
                        if engine == "chromium" and audience == "operator":
                            page.route(
                                "**/operator/workspace/summary",
                                lambda route: route.fulfill(
                                    status=401,
                                    content_type="application/json",
                                    body=json.dumps(
                                        {"detail": "Controlled expired access"}
                                    ),
                                ),
                            )
                            page.locator("#personalRefresh").click()
                            expect(page.locator("#personalStatus")).to_contain_text(
                                "Access expired"
                            )
                            expect(page.locator("#personalDaily")).to_have_text("")
                            expect(page.locator("#personalMessages")).to_have_text("")
                            page.unroute("**/operator/workspace/summary")
                            page.locator("#personalRefresh").click()
                            expect(page.locator("#personalStatus")).to_contain_text(
                                "Workspace loaded"
                            )
                        page.locator("[data-personal-tab=calendar]").click()
                        card = (
                            page.locator("#personalEvents .personal-event")
                            .filter(has_text=f"QA reminder {engine} {audience} updated")
                            .first
                        )
                        page.once("dialog", lambda prompt: prompt.accept())
                        card.get_by_role("button", name="Delete", exact=True).click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Event deleted"
                        )
                        page.locator("#personalSearch").fill("private search text")
                        delayed = []
                        if engine == "chromium" and audience == "operator":
                            page.route(
                                "**/workspace/events?**",
                                lambda route: delayed.append(route),
                            )
                            page.locator("#personalView").select_option("week")
                            expect(page.locator("#personalView")).to_be_disabled()
                            expect(
                                page.locator("#personalConversations")
                            ).to_be_disabled()
                        for width in (320, 390, 768, 1440, 1920):
                            page.set_viewport_size({"width": width, "height": 900})
                            assert page.evaluate(
                                "document.documentElement.scrollWidth <= innerWidth"
                            ), (engine, audience, width)
                        page.screenshot(
                            path=str(output / f"{engine}-{audience}.png"),
                            animations="disabled",
                        )
                        page.locator("#personalClose").click()
                        page.locator(
                            "#disconnectBtn"
                            if audience == "operator"
                            else "#disconnect"
                        ).click()
                        for route in delayed:
                            route.fulfill(
                                status=200,
                                content_type="application/json",
                                body=json.dumps({"items": [], "total": 0}),
                            )
                        page.locator("#personalLauncher").click()
                        expect(page.locator("#personalDaily")).to_have_text("")
                        expect(page.locator("#personalMessages")).to_have_text("")
                        expect(page.locator("#personalSearch")).to_have_value("")
                        expect(
                            page.locator("[data-personal-tab=notifications]")
                        ).to_have_text("Notifications")
                        assert not errors, errors
                        results.append(
                            {"browser": engine, "portal": audience, "passed": True}
                        )
                        context.close()
                        print(
                            f"PASS personal workspace {engine} {audience}", flush=True
                        )
                    browser.close()
        finally:
            stop_server(server)
    (output / "results.json").write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
