"""Durable staged imports, fenced page checkpoints and conditional publication."""

import hashlib
import json
import time

from ..retrieval import split_chunks
from ..store import _now
from .common import ServiceError, digest, encode, uid


PIPELINE = "pdf-text-nfkc-visible-glyphs-fixed900-120-v1"


class Jobs:
    def __init__(self, database, authorization, indexes=None):
        self.db, self.auth, self.indexes = database, authorization, indexes

    def enqueue(self, context, kb_id, filename, checksum, mode, source_key, book_id=None, expected=None, key=None):
        now = time.time()
        with self.db.tx(write=True) as connection:
            actor = self.auth.require(connection, context, [kb_id], "ingest", "editor")
            request_hash = digest(encode([kb_id, filename, checksum, mode, source_key, book_id, expected]))
            if key:
                previous = self.idempotent(connection, context, "upload", key, request_hash)
                if previous:
                    return {"job_id": previous, "unchanged": True}
            if mode == "new":
                book = connection.execute("SELECT * FROM team_books WHERE kb_id=? AND source_key=?", (kb_id, source_key)).fetchone()
                if book:
                    existing = connection.execute("SELECT j.id FROM team_jobs j JOIN team_revisions r ON r.id=j.revision_id "
                                                  "WHERE r.book_id=? AND r.file_hash=? AND r.pipeline=? "
                                                  "AND j.status NOT IN ('failed','cancelled') ORDER BY j.created DESC LIMIT 1",
                                                  (book["id"], checksum, PIPELINE)).fetchone()
                    if existing:
                        if key:
                            self.bind(connection, context, "upload", key, request_hash, existing[0])
                        return {"job_id": existing[0], "unchanged": True}
                    raise ServiceError(409, "SOURCE_EXISTS")
                book_id = uid("book")
                connection.execute("INSERT INTO team_books VALUES(?,?,?,?,NULL)", (book_id, kb_id, source_key, filename))
            else:
                book = connection.execute("SELECT * FROM team_books WHERE id=? AND kb_id=?", (book_id, kb_id)).fetchone()
                if not book or not expected or book["current_revision"] != expected:
                    raise ServiceError(409, "KNOWLEDGE_CHANGED")
                existing = connection.execute("SELECT j.id FROM team_jobs j JOIN team_revisions r ON r.id=j.revision_id WHERE r.id=? AND r.file_hash=? AND r.pipeline=? AND j.status='ready'",
                                              (expected, checksum, PIPELINE)).fetchone()
                if existing:
                    if key:
                        self.bind(connection, context, 'upload', key, request_hash, existing[0])
                    return {'job_id': existing[0], 'unchanged': True}
            if connection.execute("SELECT COUNT(*) FROM team_jobs WHERE status='queued'").fetchone()[0] >= 10:
                raise ServiceError(429, "IMPORT_QUEUE_FULL", retryable=True)
            revision, job = uid("rev"), uid("job")
            number = connection.execute("SELECT COALESCE(MAX(number),0)+1 FROM team_revisions WHERE book_id=?", (book_id,)).fetchone()[0]
            connection.execute("INSERT INTO team_revisions(id,book_id,number,status,expected_revision,file_hash,filename,pipeline,created) "
                               "VALUES(?,?,?,'staging',?,?,?,?,?)",
                               (revision, book_id, number, expected, checksum, filename, PIPELINE, now))
            connection.execute("INSERT INTO team_jobs(id,owner,kb_id,revision_id,status,created,updated) VALUES(?,?,?,?,'queued',?,?)",
                               (job, actor["id"], kb_id, revision, now, now))
            self.db.audit(connection, actor["id"], "import.enqueue", revision, {"job_id": job, "kb_id": kb_id})
            if key:
                self.bind(connection, context, "upload", key, request_hash, job)
            return {"job_id": job, "revision_id": revision, "document_id": book_id, "unchanged": False}

    def get(self, context, job_id):
        with self.db.tx() as connection:
            row = connection.execute("SELECT j.*,r.quality,r.book_id FROM team_jobs j JOIN team_revisions r ON r.id=j.revision_id WHERE j.id=?",
                                     (job_id,)).fetchone()
            if not row:
                raise ServiceError(404, "RESOURCE_NOT_FOUND")
            current = self.auth.require(connection, context, [row["kb_id"]])
            if row["owner"] != current["id"]:
                self.auth.require(connection, context, [row["kb_id"]], role="editor")
            return {"job_id": row["id"], "status": row["status"], "pages_total": row["pages_total"],
                    "pages_done": row["pages_done"], "attempt": row["attempt"], "updated_at": row["updated"],
                    "document_id": row["book_id"], "revision_id": row["revision_id"],
                    "quality_summary": json.loads(row["quality"]), "error": row["error"]}

    def cancel(self, context, job_id):
        self.get(context, job_id)
        with self.db.tx(write=True) as connection:
            row = connection.execute("SELECT * FROM team_jobs WHERE id=?", (job_id,)).fetchone()
            self.auth.require(connection, context, [row["kb_id"]], "ingest", "editor")
            if row["status"] in ("ready", "failed", "cancelled"):
                return {"status": row["status"]}
            connection.execute("UPDATE team_jobs SET cancel=1,status=CASE WHEN status='parsing' THEN 'cancel_requested' ELSE 'cancelled' END,updated=? WHERE id=?",
                               (time.time(), job_id))
            if row['status'] != 'parsing':
                connection.execute("UPDATE team_revisions SET status='cancelled' WHERE id=?", (row['revision_id'],))
            self.db.audit(connection, context["id"], "import.cancel", job_id)
            return {"status": "cancel_requested" if row["status"] == "parsing" else "cancelled"}

    def claim(self):
        """At-least-once execution: expired workers lose their fencing token."""
        now = time.time()
        with self.db.tx(write=True) as connection:
            connection.execute("UPDATE team_revisions SET status='cancelled' WHERE id IN (SELECT revision_id FROM team_jobs WHERE status='cancel_requested' AND lease_until<=?)", (now,))
            connection.execute("UPDATE team_jobs SET status='cancelled',fence=fence+1,updated=? WHERE status='cancel_requested' AND lease_until<=?", (now, now))
            if connection.execute("SELECT 1 FROM team_jobs WHERE status IN ('parsing','cancel_requested') AND lease_until>? LIMIT 1", (now,)).fetchone():
                return None
            row = connection.execute("SELECT * FROM team_jobs WHERE cancel=0 AND (status='queued' OR (status='parsing' AND lease_until<?)) ORDER BY created LIMIT 1",
                                     (now,)).fetchone()
            if not row:
                return None
            connection.execute("UPDATE team_jobs SET status='parsing',attempt=attempt+1,fence=fence+1,lease_until=?,updated=? WHERE id=?",
                               (now + self.db.settings.worker_lease_seconds, now, row["id"]))
            return dict(connection.execute("SELECT * FROM team_jobs WHERE id=?", (row["id"],)).fetchone())

    def retry(self, context, job_id):
        """Resume committed pages; a new fence rejects any abandoned child."""
        with self.db.tx(write=True) as connection:
            row = connection.execute("SELECT * FROM team_jobs WHERE id=?", (job_id,)).fetchone()
            if not row:
                raise ServiceError(404, "RESOURCE_NOT_FOUND")
            self.auth.require(connection, context, [row["kb_id"]], "ingest", "editor")
            if row["status"] not in ("failed", "cancelled") or row["attempt"] >= 3:
                raise ServiceError(409, "JOB_NOT_RETRYABLE")
            if connection.execute("SELECT COUNT(*) FROM team_jobs WHERE status='queued'").fetchone()[0] >= 10:
                raise ServiceError(429, "IMPORT_QUEUE_FULL", retryable=True)
            connection.execute("UPDATE team_jobs SET status='queued',cancel=0,error=NULL,fence=fence+1,lease_until=0,updated=? WHERE id=?", (time.time(), job_id))
            connection.execute("UPDATE team_revisions SET status='staging',reviewed=0 WHERE id=?", (row["revision_id"],))
            self.db.audit(connection, context["id"], "import.retry", job_id)
        return {"job_id": job_id, "status": "queued"}

    def correct_page(self, context, book_id, revision_id, page, text, expected, note):
        """Only unpublished pages are editable; retain the extracted raw text."""
        with self.db.tx(write=True) as connection:
            revision = self.revision(connection, book_id, revision_id)
            self.auth.require(connection, context, [revision["kb_id"]], "ingest", "editor")
            if revision["status"] not in ("needs_review", "awaiting_publish"):
                raise ServiceError(409, "REVISION_NOT_EDITABLE")
            row = connection.execute("SELECT p.document_id,p.quality,d.text FROM team_pages p JOIN documents d ON d.id=p.document_id WHERE p.revision_id=? AND p.page=?", (revision_id, page)).fetchone()
            if not row:
                raise ServiceError(404, "RESOURCE_NOT_FOUND")
            if digest(row["text"]) != expected:
                raise ServiceError(412, "PRECONDITION_FAILED")
            connection.execute("UPDATE documents SET text=?,content_hash=? WHERE id=?", (text, digest(text), row["document_id"]))
            connection.execute("DELETE FROM chunks WHERE document_id=?", (row["document_id"],))
            for ordinal, chunk in enumerate(split_chunks(text)):
                connection.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)", (uid("chunk"), row["document_id"], ordinal, chunk["text"], chunk["start_offset"], chunk["end_offset"]))
            quality = json.loads(row["quality"])
            quality["manually_corrected"] = True
            connection.execute("UPDATE team_pages SET quality=? WHERE revision_id=? AND page=?", (encode(quality), revision_id, page))
            connection.execute("UPDATE team_revisions SET reviewed=0,status='needs_review' WHERE id=?", (revision_id,))
            connection.execute("INSERT INTO team_runtime VALUES(?, '1') ON CONFLICT(name) DO UPDATE SET payload=CAST(payload AS INTEGER)+1", ('revision-edit:' + revision_id,))
            connection.execute("UPDATE team_jobs SET status='needs_review',updated=? WHERE revision_id=?", (time.time(), revision_id))
            self.db.audit(connection, context["id"], "page.correct", revision_id, {"page": page, "note": note, "before_hash": expected, "after_hash": digest(text)})
        return {"text_sha256": digest(text), "status": "needs_review"}

    def fenced(self, connection, job_id, fence):
        row = connection.execute("SELECT j.*,r.book_id,r.number,r.filename,r.file_hash FROM team_jobs j "
                                 "JOIN team_revisions r ON r.id=j.revision_id WHERE j.id=?", (job_id,)).fetchone()
        if not row or row["fence"] != fence or row["lease_until"] <= time.time() or row['status'] != 'parsing':
            raise ServiceError(409, "WORKER_LEASE_LOST")
        if row["cancel"]:
            raise ServiceError(409, "JOB_CANCELLED")
        return row

    def page(self, job_id, fence, page, raw, text, quality, total):
        self.db.capacity_check((len(raw.encode('utf-8')) + len(text.encode('utf-8'))) * 3)
        with self.db.tx(write=True) as connection:
            job = self.fenced(connection, job_id, fence)
            present = connection.execute("SELECT 1 FROM team_pages WHERE revision_id=? AND page=?", (job["revision_id"], page)).fetchone()
            if not present:
                document = uid("doc")
                connection.execute("INSERT INTO documents VALUES(?,?,?,?,?,?,0,?)",
                                   (document, "team:{}:page:{}".format(job["book_id"], page),
                                    job["filename"], job["number"], hashlib.sha256(text.encode()).hexdigest(), text, _now()))
                for ordinal, chunk in enumerate(split_chunks(text)):
                    connection.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)", (uid("chunk"), document, ordinal,
                                       chunk["text"], chunk["start_offset"], chunk["end_offset"]))
                connection.execute("INSERT INTO team_pages VALUES(?,?,?,?,?)", (job["revision_id"], page, document, raw, encode(quality)))
            connection.execute("UPDATE team_jobs SET pages_done=MAX(pages_done,?),pages_total=?,lease_until=?,updated=? WHERE id=? AND fence=?",
                               (page, total, time.time() + self.db.settings.worker_lease_seconds, time.time(), job_id, fence))

    def finish(self, job_id, fence, error=None):
        with self.db.tx(write=True) as connection:
            row = connection.execute("SELECT * FROM team_jobs WHERE id=? AND fence=?", (job_id, fence)).fetchone()
            if not row or row["status"] not in ("parsing", "cancel_requested") or row["lease_until"] <= time.time():
                return
            qualities = [json.loads(item[0]) for item in connection.execute("SELECT quality FROM team_pages WHERE revision_id=?", (row["revision_id"],))]
            summary = {"pages": len(qualities), "flagged_pages": sum(bool(q) for q in qualities),
                       "empty_pages": sum(bool(q.get("empty")) for q in qualities),
                       "missing_glyph_pages": sum(bool(q.get("missing_glyphs")) for q in qualities)}
            state = "cancelled" if row["cancel"] else "failed" if error else "needs_review" if summary["flagged_pages"] else "awaiting_publish"
            connection.execute("UPDATE team_jobs SET status=?,error=?,updated=? WHERE id=? AND fence=?", (state, error, time.time(), job_id, fence))
            connection.execute("UPDATE team_revisions SET status=?,quality=? WHERE id=?", (state, encode(summary), row["revision_id"]))

    def review(self, context, book_id, revision_id, note):
        with self.db.tx(write=True) as connection:
            row = self.revision(connection, book_id, revision_id)
            self.auth.require(connection, context, [row["kb_id"]], "publish", "publisher")
            if row["status"] not in ("needs_review", "awaiting_publish"):
                raise ServiceError(409, "REVISION_NOT_READY")
            if not connection.execute("SELECT 1 FROM chunks c JOIN team_pages p ON c.document_id=p.document_id WHERE p.revision_id=? LIMIT 1", (revision_id,)).fetchone():
                raise ServiceError(422, "PDF_NEEDS_OCR")
            connection.execute("UPDATE team_revisions SET reviewed=1,status='awaiting_publish' WHERE id=?", (revision_id,))
            connection.execute("UPDATE team_jobs SET status='awaiting_publish',updated=? WHERE revision_id=?", (time.time(), revision_id))
            self.db.audit(connection, context["id"], "revision.review", revision_id, {"note": note})
        return {"status": "awaiting_publish"}

    @staticmethod
    def revision(connection, book_id, revision_id):
        row = connection.execute("SELECT r.*,b.kb_id,b.current_revision FROM team_revisions r JOIN team_books b ON b.id=r.book_id WHERE r.id=? AND b.id=?",
                                 (revision_id, book_id)).fetchone()
        if not row:
            raise ServiceError(404, "RESOURCE_NOT_FOUND")
        return row

    def publish(self, context, book_id, revision_id, expected, new=False):
        with self.db.tx() as connection:
            row = self.revision(connection, book_id, revision_id)
            self.auth.require(connection, context, [row["kb_id"]], "publish", "publisher")
            kb_id = row["kb_id"]
            edit_epoch = self.edit_epoch(connection, revision_id)
            if row["status"] == "published" and row["current_revision"] == revision_id:
                return {"status": "ready", "revision_id": revision_id, "unchanged": True}
            if row["status"] != "awaiting_publish" or not row["reviewed"]:
                raise ServiceError(409, "REVIEW_REQUIRED")
        generation, artifact, index = self.indexes.prepare(kb_id, book_id, revision_id)
        with self.db.tx(write=True) as connection:
            row = self.revision(connection, book_id, revision_id)
            self.auth.require(connection, context, [kb_id], "publish", "publisher")
            if row["status"] != "awaiting_publish" or not row["reviewed"]:
                raise ServiceError(409, "REVIEW_REQUIRED")
            if self.edit_epoch(connection, revision_id) != edit_epoch:
                raise ServiceError(409, "KNOWLEDGE_CHANGED")
            if (new and row["current_revision"] is not None) or (not new and expected != row["current_revision"]):
                raise ServiceError(412, "PRECONDITION_FAILED")
            actual = connection.execute("SELECT generation FROM team_kbs WHERE id=?", (kb_id,)).fetchone()[0]
            if actual != generation:
                raise ServiceError(409, "KNOWLEDGE_CHANGED")
            if row["current_revision"]:
                for old in connection.execute("SELECT document_id FROM team_pages WHERE revision_id=?", (row["current_revision"],)).fetchall():
                    self.db.core._invalidate_source(connection, old[0])
                connection.execute("UPDATE team_revisions SET status='archived' WHERE id=?", (row["current_revision"],))
            connection.execute("UPDATE documents SET active=1 WHERE id IN (SELECT document_id FROM team_pages WHERE revision_id=?)", (revision_id,))
            connection.execute("UPDATE team_books SET current_revision=? WHERE id=?", (revision_id, book_id))
            connection.execute("UPDATE team_revisions SET status='published' WHERE id=?", (revision_id,))
            connection.execute("UPDATE team_kbs SET generation=generation+1,artifact=? WHERE id=?", (artifact, kb_id))
            connection.execute("UPDATE team_jobs SET status='publishing',updated=? WHERE revision_id=?", (time.time(), revision_id))
            self.db.audit(connection, context["id"], "revision.publish", revision_id, {"generation": generation + 1})
        self.indexes.install(kb_id, generation + 1, index)
        with self.db.tx(write=True) as connection:
            connection.execute("UPDATE team_jobs SET status='ready',updated=? WHERE revision_id=?", (time.time(), revision_id))
        return {"status": "ready", "revision_id": revision_id, "generation": generation + 1}

    @staticmethod
    def edit_epoch(connection, revision_id):
        row = connection.execute("SELECT payload FROM team_runtime WHERE name=?", ('revision-edit:' + revision_id,)).fetchone()
        return int(row[0]) if row else 0

    @staticmethod
    def idempotent(connection, context, operation, key, request_hash):
        row = connection.execute("SELECT * FROM team_idempotency WHERE owner=? AND operation=? AND key=?",
                                 (context["id"], operation, key)).fetchone()
        if row and row["expires"] > time.time():
            if row["request_hash"] != request_hash:
                raise ServiceError(409, "IDEMPOTENCY_CONFLICT")
            if row["status"] == "running":
                raise ServiceError(409, "REQUEST_IN_PROGRESS", retryable=True)
            return row["result_id"]
        return None

    @staticmethod
    def bind(connection, context, operation, key, request_hash, result_id, state="done"):
        connection.execute("INSERT OR REPLACE INTO team_idempotency VALUES(?,?,?,?,?,?,?)",
                           (context["id"], operation, key, request_hash, result_id, state, time.time() + 86400))

    def publish_idempotent(self, context, book_id, revision_id, expected, new, key):
        request_hash = digest(encode([book_id, revision_id, expected, new]))
        with self.db.tx(write=True) as connection:
            row = self.revision(connection, book_id, revision_id)
            self.auth.require(connection, context, [row["kb_id"]], "publish", "publisher")
            # Recover a crash after the atomic pointer switch but before the
            # separate HTTP bookkeeping completed. The committed revision wins.
            running = connection.execute("SELECT * FROM team_idempotency WHERE owner=? AND operation='publish' AND key=?",
                                         (context["id"], key)).fetchone()
            if running and running["request_hash"] == request_hash and row["current_revision"] == revision_id:
                connection.execute("UPDATE team_idempotency SET status='done' WHERE owner=? AND operation='publish' AND key=?", (context["id"], key))
            previous = self.idempotent(connection, context, "publish", key, request_hash)
            if previous:
                if row["current_revision"] != previous:
                    raise ServiceError(409, "KNOWLEDGE_CHANGED")
                return {"status": "ready", "revision_id": previous, "unchanged": True}
            self.bind(connection, context, "publish", key, request_hash, revision_id, "running")
        try:
            result = self.publish(context, book_id, revision_id, expected, new)
            with self.db.tx(write=True) as connection:
                connection.execute("UPDATE team_idempotency SET status='done' WHERE owner=? AND operation='publish' AND key=?",
                                   (context["id"], key))
            return result
        except BaseException:
            with self.db.tx(write=True) as connection:
                connection.execute("DELETE FROM team_idempotency WHERE owner=? AND operation='publish' AND key=? AND status='running'",
                                   (context["id"], key))
            raise
