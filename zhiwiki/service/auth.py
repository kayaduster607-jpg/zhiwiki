"""Revocable opaque credentials and current, transaction-scoped authorization."""

import hashlib
import hmac
import json
import re
import secrets
import time

from .common import ServiceError, digest, encode, uid


ROLES = {"reader": 1, "editor": 2, "publisher": 3, "admin": 4}
READ_SCOPES = {"retrieve", "read", "validate", "download"}


def password_hash(password, salt=None):
    if not isinstance(password, str) or not 12 <= len(password) <= 1024:
        raise ValueError("Password must contain 12–1024 characters")
    salt = salt or secrets.token_hex(16)
    hashed = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()
    return salt + ":" + hashed


class Authorization:
    def __init__(self, database):
        self.db = database

    def create_user(self, username, password, admin=False, actor=None):
        if not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{2,64}", username):
            raise ValueError("Username must use 2–64 ASCII letters, digits, dots, underscores or hyphens")
        hashed, user_id = password_hash(password), uid("usr")
        with self.db.tx(write=True) as connection:
            if actor:
                self.admin(connection, actor)
            if connection.execute("SELECT 1 FROM team_users WHERE username=?", (username,)).fetchone():
                raise ServiceError(409, "USERNAME_EXISTS")
            connection.execute("INSERT INTO team_users(id,username,password,admin) VALUES(?,?,?,?)",
                               (user_id, username, hashed, int(admin)))
            self.db.audit(connection, actor["id"] if actor else "bootstrap", "user.create", user_id, {"admin": bool(admin)})
        return user_id

    def login(self, username, password):
        with self.db.tx() as connection:
            row = connection.execute("SELECT * FROM team_users WHERE username=?", (username,)).fetchone()
        # Run the same KDF even for unknown usernames to reduce timing disclosure.
        stored = row["password"] if row else "00" * 16 + ":" + "00" * 64
        try:
            verified = hmac.compare_digest(password_hash(password, stored.split(":")[0]), stored)
        except ValueError:
            verified = False
        if not row or row["disabled"] or not verified:
            raise ServiceError(401, "UNAUTHENTICATED")
        return self.issue(row["id"], "session", ["*"], [], 8 * 3600)

    def issue(self, user_id, kind="token", scopes=None, kb_ids=None, seconds=None, actor=None):
        secret, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
        scopes = sorted(READ_SCOPES if scopes is None else scopes)
        if not scopes:
            raise ValueError('At least one explicit token scope is required')
        if kind != "session" and not set(scopes).issubset(READ_SCOPES):
            raise ServiceError(403, "PERMISSION_DENIED")
        lifetime = seconds or self.db.settings.token_days * 86400
        if not 60 <= lifetime <= 7 * 86400:
            raise ValueError("Token lifetime must be between 60 seconds and seven days")
        with self.db.tx(write=True) as connection:
            if actor:
                current = self.current(connection, actor)
                if current["kind"] != "session" or current["id"] != user_id:
                    raise ServiceError(403, "PERMISSION_DENIED")
            user = connection.execute("SELECT * FROM team_users WHERE id=? AND disabled=0", (user_id,)).fetchone()
            if not user:
                raise ServiceError(401, "UNAUTHENTICATED")
            allowed = self.db.kb_ids(connection, user_id, user["admin"])
            selected = list(kb_ids or allowed)
            if not set(selected).issubset(allowed):
                raise ServiceError(403, "PERMISSION_DENIED")
            connection.execute("INSERT INTO team_tokens VALUES(?,?,?,?,?,?,?,0,?)",
                               (digest(secret), user_id, kind, encode(scopes), encode(selected),
                                time.time() + lifetime, digest(csrf), time.time()))
            self.db.audit(connection, user_id, "credential.issue", digest(secret), {"kind": kind})
        return {"token": secret, "csrf": csrf, "token_id": digest(secret), "expires_in": lifetime}

    def principal(self, secret):
        if not secret or len(secret) > 512:
            raise ServiceError(401, "UNAUTHENTICATED")
        with self.db.tx() as connection:
            return self.current(connection, {"token_hash": digest(secret)})

    def current(self, connection, context):
        row = connection.execute("SELECT t.*,u.username,u.admin,u.disabled FROM team_tokens t "
                                 "JOIN team_users u ON u.id=t.user_id WHERE t.hash=?",
                                 (context["token_hash"],)).fetchone()
        if not row or row["revoked"] or row["disabled"] or row["expires"] <= time.time():
            raise ServiceError(401, "UNAUTHENTICATED")
        return {"id": row["user_id"], "token_hash": row["hash"], "username": row["username"],
                "admin": bool(row["admin"]), "kind": row["kind"], "csrf_hash": row["csrf_hash"],
                "scopes": json.loads(row["scopes"]), "kb_ids": json.loads(row["kb_ids"])}

    def require(self, connection, context, kb_ids=(), scope="read", role="reader"):
        current = self.current(connection, context)
        if current["kind"] != "session" and scope not in current["scopes"]:
            raise ServiceError(403, "PERMISSION_DENIED")
        for kb_id in set(kb_ids):
            if not connection.execute("SELECT 1 FROM team_kbs WHERE id=?", (kb_id,)).fetchone():
                raise ServiceError(404, "RESOURCE_NOT_FOUND")
            if current["kind"] != "session" and kb_id not in current["kb_ids"]:
                raise ServiceError(403, "PERMISSION_DENIED")
            grant = connection.execute("SELECT role FROM team_grants WHERE user_id=? AND kb_id=?",
                                       (current["id"], kb_id)).fetchone()
            if not current["admin"] and (not grant or ROLES[grant["role"]] < ROLES[role]):
                raise ServiceError(403, "PERMISSION_DENIED")
        return current

    def admin(self, connection, context):
        current = self.current(connection, context)
        if not current["admin"] or current["kind"] != "session":
            raise ServiceError(403, "PERMISSION_DENIED")
        return current

    def create_kb(self, title, kb_id=None, actor=None):
        kb_id = kb_id or uid("kb")
        if not re.fullmatch(r"[A-Za-z0-9_-]{2,100}", kb_id) or not title.strip() or len(title) > 200:
            raise ValueError("Invalid knowledge base ID or title")
        with self.db.tx(write=True) as connection:
            if actor:
                self.admin(connection, actor)
            connection.execute("INSERT INTO team_kbs(id,title) VALUES(?,?)", (kb_id, title))
            self.db.audit(connection, actor["id"] if actor else "bootstrap", "knowledge_base.create", kb_id)
        return kb_id

    def grant(self, user_id, kb_id, role, actor=None):
        if role not in ROLES:
            raise ValueError("Use reader, editor, publisher or admin")
        with self.db.tx(write=True) as connection:
            if actor:
                self.admin(connection, actor)
            connection.execute("INSERT INTO team_grants VALUES(?,?,?) ON CONFLICT(user_id,kb_id) "
                               "DO UPDATE SET role=excluded.role", (user_id, kb_id, role))
            connection.execute("UPDATE team_kbs SET acl_epoch=acl_epoch+1 WHERE id=?", (kb_id,))
            self.db.audit(connection, actor["id"] if actor else "bootstrap", "grant.set", user_id, {"kb_id": kb_id, "role": role})
