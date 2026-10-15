"""Run an explicit PDF through a separate, offline page-based verification store.

This is a verification adapter, not the planned native asynchronous PDF API.
Run with an existing interpreter that has pypdf; no packages are installed.
"""

import argparse
import hashlib
import json
import sys
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhiwiki.compiler import WikiCompiler
from zhiwiki.config import Settings, reject_environment_path
from zhiwiki.engine import AgentEngine
from zhiwiki.store import KnowledgeStore


QUESTIONS = (
    "随机梯度下降为什么比梯度下降的迭代轨迹更曲折？",
    "FCN 如何为每个像素预测类别？",
    "过拟合与欠拟合有什么区别？",
    "丢弃法怎样缓解过拟合？",
    "批量归一化如何在训练和预测时使用？",
    "注意力机制在编码器解码器中如何使用？",
    "梯度裁剪为什么能缓解梯度爆炸？",
    "词嵌入中的负采样是什么？",
)


def checked_path(raw_path):
    """Refuse environment-file components before resolving or opening a path."""
    path = Path(raw_path)
    reject_environment_path(path)
    resolved = path.resolve()
    reject_environment_path(resolved)
    return resolved


def file_hash(path):
    """Hash the explicitly selected book without loading the whole PDF in RAM."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def normalize_page(raw):
    """Keep missing glyphs visible instead of silently deleting PDF NULs."""
    text = unicodedata.normalize("NFKC", raw)
    missing_glyphs = text.count("\x00")
    return text.replace("\x00", "\ufffd"), missing_glyphs


def extract_pages(pdf_path, output):
    """Attempt every page and preserve empty/error outcomes for quality review."""
    import pypdf

    began = time.monotonic()
    records = []
    with pypdf.PdfReader(str(pdf_path)) as reader:
        if reader.is_encrypted and not reader.decrypt(""):
            raise ValueError("PDF requires an explicitly authorized password workflow")
        count = len(reader.pages)
        if count > 2000:
            raise ValueError("Verification is limited to 2,000 pages")
        with (output / "pages.jsonl").open("w", encoding="utf-8") as handle:
            for index, page in enumerate(reader.pages):
                record = {"pdf_page": index + 1}
                try:
                    raw = page.extract_text() or ""
                    text, missing_glyphs = normalize_page(raw)
                    record.update(raw_text=raw, text=text, chars=len(text),
                                  text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                                  status="extracted" if text.strip() else "empty",
                                  missing_glyphs=missing_glyphs,
                                  low_text=bool(text.strip()) and len(text.strip()) < 40)
                except Exception as error:
                    # Record a public error type without treating missing pages as success.
                    record.update(status="error", error_type=type(error).__name__, chars=0)
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
                records.append(record)
                if (index + 1) % 50 == 0 or index + 1 == count:
                    print(json.dumps({"stage": "extract", "pages": index + 1, "total": count}), flush=True)
    return records, {"parser": "pypdf", "parser_version": pypdf.__version__,
                     "normalization": "NFKC; PDF NUL glyphs replaced with visible U+FFFD and counted",
                     "extract_seconds": round(time.monotonic() - began, 3)}


def ingest_pages(store, title, source_key, records):
    """Reuse core ingestion; page documents are only this verification adapter."""
    documents = []
    began = time.monotonic()
    for record in records:
        if record["status"] != "extracted":
            continue
        page = record["pdf_page"]
        document = store.ingest("{} · PDF第{}页".format(title, page), record["text"],
                                "{}:pdf-page:{}".format(source_key, page))
        # Exact slicing is checked against the actual normalized text in the store.
        stored = store.get_document(document["id"])
        if stored["text"] != record["text"]:
            raise AssertionError("Stored page differs from normalized extraction")
        for chunk in stored["chunks"]:
            start, end = (int(value) for value in chunk["locator"].split(":", 1)[1].split("-"))
            if chunk["text"] != record["text"][start - 1:end]:
                raise AssertionError("Evidence offset does not reconstruct the page slice")
        documents.append({"pdf_page": page, "id": document["id"],
                          "version": document["version"], "chunks": len(document["chunk_ids"]),
                          "unchanged": document["unchanged"]})
    return documents, round(time.monotonic() - began, 3)


def check_queries(store, settings):
    """Exercise real retrieval; report evidence without inventing accuracy labels."""
    results = []
    for question in QUESTIONS:
        ranked = store.search(question, limit=3)
        run = AgentEngine(store, settings).run(question)
        results.append({"question": question, "status": run["status"], "run_id": run["id"],
                        "mode": run["mode"], "model_calls": run["usage"]["model_calls"],
                        "elapsed_ms": run["elapsed_ms"], "answer_correctness": "not_scored",
                        "top_hits": [{"title": item["title"], "id": item["id"],
                                      "source_key": item["source_key"], "locator": item["locator"],
                                      "score": item["score"], "preview": item["text"][:300]}
                                     for item in ranked]})
    return results


def check_draft(store, settings, documents):
    """Generate a real offline draft and verify its original-source dependencies."""
    selected = [item["id"] for item in documents if item["pdf_page"] == 266]
    if not selected:
        selected = [documents[0]["id"]]
    wiki = WikiCompiler(store, settings).draft("book-verification-gradient", "梯度下降书籍取证草稿", selected)
    if wiki["status"] != "draft" or not wiki["source_ids"]:
        raise AssertionError("Draft lifecycle or provenance failed")
    return {"id": wiki["id"], "status": wiki["status"], "mode": wiki["mode"],
            "source_count": len(wiki["source_ids"]), "published": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--output-dir", default="reports/book-full-run")
    parser.add_argument("--source-key", default="verification:d2l-textbook")
    args = parser.parse_args()
    source, output = checked_path(args.pdf), checked_path(args.output_dir)
    if source.suffix.lower() != ".pdf" or source.stat().st_size > 200 * 1024 * 1024:
        raise ValueError("Select a PDF no larger than 200 MiB")
    output.mkdir(parents=True, exist_ok=True)
    checksum = file_hash(source)
    previous = output / "report.json"
    if previous.exists() and json.loads(previous.read_text(encoding="utf-8"))["source_sha256"] != checksum:
        raise ValueError("Use another output directory for a different PDF revision")
    pages, parser_info = extract_pages(source, output)
    if file_hash(source) != checksum:
        raise ValueError("The selected PDF changed during extraction; retry a stable revision")
    store = KnowledgeStore(output / "data")
    settings = Settings(data_dir=str(output / "data"), provider="offline")
    documents, ingest_seconds = ingest_pages(store, source.stem, args.source_key, pages)
    if not documents:
        raise ValueError("No page text extracted; OCR/quality review is required")
    queries = check_queries(store, settings)
    draft = check_draft(store, settings, documents)
    write_json(output / "config.json", {"data_dir": "data", "provider": "offline"})
    report = {"generated_at": datetime.now(timezone.utc).isoformat(), "source": str(source),
              "source_sha256": checksum, "source_bytes": source.stat().st_size,
              "scope": "Full PDF attempted; page-based offline verification, not native PDF API or semantic QA",
              **parser_info, "total_pages": len(pages), "extracted_pages": len(documents),
              "empty_pages": [p["pdf_page"] for p in pages if p["status"] == "empty"],
              "error_pages": [{"page": p["pdf_page"], "error_type": p["error_type"]}
                              for p in pages if p["status"] == "error"],
              "low_text_pages": [p["pdf_page"] for p in pages if p.get("low_text")],
              "missing_glyph_pages": [{"page": p["pdf_page"], "missing_glyphs": p["missing_glyphs"]}
                                      for p in pages if p.get("missing_glyphs")],
              "text_chars": sum(p["chars"] for p in pages),
              "chunk_count": sum(d["chunks"] for d in documents), "documents": documents,
              "ingest_seconds": ingest_seconds, "offset_checks": "passed for every stored chunk",
              "external_model_calls": 0, "queries": queries, "wiki_draft": draft,
              "limitations": ["Images, equations and tables have no semantic fidelity guarantee",
                              "Empty/low-text pages require review; not all pages are necessarily scanned",
                              "Question smoke tests are not independent quality or capacity evaluation",
                              "Each page is a document only in this isolated verification store"]}
    write_json(previous, report)
    print(json.dumps({"output": str(output), "pages": len(pages), "extracted_pages": len(documents),
                      "chunks": report["chunk_count"], "errors": report["error_pages"],
                      "query_statuses": [q["status"] for q in queries]}, ensure_ascii=False), flush=True)
    return 1 if report["error_pages"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
