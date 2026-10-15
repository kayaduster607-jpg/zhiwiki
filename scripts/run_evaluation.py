"""Run the original second-domain benchmark without installing dependencies."""

import argparse
import json
import sys
from pathlib import Path

# Allow invocation by absolute script path without installing the local package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhiwiki.benchmarks import run_benchmark, save_benchmark_artifacts
from zhiwiki.config import load_settings


def main(argv=None):
    parser = argparse.ArgumentParser(description="Second-domain retrieval and human-review benchmark; offline by default")
    parser.add_argument("--config", help="Explicit JSON settings; never an environment file")
    parser.add_argument("--split", choices=("dev", "holdout", "all"), default="dev")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--seed", type=int, default=1729)
    parser.add_argument("--output", default="reports/benchmark-run.json")
    execution = parser.add_mutually_exclusive_group()
    execution.add_argument("--retrieval-only", action="store_true", help="Only raw-document retrieval; no answer generation")
    execution.add_argument("--live", action="store_true", help="Explicitly authorize calls to configured provider=openai")
    args = parser.parse_args(argv)
    try:
        report = run_benchmark(load_settings(args.config), split=args.split, k=args.top_k, seed=args.seed,
                               live=args.live, retrieval_only=args.retrieval_only)
        paths = save_benchmark_artifacts(report, args.output)
        print(json.dumps({"benchmark_run_id": report["benchmark_run_id"], "mode": report["execution_mode"],
                          "summary": report["summary"], "artifacts": paths}, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as error:
        print("Evaluation failed: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
