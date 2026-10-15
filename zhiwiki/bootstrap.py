"""显式载入固定清单中的虚构资料与人工 Wiki，不扫描用户工作区。"""

import json
import re
import threading
from datetime import date
from pathlib import Path

from .ingest import read_file


EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples"
_SEED_LOCK = threading.Lock()


def _read_definition(filename):
    """定义文件名由代码固定；不允许从调用方指定任意配置路径。"""
    if filename not in ("manifest.json", "wiki_samples.json"):
        raise ValueError("未知样例定义文件")
    path = (EXAMPLES_DIR / filename).resolve()
    path.relative_to(EXAMPLES_DIR.resolve())
    # JSON 只是固定样例元数据，不是环境变量或可执行指令。
    if any(part.casefold().startswith(".env") for part in path.parts):
        raise ValueError("禁止读取 .env 路径")
    if path.stat().st_size > 256 * 1024:
        raise ValueError("样例定义超过大小上限")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != 1 or payload.get("fictional") is not True:
        raise ValueError("样例定义必须明确标记 fictional 和 schema_version=1")
    return payload


def _document_payloads():
    """先校验完整清单再开始写入，README 与其他新增文件不会被隐式导入。"""
    entries = _read_definition("manifest.json").get("documents")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 40:
        raise ValueError("样例文档清单必须包含 1–40 项")
    payloads = []
    source_keys = set()
    files = set()
    for entry in entries:
        payload, filename = _manifest_document(entry)
        if payload["source_key"] in source_keys or filename in files:
            raise ValueError("样例清单存在重复来源键或文件")
        source_keys.add(payload["source_key"])
        files.add(filename)
        payloads.append(payload)
    return payloads


def _manifest_document(entry):
    if not isinstance(entry, dict) or entry.get("fictional") is not True:
        raise ValueError("每份样例资料必须明确标记为虚构")
    filename = entry.get("file", "")
    if not isinstance(filename, str) or not re.fullmatch(r"documents/[a-z0-9-]+\.md", filename):
        raise ValueError("样例文件必须来自固定 documents 目录的 Markdown 文件")
    source_key = entry.get("source_key", "")
    if not isinstance(source_key, str) or not re.fullmatch(r"example:[a-z0-9-]+", source_key):
        raise ValueError("样例来源键必须使用 example: 命名空间")
    for field in ("title", "category", "summary", "updated_date"):
        if not isinstance(entry.get(field), str) or not entry[field].strip():
            raise ValueError("样例元数据缺少字段：" + field)
    date.fromisoformat(entry["updated_date"])
    path = (EXAMPLES_DIR / filename).resolve()
    path.relative_to((EXAMPLES_DIR / "documents").resolve())
    payload = read_file(path)
    if payload["text"].splitlines()[0] != "# " + entry["title"]:
        raise ValueError("样例标题与正文一级标题不一致：" + filename)
    payload.update(title=entry["title"], source_key=source_key)
    return payload, filename


def seed_examples(store):
    """兼容原 CLI/API：只返回资料列表，不生成或发布 Wiki。"""
    return [store.ingest(**payload) for payload in _document_payloads()]


def seed_workspace(store):
    """用户显式载入时才创建人工示例页；已有用户页面永不被覆盖。"""
    # 防止同一服务进程中双击按钮造成重复的示例修订。跨进程仍检查
    # 首次修订编号与发布结果，不因竞态发布用户创建或改写的页面。
    with _SEED_LOCK:
        documents = seed_examples(store)
        definitions = _sample_pages()
        current_documents = {item["source_key"]: store.get_document(item["id"]) for item in documents}
        pages, created, skipped = [], [], []
        for definition in definitions:
            _seed_page(store, definition, current_documents, pages, created, skipped)
        return {"documents": documents, "pages": pages, "created_pages": created,
                "skipped_pages": skipped, "fictional": True}


def _sample_pages():
    pages = _read_definition("wiki_samples.json").get("pages")
    if not isinstance(pages, list) or not 1 <= len(pages) <= 8:
        raise ValueError("人工示例 Wiki 清单必须包含 1–8 页")
    slugs = set()
    for page in pages:
        if not isinstance(page, dict) or not re.fullmatch(r"sample-[a-z0-9-]+", page.get("slug", "")):
            raise ValueError("示例 Wiki 必须使用 sample- 命名空间")
        if page["slug"] in slugs:
            raise ValueError("人工示例 Wiki slug 重复")
        slugs.add(page["slug"])
    return pages


def _sample_claims(definition, documents):
    """把人工定义的逐字引文转换为当前原文 chunk ID，不进行模型生成。"""
    claims = []
    entries = definition.get("claims")
    if not isinstance(entries, list) or not entries:
        raise ValueError("人工示例页面缺少结论")
    for claim in entries:
        if not isinstance(claim, dict) or not isinstance(claim.get("text"), str):
            raise ValueError("人工示例结论无效")
        sources = claim.get("sources")
        if not isinstance(sources, list) or not sources:
            raise ValueError("人工示例结论缺少逐字来源")
        evidence_ids = [_quote_chunk(source, documents) for source in sources]
        claims.append({"text": claim["text"], "evidence_ids": list(dict.fromkeys(evidence_ids))})
    return claims


def _quote_chunk(source, documents):
    if not isinstance(source, dict) or not isinstance(source.get("quote"), str) or not source["quote"].strip():
        raise ValueError("人工示例引文不能为空")
    document = documents.get(source.get("source_key"))
    if not document or not document["active"]:
        raise ValueError("人工示例缺少当前有效原文")
    for chunk in document["chunks"]:
        if chunk["active"] and source["quote"] in chunk["text"]:
            return chunk["id"]
    raise ValueError("人工示例引文未出现在当前原文分块中")


def _seed_page(store, definition, documents, pages, created, skipped):
    slug = definition["slug"]
    try:
        claims = _sample_claims(definition, documents)
    except ValueError:
        skipped.append({"slug": slug, "reason": "source_conflict"})
        return
    existing = next((wiki for wiki in store.list_wiki() if wiki["slug"] == slug), None)
    if existing is not None:
        reason = "existing_page"
        if _same_sample(existing, definition, claims) and store.read_evidence(existing["id"])["active"]:
            pages.append(existing)
            reason = "unchanged"
        skipped.append({"slug": slug, "reason": reason, "id": existing["id"]})
        return
    wiki = store.create_wiki(slug, definition["title"], claims, mode="sample", links=definition.get("links", []))
    if wiki["version"] != 1:
        skipped.append({"slug": slug, "reason": "concurrent_change", "id": wiki["id"]})
        return
    try:
        published = store.publish_wiki(wiki["id"])
    except ValueError:
        skipped.append({"slug": slug, "reason": "concurrent_change", "id": wiki["id"]})
        return
    pages.append(published)
    created.append(published["id"])


def _same_sample(existing, definition, claims):
    """只识别完全相同且已发布的示例，不替用户发布任何已有草稿。"""
    return (existing["mode"] == "sample" and existing["status"] == "published"
            and not existing["stale"] and existing["title"] == definition["title"]
            and existing["claims"] == claims and existing["links"] == definition.get("links", []))
