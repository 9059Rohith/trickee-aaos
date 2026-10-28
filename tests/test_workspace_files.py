import sqlite3
import threading
from types import SimpleNamespace

import pytest
from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from trickee.workspace_files import install_workspace_files, MAX_FILE_BYTES


@pytest.fixture
def files():
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.row_factory = sqlite3.Row
    cloud = SimpleNamespace(db=db, _write_lock=threading.RLock())

    def owner(x_owner: str = Header(default="")):
        if not x_owner:
            raise HTTPException(401)
        return {"key": x_owner}

    app = FastAPI()
    install_workspace_files(app, cloud, owner, owner)
    with TestClient(app) as client:
        yield client
    db.close()


def upload(client, body=b"private notes", name="notes.txt", request="request-123"):
    return client.post(
        "/api/v1/operator/workspace/files",
        params={"filename": name, "request_id": request},
        content=body,
        headers={"X-Owner": "alice"},
    )


def test_private_crud_idempotency(files):
    base = "/api/v1/operator/workspace/files"
    assert files.get(base).status_code == 401
    response = upload(files)
    assert response.status_code == 201
    item = response.json()
    assert upload(files).json()["file_id"] == item["file_id"]
    assert upload(files, b"changed").status_code == 409
    path = base + "/" + item["file_id"]
    assert files.get(path, headers={"X-Owner": "bob"}).status_code == 404
    assert (
        files.get(
            path.replace("/operator/", "/driver/"), headers={"X-Owner": "alice"}
        ).status_code
        == 404
    )
    result = files.get(path + "/preview", headers={"X-Owner": "alice"})
    assert result.content == b"private notes"
    assert result.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in result.headers["content-security-policy"]
    assert (
        files.get(base, params={"q": "NOTES"}, headers={"X-Owner": "alice"}).json()[
            "total"
        ]
        == 1
    )
    assert files.delete(path, headers={"X-Owner": "alice"}).status_code == 204
    assert files.get(path, headers={"X-Owner": "alice"}).status_code == 404


@pytest.mark.parametrize(
    "name,body",
    [
        ("evil.html", b"<html>"),
        ("x.svg", b"<svg/>"),
        ("../notes.txt", b"text"),
        ("x.txt", b"<script>alert(1)</script>"),
        ("x.png", b"not an image"),
        ("x.pdf", b"%PDF-1.7\n/JavaScript bad\n%%EOF"),
        ("x.txt", b"\x00\xff"),
    ],
)
def test_unsafe_content_rejected(files, name, body):
    assert upload(files, body, name).status_code == 422


def test_bounded_upload(files):
    assert upload(files, b"x" * (MAX_FILE_BYTES + 1)).status_code == 413


def test_image_normalizes_trailing_payload(files):
    import io
    from PIL import Image

    output = io.BytesIO()
    Image.new("RGB", (2, 2), "blue").save(output, format="PNG")
    result = upload(files, output.getvalue() + b"<script>evil</script>", "photo.png")
    assert result.status_code == 201
    saved = files.get(
        "/api/v1/operator/workspace/files/" + result.json()["file_id"] + "/download",
        headers={"X-Owner": "alice"},
    )
    assert b"<script>" not in saved.content
    assert saved.headers["content-disposition"].startswith("attachment")


def test_rate_limit_and_delete_replay(files):
    for index in range(12):
        assert upload(files, request=f"request-{index:03}").status_code == 201
    assert upload(files, request="request-next").status_code == 429
    assert upload(files, request="request-000").status_code == 201
    item = upload(files, request="request-000").json()["file_id"]
    assert (
        files.delete(
            "/api/v1/operator/workspace/files/" + item, headers={"X-Owner": "alice"}
        ).status_code
        == 204
    )
    assert upload(files, request="request-000").status_code == 404


def test_owner_quota_atomic(files, monkeypatch):
    import trickee.workspace_files as module

    monkeypatch.setattr(module, "MAX_OWNER_BYTES", 15)
    assert upload(files, b"x" * 10, request="request-first").status_code == 201
    assert upload(files, b"y" * 10, request="request-other").status_code == 413
    result = files.get(
        "/api/v1/operator/workspace/files", headers={"X-Owner": "alice"}
    ).json()
    assert result["total"] == 1


def test_concurrent_idempotency(files):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as executor:
        responses = list(executor.map(lambda _: upload(files), range(8)))
    assert all(response.status_code == 201 for response in responses)
    assert len({response.json()["file_id"] for response in responses}) == 1


def test_library_persists_on_restart(tmp_path):
    path = str(tmp_path / "files.db")

    def start():
        db = sqlite3.connect(path, check_same_thread=False)
        db.row_factory = sqlite3.Row
        app = FastAPI()
        install_workspace_files(
            app,
            SimpleNamespace(db=db, _write_lock=threading.RLock()),
            lambda: {"key": "alice"},
            lambda: {"key": "alice"},
        )
        return db, TestClient(app)

    db, client = start()
    with client:
        result = upload(client).json()
    db.close()
    db, client = start()
    with client:
        response = client.get(
            "/api/v1/operator/workspace/files/" + result["file_id"] + "/download"
        )
        assert response.content == b"private notes"
        assert upload(client).json()["file_id"] == result["file_id"]
    db.close()
