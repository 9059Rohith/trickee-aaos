# Trickee Vehicle OS

Trickee is an EV intelligence platform built as applications and services around Android Automotive OS. This checkout contains a simulator, cloud API and digital twin, fleet and browser driver consoles, native Android phone companion, AAOS telemetry reader and a separate route-only Car App Library navigation module. It is **not a finished Google Maps equivalent or an approved production vehicle release**.

## Measured status

As of 2026-10-12, **0/12 production release gates (0.0%)** are fully accepted. Four gates pass only in local tests; four are partial and four are open. The current Python suite passed **256/256 tests** and the defined local software matrix remains **20/20**. These are different measures: test pass rate does not equal whole-product completion. Earlier 52–55% overall figures were planning estimates; the requested product has no fixed weighted acceptance denominator, so 80% whole-product completion cannot be verified from those numbers.

Read [the complete audit](docs/PROJECT_AUDIT.md), [release gates](docs/RELEASE_STATUS.md), [architecture](docs/ARCHITECTURE.md), [test evidence](docs/TEST_EVIDENCE.md), [operations](docs/OPERATIONS.md), [API](docs/API.md), [security and privacy](docs/SECURITY_AND_PRIVACY.md), and [licenses and provenance](docs/LICENSES_AND_PROVENANCE.md).

## Personal calendar and assistant

Both browser portals include **Planner & assistant** after connecting: personal calendar views, bounded daily/weekly/monthly recurrence, background-generated in-app reminders, persistent read-only conversations, preferences and JSON export. **Private files** provides bounded authenticated upload/preview/download/deletion. **Jump to** includes server-saved favorites, calendar views and guided onboarding. The native phone companion has a parked-only personal workspace for events, reminders, conversations and preferences. Account access/recovery guidance is available before login at `/account-access`. Password reset/verification remain with the configured identity provider; no reset email or live AI integration is simulated.

See the [latest implementation and verification report](docs/REMAINING_WORK_REPORT_2026-10-17.md), [earlier portal report](docs/PORTAL_COMPLETION_REPORT_2026-10-17.md) and [capability audit](docs/PORTAL_CAPABILITY_AUDIT_2026-10-17.md) for exact scope, evidence and external acceptance requirements.

## Run a local demo

The [2026-10-17 hardening and verification report](docs/APPLICATION_VERIFICATION_2026-10-17.md)
records the earlier **13/13 local full-profile pass**, including **268 Python tests**,
browser workflows, Android builds/lint and isolated container recovery. Fresh
native emulator journeys remain incomplete; this does not change production acceptance.

With Python 3.12 from this folder on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.lock
$env:TRICKEE_DEMO_MODE='1'
.\.venv\Scripts\python -m uvicorn trickee.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the fleet console, `/driver` for the browser driver companion, and `/docs` for the live API schema. The demo operator key is `local-demo-operator-key` and must remain confined to loopback. In a second terminal, run `python -m trickee.simulator --url http://127.0.0.1:8000` to exercise a synthetic vehicle. See [OPERATIONS.md](docs/OPERATIONS.md) for Docker, Android builds, tests, backup and release prerequisites.

## Source layout

| Path | Purpose |
| --- | --- |
| `trickee/` | Domain model, local agent, durable store, cloud API, provider adapters, dispatch, service cases and simulator OTA. |
| `fleet-console/` | Operator web application. |
| `driver-console/` | Browser driver companion. |
| `android-app/app/` | AAOS CarPropertyManager telemetry reader. |
| `android-app/navigation/` | Route-only Navigation-category car app. |
| `android-app/driver/` | Native phone driver app. |
| `aosp/` | Official pinned AOSP reference source fetch/build scripts. |
| `tests/`, `scripts/` | Automated tests and local verification utilities. |
| `docs/` | Current documentation plus preserved local test evidence and SBOM. |
| `dist/` | Debug APKs and unsigned release build artifacts, **not** approved distribution packages. |

Trickee-owned code is Apache-2.0. Keep upstream AOSP and third-party notices with their respective components.
