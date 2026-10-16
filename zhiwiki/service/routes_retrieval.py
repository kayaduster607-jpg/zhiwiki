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
                claimed = connection.execute("SELECT operation,status,expires FROM team_idempotency "
                                             "WHERE owner=? AND result_id=? AND operation IN ('retrieve','read')",
                                             (current['id'], retrieval_id)).fetchone()
                scope = claimed['operation'] if claimed else 'retrieve'
                services.auth.require(connection, current, scope=scope)
                if claimed and claimed['status'] == 'running' and claimed['expires'] > time.time():
                    # Location must remain pollable before a receipt exists.
                    # Other users cannot distinguish this from an unknown ID.
                    error = ServiceError(409, 'REQUEST_IN_PROGRESS', retryable=True)
                    error.location = retrieval_id
                    raise error
                return services.retrievals.receipt(connection, current, retrieval_id, scope=scope)
        current, stop = context(request), deadline(request, services.settings)
        # Status must remain available while the principal's tool slot is busy.
        # A small control-plane cap shares rate budgets, never execution slots.
        if not hasattr(services, 'status_slots'):
            services.status_slots = asyncio.Semaphore(4)
            services.status_users = set()
        if current['id'] in services.status_users:
            raise ServiceError(429, 'USER_CONCURRENCY_LIMIT', retryable=True)
        if services.status_slots.locked():
            raise ServiceError(503, 'OVERLOADED', retryable=True)
        services.gate.rate_check(current['id'])
        await services.status_slots.acquire()
        services.status_users.add(current['id'])
        try:
            future = asyncio.get_running_loop().run_in_executor(None, read, current, stop)
        except BaseException:
            services.status_users.discard(current['id'])
            services.status_slots.release()
            raise

        def finished(done):
            try:
                done.exception()
            except (asyncio.CancelledError, Exception):
                pass
            services.status_users.discard(current['id'])
            services.status_slots.release()

        future.add_done_callback(finished)
        try:
            result = await asyncio.wait_for(asyncio.shield(future), max(.001, stop - time.monotonic()))
        except asyncio.TimeoutError:
            raise ServiceError(504, 'DEADLINE_EXCEEDED') from None
        return envelope(request, result)

    @router.post("/evidence:read", tags=["retrieval"], response_model=RetrievalOutput)
    async def evidence(payload: ReadInput, request: Request):
        key = idempotency_key(request)
        return await execute(request, lambda current, stop: services.retrievals.read(current, payload.model_dump(), key, stop))

    @router.post("/citations:validate", tags=["retrieval"], response_model=ValidationOutput)
    async def validate(payload: ValidateInput, request: Request):
        return await execute(request, lambda current, stop: services.retrievals.validate(current, payload.model_dump(), stop))

    return router
