"""Cross-layer races and repeatable upgrades against real temporary databases."""

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from zhiwiki.api import create_app
from zhiwiki.config import Settings
from zhiwiki.schema import SCHEMA
from zhiwiki.store import KnowledgeStore


class TransactionBoundaryTests(unittest.TestCase):
    def test_upload_rechecks_identity_after_the_api_read_was_current(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(directory)
            original = store.ingest("目标资料", "原版和田运输资料。", "source")
            page = store.create_wiki("shipping", "运输知识", [
                {"text": "和田运输资料。", "evidence_ids": original["chunk_ids"]}])
            store.publish_wiki(page["id"])
            store.search("和田")  # Keep an actual old index during the race.
            other = KnowledgeStore(directory)
            original_get = store.get_document
            committed = []

            def read_then_competing_commit(document_id):
                observed = original_get(document_id)
                # The HTTP route sees a valid v1. Before it reaches ingest,
                # another instance commits v2 against exactly that identity.
                committed.append(other.ingest("别人已保存", "新版也门运输资料。", "source",
                                              expected_document_id=original["id"]))
                return observed

            app = create_app(Settings(data_dir=directory), store=store)
            with TestClient(app) as client, patch.object(store, "get_document", read_then_competing_commit):
                response = client.post("/api/documents/upload", params={
                    "mode": "update", "target_document_id": original["id"], "expected_version": 1},
                    files={"file": ("policy.md", "迟到页面不能覆盖".encode("utf-8"), "text/markdown")})
            self.assertEqual(response.status_code, 409, response.text)
            self.assertEqual(store.list_documents()[0]["id"], committed[0]["id"])
            self.assertEqual(len(store.list_documents(include_history=True)), 2)
            self.assertTrue(store.get_wiki(page["id"])["stale"])
            self.assertEqual(store.search("和田"), [])
            self.assertEqual(store.search("也门")[0]["document_id"], committed[0]["id"])

    def test_legacy_upgrade_and_repeated_startups_preserve_epoch_and_trigger_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "knowledge.sqlite3"
            connection = sqlite3.connect(str(database))
            try:
                connection.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS retrieval_state")[0])
                connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
                    ("doc_before_upgrade", "source", "旧库资料", 1, "old-hash", "和田资料。", 1, "2026-01-01"))
                connection.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                    ("chunk_before_upgrade", "doc_before_upgrade", 0, "和田资料。", 0, 5))
                connection.commit()
            finally:
                connection.close()

            original_store = KnowledgeStore(directory)
            self.assertEqual(original_store.search("和田")[0]["id"], "chunk_before_upgrade")
            epoch = original_store.cache_stats()["epoch"]
            for _ in range(3):
                reopened = KnowledgeStore(directory)
                self.assertEqual(reopened.search("和田")[0]["id"], "chunk_before_upgrade")
                self.assertEqual(reopened.cache_stats()["epoch"], epoch)
                with reopened._connection() as current:
                    triggers = current.execute(
                        "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'retrieval_%'").fetchall()
                    self.assertEqual(len(triggers), 12)
            self.assertTrue(original_store.cache_stats()["current"])
            latest = reopened.ingest("升级后的资料", "也门资料。", "source",
                                     expected_document_id="doc_before_upgrade")
            self.assertGreater(reopened.cache_stats()["database_epoch"], epoch)
            self.assertEqual(original_store.search("和田"), [])
            self.assertEqual(original_store.search("也门")[0]["document_id"], latest["id"])


if __name__ == "__main__":
    unittest.main()
