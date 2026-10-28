"""Exercise private library in real browsers on an isolated database/server."""

import hashlib
import os
from pathlib import Path
from playwright.sync_api import sync_playwright, expect
from scripts.verify_native_driver import free_port, start_server, stop_server
from scripts.verify_load_isolated import isolated_directory
from trickee.store import CloudStore
from trickee.simulator import demo_profile


def main():
    results = []
    with isolated_directory("trickee-files-") as directory:
        database = Path(directory) / "cloud.db"
        store = CloudStore(str(database))
        store.register(demo_profile(), "QA-FILES")
        token = "private-files-browser-driver-123456789"
        store.issue_driver_credential(
            "TRICKEE-DEMO-1", hashlib.sha256(token.encode()).hexdigest()
        )
        store.close()
        port = free_port()
        server = start_server(database, port)
        try:
            with sync_playwright() as playwright:
                for engine in os.environ.get(
                    "TRICKEE_QA_BROWSERS", "chromium,firefox,webkit"
                ).split(","):
                    browser = getattr(playwright, engine).launch()
                    for path, role in (("/", "operator"), ("/driver", "driver")):
                        context = browser.new_context(
                            viewport={"width": 390, "height": 844},
                            reduced_motion="reduce",
                            accept_downloads=True,
                        )
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        page.goto(f"http://127.0.0.1:{port}{path}")
                        if role == "operator":
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
                        page.locator("#filesLauncher").click()
                        name = f"{engine}-{role}.txt"
                        page.locator("#filesUpload").set_input_files(
                            {
                                "name": name,
                                "mimeType": "text/plain",
                                "buffer": b"private browser content",
                            }
                        )
                        page.locator("#filesUploadForm button").click()
                        row = page.locator("#filesList li").filter(has_text=name)
                        expect(row).to_be_visible()
                        row.get_by_role(
                            "button", name=f"Preview {name}", exact=True
                        ).click()
                        expect(page.locator("#filesPreview")).to_contain_text(
                            "private browser content"
                        )
                        with page.expect_download() as download:
                            row.get_by_role(
                                "button", name=f"Download {name}", exact=True
                            ).click()
                        assert (
                            Path(download.value.path()).read_bytes()
                            == b"private browser content"
                        )
                        page.locator("#filesClose").click()
                        page.locator("#filesLauncher").click()
                        expect(row).to_be_visible()
                        row.get_by_role(
                            "button", name=f"Delete {name}", exact=True
                        ).click()
                        expect(row).to_have_count(0)
                        page.locator("#filesClose").click()
                        page.evaluate("""() => {
                            const originalAPI = window.api;
                            const originalContext = window.trickeeWorkspaceContext;
                            window.__filesContext = originalContext();
                            window.trickeeWorkspaceContext = () => window.__filesContext;
                            let first = true;
                            window.api = (...args) => {
                                if (first && args[0].includes('/workspace/files')) {
                                    first = false;
                                    return new Promise((resolve, reject) => { window.__failOldFileRequest = () => reject(Object.assign(new Error('expired old identity'), {status:401})); });
                                }
                                return originalAPI(...args);
                            };
                        }""")
                        page.locator("#filesLauncher").click()
                        page.evaluate(
                            "window.__filesContext = {...window.__filesContext, generation:window.__filesContext.generation+1}"
                        )
                        expect(page.locator("#filesDialog")).not_to_be_visible()
                        page.locator("#filesLauncher").click()
                        expect(page.locator("#filesStatus")).to_contain_text(
                            "Files loaded"
                            if page.locator("#filesList li").count()
                            else "No files found"
                        )
                        page.evaluate("window.__failOldFileRequest()")
                        expect(page.locator("#filesDialog")).to_be_visible()
                        expect(page.locator("#filesSearch")).to_be_enabled()
                        assert page.evaluate(
                            "document.documentElement.scrollWidth <= innerWidth"
                        )
                        assert not errors, errors
                        context.close()
                        results.append(f"{engine}/{role}: passed")
                        print(results[-1], flush=True)
                    browser.close()
        finally:
            stop_server(server)


if __name__ == "__main__":
    main()
