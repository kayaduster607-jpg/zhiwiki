"""Consistent snapshots and fail-closed restore into a new, empty directory."""

import json
import shutil
import sqlite3
import time
from contextlib import closing

from .common import encode, safe_path
from .database import ServiceDatabase
from .persistent_index import checksum


def backup_workspace(settings, destination):
    database = ServiceDatabase(settings)
    output = safe_path(destination)
    if output == database.root or database.root in output.parents:
        raise ValueError("Snapshot must be outside the live data directory")
    output.mkdir(parents=True, exist_ok=True)
    if any(output.iterdir()):
        raise ValueError("Use a new empty snapshot directory")
    started = time.monotonic()
    target = output / "knowledge.sqlite3"
    with closing(database.connect()) as source, closing(sqlite3.connect(str(target))) as backup:
        source.backup(backup)
    paths = ["knowledge.sqlite3"]
    # The database snapshot selects exactly which immutable blobs are needed.
    with closing(sqlite3.connect(str(target))) as connection:
        files = [("uploads/" + row[0]) for row in connection.execute("SELECT DISTINCT file_hash FROM team_revisions WHERE filename<>'legacy'")]
        files += [row[0] for row in connection.execute("SELECT artifact FROM team_kbs WHERE artifact IS NOT NULL")]
    for relative in sorted(set(files)):
        source = database.artifact_path(relative)
        if not source.is_file():
            raise ValueError("Snapshot source artifact missing: " + relative)
        copied = output / relative
        copied.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, copied)
        paths.append(relative)
    manifest = {"format": 1, "created_at": time.time(), "files": {relative: checksum(output / relative) for relative in paths}}
    (output / "manifest.json").write_text(encode(manifest), encoding="utf-8")
    return {"snapshot": str(output), "files": len(paths), "seconds": round(time.monotonic() - started, 3)}


def restore_workspace(snapshot, destination):
    source, target = safe_path(snapshot), safe_path(destination)
    if target == source or source in target.parents or target in source.parents:
        raise ValueError("Restore into a separate empty directory")
    manifest_path = source / 'manifest.json'
    if manifest_path.stat().st_size > 1024 * 1024:
        raise ValueError('Backup manifest too large')
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("format") != 1 or "knowledge.sqlite3" not in manifest.get("files", {}):
        raise ValueError("Unsupported backup manifest")
    verified = []
    for relative, expected in manifest["files"].items():
        path = safe_path(source / relative)
        output = safe_path(target / relative)
        if source not in path.parents or target not in output.parents or not path.is_file() or path.is_symlink():
            raise ValueError("Unsafe backup member")
        if checksum(path) != expected:
            raise ValueError("Backup checksum mismatch")
        verified.append((path, output))
    target.mkdir(parents=True, exist_ok=True)
    if any(target.iterdir()):
        raise ValueError("Restore never overwrites an existing workspace")
    started = time.monotonic()
    for path, output in verified:
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, output)
    with closing(sqlite3.connect(str(target / "knowledge.sqlite3"))) as connection:
        if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok" or connection.execute("PRAGMA foreign_key_check").fetchall():
            raise ValueError("Restored database failed integrity verification")
    return {"restored": str(target), "files": len(verified), "seconds": round(time.monotonic() - started, 3)}


def cleanup(settings):
    """Expire only short-lived receipts/keys; published/history content is kept."""
    database = ServiceDatabase(settings)
    return expire_ephemeral(database)


def expire_ephemeral(database):
    """Safe automatic expiry; audit/history/source retention is a separate policy."""
    with database.tx(write=True) as connection:
        receipts = connection.execute("DELETE FROM team_receipts WHERE expires<?", (time.time(),)).rowcount
        keys = connection.execute("DELETE FROM team_idempotency WHERE expires<?", (time.time(),)).rowcount
    return {"expired_receipts": receipts, "expired_keys": keys, "source_history_deleted": False}
