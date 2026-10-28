"""Audit connected personal workspace tabs and public account recovery guidance."""

import argparse
import hashlib
import json
import os
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from scripts.verify_load_isolated import isolated_directory
from scripts.verify_native_driver import free_port, start_server, stop_server
from trickee.store import CloudStore
from trickee.simulator import demo_profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--axe", type=Path, required=True)
    args = parser.parse_args()
    script = args.axe.read_text(encoding="utf-8")
    output = Path(
        os.environ.get("TRICKEE_QA_OUTPUT", "dist/verification/personal-accessibility")
    )
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    with isolated_directory("trickee-personal-a11y-") as directory:
        database = directory / "cloud.db"
        store = CloudStore(str(database))
        store.register(demo_profile(), "QA-A11Y")
        token = "accessibility-driver-credential-123456789"
        store.issue_driver_credential(
            "TRICKEE-DEMO-1", hashlib.sha256(token.encode()).hexdigest()
        )
        store.close()
        port = free_port()
        server = start_server(database, port)
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                page = browser.new_page(
                    viewport={"width": 1440, "height": 1000}, reduced_motion="reduce"
                )
                for path in ("/", "/driver"):
                    page.goto(
                        f"http://127.0.0.1:{port}{path}", wait_until="domcontentloaded"
                    )
                    if path == "/":
                        page.locator("#operatorKey").fill("local-demo-operator-key")
                        page.locator("#connectBtn").click()
                    else:
                        page.locator("#driverKey").fill(token)
                        page.locator("#connectForm button").click()
                    expect(page.locator("#connection.connected")).to_be_visible()
                    page.add_script_tag(content=script)
                    for theme in ("dark", "light") if path == "/" else ("dark",):
                        if theme == "light":
                            page.locator("#themeToggle").click()
                        page.locator("#personalLauncher").click()
                        expect(page.locator("#personalStatus")).to_contain_text(
                            "Workspace loaded"
                        )
                        for tab in (
                            "calendar",
                            "assistant",
                            "notifications",
                            "settings",
                        ):
                            page.locator(f"[data-personal-tab={tab}]").click()
                            expect(
                                page.locator("#personalDialog")
                            ).not_to_have_attribute("aria-busy", "true")
                            result = page.evaluate(
                                "async()=>await axe.run(document.querySelector('#personalDialog'),{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})"
                            )
                            rows.append(
                                {
                                    "path": path,
                                    "theme": theme,
                                    "tab": tab,
                                    "violations": result["violations"],
                                    "incomplete": result["incomplete"],
                                }
                            )
                            page.screenshot(
                                path=str(
                                    output
                                    / f"{path.strip('/') or 'fleet'}-{theme}-{tab}.png"
                                ),
                                animations="disabled",
                            )
                        page.locator("#personalClose").click()
                        for name, launch, target, ready, close in (
                            (
                                "navigation",
                                "#workspaceJump",
                                "#workspaceDialog",
                                "#navigationPrivate",
                                "#workspaceClose",
                            ),
                            (
                                "files",
                                "#filesLauncher",
                                "#filesDialog",
                                "#filesSearch",
                                "#filesClose",
                            ),
                        ):
                            page.locator(launch).click()
                            expect(page.locator(ready)).to_be_visible()
                            expect(page.locator(target)).not_to_have_attribute(
                                "aria-busy", "true"
                            )
                            if name == "files":
                                expect(page.locator("#filesStatus")).to_contain_text(
                                    "files", ignore_case=True
                                )
                            result = page.evaluate(
                                "async selector=>await axe.run(document.querySelector(selector),{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})",
                                target,
                            )
                            rows.append(
                                {
                                    "path": path,
                                    "theme": theme,
                                    "tab": name,
                                    "violations": result["violations"],
                                    "incomplete": result["incomplete"],
                                }
                            )
                            page.screenshot(
                                path=str(
                                    output
                                    / f"{path.strip('/') or 'fleet'}-{theme}-{name}.png"
                                ),
                                animations="disabled",
                            )
                            page.locator(close).click()
                page.goto(f"http://127.0.0.1:{port}/account-access", wait_until="load")
                page.add_script_tag(content=script)
                result = page.evaluate(
                    "async()=>await axe.run(document,{runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})"
                )
                rows.append(
                    {
                        "path": "/account-access",
                        "violations": result["violations"],
                        "incomplete": result["incomplete"],
                    }
                )
                browser.close()
        finally:
            stop_server(server)
    (output / "audit.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    print(
        json.dumps(
            [
                {
                    "path": row["path"],
                    "tab": row.get("tab"),
                    "theme": row.get("theme"),
                    "violations": len(row["violations"]),
                }
                for row in rows
            ],
            indent=2,
        )
    )
    if any(row["violations"] for row in rows):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
