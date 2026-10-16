"""Scoped retrieval receipts, idempotent replay and atomic citation validation."""

import json
import time
from datetime import datetime, timezone

from .common import ServiceError, check_deadline, digest, encode, uid


def timestamp(seconds=None):
    return datetime.fromtimestamp(seconds or time.time(), timezone.utc).isoformat()


class Retrievals:
    def __init__(self, database, authorization, indexes):
        self.db, self.auth, self.indexes = database, authorization, indexes

    def snapshots(self, connection, kb_ids):
        return [{"knowledge_base_id": kb_id, "generation": connection.execute(
            "SELECT generation FROM team_kbs WHERE id=?", (kb_id,)).fetchone()[0]} for kb_id in kb_ids]

    def claim(self, context, operation, key, payload, deadline):
        request_hash = digest(encode(payload))
        with self.db.tx(write=True, deadline=deadline) as connection:
            current = self.auth.require(connection, context, scope=operation)
            row = connection.execute("SELECT * FROM team_idempotency WHERE owner=? AND operation=? AND key=?",
                                     (current["id"], operation, key)).fetchone()
            if row and row["expires"] > time.time():
                if row["request_hash"] != request_hash:
                    raise ServiceError(409, "IDEMPOTENCY_CONFLICT")
                if row["status"] == "running":
                    error = ServiceError(409, "REQUEST_IN_PROGRESS", retryable=True)
                    error.location = row["result_id"]
                    raise error
                return row["result_id"], True
            receipt_id = uid("ret")
            connection.execute("INSERT OR REPLACE INTO team_idempotency VALUES(?,?,?,?,?,'running',?)",
                               (current["id"], operation, key, request_hash, receipt_id,
                                time.time() + self.db.settings.receipt_seconds))
        return receipt_id, False

    def release_claim(self, context, operation, key):
        with self.db.tx(write=True) as connection:
            connection.execute("DELETE FROM team_idempotency WHERE owner=? AND operation=? AND key=? AND status='running'",
                               (context["id"], operation, key))

    def receipt(self, connection, context, receipt_id, scope="read"):
        row = connection.execute("SELECT * FROM team_receipts WHERE id=?", (receipt_id,)).fetchone()
        if not row or row["owner"] != context["id"]:
            raise ServiceError(403, "EVIDENCE_NOT_OBTAINED")
        if row["expires"] <= time.time():
            raise ServiceError(410, "RECEIPT_EXPIRED")
        kb_ids = json.loads(row["kb_ids"])
        self.auth.require(connection, context, kb_ids, scope)
        snapshots = json.loads(row["snapshots"])
        if snapshots != self.snapshots(connection, kb_ids):
            raise ServiceError(409, "KNOWLEDGE_CHANGED")
        return json.loads(row["payload"])

    @staticmethod
    def view(item, remaining):
        text = item["text"][:remaining]
        return {"evidence_id": item["id"], "knowledge_base_id": item["knowledge_base_id"],
                "document_id": item["document_id"], "revision_id": item["revision_id"],
                "title": item["title"], "text": text, "score": item.get("score", 0),
                "locator": {"kind": item.get("locator_kind", "pdf"),
                            "pdf_page_start": item["pdf_page"] if item.get("locator_kind", "pdf") == "pdf" else None,
                            "pdf_page_end": item["pdf_page"] if item.get("locator_kind", "pdf") == "pdf" else None, "printed_page": None, "chapter_path": [],
                            "text_revision": item["revision_id"], "start_offset": item["start_offset"],
                            "end_offset": item["start_offset"] + len(text)},
                "text_sha256": digest(text), "quality_flags": item["quality_flags"], "untrusted_content": True}

    def evidence_current(self, connection, evidence):
        row = connection.execute("""SELECT c.text,c.start_offset,p.revision_id,b.current_revision,d.active,b.kb_id
                FROM chunks c JOIN documents d ON d.id=c.document_id JOIN team_pages p ON p.document_id=d.id
                JOIN team_revisions r ON r.id=p.revision_id JOIN team_books b ON b.id=r.book_id WHERE c.id=?""",
                (evidence["evidence_id"],)).fetchone()
        if not row or not row["active"] or row["revision_id"] != row["current_revision"]:
            raise ServiceError(409, "KNOWLEDGE_CHANGED")
        text = evidence["text"]
        if (row["revision_id"] != evidence["revision_id"] or row["kb_id"] != evidence["knowledge_base_id"]
                or not row["text"].startswith(text) or digest(text) != evidence["text_sha256"]):
            raise ServiceError(409, "HASH_MISMATCH")

    def persist(self, context, receipt_id, key, operation, payload, snapshots, items, budget, deadline, started, warnings=()):
        views, remaining = [], budget
        for item in items:
            if remaining <= 0:
                break
            view = self.view(item, remaining)
            views.append(view)
            remaining -= len(view["text"])
        result = {"schema_version": "1.0", "retrieval_id": receipt_id,
                  "receipt_expires_at": timestamp(time.time() + self.db.settings.receipt_seconds),
                  "status": "ok" if views else "empty", "checked_at": timestamp(),
                  "knowledge_snapshots": snapshots, "evidence": views, "returned_chars": budget - remaining,
                  "truncated": sum(len(item["text"]) for item in items) > budget,
                  "retrieval": {"strategy": "sparse-bm25" if len(snapshots) == 1 else "rrf-sparse-bm25",
                                "version": "sparse-bm25-v1", "score_type": "bm25" if len(snapshots) == 1 else "rrf"},
                  "warnings": sorted({flag for view in views for flag in view["quality_flags"]} | set(warnings)),
                  "usage": {"model_calls": 0, "tool_calls": 1},
                  "elapsed_ms": round((time.monotonic() - started) * 1000, 3)}
        kb_ids = [snapshot["knowledge_base_id"] for snapshot in snapshots]
        # This single write transaction is the final authority for source and
        # permission checks plus receipt publication; no network I/O occurs here.
        with self.db.tx(write=True, deadline=deadline) as connection:
            self.auth.require(connection, context, kb_ids, operation)
            if snapshots != self.snapshots(connection, kb_ids):
                raise ServiceError(409, "KNOWLEDGE_CHANGED")
            for evidence in views:
                self.evidence_current(connection, evidence)
            connection.execute("INSERT INTO team_receipts VALUES(?,?,?,?,?,?,?)",
                               (receipt_id, context["id"], context["token_hash"], encode(kb_ids), encode(snapshots),
                                encode(result), time.time() + self.db.settings.receipt_seconds))
            connection.execute("UPDATE team_idempotency SET status='done' WHERE owner=? AND operation=? AND key=?",
                               (context["id"], operation, key))
        return result

    def retrieve(self, context, payload, key, deadline):
        started = time.monotonic()
        receipt_id, replay = self.claim(context, "retrieve", key, payload, deadline)
        if replay:
            with self.db.tx(deadline=deadline) as connection:
                return self.receipt(connection, context, receipt_id, scope="retrieve")
        try:
            kb_ids = sorted(payload["knowledge_base_ids"])
            with self.db.tx(deadline=deadline) as connection:
                self.auth.require(connection, context, kb_ids, "retrieve")
                snapshots = self.snapshots(connection, kb_ids)
                filters = payload.get("filters", {})
                allowed = self.filtered_books(connection, kb_ids, filters)
            items = self.indexes.search(snapshots, payload["query"], payload["top_k"], deadline, allowed)
            return self.persist(context, receipt_id, key, "retrieve", payload, snapshots,
                                items[:payload["top_k"]], payload["context_char_budget"], deadline, started,
                                ["unknown_metadata_excluded"] if filters.get("language") or filters.get("effective_at") else [])
        except BaseException:
            self.release_claim(context, "retrieve", key)
            raise

    def read(self, context, payload, key, deadline):
        started = time.monotonic()
        receipt_id, replay = self.claim(context, "read", key, payload, deadline)
        try:
            with self.db.tx(deadline=deadline) as connection:
                if replay:
                    return self.receipt(connection, context, receipt_id)
                obtained, snapshots = {}, {}
                for previous in payload["retrieval_ids"]:
                    record = self.receipt(connection, context, previous)
                    obtained.update({item["evidence_id"]: item for item in record["evidence"]})
                    snapshots.update({item["knowledge_base_id"]: item for item in record["knowledge_snapshots"]})
                selected = []
                for evidence_id in payload["evidence_ids"]:
                    if evidence_id not in obtained:
                        raise ServiceError(403, "EVIDENCE_NOT_OBTAINED")
                    source = obtained[evidence_id]
                    self.evidence_current(connection, source)
                    chunk = connection.execute("SELECT text,start_offset,end_offset FROM chunks WHERE id=?", (evidence_id,)).fetchone()
                    selected.append({"id": evidence_id, "kind": "document", "text": chunk["text"],
                                     "start_offset": chunk["start_offset"], "end_offset": chunk["end_offset"],
                                     "pdf_page": source["locator"]["pdf_page_start"],
                                     "locator_kind": source["locator"]["kind"],
                                     **{name: source[name] for name in ("title", "knowledge_base_id", "document_id", "revision_id", "quality_flags")}})
            return self.persist(context, receipt_id, key, "read", payload, sorted(snapshots.values(), key=lambda x: x["knowledge_base_id"]),
                                selected, payload["context_char_budget"], deadline, started)
        except BaseException:
            if not replay:
                self.release_claim(context, "read", key)
            raise

    def validate(self, context, payload, deadline):
        with self.db.tx(write=True, deadline=deadline) as connection:
            self.auth.require(connection, context, scope="validate")
            views, snapshots = {}, {}
            for receipt_id in payload["retrieval_ids"]:
                record = self.receipt(connection, context, receipt_id, scope="validate")
                views.update({(item["evidence_id"], item["text_sha256"]): item for item in record["evidence"]})
                snapshots.update({item["knowledge_base_id"]: item for item in record["knowledge_snapshots"]})
            for citation in payload["citations"]:
                view = views.get((citation["evidence_id"], citation["text_sha256"]))
                if view is None:
                    raise ServiceError(403, "EVIDENCE_NOT_OBTAINED")
                self.evidence_current(connection, view)
            return {"valid": True, "checked_at": timestamp(), "knowledge_snapshots": list(snapshots.values()),
                    "semantic_support": "not_checked"}

    def filtered_books(self, connection, kb_ids, filters):
        document_ids = set(filters.get("document_ids", []))
        for book_id in document_ids:
            row = connection.execute("SELECT kb_id FROM team_books WHERE id=?", (book_id,)).fetchone()
            if not row or row[0] not in kb_ids:
                raise ServiceError(403, "PERMISSION_DENIED")
        if not any(filters.values()):
            return None
        result = set()
        for row in connection.execute("SELECT b.id,b.kb_id,m.payload FROM team_books b LEFT JOIN team_metadata m ON m.book_id=b.id"):
            if row["kb_id"] not in kb_ids or (document_ids and row["id"] not in document_ids):
                continue
            metadata = json.loads(row["payload"] or "{}")
            if filters.get("language") and metadata.get("language") != filters["language"]:
                continue
            if not set(filters.get("tags", [])).issubset(metadata.get("tags", [])):
                continue
            at = filters.get("effective_at")
            if at and (not metadata.get("effective_from") or at < metadata["effective_from"]
                       or (metadata.get("effective_to") and at > metadata["effective_to"])):
                continue
            result.add(row["id"])
        return result
