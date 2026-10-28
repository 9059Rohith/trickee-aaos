# Connected deployment access audit

Read-only audit following the user's request to use existing account connections. No deployment, DNS, environment variable, database, billing or remote repository settings were changed. Secret values are omitted from evidence.

## Confirmed access and mapping

| Resource | Confirmed result |
| --- | --- |
| Vercel team | `ajeyas-projects-ac1f8c11`, connected and readable |
| `trickee-evify-live` | Latest production deployment READY at inspection, linked to `Ajey95/trickee-evify-production`; includes `trickee.co.in` / `www.trickee.co.in` domains |
| `trickee-evify-production` | Latest production deployment READY, linked to the same GitHub repository |
| GitHub | Connected `Ajey95` account can read the deployment repository |
| Supabase | Project named `Trickee`, ref `brwlcivihnxvtmbgentx`, reported INACTIVE. Its use by the active backend has not been established; it was not restored or modified. |
| Google Cloud CLI | One active login. Exactly one project was listed as accessible; it did not match backend project number `397358873357`. Direct lookup of that backend project returned a caller-permission error. |

The Vercel repository has a Next.js frontend under `production/trickee-frontend`, a distinct FastAPI/Postgres backend and separate deployment history. It does not contain this workspace's `trickee/api.py`, `android-app` or personal-workspace implementation. The local `trcikee-aaos` repository has no Git remote. A sibling checkout of the production repository has its own uncommitted work and was left untouched.

The current AAOS backend uses a durable local SQLite database and a long-running reminder worker. Its existing deployment template runs Docker/Caddy with persistent storage. Vercel supports FastAPI through its [Python Functions runtime](https://vercel.com/docs/functions/runtimes/python), but that does not establish a persistent-storage/worker deployment for this application. No disposable-database preview was presented as production.

## Live read-only probes

| Endpoint | Observed status |
| --- | --- |
| `https://www.trickee.co.in/` | 200 |
| `https://trickee-evify-live.vercel.app/login` | 200 |
| `https://trickee-backend.onrender.com/health` | 404; outdated target from repository README |
| `https://trickee-backend-397358873357.asia-southeast1.run.app/health` | 500 |
| Same Cloud Run origin: `/ready`, `/api/v1/auth/me`, `/api/v1/vehicles` | 500 |

The active Cloud Run origin was confirmed from the live Vercel project's `BACKEND_URL` and `NEXT_PUBLIC_BACKEND_URL` settings. Only the HTTPS origin was retained; authentication secrets were not displayed or copied into this workspace.

The web-fetch tool could not retrieve these application pages; direct HTTP probes succeeded and supplied the observed statuses above. A backend 500 is a verified failure, not a diagnosed database root cause. Supabase inactivity alone is insufficient to attribute that failure.

## Required access and safe continuation

Investigating the existing live backend requires a Google Cloud identity with access to project `397358873357` (service configuration and logs first). The currently authenticated identity cannot describe that project. The first service/log inspection used the CLI's configured project, found no matching service, and was not treated as evidence that the actual service was missing; project-number verification identified the mismatch.

No additional Vercel/GitHub API key is needed for the operations already verified. Sensitive provider credentials may require service-specific authorization; visible setting names do not prove decrypted secrets are available or valid.

Deploying the local AAOS application still requires an explicitly mapped persistent backend target. Existing production projects were not repointed to this different codebase. Reusing the Next.js application's secrets, changing its database or replacing production would not establish a valid AAOS deployment.

Raw non-secret HTTP evidence is in `dist/verification/connected-access-20261017/public-http.json` and `active-backend.json`. This read-only audit does not change the previously verified application source or its local test results.
