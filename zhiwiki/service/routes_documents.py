"""Authenticated streamed uploads, page provenance and explicit publication."""

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, Form, Query, Request, Response, UploadFile
from fastapi.responses import FileResponse

from ..config import reject_environment_path
from .common import ServiceError, encode, uid
from .models import MetadataInput, PageCorrectionInput, ReviewInput
from .routes_retrieval import context, envelope, idempotency_key, workspace


async def save_upload(services, uploaded):
    filename = uploaded.filename or ""
    reject_environment_path(Path(filename))
    if not filename or len(filename) > 200 or any(char in filename for char in ("/", "\\", ":", "\x00")):
        raise ServiceError(422, "INVALID_FILENAME")
    suffix = Path(filename).suffix.lower()
    if suffix not in (".pdf", ".md", ".txt", ".docx"):
        raise ServiceError(422, "UNSUPPORTED_FILE")
    limit = services.settings.max_upload_bytes if suffix == ".pdf" else 2 * 1024 * 1024
    temporary = services.db.root / "uploads" / (uid("upload") + ".pending")
    services.db.capacity_check(limit)
    hasher, size, prefix = hashlib.sha256(), 0, b""
    try:
        with temporary.open("wb") as handle:
            while True:
                block = await uploaded.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if size > limit:
                    raise ServiceError(413, "PAYLOAD_TOO_LARGE")
                if not prefix:
                    prefix = block[:1024]
                hasher.update(block)
                await asyncio.to_thread(handle.write, block)
        if not size or (suffix == ".pdf" and b"%PDF-" not in prefix):
            raise ServiceError(422, "INVALID_FILE_CONTENT")
        checksum = hasher.hexdigest()
        target = services.db.root / "uploads" / checksum
        if not target.exists():
            temporary.replace(target)
        return filename, checksum
    finally:
        # This is the exact generated temporary file, never a user-supplied path.
        temporary.unlink(missing_ok=True)
        await uploaded.close()


def documents_router(services):
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", dependencies=[Depends(workspace)])

    @router.get("/knowledge-bases", tags=["documents"])
    def libraries(request: Request):
        with services.db.tx() as connection:
            current = services.auth.current(connection, context(request))
            ids = services.db.kb_ids(connection, current["id"], current["admin"])
            if current["kind"] != "session":
                ids = [key for key in ids if key in current["kb_ids"]]
            values = [dict(connection.execute("SELECT id,title,generation FROM team_kbs WHERE id=?", (key,)).fetchone()) for key in ids]
        return envelope(request, {"knowledge_bases": values})

    @router.post("/uploads", status_code=202, tags=["documents"])
    async def upload(request: Request, response: Response, file: UploadFile = File(...), knowledge_base_id: str = Form(...),
                     mode: Literal["new", "update"] = Form(...), logical_source_key: str = Form(...),
                     document_id: Optional[str] = Form(None), expected_revision_id: Optional[str] = Form(None)):
        key = idempotency_key(request)
        if knowledge_base_id != request.query_params.get("knowledge_base_id"):
            raise ServiceError(400, "UPLOAD_KNOWLEDGE_BASE_MISMATCH")
        if not logical_source_key.strip() or len(logical_source_key) > 200 or "\x00" in logical_source_key:
            raise ServiceError(422, "INVALID_SOURCE_KEY")
        if mode == "new" and (document_id or expected_revision_id):
            raise ServiceError(422, "INVALID_UPDATE_TARGET")
        if mode == "update" and (not document_id or not expected_revision_id):
            raise ServiceError(428, "PRECONDITION_REQUIRED")
        filename, checksum = await save_upload(services, file)
        value = await asyncio.to_thread(services.jobs.enqueue, context(request), knowledge_base_id,
                                       filename, checksum, mode, logical_source_key, document_id, expected_revision_id, key)
        value["status_url"] = "/v1/workspaces/default/jobs/" + value["job_id"]
        response.headers['Location'] = value['status_url']
        return envelope(request, value)

    @router.get("/jobs/{job_id}", tags=["documents"])
    def job(job_id: str, request: Request):
        return envelope(request, services.jobs.get(context(request), job_id))

    @router.post("/jobs/{job_id}:cancel", status_code=202, tags=["documents"])
    def cancel(job_id: str, request: Request):
        return envelope(request, services.jobs.cancel(context(request), job_id))

    @router.post("/jobs/{job_id}:retry", status_code=202, tags=["documents"])
    def retry(job_id: str, request: Request):
        return envelope(request, services.jobs.retry(context(request), job_id))

    @router.get("/documents", tags=["documents"])
    def documents(request: Request, knowledge_base_id: str, cursor: str = "", limit: int = Query(20, ge=1, le=100)):
        from .cursors import decode, issue
        owner = context(request)["id"]
        after = decode(services.db.cursor_key, cursor, owner, knowledge_base_id, limit) if cursor else ""
        with services.db.tx() as connection:
            services.auth.require(connection, context(request), [knowledge_base_id])
            rows = connection.execute("SELECT id,title,current_revision FROM team_books WHERE kb_id=? AND id>? ORDER BY id LIMIT ?",
                                      (knowledge_base_id, after, limit + 1)).fetchall()
        result = [dict(row) for row in rows[:limit]]
        return envelope(request, {"documents": result, "next_cursor": issue(services.db.cursor_key, owner, knowledge_base_id, result[-1]["id"], limit) if len(rows) > limit else None})

    def accessible_revision(connection, request, book_id, revision_id):
        row = services.jobs.revision(connection, book_id, revision_id)
        services.auth.require(connection, context(request), [row["kb_id"]])
        if row["current_revision"] != revision_id:
            services.auth.require(connection, context(request), [row["kb_id"]], role="editor")
        return row

    @router.get("/documents/{book_id}/revisions/{revision_id}", tags=["documents"])
    def revision(book_id: str, revision_id: str, request: Request):
        with services.db.tx() as connection:
            row = accessible_revision(connection, request, book_id, revision_id)
            return envelope(request, {"document_id": book_id, "revision_id": revision_id, "status": row["status"],
                                      "number": row["number"], "quality": json.loads(row["quality"]), "reviewed": bool(row["reviewed"])})

    @router.get("/documents/{book_id}/revisions/{revision_id}/pages/{page}", tags=["documents"])
    def page(book_id: str, revision_id: str, page: int, request: Request):
        with services.db.tx() as connection:
            accessible_revision(connection, request, book_id, revision_id)
            row = connection.execute("SELECT d.text,p.raw_text,p.quality FROM team_pages p JOIN documents d ON d.id=p.document_id WHERE p.revision_id=? AND p.page=?",
                                     (revision_id, page)).fetchone()
            if not row:
                raise ServiceError(404, "RESOURCE_NOT_FOUND")
            from .common import digest
            return envelope(request, {"pdf_page": page, "text": row["text"], "raw_text": row["raw_text"], "text_sha256": digest(row["text"]), "quality": json.loads(row["quality"])})

    @router.put("/documents/{book_id}/revisions/{revision_id}/pages/{page}", tags=["documents"])
    def correct(book_id: str, revision_id: str, page: int, payload: PageCorrectionInput, request: Request):
        return envelope(request, services.jobs.correct_page(context(request), book_id, revision_id, page,
                                                           payload.text, payload.expected_text_sha256, payload.note))

    @router.get("/documents/{book_id}/revisions/{revision_id}/file", tags=["documents"])
    def download(book_id: str, revision_id: str, request: Request):
        with services.db.tx() as connection:
            row = accessible_revision(connection, request, book_id, revision_id)
            services.auth.require(connection, context(request), [row["kb_id"]], "download")
            path = services.db.root / "uploads" / row["file_hash"]
            if not path.is_file():
                raise ServiceError(404, "RESOURCE_NOT_FOUND")
            filename = row["filename"]
        return FileResponse(path, filename=filename)

    @router.post("/documents/{book_id}/revisions/{revision_id}:review", tags=["documents"])
    def review(book_id: str, revision_id: str, payload: ReviewInput, request: Request):
        return envelope(request, services.jobs.review(context(request), book_id, revision_id, payload.note))

    @router.post("/documents/{book_id}/revisions/{revision_id}:publish", tags=["documents"])
    async def publish(book_id: str, revision_id: str, request: Request):
        key = idempotency_key(request)
        expected = request.headers.get("If-Match")
        new = request.headers.get("If-None-Match") == "*"
        if not expected and not new:
            raise ServiceError(428, "PRECONDITION_REQUIRED")
        if expected and new:
            raise ServiceError(400, "BAD_REQUEST")
        result = await asyncio.to_thread(services.jobs.publish_idempotent, context(request), book_id, revision_id,
                                        expected.strip('"') if expected else None, new, key)
        return envelope(request, result)

    def update_metadata(book_id, payload, request):
        if payload.effective_from and payload.effective_to and payload.effective_from > payload.effective_to:
            raise ServiceError(422, "INVALID_EFFECTIVE_RANGE")
        with services.db.tx(write=True) as connection:
            book = connection.execute("SELECT kb_id FROM team_books WHERE id=?", (book_id,)).fetchone()
            if not book:
                raise ServiceError(404, "RESOURCE_NOT_FOUND")
            services.auth.require(connection, context(request), [book[0]], "ingest", "editor")
            connection.execute("INSERT OR REPLACE INTO team_metadata VALUES(?,?)", (book_id, encode(payload.model_dump(mode="json"))))
            connection.execute("UPDATE team_kbs SET generation=generation+1 WHERE id=?", (book[0],))
            generation = connection.execute("SELECT generation FROM team_kbs WHERE id=?", (book[0],)).fetchone()[0]
            services.db.audit(connection, context(request)["id"], "metadata.update", book_id)
        # Content index is unchanged, but receipts must observe metadata updates.
        services.indexes.install(book[0], generation, services.indexes.get(book[0], generation - 1))
        return envelope(request, {"updated": True})

    @router.put("/documents/{book_id}/metadata", tags=["documents"])
    def metadata(book_id: str, payload: MetadataInput, request: Request):
        # Serialize the metadata generation switch with other preparations so
        # repeated edits cannot leave the in-memory generation behind the DB.
        with services.indexes.build_lock:
            return update_metadata(book_id, payload, request)

    return router
