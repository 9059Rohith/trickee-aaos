# Implemented architecture and boundaries

This is an AAOS application platform, not an operating-system fork. The checkout contains small pinned official AOSP source trees for reference and three independently packaged Android apps. It has **not** produced a full AAOS image.

```text
Synthetic vehicle / AAOS CarPropertyManager
                   |
          validated SignalFrame
                   |
        local VehicleAgent + outbox
                   |
          authenticated batch API
                   |
     SQLite event log + latest-state twin
                   |
       fleet console / driver clients
```

## Components

| Component | Current role | Boundary |
| --- | --- | --- |
| `trickee/domain.py` | Canonical signals, state transitions, physics baseline, charger ranking and rule health | Unknown vehicle values stay unknown; estimates and simulation carry provenance. |
| `trickee/agent.py` and `trickee/store.py` | Local offline queue, cloud event store, twin, trips and consent-linked phone data | Sequence/idempotency checks prevent contradictory replay. SQLite is not a tested production database cluster. |
| `trickee/store.py` client changes | Bounded per-audience change cursors written in the same transaction as selected workflow mutations | Operator and driver browsers and the foreground native phone app re-fetch protected records. Telemetry frames and background push remain outside this feed. |
| `trickee/api.py` | FastAPI operator, vehicle and driver routes | Distinct credentials and tenant/driver scope; production ingress and identity are separate requirements. |
| `trickee/providers.py` and `trickee/route_context.py` | Optional route, place, charger, weather and elevation adapters | Production feeds are unconfigured unless supplied; errors fail closed or show unavailable. |
| `trickee/dispatch.py` | Manual dispatch proposals and lifecycle | Does not integrate a delivery-platform order feed. |
| `trickee/service_cases.py` | Driver reports, replies and operator triage | Not a maintenance prediction system. |
| `trickee/ota.py` | Signed simulator artifact update and rollback | Does not update the AAOS system partition or OEM firmware. |
| `fleet-console/` | Operator web cockpit | Browser features are not proof of mobile or car-host acceptance. |
| `driver-console/` | Browser driver companion with durable local response queues | User identity, physical-phone and field tests remain. |
| `android-app/app/` | Read-only AAOS Car API telemetry reader and outbox | No direct CAN writer. Requires target image permissions. |
| `android-app/navigation/` | Separate Car App Library route-only Navigation-category template | No licensed basemap, voice/lane guidance or approved production host. |
| `android-app/driver/` | Native phone GPS/IMU, trips, route preview, durable queues and foreground update watcher | Tested on API 36 emulator; background push and physical-phone validation remain open. |

## Data and control rules

1. Vehicle-origin telemetry and phone GPS/IMU are separate sources. Phone measurements do not become BMS measurements.
2. Data carries observation time, source and quality. Stale and unavailable data must stay visibly labeled.
3. The copilot uses bounded read-only tools. It does not command steering, brakes, propulsion or CAN.
4. Driver and operator writes are tenant/assignment scoped. Offline responses are replayed only for their originating identity and server assignment.
5. Demo route, charger and vehicle data are simulation, not evidence of a live commercial service.

## AOSP pin

| Item | Checkout value |
| --- | --- |
| Requested manifest tag | `android-17.0.0_r1` |
| Manifest revision | `5bc9a7ce1cd78dd53613bbfd0ebf506e1e4adb0f` |
| Car Service revision | `9f04df65daa8b9a65ee05fd4039fe95446874d76` |
| Hardware interfaces revision | `0162af698935100a590b7359581ac8b1b80693e5` |
| App compile/target SDK | 37 |
| Car emulator actually exercised | API 35 |
| Phone emulator actually exercised | API 36 |
| Full AOSP image build ID | Unavailable; no image build evidenced |

The source-pinning scripts are `aosp/fetch-upstream.ps1` and `aosp/full-checkout.sh`. These revisions describe checked-out reference source, not a validated production AAOS distribution.
