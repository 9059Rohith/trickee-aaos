"""Build and smoke the isolated, non-demo staging cloud service without DNS."""

from __future__ import annotations

import os
import re
import secrets
import subprocess
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
COMPOSE = ROOT / "docker-compose.staging.yml"


def run(args: list[str], environment: dict[str, str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(args, cwd=ROOT, env=environment, capture_output=True, text=True)
    if check and result.returncode:
        raise RuntimeError(f"staging command failed ({result.returncode}): {' '.join(args[:4])}\n{result.stderr[-3000:]}")
    return result


def verify() -> None:
    project = os.getenv("TRICKEE_QA_COMPOSE_PROJECT") or "trickee-staging-qa-" + uuid.uuid4().hex[:10]
    if not re.fullmatch(r"trickee-staging-qa-[a-z0-9]+", project):
        raise ValueError("invalid isolated staging project")
    operator_key = secrets.token_hex(32)
    vehicle_key = secrets.token_hex(32)
    with tempfile.TemporaryDirectory(prefix="trickee-staging-secrets-") as directory:
        root = Path(directory)
        operator_file = root / "operator_key"
        vehicle_file = root / "vehicle_key"
        operator_file.write_text(operator_key + "\n", encoding="utf-8")
        vehicle_file.write_text(vehicle_key + "\n", encoding="utf-8")
        # File-backed Compose secrets are bind mounts; the image's non-root UID
        # needs read access. The temporary parent directory remains private.
        operator_file.chmod(0o644)
        vehicle_file.chmod(0o644)
        env = dict(os.environ)
        env.update({
            "TRICKEE_DOMAIN": "staging.example.test",
            "TRICKEE_PHONE_GPS_RETENTION_DAYS": "30",
            "TRICKEE_OPERATOR_KEY_FILE": str(operator_file),
            "TRICKEE_VEHICLE_KEY_FILE": str(vehicle_file),
        })
        base = ["docker", "compose", "-p", project, "-f", str(COMPOSE)]
        try:
            run(base + ["config", "--quiet"], env)
            run(base + ["up", "-d", "--build", "cloud"], env)
            run(base + ["run", "--rm", "--no-deps", "ingress", "caddy", "validate",
                        "--config", "/etc/caddy/Caddyfile"], env)
            check = """import urllib.error
import urllib.request
from pathlib import Path
base = 'http://127.0.0.1:8000'
assert urllib.request.urlopen(base + '/ready', timeout=3).status == 200
key = Path('/run/secrets/operator_key').read_text().strip()
valid = urllib.request.Request(base + '/api/v1/operator/me', headers={'X-Operator-Key': key})
assert urllib.request.urlopen(valid, timeout=3).status == 200
workspace = urllib.request.Request(base + '/api/v1/operator/workspace/summary', headers={'X-Operator-Key': key})
assert urllib.request.urlopen(workspace, timeout=3).status == 200
invalid = urllib.request.Request(base + '/api/v1/operator/me', headers={'X-Operator-Key': 'local-demo-operator-key'})
try:
    urllib.request.urlopen(invalid, timeout=3)
except urllib.error.HTTPError as error:
    assert error.code == 401
else:
    raise AssertionError('demo key was accepted')
"""
            for attempt in range(30):
                result = run(base + ["exec", "-T", "cloud", "python", "-c", check], env, check=False)
                if result.returncode == 0:
                    break
                if attempt == 29:
                    raise RuntimeError("staging cloud did not pass readiness/auth smoke: " + result.stderr[-1500:])
                time.sleep(1)
            seed = """from trickee.store import CloudStore
from trickee.simulator import demo_profile, scenario_frame
cloud = CloudStore('/app/data/cloud.db')
cloud.register(demo_profile())
cloud.ingest([scenario_frame('SCENARIO_001', 1)])
from trickee.personal_workspace import WorkspaceStore, CalendarEvent, Message
from datetime import datetime, timedelta, timezone
import json
workspace = WorkspaceStore(cloud)
owner = json.dumps(['operator', '*', 'legacy-operator-key', 'admin'])
start = datetime.now(timezone.utc)
workspace.save_event(owner, CalendarEvent(title='Staged personal event', starts_at=start,
    ends_at=start+timedelta(hours=1), timezone='Asia/Kolkata', request_id='staging-calendar-001',
    recurrence={'frequency':'DAILY','count':3}))
conversation = workspace.create_conversation(owner, 'Staged conversation')
workspace.message(owner, conversation['conversation_id'], Message(message='Fixture message',
    request_id='staging-message-001'), 'Fixture reply, not a production AI service')
cloud.close()
import urllib.request
key = open('/run/secrets/operator_key').read().strip()
base = 'http://127.0.0.1:8000/api/v1/operator/workspace'
upload = urllib.request.Request(base+'/files?filename=staged.txt&request_id=staging-file-001',
    data=b'backup file evidence', headers={'X-Operator-Key':key,'Content-Type':'application/octet-stream'}, method='POST')
assert urllib.request.urlopen(upload, timeout=5).status == 201
navigation = urllib.request.Request(base+'/navigation', data=json.dumps({'revision':0,
    'favorites':[{'section_id':'workspace-section-1','label':'Access'}],
    'saved_views':[{'view_id':'staging-view','name':'Weekly','view':'week','query':'connector'}],
    'onboarding_step':4,'onboarding_completed':True}).encode(),
    headers={'X-Operator-Key':key,'Content-Type':'application/json'}, method='PUT')
assert urllib.request.urlopen(navigation, timeout=5).status == 200
"""
            run(base + ["exec", "-T", "cloud", "python", "-c", seed], env)
            run(base + ["exec", "-T", "cloud", "python", "scripts/backup.py",
                        "/app/data/cloud.db", "/app/data/backup.db"], env)
            inspect = """import sqlite3
for path in ('/app/data/cloud.db', '/app/data/backup.db'):
    with sqlite3.connect(path) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        assert db.execute("SELECT COUNT(*) FROM events WHERE vehicle_id='TRICKEE-DEMO-1'").fetchone()[0] == 1
        assert db.execute('SELECT COUNT(*) FROM personal_events').fetchone()[0] == 3
        assert db.execute('SELECT COUNT(*) FROM personal_messages').fetchone()[0] == 2
        assert db.execute('SELECT COUNT(*) FROM workspace_files').fetchone()[0] == 1
        assert db.execute('SELECT content FROM workspace_files').fetchone()[0] == b'backup file evidence'
        assert db.execute('SELECT COUNT(*) FROM personal_navigation').fetchone()[0] == 1
from trickee.store import CloudStore
restored = CloudStore('/app/data/backup.db')
assert restored.twin('TRICKEE-DEMO-1') is not None
restored.close()
"""
            run(base + ["exec", "-T", "cloud", "python", "-c", inspect], env)
            run(base + ["restart", "cloud"], env)
            for attempt in range(30):
                result = run(base + ["exec", "-T", "cloud", "python", "-c", check + inspect],
                             env, check=False)
                if result.returncode == 0:
                    print("Isolated staging cloud passed: ready=200 operator=200 demo-key=401 "
                          "backup=ok restored-copy=ok restart=ok caddy-config=valid")
                    return
                if attempt == 29:
                    raise RuntimeError("staging cloud failed after restart: " + result.stderr[-1500:])
                time.sleep(1)
        finally:
            run(base + ["down", "--volumes", "--remove-orphans"], env, check=False)


if __name__ == "__main__":
    verify()
