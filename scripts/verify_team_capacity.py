"""Real HTTP capacity run with independent identities and a declared synthetic corpus.

Artifacts and credentials stay under the explicitly selected local data directory.
The sustained phase uses wall-clock seconds; it is not simulated or sped up.
"""

import argparse
import asyncio
import json
import os
import platform
import secrets
import sqlite3
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from zhiwiki.service.auth import Authorization
from zhiwiki.service.common import ServiceSettings, digest, encode, safe_path
from zhiwiki.service.database import ServiceDatabase
from zhiwiki.service.indexes import Indexes
from zhiwiki.store import _now
from zhiwiki.service.persistent_index import checksum


QUERIES = ["七天退货", "退货运费", "质量核验", "普通订单", "配送条件", "容量基准",
           "订单退货", "运费依据", "七天政策", "质量条件", "普通退货", "配送运费",
           "政策基准", "订单条件", "核验质量", "容量政策", "退货配送", "基准运费",
           "七天订单", "普通质量"]


def seed(settings, fragments):
    database = ServiceDatabase(settings)
    auth = Authorization(database)
    admin_password = secrets.token_urlsafe(24)
    auth.create_user("verification-admin", admin_password, True)
    kb_id = auth.create_kb("Synthetic capacity corpus", "kb_capacity")
    tokens = []
    for index in range(40):
        user = auth.create_user("load-user-{:02}".format(index), secrets.token_urlsafe(24))
        auth.grant(user, kb_id, "reader")
        tokens.append(auth.issue(user, kb_ids=[kb_id])["token"])
    template = "容量基准退货政策：普通订单七天内可退货，运费依据质量核验与配送条件确定。"
    per_page = 25
    source_text = (template * 1000)[:(per_page - 1) * 780 + 900]
    recipe = encode({"synthetic": True, "template": template, "fragments": fragments})
    file_hash = digest(recipe)
    (database.root / "uploads" / file_hash).write_text(recipe, encoding="utf-8")
    with database.tx(write=True) as connection:
        connection.execute("INSERT INTO team_books VALUES('book_capacity',?,'synthetic-capacity','合成容量语料','rev_capacity')", (kb_id,))
        connection.execute("INSERT INTO team_revisions(id,book_id,number,status,file_hash,filename,pipeline,created,reviewed) "
                           "VALUES('rev_capacity','book_capacity',1,'published',?,'synthetic.txt','synthetic-fixture-v1',?,1)", (file_hash, time.time()))
        inserted = 0
        for page in range((fragments + per_page - 1) // per_page):
            document = "doc_capacity_" + str(page)
            connection.execute("INSERT INTO documents VALUES(?,?,?,1,?,?,1,?)",
                               (document, "team:book_capacity:page:" + str(page + 1), "合成容量语料", digest(source_text), source_text, _now()))
            connection.execute("INSERT INTO team_pages VALUES('rev_capacity',?,?,?,'{}')", (page + 1, document, source_text))
            for ordinal in range(min(per_page, fragments - inserted)):
                start = ordinal * 780
                connection.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?)",
                                   ("chunk_capacity_{}_{}".format(page, ordinal), document, ordinal, source_text[start:start + 900], start, start + 900))
                inserted += 1
    indexes = Indexes(database)
    generation, artifact, _ = indexes.prepare(kb_id, "book_capacity", "rev_capacity")
    with database.tx(write=True) as connection:
        connection.execute("UPDATE team_kbs SET generation=1,artifact=? WHERE id=?", (artifact, kb_id))
    return {"tokens": tokens, "admin_password": admin_password, "kb_id": kb_id}


async def request(client, base, token, operation, payload=None):
    started = time.monotonic()
    headers = {"Authorization": "Bearer " + token, "Idempotency-Key": "load-" + secrets.token_hex(12)}
    try:
        response = await asyncio.wait_for(client.post(base + operation, headers=headers, json=payload), timeout=3)
        data = response.json()
        elapsed = (time.monotonic() - started) * 1000
        return {"http_status": response.status_code, "elapsed_ms": round(elapsed, 3),
                "server_elapsed_ms": data.get("elapsed_ms"), "error_code": data.get("error", {}).get("code"), "data": data}
    except Exception as error:
        return {"http_status": 0, "elapsed_ms": round((time.monotonic() - started) * 1000, 3), "error_type": type(error).__name__}


def summary(rows):
    values = sorted(row["elapsed_ms"] for row in rows)
    statuses = {}
    for row in rows:
        key = str(row["http_status"])
        statuses[key] = statuses.get(key, 0) + 1
    import math
    percentile = lambda fraction: values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None
    return {"requests": len(rows), "http_counts": statuses, "p50_ms": percentile(.5),
            "p95_ms": percentile(.95), "p99_ms": percentile(.99), "max_ms": max(values) if values else None,
            "passed": bool(rows) and all(row["http_status"] == 200 and row["elapsed_ms"] <= 3000
                                         and (row.get("server_elapsed_ms") or 0) <= 2000 for row in rows)}


async def phase(client, base, credentials, steps=1, users=20):
    prefix = "/v1/workspaces/default"

    async def task(index):
        token = credentials["tokens"][index]
        query = {"query": QUERIES[index % len(QUERIES)], "knowledge_base_ids": [credentials["kb_id"]]}
        response = await request(client, base, token, prefix + "/retrievals", query)
        result = [response]
        if response["http_status"] != 200 or steps == 1:
            return result
        data = response["data"]
        evidence = data["evidence"][0]
        for step in range(1, steps):
            if step % 2:
                body = {"retrieval_ids": [data["retrieval_id"]], "evidence_ids": [evidence["evidence_id"]]}
                response = await request(client, base, token, prefix + "/evidence:read", body)
                if response["http_status"] == 200:
                    data = response["data"]
                    evidence = data["evidence"][0]
            else:
                body = {"retrieval_ids": [data["retrieval_id"]], "citations": [{"evidence_id": evidence["evidence_id"], "text_sha256": evidence["text_sha256"]}]}
                response = await request(client, base, token, prefix + "/citations:validate", body)
            result.append(response)
            if response["http_status"] != 200:
                break
        return result

    groups = await asyncio.gather(*(task(index) for index in range(users)))
    return [row for group in groups for row in group]


async def run(base, credentials, seconds, output):
    all_results = {"bursts": [], "tool_chains": [], "six_calls": [], "sustained": [], "overcapacity": [], "fair_normal": [], "fair_aggressor": []}
    save(output, all_results, seconds)
    limits = httpx.Limits(max_connections=40, max_keepalive_connections=40)
    async with httpx.AsyncClient(limits=limits, trust_env=False, timeout=3) as client:
        for _ in range(120):
            try:
                if (await client.get(base + "/health/ready")).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(.5)
        else:
            raise RuntimeError("Team server did not become ready")
        resources = []
        async with httpx.AsyncClient(base_url=base, trust_env=False, timeout=3) as admin:
            login = await admin.post('/v1/sessions', json={'username': 'verification-admin', 'password': credentials['admin_password']})
            if login.status_code != 200:
                raise RuntimeError('Capacity admin login failed')
            async def sample_resources():
                response = await admin.get('/v1/workspaces/default/metrics')
                if response.status_code != 200:
                    raise RuntimeError('Metrics read failed')
                data = response.json()
                resources.append({'at_unix': time.time(), **{key: data[key] for key in ('admission', 'resources', 'storage')}})
                (output / 'resource-samples.json').write_text(json.dumps(resources, indent=2), encoding='utf-8')
            await sample_resources()
            # A background observer samples the actual service, not the driver.
            async def observe():
                while True:
                    await asyncio.sleep(30)
                    await sample_resources()
            observer = asyncio.create_task(observe())
            # Keep this client open while the remaining load phases execute.
            # The context below is finalized explicitly after the final sample.
            observer_task = observer
            # Move phase execution into a separate coroutine to preserve cleanup.
            try:
                await load_phases(client, base, credentials, seconds, output, all_results)
                await sample_resources()
            finally:
                observer_task.cancel()
                await asyncio.gather(observer_task, return_exceptions=True)
        save(output, all_results, seconds)
    return all_results


async def load_phases(client, base, credentials, seconds, output, all_results):
    for round_number in range(10):
        began = time.monotonic()
        rows = await phase(client, base, credentials)
        all_results["bursts"].extend(rows)
        print(encode({"phase": "20-user-burst", "round": round_number + 1, **summary(rows)}), flush=True)
        if not summary(rows)["passed"]:
            break
        await asyncio.sleep(max(0, 10 - (time.monotonic() - began)))
    await asyncio.sleep(30)
    all_results["tool_chains"] = await phase(client, base, credentials, steps=3)
    print(encode({"phase": "20-agent-chains", **summary(all_results["tool_chains"])}), flush=True)
    await asyncio.sleep(60)
    all_results["six_calls"] = await phase(client, base, credentials, steps=6)
    print(encode({"phase": "20-agent-six-calls", **summary(all_results["six_calls"])}), flush=True)
    await asyncio.sleep(60)
    start = time.monotonic()
    for index in range(seconds):
        await asyncio.sleep(max(0, start + index - time.monotonic()))
        token = credentials["tokens"][index % 20]
        row = await request(client, base, token, "/v1/workspaces/default/retrievals",
                            {"query": QUERIES[index % len(QUERIES)], "knowledge_base_ids": [credentials["kb_id"]]})
        all_results["sustained"].append(row)
        if (index + 1) % 30 == 0:
            print(encode({"phase": "sustained", "elapsed_seconds": index + 1,
                          **summary(all_results["sustained"])}), flush=True)
            save(output, all_results, seconds)
    await asyncio.sleep(60)
    all_results['overcapacity'] = await phase(client, base, credentials, users=40)
    print(encode({'phase': '40-user-overcapacity', **summary(all_results['overcapacity'])}), flush=True)
    await asyncio.sleep(60)
    # Total normal offered rate is 0.5 req/s; one noisy identity offers 10/s.
    # The three normal principals each use only ten calls per minute.
    start = time.monotonic()
    async def aggressor():
        for ordinal in range(600):
            await asyncio.sleep(max(0, start + ordinal / 10 - time.monotonic()))
            row = await request(client, base, credentials['tokens'][20], '/v1/workspaces/default/retrievals',
                                {'query': '退货运费', 'knowledge_base_ids': [credentials['kb_id']]})
            all_results['fair_aggressor'].append(row)
    async def normal(index):
        for ordinal in range(10):
            await asyncio.sleep(max(0, start + ordinal * 6 + index * .5 - time.monotonic()))
            row = await request(client, base, credentials['tokens'][21 + index], '/v1/workspaces/default/retrievals',
                                {'query': QUERIES[index], 'knowledge_base_ids': [credentials['kb_id']]})
            all_results['fair_normal'].append(row)
    await asyncio.gather(aggressor(), *(normal(index) for index in range(3)))
    print(encode({'phase': 'fair-normal', **summary(all_results['fair_normal'])}), flush=True)
    save(output, all_results, seconds)

def save(output, results, target_seconds):
    public = {"scope": "Real loopback HTTP, 40 credentials; synthetic direct-seeded corpus, not answer-quality evaluation",
              "target_sustained_seconds": target_seconds, "summaries": {key: summary(rows) for key, rows in results.items()},
              "samples": {key: [{name: value for name, value in row.items() if name != "data"} for row in rows] for key, rows in results.items()}}
    (output / "capacity-report.json").write_text(json.dumps(public, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--fragments", type=int, default=50000)
    parser.add_argument("--seconds", type=int, default=1800)
    parser.add_argument("--port", type=int, default=8872)
    parser.add_argument("--reuse-fixture", action="store_true", help="Reuse only a previously generated private capacity fixture")
    args = parser.parse_args()
    root = safe_path(args.data_dir)
    if root.exists() and any(root.iterdir()) and not args.reuse_fixture:
        raise ValueError("Use a new empty verification directory")
    settings = ServiceSettings(data_dir=str(root), cookie_secure=False)
    print(encode({"phase": "seed", "fragments": args.fragments}), flush=True)
    if args.reuse_fixture:
        credentials = json.loads((root / "credentials.private.json").read_text(encoding="utf-8"))
        with sqlite3.connect(str(root / "knowledge.sqlite3")) as connection:
            actual = connection.execute("SELECT COUNT(*) FROM chunks WHERE id LIKE 'chunk_capacity_%'").fetchone()[0]
        if actual != args.fragments:
            raise ValueError("Capacity fixture size mismatch")
    else:
        credentials = seed(settings, args.fragments)
    (root / "credentials.private.json").write_text(encode(credentials), encoding="utf-8")
    (root / "settings.json").write_text(encode(asdict(settings)), encoding="utf-8")
    (root / "environment.json").write_text(encode({"python": sys.version, "platform": platform.platform(), "sqlite": sqlite3.sqlite_version,
                                                  "cpu": platform.processor(), "logical_cpus": os.cpu_count(),
                                                  "fragments": args.fragments, "settings": asdict(settings),
                                                  "service_sources": {str(path).replace('\\', '/'): checksum(path) for path in sorted(Path('zhiwiki/service').glob('*.py'))},
                                                  "driver_sha256": checksum(Path(__file__))}), encoding="utf-8")
    command = [sys.executable, "-B", "-m", "zhiwiki.service", "--config", str(root / "settings.json"), "serve", "--port", str(args.port), "--local-http"]
    with (root / "server.log").open("w", encoding="utf-8") as log:
        options = {"stdout": log, "stderr": subprocess.STDOUT}
        if os.name == "nt":
            options["creationflags"] = subprocess.CREATE_NO_WINDOW
        server = subprocess.Popen(command, **options)
        try:
            asyncio.run(run("http://127.0.0.1:" + str(args.port), credentials, args.seconds, root))
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait()


if __name__ == "__main__":
    main()
