"""FIFO backpressure with per-principal isolation and real completion accounting."""

import asyncio
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor

from .common import ServiceError, check_deadline


class Admission:
    def __init__(self, settings):
        self.settings = settings
        self.condition = asyncio.Condition()
        self.waiters = deque()
        self.principals = set()
        self.active = 0
        self.global_bucket = [float(settings.global_burst), time.monotonic()]
        self.user_buckets = {}
        self.executor = ThreadPoolExecutor(max_workers=settings.active_requests, thread_name_prefix="rag-query")
        self.rejected = self.completed = 0
        self.queue_waits = deque(maxlen=2048)
        self.timed_out_running = 0

    @staticmethod
    def refill(bucket, rate, burst, now):
        bucket[0] = min(burst, bucket[0] + max(0, now - bucket[1]) * rate / 60)
        bucket[1] = now

    def rate_check(self, principal):
        now = time.monotonic()
        user = self.user_buckets.setdefault(principal, [float(self.settings.user_burst), now])
        self.refill(user, self.settings.user_rpm, self.settings.user_burst, now)
        self.refill(self.global_bucket, self.settings.global_rpm, self.settings.global_burst, now)
        if user[0] < 1:
            raise ServiceError(429, "USER_RATE_LIMIT", retryable=True)
        if self.global_bucket[0] < 1:
            raise ServiceError(503, "OVERLOADED", retryable=True)
        user[0] -= 1
        self.global_bucket[0] -= 1
        # Expired inactive buckets cannot grow without limit over a long service lifetime.
        if len(self.user_buckets) > 1000:
            self.user_buckets = {key: value for key, value in self.user_buckets.items()
                                 if now - value[1] < 3600 or key in self.principals}

    async def acquire(self, principal, deadline):
        began = time.monotonic()
        marker = object()
        queued_until = min(deadline, time.monotonic() + self.settings.queue_timeout_ms / 1000)
        async with self.condition:
            if principal in self.principals:
                self.rejected += 1
                raise ServiceError(429, "USER_CONCURRENCY_LIMIT", retryable=True)
            if len(self.principals) >= self.settings.accepted_requests:
                self.rejected += 1
                raise ServiceError(503, "OVERLOADED", retryable=True)
            self.rate_check(principal)
            self.principals.add(principal)
            self.waiters.append(marker)
            try:
                while self.active >= self.settings.active_requests or self.waiters[0] is not marker:
                    remaining = queued_until - time.monotonic()
                    if remaining <= 0:
                        raise ServiceError(504, "DEADLINE_EXCEEDED")
                    try:
                        await asyncio.wait_for(self.condition.wait(), remaining)
                    except asyncio.TimeoutError:
                        raise ServiceError(504, "DEADLINE_EXCEEDED") from None
                check_deadline(deadline)
                self.waiters.popleft()
                self.active += 1
                self.queue_waits.append((time.monotonic() - began) * 1000)
                self.condition.notify_all()
            except BaseException:
                if marker in self.waiters:
                    self.waiters.remove(marker)
                self.principals.discard(principal)
                self.condition.notify_all()
                raise

    async def release(self, principal):
        async with self.condition:
            self.active -= 1
            self.principals.discard(principal)
            self.completed += 1
            self.condition.notify_all()

    async def run(self, principal, deadline, action):
        await self.acquire(principal, deadline)
        loop = asyncio.get_running_loop()
        try:
            future = loop.run_in_executor(self.executor, action)
        except BaseException:
            await self.release(principal)
            raise

        detached = False

        def finish(completed):
            # The lease follows actual worker termination, not the HTTP timeout.
            try:
                completed.exception()
            except (asyncio.CancelledError, Exception):
                pass
            if detached:
                self.timed_out_running -= 1
            loop.create_task(self.release(principal))

        future.add_done_callback(finish)
        try:
            return await asyncio.wait_for(asyncio.shield(future), max(0.001, deadline - time.monotonic()))
        except asyncio.TimeoutError:
            if not future.done():
                detached = True
                self.timed_out_running += 1
            raise ServiceError(504, "DEADLINE_EXCEEDED") from None
        except asyncio.CancelledError:
            if not future.done():
                detached = True
                self.timed_out_running += 1
            raise

    def stats(self):
        return {"active": self.active, "waiting": len(self.waiters), "accepted": len(self.principals),
                "completed": self.completed, "rejected": self.rejected,
                "timed_out_running": self.timed_out_running,
                "max_recent_queue_wait_ms": round(max(self.queue_waits, default=0), 3)}

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
