# Architecture decisions

This record captures the current design. It does not claim that unresolved target-environment work is complete.

1. **AAOS base.** Use official pinned AOSP source and Android Car Service as the platform boundary. Trickee applications run on top; no custom AAOS image is built in this checkout. `android-17.0.0_r1` is the source tag and API 37 is the Android app target. A real build ID remains unavailable.
2. **Vehicle access.** The Android app reads through `CarPropertyManager`; a future OEM image must map vendor hardware through VHAL. Neither the app nor the copilot writes CAN frames.
3. **Signal provenance.** Canonical telemetry stores source, quality, time and sequence. Missing BMS values remain unknown; phone GPS and manual SOC have distinct provenance.
4. **Offline behavior.** The local vehicle agent and phone/driver clients use durable queues with idempotent replay or explicit conflicts. Local decisions continue through cloud outages where data permits.
5. **Cloud shape.** A FastAPI modular monolith and SQLite event/twin store support local and single-instance staging scenarios. Horizontal production scale requires separate validation and likely storage/event architecture changes.
6. **Provider boundaries.** Road, place, charger and context adapters are optional, validated and fail closed. Simulated data is labeled, and production use requires contracted endpoints and outage/field checks.
7. **Intelligence.** Physics and bounded deterministic tools are the current baseline. Learned personal range/ETA, health models and autonomous optimizer claims await datasets and measured evaluation.
8. **Copilot safety.** Only allowlisted read-only/advisory tools are available. No safety-critical actuation is exposed.
9. **Identity.** Local scoped keys, revocable driver credentials and optional signed-token verification are implemented. Browser SSO, device enrollment and operational key policy are later release gates.
10. **Privacy.** Trip phone-data consent, export, erasure and replay tombstones exist locally; policy, backup/device cleanup and field review remain open.
11. **OTA.** The signed rollback path updates simulator artifacts only. Android platform or OEM firmware update remains outside this implementation.
12. **Release.** Local builds and emulators are milestones. API 37/OEM image, staging, hardware, safety, security, privacy, signing and distribution need separate acceptance.
13. **Map provider consistency.** The current Leaflet/OpenStreetMap surface can show the OSRM-compatible route output. Google Routes geometry must not be added to that surface: Google's Routes API policy requires route results displayed on a map to use a Google Map. A Google-backed mode needs its own licensed map implementation, attribution, terms and acceptance before activation (https://developers.google.com/maps/documentation/routes/policies).

The pasted brief's suggested direct app-to-CAN path is implemented as an AAOS Car API boundary. The separately cited primary technical-research PDF was not supplied, so its contents could not be reconciled.
