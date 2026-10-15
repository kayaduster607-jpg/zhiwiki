"""Validate and aggregate a human-scored package. Never call a judge model."""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zhiwiki.benchmarks import read_json, score_reviews, write_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="Validate same-run answer hashes and aggregate human 0–4 ratings")
    parser.add_argument("--report", required=True)
    parser.add_argument("--reviews", required=True)
    parser.add_argument("--mapping", required=True)
    parser.add_argument("--output", default="reports/benchmark-human-scores.json")
    args = parser.parse_args(argv)
    try:
        result = score_reviews(read_json(args.report), read_json(args.reviews), read_json(args.mapping))
        write_json(args.output, result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError) as error:
        print("Scoring failed: " + str(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
