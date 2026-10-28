# Verification evidence and limits

## Current and preserved local checks

| Check | Result | Scope |
| --- | --- | --- |
| `python -m pytest -q` | **256 passed**, one Starlette/httpx deprecation warning, 77.79 s on 2026-10-12 | Python unit/API/integration tests in the current checkout, including staging configuration and partner-order contracts. |
| `node --check` on both browser clients | **Passed** | JavaScript syntax. |
| `python -m scripts.qa_dispatch_browser` | **Passed on 2026-10-12** | Isolated Chromium partner mirror, operator/driver dispatch and assignment, automatic updates and service-case draft preservation. |
| `python scripts/verify_staging_container.py` under Ubuntu Docker | **Passed on 2026-10-12** | Isolated non-demo cloud, Caddyfile validation, file-backed keys, local backup integrity, restored-copy reopening and restart recovery; no public TLS issuance. |
| `python scripts/verify_driver_ui.py` | **Passed** | Isolated Chromium driver workflow: consent recovery, GPS queue, trip stops, advisory identity isolation and phone-data erasure. |
| `python scripts/verify_fleet_workflows_ui.py` | **Passed** | Isolated Chromium credentials, alerts, plans and daily operations. |
| `python scripts/verify_ui_isolated.py` | **Passed** | Isolated fleet visual/interaction checks at desktop and mobile widths, theme, reduced motion and credential isolation. |
| `python -m scripts.qa_native_updates` | **Passed on API 36 phone emulator, 2026-10-08** | New advisory, dispatch proposal and service case appeared without manual refresh; a backgrounded app caught up on resume; revoked driver credential was removed from native storage. The script uses an isolated backend and resets only the emulator's test app. |
| `python -u scripts/verify_native_driver.py` | **Passed on API 36 phone emulator, 2026-10-08** | Route alternatives/guidance and outage fallback, charger states, offline advisory conflict/replay, GPS and IMU upload/replay, consent, driver isolation, trip stops, erasure and Maps handoff. Fresh screenshots and result JSON are in `docs/native-driver-evidence/`. |
| `:driver:testDebugUnitTest :driver:assembleDebug :driver:lintDebug` | **Passed, 2026-10-08** | 20 Android JVM tests with zero failures, debug APK and lint with installed API 37 SDK. Debug APK SHA-256: `F852070172E0DC88905BAF923DA52AC8A0052BC0EC74DF713F899DE24542531F`. |
| `python scripts/check_release_gates.py --json` | Exit 1 as designed on 2026-10-12; `0` production passes, `4` local passes, `4` partial, `4` open | The current twelve-gate release table. |
| Git inspection | Branch `feat/aaos-guidance-and-release-hardening`; no configured remote | A hosted CI run for this revision cannot be claimed. |
| AOSP source inspection | Manifest, Car Service and hardware-interface revisions match [ARCHITECTURE.md](ARCHITECTURE.md) | This proves source pinning, not a full image build. |

## Evidence already present before this audit

- API 35 AAOS telemetry and navigation emulator screenshots and verification JSON: `docs/aaos-evidence/`, `docs/aaos-navigation-evidence/`.
- API 36 native phone screenshots and verification JSON: `docs/native-driver-evidence/`. Three tracked screenshots and the result JSON were refreshed by the full emulator run; five unrelated untracked local screenshots were left untouched.
- Browser UI screenshots and result JSON: `docs/ui-parity/`.
- Local load result: `docs/load/verification.json`.
- Server/web SBOM and dependency audit: `docs/SBOM.cdx.json`, `docs/security-audit.json`.
- APK files and their recorded hashes: `dist/`.

The evidence directories mix historical records with the fresh API 36 phone run above. A green emulator run does not prove physical-device, target car-host or field acceptance. No hosted CI run for this revision exists.

## Known evidence gaps

No full AOSP image build, API 37/OEM car-host run, physical-device field run, contracted service staging result, production security/safety approval, controlled signing, or release distribution evidence is present. No real traffic, energy, order or charger-session dataset validates the requested intelligence claims. These gaps remain open regardless of a green local test suite.
