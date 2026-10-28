# Application Hardening Implementation Plan

> Execution: inline using superpowers:executing-plans, as authorized by the user's request to plan, implement, execute and test without questions.

**Goal:** Make local application operation and verification repeatable, repair reproduced defects, and identify remaining external acceptance precisely.

**Architecture:** Reuse the isolated verification scripts and existing application interfaces. Add an orchestrator for structured, revision-linked verification evidence without treating local checks as production acceptance.

**Tech stack:** Python 3.12, FastAPI, SQLite, Playwright Chromium, PowerShell, Android Gradle/JDK 17.

**Spec:** `docs/superpowers/specs/2026-10-17-application-hardening-design.md`.

## Global constraints

- Preserve user data and pre-existing untracked model artifacts.
- Use isolated fixtures and loopback-only demo access.
- Keep unavailable checks visibly distinct from passes.
- Field, partner, hardware and approval evidence remains external.

## Review focus

- A failing or timed-out check must not yield a successful overall result.
- Missing tooling must produce actionable evidence and a non-successful full result.
- Child processes must not inherit unrelated production/provider configuration.
- Verification must not overwrite application databases or historical evidence silently.
- Launching from another directory must resolve the repository and its Python environment correctly.

## Task 1: Establish current baseline

- [x] Run full Python tests, isolated browser journeys, load/SBOM checks and Android debug/release builds and lint.
- [x] Inspect available emulators/container runtime and attempt available checks.
- [x] Record reproduced failures and their causes before changing implementation.

## Task 2: Repair local operation and add repeatable verification

Files: `scripts/run-local.ps1`, `scripts/verify_application.py`, `tests/test_application_verification.py`, relevant files for reproduced application failures.

- [x] Write failing tests for check success/failure/skip/timeout aggregation and JSON report creation.
- [x] Implement `run_check`, `run_checks` and CLI profiles using subprocess argument lists, bounded timeouts, separate logs and revision/source identity.
- [x] Make the local launcher resolve its root and virtualenv, accept a port, and preserve the caller's location.
- [x] Repair reproduced Windows virtualenv child-server cleanup and asynchronous map-config test race; focused server-shutdown regression failed before the fix and passed afterward.

## Task 3: End-to-end verification and delivery

- [x] Final profile v3 passed 13/13 with unchanged source; Android/container checks passed, and emulator attempts were recorded as unfinished rather than passed.
- [x] Independent review identified Docker cleanup and evidence identity gaps; regressions now cover both fixes, source changes and descendant listeners.
- [x] Fresh evidence and unresolved runtime/production acceptance are documented in `docs/APPLICATION_VERIFICATION_2026-10-17.md` and `docs/EXCELLENCE_ROADMAP.md`.
- [x] Start an isolated seeded local demo and verify health, readiness and served browser clients: `http://127.0.0.1:57556`.

## Execution ledger

- Worked in the supplied existing feature branch to preserve the shared workspace, installed Android setup and pre-existing model artifacts. No remote changes or release actions were requested or performed.
- Used inline execution, with one independent read-only review requested by the execution/review skills. The user's no-questions instruction overrode skill approval pauses.
- Baseline: 256 Python tests passed; four browser workflows passed with system Python; 400/400 load requests passed; SBOM current; all Android debug/release builds and lint passed.
- The virtualenv run exposed child API processes surviving launcher termination on Windows. A real listening-server regression reproduced the failure; process-tree cleanup fixes it. Fixture servers now also clean up failed startup.
- The fleet tile-failure test could race a pending reconnect configuration response. It now supplies the failure through the normal configuration API and passes with the virtualenv.
- Review findings fixed: independent labelled Docker resource cleanup, dependency/configuration fingerprinting, and final source comparison. Live Docker network/volume cleanup passed.
- The first native run failed on UIAutomator timeout. The subsequent run was superseded after finding the fixture shutdown defect; neither counts as a native pass. A final run uses corrected fixtures and separate artifacts.
- An early full profile was superseded while implementing the discovered fixes. Its passing checks are baseline evidence only; `dist/verification/final-20261017/` is the final run.
- Historical screenshot/JSON files touched by baseline scripts were restored; fresh runner artifacts are kept separately under ignored `dist/verification/`.
- Additional runtime finding: routine fleet refresh incremented the same generation used to guard operator actions. A controlled preview/refresh overlap reproduced silent response loss. Separate snapshot-request and identity counters fix the issue; the regression passed afterward. The independent reviewer found no new concurrency/identity defect in this change.
- Driver advisory isolation verification now keeps the original driver's reply offline until the identity switch completes, preventing its legitimate background retry from invalidating the test's premise.
- Final run v1 found these workflow issues and correctly reports non-success/source changes. The replacement final run is `dist/verification/final-v2-20261017/`; it must finish before any final success claim.
- Run v2 was superseded when explicit WSL runtime selection was implemented; its passing results are not a final source-stable acceptance claim.
- Final v3: `--profile full --android-offline --docker-wsl Ubuntu`, 13/13 checks passed, 268 Python tests passed, source unchanged. Exact-lock Python advisory audit also returned 0 known vulnerabilities across 21 dependencies.
- Fresh phone/AAOS runtime acceptance remains incomplete after SystemUI ANR, UIAutomator failures and cold-boot/ADB failures. Those failures and logs were preserved. A clean test AVD was attempted; unrelated emulators were not altered.
- The locally verifiable hardening plan is complete. Whole-product and production acceptance remain open; no top-percentile ranking or full native/vehicle acceptance is claimed.
