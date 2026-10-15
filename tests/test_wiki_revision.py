"""Manual Wiki edits preserve history and reject stale optimistic updates."""

import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from zhiwiki.conflicts import ConflictError
from zhiwiki.store import KnowledgeStore


class WikiRevisionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = KnowledgeStore(self.directory.name)
        self.document = self.store.ingest("售后政策", "退货申请期限为签收后七天。", "policy")
        self.claims = [{"text": "签收后七天可申请退货。", "evidence_ids": self.document["chunk_ids"]}]
        self.base = self.store.create_wiki("returns", "退货知识", self.claims, links=["related-policy"])

    def revise(self, base=None, claims=None, **kwargs):
        """Use the explicit editor base ID and version in every normal request."""
        base = base or self.base
        return self.store.revise_wiki(base["id"], "人工核对后的退货知识",
            self.claims if claims is None else claims, expected_version=base["version"], **kwargs)

    def update_source(self):
        return self.store.ingest("售后政策", "退货申请期限更新为签收后十五天。", "policy")

    def test_manual_revision_preserves_exact_base_and_creates_draft(self):
        before = self.store.get_wiki(self.base["id"])
        revised = self.revise()
        self.assertEqual(revised["mode"], "manual")
        self.assertEqual(revised["status"], "draft")
        self.assertFalse(revised["stale"])
        self.assertEqual(revised["slug"], self.base["slug"])
        self.assertEqual(revised["version"], 2)
        self.assertNotEqual(revised["id"], self.base["id"])
        self.assertEqual(self.store.get_wiki(self.base["id"]), before)
        self.assertEqual(len(self.store.list_wiki(include_history=True)), 2)
        self.assertEqual(self.store.search("退货", kind="wiki"), [])

    def test_published_base_stays_visible_until_new_draft_is_published(self):
        self.store.publish_wiki(self.base["id"])
        before = self.store.get_wiki(self.base["id"])
        revised = self.revise()
        self.assertEqual(self.store.get_wiki(self.base["id"]), before)
        self.assertEqual([item["id"] for item in self.store.search("退货", kind="wiki")], [self.base["id"]])
        self.store.publish_wiki(revised["id"])
        self.assertEqual(self.store.get_wiki(self.base["id"])["status"], "archived")
        self.assertEqual([item["id"] for item in self.store.search("退货", kind="wiki")], [revised["id"]])
        with self.assertRaises(ValueError):
            self.store.publish_wiki(self.base["id"])

    def test_superseded_base_and_replayed_save_are_conflicts(self):
        revised = self.revise()
        with self.assertRaises(ConflictError):
            self.revise()
        with self.assertRaises(ConflictError):
            self.store.revise_wiki(revised["id"], "标题", self.claims, expected_version=1)
        self.assertEqual(len(self.store.list_wiki(include_history=True)), 2)

    def test_expected_version_is_required_and_strictly_positive_integer(self):
        with self.assertRaises(TypeError):
            self.store.revise_wiki(self.base["id"], "标题", self.claims)
        for value in (None, True, False, 0, -1, 1.0, "1"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.store.revise_wiki(self.base["id"], "标题", self.claims, expected_version=value)
        self.assertEqual(len(self.store.list_wiki(include_history=True)), 1)

    def test_missing_base_is_not_found(self):
        with self.assertRaises(KeyError):
            self.store.revise_wiki("wiki_unknown", "标题", self.claims, expected_version=1)

    def test_stale_base_can_be_repaired_only_with_current_sources(self):
        self.store.publish_wiki(self.base["id"])
        current = self.update_source()
        before = self.store.get_wiki(self.base["id"])
        self.assertEqual(before["status"], "stale")
        with self.assertRaises(ConflictError):
            self.revise()
        fresh_claims = [{"text": "签收后十五天可申请退货。", "evidence_ids": current["chunk_ids"]}]
        revised = self.revise(claims=fresh_claims)
        self.assertEqual(revised["status"], "draft")
        self.assertEqual(revised["source_ids"], current["chunk_ids"])
        self.assertEqual(self.store.get_wiki(self.base["id"]), before)
        self.assertIn("七天", self.store.export_wiki(self.base["id"]))
        self.assertIn("十五天", self.store.export_wiki(revised["id"]))

    def test_mixed_current_and_stale_sources_reject_entire_revision(self):
        current = self.update_source()
        claims = [{"text": "比较条款", "evidence_ids": self.document["chunk_ids"] + current["chunk_ids"]}]
        with self.assertRaises(ConflictError):
            self.revise(claims=claims)
        self.assertEqual(len(self.store.list_wiki(include_history=True)), 1)

    def test_missing_source_is_conflict_and_wiki_source_is_invalid_input(self):
        with self.assertRaises(ConflictError):
            self.revise(claims=[{"text": "结论", "evidence_ids": ["chunk_unknown"]}])
        with self.assertRaises(ValueError):
            self.revise(claims=[{"text": "结论", "evidence_ids": [self.base["id"]]}])

    def test_links_default_to_base_and_explicit_empty_clears(self):
        first = self.revise()
        self.assertEqual(first["links"], ["related-policy"])
        second = self.revise(base=first, links=[])
        self.assertEqual(second["links"], [])
        third = self.revise(base=second, links=["new-page", "new-page"])
        self.assertEqual(third["links"], ["new-page"])

    def test_revision_note_persists_separately_from_claims(self):
        revised = self.revise(revision_note="  更正条件，并保留当前来源。  ")
        reopened = KnowledgeStore(self.directory.name).get_wiki(revised["id"])
        self.assertIn("修订说明（维护备注，不作为知识结论）", reopened["body"])
        self.assertIn("更正条件，并保留当前来源。", reopened["body"])
        self.assertEqual(reopened["revision_note"], "更正条件，并保留当前来源。")
        self.assertEqual(reopened["claims"], self.claims)
        self.assertNotIn("更正条件", self.store.get_wiki(self.base["id"])["body"])
        self.assertIn("更正条件", self.store.export_wiki(revised["id"]))

    def test_maintenance_note_is_excluded_from_search_and_read_evidence(self):
        revised = self.revise(revision_note="maintenanceonlytoken 这个说明没有原文依据。")
        self.store.publish_wiki(revised["id"])
        for store in (self.store, KnowledgeStore(self.directory.name)):
            # Check both a cold and a warmed index, including a reopened store.
            for _ in range(2):
                self.assertEqual(store.search("maintenanceonlytoken", kind="wiki"), [])
                hits = store.search("退货", kind="wiki")
                self.assertEqual([hit["id"] for hit in hits], [revised["id"]])
                self.assertNotIn("maintenanceonlytoken", hits[0]["text"])
            evidence = store.read_evidence(revised["id"])
            self.assertNotIn("maintenanceonlytoken", evidence["text"])
            self.assertEqual(evidence["text"], store._wiki_body(
                revised["title"], revised["claims"], revised["mode"]))
        self.assertIn("maintenanceonlytoken", self.store.export_wiki(revised["id"]))

    def test_note_marker_inside_claim_is_not_parsed_as_maintenance_note(self):
        claim = {"text": "引用文字：\n\n## 修订说明（维护备注，不作为知识结论）\n\n仍是已提交的结论文字。",
                 "evidence_ids": self.document["chunk_ids"]}
        first = self.revise(claims=[claim])
        self.assertIsNone(first["revision_note"])
        self.assertIn("仍是已提交的结论文字", self.store.read_evidence(first["id"])["text"])
        second = self.revise(base=first, claims=[claim], revision_note="真正维护备注")
        self.assertEqual(second["revision_note"], "真正维护备注")
        self.assertNotIn("真正维护备注", self.store.read_evidence(second["id"])["text"])

    def test_unknown_body_suffix_is_not_inferred_as_note(self):
        with self.store._connection(write=True) as connection:
            connection.execute("UPDATE wikis SET body=body || ? WHERE id=?",
                               ("\n\n未知正文扩展", self.base["id"]))
        self.assertIsNone(self.store.get_wiki(self.base["id"])["revision_note"])

    def test_revision_input_limits_reuse_core_claim_validation(self):
        invalid_claims = ([], [{"text": "", "evidence_ids": self.document["chunk_ids"]}],
            [{"text": "结论", "evidence_ids": []}],
            [{"text": "字" * 10001, "evidence_ids": self.document["chunk_ids"]}],
            self.claims * 101,
            [{"text": "字" * 10000, "evidence_ids": self.document["chunk_ids"]}] * 11)
        for claims in invalid_claims:
            with self.subTest(count=len(claims)), self.assertRaises(ValueError):
                self.revise(claims=claims)
        for note in (7, "x" * 1001, "note\x00"):
            with self.subTest(note_type=type(note).__name__), self.assertRaises(ValueError):
                self.revise(revision_note=note)
        self.assertEqual(len(self.store.list_wiki(include_history=True)), 1)

    def test_failure_after_insert_rolls_back_entire_revision(self):
        # Raising while materializing the newly inserted row leaves the original
        # and dependency table unchanged once the transaction rolls back.
        with patch.object(self.store, "_wiki", side_effect=RuntimeError("injected readback failure")):
            with self.assertRaises(RuntimeError):
                self.revise()
        self.assertEqual(len(self.store.list_wiki(include_history=True)), 1)
        revised = self.revise()
        self.assertEqual(revised["version"], 2)

    def test_concurrent_editors_allow_exactly_one_revision(self):
        other = KnowledgeStore(self.directory.name)
        barrier = threading.Barrier(2)

        def save(store, suffix):
            barrier.wait(timeout=10)
            try:
                result = store.revise_wiki(self.base["id"], "退货核验 " + suffix,
                    self.claims, expected_version=1)
                return result["status"]
            except ConflictError:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as executor:
            first = executor.submit(save, self.store, "A")
            second = executor.submit(save, other, "B")
            states = [first.result(timeout=20), second.result(timeout=20)]
        self.assertEqual(sorted(states), ["conflict", "draft"])
        self.assertEqual(len(self.store.list_wiki(include_history=True)), 2)

    def test_source_update_race_never_leaves_valid_old_revision(self):
        other = KnowledgeStore(self.directory.name)
        barrier = threading.Barrier(2)

        def save():
            barrier.wait(timeout=10)
            try:
                return self.revise()["id"]
            except ConflictError:
                return None

        def update():
            barrier.wait(timeout=10)
            return other.ingest("售后政策", "退货期限变更为十五天。", "policy")

        with ThreadPoolExecutor(max_workers=2) as executor:
            revision = executor.submit(save)
            source = executor.submit(update)
            revision_id = revision.result(timeout=20)
            source.result(timeout=20)
        if revision_id:
            self.assertTrue(self.store.get_wiki(revision_id)["stale"])
            with self.assertRaises(ValueError):
                self.store.publish_wiki(revision_id)
        else:
            self.assertEqual(len(self.store.list_wiki(include_history=True)), 1)
        self.assertEqual(self.store.search("退货", kind="wiki"), [])


if __name__ == "__main__":
    unittest.main()
