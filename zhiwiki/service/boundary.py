"""Authenticate before body parsing and bound streamed requests without buffering."""

import asyncio
import re
import time
import logging
from urllib.parse import urlsplit

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse

from .common import ServiceError, digest, uid


def error_response(error, request_id):
    headers = {"X-Request-Id": request_id}
    if error.retryable:
        headers["Retry-After"] = "1"
    if getattr(error, "location", None):
        headers["Location"] = "/v1/workspaces/default/retrievals/" + error.location
    return JSONResponse({"schema_version": "1.0", "request_id": request_id,
                         "error": {"code": error.code, "message": error.message, "retryable": error.retryable,
                                   **({"retry_after_ms": 1000} if error.retryable else {})}}, error.status, headers=headers)


class RequestBoundary:
    def __init__(self, app, authorization, settings, telemetry):
        self.app, self.auth, self.settings = app, authorization, settings
        self.telemetry = telemetry
        self.uploads = asyncio.Semaphore(2)
        self.upload_users = set()
        self.downloads = asyncio.Semaphore(4)
        self.download_users = set()

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        headers = dict(scope.get("headers", []))
        request_id = headers.get(b"x-request-id", b"").decode("latin1")
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", request_id):
            request_id = uid("req")
        scope.setdefault("state", {})["request_id"] = request_id
        scope["state"]["received_at"] = time.monotonic()
        traceparent = headers.get(b'traceparent', b'').decode('latin1')
        if re.fullmatch(r'00-(?!0{32}-)[0-9a-f]{32}-(?!0{16}-)[0-9a-f]{16}-[0-9a-f]{2}', traceparent):
            scope['state']['traceparent'] = traceparent
        path = scope["path"]
        principal = None
        upload = path.endswith("/uploads")
        lease = False
        download_lease = False
        status = 500
        response_started = False
        # Keep metric labels bounded even when callers probe random IDs/paths.
        operation = next((label for suffix, label in (("/retrievals", "retrieve"), ("/evidence:read", "read"), ("/citations:validate", "validate"), ("/uploads", "upload")) if path.endswith(suffix)), "other")
        try:
            origin = headers.get(b"origin", b"").decode("latin1")
            if origin and urlsplit(origin).netloc != headers.get(b"host", b"").decode("latin1"):
                raise ServiceError(403, "CROSS_SITE_REQUEST")
            public = path in ("/", "/login", "/health/live", "/health/ready") or (path == "/v1/sessions" and scope["method"] == "POST")
            if not public:
                from http.cookies import SimpleCookie
                cookies = SimpleCookie()
                cookies.load(headers.get(b"cookie", b"").decode("latin1"))
                bearer = headers.get(b"authorization", b"").decode("latin1")
                secret = bearer[7:] if bearer.startswith("Bearer ") else cookies["zw_session"].value if "zw_session" in cookies else ""
                principal = await asyncio.to_thread(self.auth.principal, secret)
                if not bearer and scope["method"] not in ("GET", "HEAD", "OPTIONS"):
                    csrf = headers.get(b"x-csrf-token", b"").decode("latin1")
                    if digest(csrf) != principal["csrf_hash"]:
                        raise ServiceError(403, "CSRF_FAILED")
                scope["state"]["principal"] = principal
            if path.endswith('/file'):
                if principal['id'] in self.download_users or self.downloads.locked():
                    raise ServiceError(429, 'DOWNLOAD_CONCURRENCY_LIMIT', retryable=True)
                await self.downloads.acquire()
                self.download_users.add(principal['id'])
                download_lease = True
            if upload:
                from urllib.parse import parse_qs
                target = parse_qs(scope.get("query_string", b"").decode()).get("knowledge_base_id", [""])[0]
                if not target:
                    raise ServiceError(400, "UPLOAD_KNOWLEDGE_BASE_REQUIRED")
                with self.auth.db.tx() as connection:
                    self.auth.require(connection, principal, [target], "ingest", "editor")
                if principal["id"] in self.upload_users or self.uploads.locked():
                    raise ServiceError(429, "UPLOAD_CONCURRENCY_LIMIT", retryable=True)
                await self.uploads.acquire()
                self.upload_users.add(principal["id"])
                lease = True
            limit = self.settings.max_upload_bytes + 65536 if upload else 131072
            declared = headers.get(b"content-length", b"0")
            if not declared.isdigit() or int(declared) > limit:
                raise ServiceError(413, "PAYLOAD_TOO_LARGE")
            started, received = time.monotonic(), 0

            async def bounded_receive():
                nonlocal received
                remaining = (self.settings.upload_timeout_seconds if upload else 10) - (time.monotonic() - started)
                try:
                    message = await asyncio.wait_for(receive(), max(0.001, min(30, remaining)))
                except asyncio.TimeoutError:
                    raise HTTPException(408, "REQUEST_BODY_TIMEOUT") from None
                received += len(message.get("body", b""))
                if received > limit:
                    raise HTTPException(413, "PAYLOAD_TOO_LARGE")
                return message

            async def tagged_send(message):
                nonlocal status, response_started
                if message["type"] == "http.response.start":
                    status, response_started = message["status"], True
                    message["headers"] = list(message.get("headers", [])) + [(b"x-request-id", request_id.encode())]
                # A stalled download cannot occupy a response task indefinitely.
                if download_lease and time.monotonic() - started >= 180:
                    raise asyncio.TimeoutError()
                await asyncio.wait_for(send(message), 10)

            await self.app(scope, bounded_receive, tagged_send)
        except ServiceError as error:
            status = error.status
            await error_response(error, request_id)(scope, receive, send)
        except Exception as error:
            logging.getLogger("zhiwiki.service").error("request_failed request_id=%s exception_type=%s", request_id, type(error).__name__)
            if not response_started:
                await error_response(ServiceError(500, "INTERNAL_ERROR"), request_id)(scope, receive, send)
            else:
                raise
        finally:
            milliseconds = (time.monotonic() - scope["state"]["received_at"]) * 1000
            self.telemetry.observe(operation, status, milliseconds)
            logging.getLogger("zhiwiki.service").info("request request_id=%s traceparent=%s operation=%s principal=%s status=%s elapsed_ms=%.3f", request_id, scope['state'].get('traceparent', ''), operation, principal["id"] if principal else "anonymous", status, milliseconds)
            if lease:
                self.upload_users.discard(principal["id"])
                self.uploads.release()
            if download_lease:
                self.download_users.discard(principal['id'])
                self.downloads.release()
