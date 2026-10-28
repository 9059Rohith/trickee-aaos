"""Exercise the verification runner with real successful and failing processes."""
import json
import io
import os
import socket
import subprocess
import sys

import pytest

from scripts import verify_application as verification

from scripts.verify_application import Check, run_check, run_checks, verification_environment


def test_success_and_failure_keep_individual_logs(tmp_path):
    success = run_check(Check("success", (sys.executable, "-c", "print('healthy')")), tmp_path)
    failure = run_check(Check("failure", (sys.executable, "-c", "raise SystemExit(7)")), tmp_path)
    assert success["status"] == "PASS"
    assert "healthy" in (tmp_path / success["log"]).read_text()
    assert failure["status"] == "FAIL"
    assert failure["exit_code"] == 7


def test_timeout_and_missing_executable_are_not_passes(tmp_path):
    timeout = run_check(Check("slow", (sys.executable, "-c", "import time; time.sleep(60)"), timeout=0.2), tmp_path)
    missing = run_check(Check("missing", ("trickee-no-such-executable",)), tmp_path)
    assert timeout["status"] == "TIMEOUT"
    assert timeout["duration_seconds"] < 10
    assert missing["status"] == "FAIL"
    assert "reason" in missing


def test_skipped_required_check_prevents_overall_success_and_persists_results(tmp_path):
    report = run_checks([
        Check("first", (sys.executable, "-c", "print('ok')")),
        Check("unavailable", (), unavailable="Install the required tool"),
        Check("last", (sys.executable, "-c", "raise SystemExit(3)")),
    ], tmp_path, profile="test")
    persisted = json.loads((tmp_path / "report.json").read_text())
    assert report == persisted
    assert report["passed"] is False
    assert [result["status"] for result in report["checks"]] == ["PASS", "SKIP", "FAIL"]
    assert report["checks"][1]["reason"] == "Install the required tool"
    assert report["finished_at"]
    assert report["source"]["revision"]


def test_empty_checklist_cannot_claim_success(tmp_path):
    assert run_checks([], tmp_path, profile="test")["passed"] is False


def test_verification_does_not_inherit_production_configuration(monkeypatch):
    monkeypatch.setenv("TRICKEE_DB", "production.db")
    monkeypatch.setenv("TRICKEE_ORDER_FEED_TOKEN", "private-token")
    monkeypatch.setenv("TRICKEE_DEPLOYMENT_MODE", "production")
    environment = verification_environment()
    assert not any(key.startswith("TRICKEE_") for key in environment)
    assert environment["PATH"] == os.environ["PATH"]


def test_child_writes_evidence_in_run_directory(tmp_path):
    check = Check("evidence", (sys.executable, "-c",
        "import os,pathlib; p=pathlib.Path(os.environ['TRICKEE_QA_OUTPUT']); "
        "p.mkdir(parents=True); (p/'proof.txt').write_text('fresh')"))
    result = run_check(check, tmp_path)
    assert result["status"] == "PASS"
    assert (tmp_path / "evidence-artifacts" / "proof.txt").read_text() == "fresh"


def test_fingerprint_includes_dependency_and_container_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "ROOT", tmp_path)
    lock = tmp_path / "requirements.lock"
    lock.write_text("first-version")
    first = verification._source()["source_sha256"]
    lock.write_text("second-version")
    second = verification._source()["source_sha256"]
    (tmp_path / "Dockerfile").write_text("FROM python:3.12-slim")
    third = verification._source()["source_sha256"]
    assert len({first, second, third}) == 3


def test_source_changed_during_verification_cannot_pass(tmp_path, monkeypatch):
    monkeypatch.setattr(verification, "ROOT", tmp_path)
    (tmp_path / "requirements.lock").write_text("before")
    report = run_checks([Check("edit", (sys.executable, "-c",
        "from pathlib import Path; Path('requirements.lock').write_text('after')"))],
        tmp_path / "evidence", profile="test")
    assert report["checks"][0]["status"] == "PASS"
    assert report["passed"] is False
    assert report["source_changed"] is True


def test_compose_cleanup_runs_after_timeout(tmp_path, monkeypatch):
    cleaned = []
    monkeypatch.setattr(verification, "_cleanup_compose", lambda project, log: cleaned.append(project), raising=False)
    project = "trickee-staging-qa-test123"
    result = run_check(Check("staging", (sys.executable, "-c", "import time; time.sleep(60)"),
                             timeout=0.2, compose_project=project), tmp_path)
    assert result["status"] == "TIMEOUT"
    assert cleaned == [project]


def test_timeout_stops_descendant_listener(tmp_path):
    port_file = tmp_path / "listener-port"
    child = ("import socket,time; from pathlib import Path; "
             "s=socket.socket(); s.bind(('127.0.0.1',0)); s.listen(); "
             f"Path({str(port_file)!r}).write_text(str(s.getsockname()[1])); time.sleep(60)")
    parent = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(60)"
    result = run_check(Check("descendant", (sys.executable, "-c", parent), timeout=4), tmp_path)
    assert result["status"] == "TIMEOUT"
    port = int(port_file.read_text())
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", port), timeout=0.5)


def test_fixture_server_shutdown_closes_virtualenv_child_listener(tmp_path):
    from scripts.verify_native_driver import free_port, start_server, stop_server
    from scripts.verify_application import _stop_tree

    port = free_port()
    server = start_server(tmp_path / "cloud.db", port)
    try:
        stop_server(server)
        with pytest.raises(OSError):
            socket.create_connection(("127.0.0.1", port), timeout=0.5)
    finally:
        _stop_tree(server)


def test_compose_cleanup_uses_selected_runtime_and_exact_project_label(monkeypatch):
    calls = []
    prefix = ("wsl", "-d", "Ubuntu", "--", "docker")
    def run(command, **options):
        calls.append(command)
        return subprocess.CompletedProcess(command, 0, stdout="isolated-resource\n")
    monkeypatch.setattr(verification.subprocess, "run", run)
    verification._cleanup_compose("trickee-staging-qa-test123", io.StringIO(), prefix)
    assert len(calls) == 6
    assert all(tuple(command[:len(prefix)]) == prefix for command in calls)
    assert all("label=com.docker.compose.project=trickee-staging-qa-test123" in command
               for command in calls[::2])


def test_dispatch_fixture_process_stops_and_releases_database(tmp_path):
    import httpx
    from scripts.qa_dispatch_browser import start_dispatch_server, free_port, OPERATOR_KEY
    from scripts.verify_application import _stop_tree

    database = tmp_path / 'dispatch.db'
    port = free_port()
    process = start_dispatch_server(database, port)
    try:
        assert httpx.get(f'http://127.0.0.1:{port}/api/v1/vehicles',
                         headers={'X-Operator-Key': OPERATOR_KEY}).status_code == 200
    finally:
        _stop_tree(process)
    with pytest.raises(OSError):
        socket.create_connection(('127.0.0.1', port), timeout=.5)
    database.unlink()


def test_dispatch_readiness_allows_slow_start_and_requires_http(monkeypatch):
    from scripts import qa_dispatch_browser as fixture
    from types import SimpleNamespace

    clock = [0.0]
    calls = []
    monkeypatch.setattr(fixture.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(fixture.time, 'sleep', lambda delay: clock.__setitem__(0, clock[0] + 5))

    def get(url, **options):
        calls.append(url)
        return SimpleNamespace(status_code=200 if clock[0] >= 20 else 503)

    monkeypatch.setattr(fixture.httpx, 'get', get)
    fixture.wait_ready(1234, SimpleNamespace(poll=lambda: None))
    assert clock[0] >= 20
    assert calls[-2:] == ['http://127.0.0.1:1234/ready', 'http://127.0.0.1:1234/api/v1/vehicles']


def test_dispatch_readiness_fails_fast_on_child_exit(monkeypatch):
    from scripts import qa_dispatch_browser as fixture
    from types import SimpleNamespace

    monkeypatch.setattr(fixture.time, 'sleep', lambda delay: pytest.fail('dead process must not wait'))
    with pytest.raises(RuntimeError, match='exit code 7'):
        fixture.wait_ready(1234, SimpleNamespace(poll=lambda: 7))
