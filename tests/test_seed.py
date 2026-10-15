"""固定样例清单、人工来源与重复载入边界，兼容标准库 unittest。"""

import json
import re
import tempfile
import unittest
from unittest.mock import patch

from zhiwiki import bootstrap
from zhiwiki.store import KnowledgeStore


class SeedTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = KnowledgeStore(self.directory.name)

    def test_manifest_loads_sixteen_documents_and_preserves_old_keys(self):
        documents = bootstrap.seed_examples(self.store)
        self.assertEqual(len(documents), 16)
        by_key = {item["source_key"]: item for item in documents}
        self.assertTrue({"example:refund-2025", "example:refund-2026", "example:shipping", "example:warranty"} <= set(by_key))
        self.assertEqual(self.store.list_wiki(), [])
        old = self.store.get_document(by_key["example:refund-2025"]["id"])
        current = self.store.get_document(by_key["example:refund-2026"]["id"])
        self.assertIn("签收后 7 个自然日内申请换货", old["text"])
        self.assertIn("签收后 15 个自然日内申请换货", current["text"])
        self.assertEqual((old["version"], current["version"]), (1, 1))
        self.assertTrue(old["active"] and current["active"])

    def test_long_documents_have_real_multiple_traceable_chunks(self):
        documents = bootstrap.seed_examples(self.store)
        long_documents = []
        for item in documents:
            document = self.store.get_document(item["id"])
            chinese_characters = len(re.findall(r"[\u3400-\u9fff]", document["text"]))
            if 2000 <= chinese_characters <= 5000:
                long_documents.append(document)
                self.assertGreaterEqual(len(document["chunks"]), 3)
                self.assertIn("\n## ", document["text"])
                for chunk in document["chunks"]:
                    start, end = map(int, chunk["locator"].split(":")[1].split("-"))
                    self.assertEqual(document["text"][start - 1:end], chunk["text"])
        self.assertEqual(len(long_documents), 4)

    def test_readme_and_definitions_never_enter_knowledge_documents(self):
        with patch.object(bootstrap.Path, "glob", side_effect=AssertionError("must not scan directories")):
            documents = bootstrap.seed_examples(self.store)
        self.assertEqual(len(documents), 16)
        self.assertFalse(any("readme" in item["source_key"].lower() for item in documents))
        self.assertFalse(any("evaluation" in item["source_key"] for item in documents))

    def test_workspace_publishes_four_source_backed_human_samples(self):
        result = bootstrap.seed_workspace(self.store)
        self.assertTrue(result["fictional"])
        self.assertEqual((len(result["documents"]), len(result["pages"]), len(result["created_pages"])), (16, 4, 4))
        self.assertEqual(result["skipped_pages"], [])
        for page in result["pages"]:
            self.assertEqual(page["mode"], "sample")
            self.assertEqual(page["status"], "published")
            self.assertTrue(page["slug"].startswith("sample-"))
            self.assertIn("人工整理示例", page["title"])
            for claim in page["claims"]:
                self.assertTrue(claim["evidence_ids"])
                for evidence_id in claim["evidence_ids"]:
                    evidence = self.store.read_evidence(evidence_id)
                    self.assertEqual(evidence["kind"], "document")
                    self.assertTrue(evidence["active"])
        self.assertEqual(self.store.lint_wiki(), [])

    def test_repeated_workspace_load_is_idempotent(self):
        first = bootstrap.seed_workspace(self.store)
        second = bootstrap.seed_workspace(self.store)
        self.assertTrue(all(document["unchanged"] for document in second["documents"]))
        self.assertEqual({page["id"] for page in first["pages"]}, {page["id"] for page in second["pages"]})
        self.assertEqual(second["created_pages"], [])
        self.assertEqual(len(second["skipped_pages"]), 4)
        self.assertTrue(all(item["reason"] == "unchanged" for item in second["skipped_pages"]))
        self.assertEqual(len(self.store.list_wiki(include_history=True)), 4)
        self.assertEqual(len(self.store.list_documents(include_history=True)), 16)

    def test_existing_user_slug_is_not_overwritten_or_published(self):
        document = self.store.ingest("用户原文", "这是用户已核对的内部说明。", "user:note")
        user_page = self.store.create_wiki("sample-device-support", "用户自己整理的页面",
                                           [{"text": "用户内容", "evidence_ids": document["chunk_ids"]}])
        result = bootstrap.seed_workspace(self.store)
        self.assertEqual(len(result["created_pages"]), 3)
        self.assertEqual(self.store.get_wiki(user_page["id"])["status"], "draft")
        self.assertEqual(self.store.get_wiki(user_page["id"])["title"], "用户自己整理的页面")
        matching = [item for item in result["skipped_pages"] if item["slug"] == "sample-device-support"]
        self.assertEqual(matching[0]["reason"], "existing_page")
        self.assertEqual(len([item for item in self.store.list_wiki(True) if item["slug"] == "sample-device-support"]), 1)

    def test_source_revision_invalidates_samples_and_reload_does_not_republish(self):
        first = bootstrap.seed_workspace(self.store)
        current = next(item for item in first["documents"] if item["source_key"] == "example:refund-2026")
        document = self.store.get_document(current["id"])
        self.store.ingest(document["title"], document["text"] + "\n新增虚构复核备注。", document["source_key"])
        page = next(item for item in first["pages"] if item["slug"] == "sample-policy-version-guide")
        self.assertTrue(self.store.get_wiki(page["id"])["stale"])
        second = bootstrap.seed_workspace(self.store)
        self.assertTrue(self.store.get_wiki(page["id"])["stale"])
        self.assertNotIn(page["id"], {item["id"] for item in second["pages"]})
        self.assertFalse(self.store.read_evidence(page["id"])["active"])

    def test_unmatched_quote_cannot_be_published_as_sample(self):
        documents = bootstrap.seed_examples(self.store)
        records = {item["source_key"]: self.store.get_document(item["id"]) for item in documents}
        definition = {"claims": [{"text": "编造的结果", "sources": [
            {"source_key": "example:shipping", "quote": "所有会员终身免费回寄"}]}]}
        with self.assertRaises(ValueError):
            bootstrap._sample_claims(definition, records)

    def test_evaluation_has_twenty_four_cases_with_existing_source_keys(self):
        manifest = json.loads((bootstrap.EXAMPLES_DIR / "manifest.json").read_text(encoding="utf-8"))
        cases = json.loads((bootstrap.EXAMPLES_DIR / "evaluation.json").read_text(encoding="utf-8"))
        keys = {item["source_key"] for item in manifest["documents"]}
        self.assertEqual(len(cases), 24)
        self.assertEqual(len({case["id"] for case in cases}), 24)
        self.assertEqual(sum(case.get("expect_abstain", False) for case in cases), 1)
        for case in cases:
            self.assertTrue(set(case["expected_sources"]) <= keys)
            self.assertTrue(case["answer_points"])


if __name__ == "__main__":
    unittest.main()
