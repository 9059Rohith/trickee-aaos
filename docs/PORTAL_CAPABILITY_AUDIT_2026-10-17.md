# Portal capability audit and implementation scope

Later implementation closes several local omissions listed in this initial audit. See [REMAINING_WORK_REPORT_2026-10-17.md](REMAINING_WORK_REPORT_2026-10-17.md) for the current files, recurrence, background reminders, favorites/onboarding and native phone scope and evidence.

Trickee Vehicle OS is an EV intelligence platform, not a general account-management SaaS. It has **two web portals** (`fleet-console`, `driver-console`), a native Android phone companion, an AAOS telemetry reader and a separate native car-navigation application. Admin/operator/viewer are permission levels within the fleet portal. No third web portal was discovered or invented.

Architecture: FastAPI/Pydantic, SQLite WAL, vanilla JavaScript/Leaflet and Android Java/Car App Library. Existing operating flows and database records are retained. New personal-workspace tables are additive and use the same database/backup process. The car apps remain constrained native surfaces; account administration and free-form chat are not inserted into driving UI.

Status legend: **E** existing locally verified capability; **N** new implemented capability, verified by the new API/browser checks; **P** partial (scope explained); **B** blocked by external configuration or runtime evidence; **X** intentionally not implemented, with reason. Local tests do not establish production acceptance.

## Shared 30-capability checklist

| # | Requested capability | Result |
| --- | --- | --- |
| 1 | Login | E: server-validated scoped operator keys, signed OIDC tokens, provisioned driver keys. No local user-password database. |
| 2 | Registration | B: identity-provider registration link supported; no provider tenant or public signup configured. Public users cannot choose an administrator role. |
| 3 | Forgot password | P/B: public account-recovery guidance and validated HTTPS provider handoff. Email reset/token lifecycle must be supplied and tested by the actual IdP; no fake email or insecure local reset tokens. |
| 4 | Password changes | B: configured provider security page; keys use issue/revoke rather than passwords. |
| 5 | Email/OTP verification | B: no verified email identity/provider or OTP service exists in this checkout. Not falsely represented as operational. |
| 6 | Logout/sessions | E: disconnect clears active tab credentials; signed-token expiry and driver credential revocation tested. Global IdP session logout requires provider integration. |
| 7 | Profile | N/P: personal display name and preferences persist. Contact changes/avatar upload are not implemented; no verified contact directory exists. |
| 8 | Privacy/settings | E/N: phone consent/export/erasure plus private workspace export and record deletion. IdP account deactivation/deletion remains external. Backups require retention management. |
| 9 | Navigation | E/N: responsive section navigator and personal-workspace modal, keyboard focus and clear selected pages. |
| 10 | Search | E/N: vehicle and section search, personal event title/description search. No misleading universal search over unsupported record types. |
| 11 | Filters/sort/paging | E/N/P: real state filters, chronological calendar paging, conversation/history and reminder paging; not every historical list has advanced sorting. |
| 12 | Onboarding | P: purpose-specific empty states and contextual help; no multi-step first-login tour or completion tracker. |
| 13 | Help/support | E/N: operational service-case threads, account-access page and workspace help. No promise of staffed emergency support. |
| 14 | Notifications | E/N: existing operational notices plus personal calendar center with unread counts, read/unread, read-all, archive, timestamps and paging. |
| 15 | Notification preferences | N/P: in-app personal reminder generation preference. Email/SMS/push are not supported channels. |
| 16 | Email notifications | B: no mail service configured; no delivered-email claim. Account mail remains an IdP responsibility. |
| 17 | Live updates | E: authenticated change polling and refresh. Personal reminders generate on workspace refresh, not an always-running background service. |
| 18 | Persistent messaging | E/N: authorized service-case conversations plus personal assistant threads and timestamps. No unscoped general user-to-user DM system. |
| 19 | Calendar views | N: month, week, day, agenda, previous/next/today, event details, category and search. Times explicitly use device-local entry; UTC is persisted with IANA zone metadata. |
| 20 | Event CRUD | N: create/edit/delete/complete/reopen, server validation, revision conflicts, idempotent creation retries and no resurrection after deletion. |
| 21 | Reminders | N/P: due reminder generation and durable deduplication, unread management and preference; delivery is explicitly IN_APP_ON_REFRESH. No background email/push scheduler. |
| 22 | Recurrence/zones | N/X: explicit offsets, IANA validation, UTC normalization and DST tests. Recurring occurrence/cancellation engine not implemented; calendar is for one-time personal events. |
| 23 | Persistent chatbot | N: launcher/modal, multiple conversations, database history, timestamps, bounded loading, failed-send retention, retry idempotency, paging and deletion. |
| 24 | Role-specific assistant | N: read-only rule-based guidance and actual authorized record counts scoped by tenant, actor, role and driver assignment. |
| 25 | Daily message | N: current authorized vehicle, due/open calendar and unread reminder counts; local date follows saved summary time zone. No fabricated metrics or predictions. |
| 26 | Assistant safety | N: no external AI/data transmission, no vehicle/user/permission mutation tools, rate limit, request conflict checks and strict record ownership. |
| 27 | Animation | E/N: existing hero/data feedback plus shared motion tokens and personal message/event transitions; OS/user reduced motion respected. No decorative overload. |
| 28 | States/retry | E/N: loading, validation, errors, disconnection, emptiness, revision conflict, bounded request timeout, retry and sensitive-state clearing. |
| 29 | Accessibility/responsive | E/N/P: browser matrix, semantic controls, focus, reduced motion and automated audits. Human screen-reader/physical-device conformance still unverified. |
| 30 | Reliability/preferences | E/N: actual backend persistence, user-scoped preferences, escaped rendering, timezone handling, optimistic concurrency, backup/restart checks. No claim that every possible defect is eliminated. |

## Thirty capability slots adapted to each actual surface

The native column covers the existing phone/AAOS clients. **P** there means source/build/JVM coverage exists but fresh complete native runtime verification remains unavailable; it must not be treated as a tested third web portal.

| # | Capability | Fleet cockpit | Driver companion | Native phone/AAOS applications |
| --- | --- | --- | --- | --- |
| 1 | Role dashboard | E: fleet/twin metrics | E: assigned vehicle/trips | P: telemetry/navigation/phone screens |
| 2 | Quick actions | E: section navigation | E: section navigation | P: native trip/route actions |
| 3 | Account overview | E: role/tenant authority | E: driver/assignment | P: native credential/device status |
| 4 | Profile editing | N: display preferences | N: display preferences | X: no new native account editor |
| 5 | Security controls | E/B: scoped keys/OIDC; provider recovery | E: provision/revoke and consent | P: native credential storage; hardware/runtime unaccepted |
| 6 | Personalized daily state | N: authorized counts | N: assigned counts | X: no new native daily-summary screen |
| 7 | Tasks | E/N: plans and personal calendar | E/N: confirmed plans and personal calendar | P: existing native plans/trips |
| 8 | Pending work | E/N: alerts/cases/reminders | E/N: proposals/advisories/reminders | P: existing native queues/inboxes |
| 9 | Recent activity | E: audit/incidents | E: trip and case history | P: native history surfaces |
| 10 | Search | E/N: vehicles/events/sections | N: events/sections | P: existing native route/place interaction; not universal search |
| 11 | Filters | E/N: state and personal event search | N: calendar view/search | X: no new native advanced filter panel |
| 12 | Sort/paging | N/P: calendar/time paging; existing list limits | N/P: calendar/history paging | P: bounded native responses; comprehensive paging unverified |
| 13 | Record detail | E/N: twin/plans/cases/events | E/N: trip/proposal/case/events | P: existing native details |
| 14 | Create | E/N: plans/proposals/case updates/events/chat | E/N: trips/cases/events/chat | P: native trip/report workflows |
| 15 | Edit | E/N: plan edits/event revisions | N: private event revisions | P: existing native workflow edits |
| 16 | Cancel/delete | E/N: cancel operational assignments; private event/chat deletion | E/N: decline proposals; delete own events/chat | P: native decline/stop actions; no car account deletion UI |
| 17 | Draft/retry | E/N: operational notes and idempotent private retries | E/N: durable operational queues; unsent personal form input | P: durable native command queues; new web drafts are not native features |
| 18 | Validation | E/N: API authoritative constraints | E/N: API authoritative constraints | P: client/JVM validation, API checks |
| 19 | File management | X: arbitrary attachments not added; existing authorized JSON exports retained | X: binary attachments not added; phone/workspace exports retained | X: no new binary upload/preview system |
| 20 | Status tracking | E/N: actual operational and private states | E/N: actual trip and private states | P: existing native status/provenance |
| 21 | History | E/N: audit plus persistent personal messages | E/N: case/trip and assistant history | P: existing native histories; runtime retest blocked |
| 22 | Notifications | E/N: operational + private reminders | E/N: operational inbox + private reminders | P: native foreground updates; background push not configured |
| 23 | Communication | E/N: service threads/read-only assistant | E/N: service threads/read-only assistant | P: existing native advisory/case interaction; no free-form car chatbot |
| 24 | Calendar | N: private calendar + existing daily plans | N: private calendar + confirmed daily plans | P/X: existing plan screens, no new car calendar editor |
| 25 | Assistant | E/N: grounded copilot + persistent rule-based assistant | N: persistent rule-based assistant | X: not inserted into driving UI; no native implementation claimed |
| 26 | Reports/export | E/N: operational and own workspace export | E/N: phone data and own workspace export | P: backend exports; no new native export UI |
| 27 | Favorites/saved views | P: theme/motion preference; no generic favorites | P: motion preference; no generic favorites | X: no new native favorites system |
| 28 | Support | E/N: service cases and help | E/N: service reports, threads and help | P: native service workflows, no emergency-support claim |
| 29 | Preferences/admin | E/N: scoped admin provisioning, personal preferences | E/N: consent and private preferences | P: native consent/settings; no unauthorized admin controls |
| 30 | Accessibility/reliability | E/N/P: browser tests/builds; manual audit pending | E/N/P: browser tests; physical phone pending | B: fresh emulator/physical/OEM runtime validation remains blocked |

## External and explicitly unfinished scope

There is no configured identity tenant/mail transport for end-to-end email reset, verification or registration, no public deployment host/domain credentials, no controlled production signing and no accepted field/OEM evidence. Arbitrary binary attachments, general-purpose favorites, multi-step onboarding, recurrence scheduling and native UI equivalents for the new personal workspace are **not implemented**. These are not hidden behind dummy controls, renamed existing features or marked as passed. A new username/password store was deliberately not substituted for the working key/OIDC architecture.

The broader brief is therefore not 100% complete. This audit is a capability inventory, not a fabricated project-completion percentage. Use the final verification report for executed test totals and the twelve release gates for production acceptance.
