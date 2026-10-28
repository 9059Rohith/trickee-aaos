# Remaining Application Work Implementation Plan

> **For agentic workers:** use the focused independent tasks below, then integrate and verify the whole application.

**Goal:** complete locally implementable pending capabilities with real persistence and tests.
**Architecture:** retain existing authenticated workspace APIs and SQLite; separate files/navigation modules; extend calendar in place; native phone uses existing API configuration.
**Tech Stack:** FastAPI, SQLite, vanilla JavaScript, Android Java, pytest, Playwright.
**Spec:** `docs/superpowers/specs/2026-10-17-remaining-work-design.md`

## Global constraints

- No questions, no destructive reset, no changes to unrelated emulator/services, no invented production status.
- Preserve pending changes and existing authentication/safety boundaries.
- New storage is server-owned and survives backup/restart.
- Browser plugin unavailable: use installed Playwright.

## Review focus

- Identity switching must clear private data and cancel stale writes/results.
- Hostile file content must not execute from the application origin.
- Recurrence must not duplicate data across workers or drift wall time through DST.
- Navigation preferences must reference only legitimate visible sections.
- Native personal workflows must not create driver-distraction interactions in car UI.

## Tasks

- [x] Files: add isolated file module, API tests and browser library; enforce owner/size/type quotas and attachment-safe responses. Red/green malicious input and cross-owner cases.
- [x] Calendar: extend recurrence/occurrence controls and worker/CLI with deterministic DST/month-end/idempotency tests, then browser checks.
- [x] Navigation: add owner-bound favorites/saved views/onboarding module and browser controls; test validation, isolation, persistence and logout clearing.
- [x] Phone: implement safe native personal workspace and deterministic model tests; build/lint existing modules; identify runtime evidence limits.
- [x] Integrate API installers/assets/service worker, backup checks and runner.
- [x] Review changes and fix actual defects; run full stable-source profile, security/content-boundary tests and accessibility/browser matrix; retain results and update pending list honestly.

Final evidence: `dist/verification/remaining-full-20261017/report.json` — 23/23 checks passed, 328 Python and 50 Android JVM tests, unchanged source fingerprint. External acceptance requirements remain documented in `docs/REMAINING_WORK_REPORT_2026-10-17.md`.
