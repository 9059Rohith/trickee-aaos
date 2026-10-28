# API and data contracts

The generated schema for the current backend is [openapi.json](openapi.json): **102 path templates** and 43 component schemas. Generate it again from the current application after API changes:

```powershell
python -c "import json; from pathlib import Path; from trickee.api import create_app; s=create_app(':memory:','audit-operator-key-123','audit-vehicle-key-456').openapi(); Path('docs/openapi.json').write_text(json.dumps(s,indent=2,sort_keys=True)+'\n',encoding='utf-8')"
```

When a configured backend is running, `/docs` and `/openapi.json` are the live contract. The checked-in schema is generated with no optional provider configured and contains no operational credential.

## Authentication

| Caller | Header | Scope |
| --- | --- | --- |
| Operator with scoped or local-demo key | `X-Operator-Key` | Tenant and role (`admin`, `fleet_operator`, `viewer`) if scoped accounts are configured. |
| Operator with configured signed access token | `Authorization: Bearer …` | Approved exact subject, tenant and role. An ID token is not accepted. Do not send both operator headers. |
| Vehicle | `X-Vehicle-Key` | Derived per-vehicle ingest key; only that vehicle's telemetry and update-result routes. |
| Driver | `X-Driver-Key` | Admin-issued revocable bearer credential for one tenant, driver and currently assigned vehicle. |

Operator keys, bearer tokens and driver credentials must be delivered over TLS outside loopback. API responses include `Cache-Control: no-store`. The demo global key is only for loopback development.

## Endpoint families

| Prefix or route | Purpose |
| --- | --- |
| `/health`, `/ready` | Process and database readiness. |
| `/api/v1/vehicles`, `/api/v1/telemetry/batches` | Vehicle registration, scoped telemetry ingest and twin reads. |
| `/api/v1/vehicles/{vehicle_id}/trips`, `/api/v1/driver/trips` | Operator and driver trip lifecycle, stops, history and GPS batches. |
| `/api/v1/vehicles/{vehicle_id}/daily-plans`, `/api/v1/driver/daily-plans` | Draft, edit, confirm and read driver schedule. |
| `/api/v1/vehicles/{vehicle_id}/nudges`, `/api/v1/driver/nudges` | Advisory creation and driver response. |
| `/api/v1/vehicles/{vehicle_id}/dispatch-proposals`, `/api/v1/driver/dispatch-proposals` | Manual offer creation, seen, accept/decline, cancellation and events. |
| `/api/v1/partner-orders` | Tenant-scoped, read-only mirror of imported partner order records; no automatic assignment or fulfilment claim. |
| `/api/v1/vehicles/{vehicle_id}/service-cases`, `/api/v1/driver/service-cases` | Driver issue report, message thread and operator triage. |
| `/api/v1/routes/road`, `/api/v1/driver/routes/road`, `/api/v1/driver/routes/match` | Optional provider road routes and map matching. |
| `/api/v1/places/*`, `/api/v1/chargers/nearby` | Configured provider search and charging data. |
| `/api/v1/fleet/*` | Tenant summary, daily operations, alerts and read-only decision preview. |
| `/api/v1/driver/consent/phone-trip`, `/api/v1/driver/phone-data/*` | Consent, export and erasure. |
| `/api/v1/fleet/updates`, `/api/v1/driver/updates` | Authenticated long-poll hints for saved dispatch, service-case, plan, advisory and trip changes. |
| `/api/v1/copilot`, `/api/v1/ota/campaigns` | Bounded read-only copilot and **simulator** OTA campaigns. |

## Contract rules

- `source`, `quality` and timestamps distinguish simulation, device reports, stale data, unavailable feeds and estimates. Missing BMS values cannot be inferred from phone sensors.
- Telemetry, GPS, dispatch, plan and service-case commands use bounded inputs and idempotency or conflict checks as implemented in their stores. Retry an identical command with its original client ID; do not silently replace a conflicting event.
- Road routing and external feeds are optional. A configured provider's response is validated; absent or failing providers produce unavailable or explicit error states, not fabricated live data.
- Phone-data withdrawal stops this application's collection gate and cloud ingestion, but other offline devices and historical backups need an operational cleanup policy.
- The API has no generalized vehicle actuation or direct CAN route.
- Update feeds accept `after` (nonnegative per-audience cursor) and `wait_seconds` (0–20). They return at most 100 resource-ID hints, `next_cursor`, and `reset_required`. A client must re-fetch authorized resources before advancing its cursor. A stale cursor after bounded retention requires a full snapshot refresh. The feed does not contain message text, coordinates or tokens; it does not cover every telemetry frame or replace native push delivery.

Use the schema for field-level types and response status codes. Do not use this summary as a substitute for the generated contract or the endpoint tests.

## Optional partner order feed contract

The pull job accepts one HTTPS endpoint with a bearer token. Each response is a JSON object with `orders` (at most 100) and `next_cursor` (opaque URL-safe string or null). The endpoint accepts `cursor` for the next page. The job follows at most ten pages and rejects redirects, repeated IDs or cursors, malformed positions, timestamps without timezones, unknown statuses and conflicting same-timestamp revisions. A record has `id`, `updated_at`, `status` (`CREATED`, `ASSIGNED`, `PICKED_UP`, `COMPLETED`, or `CANCELLED`), `pickup` and `destination` latitude/longitude objects, `pickup_label`, `destination_label`, and optional `vehicle_id`. The configured tenant and source come from the operator job, not the provider payload. A provider-specific transformer must be reviewed and tested if its actual schema differs.
