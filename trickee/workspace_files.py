"""Authenticated, bounded private file storage. No external malware scan is claimed."""

from __future__ import annotations

import hashlib
import io
import re
import uuid
import warnings
from datetime import datetime, timezone
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response

MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_OWNER_BYTES = 10 * MAX_FILE_BYTES
MAX_OWNER_FILES = 40
MAX_OWNER_COMMANDS = 1000


def validate_file(filename, body):
    if (
        not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _().-]{0,119}", filename)
        or ".." in filename
        or re.search(
            r"\.(?:exe|dll|bat|cmd|com|ps1|js|html?|svg|sh)(?:\.|$)", filename, re.I
        )
    ):
        raise HTTPException(422, "Use a simple filename without paths")
    suffix = filename.rsplit(".", 1)[-1].lower()
    if not body:
        raise HTTPException(422, "Empty files are not supported")
    if suffix == "txt":
        try:
            value = body.decode("utf-8")
        except UnicodeDecodeError:
            raise HTTPException(422, "Text must be UTF-8") from None
        if any(ord(c) < 32 and c not in "\n\r\t" for c in value) or re.search(
            r"<\s*[!/?a-z]|javascript:", value, re.I
        ):
            raise HTTPException(422, "Only plain text is supported")
        return "text/plain; charset=utf-8"
    if suffix == "pdf":
        # Restrict PDFs to inert documents; embedded actions and attachments are unsupported.
        if (
            not re.match(rb"%PDF-1\.[0-7][\r\n]", body)
            or not body.rstrip().endswith(b"%%EOF")
            or not all(
                token in body
                for token in (b" obj", b"endobj", b"/Root", b"startxref", b"trailer")
            )
            or re.search(
                rb"/(?:JavaScript|JS|OpenAction|AA|Launch|EmbeddedFile|RichMedia|XFA|AcroForm|URI)\b|<\s*(?:script|html)",
                body,
                re.I,
            )
        ):
            raise HTTPException(422, "Unsupported PDF content")
        # Compressed object streams can hide active objects; fail closed.
        if re.search(rb"/(?:ObjStm|Encrypt|Filter)\b|#[0-9a-fA-F]{2}", body):
            raise HTTPException(422, "Unsupported PDF encoding")
        return "application/pdf"
    if suffix in ("png", "jpg", "jpeg"):
        try:
            from PIL import Image

            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(body)) as image:
                    expected = "PNG" if suffix == "png" else "JPEG"
                    if (
                        image.format != expected
                        or image.width * image.height > 4_000_000
                        or getattr(image, "n_frames", 1) != 1
                    ):
                        raise ValueError()
                    image.verify()
        except Exception:
            raise HTTPException(422, "Invalid or unsupported image") from None
        return "image/png" if suffix == "png" else "image/jpeg"
    raise HTTPException(422, "Supported files: PNG, JPEG, PDF and plain UTF-8 TXT")


def install_workspace_files(
    api, cloud, operator_owner_dependency, driver_owner_dependency
):
    with cloud._write_lock, cloud.db:
        cloud.db.executescript("""
        CREATE TABLE IF NOT EXISTS workspace_files (
          file_id TEXT PRIMARY KEY, owner TEXT NOT NULL, filename TEXT NOT NULL,
          media_type TEXT NOT NULL, size INTEGER NOT NULL, sha256 TEXT NOT NULL,
          created_at TEXT NOT NULL, content BLOB NOT NULL);
        CREATE INDEX IF NOT EXISTS workspace_files_owner ON workspace_files(owner,created_at,file_id);
        CREATE TABLE IF NOT EXISTS workspace_file_commands (
          owner TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
          file_id TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(owner,request_id));
        """)

    def metadata(row):
        return {
            key: row[key]
            for key in (
                "file_id",
                "filename",
                "media_type",
                "size",
                "sha256",
                "created_at",
            )
        }

    def register(role, dependency):
        router = APIRouter(prefix=f"/api/v1/{role}/workspace/files")

        def owner_key(owner):
            return role + ":" + owner["key"]

        def find(file_id, owner):
            with cloud._write_lock:
                row = cloud.db.execute(
                    "SELECT * FROM workspace_files WHERE file_id=? AND owner=?",
                    (file_id, owner_key(owner)),
                ).fetchone()
            if row is None:
                raise HTTPException(404, "File not found")
            return row

        @router.get("")
        def listing(
            q: str = Query(default="", max_length=120),
            limit: int = Query(default=20, ge=1, le=40),
            offset: int = Query(default=0, ge=0, le=10000),
            owner=Depends(dependency),
        ):
            escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            args = (owner_key(owner), "%" + escaped + "%")
            with cloud._write_lock:
                total = cloud.db.execute(
                    "SELECT count(*) FROM workspace_files WHERE owner=? AND filename LIKE ? ESCAPE '\\'",
                    args,
                ).fetchone()[0]
                rows = cloud.db.execute(
                    "SELECT file_id,filename,media_type,size,sha256,created_at FROM workspace_files WHERE owner=? AND filename LIKE ? ESCAPE '\\' ORDER BY created_at DESC,file_id LIMIT ? OFFSET ?",
                    (*args, limit, offset),
                ).fetchall()
            return {
                "items": [metadata(row) for row in rows],
                "total": total,
                "limit": limit,
                "offset": offset,
            }

        @router.post("", status_code=201)
        async def upload(
            request: Request,
            filename: str = Query(max_length=120),
            request_id: str = Query(pattern=r"^[A-Za-z0-9_-]{8,80}$"),
            owner=Depends(dependency),
        ):
            declared = request.headers.get("content-length")
            if declared and (not declared.isdigit() or int(declared) > MAX_FILE_BYTES):
                raise HTTPException(413, "File exceeds 2 MiB")
            body = bytearray()
            async for chunk in request.stream():
                if len(body) + len(chunk) > MAX_FILE_BYTES:
                    raise HTTPException(413, "File exceeds 2 MiB")
                body.extend(chunk)
            body = bytes(body)
            media = validate_file(filename, body)
            command_hash = hashlib.sha256(
                filename.encode() + b"\x00" + body
            ).hexdigest()
            if media.startswith("image/"):
                # Decode and re-encode pixels to discard metadata, trailing payloads and polyglots.
                from PIL import Image

                try:
                    with Image.open(io.BytesIO(body)) as image:
                        clean = Image.new(
                            "RGBA" if media == "image/png" else "RGB", image.size
                        )
                        clean.paste(image.convert(clean.mode))
                        target = io.BytesIO()
                        clean.save(
                            target, format="PNG" if media == "image/png" else "JPEG"
                        )
                        body = target.getvalue()
                except Exception:
                    raise HTTPException(422, "Image cannot be decoded") from None
                if len(body) > MAX_FILE_BYTES:
                    raise HTTPException(413, "Decoded image exceeds storage limit")
            digest = hashlib.sha256(body).hexdigest()
            key = owner_key(owner)
            with cloud._write_lock, cloud.db:
                cloud.db.execute("BEGIN IMMEDIATE")
                previous = cloud.db.execute(
                    "SELECT * FROM workspace_file_commands WHERE owner=? AND request_id=?",
                    (key, request_id),
                ).fetchone()
                if previous:
                    if previous["digest"] != command_hash:
                        raise HTTPException(
                            409, "Upload request already used for different content"
                        )
                    return metadata(find(previous["file_id"], owner))
                timestamp = datetime.now(timezone.utc).isoformat()
                # ISO timestamp comparison uses a matching ISO prefix and explicit UTC offset.
                from datetime import timedelta

                cutoff = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
                recent = cloud.db.execute(
                    "SELECT count(*) FROM workspace_file_commands WHERE owner=? AND created_at>?",
                    (key, cutoff),
                ).fetchone()[0]
                if recent >= 12:
                    raise HTTPException(429, "Upload rate limit; try again later")
                # Keep command tombstones bounded too. Preserve successful upload retries;
                # only expired tombstones for deleted files may be discarded.
                cutoff_history = (
                    datetime.now(timezone.utc) - timedelta(days=1)
                ).isoformat()
                cloud.db.execute(
                    "DELETE FROM workspace_file_commands WHERE owner=? AND created_at<? AND file_id NOT IN (SELECT file_id FROM workspace_files WHERE owner=?)",
                    (key, cutoff_history, key),
                )
                command_count = cloud.db.execute(
                    "SELECT count(*) FROM workspace_file_commands WHERE owner=?", (key,)
                ).fetchone()[0]
                if command_count >= MAX_OWNER_COMMANDS:
                    raise HTTPException(
                        429,
                        "Upload history limit reached; deleted-file retries expire after 24 hours",
                    )
                count, size = cloud.db.execute(
                    "SELECT count(*),coalesce(sum(size),0) FROM workspace_files WHERE owner=?",
                    (key,),
                ).fetchone()
                if count >= MAX_OWNER_FILES or size + len(body) > MAX_OWNER_BYTES:
                    raise HTTPException(413, "Private file storage quota reached")
                file_id = str(uuid.uuid4())
                cloud.db.execute(
                    "INSERT INTO workspace_files VALUES (?,?,?,?,?,?,?,?)",
                    (file_id, key, filename, media, len(body), digest, timestamp, body),
                )
                cloud.db.execute(
                    "INSERT INTO workspace_file_commands VALUES (?,?,?,?,?)",
                    (key, request_id, command_hash, file_id, timestamp),
                )
                return metadata(find(file_id, owner))

        @router.get("/{file_id}")
        def detail(file_id: str, owner=Depends(dependency)):
            return metadata(find(file_id, owner))

        def content(file_id, owner, preview=False):
            row = find(file_id, owner)
            # PDFs always download: no browser PDF engine execution on application origin.
            inline = preview and row["media_type"] != "application/pdf"
            return Response(
                row["content"],
                media_type=row["media_type"],
                headers={
                    "Content-Disposition": f"{'inline' if inline else 'attachment'}; filename=\"{row['filename']}\"; filename*=UTF-8''{quote(row['filename'])}",
                    "X-Content-Type-Options": "nosniff",
                    "Content-Security-Policy": "sandbox; default-src 'none'",
                    "Cache-Control": "no-store",
                    "Cross-Origin-Resource-Policy": "same-origin",
                },
            )

        @router.get("/{file_id}/download")
        def download(file_id: str, owner=Depends(dependency)):
            return content(file_id, owner)

        @router.get("/{file_id}/preview")
        def preview(file_id: str, owner=Depends(dependency)):
            return content(file_id, owner, True)

        @router.delete("/{file_id}", status_code=204)
        def delete(file_id: str, owner=Depends(dependency)):
            with cloud._write_lock, cloud.db:
                cloud.db.execute("BEGIN IMMEDIATE")
                find(file_id, owner)
                cloud.db.execute(
                    "DELETE FROM workspace_files WHERE file_id=? AND owner=?",
                    (file_id, owner_key(owner)),
                )
            return Response(status_code=204)

        api.include_router(router)

    register("operator", operator_owner_dependency)
    register("driver", driver_owner_dependency)
