"""The cloud store is accessed by concurrent FastAPI worker threads."""
from concurrent.futures import ThreadPoolExecutor
import sqlite3

from trickee import store as store_module
from trickee.simulator import demo_profile, scenario_frame


def test_cloud_store_thread_isolation_and_parallel_reads(tmp_path, monkeypatch):
    original_connect = store_module._connect

    def strict_connection(path: str) -> sqlite3.Connection:
        connection = original_connect(path)
        connection.close()
        strict = sqlite3.connect(path, check_same_thread=True, timeout=5)
        strict.row_factory = sqlite3.Row
        strict.execute("PRAGMA journal_mode=WAL")
        strict.execute("PRAGMA busy_timeout=5000")
        return strict

    monkeypatch.setattr(store_module, "_connect", strict_connection)
    cloud = store_module.CloudStore(str(tmp_path / "cloud.db"))
    cloud.register(demo_profile())
    cloud.ingest([scenario_frame("SCENARIO_001", 1)])

    def read(_: int) -> tuple[list[str], int]:
        return cloud.vehicle_ids(), cloud.twin("TRICKEE-DEMO-1")["last_sequence"]

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(read, range(120)))
    assert all(ids == ["TRICKEE-DEMO-1"] and sequence == 1 for ids, sequence in results)


def test_cloud_store_parallel_ingest_keeps_every_sequence(tmp_path):
    cloud = store_module.CloudStore(str(tmp_path / "cloud.db"))
    cloud.register(demo_profile())

    def ingest(sequence: int) -> dict:
        return cloud.ingest([scenario_frame("SCENARIO_001", sequence)])

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(ingest, range(1, 61)))
    assert sum(result["accepted"] for result in results) == 60
    assert len(cloud.events("TRICKEE-DEMO-1", 100)) == 60
    assert cloud.twin("TRICKEE-DEMO-1")["last_sequence"] == 60
