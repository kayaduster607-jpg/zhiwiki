"""Exercise the existing offline HTTP API; this does not implement the /v1 PRD."""

import argparse
import json
import sys
import tempfile
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def request(base_url, path, payload=None):
    """Return bounded public results, including expected overload responses."""
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = {} if data is None else {"Content-Type": "application/json"}
    started = time.monotonic()
    try:
        with urlopen(Request(base_url + path, data=data, headers=headers), timeout=15) as response:
            body = response.read(4 * 1024 * 1024 + 1)
            status = response.status
    except HTTPError as error:
        status, body = error.code, error.read(16384)
    if len(body) > 4 * 1024 * 1024:
        raise ValueError("Response exceeded verification limit")
    return {"http_status": status, "elapsed_ms": round((time.monotonic() - started) * 1000, 3),
            "payload": json.loads(body)}


def verify_question(base_url, question):
    result = request(base_url, "/api/query", {"question": question})
    run = result["payload"]
    if result["http_status"] != 200 or run.get("mode") != "offline" or run["usage"]["model_calls"] != 0:
        raise AssertionError("Expected the actual offline API, without any model calls")
    if run["status"] != "completed" or not run["citations"]:
        raise AssertionError("Question did not obtain valid source excerpts")
    citation = run["citations"][0]
    evidence = request(base_url, "/api/evidence/" + citation["id"])["payload"]
    saved = request(base_url, "/api/runs/" + run["id"])["payload"]
    if evidence["text"] != citation["text"] or not evidence["active"] or saved["answer"] != run["answer"]:
        raise AssertionError("Evidence round-trip or persisted record differed")
    return {"question": question, "http_status": 200, "run_status": run["status"],
            "elapsed_ms": result["elapsed_ms"], "run_id": run["id"],
            "first_citation": {key: citation[key] for key in ("id", "title", "locator", "version")},
            "source_roundtrip": "passed", "run_persistence": "passed"}


def verify_burst(base_url, question, count=20):
    """Align anonymous requests to probe the current instance's admission gate."""
    barrier = threading.Barrier(count)

    def call(index):
        barrier.wait(timeout=10)
        response = request(base_url, "/api/query", {"question": question})
        payload = response.pop("payload")
        return {"request": index, **response, "run_status": payload.get("status"),
                "model_calls": payload.get("usage", {}).get("model_calls")}

    with ThreadPoolExecutor(max_workers=count) as pool:
        results = list(pool.map(call, range(count)))
    counts = Counter(item["http_status"] for item in results)
    if set(counts) - {200, 429}:
        raise AssertionError("Unexpected burst HTTP statuses: " + str(counts))
    return {"simultaneous_requests": count, "http_counts": dict(counts), "results": results,
            "scope": "One anonymous burst on one local process; not authenticated multi-user or sustained capacity validation"}


def verify_lifecycle(report_path):
    """Publish/update/revise a real page copy in a disposable HTTP test store."""
    from fastapi.testclient import TestClient
    from zhiwiki.api import create_app
    from zhiwiki.config import Settings

    pages = report_path.parent / "pages.jsonl"
    with pages.open(encoding="utf-8") as handle:
        records = (json.loads(line) for line in handle)
        page = next(record for record in records if record["pdf_page"] == 266)
    checks = {}
    with tempfile.TemporaryDirectory(prefix="book_lifecycle_") as directory:
        with TestClient(create_app(Settings(data_dir=directory, provider="offline"))) as client:
            payload = {"title": "Book page 266 verification copy", "source_key": "lifecycle:page266", "text": page["text"]}
            original = client.post("/api/documents", json=payload).json()
            draft = client.post("/api/wiki/draft", json={"slug": "book-gradient", "title": "梯度下降摘录", "document_ids": [original["id"]]}).json()
            published = client.post("/api/wiki/" + draft["id"] + "/publish")
            checks["draft_then_publish"] = draft["status"] == "draft" and published.status_code == 200
            update = dict(payload, text=payload["text"] + "\n【隔离核验标记】本页副本已形成新版本。")
            current = client.post("/api/documents", json=update).json()
            old = client.get("/api/evidence/" + original["chunk_ids"][0]).json()
            stale = client.get("/api/wiki/" + draft["id"]).json()
            checks["source_update_invalidates_published_wiki"] = current["version"] == 2 and stale["stale"] and stale["status"] == "stale"
            checks["historical_evidence_retained_but_inactive"] = old["text"] == payload["text"] and not old["active"]
            # Exercise the real manual-revision endpoint with a current source.
            revision_input = {"expected_version": draft["version"], "title": "核对新版本后的摘录", "claims": [{"text": current.get("text", update["text"])[:200], "evidence_ids": [current["chunk_ids"][0]]}]}
            revised = client.post("/api/wiki/" + draft["id"] + "/revisions", json=revision_input)
            checks["manual_revision_with_current_source"] = revised.status_code == 201 and revised.json()["status"] == "draft"
            republished = client.post("/api/wiki/" + revised.json()["id"] + "/publish")
            checks["revised_page_published"] = republished.status_code == 200
            conflict = client.post("/api/wiki/" + draft["id"] + "/revisions", json=revision_input)
            checks["stale_editor_rejected"] = conflict.status_code == 409
            history = client.get("/api/wiki/" + draft["id"] + "/export")
            checks["historical_markdown_export"] = history.status_code == 200 and original["chunk_ids"][0] in history.text
    if not all(checks.values()):
        raise AssertionError("Book lifecycle checks failed: " + str(checks))
    return {"source_pdf_page": 266, "mutations_only_in_temporary_database": True,
            "external_model_calls": 0, "checks": checks}


def verify_sse(base_url, question):
    """Check the actual public event stream, rather than pretending token streaming."""
    data = json.dumps({"question": question}, ensure_ascii=False).encode("utf-8")
    req = Request(base_url + "/api/query/stream", data=data,
                  headers={"Content-Type": "application/json"})
    with urlopen(req, timeout=15) as response:
        body = response.read(2 * 1024 * 1024 + 1)
        if "text/event-stream" not in response.headers.get("Content-Type", ""):
            raise AssertionError("Expected an actual SSE response")
    if len(body) > 2 * 1024 * 1024:
        raise AssertionError("SSE verification response exceeded the size limit")
    events, final = [], None
    for block in body.decode("utf-8").split("\n\n"):
        lines = block.splitlines()
        names = [line[7:] for line in lines if line.startswith("event: ")]
        if not names:
            continue
        events.append(names[0])
        if names[0] == "final":
            final = json.loads("\n".join(line[6:] for line in lines if line.startswith("data: ")))
    if events.count("final") != 1 or final["mode"] != "offline" or final["usage"]["model_calls"] != 0:
        raise AssertionError("SSE final event or offline mode was inconsistent")
    return {"event_counts": dict(Counter(events)), "final_status": final["status"],
            "mode": final["mode"], "model_token_streaming": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8871")
    parser.add_argument("--report", default="reports/book-full-run/report.json")
    args = parser.parse_args()
    parsed = urlsplit(args.base_url)
    if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost") or parsed.username or parsed.password:
        raise ValueError("This check only sends book queries to an explicit local service")
    report_path = Path(args.report)
    if any(part.lower().startswith(".env") for part in report_path.parts + report_path.resolve().parts):
        raise ValueError("Environment paths are forbidden")
    book = json.loads(report_path.read_text(encoding="utf-8"))
    health = request(args.base_url, "/api/health")["payload"]
    if health["mode"] != "offline":
        raise ValueError("Refuse a service configured for live model calls")
    documents = request(args.base_url, "/api/documents")["payload"]["documents"]
    if len(documents) != book["extracted_pages"]:
        raise AssertionError("The endpoint is not the intended full-book workspace")
    cases = [verify_question(args.base_url, row["question"]) for row in book["queries"]]
    wiki_id = book["wiki_draft"]["id"]
    wiki = request(args.base_url, "/api/wiki/" + wiki_id)["payload"]
    if wiki["status"] != "draft" or not wiki["source_ids"]:
        raise AssertionError("Expected the real unpublished source-backed Wiki draft")
    burst = verify_burst(args.base_url, cases[0]["question"])
    result = {"compatibility_mode": "legacy_api", "base_url": args.base_url,
              "health": health, "document_count": len(documents), "external_model_calls": 0,
              "queries": cases, "wiki_draft": {"id": wiki_id, "status": wiki["status"]},
              "burst": burst, "lifecycle": verify_lifecycle(report_path),
              "sse": verify_sse(args.base_url, cases[0]["question"]),
              "post_burst_health": request(args.base_url, "/api/health")["payload"]}
    destination = report_path.parent / "http-report.json"
    destination.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"report": str(destination), "questions_passed": len(cases),
                      "document_count": len(documents), "burst_http_counts": burst["http_counts"],
                      "burst_max_elapsed_ms": max(item["elapsed_ms"] for item in burst["results"])}, ensure_ascii=False))


if __name__ == "__main__":
    main()
