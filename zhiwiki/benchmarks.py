"""Reproducible local evaluation with separate retrieval and human answer scores.

No evaluator or judge model is used. Live generation requires an explicit flag;
reference answers and qrels are never ingested into the knowledge store.
"""

import hashlib
import json
import math
import platform
import random
import re
import statistics
import sys
import tempfile
import time
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .config import reject_environment_path
from .engine import AgentEngine
from .store import KnowledgeStore


BENCHMARK_DIR = Path(__file__).resolve().parent.parent / "examples" / "benchmark"
DIMENSIONS = ("correctness", "groundedness", "completeness")
STRATEGIES = ("baseline", "agentic")


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def answer_hash(answer):
    """Hash the exact answer, including whitespace, for review/run association."""
    if not isinstance(answer, str):
        raise ValueError("Answer must be text")
    return hashlib.sha256(answer.encode("utf-8")).hexdigest()


def _hash(value):
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _safe_path(path):
    path = Path(path)
    reject_environment_path(path)
    resolved = path.resolve()
    reject_environment_path(resolved)
    return resolved


def read_json(path):
    """Read explicitly selected JSON only; reject environment paths before I/O."""
    path = _safe_path(path)
    if path.suffix.lower() != ".json" or path.stat().st_size > 20_000_000:
        raise ValueError("Expected a JSON file below 20 MB")
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path, payload):
    path = _safe_path(path)
    if path.suffix.lower() != ".json":
        raise ValueError("Report output must be JSON")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _file_record(root, relative):
    path = _safe_path(root / relative)
    path.relative_to(root)
    if path.stat().st_size > 2_000_000:
        raise ValueError("Benchmark fixture is too large")
    data = path.read_bytes()
    return data, {"file": relative, "sha256": hashlib.sha256(data).hexdigest()}


def load_benchmark(split="dev", root=None):
    """Load a fixed corpus manifest and separate labelled question files."""
    if split not in ("dev", "holdout", "all"):
        raise ValueError("split must be dev, holdout or all")
    root = _safe_path(root or BENCHMARK_DIR)
    manifest = read_json(root / "manifest.json")
    if manifest.get("schema_version") != 1 or manifest.get("fictional") is not True:
        raise ValueError("Benchmark must be versioned and explicitly fictional")
    documents, corpus_files = _load_corpus(root, manifest)
    cases, question_files = _load_questions(root, manifest, split)
    _validate_cases(cases, {document["source_key"] for document in documents})
    _, manifest_file = _file_record(root, "manifest.json")
    dataset = {"dataset_id": manifest["dataset_id"], "split": split, "fictional": True,
               "authoring": manifest["authoring"], "document_count": len(documents), "case_count": len(cases),
               "corpus_sha256": _hash(corpus_files), "questions_sha256": _hash(question_files),
               "manifest_sha256": manifest_file["sha256"], "corpus_files": corpus_files,
               "question_files": question_files}
    return {"documents": documents, "cases": cases, "dataset": dataset}


def _load_corpus(root, manifest):
    entries = manifest.get("documents")
    if not isinstance(entries, list) or not 1 <= len(entries) <= 40:
        raise ValueError("Invalid corpus manifest")
    documents, records, seen = [], [], set()
    for entry in entries:
        relative = entry.get("file", "")
        source_key = entry.get("source_key", "")
        if not re.fullmatch(r"corpus/[a-z0-9-]+\.md", relative):
            raise ValueError("Only declared corpus Markdown may be ingested")
        if not re.fullmatch(r"benchmark:[a-z0-9-]+", source_key) or source_key in seen:
            raise ValueError("Invalid or duplicate corpus source key")
        data, record = _file_record(root, relative)
        text = data.decode("utf-8")
        if not text.startswith("# " + entry["title"] + "\n"):
            raise ValueError("Corpus title does not match its manifest")
        seen.add(source_key)
        documents.append({"source_key": source_key, "title": entry["title"], "text": text})
        records.append(dict(record, source_key=source_key))
    return documents, records


def _load_questions(root, manifest, split):
    cases, records = [], []
    for name in (("dev", "holdout") if split == "all" else (split,)):
        relative = manifest["splits"][name]
        if relative != "questions/" + name + ".json":
            raise ValueError("Question split must use its fixed separate path")
        data, record = _file_record(root, relative)
        rows = json.loads(data.decode("utf-8"))
        if not isinstance(rows, list):
            raise ValueError("Question split must be a list")
        cases.extend(dict(row, split=name) for row in rows)
        records.append(record)
    return cases, records


def _validate_cases(cases, source_keys):
    seen = set()
    for case in cases:
        case_id = case.get("id")
        if not isinstance(case_id, str) or case_id in seen:
            raise ValueError("Question IDs must be unique")
        seen.add(case_id)
        for key in ("question", "reference_answer"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError("Question/reference answer must be nonempty")
        if not isinstance(case.get("key_points"), list) or not case["key_points"]:
            raise ValueError("Reference key points are required")
        qrels = case.get("qrels")
        if not isinstance(qrels, dict) or set(qrels) - source_keys:
            raise ValueError("qrels must refer to known corpus sources")
        if any(type(grade) is not int or not 0 <= grade <= 2 for grade in qrels.values()):
            raise ValueError("qrels grades must be integers 0–2")


def retrieval_metrics(ranked_sources, qrels, k):
    """Document-level metrics after deduplication; empty relevance => null."""
    if type(k) is not int or not 1 <= k <= 20:
        raise ValueError("k must be an integer from 1 to 20")
    relevant = {source for source, grade in qrels.items() if grade > 0}
    ranking = list(dict.fromkeys(ranked_sources))[:k]
    if not relevant:
        return {"recall_at_k": None, "mrr_at_k": None, "ndcg_at_k": None}
    recall = len(set(ranking) & relevant) / len(relevant)
    reciprocal = next((1.0 / rank for rank, source in enumerate(ranking, 1) if source in relevant), 0.0)
    dcg = sum((2 ** qrels.get(source, 0) - 1) / math.log2(rank + 1)
              for rank, source in enumerate(ranking, 1))
    ideal = sum((2 ** grade - 1) / math.log2(rank + 1)
                for rank, grade in enumerate(sorted(qrels.values(), reverse=True)[:k], 1))
    return {"recall_at_k": recall, "mrr_at_k": reciprocal, "ndcg_at_k": dcg / ideal}


def _retrieve_case(store, case, k):
    started = time.perf_counter()
    sources, seen, failure = [], set(), None
    try:
        # Candidate cap is explicit. Repeated chunks from one document do not
        # earn repeated relevance credit in these document-level qrels.
        for evidence in store.search(case["question"], kind="document", limit=20):
            if evidence["source_key"] not in seen:
                seen.add(evidence["source_key"])
                sources.append({"source_key": evidence["source_key"], "rank": len(sources) + 1,
                                "score": evidence["score"], "evidence_id": evidence["id"]})
            if len(sources) == k:
                break
    except Exception as error:
        failure = {"type": type(error).__name__, "message": "Retrieval failed; exception body omitted"}
    metrics = retrieval_metrics([item["source_key"] for item in sources], case["qrels"], k)
    if failure:
        metrics = {key: None for key in metrics}
    return {"case_id": case["id"], "split": case["split"], "question": case["question"],
            "qrels": case["qrels"], "ranked_sources": sources, "metrics": metrics,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "failure": failure,
            "expect_abstain": bool(case.get("expect_abstain")), "empty_result": not sources}


def _settings_snapshot(settings, requested_provider):
    """Persist budgets and model identity, never credentials, URL secrets or paths."""
    endpoint = urlsplit(settings.base_url)
    snapshot = {name: getattr(settings, name) for name in (
        "provider", "model", "max_rounds", "max_tool_calls", "timeout_seconds",
        "request_timeout_seconds", "max_context_chars", "max_total_tokens", "max_output_tokens")}
    snapshot.update(requested_provider=requested_provider, endpoint_scheme=endpoint.scheme or None,
                    endpoint_host=endpoint.hostname, endpoint_port=endpoint.port,
                    sampling_parameters="not_overridden_by_adapter; provider defaults apply")
    return snapshot


def _implementation_snapshot():
    files = []
    for name in ("benchmarks.py", "engine.py", "provider.py", "retrieval.py", "chunking.py", "store.py"):
        path = Path(__file__).resolve().parent / name
        if path.exists():
            files.append({"file": "zhiwiki/" + name, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
    return {"python": sys.version.split()[0], "platform": platform.system(), "files": files}


def _answer_trial(store, settings, case, strategy, sequence):
    started = time.perf_counter()
    failure = None
    try:
        run = AgentEngine(store, settings).run(case["question"], strategy=strategy)
    except Exception as error:
        failure = {"type": type(error).__name__, "message": "Generation failed; exception body omitted"}
        run = {"id": None, "mode": settings.provider, "status": "execution_error", "answer": "",
               "citations": [], "events": [], "usage": {}, "stop_reason": "execution_error"}
    usage = run.get("usage", {})
    actual = None
    if usage.get("token_usage_known") is True and all(type(usage.get(name)) is int and usage[name] >= 0
                                                    for name in ("prompt_tokens", "completion_tokens", "total_tokens")):
        actual = {name: usage.get(name) for name in ("prompt_tokens", "completion_tokens", "total_tokens")}
    return {"trial_id": "trial_" + uuid.uuid4().hex, "engine_run_id": run.get("id"),
            "case_id": case["id"], "split": case["split"], "sequence": sequence, "strategy": strategy,
            "mode": run["mode"], "status": run["status"], "stop_reason": run.get("stop_reason"),
            "answer": run["answer"], "answer_sha256": answer_hash(run["answer"]),
            "citations": run.get("citations", []), "events": run.get("events", []),
            "usage": usage, "actual_token_usage": actual,
            "elapsed_ms": round((time.perf_counter() - started) * 1000, 3), "failure": failure,
            "human_review": {dimension: None for dimension in DIMENSIONS}}


def run_benchmark(settings, split="dev", k=5, seed=1729, live=False, retrieval_only=False, root=None):
    """Run in a disposable corpus-only database. No flag => offline even if configured live."""
    if type(seed) is not int or type(k) is not int or not 1 <= k <= 20:
        raise ValueError("seed must be an integer; k must be 1–20")
    if live and (retrieval_only or settings.provider != "openai"):
        raise ValueError("--live requires provider=openai and cannot combine with --retrieval-only")
    benchmark = load_benchmark(split, root)
    effective = settings if live else replace(settings, provider="offline")
    settings_snapshot = _settings_snapshot(effective, settings.provider)
    retrieval, runs = [], []
    with tempfile.TemporaryDirectory(prefix="zhiwiki_benchmark_") as directory:
        isolated = replace(effective, data_dir=directory)
        store = KnowledgeStore(directory)
        for document in benchmark["documents"]:
            store.ingest(**document)
        for case_index, case in enumerate(benchmark["cases"]):
            retrieval.append(_retrieve_case(store, case, k))
            if not retrieval_only:
                # AB/BA order alternates by case. Each case remains adjacent;
                # both arms share frozen settings and the same immutable corpus.
                order = STRATEGIES if case_index % 2 == 0 else tuple(reversed(STRATEGIES))
                for strategy in order:
                    runs.append(_answer_trial(store, isolated, case, strategy, len(runs)))
    report = {"schema_version": 1, "benchmark_run_id": "benchmark_" + uuid.uuid4().hex,
              "generated_at": datetime.now(timezone.utc).isoformat(),
              "execution_mode": "retrieval_only" if retrieval_only else ("live" if live else "offline"),
              "live_authorized": bool(live), "dataset": benchmark["dataset"],
              "settings_snapshot": settings_snapshot,
              "implementation": _implementation_snapshot(),
              "protocol": {"k": k, "candidate_chunk_limit": 20, "qrels_unit": "unique_document",
                           "unlisted_qrel_grade": 0, "pair_order": "adjacent AB/BA alternating",
                           "anonymization_seed": seed, "rating_dimensions": list(DIMENSIONS),
                           "rating_scale": [0, 1, 2, 3, 4], "judge_model_used": False},
              "retrieval": retrieval, "runs": runs,
              "references": [{key: case[key] for key in ("id", "split", "question", "reference_answer", "key_points", "qrels")}
                             for case in benchmark["cases"]],
              "limitations": ["Original synthetic data; held-out split is not a third-party blind test",
                              "Document relevance metrics are separate from answer quality",
                              "No automatic correctness/groundedness/completeness score",
                              "Offline paired outputs test plumbing, not model improvement",
                              "One run per arm; no statistical significance or production cost claims"]}
    report["summary"] = _report_summary(report)
    report["summary_by_split"] = {
        name: _report_summary({"retrieval": [row for row in retrieval if row["split"] == name],
                               "runs": [row for row in runs if row["split"] == name]})
        for name in sorted({case["split"] for case in benchmark["cases"]})}
    return report


def _mean(values):
    values = [value for value in values if value is not None]
    return statistics.mean(values) if values else None


def _report_summary(report):
    retrieval = report["retrieval"]
    result = {"retrieval": {key: _mean(row["metrics"][key] for row in retrieval)
                            for key in ("recall_at_k", "mrr_at_k", "ndcg_at_k")},
              "retrieval_cases": len(retrieval),
              "relevance_scored_cases": sum(row["metrics"]["recall_at_k"] is not None for row in retrieval),
              "retrieval_failures": sum(row["failure"] is not None for row in retrieval),
              "empty_result_expected_cases": sum(row["expect_abstain"] for row in retrieval),
              "empty_result_expected_passes": sum(row["expect_abstain"] and row["empty_result"] and not row["failure"] for row in retrieval),
              "answers": {}}
    for strategy in STRATEGIES:
        rows = [row for row in report["runs"] if row["strategy"] == strategy]
        statuses = {}
        for row in rows:
            statuses[row["status"]] = statuses.get(row["status"], 0) + 1
        known = all(row["actual_token_usage"] is not None for row in rows)
        result["answers"][strategy] = {
            "runs": len(rows), "statuses": statuses, "mean_elapsed_ms": _mean(row["elapsed_ms"] for row in rows),
            "failed_runs": sum(row["status"] not in ("completed", "insufficient_evidence") for row in rows),
            "model_calls": sum(row["usage"].get("model_calls", 0) for row in rows),
            "actual_total_tokens": sum(row["actual_token_usage"]["total_tokens"] for row in rows) if rows and known else None,
            "human_review": {dimension: None for dimension in DIMENSIONS}}
    return result


def make_review_package(report, seed=None):
    """Hide arm, timing, usage and trace. Exact output text may still reveal style."""
    seed = report["protocol"]["anonymization_seed"] if seed is None else seed
    references = {case["id"]: case for case in report["references"]}
    ordered = list(report["runs"])
    random.Random(seed).shuffle(ordered)
    reviews, mappings = [], []
    for index, trial in enumerate(ordered, 1):
        review_id = "review_{:04d}".format(index)
        reference = references[trial["case_id"]]
        reviews.append({"review_id": review_id, "answer_sha256": trial["answer_sha256"],
                        "question": reference["question"], "answer": trial["answer"],
                        "reference_answer": reference["reference_answer"], "key_points": reference["key_points"],
                        "cited_evidence": [{key: item[key] for key in ("title", "text", "source_key", "version", "locator")}
                                           for item in trial["citations"]],
                        "ratings": {dimension: None for dimension in DIMENSIONS}, "comment": ""})
        mappings.append({"review_id": review_id, "trial_id": trial["trial_id"], "case_id": trial["case_id"],
                         "strategy": trial["strategy"], "answer_sha256": trial["answer_sha256"]})
    common = {"schema_version": 1, "benchmark_run_id": report["benchmark_run_id"], "seed": seed}
    package = dict(common, instructions="Human ratings only: integer 0–4, null means not reviewed. Keep IDs and answer hashes unchanged.", rows=reviews)
    mapping = dict(common, warning="Private arm mapping; do not share with raters before ratings are frozen.", rows=mappings)
    return package, mapping


def save_benchmark_artifacts(report, output):
    """Save complete report plus separate human review and private mapping files."""
    output = _safe_path(output)
    review_path = output.with_name(output.stem + ".review.json")
    mapping_path = output.with_name(output.stem + ".mapping.json")
    package, mapping = make_review_package(report)
    write_json(output, report)
    write_json(review_path, package)
    write_json(mapping_path, mapping)
    return {"report": str(output), "reviews": str(review_path), "mapping": str(mapping_path)}


def _unique_rows(rows, field, label):
    if not isinstance(rows, list):
        raise ValueError(label + " must contain a row list")
    result = {}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get(field), str) or row[field] in result:
            raise ValueError("Duplicate or invalid " + label + " ID")
        result[row[field]] = row
    return result


def _validate_mapping(report, mapping):
    trials = _unique_rows(report["runs"], "trial_id", "trial")
    entries = _unique_rows(mapping["rows"], "review_id", "mapping")
    seen_trials = set()
    for entry in entries.values():
        trial = trials.get(entry.get("trial_id"))
        if trial is None or entry["trial_id"] in seen_trials:
            raise ValueError("Unknown or duplicate mapped trial")
        seen_trials.add(entry["trial_id"])
        for key in ("case_id", "strategy", "answer_sha256"):
            if entry.get(key) != trial[key]:
                raise ValueError("Mapping does not match this report")
        if answer_hash(trial["answer"]) != trial["answer_sha256"]:
            raise ValueError("Report answer hash is invalid")
    if set(trials) != seen_trials:
        raise ValueError("Mapping must cover every report trial exactly once")
    return entries


def _ratings(row):
    values = row.get("ratings")
    if not isinstance(values, dict) or set(values) != set(DIMENSIONS):
        raise ValueError("Ratings must contain exactly the three human dimensions")
    for value in values.values():
        if value is not None and (type(value) is not int or not 0 <= value <= 4):
            raise ValueError("Human scores must be integers 0–4 or null")
    return values


def score_reviews(report, package, mapping):
    """Score only supplied human ratings; reject cross-run, stale or duplicate rows."""
    run_id = report.get("benchmark_run_id")
    if not isinstance(run_id, str) or package.get("benchmark_run_id") != run_id or mapping.get("benchmark_run_id") != run_id:
        raise ValueError("Review package, mapping and report must have the same benchmark_run_id")
    entries = _validate_mapping(report, mapping)
    reviews = _unique_rows(package.get("rows"), "review_id", "review")
    evaluated = []
    for review_id, row in reviews.items():
        entry = entries.get(review_id)
        if entry is None:
            raise ValueError("Unknown review ID")
        if row.get("answer_sha256") != entry["answer_sha256"] or answer_hash(row.get("answer")) != entry["answer_sha256"]:
            raise ValueError("Review answer/hash does not match this run")
        evaluated.append(dict(entry, ratings=_ratings(row)))
    summaries = {}
    for strategy in STRATEGIES:
        rows = [row for row in evaluated if row["strategy"] == strategy]
        summaries[strategy] = {dimension: {"reviewed": sum(row["ratings"][dimension] is not None for row in rows),
                                          "mean": _mean(row["ratings"][dimension] for row in rows)}
                               for dimension in DIMENSIONS}
    comparisons = _paired_scores(evaluated)
    return {"schema_version": 1, "benchmark_run_id": run_id, "review_rows_supplied": len(evaluated),
            "fully_reviewed_rows": sum(all(row["ratings"][dimension] is not None for dimension in DIMENSIONS) for row in evaluated),
            "summary": summaries, "matched_pairs": comparisons,
            "limitations": ["Only human-entered scores are summarized; null is never zero",
                            "Matched comparisons require both arms scored on the same dimension",
                            "Small synthetic internal sample; no significance or model superiority claim"]}


def _paired_scores(rows):
    by_case = {}
    for row in rows:
        key = (row["case_id"], row["strategy"])
        if key in by_case:
            raise ValueError("Duplicate case/strategy trial in review mapping")
        by_case[key] = row["ratings"]
    result = {}
    for dimension in DIMENSIONS:
        pairs = []
        for case_id in sorted({row["case_id"] for row in rows}):
            baseline = by_case.get((case_id, "baseline"), {}).get(dimension)
            agentic = by_case.get((case_id, "agentic"), {}).get(dimension)
            if baseline is not None and agentic is not None:
                pairs.append({"case_id": case_id, "baseline": baseline, "agentic": agentic, "delta": agentic - baseline})
        result[dimension] = {"pairs": len(pairs), "baseline_mean": _mean(row["baseline"] for row in pairs),
                             "agentic_mean": _mean(row["agentic"] for row in pairs),
                             "mean_delta": _mean(row["delta"] for row in pairs),
                             "agentic_higher": sum(row["delta"] > 0 for row in pairs),
                             "ties": sum(row["delta"] == 0 for row in pairs),
                             "agentic_lower": sum(row["delta"] < 0 for row in pairs), "rows": pairs}
    return result
