"""Retrieval invariants across real SQLite connections, commits, and snapshots."""

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from unittest.mock import patch
from pathlib import Path

from zhiwiki.retrieval import BM25Index, bm25_search
from zhiwiki.schema import SCHEMA
from zhiwiki.store import KnowledgeStore


class TracedStore(KnowledgeStore):
    def __init__(self, data_dir):
        self.statements = []
        super().__init__(data_dir)

    @contextmanager
    def _connection(self, write=False):
        with super()._connection(write=write) as connection:
            connection.set_trace_callback(self.statements.append)
            yield connection

    def select_statements(self):
        return [sql for sql in self.statements if sql.lstrip().upper().startswith("SELECT")]


class RetrievalCacheTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = TracedStore(self.directory.name)

    def document(self, title="退货指南", text="退货需提供订单凭证。", key="returns"):
        return self.store.ingest(title, text, key)

    def wiki(self, document, slug="return-guide", text="退货需订单凭证。"):
        return self.store.create_wiki(slug, "退货页面", [
            {"text": text, "evidence_ids": document["chunk_ids"][:1]}])

    def test_cold_load_is_four_selects_and_warm_load_needs_only_epoch(self):
        document = self.document(text="退货凭证。" * 1000)
        self.assertGreater(len(document["chunk_ids"]), 5)
        self.store.statements.clear()
        first = self.store.search("退货")
        self.assertEqual(len(self.store.select_statements()), 4)
        self.assertFalse(any("SELECT * FROM documents" in sql for sql in self.store.select_statements()))
        self.store.statements.clear()
        with patch("zhiwiki.search_index.BM25Index", side_effect=AssertionError("warm search rebuilt corpus")):
            second = self.store.search("退货")
        self.assertEqual(first, second)
        self.assertEqual(len(self.store.select_statements()), 1)
        self.assertIn("retrieval_state", self.store.select_statements()[0])
        self.assertEqual(self.store.cache_stats()["builds"], 1)

    def test_document_list_and_read_do_not_load_metadata_per_chunk(self):
        document = self.document(text="退货凭证。" * 1000)
        self.document("质检报告", "质检说明。", "inspection")
        self.store.statements.clear()
        self.assertEqual(len(self.store.list_documents()), 2)
        self.assertEqual(len(self.store.select_statements()), 2)
        self.assertTrue(all("text" not in sql.lower() for sql in self.store.select_statements()))
        self.store.statements.clear()
        result = self.store.get_document(document["id"])
        self.assertEqual(len(result["chunks"]), len(document["chunk_ids"]))
        self.assertEqual(len(self.store.select_statements()), 2)
        self.store.statements.clear()
        self.assertTrue(self.store.read_evidence(document["chunk_ids"][0])["active"])
        self.assertEqual(len(self.store.select_statements()), 1)

    def test_a_second_store_update_invalidates_a_warm_index(self):
        old = self.document(text="和田地理旧版资料。")
        self.assertEqual(self.store.search("和田")[0]["document_id"], old["id"])
        other = KnowledgeStore(self.directory.name)
        new = other.ingest("新版", "也门地理新版资料。", "returns")
        self.assertFalse(self.store.cache_stats()["current"])
        self.assertEqual(self.store.search("和田"), [])
        self.assertEqual(self.store.search("也门")[0]["document_id"], new["id"])
        self.assertEqual(self.store.cache_stats()["builds"], 2)
        self.assertFalse(self.store.read_evidence(old["chunk_ids"][0])["active"])

    def test_publish_draft_and_source_invalidation_across_instances(self):
        document = self.document()
        first = self.wiki(document)
        other = KnowledgeStore(self.directory.name)
        self.assertEqual(self.store.search("退货", kind="wiki"), [])
        other.publish_wiki(first["id"])
        self.assertEqual(self.store.search("退货", kind="wiki")[0]["id"], first["id"])
        second = other.create_wiki("return-guide", "退货新版", [
            {"text": "退货保留凭证。", "evidence_ids": document["chunk_ids"][:1]}])
        self.assertEqual(self.store.search("退货", kind="wiki")[0]["id"], first["id"])
        other.publish_wiki(second["id"])
        self.assertEqual([item["id"] for item in self.store.search("退货", kind="wiki")], [second["id"]])
        other.ingest("更新来源", "订单改为电子归档。", "returns")
        self.assertEqual(self.store.search("退货", kind="wiki"), [])

    def test_recording_runs_does_not_invalidate_a_warm_index(self):
        self.document()
        self.store.search("退货")
        before = self.store.cache_stats()
        KnowledgeStore(self.directory.name).save_run({"id": "run_cache_test", "answer": "摘录"})
        self.store.search("退货")
        after = self.store.cache_stats()
        self.assertEqual(before["epoch"], after["epoch"])
        self.assertEqual(after["builds"], 1)
        self.assertEqual(after["hits"], 1)

    def test_rolled_back_source_and_epoch_changes_remain_invisible(self):
        document = self.document()
        expected = self.store.search("退货")
        epoch = self.store.cache_stats()["epoch"]
        with self.assertRaisesRegex(RuntimeError, "rollback"):
            with self.store._connection(write=True) as connection:
                connection.execute("UPDATE documents SET active=0 WHERE id=?", (document["id"],))
                raise RuntimeError("rollback")
        self.assertEqual(self.store.search("退货"), expected)
        self.assertEqual(self.store.cache_stats()["epoch"], epoch)
        self.assertEqual(self.store.cache_stats()["builds"], 1)

    def test_epoch_and_candidates_share_a_snapshot_when_a_writer_commits_between_them(self):
        old = self.document(text="和田原始资料。")
        other = KnowledgeStore(self.directory.name)
        load = self.store._search_candidates
        newer = []

        def write_between_epoch_and_candidates(connection):
            newer.append(other.ingest("新版", "也门新资料。", "returns"))
            return load(connection)

        with patch.object(self.store, "_search_candidates", write_between_epoch_and_candidates):
            result = self.store.search("和田")
        self.assertEqual(result[0]["document_id"], old["id"])
        self.assertFalse(self.store.cache_stats()["current"])
        self.assertEqual(self.store.search("和田"), [])
        self.assertEqual(self.store.search("也门")[0]["document_id"], newer[0]["id"])

    def test_simultaneous_searches_reuse_one_cold_build(self):
        self.document(text="退货凭证。" * 200)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.store.search("退货"), range(12)))
        self.assertTrue(all(result == results[0] for result in results))
        self.assertEqual(self.store.cache_stats()["builds"], 1)
        self.assertEqual(self.store.cache_stats()["hits"], 11)

    def test_mutating_a_returned_wiki_does_not_poison_the_cache(self):
        document = self.document()
        page = self.wiki(document)
        self.store.publish_wiki(page["id"])
        result = self.store.search("退货", kind="wiki")[0]
        result["source_ids"].clear()
        result["text"] = "伪造文本"
        again = self.store.search("退货", kind="wiki")[0]
        self.assertEqual(again["source_ids"], document["chunk_ids"][:1])
        self.assertNotEqual(again["text"], "伪造文本")

    def test_kind_statistics_match_an_isolated_corpus(self):
        document = self.document()
        self.document("退货与换货", "退货换货均保留凭证。", "exchange")
        page = self.wiki(document)
        self.store.publish_wiki(page["id"])
        with self.store._connection() as connection:
            corpus = self.store._search_candidates(connection)
        index = BM25Index(corpus)
        for kind in ("document", "wiki", "all"):
            subset = [item for item in corpus if kind == "all" or item["kind"] == kind]
            self.assertEqual(index.search("退货凭证", kind=kind), bm25_search("退货凭证", subset))

    def test_source_relation_delete_and_restore_invalidate_published_candidates(self):
        document = self.document()
        page = self.wiki(document)
        self.store.publish_wiki(page["id"])
        self.assertTrue(self.store.search("退货", kind="wiki"))
        other = KnowledgeStore(self.directory.name)
        with other._connection(write=True) as connection:
            connection.execute("DELETE FROM wiki_sources WHERE wiki_id=?", (page["id"],))
        self.assertEqual(self.store.search("退货", kind="wiki"), [])
        with other._connection(write=True) as connection:
            connection.execute("INSERT INTO wiki_sources VALUES (?,?)", (page["id"], document["chunk_ids"][0]))
        self.assertEqual(self.store.search("退货", kind="wiki")[0]["id"], page["id"])

    def test_epoch_migration_keeps_legacy_document_wiki_and_run_records(self):
        legacy_dir = Path(self.directory.name) / "legacy"
        legacy_dir.mkdir()
        connection = sqlite3.connect(str(legacy_dir / "knowledge.sqlite3"))
        try:
            # The prefix is the unchanged v2 schema, without any epoch table.
            connection.executescript(SCHEMA.split("CREATE TABLE IF NOT EXISTS retrieval_state")[0])
            connection.execute("INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
                               ("doc_legacy", "legacy", "退货旧档", 1, "old-hash", "退货政策。", 1, "2026-01-01"))
            connection.execute("INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                               ("chunk_legacy", "doc_legacy", 0, "退货政策。", 0, 5))
            claims = [{"text": "退货政策。", "evidence_ids": ["chunk_legacy"]}]
            connection.execute("INSERT INTO wikis VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                               ("wiki_legacy", "legacy", "退货知识", 1, "published", 0,
                                json.dumps(claims), "[]", "退货政策。", "offline", "2026-01-01"))
            connection.execute("INSERT INTO wiki_sources VALUES (?,?)", ("wiki_legacy", "chunk_legacy"))
            connection.execute("INSERT INTO runs VALUES (?,?,?)", ("run_legacy", '{"answer":"saved"}', "2026-01-01"))
            connection.commit()
        finally:
            connection.close()
        migrated = KnowledgeStore(legacy_dir)
        self.assertEqual(migrated.get_document("doc_legacy")["chunk_ids"], ["chunk_legacy"])
        self.assertEqual(migrated.get_run("run_legacy")["answer"], "saved")
        self.assertEqual(migrated.search("退货", kind="wiki")[0]["id"], "wiki_legacy")
        KnowledgeStore(legacy_dir).ingest("新版", "电子凭证。", "legacy")
        self.assertEqual(migrated.search("退货", kind="wiki"), [])
        self.assertEqual(migrated.get_wiki("wiki_legacy")["claims"], claims)


class ChineseContentTermsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = KnowledgeStore(self.directory.name)

    def test_compounds_with_function_characters_are_not_deleted(self):
        # Different grammatical positions and unrelated words rule out a
        # four-keyword allowlist as a viable implementation of this behavior.
        words = ("和田", "也门", "与门", "异或", "和声", "共和", "或然", "也许", "它山", "而立", "目的", "求是", "实事求是")
        for index, word in enumerate(words):
            self.store.ingest("词项记录", word + "包含独立的条目资料。", str(index))
        for word in words:
            for query in (word, "请问" + word + "是什么？", "告诉我关于" + word + "的含义。"):
                with self.subTest(query=query):
                    results = self.store.search(query)
                    self.assertTrue(results)
                    self.assertIn(word, results[0]["text"])

    def test_shared_single_characters_and_function_words_are_not_anchors(self):
        self.store.ingest("资料", "田野观察，门锁更换，异常记录。我们可以告诉客户定义是什么，如何办理。", "unrelated")
        for query in ("和田", "也门是什么", "与门的含义", "异或", "这个是什么，可以告诉我吗？", "是", "和"):
            with self.subTest(query=query):
                self.assertEqual(self.store.search(query), [])

    def test_trailing_question_particles_do_not_form_false_compounds(self):
        self.store.ingest("其他资料", "田是姓氏，门是建筑入口，论是观点。", "other")
        for query in ("和田是什么", "也门是什么", "未知理论是什么"):
            with self.subTest(query=query):
                self.assertEqual(self.store.search(query), [])


if __name__ == "__main__":
    unittest.main()
