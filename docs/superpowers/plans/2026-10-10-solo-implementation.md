# Trickee AAOS Solo Implementation Plan

> **For agentic workers:** Implement this plan task by task with a test and review gate for each task. The user has already selected autonomous execution without follow-up questions.

**Goal:** Finish every software deliverable that can be specified, implemented, and verified without OEM hardware or partner approvals, while keeping simulated and live behavior distinguishable.

**Architecture:** Keep official AOSP/AAOS upstream and add Trickee apps, vehicle adapters, backend services, and web/phone clients as separate components. Data crosses the vehicle boundary through validated, provenance-tagged signal frames. External systems have explicit adapters and contract fixtures; their absence yields unavailable states, never fabricated live results.

**Tech stack:** AOSP/AAOS AIDL VHAL and Car App Library; Java 17 Android apps; Python 3.12/FastAPI/SQLite; current vanilla JS browser consoles; Docker, GitHub Actions, pytest, JUnit, and browser/emulator QA.

**Spec:** The user's original `Pasted text.txt` product brief (sections 0–92), `docs/PROJECT_AUDIT.md`, `docs/RELEASE_STATUS.md`, and `docs/ARCHITECTURE.md`.

## Global constraints

- Use `trcikee-aaos/` as the deliverable folder. Keep AOSP upstream separate from Trickee-owned code and preserve licenses.
- Vehicle commands remain read-only unless authorized OEM-specific safety interfaces and acceptance exist. No direct CAN writes from applications.
- Telemetry carries source, timestamp, quality, and simulation/estimate labels. Unknown physical values remain unknown.
- An AAOS in-motion screen follows Car App Library templates and car app quality rules; the older website's animations are reproduced only where permitted and appropriate.
- Each feature finishes with a passing automated test, a working local user journey when applicable, and updated API/build evidence. A passing unit test alone is insufficient.
- The request for 10,000 **new product-code lines** is tracked from commit `0a16713`. Tests are required but counted separately. Vendor code, generated schemas, documentation, snapshots, scripts and repeated boilerplate do not count. Line count is an additional output constraint, never the acceptance criterion for a feature.
- Baseline on 2026-10-10: 20,293 first-party tracked source/test/script lines; 13,256 are product source and 7,037 are tests/scripts. The current checkout passes 128 Python tests. No new product code is claimed by this plan.

## Research decisions

1. Android's navigation templates provide map surfaces and guided navigation; navigation category and permissions are required. Prefer supported MapWithContent/Navigation templates and test host maneuver, notification, and cluster behavior: https://developer.android.com/training/cars/apps/navigation
2. Car quality criteria restrict AAOS activities and screen animation while driving. Brand palette can remain; web animation parity is not a valid driving UI acceptance test: https://developer.android.com/docs/quality-guidelines/car-app-quality?category=navigation
3. AOSP FakeVehicleHardware is an emulator reference, not a real vehicle bus implementation. An OEM adapter remains a separate contract and test target: https://source.android.com/docs/automotive/vhal/reference-implementation
4. A complete AOSP checkout/build requires a 64-bit Linux host with substantial RAM and disk. A pinned source reference is not an image build: https://source.android.com/docs/setup/start
   This Windows 11 workstation has an Ubuntu WSL distribution but only 31.4 GB RAM and 39.0 GB free on C: as checked on 2026-10-10. It does not meet AOSP's published 64 GB RAM and 400 GB free-disk guidance, so an image build is not an independently verifiable local gate on this host.
5. OSRM Route, Match, and Table are separate contracts. Table returns fastest-route durations/distances between coordinate pairs; use bounded matrix requests and validate unreachable results: https://project-osrm.org/docs/v5.24.0/api/
6. OCPI 2.3.0 defines locations, sessions, CDRs, tariffs, tokens, and commands with versioned modules. Implement a contract-tested adapter; do not claim an onboarded operator from fixtures: https://github.com/ocpi/ocpi
7. AOSP system OTA uses update packages and update_engine/Virtual A/B. The existing signed simulator updater is not an AAOS system updater: https://source.android.com/docs/core/ota

## Review focus

- Stale, missing, implausible, or contradictory vehicle signals must remain marked unusable; Task 2 owns the tests.
- Route gaps, off-road fixes, unreachable matrix cells, and provider outages must not become valid guidance; Task 3 owns the tests.
- Charger feed/session/CDR duplicates or out-of-order updates must not misstate availability or cost; Task 4 owns the tests.
- Revoked identity or withdrawn consent must stop data access/replay across browsers and devices; Tasks 7 and 9 own the tests.
- OTA failure, backend outage, or process restart must preserve last good state and recover predictably; Tasks 2 and 11 own the tests.

## Autonomous work and testable task list

### Task 1: Feature parity inventory and executable acceptance matrix

**Files:** Create `scripts/audit_feature_parity.py`, `tests/test_feature_parity.py`, `docs/FEATURE_MATRIX.md`; read `../trcikee-animated/app`, `../trcikee-animated/components`, `../trcikee-animated/lib`, and the current web/native apps.

**Interface:** `audit_feature_parity(source_root: Path, target_root: Path) -> list[FeatureRow]`, where each row identifies an original feature, target surface, status, and acceptance command.

- [ ] Record the older app's routes, visual tokens, motion behaviors, auth and driver/fleet workflows as traceable rows.
- [ ] Test that every inventoried public feature has a target disposition and every complete row has a runnable check.
- [ ] Run `python -m pytest -q tests/test_feature_parity.py`; review the generated matrix against the brief.

### Task 2: Vehicle signal, Fake VHAL, and offline resilience

**Files:** Create `trickee/signal_quality.py`, `trickee/replay.py`, `tests/test_signal_quality.py`, `tests/test_replay.py`, `aosp/trickee-vhal/` config and host test fixtures; modify `trickee/domain.py`, `trickee/agent.py`, `android-app/app/src/main/java/com/trickee/vehicleos/MainActivity.java`.

**Interface:** `validate_frame(frame: SignalFrame, profile: VehicleProfile, now: datetime) -> SignalAssessment`; `replay_events(events: Iterable[SignalFrame]) -> ReplayResult`.

- [ ] Test invalid/stale/out-of-order signals, restart, long outage, duplicate replay, and explicit simulated provenance.
- [ ] Implement Fake VHAL property profile and a reproducible injection script for the emulator; verify AAOS CarPropertyManager observes the injected values.
- [ ] Run focused pytest, AAOS JVM tests, and `scripts/verify_aaos_recovery.py`; require recorded emulator evidence before closing.

### Task 3: Maps, routing, map matching, and guided AAOS navigation

**Files:** Extend the existing bounded OSRM Table implementation in `trickee/providers.py` and its contract tests; create `android-app/navigation/src/main/java/com/trickee/navigation/GuidanceController.java`, `.../VoiceGuidance.java`, and focused JUnit tests; modify `trickee/route_context.py`, `TrickeeNavigationService.java`, `RouteSurface.java`.

**Interface:** Existing `RoadRouter.table_to_destination(origins, destination)` handles the current dispatch matrix. Extend to a general `table(origins, destinations)` only if a journey requires it. `GuidanceController.update(Route, Fix) -> GuidanceState` remains to be implemented.

- [ ] Contract-test OSRM Route/Match/Table geometry, waypoint order, null cells, limits, timeout, and outage behavior.
- [ ] Add permitted map rendering and real route progress, reroute debounce, maneuvers, voice, notification, day/night, rotary, and distraction checks.
- [ ] Run route provider tests, navigation JUnit/lint/build, and car emulator journeys. Treat a live basemap/license as a separate acceptance dependency.

### Task 4: Charging discovery, session and cost reconciliation

**Files:** Create `trickee/ocpi.py`, `trickee/charging_sessions.py`, `tests/test_ocpi.py`, `tests/test_charging_sessions.py`; modify `trickee/providers.py`, `trickee/store.py`, `trickee/api.py`, and charging panels.

**Interface:** `OcpiClient.locations(since: datetime | None) -> list[OcpiLocation]`; `ChargingSessionStore.apply(event: SessionEvent) -> SessionState`.

- [ ] Contract-test location/status/tariff/session/CDR version and pagination, duplicated updates, cost units, and stale feed labels.
- [ ] Add ranking and a mock CPO end-to-end charging journey; keep booking/payment disabled until a real provider contract exists.
- [ ] Run pytest plus browser and native charging UI checks with a mock OCPI server.

### Task 5: Range, ETA, energy and predictive-health baselines

**Files:** Create `trickee/intelligence/` modules for dataset generation, baseline training, evaluation, registry, and drift; create corresponding `tests/test_intelligence_*.py`; modify `trickee/domain.py`, `trickee/api.py`, and fleet decision panels.

**Interface:** `train_baselines(dataset: TripDataset, seed: int) -> ModelBundle`; `evaluate_model(model: ModelBundle, holdout: TripDataset) -> EvaluationReport`.

- [ ] Generate at least 1,000 seeded synthetic trips across vehicle, driver, route, weather, SOC and charger conditions; label all outputs simulated.
- [ ] Compare physics/rule baselines with residual models using held-out data; test units, leakage, missing values, uncertainty, and rollback.
- [ ] Publish model version/provenance and reproducible metrics, without calling synthetic accuracy a real-world validation.

### Task 6: Copilot and voice with bounded tools

**Files:** Create `trickee/copilot_tools.py`, `trickee/copilot_policy.py`, `tests/test_copilot_policy.py`; add voice lifecycle classes/tests in Android modules; modify `trickee/api.py` and clients.

**Interface:** `authorize_tool(principal: Principal, tool_name: str, args: dict) -> AuthorizedCall`; `answer_from_evidence(question: str, facts: list[Fact]) -> CopilotAnswer`.

- [ ] Test prompt injection, stale facts, cross-tenant data, unsupported commands, offline speech, and interruption.
- [ ] Add read-only range, charger, trip, alert and service-case tools with citations/provenance; add push-to-talk and text fallback.
- [ ] Run policy tests, browser/native voice lifecycle tests, and a no-actuation regression check.

### Task 7: Identity, enrollment, privacy and data lifecycle

**Files:** Create `trickee/identity_flow.py`, `trickee/device_enrollment.py`, related tests; modify `trickee/operator_tokens.py`, `trickee/api.py`, `fleet-console/app.js`, `driver-console/app.js`, and Android credential stores.

**Interface:** `verify_access_token(token: str, expected_audience: str) -> Principal`; `enroll_device(principal: Principal, proof: DeviceProof) -> Enrollment`.

- [ ] Implement browser authorization code/PKCE flow against a configurable OIDC provider and deterministic local test provider.
- [ ] Test token audience, issuer, expiry, replay, role/tenant scope, revocation, recovery, consent withdrawal, export, and erasure.
- [ ] Run pytest, browser auth journeys, Android JVM tests, and revoked/offline replay scenarios.

### Task 8: Dispatch, plans, alerts, service and fleet workflows

**Files:** Create `trickee/order_adapter.py`, `trickee/workflow_metrics.py`, related tests; modify `trickee/dispatch.py`, `trickee/daily_plans.py`, `trickee/service_cases.py`, `trickee/store.py`, `fleet-console/`, and `driver-console/`.

**Interface:** `OrderSource.fetch(cursor: str | None) -> OrderPage`; `reconcile_assignments(events: list[OrderEvent]) -> ReconciliationReport`.

- [ ] Implement a realistic contract fixture and fail-closed partner adapter; test duplicate and conflicting assignments.
- [ ] Finish plan/offer/service state transitions, background delivery hooks, accessibility, measured dashboards, and explicit source freshness.
- [ ] Run workflow pytest, browser journey QA, and multi-client update/outage checks.

### Task 9: Native phone lifecycle and UI parity

**Files:** Split focused classes out of `android-app/driver/src/main/java/com/trickee/driver/MainActivity.java`; create corresponding JUnit tests; modify UI resources and `driver-console/` as needed.

**Interface:** Separate `TripCapture`, `OfflineOutbox`, `AccountSession`, `NotificationSync`, and `DriverPresentation` responsibilities with explicit state persistence.

- [ ] Finish sign-in/recovery, settings, background notification, battery/permission behavior, offline drafts and consent across app restarts.
- [ ] Match existing Trickee palette and motion on phone/web, test reduced motion and contrast, and keep AAOS driving UI within car quality rules.
- [ ] Run Java unit/lint/build tests, phone emulator scripts, and browser visual/journey QA; retain physical-device testing as a later acceptance gate.

### Task 10: OTA, operations, security and release automation

**Files:** Create `trickee/update_campaign.py`, `trickee/observability.py`, tests, AOSP integration scripts, and staging IaC/config; modify `trickee/ota.py`, `Dockerfile`, `docker-compose.yml`, `.github/workflows/ci.yml`, `scripts/build_sbom.py`.

**Interface:** `CampaignManager.advance(campaign_id: str, cohort: str, result: UpdateResult) -> CampaignState`; `collect_health() -> ServiceHealth`.

- [ ] Implement signed app/simulator campaigns, staged rings, rollback recording, recovery, audit logs, metrics and alert rules.
- [ ] Add Android dependency inventory, deterministic CI, TLS staging configuration, migrations, backup/restore and smoke checks.
- [ ] Test failed update, crash/restart, restore, secret rejection, artifact integrity and release rollback; label system OTA as unaccepted until target image verification.

### Task 11: Integrated acceptance and release evidence

**Files:** Modify `scripts/check_release_gates.py`, `docs/FEATURE_MATRIX.md`, `docs/RELEASE_STATUS.md`; create reproducible end-to-end scenario scripts and tests in `scripts/` and `tests/`.

**Interface:** `check_release_gates(evidence_root: Path) -> list[GateResult]`; no PASS without artifact identity, environment and revision.

- [ ] Run full local backend, browser, Android JVM/lint/build, API 35/target emulator, container, outage and backup suites.
- [ ] Capture passing revision, commands, logs/screenshots, and unresolved external dependencies for each feature and gate.
- [ ] Count net new first-party product-code lines against `0a16713`; check the requested 10,000-line target without padding, count tests separately, and separately report feature acceptance.

## Boundaries of independent completion

All eleven tasks have software and local-test portions I can implement alone. Full acceptance of a production vehicle VHAL, commercial charger/payment, live order provider, licensed map/traffic data, real identity tenant, field-calibrated models, signed OEM image/OTA, and on-road safety needs their actual contracts, data, hardware, and approvals. Adapter code and deterministic fixtures do not substitute for those checks.
The full AOSP image build also needs a suitable Linux build machine. The current host can build Android app modules and run available emulators but does not have the published resources for a full checkout/build.

## Execution order

1. Tasks 1–2 establish the feature denominator and trusted vehicle state.
2. Tasks 3–4 build route and charging contracts used by the car/phone experiences.
3. Tasks 5–8 complete intelligence, access and operations against those contracts.
4. Tasks 9–10 harden native UX and release machinery.
5. Task 11 reruns and records every acceptance check; any failed item reopens its owning task.
