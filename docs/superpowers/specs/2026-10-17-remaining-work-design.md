# Remaining locally implementable application work

The user requests completion of the previously listed pending work without questions. Preserve the FastAPI/SQLite, vanilla-browser and Android architecture and existing uncommitted changes. Implement local capabilities; external acceptance must remain evidence-dependent.

## Deliverables and boundaries

1. Private file library for both browser roles: bounded uploads, metadata, safe previews/downloads, deletion, tenant/actor/role/assignment isolation and no executable same-origin uploads. SQLite BLOB persistence keeps backup/restart atomic. Validate actual supported content, cap per-file and per-owner storage, reject unsafe names/types, never trust client owner fields.
2. Recurring calendar series: bounded daily/weekly/monthly recurrence in an explicit IANA zone, DST-aware local wall time, occurrence/series editing or cancellation with clear scope, optimistic conflict handling and durable reminder deduplication. A server worker/CLI generates in-app reminders without a browser refresh. No claim of external push/email delivery without transport.
3. Server-persisted favorites, calendar saved views and guided onboarding progress for each authenticated owner. No credentials in new browser storage; clear private UI on identity changes and access failures.
4. Native phone workspace access following existing API and safe lifecycle patterns, parked-only access and no arbitrary WebView bridge. Do not place free-form personal forms in the AAOS driving surface. Build/lint/JVM verification is distinct from hardware runtime acceptance.
5. Extend API/browser/container regressions, exercise failure paths and rerun the full profile against a stable source fingerprint. Repeat relevant regressions only when changes or failures justify it. Preserve every failed result.

## External acceptance

Public DNS/TLS, real identity/email/OTP tenant flows, licensed live providers, production signing, physical devices/OEM/field ground truth and independent audits require resources not present. Supply deployable configuration/checklists and honest local evidence, never invented credentials or acceptance.

## Verification

Authorization crossing owners/roles/tenants/assignments; malicious file metadata/oversize/type confusion; DST/month-end recurrence; duplicate/background-worker races; data persistence through backup/restart; identity change during requests; keyboard/mobile and three-browser workflows; existing operational suites; Android builds/lint/unit tests; isolated non-demo container recovery.
