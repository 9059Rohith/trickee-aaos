"""Build or verify the checked-in CycloneDX inventory of server and web assets.

The Android dependency graph is built by Gradle and is outside this inventory.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "docs" / "SBOM.cdx.json"
LOCK = ROOT / "requirements.lock"
LEAFLET = ROOT / "fleet-console" / "assets" / "leaflet"
PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s#]+)$")


def expected_components() -> list[dict]:
    packages: list[dict] = []
    names: set[str] = set()
    for line in LOCK.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = PIN.fullmatch(line)
        if not match:
            raise ValueError(f"unrecognized requirement in {LOCK.name}: {line}")
        name, version = match.groups()
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        if normalized in names:
            raise ValueError(f"duplicate package: {normalized}")
        names.add(normalized)
        purl = f"pkg:pypi/{normalized}@{version}"
        packages.append({"type": "library", "name": normalized, "version": version,
                         "purl": purl, "bom-ref": purl})

    leaflet_purl = "pkg:npm/leaflet@1.9.4"
    packages.append({"type": "library", "name": "leaflet", "version": "1.9.4",
                     "purl": leaflet_purl, "bom-ref": leaflet_purl,
                     "licenses": [{"license": {"id": "BSD-2-Clause"}}]})
    for filename in ("leaflet.js", "leaflet.css"):
        path = LEAFLET / filename
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        packages.append({"type": "file", "name": f"fleet-console/assets/leaflet/{filename}",
                         "bom-ref": f"file:leaflet/{filename}",
                         "hashes": [{"alg": "SHA-256", "content": digest}]})
    return sorted(packages, key=lambda component: component["bom-ref"])


def build() -> dict:
    components = expected_components()
    app_ref = "trickee-aaos:server-web"
    return {"$schema": "http://cyclonedx.org/schema/bom-1.4.schema.json",
            "bomFormat": "CycloneDX", "specVersion": "1.4",
            "serialNumber": f"urn:uuid:{uuid.uuid4()}", "version": 1,
            "metadata": {"component": {"type": "application", "name": "trickee-aaos-server-web",
                                       "bom-ref": app_ref}},
            "components": components,
            "dependencies": [{"ref": app_ref,
                              "dependsOn": [item["bom-ref"] for item in components]}]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="fail if the checked-in inventory is stale")
    args = parser.parse_args()
    current = build()
    if args.check:
        try:
            actual = json.loads(OUTPUT.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            parser.exit(1, f"SBOM unavailable: {error}\n")
        current["serialNumber"] = actual.get("serialNumber")
        if actual != current:
            parser.exit(1, "SBOM is stale; run python scripts/build_sbom.py\n")
        print(f"SBOM current: {len(current['components'])} server/web components")
        return 0
    OUTPUT.write_text(json.dumps(current, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"Wrote {OUTPUT} with {len(current['components'])} server/web components")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
