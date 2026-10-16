"""Small public errors, stable hashes and explicitly loaded service settings."""

import hashlib
import json
import secrets
import sys
import time
import re
from dataclasses import dataclass, field, fields
from pathlib import Path

from ..config import reject_environment_path


class ServiceError(Exception):
    def __init__(self, status, code, message=None, retryable=False):
        self.status, self.code = status, code
        self.message = message or code
        self.retryable = retryable


def uid(prefix):
    return prefix + "_" + secrets.token_hex(16)


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def safe_path(value):
    path = Path(value)
    reject_environment_path(path)
    resolved = path.resolve()
    reject_environment_path(resolved)
    return resolved


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ServiceError(504, "DEADLINE_EXCEEDED")


@dataclass
class ServiceSettings:
    data_dir: str = "data-team"
    worker_python: str = sys.executable
    active_requests: int = 4
    accepted_requests: int = 20
    queue_timeout_ms: int = 1500
    request_timeout_ms: int = 2000
    cookie_secure: bool = True
    max_upload_bytes: int = 200 * 1024 * 1024
    upload_timeout_seconds: int = 180
    max_pdf_pages: int = 2000
    max_job_seconds: int = 1800
    worker_lease_seconds: int = 60
    worker_memory_mb: int = 1024
    page_timeout_seconds: int = 30
    token_days: int = 7
    receipt_seconds: int = 600
    user_rpm: int = 30
    global_rpm: int = 120
    user_burst: int = 6
    global_burst: int = 120
    max_workspace_bytes: int = 10 * 1024 * 1024 * 1024
    max_indexed_chunks: int = 50000
    allowed_hosts: list = field(default_factory=lambda: ["127.0.0.1", "localhost", "testserver"])

    def __post_init__(self):
        for item in fields(self):
            value = getattr(self, item.name)
            if item.name == "allowed_hosts":
                if (not isinstance(value, list) or not 1 <= len(value) <= 10
                        or any(not isinstance(host, str) or not re.fullmatch(r"[a-z0-9.-]{1,253}", host) for host in value)):
                    raise ValueError("Use 1–10 explicit lowercase trusted hosts, without wildcards")
            elif item.name in ("data_dir", "worker_python"):
                if not isinstance(value, str) or not value.strip():
                    raise ValueError(item.name + " must be a nonempty path")
                safe_path(value)
            elif item.name == "cookie_secure":
                if type(value) is not bool:
                    raise ValueError("cookie_secure must be boolean")
            elif type(value) is not int or value <= 0:
                raise ValueError(item.name + " must be a positive integer")
        if not 1 <= self.active_requests <= self.accepted_requests <= 100:
            raise ValueError("Use bounded active/accepted request limits")
        if not 50 <= self.request_timeout_ms <= 10000 or self.queue_timeout_ms >= self.request_timeout_ms:
            raise ValueError("Queue deadline must fit the request deadline")
        if self.max_upload_bytes > 200 * 1024 * 1024 or self.max_pdf_pages > 2000:
            raise ValueError("Upload and page safety limits cannot exceed the PRD")
        if not 128 <= self.worker_memory_mb <= 4096 or self.worker_lease_seconds <= self.page_timeout_seconds:
            raise ValueError("Use a bounded worker memory budget and a lease longer than the page deadline")
        if self.max_indexed_chunks > 50000 or self.token_days > 7:
            raise ValueError('Phase-one limits: 50,000 chunks per library and seven-day tokens')


def load_service_settings(path=None):
    values = {}
    base = Path.cwd()
    if path:
        source = safe_path(path)
        if source.suffix.lower() != ".json" or source.stat().st_size > 65536:
            raise ValueError("Use an explicit JSON configuration of at most 64 KiB")
        values = json.loads(source.read_text(encoding="utf-8-sig"))
        if not isinstance(values, dict):
            raise ValueError("Configuration must be an object")
        base = source.parent
    if set(values) - {item.name for item in fields(ServiceSettings)}:
        raise ValueError("Unknown service configuration fields")
    settings = ServiceSettings(**values)
    for name in ("data_dir", "worker_python"):
        value = Path(getattr(settings, name))
        setattr(settings, name, str(safe_path(value if value.is_absolute() else base / value)))
    return settings
