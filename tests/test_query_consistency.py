"""Check evidence freshness across retrieval and final answer construction."""

import json
import tempfile
import unittest

from zhiwiki.config import Settings
from zhiwiki.engine import AgentEngine
from zhiwiki.store import KnowledgeStore


class SearchThenAnswerProvider:
    """A declared test double; its output verifies flow, not model quality."""

    def __init__(self, evidence_id, tool_name="search_documents", before_answer=None):
        self.evidence_id = evidence_id
        self.tool_name = tool_name
        self.before_answer = before_answer
        self.calls = 0

    def complete(self, messages, tools=None, timeout=None, max_tokens=None):
        """Request search first, optionally mutate sources, then cite the old ID."""
        self.calls += 1
        if self.calls == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_consistency_search",
                        "type": "function",
                        "function": {
                            "name": self.tool_name,
                            "arguments": json.dumps(
                                {"query": "退货期限", "limit": 5}, ensure_ascii=False
                            ),
                        },
                    }
                ],
            }
        else:
            if self.before_answer is not None:
                self.before_answer()
                self.before_answer = None
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "answer": "普通商品退货期限为签收后七天。",
                        "evidence_ids": [self.evidence_id],
                        "insufficient_evidence": False,
                    },
                    ensure_ascii=False,
                ),
            }
        return {
            "message": message,
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        }


class QueryConsistencyIntegrationTests(unittest.TestCase):
    """Final citations must describe current evidence, even after a source edit."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.store = KnowledgeStore(self.workspace.name)
        self.document = self.store.ingest(
            "退货期限政策",
            "退货期限：普通商品签收后七天内可申请退货。",
            "policies/returns",
        )
        self.settings = Settings(
            data_dir=self.workspace.name,
            provider="openai",
            base_url="http://127.0.0.1:8000/v1",
            model="scripted-test-double",
        )

    def replace_source(self):
        return self.store.ingest(
            "退货期限政策",
            "退货期限：普通商品签收后十四天内可申请退货。",
            "policies/returns",
        )

    def ask(self, provider):
        """Use the public execution API and verify the saved outcome matches."""
        events = []
        engine = AgentEngine(self.store, self.settings, provider=provider)
        result = engine.run(
            "普通商品签收后的退货期限是多少？",
            strategy="agentic",
            on_event=events.append,
        )
        self.assertEqual(provider.calls, 2)
        self.assertEqual(events[-1]["event"], "final")
        saved = self.store.get_run(result["id"])
        self.assertEqual(saved["status"], result["status"])
        self.assertEqual(saved["citations"], result["citations"])
        return result

    def test_unchanged_retrieved_source_can_be_cited(self):
        """The control case proves the test double emits an accepted answer."""
        source_id = self.document["chunk_ids"][0]
        result = self.ask(SearchThenAnswerProvider(source_id))

        self.assertEqual(result["status"], "completed")
        self.assertIn(source_id, [citation["id"] for citation in result["citations"]])
        self.assertTrue(all(citation["active"] for citation in result["citations"]))

    def test_source_changed_during_model_call_cannot_support_completed_answer(self):
        """Freshness is checked after the model call rather than only at search."""
        source_id = self.document["chunk_ids"][0]
        provider = SearchThenAnswerProvider(
            source_id, before_answer=self.replace_source
        )
        result = self.ask(provider)

        self.assertFalse(self.store.read_evidence(source_id)["active"])
        self.assertEqual(result["status"], "invalid_answer")
        self.assertNotIn(
            source_id, [citation["id"] for citation in result["citations"]]
        )

    def test_wiki_source_changed_during_model_call_invalidates_wiki_citation(self):
        """A Wiki citation cannot bypass freshness checking of original sources."""
        draft = self.store.create_wiki(
            slug="return-deadline",
            title="退货期限",
            claims=[
                {
                    "text": "普通商品退货期限为签收后七天。",
                    "evidence_ids": [self.document["chunk_ids"][0]],
                }
            ],
            mode="offline",
        )
        published = self.store.publish_wiki(draft["id"])
        result = self.ask(
            SearchThenAnswerProvider(
                published["id"],
                tool_name="search_wiki",
                before_answer=self.replace_source,
            )
        )

        self.assertTrue(self.store.get_wiki(published["id"])["stale"])
        self.assertEqual(result["status"], "invalid_answer")
        self.assertNotIn(
            published["id"], [citation["id"] for citation in result["citations"]]
        )


if __name__ == "__main__":
    unittest.main()
