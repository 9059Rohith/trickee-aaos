# Evidence-based product roadmap

This is the implementation and acceptance path for the twelve improvements discussed on 2026-10-17. It defines deliverables, not a percentile ranking. The current local system already implements simulation, telemetry, queues, routing adapters, scoped fleet/driver workflows, Android companions and model evaluation. Local fixtures cannot close field or partner acceptance.

| Order | Workstream | Next deliverable | Acceptance evidence |
| --- | --- | --- | --- |
| 1 | Reproducible operation | Single `scripts/verify_application.py` entry point, isolated artifacts and portable local launcher | Fresh profile report, individual logs, source fingerprint and explicit failed/unavailable checks. |
| 2 | Reliable service | Run existing non-demo Compose template on approved staging infrastructure | Issued TLS, scoped identity, monitored service objectives, restart recovery and off-site restore against the release revision. |
| 3 | Offline reliability | Extend existing replay/outbox tests to physical phone and vehicle power/network faults | Witnessed outage, process death, reboot, duplicate and conflict cases with durable records reconciled afterward. |
| 4 | Vehicle integration | Map one selected vehicle's BMS/VHAL signals into canonical frames | Signal contract, units, permission evidence, bench ground truth, missing/stale signal behavior and OEM acceptance. |
| 5 | Field prediction accuracy | Collect consented trips and evaluate range/ETA against frozen baselines | Versioned field dataset, trip/vehicle-separated held-out evaluation, error distributions and uncertainty coverage; no synthetic promotion. |
| 6 | Energy-aware planning | Validate existing decision preview with real road/traffic and charging data | Arrival reserve, tariff and deadline constraints checked on recorded scenarios and field trips; outage causes visible abstention. |
| 7 | Live charging | Onboard an authorized network through existing feed/OCPI interfaces | Station/connector mapping, freshness, tariffs, outage handling and session/CDR reconciliation against provider records. |
| 8 | Operational fleet integration | Map one partner to the versioned order-feed contract | Idempotent imports, assignment acknowledgments, conflict handling and delivery reconciliation against actual partner records. |
| 9 | In-car experience | Test current navigation/telemetry apps on the target car host | Moving/parked behavior, rotary/touch input, guidance recovery, accessibility and applicable Android car-quality checks. |
| 10 | Security/privacy | Review enrollment, tenant boundaries, deletion and signed updates | Threat model, independent review, credential expiry/revocation, lifecycle evidence and controlled signing/rollback approvals. |
| 11 | Explainability | Review all recommendations against their actual input provenance | Freshness and uncertainty visible; stale/missing inputs cannot produce confident actionable claims. |
| 12 | Measured business outcome | Run a partner-approved pilot using a pre-registered comparison | Defined primary outcome, comparable baseline/control periods, sample counts and uncertainty; costs and failures included. |

## Execution dependencies

Local software checks and container checks precede public staging. Hardware selection and provider authorization precede integration acceptance. Field-data collection precedes claims of predictive accuracy or financial savings. Freeze the pilot protocol before observing its outcome.

Use the existing model evaluation and registry modules for dataset provenance and held-out metrics. Keep simulator-trained range/ETA/charger/health artifacts marked experimental until field criteria are met. Do not attach arbitrary accuracy or savings targets after inspecting the test set.

## Repeatable local commands

```powershell
.\.venv\Scripts\python -m pip install -r requirements-dev.lock
.\.venv\Scripts\python -m pip install -r requirements-ui.in
.\.venv\Scripts\python -m playwright install chromium firefox webkit
.\.venv\Scripts\python scripts/verify_application.py --profile full
.\scripts\run-local.ps1 -Port 8000
```

Profiles: `core` covers Python, JavaScript syntax and SBOM; `web` adds isolated operational workflows, Chromium/Firefox/WebKit workspace checks and concurrent reads; `full` adds Android debug/release build/lint/unit tests and the isolated non-demo staging container. None of these profiles certifies real hardware, production providers or a production release. Native runtime checks require dedicated test emulators and are run separately with `scripts/verify_native_driver.py`, `scripts/qa_native_updates.py`, `scripts/verify_aaos_emulator.py`, and `scripts/verify_aaos_navigation.py`.

Each run writes a new directory under `dist/verification/`. A required check that fails, times out or is unavailable prevents overall success. Browser/load artifacts written through the runner stay with that run, preserving historical evidence. Inspect `scripts/check_release_gates.py --json` separately for production acceptance; its current nonzero exit is expected while those gates remain open.

Windows can select an already configured native WSL Docker engine using
`--docker-wsl Ubuntu`; `--android-offline` uses cached Gradle dependencies.
Build/configuration inputs are fingerprinted before and after verification;
a source change makes the run unsuccessful even if individual checks passed.
