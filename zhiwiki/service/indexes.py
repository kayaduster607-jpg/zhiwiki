"""Immutable sparse index artifacts; publication is a DB pointer, never pickle."""

import json
import threading

from .common import ServiceError, check_deadline, encode
from .persistent_index import PersistentIndex, checksum


class Indexes:
    def __init__(self, database):
        self.db = database
        self.lock = threading.RLock()
        self.build_lock = threading.Lock()
        self.cache = {}

    def candidates(self, connection, kb_id, replacement=None):
        """Build one consistent candidate snapshot, optionally for a staged book."""
        sql = """SELECT c.*,d.title,b.id AS book_id,b.kb_id,r.id AS revision_id,r.filename,
                 p.page,p.quality FROM team_books b JOIN team_revisions r ON r.book_id=b.id
                 JOIN team_pages p ON p.revision_id=r.id JOIN documents d ON d.id=p.document_id
                 JOIN chunks c ON c.document_id=d.id WHERE b.kb_id=? AND """
        parameters = [kb_id]
        if replacement:
            sql += "((b.id=? AND r.id=?) OR (b.id<>? AND r.id=b.current_revision))"
            parameters.extend([replacement[0], replacement[1], replacement[0]])
        else:
            sql += "r.id=b.current_revision"
        result = []
        for row in connection.execute(sql, parameters):
            quality = json.loads(row["quality"])
            flags = [key for key, value in quality.items() if value]
            result.append({"id": row["id"], "kind": "document", "title": row["title"],
                           "text": row["text"], "knowledge_base_id": row["kb_id"],
                           "document_id": row["book_id"], "revision_id": row["revision_id"],
                           "pdf_page": row["page"], "start_offset": row["start_offset"],
                           "locator_kind": "pdf" if row["filename"].lower().endswith(".pdf") else "text",
                           "end_offset": row["end_offset"], "quality_flags": flags})
        return result

    def warm(self):
        with self.db.tx() as connection:
            libraries = [dict(row) for row in connection.execute("SELECT * FROM team_kbs")]
        for library in libraries:
            if library["artifact"]:
                path = self.db.artifact_path(library["artifact"])
                if checksum(path) != path.stem:
                    raise ServiceError(503, "INDEX_NOT_READY")
                index = PersistentIndex(path)
                index.validate()
            else:
                with self.db.tx() as connection:
                    candidates = self.candidates(connection, library["id"])
                if candidates:
                    relative, index = PersistentIndex.build(self.db, candidates)
                    with self.db.tx(write=True) as connection:
                        connection.execute("UPDATE team_kbs SET artifact=? WHERE id=? AND generation=?", (relative, library["id"], library["generation"]))
                else:
                    index = PersistentIndex()
            self.install(library["id"], library["generation"], index)

    def install(self, kb_id, generation, index):
        with self.lock:
            if kb_id in self.cache and self.cache[kb_id][0] > generation:
                return
            self.cache[kb_id] = (generation, index)

    def get(self, kb_id, generation):
        with self.lock:
            cached = self.cache.get(kb_id)
        if cached is None and generation == 0:
            cached = (0, PersistentIndex())
            self.install(kb_id, *cached)
        if cached is None or cached[0] != generation:
            raise ServiceError(503, "INDEX_NOT_READY", retryable=True)
        return cached[1]

    def prepare(self, kb_id, book_id, revision_id):
        with self.build_lock:
            self.db.capacity_check(512 * 1024 * 1024)
            with self.db.tx() as connection:
                generation = connection.execute("SELECT generation FROM team_kbs WHERE id=?", (kb_id,)).fetchone()[0]
                candidates = self.candidates(connection, kb_id, (book_id, revision_id))
            if len(candidates) > self.db.settings.max_indexed_chunks:
                raise ServiceError(422, "KNOWLEDGE_BASE_TOO_LARGE")
            relative, index = PersistentIndex.build(self.db, candidates)
            return generation, relative, index

    def search(self, snapshots, query, limit, deadline, allowed_documents=None):
        candidates = {}
        multiple = len(snapshots) > 1
        for snapshot in snapshots:
            check_deadline(deadline)
            index = self.get(snapshot["knowledge_base_id"], snapshot["generation"])
            hits = index.search(query, limit, deadline, allowed_documents)
            for rank, hit in enumerate(hits, 1):
                if multiple:
                    hit["score"] = 1 / (60 + rank)
                candidates[hit["id"]] = hit
        return sorted(candidates.values(), key=lambda item: (-item["score"], item["id"]))[:limit]
