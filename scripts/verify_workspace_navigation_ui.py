"""Persisted favorites, saved views and guided onboarding across browser engines."""

import hashlib
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from scripts.verify_load_isolated import isolated_directory
from scripts.verify_native_driver import free_port, start_server, stop_server
from trickee.simulator import demo_profile
from trickee.store import CloudStore


def main():
    output = Path(os.getenv("TRICKEE_QA_OUTPUT", "dist/verification/navigation-ui"))
    output.mkdir(parents=True, exist_ok=True)
    with isolated_directory("trickee-navigation-") as folder:
        database = Path(folder) / "cloud.db"
        cloud = CloudStore(str(database))
        cloud.register(demo_profile(), "NAV-QA")
        token = "navigation-browser-driver-token-123456789"
        cloud.issue_driver_credential(
            "TRICKEE-DEMO-1", hashlib.sha256(token.encode()).hexdigest()
        )
        cloud.close()
        port = free_port()
        server = start_server(database, port)
        results = []
        try:
            with sync_playwright() as p:
                for engine in ("chromium", "firefox", "webkit"):
                    browser = getattr(p, engine).launch()
                    for path, audience in [("/", "operator"), ("/driver", "driver")]:
                        context = browser.new_context(
                            viewport={"width": 390, "height": 844},
                            reduced_motion="reduce",
                        )
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{port}{path}")
                        page.locator("#workspaceJump").click()
                        expect(page.locator("#navigationStatus")).to_contain_text(
                            "Connect"
                        )
                        expect(page.locator("#navigationPrivate")).to_be_hidden()
                        page.locator("#workspaceClose").click()
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
                        page.locator("#workspaceJump").click()
                        expect(page.locator("#navigationStatus")).to_contain_text(
                            "loaded"
                        )
                        # Each engine starts by clearing prior persisted choices through the real API.
                        base = f"/api/v1/{audience}/workspace/navigation"
                        page.evaluate(
                            """async base => { const prior = await window.api(base); await window.api(base,{method:'PUT',body:JSON.stringify({...prior,favorites:[],saved_views:[],onboarding_step:0,onboarding_completed:false})}); }""",
                            base,
                        )
                        page.locator("#workspaceClose").click()
                        page.locator("#workspaceJump").click()
                        expect(page.locator("#navigationOnboarding")).to_contain_text(
                            "1 of 4"
                        )
                        page.locator("#navigationAdd").click()
                        expect(page.locator("#navigationStatus")).to_contain_text(
                            "saved"
                        )
                        expect(page.locator("#navigationFavorites a")).to_have_count(1)
                        page.locator("#navigationViewName").fill("Connector review")
                        page.locator("#navigationViewLayout").select_option("week")
                        page.locator("#navigationViewQuery").fill("connector")
                        page.locator("#navigationViewForm button").click()
                        expect(page.locator("#navigationViews")).to_contain_text(
                            "Connector review"
                        )
                        for label in ("2 of 4", "3 of 4", "4 of 4", "Guide complete"):
                            page.locator("#navigationNext").click()
                            expect(
                                page.locator("#navigationOnboarding")
                            ).to_contain_text(label)
                        expect(page.locator("#navigationNext")).to_be_disabled()
                        with page.expect_download() as download:
                            page.locator("#navigationExport").click()
                        data = json.loads(Path(download.value.path()).read_text())
                        assert (
                            data["onboarding_completed"] and len(data["favorites"]) == 1
                        )
                        page.locator("#navigationViews").get_by_role(
                            "button", name="Open Connector review"
                        ).click()
                        expect(page.locator("#personalDialog")).to_be_visible()
                        expect(
                            page.locator("[data-personal-page=calendar]")
                        ).to_be_visible()
                        expect(page.locator("#personalView")).to_have_value("week")
                        expect(page.locator("#personalSearch")).to_have_value(
                            "connector"
                        )
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "loaded"
                        )
                        page.locator("#personalClose").click()
                        page.reload()
                        if audience == "driver":
                            expect(page.locator("#vehicleId")).to_contain_text(
                                "TRICKEE-DEMO-1"
                            )
                        else:
                            expect(
                                page.locator("#connection.connected")
                            ).to_be_visible()
                        page.locator("#workspaceJump").click()
                        expect(page.locator("#navigationOnboarding")).to_contain_text(
                            "Guide complete"
                        )
                        expect(page.locator("#navigationFavorites a")).to_have_count(1)
                        expect(page.locator("#navigationViews")).to_contain_text(
                            "Connector review"
                        )
                        for width in (320, 390, 768, 1440):
                            page.set_viewport_size({"width": width, "height": 844})
                            assert page.locator("#workspaceDialog").evaluate(
                                "(el)=>el.scrollWidth <= el.clientWidth + 2"
                            )
                        page.screenshot(path=str(output / f"{engine}-{audience}.png"))
                        # Stale revision is surfaced without overwriting another tab's choices.
                        page.evaluate(
                            """async base => { const prior = await window.api(base); await window.api(base,{method:'PUT',body:JSON.stringify(prior)}); }""",
                            base,
                        )
                        page.locator("#navigationRestart").click()
                        expect(page.locator("#navigationStatus")).to_contain_text(
                            "changed elsewhere"
                        )
                        expect(page.locator("#navigationPrivate")).to_be_hidden()
                        page.locator("#workspaceClose").click()
                        page.locator("#workspaceJump").click()
                        expect(page.locator("#navigationPrivate")).to_be_visible()
                        page.locator("#navigationViews").get_by_role(
                            "button", name="Delete Connector review"
                        ).click()
                        expect(page.locator("#navigationViews")).to_contain_text(
                            "No saved"
                        )
                        page.locator("#navigationFavorites button").click()
                        expect(page.locator("#navigationFavorites")).to_contain_text(
                            "No favorite"
                        )
                        page.locator("#workspaceClose").click()
                        page.locator(
                            "#disconnectBtn"
                            if audience == "operator"
                            else "#disconnect"
                        ).click()
                        page.locator("#workspaceJump").click()
                        expect(page.locator("#navigationPrivate")).to_be_hidden()
                        expect(page.locator("#navigationFavorites")).to_be_empty()
                        assert not errors, errors
                        results.append(
                            {"browser": engine, "portal": audience, "passed": True}
                        )
                        print(f"PASS navigation {engine} {audience}", flush=True)
                        context.close()
                    browser.close()
        finally:
            stop_server(server)
        (output / "results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
