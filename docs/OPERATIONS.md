# Build, run, test and deployment operations

## Local development

From the repository root on Windows PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements-dev.lock
.\.venv\Scripts\python -m pytest -q
$env:TRICKEE_DEMO_MODE='1'
.\.venv\Scripts\python -m uvicorn trickee.api:app --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the fleet console, `/driver` for the browser companion, and `/docs` for generated OpenAPI documentation. Use the demo credentials in `.env.example` **only** on loopback. A second terminal can run `python -m trickee.simulator --url http://127.0.0.1:8000`. `scripts/run-local.ps1` is a convenience local demo entry point.

Docker local smoke: copy `.env.example` to `.env` and run `docker compose up --build`. The Compose port is bound to `127.0.0.1`. The Dockerfile runs as a non-root user and exposes `/ready` for health checks. This is a development layout, not a network staging deployment.

## Single-instance staging template

`docker-compose.staging.yml` defines a non-demo cloud service behind Caddy HTTPS ingress. Supply a real DNS name, an approved positive phone GPS retention period, and separate 32+ character operator and vehicle keys through externally stored files. Set `TRICKEE_DOMAIN`, `TRICKEE_PHONE_GPS_RETENTION_DAYS`, `TRICKEE_OPERATOR_KEY_FILE`, and `TRICKEE_VEHICLE_KEY_FILE` in the deployment environment, then run `docker compose -f docker-compose.staging.yml config --quiet` and `docker compose -f docker-compose.staging.yml up -d --build`. The key files must be readable by the container's non-root user. Never copy `.env.example` demo credentials into staging. Caddy needs public ports 80/443 and working DNS to obtain a certificate.

`TRICKEE_DEPLOYMENT_MODE=production` additionally rejects the legacy global operator key. Configure tenant-scoped operator accounts or the approved OIDC token verifier before a production deployment. The staging template intentionally does not claim production identity acceptance.

Run `python scripts/verify_staging_container.py` before release changes. It builds an isolated staging cloud, validates the Caddyfile, checks non-demo authorization, makes an online SQLite backup, reopens the backed-up copy, checks integrity, restarts the cloud, and verifies recovery. This local check does not prove public DNS, certificate issuance, encrypted host storage, off-site backup, monitoring, or a deployed staging environment. The SQLite deployment is single-instance only; do not scale cloud replicas against one volume.

For an approved partner order feed, configure `TRICKEE_ORDER_FEED_URL`, `TRICKEE_ORDER_FEED_TOKEN_FILE` (or `TRICKEE_ORDER_FEED_TOKEN`), `TRICKEE_ORDER_FEED_SOURCE`, `TRICKEE_ORDER_FEED_TENANT_ID`, and `TRICKEE_DB` in the operator job environment. Run `python -m trickee.partner_orders` on the same database volume. The command fetches at most ten pages of 100 records and mirrors them for scoped review. It does not issue proposals, assign drivers, or confirm deliveries. Provider onboarding still requires a mapping from its actual API to the versioned feed contract.

## Android builds

Use JDK 17, the Android API 37 SDK, and the Gradle wrapper from `android-app/` on Linux or WSL:

```bash
bash ./gradlew :app:lintDebug :driver:lintDebug :navigation:lintDebug \
  :driver:testDebugUnitTest :navigation:testDebugUnitTest \
  :app:assembleDebug :driver:assembleDebug :navigation:assembleDebug \
  :app:assembleRelease :driver:assembleRelease :navigation:assembleRelease --no-daemon
```

`app` is the AAOS telemetry reader, `navigation` the separate car host app, and `driver` the phone app. The checked-in `dist/` APKs include debug-signed test files and **unsigned** release files. A debug signature or successful release compile does not authorize distribution. The controlled signing helper is `scripts/sign-android-release.ps1`; provide secrets through a secret manager, never commit them.

On the audited Windows host, set `ANDROID_HOME` to the installed SDK before using `gradlew.bat`. Its Java Unix-socket default temp path also required `JAVA_TOOL_OPTIONS=-Djdk.net.unixdomain.tmpdir=C:\Temp` for Gradle to start; this is a local build-host workaround, not an app setting.

## Test and evidence commands

For a repeatable run with separate logs and structured evidence, install
`requirements-ui.in`, run `python -m playwright install chromium firefox webkit`, then run
`python scripts/verify_application.py --profile full`. Use `--profile web` when
Android and Docker are outside the intended check. Results go into a new
`dist/verification/` directory. Required failures, timeouts and unavailable tools
produce a nonzero exit. Emulator runtime checks remain separate; see below.

On Windows with an existing native Docker engine in WSL, use
`python scripts/verify_application.py --profile full --android-offline --docker-wsl Ubuntu`.
The selected distribution must already have Python 3, Docker and Compose installed
and its daemon running. Container execution and cleanup both use that runtime;
the report records the actual command. Omit `--android-offline` when dependencies
need downloading. This does not configure or publish a network deployment.

`scripts/run-local.ps1 -Port 8000` resolves the project and its virtual environment
even when invoked from another directory. It binds only to `127.0.0.1`.

```text
python -m pytest -q
python scripts/check_release_gates.py --json
python scripts/build_sbom.py --check
python scripts/verify_ui.py
python scripts/verify_driver_ui.py
python scripts/verify_fleet_workflows_ui.py
python -m scripts.qa_dispatch_browser
python -m scripts.qa_native_updates
```

The browser UI scripts need their documented local server and Playwright setup. `scripts/qa_native_updates.py` requires a booted API 36 phone emulator, an assembled driver debug APK, and clears the `com.trickee.driver` test app on that emulator. `scripts/verify_aaos_emulator.py`, `scripts/verify_aaos_navigation.py`, and `scripts/verify_native_driver.py` need matching running emulators and local backend fixtures. Do not report those as freshly passed unless rerun. Preserved historical images and verification JSON are under `docs/aaos-evidence/`, `docs/aaos-navigation-evidence/`, `docs/native-driver-evidence/`, `docs/ui-parity/` and `docs/load/`.

## Production deployment checklist

Personal workspaces use additive SQLite tables in the same cloud database. Include
calendar events, messages, preferences, reminder state and audit records in the
database backup/retention policy. Private owners bind tenant, actor and role;
driver owners also bind vehicle assignment. Changing role or assignment intentionally
does not expose the previous workspace. Export is available to the authenticated
owner. Deleted content can remain in retained backups until their retention expires.

Optional account-provider links use `TRICKEE_ACCOUNT_LOGIN_URL`,
`TRICKEE_ACCOUNT_REGISTRATION_URL`, `TRICKEE_ACCOUNT_RECOVERY_URL` and
`TRICKEE_ACCOUNT_SECURITY_URL`. Only HTTPS links without embedded credentials or
fragments are accepted. These links do not configure email delivery: registration,
verification and recovery must be tested against the actual identity provider.
Personal reminders are generated by the API lifespan worker every 30 seconds
(configure `TRICKEE_WORKSPACE_REMINDER_SECONDS`, 1..3600) and on authenticated
refresh. The worker stops during orderly shutdown; `/ready` fails if it stops or
its most recent pass failed. `python -m scripts.workspace_reminders DATABASE`
can generate a single pass independently; `--interval 30 --runs 120` runs a bounded
standalone worker. Multiple processes deduplicate notices in SQLite. Delivery is
in-app; no email/push transport is configured. The assistant is a local read-only
rule engine.

Calendar recurrence is finite: daily/weekly/monthly with interval and count/until,
up to 366 occurrences and 2,000 calendar entries per owner. Wall time follows the
selected IANA zone; month-end days clamp from the original day, DST gaps shift
forward and overlaps choose the first fold. Entire-series editing explicitly
rebuilds from the submitted dates/rule and replaces occurrence exceptions.

Private files are stored as SQLite BLOBs and included in the database backup.
Supported files are normalized PNG/JPEG, restricted uncompressed PDF downloads,
and plain UTF-8 TXT. Limits: 2 MiB per file, 4 million image pixels, 20 MiB and 40
files per owner, 12 new uploads/minute. Reject unsupported/encrypted PDFs rather
than implying full PDF compatibility or antivirus scanning. Successful upload
request IDs are deduplicated; deleted-file command tombstones expire after 24
hours. Files form a standalone library rather than event-specific attachments.

Favorites, saved calendar views and onboarding progress are server-persisted with
revision conflict checks. The native phone's Personal workspace entry requires
fresh parked/charging telemetry and clears private forms when safety verification
expires or the Activity pauses. This is software gating, not OEM safety acceptance.

The following are prerequisites, not claims of completion:

1. Create a non-demo network staging environment with TLS, managed keys/secrets, durable encrypted storage, monitoring and off-site backups.
2. Run a witnessed restore drill and verify data integrity, API health and idempotent replay after restart.
3. Configure licensed map, road, place and charging providers and a live identity tenant; test expiry, quota, outage and revocation.
4. Build and test on the selected API 37/OEM car image, verify Car permissions and driver distraction behavior, and test physical phone battery/outage policies.
5. Run field routes and charging scenarios with known ground truth. Resolve security, privacy and safety reviews.
6. Run hosted CI for the exact release commit, generate and review a complete SBOM, sign with controlled production keys, verify the signatures and approve the rollout/rollback plan.

The measurable result of these requirements is in [RELEASE_STATUS.md](RELEASE_STATUS.md). Until all twelve production gates pass, the software must not be labeled deployment-ready.
