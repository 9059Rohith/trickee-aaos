"""Real-browser acceptance for shared workspace navigation and fleet discovery."""
from __future__ import annotations

import json
import os
from pathlib import Path
from time import perf_counter

from playwright.sync_api import sync_playwright, expect

from scripts.verify_native_driver import free_port, start_server, stop_server
from trickee.simulator import demo_profile, scenario_frame
from trickee.store import CloudStore
from scripts.verify_ui import offline_tile
import tempfile


def main() -> None:
    output = Path(os.environ.get("TRICKEE_QA_OUTPUT", "dist/verification/workspace-ui"))
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix="trickee-workspace-") as directory:
        port = free_port()
        database = Path(directory) / "cloud.db"
        store = CloudStore(str(database))
        store.register(demo_profile(), "QA-WORKSPACE")
        store.register(demo_profile().model_copy(update={"vehicle_id": "TRICKEE-QA-2"}), "QA-WORKSPACE-2")
        store.close()
        server = start_server(database, port)
        try:
            with sync_playwright() as playwright:
                for engine in ("chromium", "firefox", "webkit"):
                    browser = getattr(playwright, engine).launch(headless=True)
                    for path, name in (("/", "fleet"), ("/driver", "driver")):
                        context = browser.new_context(viewport={'width':1440,'height':900})
                        context.route('https://tile.openstreetmap.org/**', lambda route: route.fulfill(status=200, content_type='image/png', body=offline_tile()))
                        page = context.new_page()
                        page.add_init_script("""window.workspaceCLS = 0; try {new PerformanceObserver(list => {
                            for (const entry of list.getEntries()) if (!entry.hadRecentInput) window.workspaceCLS += entry.value;
                        }).observe({type:'layout-shift',buffered:true});} catch {}""")
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        started = perf_counter()
                        page.goto(f"http://127.0.0.1:{port}{path}", wait_until="domcontentloaded")
                        expect(page.locator("#workspaceJump")).to_be_visible()
                        page.evaluate('document.fonts.ready')
                        page.evaluate('async () => {await new Promise(requestAnimationFrame); await new Promise(requestAnimationFrame)}')
                        initial_cls = page.evaluate('window.workspaceCLS')
                        if engine == 'chromium':
                            assert initial_cls < .1, (name, initial_cls)
                        page.locator('#workspaceJump').click()
                        expect(page.locator('#workspaceLinks a[href="#auditWorkspace"]')).not_to_be_visible()
                        page.keyboard.press('Escape')
                        if name == "fleet":
                            page.locator('#operatorKey').fill('local-demo-operator-key')
                            page.locator('#connectBtn').click()
                            expect(page.locator('#vehicleList .vehicle-row')).to_have_count(2)
                            page.locator('#vehicleSearch').fill('qa-2')
                            expect(page.locator('#vehicleList .vehicle-row')).to_have_count(1)
                            page.locator('#vehicleSearch').fill('not-a-vehicle')
                            expect(page.locator('#vehicleList')).to_contain_text('No vehicles match')
                            page.locator('#clearVehicleFilters').click()
                            page.locator('#workspaceJump').click()
                            expect(page.locator('#workspaceLinks a[href="#auditWorkspace"]')).to_be_visible()
                            page.keyboard.press('Escape')
                            expect(page.locator('#vehicleList .vehicle-row')).to_have_count(2)
                            page.locator('#vehicleStateFilter').select_option('CHARGING')
                            expect(page.locator('#vehicleList .vehicle-row')).to_have_count(0)
                            page.locator('#clearVehicleFilters').click()
                            page.locator('#vehicleStateFilter').select_option('UNKNOWN')
                            expect(page.locator('#vehicleList .vehicle-row')).to_have_count(2)
                            page.locator('#clearVehicleFilters').click()
                        for width in (320, 375, 390, 430, 768, 1024, 1280, 1440, 1920):
                            page.set_viewport_size({"width": width, "height": 900})
                            page.locator("#workspaceJump").click()
                            expect(page.locator("#workspaceDialog")).to_be_visible()
                            assert page.evaluate("document.documentElement.scrollWidth <= innerWidth"), (engine, name, width)
                            page.screenshot(path=str(output / f"{engine}-{name}-{width}.png"), animations='disabled') if width in (390, 1440) else None
                            page.keyboard.press("Escape")
                            expect(page.locator("#workspaceJump")).to_be_focused()
                        page.keyboard.press("Control+k")
                        expect(page.locator("#workspaceSearch")).to_be_focused()
                        # Repeated shortcut activation must leave one coherent dialog state.
                        for _ in range(4):
                            page.keyboard.press("Control+k")
                        expect(page.locator("#workspaceDialog")).to_be_visible()
                        page.keyboard.press("Tab")
                        assert page.evaluate("document.querySelector('#workspaceDialog').contains(document.activeElement)")
                        page.locator('#workspaceSearch').focus()
                        page.locator("#workspaceSearch").fill("zzzz-no-section")
                        expect(page.locator("#workspaceEmpty")).to_be_visible()
                        page.locator("#workspaceSearch").fill("trip")
                        link = page.locator("#workspaceLinks a:visible").first
                        target = link.get_attribute("href")
                        link.click()
                        expect(page.locator("#workspaceDialog")).not_to_be_visible()
                        assert page.evaluate("location.hash") == target
                        page.locator("#workspaceJump").click()
                        page.locator("#workspaceMotion").check()
                        assert page.locator("html").get_attribute("data-motion") == "reduced"
                        if name == 'fleet':
                            assert page.locator('.hero-route path').first.evaluate('el => getComputedStyle(el).strokeDashoffset') == '0px'
                        page.reload(wait_until="domcontentloaded")
                        expect(page.locator('#workspaceJump')).to_be_visible()
                        assert page.locator("html").get_attribute("data-motion") == "reduced"
                        page.emulate_media(reduced_motion="reduce")
                        page.locator("#workspaceJump").click()
                        assert page.locator("#workspaceMotion").is_disabled()
                        assert page.locator("#workspaceMotion").is_checked()
                        assert page.locator('#workspaceDialog').evaluate("el => getComputedStyle(el).animationName") == 'none'
                        page.keyboard.press("Escape")
                        if name == 'fleet':
                            page.emulate_media(reduced_motion='no-preference')
                            page.locator('#themeToggle').click()
                            assert page.locator('html').get_attribute('data-theme') == 'light'
                            page.locator('#workspaceJump').click()
                            page.screenshot(path=str(output / f'{engine}-fleet-light.png'), animations='disabled')
                            page.keyboard.press('Escape')
                        if name == 'driver' and engine == 'chromium':
                            page.wait_for_function('navigator.serviceWorker.controller !== null')
                            context.set_offline(True)
                            page.reload(wait_until='domcontentloaded')
                            page.locator('#workspaceJump').click()
                            expect(page.locator('#workspaceDialog')).to_be_visible()
                            expect(page.locator('#workspaceLinks a:visible').first).to_be_visible()
                            context.set_offline(False)
                        assert not errors, errors
                        timing = page.evaluate("""() => { const n = performance.getEntriesByType('navigation')[0]; return {dom_content_loaded_ms:n.domContentLoadedEventEnd,load_ms:n.loadEventEnd}; }""")
                        results.append({"browser": engine, "surface": name, "passed": True, "initial_cls": initial_cls if engine == 'chromium' else None, "navigation_timing": timing,
                                        "duration_seconds": round(perf_counter() - started, 3)})
                        context.close()
                        print(f'PASS {engine} {name}', flush=True)
                    touch = browser.new_context(viewport={"width": 390, "height": 844}, has_touch=True)
                    touch.add_init_script("Object.defineProperty(window, 'localStorage', {get(){throw new Error('storage blocked')}});")
                    # Shared shell must remain useful without persistent storage.
                    page = touch.new_page()
                    page.goto(f'http://127.0.0.1:{port}/driver', wait_until='domcontentloaded')
                    page.locator('#workspaceJump').tap()
                    expect(page.locator('#workspaceDialog')).to_be_visible()
                    page.locator('#workspaceMotion').check()
                    assert page.locator('html').get_attribute('data-motion') == 'reduced'
                    page.locator('#workspaceClose').tap()
                    expect(page.locator('#workspaceDialog')).not_to_be_visible()
                    results.append({'browser': engine, 'surface': 'driver-touch-storage-blocked', 'passed': True})
                    touch.close()
                    browser.close()
                browser = playwright.chromium.launch(headless=True)
                page = browser.new_page(reduced_motion='reduce')
                page.goto(f'http://127.0.0.1:{port}/', wait_until='domcontentloaded')
                page.locator('#operatorKey').fill('local-demo-operator-key')
                page.locator('#connectBtn').click()
                expect(page.locator('#vehicleList .vehicle-row')).to_have_count(2)
                expect(page.locator('.charge-track')).to_have_attribute('role', 'group')
                for sequence, charge in ((1, 0), (2, 72)):
                    store = CloudStore(str(database))
                    store.ingest([scenario_frame('SCENARIO_001', sequence).model_copy(update={'soc_pct': charge})])
                    store.close()
                    page.locator('#refreshBtn').click()
                    expect(page.locator('.charge-track')).to_have_attribute('role', 'meter')
                    expect(page.locator('.charge-track')).to_have_attribute('aria-valuenow', str(charge))
                page.locator('#disconnectBtn').click()
                expect(page.locator('.charge-track')).to_have_attribute('role', 'group')
                assert page.locator('.charge-track').get_attribute('aria-valuenow') is None
                page.locator('#workspaceJump').click()
                expect(page.locator('#workspaceLinks a[href="#auditWorkspace"]')).not_to_be_visible()
                results.append({'browser':'chromium','surface':'battery-and-disconnect-transitions','passed':True})
                browser.close()
        finally:
            stop_server(server)
    (output / "results.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
