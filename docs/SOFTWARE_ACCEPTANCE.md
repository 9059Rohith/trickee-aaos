# Local software acceptance matrix

Scope: locally runnable Trickee Vehicle OS software in this checkout. This is **not** a percentage of the entire product brief or production readiness. The starting evidence score was **0/100** when this matrix was introduced: no row had yet received a fresh run against the preserved working tree. Existing implementation was present and must not be interpreted as 0% built. Each row is five points, binary; a missing or failed required check earns zero. The target is at least 75 verified points.

Baseline revision: `0a16713b5774151781aaffc0c1e075702cd130f1`, with pre-existing working-tree changes preserved. The exact starting implementation score cannot be reconstructed because that working tree was not snapshotted before additional edits. The final evidence score below uses current source and the stated tests.

| Area (5 points each) | Binary local acceptance check | Result | Evidence |
| --- | --- | --- | --- |
| Vehicle simulation | Canonical signal and scenario tests pass | Pass | `python -m pytest -q` |
| Telemetry and twin | Ingest, quality and digital-twin tests pass | Pass | `python -m pytest -q` |
| Offline agent | Replay, idempotency and outage tests pass | Pass | `python -m pytest -q` |
| Scoped API access | Tenant, operator, driver and vehicle isolation tests pass | Pass | `python -m pytest -q` |
| Trips and stops | Full trip lifecycle and stop tests pass | Pass | `python -m pytest -q`; `python -m scripts.verify_driver_ui` |
| Daily plans and advisories | Plan and nudge lifecycle tests pass | Pass | `python -m pytest -q`; `python -m scripts.verify_fleet_workflows_ui` |
| Road routing | Route, match, matrix and provider failure tests pass | Pass | `python -m pytest -q`; `python -m scripts.verify_ui` |
| Charging feeds | Configured feed and OCPI Location tests pass | Pass | `python -m pytest -q` |
| Charging records | Session and CDR sync, rollback and reconciliation tests pass | Pass | `python -m pytest -q tests/test_ocpi_sync.py tests/test_charging_sessions.py` |
| Dispatch proposals | Durable scoped proposal, response and race tests pass | Pass | `python -m pytest -q`; `python -m scripts.qa_dispatch_browser` |
| Internal assignments | Accepted proposal through driver report and operator reconciliation passes API and browser tests | Pass | `python -m pytest -q tests/test_internal_orders.py`; `python -m scripts.qa_dispatch_browser` |
| Service cases | Driver/operator lifecycle and live-update tests pass | Pass | `python -m pytest -q`; `python -m scripts.qa_dispatch_browser` |
| Fleet browser | Responsive, accessible, workflow and reduced-motion browser checks pass | Pass | `python -m scripts.verify_ui`; `python -m scripts.verify_fleet_workflows_ui` |
| Driver browser | Connected, offline and replay browser checks pass | Pass | `python -m scripts.verify_driver_ui`; `python -m scripts.qa_dispatch_browser` |
| Native phone | Build and current APK emulator journey, offline and revocation checks pass | Pass | Gradle build/lint/unit tests; current API 36 APK passed full trip, GPS/IMU, offline replay, erasure and Maps handoff (`docs/native-driver-evidence/verification.json`), plus foreground updates, assignment reconciliation and revocation (`docs/native-driver-evidence/updates-verification.json`); both reports match APK SHA-256 `212c394b...49813e9` |
| AAOS telemetry | Build and emulator Car API checks pass | Pass | WSL Gradle build/lint; `docs/aaos-evidence/verification.json` (current APK: live battery 64% frame, speed 36 km/h, parked lock and sealed key) |
| AAOS navigation | Build and current APK emulator guidance/recovery checks pass | Pass | Gradle build/lint/unit tests; current API 35 fixture APK passed route preview, guidance, permission denial, pause/recovery, Stop, destination replacement and reroute outage/recovery (`docs/aaos-navigation-evidence/fixture-current/verification.json`) |
| Privacy and audit | Consent, export, erasure and scoped audit-read tests pass | Pass | `python -m pytest -q tests/test_audit_access.py`; full Python suite |
| Operations and packaging | Docker smoke, restore and dependency/SBOM checks pass | Pass | Demo/configured image smoke, image backup smoke, `docker compose config --quiet`, `python scripts/build_sbom.py --check` |
| End-to-end resilience | Fleet/driver/vehicle assignment journey and offline failure recovery pass | Pass | `python -m scripts.qa_dispatch_browser`; `python -m scripts.verify_driver_ui` |

**Current verified score: 100/100 (20/20 rows)**. The full Python suite passed **256 tests** on 2026-10-12, including AOSP VHAL overlay checks, telemetry quality, scoped notification delivery, OCPI charging sync, partner order feed validation, deployment configuration and model evaluation. All three Android modules passed JVM unit tests where present, debug and unsigned release assembly, and debug and release lint through the WSL Linux SDK on 2026-10-11. The current API 35 AAOS telemetry APK passed Car Service startup, battery and speed changes through VHAL, cloud upload, parked lock and sealed-key checks. The current API 35 navigation fixture APK passed the full route guidance and outage journey. The current API 36 phone APK passed the full native journey, foreground updates and credential revocation. The dispatch browser journey passed on 2026-10-12 with a mirrored partner record. The isolated staging cloud passed Caddyfile validation, authentication, local backup integrity and restored-copy reopening after restart. `scripts/check_release_gates.py --json` still reports **0/12 production gates accepted**.

This matrix measures twenty defined local software checks, not a weighted percentage of the entire product. A code-line target cannot substitute for feature acceptance. Local simulation, synthetic emulator routes and mock OCPI collections do not establish live provider, OEM vehicle, field, security approval or production evidence.
