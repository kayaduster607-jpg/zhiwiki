"""Role-separated Wiki maintenance and administrator-only operational metadata."""

import json

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import PlainTextResponse

from .common import ServiceError, digest
from .models import WikiDraftInput
from .routes_retrieval import context, envelope, workspace


def maintenance_router(services):
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", dependencies=[Depends(workspace)])

    def wiki_scope(connection, request, wiki_id, role="reader"):
        row = connection.execute("SELECT t.kb_id,w.* FROM team_wikis t JOIN wikis w ON w.id=t.wiki_id WHERE w.id=?", (wiki_id,)).fetchone()
        if not row:
            raise ServiceError(404, "RESOURCE_NOT_FOUND")
        services.auth.require(connection, context(request), [row["kb_id"]], role=role)
        if role == "reader" and (row["status"] != "published" or row["stale"]):
            services.auth.require(connection, context(request), [row["kb_id"]], role="editor")
        return row

    def source_scope(connection, kb_id, ids):
        for chunk_id in ids:
            row = connection.execute("SELECT b.kb_id,b.current_revision,p.revision_id,d.active FROM chunks c "
                                     "JOIN documents d ON d.id=c.document_id JOIN team_pages p ON p.document_id=d.id "
                                     "JOIN team_revisions r ON r.id=p.revision_id JOIN team_books b ON b.id=r.book_id WHERE c.id=?",
                                     (chunk_id,)).fetchone()
            if not row or row["kb_id"] != kb_id or not row["active"] or row["revision_id"] != row["current_revision"]:
                raise ServiceError(409, "KNOWLEDGE_CHANGED")

    @router.post("/wiki/drafts", status_code=201, tags=["wiki"])
    def draft(payload: WikiDraftInput, request: Request, knowledge_base_id: str):
        with services.db.tx(write=True) as connection:
            services.auth.require(connection, context(request), [knowledge_base_id], "ingest", "editor")
            source_scope(connection, knowledge_base_id, payload.evidence_ids)
            claims = []
            for evidence_id in payload.evidence_ids:
                text = connection.execute("SELECT text FROM chunks WHERE id=?", (evidence_id,)).fetchone()[0]
                claims.append({"text": "【离线原文摘录，未经模型综合】\n" + text, "evidence_ids": [evidence_id]})
            claims, sources = services.db.core._validate_claims(claims)
            slug = "t-" + digest(knowledge_base_id)[:12] + "-" + payload.slug
            latest = connection.execute("SELECT MAX(version) FROM wikis WHERE slug=?", (slug,)).fetchone()[0]
            if latest is not None and payload.expected_version != latest:
                raise ServiceError(409, "VERSION_CONFLICT")
            page = services.db.core._insert_wiki_revision(connection, slug, payload.title, claims, sources, [], "offline", (latest or 0) + 1)
            connection.execute("INSERT INTO team_wikis VALUES(?,?,?)", (page["id"], knowledge_base_id, context(request)["id"]))
            services.db.audit(connection, context(request)["id"], "wiki.draft", page["id"])
        return envelope(request, page)

    @router.get("/wiki/{wiki_id}", tags=["wiki"])
    def wiki(wiki_id: str, request: Request):
        with services.db.tx() as connection:
            row = wiki_scope(connection, request, wiki_id)
            return envelope(request, services.db.core._wiki(connection, row))

    @router.post("/wiki/{wiki_id}:publish", tags=["wiki"])
    def publish(wiki_id: str, request: Request):
        from .routes_retrieval import idempotency_key
        key = idempotency_key(request)
        expected = request.headers.get("If-Match")
        if expected is None:
            raise ServiceError(428, "PRECONDITION_REQUIRED")
        with services.db.tx(write=True) as connection:
            row = wiki_scope(connection, request, wiki_id, "publisher")
            services.auth.require(connection, context(request), [row["kb_id"]], "publish", "publisher")
            request_hash = digest(wiki_id + ":" + expected)
            previous = services.jobs.idempotent(connection, context(request), "wiki_publish", key, request_hash)
            if previous and (row["status"] != "published" or row["stale"]):
                raise ServiceError(409, "KNOWLEDGE_CHANGED")
            if expected.strip('"') != str(row["version"]):
                raise ServiceError(412, "PRECONDITION_FAILED")
            latest = connection.execute("SELECT MAX(version) FROM wikis WHERE slug=?", (row["slug"],)).fetchone()[0]
            if latest != row["version"] or row["stale"]:
                raise ServiceError(409, "VERSION_CONFLICT")
            claims = json.loads(row["claims_json"])
            source_scope(connection, row["kb_id"], [value for claim in claims for value in claim["evidence_ids"]])
            connection.execute("UPDATE wikis SET status='archived' WHERE slug=? AND status='published' AND id<>?", (row["slug"], wiki_id))
            connection.execute("UPDATE wikis SET status='published' WHERE id=?", (wiki_id,))
            services.db.audit(connection, context(request)["id"], "wiki.publish", wiki_id)
            services.jobs.bind(connection, context(request), "wiki_publish", key, request_hash, wiki_id)
        return envelope(request, {"id": wiki_id, "status": "published"})

    @router.get("/wiki/{wiki_id}/export", tags=["wiki"])
    def export(wiki_id: str, request: Request):
        with services.db.tx() as connection:
            row = wiki_scope(connection, request, wiki_id)
            body = services.db.core._wiki(connection, row)["body"]
        return PlainTextResponse(body, media_type="text/markdown", headers={"Content-Disposition": 'attachment; filename="wiki.md"'})

    @router.get("/audit-events", tags=["admin"])
    def audit(request: Request, after: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=100)):
        with services.db.tx() as connection:
            services.auth.admin(connection, context(request))
            rows = [dict(row) for row in connection.execute("SELECT * FROM team_audit WHERE id>? ORDER BY id LIMIT ?", (after, limit))]
        return envelope(request, {"events": rows})

    @router.get("/metrics", tags=["admin"])
    def metrics(request: Request):
        with services.db.tx() as connection:
            services.auth.admin(connection, context(request))
            jobs = {row[0]: row[1] for row in connection.execute("SELECT status,COUNT(*) FROM team_jobs GROUP BY status")}
            generations = {row[0]: row[1] for row in connection.execute("SELECT id,generation FROM team_kbs")}
            worker = connection.execute("SELECT payload FROM team_runtime WHERE name='worker_heartbeat'").fetchone()
        from .telemetry import resources
        return envelope(request, {"admission": services.gate.stats(), "jobs": jobs, "generations": generations,
                                  "http": services.telemetry.snapshot(), "resources": resources(services.db.root),
                                  "storage": services.db.storage_stats(),
                                  "worker_heartbeat_at": float(worker[0]) if worker else None})

    @router.post("/users/{user_id}:disable", tags=["admin"])
    def disable(user_id: str, request: Request):
        with services.db.tx(write=True) as connection:
            services.auth.admin(connection, context(request))
            connection.execute("UPDATE team_users SET disabled=1 WHERE id=?", (user_id,))
            services.db.audit(connection, context(request)["id"], "user.disable", user_id)
        return envelope(request, {"disabled": True})

    return router
