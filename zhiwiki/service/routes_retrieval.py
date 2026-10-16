"""Short read-only tool endpoints; queue time belongs to the same deadline."""

import re
import time
import asyncio

from fastapi import APIRouter, Depends, Request

from .common import ServiceError
from .models import ErrorOutput, ReadInput, RetrievalInput, RetrievalOutput, ValidateInput, ValidationOutput


def workspace(workspace_id):
    if workspace_id != "default":
        raise ServiceError(404, "RESOURCE_NOT_FOUND")


def context(request):
    return request.state.principal


def envelope(request, value):
    return {**value, "schema_version": "1.0", "request_id": request.state.request_id,
            "elapsed_ms": round((time.monotonic() - request.state.received_at) * 1000, 3)}


def deadline(request, settings):
    raw = request.headers.get("X-Request-Timeout-Ms", str(settings.request_timeout_ms))
    if not raw.isdigit() or not 50 <= int(raw) <= settings.request_timeout_ms:
        raise ServiceError(400, "INVALID_DEADLINE")
    return request.state.received_at + int(raw) / 1000


def idempotency_key(request):
    key = request.headers.get("Idempotency-Key", "")
    if not re.fullmatch(r"[A-Za-z0-9_:.\-]{8,128}", key):
        raise ServiceError(400, "IDEMPOTENCY_KEY_REQUIRED")
    return key


def retrieval_router(services):
    router = APIRouter(prefix="/v1/workspaces/{workspace_id}", dependencies=[Depends(workspace)],
                       responses={code: {"model": ErrorOutput} for code in (400, 401, 403, 409, 410, 422, 429, 503, 504)})

    async def execute(request, action):
        current = context(request)
        stop = deadline(request, services.settings)
        running = asyncio.create_task(services.gate.run(current["id"], stop, lambda: action(current, stop)))

        async def disconnected():
            while not running.done():
                if await request.is_disconnected():
                    running.cancel()
                    raise ServiceError(499, "CLIENT_DISCONNECTED")
                await asyncio.sleep(.05)

        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait([running, watcher], return_when=asyncio.FIRST_COMPLETED)
            if watcher in done and not running.done():
                await watcher
            result = await running
        finally:
            watcher.cancel()
            if not running.done():
                running.cancel()
            await asyncio.gather(watcher, running, return_exceptions=True)
        return envelope(request, result)

    @router.post("/retrievals", tags=["retrieval"], response_model=RetrievalOutput)
    async def retrieve(payload: RetrievalInput, request: Request):
        key = idempotency_key(request)
        body = payload.model_dump(mode="json")
        return await execute(request, lambda current, stop: services.retrievals.retrieve(current, body, key, stop))

    @router.get("/retrievals/{retrieval_id}", tags=["retrieval"], response_model=RetrievalOutput)
    async def result(retrieval_id: str, request: Request):
        def read(current, stop):
            with services.db.tx(deadline=stop) as connection:
                return services.retrievals.receipt(connection, current, retrieval_id, scope="retrieve")
        return await execute(request, read)

    @router.post("/evidence:read", tags=["retrieval"], response_model=RetrievalOutput)
    async def evidence(payload: ReadInput, request: Request):
        key = idempotency_key(request)
        return await execute(request, lambda current, stop: services.retrievals.read(current, payload.model_dump(), key, stop))

    @router.post("/citations:validate", tags=["retrieval"], response_model=ValidationOutput)
    async def validate(payload: ValidateInput, request: Request):
        return await execute(request, lambda current, stop: services.retrievals.validate(current, payload.model_dump(), stop))

    return router
