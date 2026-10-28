from pathlib import Path
import hashlib

import pytest

from scripts.check_release_gates import GATES, GateError, audit


STATUS_FILE = Path(__file__).resolve().parents[1] / "docs" / "RELEASE_STATUS.md"


def test_current_status_cannot_be_misreported_as_twelve_production_passes():
    report = audit(STATUS_FILE.read_text(encoding="utf-8"))
    assert report == {"production_pass": 0, "local_pass": 4, "partial": 4, "open": 4}


def test_status_words_alone_cannot_claim_twelve_production_passes():
    text = STATUS_FILE.read_text(encoding="utf-8")
    text = text.replace("**PASS locally**", "**PASS**")
    text = text.replace("**PASS on API 35**", "**PASS**")
    text = text.replace("**PASS on API 36**", "**PASS**")
    text = text.replace("**PARTIAL**", "**PASS**").replace("**OPEN**", "**PASS**")
    with pytest.raises(GateError, match="evidence"):
        audit(text)


def test_pass_needs_a_hashed_artifact_environment_and_exact_revision(tmp_path):
    proof = tmp_path / "simulator.txt"
    proof.write_text("Local acceptance log", encoding="utf-8")
    digest = hashlib.sha256(proof.read_bytes()).hexdigest()
    revision = "a" * 40
    lines = ["| Gate | Status | Evidence or remaining work |",
             "| --- | --- | --- |"]
    for index, gate in enumerate(GATES):
        if index == 0:
            lines.append(f"| {gate} | **PASS** | artifact=simulator.txt; sha256={digest}; "
                         f"environment=production; revision={revision} |")
        else:
            lines.append(f"| {gate} | **OPEN** | No acceptance artifact yet. |")
    text = "\n".join(lines)
    assert audit(text, evidence_root=tmp_path, revision=revision)["production_pass"] == 1
    with pytest.raises(GateError, match="revision"):
        audit(text, evidence_root=tmp_path, revision="b" * 40)
    proof.write_text("Changed log", encoding="utf-8")
    with pytest.raises(GateError, match="hash"):
        audit(text, evidence_root=tmp_path, revision=revision)


def test_pass_rejects_evidence_path_outside_root(tmp_path):
    text = STATUS_FILE.read_text(encoding="utf-8")
    text = text.replace("| Simulator and cloud | **PASS locally** |",
                        "| Simulator and cloud | **PASS** | artifact=../outside.txt; "
                        "sha256=" + "a" * 64 + "; environment=production; revision=" + "b" * 40 + "; ")
    with pytest.raises(GateError, match="artifact"):
        audit(text, evidence_root=tmp_path, revision="b" * 40)


def test_missing_gate_is_a_checklist_error():
    text = STATUS_FILE.read_text(encoding="utf-8")
    text = "\n".join(line for line in text.splitlines()
                     if not line.startswith("| OEM hardware |"))
    with pytest.raises(GateError, match="missing"):
        audit(text)
