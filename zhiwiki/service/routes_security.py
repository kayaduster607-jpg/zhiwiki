"""Local accounts, revocable browser sessions and user-limited API credentials."""

import asyncio
import json
import time
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .common import ServiceError
from .models import GrantInput, KnowledgeBaseInput, LoginInput, TokenInput, UserInput
from .routes_retrieval import context, envelope


def security_router(services):
    router = APIRouter()
    attempts = {}
    login_slots = asyncio.Semaphore(4)

    def limit_login(address, username):
        now = time.monotonic()
        if len(attempts) >= 2000:
            for key in list(attempts):
                if not attempts[key] or now - attempts[key][-1] > 60:
                    del attempts[key]
            if len(attempts) >= 2000:
                raise ServiceError(429, 'LOGIN_RATE_LIMIT', retryable=True)
        for key, maximum in (("ip:" + address, 20), ("user:" + username, 5)):
            records = attempts.setdefault(key, deque())
            while records and now - records[0] > 60:
                records.popleft()
            if len(records) >= maximum:
                raise ServiceError(429, "LOGIN_RATE_LIMIT", retryable=True)
            records.append(now)
        if len(attempts) > 2000:
            for key in list(attempts):
                if not attempts[key] or now - attempts[key][-1] > 60:
                    del attempts[key]

    @router.post("/v1/sessions", tags=["identity"])
    async def login(payload: LoginInput, request: Request):
        limit_login(request.client.host if request.client else "local", payload.username)
        async with login_slots:
            issued = await asyncio.to_thread(services.auth.login, payload.username, payload.password)
        response = JSONResponse(envelope(request, {"authenticated": True, "csrf_token": issued["csrf"]}))
        response.set_cookie("zw_session", issued["token"], max_age=issued["expires_in"], httponly=True,
                            secure=services.settings.cookie_secure, samesite="strict")
        response.set_cookie("zw_csrf", issued["csrf"], max_age=issued["expires_in"], httponly=False,
                            secure=services.settings.cookie_secure, samesite="strict")
        return response

    @router.delete("/v1/sessions/current", tags=["identity"])
    def logout(request: Request):
        current = context(request)
        with services.db.tx(write=True) as connection:
            services.auth.require(connection, current)
            connection.execute("UPDATE team_tokens SET revoked=1 WHERE hash=?", (current["token_hash"],))
        response = JSONResponse(envelope(request, {"logged_out": True}))
        response.delete_cookie("zw_session")
        response.delete_cookie("zw_csrf")
        return response

    @router.get("/v1/me", tags=["identity"])
    def me(request: Request):
        current = context(request)
        return envelope(request, {key: current[key] for key in ("id", "username", "admin", "kind", "scopes")})

    @router.post("/v1/tokens", tags=["identity"])
    def token(payload: TokenInput, request: Request):
        current = context(request)
        with services.db.tx() as connection:
            services.auth.current(connection, current)
            if current["kind"] != "session":
                raise ServiceError(403, "PERMISSION_DENIED")
        issued = services.auth.issue(current["id"], scopes=payload.scopes, kb_ids=payload.knowledge_base_ids,
                                     seconds=payload.expires_in, actor=current)
        return envelope(request, {key: issued[key] for key in ("token", "token_id", "expires_in")})

    @router.delete("/v1/tokens/{token_id}", tags=["identity"])
    def revoke(token_id: str, request: Request):
        with services.db.tx(write=True) as connection:
            current = services.auth.current(connection, context(request))
            row = connection.execute("SELECT user_id FROM team_tokens WHERE hash=?", (token_id,)).fetchone()
            if not row or (row[0] != current["id"] and not current["admin"]):
                raise ServiceError(404, "RESOURCE_NOT_FOUND")
            if current["kind"] != "session":
                raise ServiceError(403, "PERMISSION_DENIED")
            connection.execute("UPDATE team_tokens SET revoked=1 WHERE hash=?", (token_id,))
            services.db.audit(connection, current["id"], "credential.revoke", token_id)
        return envelope(request, {"revoked": True})

    @router.post("/v1/users", status_code=201, tags=["admin"])
    def create_user(payload: UserInput, request: Request):
        with services.db.tx() as connection:
            services.auth.admin(connection, context(request))
        return envelope(request, {"user_id": services.auth.create_user(payload.username, payload.password, payload.admin, context(request))})

    @router.post("/v1/workspaces/{workspace_id}/knowledge-bases", status_code=201, tags=["admin"])
    def create_kb(workspace_id: str, payload: KnowledgeBaseInput, request: Request):
        from .routes_retrieval import workspace
        workspace(workspace_id)
        with services.db.tx() as connection:
            services.auth.admin(connection, context(request))
        kb_id = services.auth.create_kb(payload.title, payload.id, context(request))
        return envelope(request, {"knowledge_base_id": kb_id})

    @router.put("/v1/workspaces/{workspace_id}/knowledge-bases/{kb_id}/members/{user_id}", tags=["admin"])
    def grant(workspace_id: str, kb_id: str, user_id: str, payload: GrantInput, request: Request):
        from .routes_retrieval import workspace
        workspace(workspace_id)
        with services.db.tx() as connection:
            services.auth.admin(connection, context(request))
        services.auth.grant(user_id, kb_id, payload.role, context(request))
        return envelope(request, {"granted": True})

    @router.delete("/v1/workspaces/{workspace_id}/knowledge-bases/{kb_id}/members/{user_id}", tags=["admin"])
    def ungrant(workspace_id: str, kb_id: str, user_id: str, request: Request):
        from .routes_retrieval import workspace
        workspace(workspace_id)
        with services.db.tx(write=True) as connection:
            services.auth.admin(connection, context(request))
            connection.execute("DELETE FROM team_grants WHERE user_id=? AND kb_id=?", (user_id, kb_id))
            connection.execute("UPDATE team_kbs SET acl_epoch=acl_epoch+1 WHERE id=?", (kb_id,))
            services.db.audit(connection, context(request)["id"], "grant.revoke", user_id, {"kb_id": kb_id})
        return envelope(request, {"revoked": True})

    return router
