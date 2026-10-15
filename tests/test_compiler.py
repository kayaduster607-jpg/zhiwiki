"""Wiki synthesis and dependency checks with deterministic model substitutes."""

import json

import pytest

from zhiwiki.compiler import WikiCompiler
from zhiwiki.config import Settings
from zhiwiki.store import KnowledgeStore


class WikiProvider:
    def __init__(self, content, before_return=None):
        self.content = content
        self.before_return = before_return
        self.calls = 0

    def complete(self, messages, tools=None, timeout=None, max_tokens=None):
        self.calls += 1
        assert tools is None
        if self.before_return:
            self.before_return()
        return {"message": {"role": "assistant", "content": self.content}, "usage": {
            "prompt_tokens": 200, "completion_tokens": 50, "total_tokens": 250}}


@pytest.fixture
def source(tmp_path):
    store = KnowledgeStore(tmp_path / "data")
    doc = store.ingest("退货政策", "消费者可以在签收后七天内申请退货。", "refund")
    return store, doc


def online():
    return Settings(provider="openai", base_url="http://localhost:9999/v1", model="mock")


def test_offline_only_creates_labelled_draft(source):
    store, doc = source
    wiki = WikiCompiler(store, Settings()).draft("refund", "退货知识", [doc["id"]])
    assert wiki["status"] == "draft"
    assert wiki["mode"] == "offline"
    assert "离线原文摘录" in wiki["claims"][0]["text"]
    assert store.search("退货", kind="wiki") == []


def test_model_generates_source_grounded_claim_and_requires_publish(source):
    store, doc = source
    payload = {"claims": [{"text": "退货申请期限为签收后七天。", "evidence_ids": doc["chunk_ids"]}], "links": []}
    provider = WikiProvider(json.dumps(payload, ensure_ascii=False))
    events = []
    wiki = WikiCompiler(store, online(), provider).draft("refund", "退货知识", [doc["id"]], on_event=events.append)
    assert provider.calls == 1
    assert wiki["status"] == "draft"
    assert wiki["mode"] == "openai"
    assert wiki["claims"] == payload["claims"]
    assert events[-1]["event"] == "final"
    assert events[-1]["data"]["usage"]["total_tokens"] == 250


def test_unseen_and_missing_claim_sources_rejected(source):
    store, doc = source
    for ids in ([], ["chunk_invented"]):
        content = json.dumps({"claims": [{"text": "免费退货。", "evidence_ids": ids}], "links": []})
        with pytest.raises(ValueError, match="supplied original"):
            WikiCompiler(store, online(), WikiProvider(content)).draft("refund", "退货", [doc["id"]])
    assert store.list_wiki() == []


def test_source_change_during_generation_prevents_valid_draft(source):
    store, doc = source
    content = json.dumps({"claims": [{"text": "七天退货。", "evidence_ids": doc["chunk_ids"]}], "links": []})
    provider = WikiProvider(content, lambda: store.ingest("退货政策", "退货期限更新为十五天。", "refund"))
    with pytest.raises(ValueError):
        WikiCompiler(store, online(), provider).draft("refund", "退货", [doc["id"]])
    assert store.search("退货", kind="wiki") == []


def test_historical_document_is_rejected(source):
    store, doc = source
    store.ingest("退货政策", "十五天内可申请退货。", "refund")
    with pytest.raises(ValueError, match="current"):
        WikiCompiler(store, Settings()).draft("refund", "退货", [doc["id"]])


def test_large_context_rejected_without_model_call(source):
    store, doc = source
    settings = online()
    settings.max_context_chars = 100
    provider = WikiProvider("unused")
    with pytest.raises(ValueError, match="context"):
        WikiCompiler(store, settings, provider).draft("refund", "退货", [doc["id"]])
    assert provider.calls == 0


def test_invalid_slug_rejected_before_model_call(source):
    store, doc = source
    provider = WikiProvider("unused")
    with pytest.raises(ValueError, match="slug"):
        WikiCompiler(store, online(), provider).draft("../invalid", "退货", [doc["id"]])
    assert provider.calls == 0
