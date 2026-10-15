"""Conditional writes protect users working from older source versions."""
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor

from zhiwiki.conflicts import ConflictError
from zhiwiki.store import KnowledgeStore


class ConditionalIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.store = KnowledgeStore(self.temp.name)
        self.original = self.store.ingest("原文", "最初正文", "source")

    def tearDown(self):
        self.temp.cleanup()

    def test_two_editors_cannot_both_replace_the_same_version(self):
        def update(index):
            try:
                return self.store.ingest("原文", "并发修订" + str(index), "source",
                                         expected_document_id=self.original["id"])["version"]
            except ConflictError:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(update, (1, 2)))
        self.assertCountEqual(results, [2, "conflict"])
        self.assertEqual(len(self.store.list_documents(include_history=True)), 2)

    def test_require_new_rejects_existing_source_without_changing_it(self):
        with self.assertRaises(ConflictError):
            self.store.ingest("错误覆盖", "替换", "source", require_new=True)
        self.assertEqual(self.store.get_document(self.original["id"])["text"], "最初正文")

    def test_missing_expected_source_cannot_become_new_document(self):
        with self.assertRaises(ConflictError):
            self.store.ingest("错误目标", "正文", "absent", expected_document_id=self.original["id"])
        self.assertEqual(len(self.store.list_documents()), 1)


if __name__ == "__main__":
    unittest.main()
