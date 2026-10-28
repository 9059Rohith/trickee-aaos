"""Audit the twelve human-reviewed release gates; exit 1 until all are full PASS."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
from pathlib import Path


GATES = (
    "Simulator and cloud",
    "Map and road routing",
    "Charging",
    "Operator and driver access",
    "Trip and fleet workflows",
    "Android build",
    "AAOS emulator",
    "Native phone emulator",
    "Target AAOS image",
    "Network staging",
    "OEM hardware",
    "Production release",
)


class GateError(ValueError):
    """Checklist structure or status is invalid."""


def _verify_production_evidence(evidence: str, evidence_root: Path | None,
                                revision: str | None, used: set[Path]) -> None:
    tags = re.findall(r"(?:^|;)\s*(artifact|sha256|environment|revision)=([^;]+)", evidence)
    values = {key: value.strip() for key, value in tags}
    if len(tags) != 4 or set(values) != {"artifact", "sha256", "environment", "revision"}:
        raise GateError("production PASS requires artifact, hash, environment and revision evidence")
    if not values["environment"] or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.,/-]{2,100}", values["environment"]):
        raise GateError("invalid production evidence environment")
    if (not re.fullmatch(r"[0-9a-f]{40}", values["revision"])
            or revision is None or values["revision"] != revision):
        raise GateError("production evidence revision does not match this checkout")
    if not re.fullmatch(r"[0-9a-f]{64}", values["sha256"]):
        raise GateError("invalid production evidence hash")
    relative = Path(values["artifact"])
    if evidence_root is None or relative.is_absolute() or ".." in relative.parts:
        raise GateError("production evidence artifact must stay inside the evidence root")
    root = evidence_root.resolve()
    artifact = (root / relative).resolve()
    if not artifact.is_relative_to(root) or not artifact.is_file() or artifact in used:
        raise GateError("production evidence artifact is missing, reused or outside the root")
    used.add(artifact)
    with artifact.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    if digest != values["sha256"]:
        raise GateError("production evidence hash does not match the artifact")


def audit(markdown: str, evidence_root: Path | None = None,
          revision: str | None = None) -> dict[str, int]:
    """Count statuses and verify local evidence for every production PASS.

    This checks artifact identity, not the truth of its production acceptance.
    A human reviewer still needs to inspect the evidence and environment.
    """
    results = {"production_pass": 0, "local_pass": 0, "partial": 0, "open": 0}
    seen: set[str] = set()
    used_artifacts: set[Path] = set()
    for line in markdown.splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 3 or cells[0] in {"Gate", "---"}:
            continue
        name, status, evidence = cells
        if name not in GATES:
            raise GateError(f"unknown gate: {name}")
        if name in seen:
            raise GateError(f"duplicate gate: {name}")
        if not evidence:
            raise GateError(f"missing evidence or remaining-work description: {name}")
        seen.add(name)
        if status == "**PASS**":
            _verify_production_evidence(evidence, evidence_root, revision, used_artifacts)
            results["production_pass"] += 1
        elif status.startswith("**PASS ") and status.endswith("**"):
            results["local_pass"] += 1
        elif status == "**PARTIAL**":
            results["partial"] += 1
        elif status == "**OPEN**":
            results["open"] += 1
        else:
            raise GateError(f"invalid status for {name}: {status}")
    missing = set(GATES) - seen
    if missing:
        raise GateError("missing gate(s): " + ", ".join(sorted(missing)))
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status-file", type=Path,
                        default=Path(__file__).resolve().parents[1] / "docs" / "RELEASE_STATUS.md")
    parser.add_argument("--json", action="store_true", help="print machine-readable counts")
    args = parser.parse_args()
    try:
        checkout = args.status_file.resolve().parents[1]
        current = subprocess.run(["git", "rev-parse", "HEAD"], cwd=checkout,
                                 capture_output=True, text=True, check=False)
        revision = current.stdout.strip() if current.returncode == 0 else None
        results = audit(args.status_file.read_text(encoding="utf-8"),
                        evidence_root=args.status_file.parent / "release-evidence",
                        revision=revision)
    except (OSError, GateError) as error:
        parser.exit(2, f"release gate audit error: {error}\n")
    if args.json:
        print(json.dumps({**results, "total": len(GATES),
                          "ready": results["production_pass"] == len(GATES)}, sort_keys=True))
    else:
        print(f"Production release gates: {results['production_pass']}/{len(GATES)} full PASS; "
              f"{results['local_pass']} local-only PASS, {results['partial']} PARTIAL, "
              f"{results['open']} OPEN")
    return 0 if results["production_pass"] == len(GATES) else 1


if __name__ == "__main__":
    raise SystemExit(main())
