"""Repeatable local acceptance checks; successful local checks are not a release approval."""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Check:
    name: str
    command: tuple[str, ...]
    timeout: float = 600
    unavailable: str | None = None
    compose_project: str | None = None
    docker_prefix: tuple[str, ...] = ("docker",)


def verification_environment() -> dict[str, str]:
    """Fixtures must never pick up the caller's database, secrets or live providers."""
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith("TRICKEE_")}


def _stop_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, timeout=15)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    if process.poll() is None:
        process.kill()
    process.wait(timeout=15)


def _cleanup_compose(project: str, log, prefix: tuple[str, ...] = ("docker",)) -> None:
    """Docker resources outlive processes; remove only this run's labelled resources."""
    if not re.fullmatch(r"trickee-staging-qa-[a-z0-9]+", project):
        raise ValueError("refusing cleanup of an unrelated Compose project")
    selector = "label=com.docker.compose.project=" + project
    for resource, listing, removal in (
        ("container", [*prefix, "ps", "-aq", "--filter", selector], [*prefix, "rm", "-f"]),
        ("network", [*prefix, "network", "ls", "-q", "--filter", selector], [*prefix, "network", "rm"]),
        ("volume", [*prefix, "volume", "ls", "-q", "--filter", selector], [*prefix, "volume", "rm"]),
    ):
        found = subprocess.run(listing, capture_output=True, text=True, check=True, timeout=30)
        identifiers = found.stdout.split()
        if identifiers:
            subprocess.run([*removal, *identifiers], stdout=log, stderr=subprocess.STDOUT,
                           check=True, timeout=45)
            log.write(f"Cleaned {len(identifiers)} isolated Compose {resource}(s)\n")


def run_check(check: Check, output: Path) -> dict:
    if not check.name or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for c in check.name):
        raise ValueError("check name must be a lowercase filename-safe identifier")
    output.mkdir(parents=True, exist_ok=True)
    result = {"name": check.name, "command": list(check.command), "status": "SKIP",
              "log": check.name + ".log", "duration_seconds": 0.0, "exit_code": None}
    if check.unavailable:
        result["reason"] = check.unavailable
        (output / result["log"]).write_text(check.unavailable + "\n", encoding="utf-8")
        return result
    started = time.monotonic()
    environment = verification_environment()
    environment["TRICKEE_QA_OUTPUT"] = str((output / (check.name + "-artifacts")).resolve())
    if check.compose_project:
        if not re.fullmatch(r"trickee-staging-qa-[a-z0-9]+", check.compose_project):
            raise ValueError("invalid isolated Compose project")
        environment["TRICKEE_QA_COMPOSE_PROJECT"] = check.compose_project
    with (output / result["log"]).open("w", encoding="utf-8") as log:
        try:
            process = subprocess.Popen(
                check.command, cwd=ROOT, env=environment, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=os.name != "nt",
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            try:
                result["exit_code"] = process.wait(timeout=check.timeout)
                result["status"] = "PASS" if process.returncode == 0 else "FAIL"
            except subprocess.TimeoutExpired:
                _stop_tree(process)
                result.update(status="TIMEOUT", reason=f"Exceeded {check.timeout} seconds")
            except BaseException:
                _stop_tree(process)
                raise
        except OSError as error:
            result.update(status="FAIL", reason=str(error))
            log.write(str(error) + "\n")
        finally:
            if check.compose_project:
                try:
                    if check.docker_prefix == ("docker",):
                        _cleanup_compose(check.compose_project, log)
                    else:
                        _cleanup_compose(check.compose_project, log, check.docker_prefix)
                except (OSError, ValueError, subprocess.SubprocessError) as error:
                    result.update(status="FAIL", reason=f"Isolated Docker cleanup failed: {error}")
                    log.write(result["reason"] + "\n")
    result["duration_seconds"] = round(time.monotonic() - started, 3)
    return result


def _git(*arguments: str) -> str:
    try:
        return subprocess.check_output(["git", *arguments], cwd=ROOT, text=True,
                                       stderr=subprocess.DEVNULL, timeout=15).strip()
    except (OSError, subprocess.SubprocessError):
        return "unavailable"


def _source() -> dict:
    # Hash current source, including untracked code; exclude generated evidence/data.
    digest = hashlib.sha256()
    root_inputs = [path for path in ROOT.iterdir() if path.is_file() and (
        path.name.startswith(("requirements", "docker-compose")) or path.name in {
            "Dockerfile", "pytest.ini", "pyproject.toml", ".dockerignore", ".gitattributes", ".gitignore"})]
    for path in sorted(root_inputs):
        digest.update(path.name.encode() + b"\0" + path.read_bytes())
    for base in ("trickee", "scripts", "tests", "fleet-console", "driver-console", "android-app", "staging", ".github"):
        for directory, folders, files in os.walk(ROOT / base):
            folders[:] = sorted(folder for folder in folders if folder not in {"build", ".gradle", "__pycache__"})
            for filename in sorted(files):
                path = Path(directory) / filename
                if path.name == "local.properties" or path.suffix not in {
                    ".py", ".ps1", ".js", ".css", ".html", ".java", ".kt", ".xml", ".gradle",
                    ".properties", ".jar", ".bat", ".sh", ".json", ".yml", ".yaml", ".webmanifest", ""}:
                    continue
                digest.update(path.relative_to(ROOT).as_posix().encode())
                digest.update(b"\0")
                digest.update(path.read_bytes())
    return {"revision": _git("rev-parse", "HEAD"),
            "dirty": bool(_git("status", "--porcelain")), "source_sha256": digest.hexdigest()}


def run_checks(checks: list[Check], output: Path, *, profile: str) -> dict:
    if len({check.name for check in checks}) != len(checks):
        raise ValueError("check names must be unique")
    output.mkdir(parents=True, exist_ok=True)
    report = {"profile": profile, "started_at": datetime.now(timezone.utc).isoformat(),
              "finished_at": None, "source": _source(), "python": sys.version,
              "scope": "Local software only; excludes production and field acceptance",
              "passed": False, "checks": []}

    def save() -> None:
        temporary = output / "report.json.tmp"
        temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "report.json")

    save()
    for check in checks:
        print(f"RUN {check.name}", flush=True)
        result = run_check(check, output)
        report["checks"].append(result)
        save()
        print(f"{result['status']} {check.name} ({result['duration_seconds']}s)", flush=True)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    report["source_after"] = _source()
    report["source_changed"] = any(report["source"][key] != report["source_after"][key]
                                   for key in ("revision", "source_sha256"))
    report["passed"] = (bool(checks) and not report["source_changed"]
                        and all(row["status"] == "PASS" for row in report["checks"]))
    save()
    return report


def build_checks(profile: str, *, android_offline: bool = False, docker_wsl: str | None = None) -> list[Check]:
    python = sys.executable
    checks = [Check("python", (python, "-m", "pytest", "-q"), timeout=1200),
              Check("sbom", (python, "scripts/build_sbom.py", "--check"))]
    for path in ("fleet-console/app.js", "fleet-console/map.js", "driver-console/app.js", "driver-console/sw.js", "fleet-console/assets/workspace.js", "fleet-console/assets/personal.js", "fleet-console/assets/account-access.js", "fleet-console/assets/files.js", "fleet-console/assets/navigation.js"):
        checks.append(Check(path.replace("/", "-").replace(".", "-"), ("node", "--check", path),
                            unavailable=None if shutil.which("node") else "Install Node.js to verify browser syntax"))
    if profile in {"web", "full"}:
        for name, module in (("fleet-browser", "verify_ui_isolated"), ("driver-browser", "verify_driver_ui"),
                             ("fleet-workflows", "verify_fleet_workflows_ui"), ("dispatch-browser", "qa_dispatch_browser"),
                             ("load", "verify_load_isolated"), ("workspace-browser", "verify_workspace_ui"), ("personal-workspace-browser", "verify_personal_workspace_ui"),
                             ("files-browser", "verify_workspace_files_ui"), ("navigation-browser", "verify_workspace_navigation_ui"), ("recurrence-browser", "verify_recurrence_ui")):
            checks.append(Check(name, (python, "-m", "scripts." + module)))
    if profile == "full":
        if os.name == "nt":
            android = ("powershell", "-NoProfile", "-File", "scripts/verify_android.ps1")
            if android_offline:
                android += ("-Offline",)
        else:
            tasks = tuple(f":{module}:{task}" for module in ("app", "driver", "navigation")
                          for task in ("testDebugUnitTest", "lintDebug", "lintRelease", "assembleDebug", "assembleRelease"))
            android = ("bash", "android-app/gradlew", "-p", "android-app", *tasks, "--no-daemon", "--max-workers=1")
            if android_offline:
                android += ("--offline",)
        checks.append(Check("android", android, timeout=1800,
                            unavailable=None if shutil.which("java") else "Install JDK 17 and the Android SDK"))
        project = "trickee-staging-qa-" + uuid.uuid4().hex[:12]
        if docker_wsl:
            if os.name != "nt" or not re.fullmatch(r"[A-Za-z0-9._-]+", docker_wsl):
                raise ValueError("WSL Docker requires Windows and a valid distribution name")
            linux_root = "/mnt/" + ROOT.drive[0].lower() + ROOT.as_posix()[2:]
            command = ("wsl", "-d", docker_wsl, "--", "env", "TRICKEE_QA_COMPOSE_PROJECT=" + project,
                       "python3", linux_root + "/scripts/verify_staging_container.py")
            prefix = ("wsl", "-d", docker_wsl, "--", "docker")
            unavailable = None if shutil.which("wsl") else "Install WSL with Python 3 and a running Docker engine"
        else:
            command = (python, "scripts/verify_staging_container.py")
            prefix = ("docker",)
            unavailable = None if shutil.which("docker") else "Install and start Docker"
        checks.append(Check("staging-container", command, timeout=1200, unavailable=unavailable,
                            compose_project=project, docker_prefix=prefix))
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", choices=("core", "web", "full"), default="web")
    parser.add_argument("--android-offline", action="store_true", help="Use cached Gradle dependencies")
    parser.add_argument("--docker-wsl", metavar="DISTRO", help="Run container checks and cleanup using native Docker in this WSL distribution")
    parser.add_argument("--output", type=Path, help="New directory for logs/report; existing nonempty directories are rejected")
    args = parser.parse_args()
    output = args.output or ROOT / "dist" / "verification" / (
        datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8])
    if output.exists() and any(output.iterdir()):
        parser.error("output directory must be empty to preserve previous evidence")
    report = run_checks(build_checks(args.profile, android_offline=args.android_offline,
                                    docker_wsl=args.docker_wsl), output, profile=args.profile)
    print(f"Report: {output.resolve() / 'report.json'}", flush=True)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
