# Application verification — 2026-10-17

The final local full profile passed **13/13 checks** with **no source change during the run**. This establishes the defined local checks below, not whole-product or production acceptance. Fresh phone/AAOS runtime acceptance remains incomplete.

## Implemented changes

- Fixed a fleet UI race: routine refreshes invalidated in-flight assignment previews and daily-operation responses. Identity changes and overlapping snapshot requests now use separate counters. Credential changes still invalidate pending action results. A controlled browser overlap failed before the fix and passed afterward; both preview and daily-operation overlap are exercised in the final browser run.
- Fixed Windows virtualenv fixture shutdown: stopping the Python launcher left child API processes running and temporary databases locked. A real-server regression reproduced the problem; the verification scripts now stop their process trees and clean up failed startup.
- Removed two browser-test races: map-failure configuration now arrives through the actual configuration API; queued driver replies remain offline until the intended identity switch completes.
- Added `scripts/verify_application.py`: bounded checks, individual logs, isolated artifacts, explicit failed/skipped/timeout outcomes, initial/final source fingerprints, independent labelled Docker cleanup and optional native WSL Docker execution.
- Made the local launcher resolve its repository and virtualenv from any working directory and accept a port. CI now uses the unified web profile and checks Android release lint as well as debug lint.

## Fresh final results

| Check | Result and scope |
| --- | --- |
| Python | **268 passed**, one upstream Starlette/httpx deprecation warning. Includes 12 verification-runner regressions. |
| Browser clients | Fleet desktop/mobile/theme/map/reduced-motion and credential isolation, driver offline/consent/erasure/trips, fleet planning/alerts/refresh overlaps, and dispatch/assignment/service cases all passed in Chromium. Browser plugin was unavailable; existing Playwright tooling was used. |
| JavaScript | Syntax checks passed for fleet app/map and driver app/service worker. |
| Concurrent reads | **400/400 HTTP 200**, concurrency 24; local p95 **1.066 seconds**, maximum **1.143 seconds** under this host's test load. This is not external capacity evidence. |
| Android | All three modules passed debug/release assembly and lint. Existing JVM result files contain 43 passing tests across driver/navigation; telemetry has no JVM test sources. No production signing is claimed. |
| Staging container | Native Ubuntu WSL Docker passed non-demo auth, demo-key rejection, Caddy configuration, backup integrity, restored-copy reopening and restart recovery. Independent cleanup used the same selected engine. No public TLS issuance/deployment was performed. |
| Dependency inventory | SBOM check passed for 24 server/web components. |
| Dependency advisory audit | Exact pinned Python lockfile audit: 21 dependencies, **0 known vulnerabilities reported**. This excludes Android/web dependency vulnerability assessment and independent security review. |
| Live local server | Health/readiness and fleet, driver and API documentation routes returned HTTP 200 at `http://127.0.0.1:57556`. |

The machine-readable final report, individual logs and browser screenshots are in
`dist/verification/final-v3-20261017/`. This directory is deliberately git-ignored.
Source fingerprint:
`5171efb6003324c2de6a79583e8d0b96766e0c417440bba643af703c4ec40533`.
The checkout is intentionally recorded as dirty; no hosted CI run or release commit is claimed.

## Reproduction

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.lock -r requirements-ui.in
.\.venv\Scripts\python -m playwright install chromium
.\.venv\Scripts\python scripts/verify_application.py --profile full --android-offline --docker-wsl Ubuntu
.\scripts\run-local.ps1 -Port 8000
```

The full command assumes the existing Ubuntu native Docker daemon is running and Android dependencies are cached. Use `--profile web` for backend/browser checks alone. Omit `--docker-wsl Ubuntu` to select a working Windows Docker CLI/engine. The final run used WSL because Docker Desktop became unavailable during runtime troubleshooting; that failed attempt is preserved rather than counted as a pass.

## Remaining runtime and production limits

Fresh native phone attempts did **not** complete. A SystemUI ANR was captured over the driver app's initial screen, followed by UIAutomator hierarchy failures. Host and software graphics, another existing test AVD, and a newly created clean API 36.1 AVD were attempted; later devices remained offline or did not complete boot. Evidence is under `dist/verification/native-verified-run-20261017/` and the final report directory. These are environment failures/unfinished journeys, not fresh native passes or proof that every native flow works.

The API 35 AAOS emulator also failed to reach a usable boot for a fresh telemetry/navigation journey. Boot logs are preserved with the final report. Existing historical AAOS/phone evidence was preserved and must not be represented as a new pass. An API 37 image directory was present but was not accepted as a valid package by the installed AVD tooling; an API 37 runtime was not verified. Other unrelated emulators were left untouched.

Production gates remain **0/12 fully accepted**. Real vehicle/BMS mapping, physical phones, licensed/live providers and identity, field prediction accuracy, a real fleet pilot, public staging, independent security/safety approval, signing and distribution remain external acceptance work. The concrete deliverables and evidence requirements are in [EXCELLENCE_ROADMAP.md](EXCELLENCE_ROADMAP.md).

## Review and preservation

Independent read-only review identified Docker resource cleanup and evidence-identity defects; both were fixed and tested. A second review of the newly discovered fleet refresh bug found no concrete new identity/concurrency defect. Delayed action results after credential switching and newest-first overlapping snapshots were reviewed by inspection; those exact delayed-response permutations do not yet have dedicated browser assertions.

Pre-existing databases and untracked model artifacts were preserved. Historical screenshots touched by baseline scripts were restored; final evidence lives in separate run directories. Superseded/failed runs remain visibly unsuccessful. No remote, deployment, production signature or external message was created.
