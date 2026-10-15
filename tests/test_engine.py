"""Control-flow tests use a scripted provider, never a real model service."""

import json
import time

import pytest

from zhiwiki.config import Settings
from zhiwiki.engine import AgentEngine
from zhiwiki.provider import ProviderError
from zhiwiki.store import KnowledgeStore


def tool_call(name, arguments, call_id="call_1"):
    return {"message": {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}]},
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


def final_answer(ids, answer="七天内可申请退货。", insufficient=False):
    return {"message": {"role": "assistant", "content": json.dumps({"answer": answer,
        "evidence_ids": ids, "insufficient_evidence": insufficient}, ensure_ascii=False)},
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}}


class ScriptProvider:
    def __init__(self, steps):
        self.steps = list(steps)
        self.calls = []

    def complete(self, messages, tools=None, timeout=None, max_tokens=None):
        self.calls.append({"messages": list(messages), "tools": tools, "timeout": timeout, "max_tokens": max_tokens})
        step = self.steps.pop(0)
        if isinstance(step, Exception):
            raise step
        return step(messages) if callable(step) else step


@pytest.fixture
def corpus(tmp_path):
    store = KnowledgeStore(tmp_path / "data")
    document = store.ingest("退货政策", "商品签收后七天内可以申请退货。质量问题由商家承担运费。", "refund")
    return store, document


def online_settings(**kwargs):
    return Settings(provider="openai", base_url="http://127.0.0.1:8000/v1", model="script", **kwargs)


def test_agent_search_read_search_answer_and_public_events(corpus):
    store, document = corpus
    chunk = document["chunk_ids"][0]
    provider = ScriptProvider([tool_call("search_documents", {"query": "退货"}),
        tool_call("read_evidence", {"evidence_id": chunk}, "call_2"),
        tool_call("search_documents", {"query": "运费"}, "call_3"), final_answer([chunk])])
    events = []
    result = AgentEngine(store, online_settings(), provider).run("什么情况下退货？", on_event=events.append)
    assert result["status"] == "completed"
    assert result["usage"]["model_calls"] == 4
    assert result["usage"]["tool_calls"] == 3
    assert result["citations"][0]["id"] == chunk
    assert events[-1]["event"] == "final"
    assert store.get_run(result["id"])["answer"] == result["answer"]
    assert all(0 < call["timeout"] <= 20 for call in provider.calls)
    assert [event["data"]["name"] for event in events if event["event"] == "tool" and event["data"]["phase"] == "start"] == ["search_documents", "read_evidence", "search_documents"]


def test_unseen_citation_rejected_even_when_real(corpus):
    store, document = corpus
    provider = ScriptProvider([final_answer([document["chunk_ids"][0]])])
    result = AgentEngine(store, online_settings(), provider).run("退货？")
    assert result["status"] == "invalid_answer"
    assert result["citations"] == []


@pytest.mark.parametrize("content", ["Not JSON", '{"answer":"a"}', '{"answer":"a","evidence_ids":[],"insufficient_evidence":false}'])
def test_invalid_final_format_and_missing_citation(corpus, content):
    provider = ScriptProvider([{"message": {"content": content}, "usage": {}}])
    result = AgentEngine(corpus[0], online_settings(), provider).run("退货？")
    assert result["status"] == "invalid_answer"


def test_offline_is_explicit_extract_and_makes_no_model_call(corpus):
    provider = ScriptProvider([])
    result = AgentEngine(corpus[0], Settings(), provider).run("退货")
    assert result["mode"] == "offline"
    assert result["stop_reason"] == "offline_extract"
    assert "未经模型综合" in result["answer"]
    assert result["usage"]["model_calls"] == 0
    assert not provider.calls


def test_baseline_uses_one_model_call_and_no_tools(corpus):
    store, document = corpus
    provider = ScriptProvider([final_answer([document["chunk_ids"][0]])])
    result = AgentEngine(store, online_settings(), provider).run("退货", strategy="baseline")
    assert result["status"] == "completed"
    assert provider.calls[0]["tools"] is None
    assert result["usage"]["model_calls"] == 1
    assert result["usage"]["tool_calls"] == 2


@pytest.mark.parametrize("setting,reason", [({"max_rounds": 1}, "model_round_limit"),
                                          ({"max_tool_calls": 1}, "tool_call_limit")])
def test_loop_budget_stops_provider(corpus, setting, reason):
    step = tool_call("search_documents", {"query": "退货"})
    provider = ScriptProvider([step, step, step])
    result = AgentEngine(corpus[0], online_settings(**setting), provider).run("退货")
    assert result["status"] == "budget_exceeded"
    assert result["stop_reason"] == reason
    assert result["usage"]["tool_calls"] == 1


def test_token_preflight_avoids_remote_call(corpus):
    provider = ScriptProvider([])
    result = AgentEngine(corpus[0], online_settings(max_total_tokens=100), provider).run("退货")
    assert result["stop_reason"] == "token_limit"
    assert provider.calls == []


def test_context_counts_tools_and_full_history(corpus):
    provider = ScriptProvider([])
    result = AgentEngine(corpus[0], online_settings(max_context_chars=100), provider).run("退货")
    assert result["stop_reason"] == "context_limit"
    assert provider.calls == []


def test_missing_usage_is_estimated_and_labelled(corpus):
    reply = final_answer([], "没有足够证据。", True)
    reply["usage"] = {}
    result = AgentEngine(corpus[0], online_settings(), ScriptProvider([reply])).run("未知问题")
    assert result["status"] == "insufficient_evidence"
    assert result["usage"]["token_usage_known"] is False
    assert result["usage"]["prompt_tokens"] > 0
    assert result["usage"]["completion_tokens"] > 0


def test_timeout_rechecked_after_provider(corpus):
    def delayed(_messages):
        time.sleep(0.02)
        return final_answer([], "证据不足", True)
    provider = ScriptProvider([delayed])
    result = AgentEngine(corpus[0], online_settings(timeout_seconds=0.01), provider).run("退货")
    assert result["stop_reason"] == "time_limit"
    assert result["status"] == "budget_exceeded"


def test_provider_failure_is_explicit_and_sanitized(corpus):
    provider = ScriptProvider([ProviderError("secret vendor data")])
    result = AgentEngine(corpus[0], online_settings(), provider).run("退货")
    assert result["status"] == "provider_error"
    assert "secret" not in json.dumps(result)
    assert result["usage"]["token_usage_known"] is False
    assert result["usage"]["total_tokens"] > 0


def test_tool_argument_validation_rejects_write_and_unknown_fields(corpus):
    provider = ScriptProvider([tool_call("publish_wiki", {"id": "wiki_x"}),
        tool_call("search_documents", {"query": "退货", "path": ".env"}),
        final_answer([], "证据不足", True)])
    result = AgentEngine(corpus[0], online_settings(), provider).run("发布知识库")
    assert result["usage"]["tool_calls"] == 2
    assert result["citations"] == []
    assert len([event for event in result["events"] if event["data"].get("phase") == "rejected"]) == 2


def test_cancellation_callback_stops_before_provider(corpus):
    provider = ScriptProvider([])
    def cancel(event):
        if event["event"] == "model":
            raise RuntimeError("stream_cancelled")
    with pytest.raises(RuntimeError, match="stream_cancelled"):
        AgentEngine(corpus[0], online_settings(), provider).run("退货", on_event=cancel)
    assert not provider.calls


def test_wiki_citation_contains_current_original_sources(corpus):
    store, document = corpus
    wiki = store.create_wiki("refund", "退货主题", [{"text": "七天内退货", "evidence_ids": document["chunk_ids"]}])
    store.publish_wiki(wiki["id"])
    provider = ScriptProvider([tool_call("search_wiki", {"query": "退货"}), final_answer([wiki["id"]])])
    result = AgentEngine(store, online_settings(), provider).run("退货")
    assert result["status"] == "completed"
    assert result["citations"][0]["source_evidence"][0]["id"] == document["chunk_ids"][0]
