"""Produce source-based scope candidates without launching long engine regressions."""

import argparse
from collections import Counter
import json
from pathlib import Path

from tools.check_opencypher import inventory
from tools.tck_profile import review_candidates
from tools.tck_ledger import freeze_ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reviewed", type=Path, help="Explicit reviewed source-bound candidates")
    parser.add_argument("--decision", help="Recorded architectural review decision identifier")
    args = parser.parse_args()
    report = inventory(args.checkout)
    if args.reviewed:
        if not args.decision:
            parser.error("--reviewed requires --decision")
        reviewed = json.loads(args.reviewed.read_text(encoding="utf-8"))
        review = freeze_ledger(report, reviewed, decision=args.decision)
    else:
        review = review_candidates(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.reviewed:
        print(json.dumps({"cases": review["case_count"], "profile_counts": review["profile_counts"]}))
    else:
        print(json.dumps({"candidates": len(review["cases"]), "rules": dict(Counter(
            rule for case in review["cases"] for rule in {e["rule"] for e in case["evidence"]}))}))


if __name__ == "__main__":
    main()
