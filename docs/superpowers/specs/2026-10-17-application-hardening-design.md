# Application hardening design

The requested outcome is a working, thoroughly tested Trickee application and a concrete path toward the twelve improvements discussed in chat. The user authorized planning, implementation and execution without further questions. This work preserves existing architecture and user data.

## Acceptance

Exercise the Python API and persistence suite, browser fleet/driver/dispatch journeys, JavaScript syntax, dependency inventory, concurrent reads, and all three Android modules. Attempt current emulator and container verification where tools are available. Fix reproducible defects with regression tests. Leave a running loopback demo and repeatable verification instructions.

Add a single verification entry point that runs checks, keeps individual logs and structured results, reports unavailable tooling separately from successful checks, and returns failure when required checks fail or are skipped. Do not reinterpret synthetic data or historical evidence as fresh field validation.

## Architecture and constraints

Keep FastAPI, SQLite, vanilla browser clients and the three Android modules. Run verification in subprocesses with bounded timeouts and isolated fixture databases. Existing scripts own their browser and native workflows. Preserve pre-existing untracked model artifacts and existing databases. Local demo binds only to loopback; production authentication remains fail closed.

## External acceptance

Fleet pilot outcomes, real vehicle/BMS validation, prediction accuracy on unseen field trips, licensed charging/map providers, real partner orders, production identity, public TLS staging, independent security/safety reviews and controlled signing cannot be manufactured locally. Document concrete deliverables and acceptance evidence for these workstreams; keep release gates open until that evidence exists.
