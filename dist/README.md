# Build artifacts

This folder contains six previously built APKs: debug-signed test packages and unsigned release packages for the AAOS telemetry reader, native phone driver and separate navigation module. `SHA256SUMS.txt` records the hashes of these exact files. They are **not approved for production distribution**.

The current application target is API 37. Historical emulator evidence covers API 35 car and API 36 phone environments. Build timestamps and evidence do not establish the target OEM image, physical vehicle, staging or field acceptance.

Use `scripts/sign-android-release.ps1` only with controlled production signing material after the gates in [release status](../docs/RELEASE_STATUS.md) are accepted. Rebuild and verify hashes/signatures for the intended release commit. Debug keys and unsigned APKs must not be distributed as a production vehicle release.
