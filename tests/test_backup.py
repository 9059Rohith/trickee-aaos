import sqlite3

import pytest

from scripts.backup import backup_database
from trickee.simulator import demo_profile, scenario_frame
from trickee.store import CloudStore


def test_online_backup_and_restored_twin(tmp_path):
    source = str(tmp_path / "live.db")
    destination = str(tmp_path / "restore.db")
    live = CloudStore(source)
    live.register(demo_profile())
    live.ingest([scenario_frame("SCENARIO_001", 1)])
    backup_database(source, destination)
    restored = CloudStore(destination)
    assert restored.twin("TRICKEE-DEMO-1")["last_sequence"] == 1
    assert len(restored.events("TRICKEE-DEMO-1")) == 1


def test_backup_rejects_live_database_as_destination(tmp_path):
    source = tmp_path / "live.db"
    CloudStore(str(source))
    with pytest.raises(ValueError, match="destination must differ"):
        backup_database(str(source), str(tmp_path / "folder" / ".." / "live.db"))


def test_failed_backup_preserves_previous_snapshot(tmp_path):
    source = tmp_path / "corrupt.db"
    source.write_bytes(b"not a SQLite database")
    destination = tmp_path / "previous.db"
    destination.write_bytes(b"previous snapshot remains")
    with pytest.raises(sqlite3.DatabaseError):
        backup_database(str(source), str(destination))
    assert destination.read_bytes() == b"previous snapshot remains"
    assert not list(tmp_path.glob(".previous.db.*.tmp"))
