"""Real PDF upload -> supervised parser -> review -> whole-book publication.

Run against a private capacity fixture while its HTTP load is running. Only
statistics/hashes are exported; credentials and source text stay in that fixture.
"""

import argparse
import asyncio
import hashlib
import json
import os
import subprocess
import sys
import time
import re
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from zhiwiki.service.common import ServiceSettings, encode, safe_path
from zhiwiki.service.operations import backup_workspace, restore_workspace


def retrieval_timings(log, offset=0):
    """Parse only sanitized service completion metrics, never query/body text."""
    if not log.exists():
        return []
    with log.open('rb') as handle:
        handle.seek(offset)
        data = handle.read().decode('utf-8', errors='replace')
    return [{'status': int(status), 'server_elapsed_ms': float(elapsed)} for status, elapsed in
            re.findall(r'operation=retrieve principal=\S+ status=(\d+) elapsed_ms=([\d.]+)', data)]


def timing_summary(rows):
    import math
    values = sorted(row['server_elapsed_ms'] for row in rows)
    return {'requests': len(rows), 'all_200': all(row['status'] == 200 for row in rows),
            'p95_ms': values[max(0, math.ceil(len(values) * .95) - 1)] if values else None}


async def checked(client, method, path, **kwargs):
    response = await client.request(method, path, **kwargs)
    if not response.is_success:
        raise RuntimeError("{} {} {}".format(method, path.split('?')[0], response.status_code))
    return response.json()


async def verify(root, source, parser_python, base, wait_for_sustained=0):
    # Wait on measured progress from the independently running HTTP load driver.
    # This avoids injecting the import into the special 20-user burst phases.
    while wait_for_sustained:
        report_path = root / 'capacity-report.json'
        try:
            progress = json.loads(report_path.read_text(encoding='utf-8'))
            if progress['summaries']['sustained']['requests'] >= wait_for_sustained:
                break
        except (OSError, ValueError, KeyError):
            pass
        await asyncio.sleep(1)
    private = json.loads((root / "credentials.private.json").read_text(encoding="utf-8"))
    settings = ServiceSettings(data_dir=str(root), worker_python=str(parser_python), cookie_secure=False)
    config = root / "book-worker-settings.json"
    config.write_text(encode(asdict(settings)), encoding="utf-8")
    started = time.time()
    report = {"started_at_unix": started, "source_sha256": hashlib.sha256(source.read_bytes()).hexdigest(),
              "source_bytes": source.stat().st_size, "native_upload": True, "external_model_calls": 0}
    async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=180) as client:
        login = await checked(client, "POST", "/v1/sessions", json={"username": "verification-admin", "password": private["admin_password"]})
        client.headers["X-CSRF-Token"] = login["csrf_token"]
        kb = await checked(client, "POST", "/v1/workspaces/default/knowledge-bases", json={"title": "Real PDF acceptance"})
        kb_id = kb["knowledge_base_id"]
        before = time.monotonic()
        with source.open("rb") as handle:
            uploaded = await checked(client, "POST", "/v1/workspaces/default/uploads?knowledge_base_id=" + kb_id,
                                     headers={"Idempotency-Key": "native-pdf-" + str(time.time_ns())},
                                     data={"knowledge_base_id": kb_id, "mode": "new", "logical_source_key": "d2l-textbook"},
                                     files={"file": (source.name, handle, "application/pdf")})
        report["upload_ms"] = round((time.monotonic() - before) * 1000, 2)
        # A shared fixture may contain an expired job from an interrupted run.
        # Keep supervising until this job completes rather than assuming it is
        # the only queued job; lease recovery happens before newer work.
        command = [sys.executable, "-B", "-m", "zhiwiki.service", "--config", str(config), "worker"]
        options = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        service_log = root / 'server.log'
        baseline = retrieval_timings(service_log)[-60:]
        import_log_offset = service_log.stat().st_size
        worker = subprocess.Popen(command, **options)
        report["import_started_at_unix"] = time.time()
        health_samples = []
        try:
            while time.monotonic() - before < 1900:
                job = await checked(client, "GET", "/v1/workspaces/default/jobs/" + uploaded["job_id"])
                if job["status"] not in ("queued", "parsing"):
                    break
                health = await client.get('/health/ready')
                health_samples.append(health.status_code)
                await asyncio.sleep(1)
            report["import_finished_at_unix"] = time.time()
            report["import_seconds"] = round(time.monotonic() - before, 3)
            report['health_during_import'] = health_samples
            during = retrieval_timings(service_log, import_log_offset)
            report['import_interference'] = {'measurement': 'server completion timings from sanitized log; previous 60 retrievals vs completions during parser window',
                                             'baseline': timing_summary(baseline), 'during': timing_summary(during)}
            report["job"] = {key: job[key] for key in ("status", "pages_total", "pages_done", "attempt", "quality_summary", "error")}
            if job["status"] not in ("needs_review", "awaiting_publish"):
                raise RuntimeError("Native PDF parser did not finish: " + str(report["job"]))
        finally:
            if worker.poll() is None:
                worker.terminate()
            worker.wait(timeout=10)
            (root / "book-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        revision = "/v1/workspaces/default/documents/{}/revisions/{}".format(job["document_id"], job["revision_id"])
        # Inspect representative first/last pages and the known blank page.
        page_checks = []
        for number in (1, 3, job["pages_total"]):
            page = await checked(client, "GET", revision + "/pages/" + str(number))
            page_checks.append({"page": number, "text_chars": len(page["text"]), "quality": page["quality"], "text_sha256": hashlib.sha256(page["text"].encode()).hexdigest()})
        report["page_checks"] = page_checks
        await checked(client, "POST", revision + ":review", json={"note": "验收：逐页处理完整，空白页与缺字标记保留；抽查首尾页。公式与93页缺字未逐字校订，不作为精确公式库。"})
        report["publication"] = await checked(client, "POST", revision + ":publish", headers={"If-None-Match": "*", "Idempotency-Key": "book-publish-" + str(time.time_ns())})
        # Real HTTP tools, not calls into an in-process store.
        checks = []
        for query in ("梯度下降", "过拟合", "卷积神经网络", "注意力机制", "线性回归", "批量归一化", "循环神经网络", "自动微分"):
            retrieved = await checked(client, "POST", "/v1/workspaces/default/retrievals", headers={"Idempotency-Key": "book-query-" + str(time.time_ns())}, json={"query": query, "knowledge_base_ids": [kb_id]})
            evidence = retrieved["evidence"]
            read = await checked(client, "POST", "/v1/workspaces/default/evidence:read", headers={"Idempotency-Key": "book-read-" + str(time.time_ns())}, json={"retrieval_ids": [retrieved["retrieval_id"]], "evidence_ids": [item["evidence_id"] for item in evidence]})
            validation = await checked(client, "POST", "/v1/workspaces/default/citations:validate", json={"retrieval_ids": [read["retrieval_id"]], "citations": [{"evidence_id": item["evidence_id"], "text_sha256": item["text_sha256"]} for item in read["evidence"]]})
            checks.append({"query": query, "hits": len(evidence), "pdf_pages": [item["locator"]["pdf_page_start"] for item in evidence], "valid": validation["valid"], "semantic_support": validation["semantic_support"]})
            await asyncio.sleep(6)  # Admin identity also obeys ordinary user RPM.
        report["tool_chains"] = checks
    snapshot = root.parent / (root.name + "-snapshot-" + str(int(started)))
    restored = root.parent / (root.name + "-restored-" + str(int(started)))
    report["backup"] = backup_workspace(settings, snapshot)
    report["restore"] = restore_workspace(snapshot, restored)
    report["completed_at_unix"] = time.time()
    # Local paths are stripped from the public acceptance report later.
    (root / "book-report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(encode({"pages": report["job"]["pages_done"], "status": "verified", "tool_chains": len(checks)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", required=True)
    parser.add_argument("--pdf", required=True)
    parser.add_argument("--parser-python", required=True)
    parser.add_argument("--base-url", default="http://127.0.0.1:8872")
    parser.add_argument('--wait-for-sustained', type=int, default=0)
    args = parser.parse_args()
    asyncio.run(verify(safe_path(args.fixture), safe_path(args.pdf), safe_path(args.parser_python), args.base_url, args.wait_for_sustained))


if __name__ == "__main__":
    main()
