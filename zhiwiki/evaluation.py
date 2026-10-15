"""隔离的来源命中回归，明确区分来源覆盖与答案语义正确性。"""

import json
import math
import statistics
import tempfile
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from .bootstrap import seed_examples
from .engine import AgentEngine
from .store import KnowledgeStore


def _case_result(case, run):
    expected = set(case["expected_sources"])
    actual = {item["source_key"] for item in run["citations"]}
    coverage = len(expected & actual) / len(expected) if expected else None
    return {"case_id": case["id"], "question": case["question"], "strategy": run["strategy"],
            "mode": run["mode"], "status": run["status"], "answer": run["answer"],
            "expected_sources": sorted(expected), "cited_sources": sorted(actual),
            "source_coverage": coverage, "expect_abstain": case.get("expect_abstain", False),
            "abstained": run["status"] == "insufficient_evidence", "elapsed_ms": run["elapsed_ms"],
            "usage": run["usage"], "manual_answer_checklist": case["answer_points"],
            "answer_correctness": "not_automatically_scored"}


def _summary(rows):
    coverage = [row["source_coverage"] for row in rows if row["source_coverage"] is not None]
    latencies = sorted(row["elapsed_ms"] for row in rows)
    # 最近秩 P95；小样例集仅用于回归，不当作负载测试。
    rank = max(0, math.ceil(len(latencies) * 0.95) - 1)
    return {"cases": len(rows), "mean_source_coverage": statistics.mean(coverage),
            "all_expected_sources_cases": sum(row["source_coverage"] == 1 for row in rows),
            "expected_abstentions_passed": sum(row["expect_abstain"] and row["abstained"] for row in rows),
            "p95_elapsed_ms": latencies[rank],
            "model_calls": sum(row["usage"]["model_calls"] for row in rows),
            "tool_calls": sum(row["usage"]["tool_calls"] for row in rows),
            "token_usage_known": all(row["usage"]["token_usage_known"] for row in rows),
            "total_tokens_reported_or_estimated": sum(row["usage"]["total_tokens"] for row in rows)}


def evaluate(settings):
    """固定样例在临时库执行；不读取或改变用户实际工作区知识。"""
    cases_path = Path(__file__).resolve().parent.parent / "examples" / "evaluation.json"
    cases = json.loads(cases_path.read_text(encoding="utf-8"))
    rows = []
    with tempfile.TemporaryDirectory(prefix="zhiwiki_eval_") as directory:
        isolated = replace(settings, data_dir=directory)
        store = KnowledgeStore(directory)
        seed_examples(store)
        for strategy in ("baseline", "agentic"):
            for case in cases:
                run = AgentEngine(store, isolated).run(case["question"], strategy=strategy)
                rows.append(_case_result(case, run))
    return {"generated_at": datetime.now(timezone.utc).isoformat(), "mode": settings.provider,
            "model": settings.model or None, "dataset": "{} fictional cases; {} paired runs".format(len(cases), len(rows)),
            "limitations": ["来源覆盖率不等于答案正确率", "离线两种策略均为确定性检索摘录，不测量 Agent 推理收益",
                            "未做独立盲测、模型效果提升、并发或真实业务成本结论", "运行时间来自本地样例回归，不代表生产 P95"],
            "summary": {strategy: _summary([row for row in rows if row["strategy"] == strategy])
                        for strategy in ("baseline", "agentic")}, "cases": rows}
