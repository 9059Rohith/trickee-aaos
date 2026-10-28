# Release acceptance status

Audited on 2026-10-12 on branch `feat/aaos-guidance-and-release-hardening`. This table is the input to `python scripts/check_release_gates.py --json`. A full **PASS** requires production acceptance evidence. A local emulator, fixture, or unsigned build cannot satisfy that standard.

For any future production **PASS**, put a unique evidence file in `docs/release-evidence/` and include `artifact=<relative file>; sha256=<64-character digest>; environment=<tested environment>; revision=<40-character commit>` in its table cell. The checker validates the file, digest and current revision. A reviewer must still inspect the acceptance content; this metadata cannot prove that a production field test occurred.

| Gate | Status | Evidence or remaining work |
| --- | --- | --- |
| Simulator and cloud | **PASS locally** | Deterministic vehicle simulation, canonical telemetry, digital twin, durable outbox, signed simulator OTA, cloud API and local backup tests exist. Stale, future, replayed and out-of-order telemetry no longer drives live alerts. Durable tenant-scoped battery, fault, maintenance, charging and OTA notices are locally tested. Fresh Python suite: 256 passed. No network staging acceptance. |
| Map and road routing | **PARTIAL** | Configurable road router, matching, alternatives, place search and route validation pass local tests. Matrix results preserve valid zero duration/distance separately from unreachable cells, so a nearby vehicle is not discarded by dispatch. The native car's route preview and guidance location gate now share a 15 second freshness limit. Production map tiles, router, places, traffic, voice and lane guidance, and field routes are unaccepted. |
| Charging | **PARTIAL** | Demo charger ranking, a configured feed adapter, and an OCPI 2.3.0 read-only Locations adapter with bounded pagination and browser discovery have local tests. Sessions, credit CDRs and later cost corrections can be durably pulled and flagged by an operator sync command. A synthetic charger queue and availability baseline has held-out local evaluation; it is not promoted as a field model. No onboarded production station network, tariff contract, booking, payment or field acceptance. |
| Operator and driver access | **PARTIAL** | Scoped local operator and driver credentials, tenant checks, token verifier, consent and data lifecycle tests exist. Live identity, browser sign-in, hardware enrollment, policy approval and complete audit are unaccepted. |
| Trip and fleet workflows | **PARTIAL** | Trips, stops, daily plans, advisories, episode-deduplicated notifications, manual dispatch proposals, internal assignments, trip replay and service case triage have local tests. A bounded HTTPS partner-order pull now mirrors strictly validated records into a tenant-scoped operator view, with replay/conflict handling and no automatic driver assignment. A durable scoped feed refreshes saved changes in connected browsers and the foreground native phone app; background push, an onboarded partner mapping, external assignment reconciliation and validated field metrics remain missing. |
| Android build | **PASS locally** | All three API 37 Gradle modules passed current-working-tree debug and unsigned release compilation, lint and APK assembly on 2026-10-11 via `scripts/verify_android.ps1 -Offline`. Navigation has 21 passing JVM tests; the native driver has 20; the telemetry `app` module has no JVM test sources. The script sets a short temporary path and the installed Android SDK for this Windows host. Production signing and hosted CI acceptance are absent. |
| AAOS emulator | **PASS on API 35** | The current navigation fixture APK passed route preview, foreground guidance, notice permission and cleanup, location pause/recovery, Stop, destination replacement and reroute outage/recovery with zero template-host errors (`docs/aaos-navigation-evidence/fixture-current/verification.json`). The **current telemetry APK** passed a fresh emulator check after a Car Service lifecycle reconnect fix: VHAL battery at 64% updated the UI and cloud frame, VHAL speed at 36 km/h locked parked setup and uploaded, and the ingest key remained sealed (`docs/aaos-evidence/verification.json`). These checks do not establish a production provider SLA, target API 37 car result or OEM acceptance. |
| Native phone emulator | **PASS on current API 36 APK** | The current APK passed GPS/IMU upload, offline replay, consent and driver isolation, road alternatives, guidance outage, plans, advisories, stops, completed trip, erasure and Maps handoff (`docs/native-driver-evidence/verification.json`). Foreground advisory, dispatch, assignment and service-case updates, resume catch-up and credential revocation passed (`docs/native-driver-evidence/updates-verification.json`). Both current reports identify the same APK SHA-256. Draft restoration and credential non-retention have separate local evidence. Physical-device and field acceptance remain open. |
| Target AAOS image | **OPEN** | No API 37 or OEM car image test, target permissions, host approval or UX restriction report is present. |
| Network staging | **OPEN** | A non-demo Caddy HTTPS Compose template and isolated container check now cover fail-closed startup, file-backed keys, Caddyfile validation, local backup integrity, restored-copy reopening and restart recovery. No network staging deployment with issued TLS certificate, managed secrets, encrypted persistence, monitoring or off-site restore evidence exists. |
| OEM hardware | **OPEN** | No vehicle bench, production BMS/VHAL mapping, OEM safety review or hardware permission acceptance is present. |
| Production release | **OPEN** | No controlled production signatures, hosted CI result for this commit, staging acceptance, field result or distribution approval is present. |

## Exact gate percentages

- **Fully accepted production gates:** `0 / 12 = 0.0%`.
- **Gates with a local-only pass:** `4 / 12 = 33.3%` after rounding to one decimal place. These are not production passes.
- **Partial gates:** `4 / 12 = 33.3%`.
- **Open gates:** `4 / 12 = 33.3%`.

These percentages measure the twelve named gates only. They are **not** a percentage of all requested product features. The release condition is twelve full production passes; current release readiness is **not achieved**.

## Local verification through 2026-10-12

- `python -m pytest -q`: **256 passed** on 2026-10-12 (one upstream Starlette deprecation warning).
- `python -m scripts.qa_dispatch_browser`: **passed** on 2026-10-12, including partner mirror, manual dispatch, internal assignment and service case journeys.
- `python scripts/verify_staging_container.py` in Ubuntu Docker: **passed** on 2026-10-12; non-demo auth, demo-key rejection, Caddyfile validation, local backup integrity, restored-copy reopening and restart recovery.
- Isolated browser runs: fleet map, responsive and reduced-motion states, driver flows, fleet workflows and dispatch flows all passed.
- `python scripts/verify_load_isolated.py`: 400 of 400 authenticated fleet reads passed, p95 0.073 seconds on loopback. This is a local concurrency check, not an external load or capacity result.
- WSL Linux SDK: all three Android modules passed debug/release assembly, lint, and available JVM tests on current source. The telemetry APK then passed live API 35 Car API/VHAL upload checks.
- Local Docker image: demo and non-demo health, readiness and authentication checks passed; SQLite backup integrity and restore drill passed in the native Ubuntu Docker daemon. `docker compose config --quiet` passed.
- `python scripts/build_sbom.py --check`: 24 dependency components passed the existing SBOM check.
- Current API 36 phone APK passed the full trip/offline/consent/erasure workflow and foreground inbox/revocation workflow on the host-GPU emulator. Prior SystemUI ANR attempts are superseded by these current passing runs.
