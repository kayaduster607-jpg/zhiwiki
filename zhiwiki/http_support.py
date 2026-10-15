"""本地 HTTP 服务的请求边界与事件流适配。"""

import asyncio
import json
import queue
import threading
from urllib.parse import urlsplit

from starlette.responses import JSONResponse, StreamingResponse


class LocalRequestBoundary:
    """在业务解析前限制请求体，并拒绝其他网站发起的写操作。"""

    def __init__(self, app, max_bytes=3 * 1024 * 1024):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        origin = headers.get(b"origin", b"").decode("latin1")
        host = headers.get(b"host", b"").decode("latin1")
        if origin and urlsplit(origin).netloc != host:
            await JSONResponse({"detail": "不接受跨站请求"}, 403)(scope, receive, send)
            return
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await JSONResponse({"detail": "请求体超过 3 MiB"}, 413)(scope, receive, send)
                return
            if not message.get("more_body", False):
                break
        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


def encode_event(event):
    payload = json.dumps(event["data"], ensure_ascii=False)
    return "event: {}\ndata: {}\n\n".format(event["event"], payload)


class SlotLease:
    """Response 与工作线程交接槽位，任一路径只归还一次。"""

    def __init__(self, slots):
        self.slots = slots
        self.worker_started = False
        self.released = False
        self.lock = threading.Lock()

    def start_worker(self):
        with self.lock:
            if self.released:
                raise RuntimeError("stream_cancelled")
            self.worker_started = True

    def release(self, only_unstarted=False):
        with self.lock:
            if self.released or (only_unstarted and self.worker_started):
                return
            self.released = True
            self.slots.release()


class QueryStreamingResponse(StreamingResponse):
    """响应头阶段断线时 generator 尚未运行，仍必须归还预占槽。"""

    def __init__(self, content, lease, **kwargs):
        self.lease = lease
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send):
        try:
            await super().__call__(scope, receive, send)
        finally:
            self.lease.release(only_unstarted=True)


async def stream_query(request, engine, question, strategy, lease):
    """有界队列承接同步模型调用；断开后阻止后续工具/模型轮次。"""
    messages = queue.Queue(maxsize=64)
    cancelled = threading.Event()

    def emit(event):
        while not cancelled.is_set():
            try:
                messages.put(event, timeout=0.2)
                return
            except queue.Full:
                continue
        raise RuntimeError("stream_cancelled")

    def work():
        try:
            engine.run(question, strategy=strategy, on_event=emit)
        except Exception:
            # 对外错误不包含供应商正文、请求头或配置秘密。
            if not cancelled.is_set():
                emit({"event": "error", "data": {"message": "查询执行失败，请查看配置或重试"}})
        finally:
            lease.release()
            if not cancelled.is_set():
                emit(None)

    lease.start_worker()
    try:
        worker = asyncio.get_running_loop().run_in_executor(None, work)
    except BaseException:
        lease.release()
        raise
    try:
        yield ": connected\n\n"
        while True:
            if await request.is_disconnected():
                break
            try:
                event = await asyncio.to_thread(messages.get, True, 0.3)
            except queue.Empty:
                yield ": heartbeat\n\n"
                continue
            if event is None:
                break
            yield encode_event(event)
    finally:
        cancelled.set()
        # 无法强行取消已在进行的 HTTP 调用；它受模型请求 timeout 约束。
        if worker.done():
            worker.result()
