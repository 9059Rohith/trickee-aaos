# Remaining feature work and verification

This report supersedes the unfinished-local-feature list in the earlier portal report. Local software verification remains distinct from external production and hardware acceptance.

## Implemented capabilities

| Capability | Result and bounds |
| --- | --- |
| Private files | Both portals: upload, list/search/paging, image/text preview, download, deletion. SQLite BLOB persistence and authenticated ownership. PNG/JPEG fully decoded and re-encoded without source metadata; restricted uncompressed PDFs download only; plain UTF-8 TXT supported. 2 MiB/file, 4M image pixels, 20 MiB/40 files per owner, rate/history limits. Unsupported files rejected; no external antivirus claim or event-specific attachment linkage. |
| Recurrence | Daily/weekly/monthly, interval and count/until; occurrence vs entire-series editing/deletion, UTC storage with IANA wall time, DST/month-end policies, event/series revisions and atomic quotas. Max 366 occurrences/series and 2,000 owner events. Series rebuild explicitly replaces exceptions from submitted starting dates/rule. |
| Background reminders | API lifespan worker generates durable in-app notices independently of browser refresh; stoppable lifecycle, readiness failure on worker error/exit and concurrent/repeated-worker deduplication. Bounded standalone CLI included. External email/SMS/push is not configured. |
| Favorites/saved views | Server-persisted favorite sections and named calendar layout/search views; owner isolation, validation, revision conflicts, export and logout clearing. Up to 20 favorites/10 saved views. Links target actual currently visible sections. |
| Onboarding | Four role-specific steps with persisted progress/completion and restart, inside Jump to / Your workspace. |
| Native phone workspace | Calendar create/edit/complete/delete and paging; reminder read/archive; persistent conversation creation/history/send/delete; summary and editable name/timezone/reminders. Non-exported native Activity, existing secure credentials, background network calls, lifecycle/identity cancellation, fresh parked-state guards and private-UI watchdog. Native recurrence edits target a single occurrence; series authoring remains in browser. Files/favorites/onboarding are browser features. No free-form forms inserted into AAOS driving UI. |

## Defects found and repaired

- Global response middleware replaced the file-preview sandbox. Integrated RED/GREEN regression now verifies the preserved sandbox plus frame/object/base policy.
- Old-identity file access failures could clear newer UI. Clearing now checks request identity/epoch; a delayed-failure browser regression verifies it.
- File controls created during pending work now inherit busy state, and cancellation restores controls.
- Native busy work could delay vehicle-state checks. Forms hide during requests and an independent watchdog clears stale parked-state views.
- Calendar deleted-create retry remains 404; differing-content retry and stale event/series edits are rejected.

## Verification evidence

Focused suite: 51 Python tests passed before the final integrated real-auth test was added; the expanded three-test integration suite subsequently passed. File, recurrence and navigation browser suites each passed both portals in Chromium/Firefox/WebKit, and the file suite also tests delayed old-identity failure. The final full profile repeats these workflows.

Accessibility: **19 states, zero automated violations**, with incomplete/manual items retained in `dist/verification/remaining-accessibility/audit.json`. Browser plugin unavailable; installed Playwright used. Desktop/light/dark and mobile screenshots inspected. This does not establish physical-device or screen-reader conformance.

Dependency audit: **22 pinned Python dependencies, zero known vulnerabilities**, in `dist/verification/remaining-dependency-audit.json`. Pillow 12.3.0 added for bounded image validation/normalization; both locks and server/web SBOM regenerated. Requirements input corrected to match already pinned PyJWT 2.15.1. Image handling follows Pillow's [format/decompression-bomb guidance](https://pillow.readthedocs.io/en/stable/reference/Image.html). This excludes full Android advisory and independent security assessment.

Final profile: `dist/verification/remaining-full-20261017/report.json`: **23/23 PASS; zero failures, skips or timeouts; `source_changed: false`**. Earlier failures are retained rather than relabeled as passes.

| Final verification | Result |
| --- | --- |
| Python | 328 passed; one upstream Starlette/httpx deprecation warning |
| SBOM and JavaScript | SBOM consistent; nine syntax checks passed |
| Operational browser checks | Fleet, driver, fleet workflows and dispatch passed |
| Cross-browser matrices | Shared navigation, personal workspace, files, saved views/onboarding and recurrence each passed both portals in Chromium, Firefox and WebKit |
| Concurrent read load | 400/400 HTTP 200, concurrency 24, p95 231 ms, max 322 ms on local fixture |
| Android | All three modules passed debug/release lint and builds; 50 JVM tests across 14 suites, zero failures. Lint warnings remain; this is not a zero-warning claim. |
| Non-demo container | Readiness/auth, demo-key rejection, Caddy config, backup integrity, restored copy and restart passed; includes three recurring events, conversation/messages, actual file bytes and navigation state |
| Independent reminder CLI | First isolated pass generated one notice; second generated zero duplicates |
| Separate accessibility/dependency audits | 19 states with zero automated violations; 22 Python dependencies with zero known vulnerabilities |

Verified base revision: `add024d69cfce79b48ab8b51cd90b3242bb8d9e5`, including uncommitted implementation changes. Source SHA-256: `48b4ecbca5805fdb15baef6729c7207f712f3bcea7c73ff5e768225c1b60666e`. An independent current fingerprint check matched the final report. Documentation-only updates do not change that source hash. Targeted Ruff F checks, new-file formatting and `git diff --check` passed. No public deployment or native hardware acceptance is implied.

## Operations and remaining acceptance

Later connected-account inspection confirmed Vercel/GitHub/Supabase access, but the Vercel projects deploy a different repository and Google Cloud access to their actual backend project was denied. See [CONNECTED_DEPLOYMENT_AUDIT_2026-10-17.md](CONNECTED_DEPLOYMENT_AUDIT_2026-10-17.md). The earlier "no target" finding below refers to a verified target for this local AAOS application, not an absence of all connected hosting accounts.

The existing demo database was preserved and the API restarted at **http://127.0.0.1:57556**. This is a loopback demo, not a public deployment. No unrelated emulator/service or production data was modified. [OPERATIONS.md](OPERATIONS.md) documents recurrence, worker, storage and deployment procedures.

Still requires resources or external acceptance:

- Public target/domain/DNS/TLS and managed production secrets; no target or Git remote configured.
- Real identity/email/OTP tenant for signup/recovery/verification and expiry/single-use/global-revocation testing.
- Authorized live map/road/place/charging/partner providers and actual quota/outage/expiry behavior.
- Controlled production signing, hosted CI, rollout/rollback acceptance.
- Dedicated phone/AAOS/OEM runtime and real field/charging/power/network/reboot evidence. Attached emulator belongs to another project and was untouched.
- Independent security/privacy/safety review, full dependency assessment, manual assistive-technology testing and production-scale monitored capacity/restore drills.

Repeated local tests cannot guarantee zero possible errors, percentile ranking or production acceptance. No invented completion percentage is asserted.

Production gate result remains **4 local, 4 partial, 4 open; 0 fully accepted production gates; `ready: false`**. This is an acceptance-gate count, not a percentage of implemented software.
