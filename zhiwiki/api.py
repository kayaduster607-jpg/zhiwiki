"""HTTP 装配层，关闭默认 CDN 文档并提供本地操作台。"""

import threading
import uuid
from pathlib import Path
from typing import Literal, Optional

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware

from . import __version__
from .api_models import ChunkPreviewInput, DocumentInput, QueryInput, WikiInput, WikiRevisionInput
from .bootstrap import _read_definition, seed_workspace
from .compiler import WikiCompiler
from .config import load_settings
from .conflicts import ConflictError
from .engine import AgentEngine, BudgetExceeded
from .http_support import LocalRequestBoundary, QueryStreamingResponse, SlotLease, stream_query
from .ingest import parse_upload
from .provider import ProviderError
from .retrieval import split_chunks
from .store import KnowledgeStore


def create_app(settings=None, store=None, provider_factory=None):
    """显式工厂避免 import 时读配置、建库或请求模型。"""
    settings = settings or load_settings()
    store = store or KnowledgeStore(settings.data_dir)
    app = FastAPI(title="知证 · Agentic RAG + Wiki", version=__version__, docs_url=None, redoc_url=None)
    app.state.store = store
    app.state.settings = settings
    slots = threading.BoundedSemaphore(4)
    web = Path(__file__).resolve().parent / "web"
    app.add_middleware(LocalRequestBoundary)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.mount("/assets", StaticFiles(directory=str(web)), name="assets")

    def provider():
        return provider_factory() if provider_factory else None

    def engine():
        return AgentEngine(store, settings, provider=provider())

    def guarded(action):
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "已有 4 个任务执行中，请稍后重试")
        try:
            return action()
        finally:
            slots.release()

    @app.exception_handler(ValueError)
    async def invalid_request(request, exc):
        return JSONResponse({"detail": str(exc)}, status_code=400)

    @app.exception_handler(ConflictError)
    async def version_conflict(request, exc):
        return JSONResponse({"detail": str(exc), "code": "version_conflict"}, status_code=409)

    @app.exception_handler(KeyError)
    async def missing_item(request, exc):
        return JSONResponse({"detail": "指定的资料、页面或记录不存在"}, status_code=404)

    @app.exception_handler(ProviderError)
    async def unavailable_model(request, exc):
        return JSONResponse({"detail": "模型服务不可用，请检查接口配置和运行时密钥"}, status_code=502)

    @app.exception_handler(BudgetExceeded)
    async def budget_limit(request, exc):
        return JSONResponse({"detail": "生成草稿达到预算上限，请减少所选资料或调整预算"}, status_code=400)

    @app.get("/", include_in_schema=False)
    @app.get("/docs", include_in_schema=False)
    def console():
        return FileResponse(web / "index.html")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": __version__, "mode": settings.provider,
                "model": settings.model if settings.provider == "openai" else None,
                "retrieval": "BM25", "scope": "local_single_workspace",
                "limits": {"document_text_chars": 500000, "file_bytes": 2 * 1024 * 1024,
                           "preview_text_chars": 20000, "concurrent_query_tasks": 4}}

    @app.get("/api/examples/catalog")
    def example_catalog():
        # 只读固定 manifest，不接受路径参数，也不因查看目录建立样例数据。
        manifest = _read_definition("manifest.json")
        fields = ("file", "source_key", "title", "category", "updated_date", "fictional", "summary")
        entries = [{field: item[field] for field in fields} for item in manifest["documents"]]
        return {"schema_version": manifest["schema_version"], "fictional": True,
                "document_count": len(entries), "documents": entries}

    @app.get("/api/system/retrieval")
    def retrieval_status():
        # 仅提供索引计数和代次，不返回缓存正文或机器配置。
        return store.cache_stats()

    @app.post("/api/examples")
    def seed():
        return seed_workspace(store)

    @app.post("/api/chunk-preview")
    def chunk_preview(payload: ChunkPreviewInput):
        # 纯计算：不导入文档、不保存轨迹、不创建模型适配器。
        chunks = split_chunks(payload.text, size=payload.size, overlap=payload.overlap, strategy=payload.strategy)
        return {"source_length": len(payload.text), "chunk_count": len(chunks),
                "size": payload.size, "overlap": payload.overlap, "strategy": payload.strategy,
                "duplicate_chars": sum(len(chunk["text"]) for chunk in chunks) - len(payload.text),
                "chunks": chunks}

    @app.get("/api/documents")
    def documents(history: bool = False):
        return {"documents": store.list_documents(include_history=history)}

    @app.post("/api/documents", status_code=201)
    def ingest_document(payload: DocumentInput):
        return store.ingest(**payload.model_dump())

    @app.post("/api/documents/upload", status_code=201)
    async def upload_document(file: UploadFile = File(...), mode: Literal["new", "update"] = "new",
                              target_document_id: str = "", expected_version: Optional[int] = None,
                              source_key: str = "", title: str = Query(default="", max_length=200)):
        try:
            data = await file.read(2 * 1024 * 1024 + 1)
            parsed = parse_upload(file.filename or "", data)
            if title.strip():
                parsed["title"] = title.strip()
            if source_key:
                raise ValueError("上传不再按文件名或来源键隐式更新，请指定 mode=update、target_document_id 和 expected_version")
            if mode == "new":
                if target_document_id or expected_version is not None:
                    raise ValueError("新增资料不能携带更新目标")
                parsed["source_key"] = "upload:" + uuid.uuid4().hex + ":" + (file.filename or "")
                return store.ingest(**parsed, require_new=True)
            if not target_document_id or expected_version is None or expected_version < 1:
                raise ValueError("更新资料必须指定目标文档及预期版本")
            target = store.get_document(target_document_id)
            if not target["active"] or target["version"] != expected_version:
                raise ConflictError("所选资料版本已变化，请刷新后重试")
            return store.ingest(title.strip() or target["title"], parsed["text"], target["source_key"],
                                expected_document_id=target_document_id)
        finally:
            await file.close()

    @app.get("/api/documents/{document_id}")
    def document(document_id: str):
        return store.get_document(document_id)

    @app.get("/api/evidence/{evidence_id}")
    def evidence(evidence_id: str):
        return store.read_evidence(evidence_id)

    @app.post("/api/query")
    def query(payload: QueryInput):
        return guarded(lambda: engine().run(**payload.model_dump()))

    @app.post("/api/query/stream")
    async def query_stream(payload: QueryInput, request: Request):
        if not slots.acquire(blocking=False):
            raise HTTPException(429, "已有 4 个任务执行中，请稍后重试")
        lease = SlotLease(slots)
        try:
            instance = engine()
        except Exception:
            lease.release()
            raise
        return QueryStreamingResponse(stream_query(request, instance, payload.question, payload.strategy, lease), lease,
                                 media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/runs/{run_id}")
    def run_record(run_id: str):
        return store.get_run(run_id)

    @app.get("/api/wiki")
    def wikis(history: bool = False):
        return {"pages": store.list_wiki(include_history=history)}

    @app.post("/api/wiki/draft", status_code=201)
    def draft(payload: WikiInput):
        return guarded(lambda: WikiCompiler(store, settings, provider=provider()).draft(**payload.model_dump()))

    @app.get("/api/wiki/lint")
    def lint():
        return {"issues": store.lint_wiki()}

    @app.get("/api/wiki/{wiki_id}")
    def wiki(wiki_id: str):
        return store.get_wiki(wiki_id)

    @app.post("/api/wiki/{wiki_id}/publish")
    def publish(wiki_id: str):
        return store.publish_wiki(wiki_id)

    @app.post("/api/wiki/{wiki_id}/revisions", status_code=201)
    def revise(wiki_id: str, payload: WikiRevisionInput):
        return store.revise_wiki(wiki_id, **payload.model_dump())

    @app.get("/api/wiki/{wiki_id}/export")
    def export(wiki_id: str):
        return PlainTextResponse(store.export_wiki(wiki_id), media_type="text/markdown",
                                 headers={"Content-Disposition": 'attachment; filename="wiki.md"'})

    return app
