# Security and privacy state

This document describes implemented local controls and unresolved release obligations. It is not a security certification or legal approval.

## Current controls

- A per-vehicle ingest key is derived from a separate master key. Ingest is vehicle-bound and sequence reuse is checked.
- Optional tenant-scoped operator keys and roles prevent cross-tenant fleet reads and writes in local tests. A configured RFC 9068 access-token verifier checks issuer, audience, signature, time and an exact subject allowlist. The web console does **not** implement a full browser identity login.
- Admin-issued driver credentials are random, hashed at rest, revocable and checked against the live driver/vehicle assignment. The Android phone seals its credential with Android Keystore.
- Operator writes and selected sensitive reads record an actor in the local audit table. This is not complete tamper-resistant production audit coverage.
- Driver phone GPS/IMU collection requires recorded trip consent, an active trip, Android permission and an explicit start action. Withdrawal closes the local collection gate, and the cloud rejects out-of-consent uploads.
- Phone-data export is paged; erasure withdraws consent and creates replay-blocking tombstones. A configurable retention sweep exists but requires an approved period and scheduled execution.
- The copilot uses bounded read-only/advisory tools. There is no route for direct brake, steering, propulsion or CAN commands.
- Python dependencies are pinned, the server/web SBOM is checked, API responses use no-store, and the Docker process runs non-root.

## Unresolved risks and required acceptance

| Area | Remaining requirement |
| --- | --- |
| Identity | Live tenant, browser authorization-code login/logout, recovery, hardware-backed enrollment, key rotation and session policy. |
| Transport/storage | Managed TLS ingress, secret manager, persistent encrypted storage, tested backup/restore and monitoring in a real staging network. |
| Location privacy | Approved consent notice/purpose/retention policy, cleanup of backups and other devices after erasure, provider processing terms, and an audit of access to raw traces. |
| Device storage | Native unsynced SQLite GPS queue is not application-level encrypted; physical-device and backup/transfer behavior need acceptance. |
| Vehicle safety | Target-image permission/sepolicy review, distraction and host UX testing, signal mapping, fault handling and OEM cybersecurity approval. |
| Supply chain | Android resolved dependency/font inventory, vulnerability triage, artifact provenance and controlled release signing. |
| Evaluation | Threat model, penetration assessment, privacy review, road safety review and incident/rollback exercises. |

No physical vehicle, API 37 OEM image, real customer dataset, or production identity system was accepted in this audit. A passed emulator test is only emulator evidence.
