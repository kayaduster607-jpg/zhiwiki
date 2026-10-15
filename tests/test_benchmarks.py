"""Independent benchmark protocol tests; every generation test stays offline."""

import copy
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from zhiwiki.benchmarks import (DIMENSIONS, load_benchmark, make_review_package, read_json,
                                retrieval_metrics, run_benchmark, save_benchmark_artifacts, score_reviews)
from zhiwiki.config import Settings


class BenchmarkTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = run_benchmark(Settings(), split="dev", seed=41)

    def test_second_domain_has_eight_documents_and_disjoint_fixed_splits(self):
        dev, holdout = load_benchmark("dev"), load_benchmark("holdout")
        self.assertEqual((len(dev["documents"]), len(dev["cases"]), len(holdout["cases"])), (8, 12, 12))
        self.assertFalse({case["id"] for case in dev["cases"]} & {case["id"] for case in holdout["cases"]})
        self.assertEqual(dev["dataset"]["corpus_sha256"], holdout["dataset"]["corpus_sha256"])
        self.assertNotEqual(dev["dataset"]["questions_sha256"], holdout["dataset"]["questions_sha256"])
        self.assertTrue(all(document["source_key"].startswith("benchmark:") for document in dev["documents"]))
        self.assertFalse(any("ZEPHYR_X9" in document["text"] for document in dev["documents"]))
        self.assertTrue(all(record["file"].startswith("corpus/") for record in dev["dataset"]["corpus_files"]))

    def test_metrics_use_unique_document_ranks_and_graded_ndcg(self):
        metrics = retrieval_metrics(["b", "b", "a", "c"], {"a": 2, "b": 1}, 2)
        self.assertEqual(metrics["recall_at_k"], 1)
        self.assertEqual(metrics["mrr_at_k"], 1)
        expected = (1 + 3 / math.log2(3)) / (3 + 1 / math.log2(3))
        self.assertAlmostEqual(metrics["ndcg_at_k"], expected)
        self.assertEqual(retrieval_metrics(["x"], {"a": 2}, 1)["recall_at_k"], 0)
        self.assertTrue(all(value is None for value in retrieval_metrics([], {}, 5).values()))

    def test_paired_offline_order_and_unreviewed_dimensions(self):
        report = self.report
        self.assertEqual(report["execution_mode"], "offline")
        self.assertEqual(len(report["runs"]), 24)
        for index in range(0, 24, 2):
            first, second = report["runs"][index:index + 2]
            self.assertEqual(first["case_id"], second["case_id"])
            expected = ("baseline", "agentic") if index // 2 % 2 == 0 else ("agentic", "baseline")
            self.assertEqual((first["strategy"], second["strategy"]), expected)
            self.assertEqual(first["usage"]["model_calls"], 0)
            self.assertTrue(all(value is None for value in first["human_review"].values()))
        self.assertFalse(report["protocol"]["judge_model_used"])
        self.assertEqual(report["summary"]["relevance_scored_cases"], 11)
        self.assertEqual(report["summary_by_split"]["dev"]["retrieval_cases"], 12)

    def test_configured_openai_is_still_offline_without_explicit_live(self):
        settings = Settings(provider="openai", model="fixture-model", base_url="https://user:FAKE_SECRET@fixture.invalid/v1?token=FAKE_QUERY")
        with patch("zhiwiki.engine.OpenAIProvider", side_effect=AssertionError("must not create live provider")):
            report = run_benchmark(settings, split="dev")
        self.assertEqual(report["settings_snapshot"]["provider"], "offline")
        self.assertEqual(report["settings_snapshot"]["requested_provider"], "openai")
        serialized = json.dumps(report)
        self.assertNotIn("FAKE_SECRET", serialized)
        self.assertNotIn("FAKE_QUERY", serialized)
        self.assertNotIn("api_key_env", serialized)

    def test_retrieval_only_never_constructs_engine_and_saves_empty_review_pack(self):
        with patch("zhiwiki.benchmarks.AgentEngine", side_effect=AssertionError("retrieval only")):
            report = run_benchmark(Settings(), split="holdout", retrieval_only=True)
        self.assertEqual(report["runs"], [])
        self.assertEqual(len(report["retrieval"]), 12)
        with tempfile.TemporaryDirectory() as directory:
            paths = save_benchmark_artifacts(report, Path(directory) / "retrieval.json")
            self.assertEqual(read_json(paths["reviews"])["rows"], [])
            self.assertEqual(read_json(paths["mapping"])["rows"], [])
            self.assertEqual(read_json(paths["report"])["benchmark_run_id"], report["benchmark_run_id"])

    def test_explicit_live_requires_openai_and_cannot_mix_retrieval_only(self):
        with patch("zhiwiki.benchmarks.AgentEngine", side_effect=AssertionError("must fail before dispatch")):
            with self.assertRaises(ValueError):
                run_benchmark(Settings(), live=True)
            with self.assertRaises(ValueError):
                run_benchmark(Settings(provider="openai", model="fixture", base_url="https://fixture.invalid/v1"),
                              live=True, retrieval_only=True)

    def test_anonymous_order_is_reproducible_and_arm_information_is_separate(self):
        package, mapping = make_review_package(self.report)
        again, again_mapping = make_review_package(self.report)
        self.assertEqual(package, again)
        self.assertEqual(mapping, again_mapping)
        self.assertNotEqual(mapping, make_review_package(self.report, seed=99)[1])
        serialized = json.dumps(package)
        for field in ('"strategy"', '"usage"', '"elapsed_ms"', '"events"', '"trial_id"'):
            self.assertNotIn(field, serialized)
        self.assertEqual(len(package["rows"]), 24)
        self.assertIn("strategy", mapping["rows"][0])

    def test_only_reviewed_scores_enter_summary_and_matched_comparison(self):
        package, mapping = make_review_package(self.report)
        empty = score_reviews(self.report, package, mapping)
        self.assertIsNone(empty["summary"]["baseline"]["correctness"]["mean"])
        self.assertEqual(empty["matched_pairs"]["correctness"]["pairs"], 0)
        mapped = {entry["review_id"]: entry for entry in mapping["rows"]}
        cases = sorted({entry["case_id"] for entry in mapping["rows"]})
        for row in package["rows"]:
            entry = mapped[row["review_id"]]
            if entry["case_id"] == cases[0]:
                value = 2 if entry["strategy"] == "baseline" else 4
                row["ratings"] = {dimension: value for dimension in DIMENSIONS}
            elif entry["case_id"] == cases[1] and entry["strategy"] == "baseline":
                row["ratings"]["correctness"] = 1
        scored = score_reviews(self.report, package, mapping)
        self.assertEqual(scored["fully_reviewed_rows"], 2)
        self.assertEqual(scored["summary"]["baseline"]["correctness"]["mean"], 1.5)
        self.assertEqual(scored["matched_pairs"]["correctness"]["pairs"], 1)
        self.assertEqual(scored["matched_pairs"]["correctness"]["mean_delta"], 2)

    def test_stale_unknown_duplicate_or_invalid_review_rows_are_rejected(self):
        original, mapping = make_review_package(self.report)
        variants = []
        stale = copy.deepcopy(original)
        stale["benchmark_run_id"] = "benchmark_previous"
        variants.append(stale)
        changed = copy.deepcopy(original)
        changed["rows"][0]["answer"] += " altered"
        variants.append(changed)
        changed_hash = copy.deepcopy(original)
        changed_hash["rows"][0]["answer_sha256"] = "0" * 64
        variants.append(changed_hash)
        unknown = copy.deepcopy(original)
        unknown["rows"][0]["review_id"] = "unknown"
        variants.append(unknown)
        duplicate = copy.deepcopy(original)
        duplicate["rows"].append(copy.deepcopy(duplicate["rows"][0]))
        variants.append(duplicate)
        for value in (5, -1, True, 1.5, "4"):
            invalid = copy.deepcopy(original)
            invalid["rows"][0]["ratings"]["correctness"] = value
            variants.append(invalid)
        for index, package in enumerate(variants):
            with self.subTest(index=index), self.assertRaises(ValueError):
                score_reviews(self.report, package, mapping)
        altered_mapping = copy.deepcopy(mapping)
        altered_mapping["rows"][0]["trial_id"] = "trial_unknown"
        with self.assertRaises(ValueError):
            score_reviews(self.report, original, altered_mapping)

    def test_generation_failures_are_preserved_without_exception_body(self):
        with patch("zhiwiki.benchmarks.AgentEngine", side_effect=RuntimeError("FAKE_RESPONSE_SECRET")):
            report = run_benchmark(Settings(), split="dev")
        self.assertEqual(len(report["runs"]), 24)
        self.assertTrue(all(row["failure"]["type"] == "RuntimeError" for row in report["runs"]))
        self.assertEqual(report["summary"]["answers"]["baseline"]["failed_runs"], 12)
        self.assertNotIn("FAKE_RESPONSE_SECRET", json.dumps(report))
        self.assertTrue(all(row["actual_token_usage"] is None for row in report["runs"]))


if __name__ == "__main__":
    unittest.main()
