"""Storage regression tests runnable with Python's standard unittest runner."""

import re
import tempfile
import unittest
from unittest.mock import patch

from zhiwiki.retrieval import split_chunks, tokenize
from zhiwiki.store import KnowledgeStore


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = KnowledgeStore(self.directory.name)

    def make_document(self, text="商品签收后七天内可以退货。", key="returns"):
        return self.store.ingest("退货政策", text, key)

    def make_wiki(self, document=None, slug="return-policy", links=None):
        document = document or self.make_document()
        claims = [{"text": "签收后七天内可以退货。", "evidence_ids": document["chunk_ids"][:1]}]
        return self.store.create_wiki(slug, "退货规则", claims, links=links)

    def test_ingest_is_idempotent_and_preserves_history(self):
        original = self.make_document()
        unchanged = self.make_document()
        self.assertTrue(unchanged["unchanged"])
        self.assertEqual(original["id"], unchanged["id"])
        updated = self.make_document("商品签收后十五天内可以退货。")
        self.assertEqual(updated["version"], 2)
        self.assertEqual(len(self.store.list_documents()), 1)
        self.assertEqual(len(self.store.list_documents(include_history=True)), 2)
        old_evidence = self.store.read_evidence(original["chunk_ids"][0])
        self.assertFalse(old_evidence["active"])
        self.assertIn("七天", old_evidence["text"])
        self.assertEqual(self.store.search("十五天")[0]["document_id"], updated["id"])
        self.assertTrue(all(item["active"] for item in self.store.search("退货")))

    def test_title_change_produces_new_version(self):
        old = self.make_document()
        current = self.store.ingest("2026退货政策", "商品签收后七天内可以退货。", "returns")
        self.assertEqual(current["version"], 2)
        self.assertEqual(current["content_hash"], old["content_hash"])
        self.assertFalse(current["unchanged"])

    def test_wiki_publish_then_source_update_invalidates_atomically(self):
        original = self.make_document()
        wiki = self.make_wiki(original)
        self.assertEqual(self.store.search("退货", kind="wiki"), [])
        self.store.publish_wiki(wiki["id"])
        evidence = self.store.search("退货", kind="wiki")[0]
        self.assertEqual(evidence["source_ids"], original["chunk_ids"])
        self.assertEqual(evidence["id"], wiki["id"])
        self.make_document("签收后十五天内可退货。")
        stale = self.store.get_wiki(wiki["id"])
        self.assertTrue(stale["stale"])
        self.assertFalse(self.store.read_evidence(wiki["id"])["active"])
        self.assertEqual(self.store.search("退货", kind="wiki"), [])
        with self.assertRaises(ValueError):
            self.store.publish_wiki(wiki["id"])
        export = self.store.export_wiki(wiki["id"])
        self.assertIn("v1", export)
        self.assertIn(original["chunk_ids"][0], export)
        self.assertIn("当前有效=False", export)

    def test_draft_keeps_existing_publication_visible_until_publish(self):
        document = self.make_document()
        old_wiki = self.make_wiki(document)
        self.store.publish_wiki(old_wiki["id"])
        new_wiki = self.make_wiki(document)
        self.assertEqual(self.store.list_wiki()[0]["id"], new_wiki["id"])
        self.assertEqual(self.store.search("退货", kind="wiki")[0]["id"], old_wiki["id"])
        with self.assertRaises(ValueError):
            self.store.publish_wiki(old_wiki["id"])
        self.store.publish_wiki(new_wiki["id"])
        self.assertEqual(self.store.get_wiki(old_wiki["id"])["status"], "archived")
        self.assertFalse(self.store.read_evidence(old_wiki["id"])["active"])
        self.assertEqual(self.store.search("退货", kind="wiki")[0]["id"], new_wiki["id"])

    def test_missing_stale_and_wiki_citations_rejected(self):
        old = self.make_document()
        wiki = self.make_wiki(old)
        for ids in ([], ["chunk_missing"], [wiki["id"]]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.store.create_wiki("bad", "Bad", [{"text": "claim", "evidence_ids": ids}])
        self.make_document("签收后十五天内可退货。")
        with self.assertRaises(ValueError):
            self.make_wiki(old, slug="stale")
        self.assertEqual(len(self.store.list_wiki(include_history=True)), 1)

    def test_failed_update_rolls_back_source_and_wiki(self):
        document = self.make_document()
        wiki = self.make_wiki(document)
        self.store.publish_wiki(wiki["id"])
        with patch.object(self.store, "_insert_chunks", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                self.make_document("新规则")
        self.assertTrue(self.store.read_evidence(document["chunk_ids"][0])["active"])
        self.assertFalse(self.store.get_wiki(wiki["id"])["stale"])
        self.assertEqual(self.store.get_wiki(wiki["id"])["status"], "published")
        self.assertEqual(len(self.store.list_documents(include_history=True)), 1)

    def test_lint_checks_unpublished_and_broken_links(self):
        wiki = self.make_wiki(links=["missing-page", "return-policy"])
        issues = self.store.lint_wiki()
        codes = {item["code"] for item in issues}
        self.assertEqual(codes, {"unpublished", "broken_link", "self_link"})
        self.assertTrue(all(item["id"] == wiki["id"] for item in issues))
        self.store.publish_wiki(wiki["id"])
        self.assertNotIn("unpublished", {item["code"] for item in self.store.lint_wiki()})

    def test_run_roundtrip_and_unknown_ids(self):
        run_id = self.store.save_run({"answer": "有据可查", "events": []})
        self.assertTrue(re.fullmatch(r"run_[0-9a-f]{32}", run_id))
        record = self.store.get_run(run_id)
        self.assertEqual(record["answer"], "有据可查")
        self.assertIn("created_at", record)
        for getter in (self.store.get_run, self.store.get_document, self.store.read_evidence, self.store.get_wiki):
            with self.subTest(getter=getter), self.assertRaises(KeyError):
                getter("missing")

    def test_english_chinese_bm25_and_punctuation(self):
        chinese = self.make_document()
        english = self.store.ingest("Warranty", "Battery warranty lasts two years.", "warranty")
        self.assertEqual(self.store.search("battery")[0]["document_id"], english["id"])
        self.assertEqual(self.store.search("退货")[0]["document_id"], chinese["id"])
        self.assertEqual(self.store.search("???"), [])
        self.assertEqual(self.store.search("ZXQ_UNKNOWN_TERM"), [])
        terms = tokenize("退货政策 Battery")
        self.assertIn("退", terms)
        self.assertIn("退货", terms)
        self.assertIn("battery", terms)

    def test_chunk_locators_recover_exact_original_text(self):
        text = ("第一段：退货需要保持商品完好。\n" * 80) + "售后联系电话。"
        doc = self.make_document(text)
        result = self.store.get_document(doc["id"])
        self.assertGreater(len(result["chunks"]), 1)
        for chunk in result["chunks"]:
            start, end = map(int, chunk["locator"].split(":")[1].split("-"))
            self.assertEqual(text[start - 1:end], chunk["text"])
        chunks = split_chunks(text)
        self.assertEqual(chunks[0]["start_offset"], 0)
        self.assertEqual(chunks[-1]["end_offset"], len(text))

    def test_unknown_topics_do_not_match_generic_question_words(self):
        self.store.ingest(
            "售后说明", "退货的定义是签收后七天内申请。可以告诉客户什么材料需要提供，说明如何办理。",
            "service-help"
        )
        self.store.ingest(
            "Warranty overview", "This is the definition of a warranty. We explain what it is and how it works.",
            "warranty-overview"
        )
        queries = (
            "ORBITAL_QUASAR_917 的定义是什么？",
            "请问 DARKMATTER-XY 的含义是什么，可以解释一下吗？",
            "量子纠缠是什么？请告诉我如何理解。",
            "What is the definition of AEROGEL_NEBULA? Please explain it.",
            "这个是什么，可以告诉我吗？",
            "what is it and how does it work?",
        )
        for query in queries:
            with self.subTest(query=query):
                self.assertEqual(self.store.search(query), [])

    def test_anchor_filter_keeps_short_content_queries_and_single_characters(self):
        document = self.make_document("商品签收后七天内可以退货。维修需要原始凭证。")
        english = self.store.ingest("Battery", "Battery warranty lasts two years.", "battery")
        for query in ("退货", "维修", "凭证", "退", "请问退货需要什么？", "七天"):
            with self.subTest(query=query):
                self.assertEqual(self.store.search(query)[0]["document_id"], document["id"])
        self.assertEqual(self.store.search("What is the battery warranty?")[0]["document_id"], english["id"])

    def test_input_limits_are_enforced_without_http(self):
        for arguments in (("", "text", "key"), ("title", "", "key"), ("title", "x" * 500001, "key")):
            with self.subTest(arguments=arguments[:1]), self.assertRaises(ValueError):
                self.store.ingest(*arguments)
        for limit in (0, 21, True, "5"):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.store.search("query", limit=limit)
        with self.assertRaises(ValueError):
            self.store.search("query", kind="embedding")


if __name__ == "__main__":
    unittest.main()
