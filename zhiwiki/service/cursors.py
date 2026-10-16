"""Authenticated pagination tokens bound to owner, query, order and expiration."""

import base64
import hashlib
import hmac
import json
import secrets
import time

from .common import ServiceError, encode


def secret(connection):
    # This is server state, not a browser key or caller-supplied identity.
    connection.execute("INSERT OR IGNORE INTO team_runtime VALUES('cursor_key',?)", (secrets.token_hex(32),))
    return connection.execute("SELECT payload FROM team_runtime WHERE name='cursor_key'").fetchone()[0].encode()


def issue(key, owner, kb_id, after, limit):
    payload = encode([owner, kb_id, after, limit, int(time.time()) + 600]).encode()
    signature = hmac.new(key, payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(signature + payload).decode().rstrip('=')


def decode(key, value, owner, kb_id, limit):
    try:
        if len(value) > 2048:
            raise ValueError()
        raw = base64.urlsafe_b64decode(value + '=' * (-len(value) % 4))
        signature, payload = raw[:32], raw[32:]
        data = json.loads(payload)
        if (not hmac.compare_digest(signature, hmac.new(key, payload, hashlib.sha256).digest())
                or data[:2] != [owner, kb_id] or data[3] != limit or data[4] <= time.time()
                or not isinstance(data[2], str) or len(data[2]) > 100):
            raise ValueError()
        return data[2]
    except (ValueError, TypeError, IndexError, UnicodeError):
        raise ServiceError(400, 'INVALID_CURSOR') from None
