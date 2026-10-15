"""HTTP/config tests use MockTransport and never connect to an external service."""

import json
import time

import httpx
import pytest

from zhiwiki.config import Settings, load_settings
from zhiwiki.provider import OpenAIProvider, ProviderError


def settings(**kwargs):
    values = {"provider": "openai", "base_url": "http://127.0.0.1:9999/v1", "model": "mock"}
    values.update(kwargs)
    return Settings(**values)


def test_tool_call_payload_and_private_field_removal(monkeypatch):
    monkeypatch.setenv("RAG_API_KEY", "runtime-test-secret")
    def handler(request):
        assert str(request.url) == "http://127.0.0.1:9999/v1/chat/completions"
        assert request.headers["authorization"] == "Bearer runtime-test-secret"
        payload = json.loads(request.content)
        assert payload["tool_choice"] == "auto"
        assert payload["max_tokens"] == 42
        return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": "{}",
            "reasoning_content": "private", "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "search_documents", "arguments": '{"query":"退货"}'}}]}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12}})
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        response = OpenAIProvider(settings(), client).complete([{"role": "user", "content": "hello"}],
            tools=[{"type": "function", "function": {"name": "search_documents"}}], max_tokens=42)
    assert "reasoning_content" not in response["message"]
    assert response["usage"]["total_tokens"] == 12


def test_remote_requires_https_and_runtime_credential(monkeypatch):
    with pytest.raises(ValueError, match="HTTPS"):
        OpenAIProvider(settings(base_url="http://example.com/v1"))
    monkeypatch.delenv("RAG_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="credential"):
        OpenAIProvider(settings(base_url="https://example.com/v1")).complete([])


@pytest.mark.parametrize("url", ["https://user:secret@example.com/v1", "https://example.com/v1?api_key=secret", "https://example.com/#secret"])
def test_credentials_in_url_are_rejected(url):
    with pytest.raises(ValueError):
        OpenAIProvider(settings(base_url=url))


@pytest.mark.parametrize("status", [302, 401, 500])
def test_vendor_error_body_is_never_echoed(status):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status, text="SECRET-VENDOR-BODY"))) as client:
        with pytest.raises(ProviderError) as caught:
            OpenAIProvider(settings(), client).complete([])
    assert "SECRET" not in str(caught.value)


def test_timeout_is_sanitized():
    def handler(request):
        raise httpx.ReadTimeout("SECRET request body", request=request)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="timed out") as caught:
            OpenAIProvider(settings(), client).complete([])
    assert "SECRET" not in str(caught.value)


@pytest.mark.parametrize("body", [{}, {"choices": []}, {"choices": [{"message": {"content": []}}]}])
def test_malformed_response_is_provider_error(body):
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
        with pytest.raises(ProviderError):
            OpenAIProvider(settings(), client).complete([])


def test_missing_usage_stays_unknown():
    body = {"choices": [{"message": {"content": "{}"}}]}
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))) as client:
        result = OpenAIProvider(settings(), client).complete([])
    assert result["usage"] == {}


def test_explicit_config_relative_data_dir_and_secret_rejection(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"data_dir": "knowledge", "provider": "offline"}), encoding="utf-8")
    loaded = load_settings(str(path))
    assert loaded.data_dir == str((tmp_path / "knowledge").resolve())
    path.write_text(json.dumps({"api_key": "must-not-be-accepted"}), encoding="utf-8")
    with pytest.raises(ValueError, match="API keys"):
        load_settings(str(path))


def test_environment_path_refused_before_any_read(tmp_path):
    # The path deliberately does not exist; rejection must precede filesystem reads.
    with pytest.raises(ValueError, match="Environment"):
        load_settings(str(tmp_path / ".env.secret"))
    with pytest.raises(ValueError, match="Environment"):
        load_settings(str(tmp_path / ".env-private" / "config.json"))


@pytest.mark.parametrize("options", [{"max_rounds": True}, {"timeout_seconds": float("nan")},
    {"max_tool_calls": 0}, {"provider": "other"}, {"api_key_env": "bad-name"}])
def test_settings_reject_invalid_values(options):
    with pytest.raises(ValueError):
        Settings(**options)


class ChunkedBody(httpx.SyncByteStream):
    """Simulate incremental response bodies without any real socket or model."""
    def __init__(self, chunks, delay=0):
        self.chunks = chunks
        self.delay = delay
        self.closed = False

    def __iter__(self):
        for chunk in self.chunks:
            if self.delay:
                time.sleep(self.delay)
            yield chunk

    def close(self):
        self.closed = True


def test_streamed_oversized_body_is_closed_before_full_read():
    body = ChunkedBody([b"x" * 1000000, b"x" * 1000001, b"never read"])
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))) as client:
        with pytest.raises(ProviderError, match="too large"):
            OpenAIProvider(settings(), client).complete([])
    assert body.closed


def test_streamed_slow_body_checks_elapsed_deadline():
    body = ChunkedBody([b'{"choices":', b'[]}', b"never read"], delay=0.02)
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, stream=body))) as client:
        with pytest.raises(ProviderError, match="timed out"):
            OpenAIProvider(settings(), client).complete([], timeout=0.01)
    assert body.closed
