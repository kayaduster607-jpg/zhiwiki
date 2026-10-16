"""Long-lived HTTP adapter for one RAG principal; usable as a LangGraph node.

Credentials belong to a client instance, never mutable shared HTTP headers.
Transport retries retain the logical idempotency key and total deadline.
"""

import asyncio
import random
import secrets
import time
from urllib.parse import urlsplit

import httpx


class RagAPIError(RuntimeError):
    def __init__(self, status, code, request_id=None):
        super().__init__("RAG {} {}".format(status, code))
        self.status, self.code, self.request_id = status, code, request_id


class RagClient:
    def __init__(self, base_url, token, *, http_client=None, timeout=3):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"))):
            raise ValueError("Use HTTPS, or loopback HTTP, without URL credentials")
        if not token or not .1 <= timeout <= 30:
            raise ValueError("Provide a token and a bounded total timeout")
        self.base = base_url.rstrip("/") + "/v1/workspaces/default"
        self.token, self.timeout = token, timeout
        self.owns_client = http_client is None
        self.http = http_client or httpx.AsyncClient(
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=20), trust_env=False,
            timeout=httpx.Timeout(timeout, connect=.5, pool=.5))
        self.serial = asyncio.Semaphore(1)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *unused):
        await self.aclose()

    async def aclose(self):
        if self.owns_client:
            await self.http.aclose()

    async def call(self, operation, payload, *, key=None, deadline=None):
        end = min(deadline or float("inf"), time.monotonic() + self.timeout)
        key = key or "sdk-" + secrets.token_hex(16)
        # Includes waiting for this principal's previous tool call.
        return await asyncio.wait_for(self._serial_call(operation, payload, key, end), max(.001, end - time.monotonic()))

    async def _serial_call(self, operation, payload, key, end):
        async with self.serial:
            for attempt in range(2):
                remaining = end - time.monotonic()
                if remaining <= .05:
                    raise asyncio.TimeoutError()
                headers = {"Authorization": "Bearer " + self.token, "Idempotency-Key": key,
                           "X-Request-Timeout-Ms": str(min(2000, max(50, int(remaining * 1000))))}
                try:
                    response = await self.http.post(self.base + operation, json=payload, headers=headers)
                except (httpx.ConnectError, httpx.ConnectTimeout):
                    # Only connection establishment failures are known not to
                    # have submitted a body; unknown read outcomes are surfaced.
                    if attempt:
                        raise
                    await asyncio.sleep(min(.05 + random.random() * .05, max(0, end - time.monotonic())))
                    continue
                data = response.json()
                if response.is_success:
                    return data
                error = data.get("error", {})
                if attempt == 0 and response.status_code in (429, 503) and error.get("retryable"):
                    try:
                        delay = float(response.headers.get("Retry-After", "1")) + random.random() * .05
                    except ValueError:
                        delay = 1
                    if 0 <= delay < end - time.monotonic() - .05:
                        await asyncio.sleep(delay)
                        continue
                raise RagAPIError(response.status_code, error.get("code", "HTTP_ERROR"), data.get("request_id"))

    async def retrieve(self, query, knowledge_base_ids, *, deadline=None, key=None, **options):
        return await self.call("/retrievals", {"query": query, "knowledge_base_ids": knowledge_base_ids, **options}, deadline=deadline, key=key)

    async def read(self, retrieval_ids, evidence_ids, *, deadline=None, key=None, **options):
        return await self.call("/evidence:read", {"retrieval_ids": retrieval_ids, "evidence_ids": evidence_ids, **options}, deadline=deadline, key=key)

    async def validate(self, retrieval_ids, citations, *, deadline=None):
        return await self.call("/citations:validate", {"retrieval_ids": retrieval_ids, "citations": citations}, deadline=deadline)


def langgraph_node(client, knowledge_base_ids, task_timeout=9):
    """Return an async StateGraph node; generation stays in the calling Agent."""
    async def evidence_node(state):
        deadline = min(state.get("rag_deadline", float('inf')), time.monotonic() + task_timeout)
        retrieved = await client.retrieve(state["query"], knowledge_base_ids, deadline=deadline)
        if not retrieved["evidence"]:
            return {"rag": {"retrieval": retrieved, "validation": None}}
        read = await client.read([retrieved["retrieval_id"]], [item["evidence_id"] for item in retrieved["evidence"]], deadline=deadline)
        validation = await client.validate([read["retrieval_id"]], [
            {"evidence_id": item["evidence_id"], "text_sha256": item["text_sha256"]} for item in read["evidence"]], deadline=deadline)
        return {"rag": {"retrieval": read, "validation": validation}}
    return evidence_node
