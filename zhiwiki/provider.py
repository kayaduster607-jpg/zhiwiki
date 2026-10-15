"""Small OpenAI-compatible HTTP adapter with sanitized errors and explicit auth."""

import ipaddress
import json
import os
import time
from urllib.parse import urlsplit

from .config import Settings


class ProviderError(RuntimeError):
    """Public provider error deliberately excludes vendor bodies and secrets."""


def _is_loopback(host: str) -> bool:
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def estimate_tokens(value) -> int:
    """Conservative UTF-8 byte budget, including a margin for chat wrappers."""
    # This is a guardrail estimate, never reported as measured provider usage.
    serialized = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return len(serialized.encode("utf-8")) + 32


class OpenAIProvider:
    def __init__(self, settings: Settings, client=None):
        self.settings = settings
        self.client = client
        parsed = urlsplit(settings.base_url)
        if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("base_url must be a service URL without credentials/query/fragment")
        self.loopback = _is_loopback(parsed.hostname)
        if parsed.scheme not in ("http", "https") or (not self.loopback and parsed.scheme != "https"):
            raise ValueError("Remote providers require HTTPS")
        self.url = settings.base_url.rstrip("/") + "/chat/completions"

    def complete(self, messages, tools=None, timeout=None, max_tokens=None):
        """Make one bounded request. Credentials are read only at request time."""
        # Import lazily so offline core remains usable on the Python 3.9 runtime.
        import httpx

        secret = os.environ.get(self.settings.api_key_env, "")
        if not secret and not self.loopback:
            raise ProviderError("Remote provider credential is not configured")
        headers = {"Content-Type": "application/json", "Accept-Encoding": "identity"}
        if secret:
            headers["Authorization"] = "Bearer " + secret
        payload = {"model": self.settings.model, "messages": messages,
                   "max_tokens": max_tokens or self.settings.max_output_tokens,
                   "stream": False}
        if tools:
            payload.update({"tools": tools, "tool_choice": "auto"})
        request_timeout = min(timeout or self.settings.request_timeout_seconds,
                              self.settings.request_timeout_seconds)
        try:
            if self.client is not None:
                body = self._read_response(self.client, headers, payload, request_timeout)
            else:
                with httpx.Client(trust_env=False) as client:
                    body = self._read_response(client, headers, payload, request_timeout)
            return self._normalize(body)
        except ProviderError:
            raise
        except httpx.TimeoutException:
            raise ProviderError("Provider request timed out") from None
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError, RecursionError):
            raise ProviderError("Provider request failed or response was malformed") from None

    def _read_response(self, client, headers, payload, timeout):
        """Cap response bytes and check the wall clock on each received body chunk.

        HTTPX's socket timeouts still apply while a blocking I/O is in progress.
        This bounded reader avoids waiting for an unbounded full response body.
        """
        deadline = time.monotonic() + timeout
        chunks = []
        size = 0
        with client.stream("POST", self.url, headers=headers, json=payload,
                           timeout=timeout, follow_redirects=False) as response:
            if response.status_code < 200 or response.status_code >= 300:
                raise ProviderError("Provider returned HTTP {}".format(response.status_code))
            if time.monotonic() >= deadline:
                raise ProviderError("Provider request timed out")
            # None chunk size exposes arriving chunks without waiting to fill a buffer.
            for chunk in response.iter_bytes():
                if time.monotonic() >= deadline:
                    raise ProviderError("Provider request timed out")
                size += len(chunk)
                if size > 2000000:
                    raise ProviderError("Provider response is too large")
                chunks.append(chunk)
        if time.monotonic() >= deadline:
            raise ProviderError("Provider request timed out")
        return json.loads(b"".join(chunks))

    @staticmethod
    def _normalize(body):
        """Discard private reasoning/vendor fields; validate public chat fields."""
        if not isinstance(body, dict):
            raise ProviderError("Provider returned a malformed chat response")
        choices = body.get("choices")
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise ProviderError("Provider returned a malformed chat response")
        source = choices[0].get("message")
        if not isinstance(source, dict) or source.get("role", "assistant") != "assistant":
            raise ProviderError("Provider returned a malformed assistant message")
        content = source.get("content")
        if content is not None and not isinstance(content, str):
            raise ProviderError("Provider returned unsupported message content")
        message = {"role": "assistant", "content": content}
        calls = source.get("tool_calls")
        if calls is not None:
            if not isinstance(calls, list) or len(calls) > 100:
                raise ProviderError("Provider returned invalid tool calls")
            normalized = []
            call_ids = set()
            for call in calls:
                if not isinstance(call, dict) or call.get("type") != "function":
                    raise ProviderError("Provider returned invalid tool calls")
                function = call.get("function")
                call_id = call.get("id")
                if (not isinstance(function, dict) or not isinstance(call_id, str)
                        or not call_id or len(call_id) > 200 or call_id in call_ids
                        or not isinstance(function.get("name"), str)
                        or not isinstance(function.get("arguments"), str)):
                    raise ProviderError("Provider returned invalid tool calls")
                call_ids.add(call_id)
                normalized.append({"id": call_id, "type": "function", "function": {
                    "name": function["name"], "arguments": function["arguments"]}})
            message["tool_calls"] = normalized
        usage = body.get("usage")
        result_usage = {}
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    result_usage[key] = value
        return {"message": message, "usage": result_usage}
