"""Audit real browser states using an explicitly supplied local axe-core script.

Example: python -m scripts.verify_accessibility_ui --axe path/to/axe.min.js
The caller controls the audited library version; no CDN code enters the product.
"""
from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright, expect
from scripts.verify_native_driver import free_port, start_server, stop_server


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--axe', type=Path, required=True)
    args = parser.parse_args()
    script = args.axe.read_text(encoding='utf-8')
    output = Path(os.environ.get('TRICKEE_QA_OUTPUT', 'dist/verification/accessibility'))
    output.mkdir(parents=True, exist_ok=True)
    results = []
    with tempfile.TemporaryDirectory(prefix='trickee-a11y-') as directory:
        port = free_port()
        server = start_server(Path(directory) / 'cloud.db', port)
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch()
                page = browser.new_page(viewport={'width': 1440, 'height': 1000}, reduced_motion='reduce')
                for path in ('/', '/driver'):
                    page.goto(f'http://127.0.0.1:{port}{path}', wait_until='load')
                    expect(page.locator('#workspaceJump')).to_be_visible()
                    page.add_script_tag(content=script)
                    for theme in (('dark', 'light') if path == '/' else ('dark',)):
                        if theme == 'light':
                            page.locator('#themeToggle').click()
                        for state in ('page', 'dialog'):
                            if state == 'dialog':
                                page.locator('#workspaceJump').click()
                            result = page.evaluate('''async () => await axe.run(document, {
                                runOnly:{type:'tag',values:['wcag2a','wcag2aa','wcag21aa']}})''')
                            results.append({'path':path,'theme':theme,'state':state,
                                            'axe_version':result['testEngine']['version'],
                                            'violations':result['violations'],
                                            'incomplete':result['incomplete'],
                                            'passed_rule_count':len(result['passes'])})
                            if state == 'dialog':
                                page.keyboard.press('Escape')
                browser.close()
        finally:
            stop_server(server)
    (output / 'audit.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(json.dumps([{key: value for key, value in row.items() if key not in {'violations','incomplete'}} |
                      {'violations':len(row['violations']),'manual_review_groups':len(row['incomplete'])}
                      for row in results], indent=2))
    if any(row['violations'] for row in results):
        raise SystemExit(1)


if __name__ == '__main__':
    main()
