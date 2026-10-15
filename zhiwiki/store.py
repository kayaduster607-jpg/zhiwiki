"""Transactional document history, bounded retrieval, and Wiki publication.

Each operation owns one SQLite connection. Source updates and publication use
BEGIN IMMEDIATE, so freshness checks and writes cannot race each other.
"""

import hashlib
import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

from .conflicts import ConflictError
from .retrieval import split_chunks
from .schema import SCHEMA
from .search_index import SearchIndexCache


DOCUMENT_METADATA = "id, source_key, title, version, content_hash, active, created_at"
CHUNK_WITH_DOCUMENT = """
SELECT c.id, c.document_id, c.text, c.start_offset, c.end_offset,
       d.title, d.source_key, d.version, d.active
FROM chunks AS c JOIN documents AS d ON c.document_id=d.id
"""
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _identifier(prefix: str) -> str:
    return prefix + "_" + uuid.uuid4().hex


def _text(value, label: str, maximum: int) -> str:
    """Validate at the storage boundary, including callers that bypass HTTP."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + "不能为空")
    if len(value) > maximum or "\x00" in value:
        raise ValueError(label + "超过长度上限或包含 NUL 字符")
    return value.strip()


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


class KnowledgeStore:
    """A local workspace database; construction creates its schema if needed."""

    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.data_dir / "knowledge.sqlite3"
        connection = sqlite3.connect(str(self.db_path), timeout=5.0)
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            # Install the epoch row and all invalidation triggers atomically,
            # including when opening a database created by an older version.
            connection.executescript("BEGIN IMMEDIATE;\n" + SCHEMA + "\nCOMMIT;")
        finally:
            connection.close()
        self._search_cache = SearchIndexCache()

    @contextmanager
    def _connection(self, write=False):
        """Never share connections across threads; roll back partial changes."""
        connection = sqlite3.connect(str(self.db_path), timeout=5.0, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=5000")
        try:
            if write:
                connection.execute("BEGIN IMMEDIATE")
            else:
                connection.execute("BEGIN")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def ingest(self, title, text, source_key, expected_document_id=None, require_new=False) -> Dict[str, Any]:
        title = _text(title, "标题", 300)
        source_key = _text(source_key, "来源键", 1000)
        _text(text, "正文", 500_000)
        if type(require_new) is not bool:
            raise ValueError("require_new must be a boolean")
        if expected_document_id is not None:
            _text(expected_document_id, "预期文档 ID", 100)
        if expected_document_id is not None and require_new:
            raise ValueError("新增与更新条件不能同时设置")
        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        with self._connection(write=True) as connection:
            current = connection.execute(
                "SELECT * FROM documents WHERE source_key=? AND active=1", (source_key,)
            ).fetchone()
            # 乐观检查与版本写入在同一事务，防止过期页面覆盖较新的来源。
            if require_new and current:
                raise ConflictError("来源标识已经存在，请选择更新资料")
            if expected_document_id is not None and (current is None or current["id"] != expected_document_id):
                raise ConflictError("资料已被更新，请刷新并重新选择当前版本")
            if current and current["content_hash"] == content_hash and current["title"] == title:
                result = self._document(connection, current)
                result["unchanged"] = True
                return result
            version = current["version"] + 1 if current else 1
            if current:
                self._invalidate_source(connection, current["id"])
            document_id = _identifier("doc")
            connection.execute(
                "INSERT INTO documents VALUES (?,?,?,?,?,?,?,?)",
                (document_id, source_key, title, version, content_hash, text, 1, _now())
            )
            self._insert_chunks(connection, document_id, text)
            row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            return self._document(connection, row)

    def _insert_chunks(self, connection, document_id, text):
        for ordinal, chunk in enumerate(split_chunks(text)):
            connection.execute(
                "INSERT INTO chunks VALUES (?,?,?,?,?,?)",
                (_identifier("chunk"), document_id, ordinal, chunk["text"],
                 chunk["start_offset"], chunk["end_offset"])
            )

    def _invalidate_source(self, connection, document_id):
        """Source versioning and all dependent Wiki invalidations are atomic."""
        connection.execute("UPDATE documents SET active=0 WHERE id=?", (document_id,))
        connection.execute(
            """UPDATE wikis SET stale=1,
               status=CASE WHEN status IN ('published','draft') THEN 'stale' ELSE status END
               WHERE id IN (
                 SELECT wiki_sources.wiki_id FROM wiki_sources JOIN chunks
                 ON wiki_sources.chunk_id=chunks.id WHERE chunks.document_id=?
               )""", (document_id,)
        )

    def _document(self, connection, row, include_text=False):
        result = dict(row)
        result["active"] = bool(result["active"])
        result["unchanged"] = False
        columns = "*" if include_text else "id"
        chunk_rows = connection.execute(
            "SELECT " + columns + " FROM chunks WHERE document_id=? ORDER BY ordinal", (row["id"],)
        ).fetchall()
        result["chunk_ids"] = [chunk["id"] for chunk in chunk_rows]
        if include_text:
            result["chunks"] = [self._chunk_evidence(connection, chunk, document=row) for chunk in chunk_rows]
        else:
            result.pop("text", None)
        return result

    def list_documents(self, include_history=False):
        with self._connection() as connection:
            sql = "SELECT " + DOCUMENT_METADATA + " FROM documents"
            if not include_history:
                sql += " WHERE active=1"
            sql += " ORDER BY source_key, version DESC"
            documents = [dict(row) for row in connection.execute(sql)]
            by_id = {item["id"]: item for item in documents}
            for item in documents:
                item.update(active=bool(item["active"]), unchanged=False, chunk_ids=[])
            chunk_sql = "SELECT c.id, c.document_id FROM chunks c JOIN documents d ON c.document_id=d.id"
            if not include_history:
                chunk_sql += " WHERE d.active=1"
            for row in connection.execute(chunk_sql + " ORDER BY c.document_id, c.ordinal"):
                by_id[row["document_id"]]["chunk_ids"].append(row["id"])
            return documents

    def get_document(self, document_id):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            if row is None:
                raise KeyError(document_id)
            return self._document(connection, row, include_text=True)

    def search(self, query, kind="document", limit=5):
        query = _text(query, "检索词", 4000)
        if kind not in ("document", "wiki", "all"):
            raise ValueError("kind 必须是 document、wiki 或 all")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit 必须在 1–20 之间")
        with self._connection() as connection:
            index = self._search_cache.get(connection, self._search_candidates)
        # The index is immutable; ranking need not retain a SQLite transaction
        # or a cache lock. Final answer validation still re-reads its evidence.
        return index.search(query, limit=limit, kind=kind)

    def cache_stats(self):
        result = self._search_cache.stats()
        with self._connection() as connection:
            result["database_epoch"] = connection.execute(
                "SELECT epoch FROM retrieval_state WHERE id=1").fetchone()[0]
        result["current"] = result["ready"] and result["epoch"] == result["database_epoch"]
        return result

    def _search_candidates(self, connection):
        """Three bounded SQL statements, without per-chunk full-document reads."""
        candidates = [self._joined_chunk_evidence(row) for row in connection.execute(
            CHUNK_WITH_DOCUMENT + " WHERE d.active=1")]
        # The published revision can remain searchable while a newer draft is
        # reviewed. Check every original dependency even if stale was not set.
        rows = connection.execute("""
            SELECT id, slug, title, version, claims_json, mode FROM wikis w
            WHERE w.status='published' AND w.stale=0
              AND EXISTS(SELECT 1 FROM wiki_sources s WHERE s.wiki_id=w.id)
              AND NOT EXISTS(
                SELECT 1 FROM wiki_sources s LEFT JOIN chunks c ON s.chunk_id=c.id
                LEFT JOIN documents d ON c.document_id=d.id
                WHERE s.wiki_id=w.id AND (d.id IS NULL OR d.active=0))
        """).fetchall()
        sources = {}
        for row in connection.execute("""
            SELECT s.wiki_id, s.chunk_id FROM wiki_sources s
            JOIN wikis w ON s.wiki_id=w.id
            WHERE w.status='published' AND w.stale=0 ORDER BY s.wiki_id, s.rowid
        """):
            sources.setdefault(row["wiki_id"], []).append(row["chunk_id"])
        for row in rows:
            item = dict(row)
            # Maintenance notes remain in historical exports, but only cited
            # claims belong in the retrieved knowledge context.
            item["body"] = self._wiki_body(row["title"], json.loads(row["claims_json"]), row["mode"])
            candidates.append(self._wiki_search_item(item, sources[row["id"]], True))
        return candidates

    def read_evidence(self, evidence_id):
        if not isinstance(evidence_id, str):
            raise ValueError("来源 ID 必须是字符串")
        with self._connection() as connection:
            row = connection.execute(CHUNK_WITH_DOCUMENT + " WHERE c.id=?", (evidence_id,)).fetchone()
            if row:
                return self._joined_chunk_evidence(row)
            row = connection.execute("SELECT * FROM wikis WHERE id=?", (evidence_id,)).fetchone()
            if row:
                return self._wiki_evidence(connection, row)
            raise KeyError(evidence_id)

    def _chunk_evidence(self, connection, row, document=None):
        # Historical export callers may supply a bare chunk. Read only its
        # document metadata; get_document supplies the row already in memory.
        if document is None:
            document = connection.execute(
                "SELECT title, source_key, version, active FROM documents WHERE id=?",
                (row["document_id"],)).fetchone()
        joined = dict(row)
        joined.update({key: document[key] for key in ("title", "source_key", "version", "active")})
        return self._joined_chunk_evidence(joined)

    def _joined_chunk_evidence(self, row):
        return {
            "id": row["id"], "kind": "document", "title": row["title"],
            "text": row["text"], "source_key": row["source_key"],
            "version": row["version"],
            "locator": "chars:{}-{}".format(row["start_offset"] + 1, row["end_offset"]),
            "document_id": row["document_id"], "active": bool(row["active"]), "score": 0.0,
        }

    def _wiki_evidence(self, connection, row):
        wiki = self._wiki(connection, row)
        # Maintenance notes remain in history/export, never in model evidence.
        wiki["body"] = self._wiki_body(wiki["title"], wiki["claims"], wiki["mode"])
        active = row["status"] == "published" and not row["stale"]
        if active:
            active = self._sources_current(connection, wiki["source_ids"])
        return self._wiki_search_item(wiki, wiki["source_ids"], active)

    def _wiki_search_item(self, wiki, source_ids, active):
        return {
            "id": wiki["id"], "kind": "wiki", "title": wiki["title"], "text": wiki["body"],
            "source_key": "wiki:" + wiki["slug"], "version": wiki["version"],
            "locator": "wiki:{}@{}".format(wiki["slug"], wiki["version"]),
            "document_id": None, "active": bool(active), "score": 0.0,
            "source_ids": source_ids,
        }

    def create_wiki(self, slug, title, claims, mode="offline", links=None):
        slug = _text(slug, "Wiki slug", 120)
        if not re.fullmatch(r"[\w-]+", slug, flags=re.UNICODE):
            raise ValueError("Wiki slug 仅允许文字、数字、下划线和连字符")
        title = _text(title, "Wiki 标题", 300)
        mode = _text(mode, "Wiki 模式", 40)
        normalized_claims, source_ids = self._validate_claims(claims)
        links = self._validate_links(links)
        with self._connection(write=True) as connection:
            if not self._sources_current(connection, source_ids):
                raise ValueError("Wiki 每条结论必须引用当前有效原文 chunk，不能引用 Wiki")
            latest = connection.execute("SELECT MAX(version) FROM wikis WHERE slug=?", (slug,)).fetchone()[0]
            return self._insert_wiki_revision(connection, slug, title, normalized_claims,
                source_ids, links, mode, (latest or 0) + 1)

    def revise_wiki(self, wiki_id, title, claims, links=None, *, expected_version,
                    revision_note=None):
        """Create a manual draft without replacing the reviewed base revision.

        The version check and source validation share the insertion transaction.
        Omitted links preserve the base links; an explicit [] clears them.
        """
        from .conflicts import ConflictError

        wiki_id = _text(wiki_id, "Wiki ID", 128)
        title = _text(title, "Wiki 标题", 300)
        if type(expected_version) is not int or expected_version < 1:
            raise ValueError("expected_version 必须是正整数")
        normalized_claims, source_ids = self._validate_claims(claims)
        requested_links = None if links is None else self._validate_links(links)
        note = self._validate_revision_note(revision_note)
        with self._connection(write=True) as connection:
            base = connection.execute("SELECT * FROM wikis WHERE id=?", (wiki_id,)).fetchone()
            if base is None:
                raise KeyError(wiki_id)
            latest = connection.execute(
                "SELECT id,version FROM wikis WHERE slug=? ORDER BY version DESC LIMIT 1",
                (base["slug"],)
            ).fetchone()
            if base["version"] != expected_version or latest["id"] != wiki_id:
                raise ConflictError("Wiki 已有其他修订，请刷新最新页面后再保存")
            if base["status"] not in ("draft", "published", "stale"):
                raise ConflictError("此 Wiki 状态不支持修订，请刷新最新页面")
            if not self._sources_current(connection, source_ids):
                raise ConflictError("原文引用已失效或不存在，请替换为当前有效片段后再保存")
            selected_links = json.loads(base["links_json"]) if requested_links is None else requested_links
            return self._insert_wiki_revision(connection, base["slug"], title,
                normalized_claims, source_ids, selected_links, "manual", latest["version"] + 1, note)

    @staticmethod
    def _validate_revision_note(note):
        """Keep an optional bounded maintenance note outside sourced claims."""
        if note is None:
            return None
        if not isinstance(note, str) or len(note) > 1000 or "\x00" in note:
            raise ValueError("revision_note 必须是不超过 1,000 字符且不含 NUL 的文本")
        return note.strip() or None

    def _insert_wiki_revision(self, connection, slug, title, claims, source_ids,
                              links, mode, version, revision_note=None):
        """Insert through the caller's transaction, so checks cannot race writes."""
        wiki_id = _identifier("wiki")
        body = self._wiki_body(title, claims, mode)
        if revision_note:
            # No schema change: the note persists in the immutable Markdown body.
            # It is labelled separately and is not inserted as an uncited claim.
            body += "\n\n## 修订说明（维护备注，不作为知识结论）\n\n" + revision_note
        connection.execute(
            "INSERT INTO wikis (id,slug,title,version,status,stale,claims_json,links_json,body,mode,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (wiki_id, slug, title, version, "draft", 0, _json(claims), _json(links), body, mode, _now())
        )
        connection.executemany("INSERT INTO wiki_sources VALUES (?,?)", [(wiki_id, source_id) for source_id in source_ids])
        row = connection.execute("SELECT * FROM wikis WHERE id=?", (wiki_id,)).fetchone()
        return self._wiki(connection, row)

    def _validate_claims(self, claims):
        if not isinstance(claims, list) or not 1 <= len(claims) <= 100:
            raise ValueError("Wiki 必须包含 1–100 条有引用的结论")
        normalized = []
        source_ids = []
        total_characters = 0
        for claim in claims:
            if not isinstance(claim, dict):
                raise ValueError("结论必须是对象")
            text = _text(claim.get("text"), "结论", 10000)
            total_characters += len(text)
            ids = claim.get("evidence_ids")
            if not isinstance(ids, list) or not 1 <= len(ids) <= 50:
                raise ValueError("每条结论必须包含 1–50 个原文引用")
            if any(not isinstance(source_id, str) or not source_id.startswith("chunk_") for source_id in ids):
                raise ValueError("结论只能引用原文 chunk ID")
            ids = list(dict.fromkeys(ids))
            normalized.append({"text": text, "evidence_ids": ids})
            source_ids.extend(ids)
        if total_characters > 100000:
            raise ValueError("Wiki 结论总长度超过 100,000 字符")
        return normalized, list(dict.fromkeys(source_ids))

    def _validate_links(self, links):
        if links is None:
            return []
        if not isinstance(links, list) or len(links) > 100:
            raise ValueError("Wiki 链接必须是最多 100 项的 slug 列表")
        result = []
        for link in links:
            link = _text(link, "页面链接", 120)
            if not re.fullmatch(r"[\w-]+", link, flags=re.UNICODE):
                raise ValueError("页面链接必须是 Wiki slug")
            result.append(link)
        return list(dict.fromkeys(result))

    def _wiki_body(self, title, claims, mode):
        lines = ["# " + title, "", "生成模式：" + mode]
        if mode == "offline":
            lines.extend(["", "> 离线原文摘录；未经模型综合。发布前需人工核对。"])
        for index, claim in enumerate(claims, 1):
            lines.extend(["", "## 结论 " + str(index), "", claim["text"], "",
                          "来源：" + "、".join("[" + item + "]" for item in claim["evidence_ids"])])
        return "\n".join(lines)

    def _sources_current(self, connection, source_ids):
        if not source_ids:
            return False
        # Bound SQL parameters for older SQLite builds without per-source reads.
        unique_ids = list(dict.fromkeys(source_ids))
        for start in range(0, len(unique_ids), 500):
            batch = unique_ids[start:start + 500]
            placeholders = ",".join("?" for _ in batch)
            current_count = connection.execute(
                "SELECT COUNT(*) FROM chunks JOIN documents ON chunks.document_id=documents.id "
                "WHERE chunks.id IN (" + placeholders + ") AND documents.active=1", batch
            ).fetchone()[0]
            if current_count != len(batch):
                return False
        return True

    def _wiki(self, connection, row):
        result = dict(row)
        result["stale"] = bool(result["stale"])
        result["claims"] = json.loads(result.pop("claims_json"))
        result["links"] = json.loads(result.pop("links_json"))
        result["source_ids"] = list(dict.fromkeys(source_id for claim in result["claims"] for source_id in claim["evidence_ids"]))
        result["revision_note"] = self._read_revision_note(result)
        return result

    def _read_revision_note(self, wiki):
        """Read only the exact suffix after sourced content, never a claim marker."""
        canonical = self._wiki_body(wiki["title"], wiki["claims"], wiki["mode"])
        prefix = canonical + "\n\n## 修订说明（维护备注，不作为知识结论）\n\n"
        if not wiki["body"].startswith(prefix):
            return None
        note = wiki["body"][len(prefix):]
        # Unknown or malformed suffixes are not promoted to a public note field.
        if not note or len(note) > 1000 or "\x00" in note:
            return None
        return note

    def get_wiki(self, wiki_id):
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM wikis WHERE id=?", (wiki_id,)).fetchone()
            if row is None:
                raise KeyError(wiki_id)
            return self._wiki(connection, row)

    def list_wiki(self, include_history=False):
        with self._connection() as connection:
            sql = "SELECT * FROM wikis AS w"
            if not include_history:
                sql += " WHERE version=(SELECT MAX(version) FROM wikis WHERE slug=w.slug)"
            sql += " ORDER BY slug, version DESC"
            return [self._wiki(connection, row) for row in connection.execute(sql)]

    def publish_wiki(self, wiki_id):
        with self._connection(write=True) as connection:
            row = connection.execute("SELECT * FROM wikis WHERE id=?", (wiki_id,)).fetchone()
            if row is None:
                raise KeyError(wiki_id)
            latest = connection.execute("SELECT MAX(version) FROM wikis WHERE slug=?", (row["slug"],)).fetchone()[0]
            if row["version"] != latest:
                raise ValueError("此修订已被较新版本替代，不能发布")
            wiki = self._wiki(connection, row)
            if row["stale"] or not self._sources_current(connection, wiki["source_ids"]):
                raise ValueError("来源已失效，必须重新生成草稿并核对后发布")
            connection.execute("UPDATE wikis SET status='archived' WHERE slug=? AND status='published' AND id<>?", (row["slug"], wiki_id))
            connection.execute("UPDATE wikis SET status='published' WHERE id=?", (wiki_id,))
            return self._wiki(connection, connection.execute("SELECT * FROM wikis WHERE id=?", (wiki_id,)).fetchone())

    def lint_wiki(self):
        """Report deterministic integrity issues without claiming entailment QA."""
        with self._connection() as connection:
            rows = connection.execute("SELECT * FROM wikis AS w WHERE version=(SELECT MAX(version) FROM wikis WHERE slug=w.slug)").fetchall()
            slugs = {row[0] for row in connection.execute("SELECT DISTINCT slug FROM wikis")}
            issues = []
            for row in rows:
                wiki = self._wiki(connection, row)
                if wiki["stale"] or not self._sources_current(connection, wiki["source_ids"]):
                    issues.append({"id": wiki["id"], "code": "stale_sources", "message": "页面含已失效或缺失的原文来源"})
                if wiki["status"] != "published":
                    issues.append({"id": wiki["id"], "code": "unpublished", "message": "最新修订尚未发布"})
                if not wiki["claims"] or any(not claim.get("evidence_ids") for claim in wiki["claims"]):
                    issues.append({"id": wiki["id"], "code": "missing_citation", "message": "结论缺少原文引用"})
                for link in wiki["links"]:
                    if link not in slugs:
                        issues.append({"id": wiki["id"], "code": "broken_link", "message": "链接页面不存在：" + link})
                    elif link == wiki["slug"]:
                        issues.append({"id": wiki["id"], "code": "self_link", "message": "页面链接指向自身"})
            return issues

    def export_wiki(self, wiki_id):
        """Regenerate an export from historical records, even after invalidation."""
        with self._connection() as connection:
            row = connection.execute("SELECT * FROM wikis WHERE id=?", (wiki_id,)).fetchone()
            if row is None:
                raise KeyError(wiki_id)
            wiki = self._wiki(connection, row)
            lines = [wiki["body"], "", "## 原始资料", "",
                     "Wiki 修订：{} · 状态：{} · 失效：{}".format(wiki["version"], wiki["status"], wiki["stale"]), ""]
            for source_id in wiki["source_ids"]:
                chunk = connection.execute("SELECT * FROM chunks WHERE id=?", (source_id,)).fetchone()
                source = self._chunk_evidence(connection, chunk)
                lines.append("- [{}] {} · v{} · {} · source_key={} · 当前有效={}".format(
                    source_id, source["title"], source["version"], source["locator"], source["source_key"], source["active"]))
            if wiki["links"]:
                lines.extend(["", "## 相关页面", ""] + ["- [[" + link + "]]" for link in wiki["links"]])
            return "\n".join(lines) + "\n"

    def save_run(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("运行记录必须是对象")
        record = dict(payload)
        run_id = record.get("id") or _identifier("run")
        if not isinstance(run_id, str) or not re.fullmatch(r"run_[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("运行 ID 必须为 run_ 前缀标识")
        record["id"] = run_id
        record.setdefault("created_at", _now())
        try:
            encoded = _json(record)
        except (TypeError, ValueError) as error:
            raise ValueError("运行记录必须可 JSON 序列化") from error
        if len(encoded) > 2_000_000:
            raise ValueError("运行记录超过 2,000,000 字符上限")
        with self._connection(write=True) as connection:
            connection.execute("INSERT INTO runs VALUES (?,?,?)", (run_id, encoded, record["created_at"]))
        return run_id

    def get_run(self, run_id):
        with self._connection() as connection:
            row = connection.execute("SELECT payload_json FROM runs WHERE id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            return json.loads(row["payload_json"])
