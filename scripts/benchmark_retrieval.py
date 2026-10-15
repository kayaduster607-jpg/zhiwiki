"""Reproduce a bounded, single-process retrieval benchmark without extra packages."""

import argparse
import json
import math
import platform
import statistics
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhiwiki import retrieval
from zhiwiki.store import KnowledgeStore


QUERIES = ("退货凭证", "和田地区", "Battery warranty", "异或逻辑")
DOCUMENT_COUNT = 64
REPEATS = 3


class MeasuredStore(KnowledgeStore):
    def __init__(self, data_dir):
        self.select_count = 0
        self.full_document_chars = 0
        super().__init__(data_dir)

    def _trace(self, statement):
        if statement.lstrip().upper().startswith("SELECT"):
            self.select_count += 1

    @contextmanager
    def _connection(self, write=False):
        with super()._connection(write=write) as connection:
            connection.set_trace_callback(self._trace)
            yield connection


def populate(store):
    """Deterministic artificial corpus: 64 documents, no external or user data."""
    topics = (
        "退货凭证包含订单截图，售后受理申请并核对退款路径。",
        "和田地区记录运输节点，物流交接保留追踪编号。",
        "Battery warranty covers manufacturing faults and requires a purchase receipt. ",
        "异或逻辑输出由输入差异决定，与门逻辑需要同时满足输入条件。",
    )
    total_chars = 0
    for index in range(DOCUMENT_COUNT):
        topic = topics[index % len(topics)]
        paragraph = (topic * (2600 // len(topic) + 1))[:2600]
        text = "# 合成记录 {}\n\n{}\n记录编号 B{:04d}。".format(index, paragraph, index)
        store.ingest("合成资料 {}".format(index), text, "bench-{:04d}".format(index))
        total_chars += len(text)
    return {"documents": DOCUMENT_COUNT, "source_characters": total_chars,
            "chunks": sum(len(item["chunk_ids"]) for item in store.list_documents()),
            "queries": list(QUERIES), "queries_per_path": len(QUERIES) * REPEATS}


def legacy_bm25(query, evidence, limit=5):
    """v2's full-corpus scoring loop, sharing the corrected query anchor policy.

    This isolates retrieval cost, not a claim that old and new recall quality is
    identical. The benchmark deliberately avoids the v2 compound-word bug.
    """
    items = [item for item in evidence if item["text"].strip()]
    terms, anchors = set(retrieval.tokenize(query)), retrieval._anchor_terms(query)
    counts = [Counter(retrieval.tokenize(item["title"] + " " + item["title"] + " " + item["text"]))
              for item in items]
    lengths = [sum(item.values()) for item in counts]
    average = sum(lengths) / max(len(lengths), 1) or 1.0
    frequencies = Counter()
    for counter in counts:
        frequencies.update(counter.keys() & terms)
    ranked = []
    for item, counter, length in zip(items, counts, lengths):
        if anchors.intersection(counter):
            score = retrieval._bm25_score(terms, counter, length, average, frequencies, len(items))
            if score > 0:
                ranked.append(dict(item, score=round(score, 8)))
    ranked.sort(key=lambda item: (-item["score"], item["id"]))
    return ranked[:limit]


def legacy_search(store, query):
    """Retain v2's N+1 SELECT * documents and per-request corpus tokenization."""
    with store._connection() as connection:
        rows = connection.execute("""
            SELECT chunks.* FROM chunks JOIN documents ON chunks.document_id=documents.id
            WHERE documents.active=1
        """).fetchall()
        evidence = []
        for row in rows:
            document = connection.execute("SELECT * FROM documents WHERE id=?", (row["document_id"],)).fetchone()
            store.full_document_chars += len(document["text"])
            evidence.append(store._chunk_evidence(connection, row, document=document))
    return legacy_bm25(query, evidence)


def measure(store, action, query):
    """Trace only measured SELECTs; setup, statistics reads, and writes are out."""
    store.select_count = store.full_document_chars = 0
    calls = characters = 0
    tokenize = retrieval.tokenize

    def counted(text):
        nonlocal calls, characters
        calls += 1
        characters += len(text)
        return tokenize(text)

    with patch.object(retrieval, "tokenize", counted):
        began = perf_counter()
        result = action(query)
        elapsed = (perf_counter() - began) * 1000
    return result, {"elapsed_ms": round(elapsed, 3), "selects": store.select_count,
                    "tokenize_calls": calls, "tokenized_characters": characters,
                    "full_document_characters_read": store.full_document_chars}


def summarize(samples):
    elapsed = sorted(sample["elapsed_ms"] for sample in samples)
    return {"samples": len(samples), "median_ms": round(statistics.median(elapsed), 3),
            "p95_ms": elapsed[math.ceil(len(elapsed) * 0.95) - 1],
            "min_ms": elapsed[0], "max_ms": elapsed[-1],
            "selects_per_query": sorted(set(sample["selects"] for sample in samples)),
            "tokenize_calls_total": sum(sample["tokenize_calls"] for sample in samples),
            "tokenized_characters_total": sum(sample["tokenized_characters"] for sample in samples),
            "full_document_characters_read_total": sum(sample["full_document_characters_read"] for sample in samples),
            "raw_samples": samples}


def benchmark():
    with tempfile.TemporaryDirectory(prefix="zhiwiki-retrieval-bench-") as data_dir:
        legacy = MeasuredStore(data_dir)
        corpus = populate(legacy)
        samples = {"legacy_n_plus_one": [], "cached_cold": [], "cached_warm": []}
        warm = MeasuredStore(data_dir)
        warm.search(QUERIES[0])  # Establish the warm index outside measurement.
        equal_rankings = True
        cold_builds = 0
        for _ in range(REPEATS):
            for query in QUERIES:
                baseline, sample = measure(legacy, lambda value: legacy_search(legacy, value), query)
                samples["legacy_n_plus_one"].append(sample)
                cold = MeasuredStore(data_dir)
                result, sample = measure(cold, cold.search, query)
                samples["cached_cold"].append(sample)
                cold_builds += cold.cache_stats()["builds"]
                equal_rankings = equal_rankings and result == baseline
                result, sample = measure(warm, warm.search, query)
                samples["cached_warm"].append(sample)
                equal_rankings = equal_rankings and result == baseline
        if not equal_rankings:
            raise AssertionError("The measured paths produced different evidence or rankings")
        report = {"generated_at": datetime.now(timezone.utc).isoformat(),
                  "python": platform.python_version(), "sqlite": __import__("sqlite3").sqlite_version,
                  "platform": platform.platform(), "corpus": corpus,
                  "scope": "Single-process local synthetic microbenchmark; not production QPS or answer-quality evaluation.",
                  "baseline": "v2-equivalent N+1/full-document reads and corpus retokenization, with the shared corrected anchor filter",
                  "measurement": "12 samples per path; setup and warm-up excluded; SELECT trace and tokenize instrumentation enabled; p95 nearest-rank",
                  "equal_results": equal_rankings,
                  "paths": {name: summarize(values) for name, values in samples.items()},
                  "index_builds": {"legacy_per_query_retokenizations": len(samples["legacy_n_plus_one"]),
                                   "cold": cold_builds, "warm_including_one_setup_build": warm.cache_stats()["builds"]},
                  "warm_cache_stats": warm.cache_stats()}
        return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", default="reports/retrieval-benchmark.json")
    arguments = parser.parse_args()
    report = benchmark()
    output = Path(arguments.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "equal_results": report["equal_results"],
                      "paths": {key: {field: value[field] for field in ("median_ms", "p95_ms", "selects_per_query")}
                                for key, value in report["paths"].items()}}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
