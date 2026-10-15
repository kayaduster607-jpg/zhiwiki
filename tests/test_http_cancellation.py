"""Exercise early SSE disconnections through the complete ASGI application."""

import asyncio
import json
import tempfile
import threading
import unittest

from starlette.requests import ClientDisconnect

from zhiwiki.api import create_app
from zhiwiki.config import Settings


class BlockingScriptProvider:
    """Hold one simulated model call until the test explicitly releases it."""

    def __init__(self, release):
        self.release = release
        self.entered = threading.Event()
        self.calls = 0

    def complete(self, messages, tools=None, timeout=None, max_tokens=None):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=5):
            raise RuntimeError("The test did not release its simulated model call")
        if self.calls == 1:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [{
                    "id": "call_cancel_check",
                    "type": "function",
                    "function": {
                        "name": "search_documents",
                        "arguments": json.dumps({"query": "退货", "limit": 1}),
                    },
                }],
            }
        else:
            message = {
                "role": "assistant",
                "content": json.dumps({
                    "answer": "No supporting documents are available.",
                    "evidence_ids": [],
                    "insufficient_evidence": True,
                }),
            }
        return {
            "message": message,
            "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
        }


class HttpCancellationIntegrationTests(unittest.IsolatedAsyncioTestCase):
    """A dropped client must neither leak slots nor free a running worker's slot."""

    def setUp(self):
        self.workspace = tempfile.TemporaryDirectory()
        self.addCleanup(self.workspace.cleanup)
        self.app = create_app(Settings(data_dir=self.workspace.name))

    @staticmethod
    async def wait_until(condition, seconds=3):
        """Yield to the ASGI loop while waiting for a controlled test worker."""
        deadline = asyncio.get_running_loop().time() + seconds
        while not condition():
            if asyncio.get_running_loop().time() >= deadline:
                raise AssertionError("Timed out waiting for the test condition")
            await asyncio.sleep(0.01)

    async def request(self, path, fail_on=None, before_disconnect=None):
        """ASGI 2.4 reports closed sockets by raising OSError from send."""
        body = json.dumps({"question": "退货期限"}, ensure_ascii=False).encode("utf-8")
        delivered = False
        messages = []
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [(b"host", b"testserver"), (b"content-type", b"application/json")],
            "client": ("127.0.0.1", 50000),
            "server": ("testserver", 80),
        }

        async def receive():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(message):
            messages.append(message)
            if message["type"] == fail_on:
                if before_disconnect is not None:
                    await before_disconnect()
                raise OSError("Simulated closed client socket")

        try:
            await self.app(scope, receive, send)
        except (ClientDisconnect, OSError):
            if fail_on is None:
                raise
        return next(item["status"] for item in messages if item["type"] == "http.response.start")

    async def test_disconnect_before_headers_does_not_exhaust_query_slots(self):
        """The body generator never starts in this formerly leaking path."""
        for _ in range(4):
            await self.request("/api/query/stream", fail_on="http.response.start")

        self.assertEqual(await self.request("/api/query"), 200)

    async def test_disconnect_during_body_keeps_slot_until_worker_returns(self):
        """Cancellation prevents the next tool/model step but cannot kill a call."""
        release = threading.Event()
        providers = []

        def factory():
            provider = BlockingScriptProvider(release)
            providers.append(provider)
            return provider

        settings = Settings(
            data_dir=self.workspace.name,
            provider="openai",
            base_url="http://127.0.0.1:8000/v1",
            model="cancellation-test-double",
        )
        self.app = create_app(settings, provider_factory=factory)
        try:
            for index in range(4):
                async def wait_for_model():
                    await self.wait_until(lambda: providers[index].entered.is_set())

                await self.request(
                    "/api/query/stream",
                    fail_on="http.response.body",
                    before_disconnect=wait_for_model,
                )
            # The four disconnected requests still have model calls in progress.
            self.assertEqual(await self.request("/api/query"), 429)
            release.set()
            await asyncio.sleep(0.1)

            # Cancellation must stop each script before its requested next tool.
            self.assertEqual([provider.calls for provider in providers], [1, 1, 1, 1])
            self.assertEqual(await self.request("/api/query"), 200)
        finally:
            release.set()
            # Allow worker finally blocks to finish before the temporary DB closes.
            await asyncio.sleep(0.1)


if __name__ == "__main__":
    unittest.main()
