"""Exercise the Wiki lifecycle against real SQLite transactions and retrieval."""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor

from zhiwiki.store import KnowledgeStore


class WikiLifecycleIntegrationTests(unittest.TestCase):
    """Keep source versions, published revisions and citations consistent."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.store = KnowledgeStore(self.workspace.name)
        self.old_text = "退货政策：普通商品签收后七天内可以申请退货。"
        self.document = self.store.ingest(
            "售后退货政策", self.old_text, "policies/returns"
        )

    def make_draft(self, document=None, slug="return-policy", claim=None):
        """Create a cited draft using the public storage contract."""
        selected = document or self.document
        return self.store.create_wiki(
            slug=slug,
            title="退货规则",
            claims=[
                {
                    "text": claim or self.old_text,
                    "evidence_ids": [selected["chunk_ids"][0]],
                }
            ],
            mode="offline",
        )

    def search_wiki(self):
        return self.store.search("退货", kind="wiki", limit=20)

    def update_source(self):
        return self.store.ingest(
            "售后退货政策",
            "退货政策：普通商品签收后十四天内可以申请退货。",
            "policies/returns",
        )

    def test_new_draft_preserves_previous_published_search_result(self):
        """An unfinished revision must not hide the still-valid publication."""
        first = self.store.publish_wiki(self.make_draft()["id"])
        newer = self.make_draft(claim="退货申请期限为签收后七天。")

        self.assertEqual(newer["status"], "draft")
        self.assertGreater(newer["version"], first["version"])
        hits_before = self.search_wiki()
        self.assertEqual([item["id"] for item in hits_before], [first["id"]])

        self.store.publish_wiki(newer["id"])
        hits_after = self.search_wiki()
        self.assertEqual([item["id"] for item in hits_after], [newer["id"]])
        self.assertEqual(self.store.get_wiki(first["id"])["status"], "archived")

    def test_source_update_excludes_old_wiki_and_rebuild_restores_search(self):
        """Updating the original invalidates old knowledge atomically."""
        published = self.store.publish_wiki(self.make_draft()["id"])
        original_export = self.store.export_wiki(published["id"])
        replacement = self.update_source()

        self.assertEqual(self.search_wiki(), [])
        self.assertTrue(self.store.get_wiki(published["id"])["stale"])
        old_chunk = self.store.read_evidence(self.document["chunk_ids"][0])
        self.assertFalse(old_chunk["active"])
        self.assertIn(self.old_text, old_chunk["text"])
        self.assertEqual(
            self.store.get_document(self.document["id"])["text"], self.old_text
        )

        repaired = self.make_draft(
            document=replacement,
            claim="退货政策：普通商品签收后十四天内可以申请退货。",
        )
        self.store.publish_wiki(repaired["id"])
        self.assertEqual(
            [item["id"] for item in self.search_wiki()], [repaired["id"]]
        )
        # Historical exports keep the original claim and original source version.
        historical_export = self.store.export_wiki(published["id"])
        self.assertIn(self.old_text, original_export)
        self.assertIn(self.old_text, historical_export)
        self.assertNotIn("十四天", historical_export)
        self.assertNotEqual(
            historical_export, self.store.export_wiki(repaired["id"])
        )

    def test_source_update_prevents_publishing_existing_draft(self):
        """Publication checks current sources again instead of trusting draft time."""
        draft = self.make_draft()
        self.update_source()

        with self.assertRaises(ValueError):
            self.store.publish_wiki(draft["id"])
        self.assertEqual(self.search_wiki(), [])

    def test_wiki_evidence_resolves_to_immutable_original_chunks(self):
        """Search results expose original provenance, not another Wiki's prose."""
        published = self.store.publish_wiki(self.make_draft()["id"])
        found = self.search_wiki()

        self.assertEqual(len(found), 1)
        wiki_evidence = self.store.read_evidence(found[0]["id"])
        self.assertTrue(wiki_evidence["active"])
        self.assertEqual(wiki_evidence["kind"], "wiki")
        self.assertEqual(
            set(wiki_evidence["source_ids"]), set(published["source_ids"])
        )
        self.assertEqual(
            set(wiki_evidence["source_ids"]), {self.document["chunk_ids"][0]}
        )
        for source_id in wiki_evidence["source_ids"]:
            source = self.store.read_evidence(source_id)
            self.assertEqual(source["kind"], "document")
            self.assertEqual(source["document_id"], self.document["id"])
            self.assertEqual(source["version"], self.document["version"])
            self.assertTrue(source["active"])
            self.assertTrue(source["locator"])

    def test_any_changed_source_invalidates_a_multi_source_wiki(self):
        """All cited documents participate in invalidation, including the second."""
        other = self.store.ingest(
            "退货运费规则", "退货运费：质量问题退货由商家承担运费。", "policies/fees"
        )
        draft = self.store.create_wiki(
            slug="return-with-fees",
            title="退货与运费",
            claims=[
                {
                    "text": self.old_text,
                    "evidence_ids": [self.document["chunk_ids"][0]],
                },
                {
                    "text": "质量问题退货由商家承担运费。",
                    "evidence_ids": [other["chunk_ids"][0]],
                },
            ],
            mode="offline",
        )
        self.store.publish_wiki(draft["id"])
        self.store.ingest(
            "退货运费规则", "退货运费：非质量问题退货由买家承担运费。", "policies/fees"
        )

        self.assertTrue(self.store.get_wiki(draft["id"])["stale"])
        self.assertEqual(self.search_wiki(), [])

    def test_concurrent_publish_and_source_update_never_expose_stale_wiki(self):
        """Either transaction order is valid; a trusted stale page is never valid."""
        for index in range(6):
            with self.subTest(iteration=index):
                source_key = "race/returns/{0}".format(index)
                source = self.store.ingest("退货并发政策", self.old_text, source_key)
                draft = self.make_draft(
                    document=source, slug="race-returns-{0}".format(index)
                )
                barrier = threading.Barrier(2)

                def publish():
                    barrier.wait(timeout=10)
                    try:
                        self.store.publish_wiki(draft["id"])
                        return "published_before_update"
                    except ValueError:
                        return "rejected_after_update"

                def update():
                    barrier.wait(timeout=10)
                    return self.store.ingest(
                        "退货并发政策",
                        "退货政策：普通商品签收后十四天内可以申请退货。",
                        source_key,
                    )

                with ThreadPoolExecutor(max_workers=2) as executor:
                    publication = executor.submit(publish)
                    revision = executor.submit(update)
                    publication.result(timeout=20)
                    updated = revision.result(timeout=20)

                self.assertGreater(updated["version"], source["version"])
                self.assertFalse(
                    self.store.read_evidence(source["chunk_ids"][0])["active"]
                )
                final_wiki = self.store.get_wiki(draft["id"])
                self.assertTrue(final_wiki["stale"])
                self.assertNotIn(draft["id"], [item["id"] for item in self.search_wiki()])


if __name__ == "__main__":
    unittest.main()
