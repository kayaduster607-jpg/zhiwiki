"""Short transactions, persistent state and non-destructive legacy migration."""

import json
import sqlite3
import time
import threading
import shutil
from collections import deque
from contextlib import contextmanager, closing

from ..store import KnowledgeStore
from .common import ServiceError, check_deadline, encode, safe_path, uid
from .schema import SCHEMA


class ServiceDatabase:
    def __init__(self, settings):
        self.settings = settings
        self.write_lock = threading.RLock()
        self.storage_lock = threading.Lock()
        self.busy_count = 0
        self.write_timings = deque(maxlen=2048)
        self.root = safe_path(settings.data_dir)
        self.root.mkdir(parents=True, exist_ok=True)
        self.backup_before_migration()
        self.core = KnowledgeStore(self.root)
        self.path = self.core.db_path
        self.root.joinpath("uploads").mkdir(exist_ok=True)
        self.root.joinpath("indexes").mkdir(exist_ok=True)
        with closing(self.connect()) as connection:
            connection.executescript("BEGIN IMMEDIATE;" + SCHEMA + "COMMIT;")
        from .cursors import secret
        with self.tx(write=True) as connection:
            self.cursor_key = secret(connection)
        self.migrate_legacy()

    def connect(self):
        connection = sqlite3.connect(str(self.path), timeout=1, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=1000")
        return connection

    def backup_before_migration(self):
        """Take a consistent, non-overwriting snapshot before first team DDL."""
        source = self.root / 'knowledge.sqlite3'
        if not source.exists():
            return
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as connection:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='team_schema'").fetchone():
                return
            snapshots = self.root / 'migration-snapshots'
            snapshots.mkdir(exist_ok=True)
            target = snapshots / (uid('before-team') + '.sqlite3')
            with closing(sqlite3.connect(str(target))) as backup:
                connection.backup(backup)

    @contextmanager
    def tx(self, write=False, deadline=None):
        # SQLite has one writer. Serialize local writers before opening their
        # transaction so a busy retry cannot starve another admitted request.
        acquired = False
        began = time.monotonic()
        if write:
            acquired = self.write_lock.acquire(timeout=max(.001, deadline - time.monotonic()) if deadline else 5)
            if not acquired:
                check_deadline(deadline)
                raise ServiceError(503, "STORAGE_UNAVAILABLE", retryable=True)
        try:
            connection = self.connect()
        except BaseException:
            if acquired:
                self.write_lock.release()
            raise
        try:
            if deadline is not None:
                remaining = max(1, min(1000, int((deadline - time.monotonic()) * 1000)))
                connection.execute("PRAGMA busy_timeout={}".format(remaining))
                connection.set_progress_handler(lambda: int(time.monotonic() >= deadline), 1000)
            check_deadline(deadline)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            check_deadline(deadline)
            connection.commit()
        except sqlite3.OperationalError as error:
            if getattr(error, 'sqlite_errorcode', 0) & 255 in (5, 6):
                with self.storage_lock:
                    self.busy_count += 1
            connection.rollback()
            check_deadline(deadline)
            raise ServiceError(503, "STORAGE_UNAVAILABLE", retryable=True) from None
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()
            if acquired:
                with self.storage_lock:
                    self.write_timings.append((time.monotonic() - began) * 1000)
                self.write_lock.release()

    def storage_stats(self):
        with self.storage_lock:
            return {'sqlite_busy': self.busy_count, 'write_sample_count': len(self.write_timings),
                    'max_recent_write_and_wait_ms': round(max(self.write_timings, default=0), 3)}

    @staticmethod
    def audit(connection, actor, action, resource, metadata=None):
        connection.execute("INSERT INTO team_audit(at,actor,action,resource,metadata) VALUES(?,?,?,?,?)",
                           (time.time(), actor, action, resource, encode(metadata or {})))

    def migrate_legacy(self):
        """Map existing IDs without copying/rewriting content or inventing owners."""
        with self.tx(write=True) as connection:
            rows = connection.execute("SELECT d.* FROM documents d LEFT JOIN team_pages p "
                                      "ON p.document_id=d.id WHERE p.document_id IS NULL AND d.source_key NOT LIKE 'team:%'").fetchall()
            if not rows:
                return
            connection.execute("INSERT OR IGNORE INTO team_kbs(id,title) VALUES('kb_legacy','Legacy: admin-only')")
            connection.execute("UPDATE team_kbs SET generation=generation+1,artifact=NULL WHERE id='kb_legacy'")
            for row in rows:
                book = connection.execute("SELECT id FROM team_books WHERE kb_id='kb_legacy' AND source_key=?",
                                          (row["source_key"],)).fetchone()
                book_id = book["id"] if book else uid("book")
                if not book:
                    connection.execute("INSERT INTO team_books VALUES(?,?,?,?,NULL)",
                                       (book_id, "kb_legacy", row["source_key"], row["title"]))
                revision = uid("rev")
                state = "published" if row["active"] else "archived"
                connection.execute("INSERT INTO team_revisions(id,book_id,number,status,file_hash,filename,pipeline,created,reviewed) "
                                   "VALUES(?,?,?,?,?,?,?,?,1)", (revision, book_id, row["version"], state,
                                   row["content_hash"], "legacy", "legacy-fixed-v1", time.time()))
                connection.execute("INSERT INTO team_pages VALUES(?,?,?,?,?)",
                                   (revision, 1, row["id"], row["text"], encode({"legacy_locator": True})))
                if row["active"]:
                    connection.execute("UPDATE team_books SET current_revision=? WHERE id=?", (revision, book_id))
            for wiki in connection.execute("SELECT id FROM wikis").fetchall():
                connection.execute("INSERT OR IGNORE INTO team_wikis VALUES(?,'kb_legacy','unknown')", (wiki["id"],))

    def kb_ids(self, connection, user_id, admin=False):
        sql = "SELECT id FROM team_kbs" if admin else "SELECT kb_id AS id FROM team_grants WHERE user_id=?"
        return [row["id"] for row in connection.execute(sql, () if admin else (user_id,))]

    def artifact_path(self, relative):
        path = safe_path(self.root / relative)
        if self.root not in path.parents:
            raise ServiceError(503, "INDEX_NOT_READY")
        return path

    def capacity_check(self, reserve=0):
        """Bound owned data without opening file contents or following symlinks."""
        total = 0
        for folder in (self.root, self.root / 'uploads', self.root / 'indexes'):
            for path in folder.iterdir():
                if path.name.lower().startswith('.env') or path.is_symlink() or not path.is_file():
                    continue
                total += path.stat().st_size
        if (total + reserve > self.settings.max_workspace_bytes
                or shutil.disk_usage(self.root).free < reserve + 512 * 1024 * 1024):
            raise ServiceError(503, 'STORAGE_CAPACITY_EXCEEDED', retryable=True)
