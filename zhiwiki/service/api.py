"""Team app factory: one API owner, separately supervised ingestion, no LLM calls."""

import asyncio
import sqlite3
import time
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from starlette.exceptions import HTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .admission import Admission
from .auth import Authorization
from .boundary import RequestBoundary, error_response
from .common import ServiceError, ServiceSettings
from .database import ServiceDatabase
from .indexes import Indexes
from .jobs import Jobs
from .locking import InstanceLock
from .retrievals import Retrievals
from .routes_retrieval import envelope, retrieval_router
from .routes_security import security_router
from .telemetry import Telemetry, resources


def create_service_app(settings=None, database=None):
    settings = settings or ServiceSettings()
    database = database or ServiceDatabase(settings)
    auth, indexes, gate = Authorization(database), Indexes(database), Admission(settings)
    services = SimpleNamespace(settings=settings, db=database, auth=auth, indexes=indexes, gate=gate, telemetry=Telemetry(),
                               retrievals=Retrievals(database, auth, indexes), jobs=Jobs(database, auth, indexes))
    instance = InstanceLock(database.root / "team-api.lock")

    async def expire_receipts():
        from .operations import expire_ephemeral
        while True:
            await asyncio.sleep(60)
            try:
                await asyncio.to_thread(expire_ephemeral, database)
            except Exception as error:
                logging.getLogger("zhiwiki.service").warning("expiry_failed exception_type=%s", type(error).__name__)

    @asynccontextmanager
    async def lifespan(app):
        instance.acquire()
        expiry = None
        try:
            await asyncio.to_thread(indexes.warm)
            # Exclusive API ownership and successful warm-up establish the only
            # safe restart point for abandoned HTTP claims/publication status.
            with database.tx(write=True) as connection:
                connection.execute("UPDATE team_jobs SET status='ready',updated=? WHERE status='publishing' AND revision_id IN (SELECT id FROM team_revisions WHERE status IN ('published','archived'))", (time.time(),))
                connection.execute("UPDATE team_idempotency SET status='done' WHERE status='running' AND operation='publish' AND result_id IN (SELECT id FROM team_revisions WHERE status IN ('published','archived'))")
                connection.execute("DELETE FROM team_idempotency WHERE status='running' AND operation IN ('retrieve','read','publish')")
            expiry = asyncio.create_task(expire_receipts())
            yield
        finally:
            if expiry:
                expiry.cancel()
                await asyncio.gather(expiry, return_exceptions=True)
            await asyncio.to_thread(gate.close)
            instance.close()

    app = FastAPI(title="知证 · Team Retrieval API", version="1.0.0", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url="/v1/openapi.json")
    app.state.services = services
    app.add_middleware(RequestBoundary, authorization=auth, settings=settings, telemetry=services.telemetry)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.allowed_hosts)

    @app.exception_handler(ServiceError)
    async def service_error(request, error):
        return error_response(error, request.state.request_id)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        # Do not echo input values such as passwords or opaque credentials.
        return error_response(ServiceError(422, "VALIDATION_ERROR"), request.state.request_id)

    @app.exception_handler(ValueError)
    async def value_error(request, error):
        return error_response(ServiceError(422, "VALIDATION_ERROR"), request.state.request_id)

    @app.exception_handler(HTTPException)
    async def http_error(request, error):
        return error_response(ServiceError(error.status_code, "PAYLOAD_TOO_LARGE" if error.status_code == 413 else "REQUEST_REJECTED"), request.state.request_id)

    @app.exception_handler(sqlite3.IntegrityError)
    async def conflict(request, error):
        return error_response(ServiceError(409, "STATE_CONFLICT"), request.state.request_id)

    @app.get("/health/live", include_in_schema=False)
    async def live():
        return {"status": "ok"}

    @app.get("/health/ready", include_in_schema=False)
    def ready():
        try:
            with database.tx(write=True) as connection:
                connection.execute("SELECT 1").fetchone()
                for row in connection.execute("SELECT id,generation FROM team_kbs"):
                    indexes.get(row[0], row[1])
            if resources(database.root)["free_disk_bytes"] < 512 * 1024 * 1024:
                raise OSError("Low disk")
            return {"status": "ready"}
        except (ServiceError, OSError):
            return JSONResponse({"status": "not_ready"}, 503)

    @app.get("/v1/capabilities")
    def capabilities(request: Request):
        return envelope(request, {"capabilities": ["retrieve", "evidence_read", "citation_validate", "pdf_text_ingest_jobs"],
                                  "model_calls_on_retrieve": 0,
                                  "limits": {"query_chars": 4000, "top_k": 20, "context_char_budget": 20000,
                                             "request_timeout_ms": settings.request_timeout_ms,
                                             "accepted_requests": settings.accepted_requests}})

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
    def legacy_disabled(path: str):
        raise ServiceError(410, "LEGACY_API_DISABLED", "团队模式使用受权的 /v1 接口；原工作台保持独立本地模式。")

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    @app.get("/login", response_class=HTMLResponse, include_in_schema=False)
    def login_page():
        from .web import CONSOLE
        return CONSOLE

    app.include_router(security_router(services))
    app.include_router(retrieval_router(services))
    from .routes_documents import documents_router
    from .routes_maintenance import maintenance_router
    app.include_router(documents_router(services))
    app.include_router(maintenance_router(services))
    return app
