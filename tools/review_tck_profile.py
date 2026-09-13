"""Produce source-based scope candidates without launching long engine regressions."""

import argparse
from collections import Counter
import json
from pathlib import Path

from tools.check_opencypher import inventory
from tools.tck_profile import review_candidates
from tools.tck_ledger import expand_flexible_ledger, expand_multilabel_ledger, freeze_ledger


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--reviewed", type=Path, help="Explicit reviewed source-bound candidates")
    mode.add_argument("--expand-ledger", type=Path, help="Preserve frozen V1 and generate authorized flexible-model V2")
    mode.add_argument("--expand-multilabel-ledger", type=Path, help="Preserve frozen V2 and generate authorized V3")
    parser.add_argument("--predecessor-ledger", type=Path, help="Frozen V1 ancestor for V3 generation")
    parser.add_argument("--nested-storage-decision", help="Explicit Set1 #0010 nested-storage decision")
    parser.add_argument("--decision", help="Recorded architectural review decision identifier")
    args = parser.parse_args()
    if args.expand_ledger and not args.decision:
        parser.error("--expand-ledger requires --decision")
    if args.expand_multilabel_ledger:
        if not (args.decision and args.predecessor_ledger and args.nested_storage_decision):
            parser.error("V3 requires --decision, --predecessor-ledger and --nested-storage-decision")
    elif args.predecessor_ledger or args.nested_storage_decision:
        parser.error("V3 arguments require --expand-multilabel-ledger")
    if args.output.exists():
        parser.error("Refusing to overwrite an existing review or ledger")
    report = inventory(args.checkout)
    if args.expand_multilabel_ledger:
        previous = json.loads(args.expand_multilabel_ledger.read_text(encoding="utf-8"))
        ancestor = json.loads(args.predecessor_ledger.read_text(encoding="utf-8"))
        review = expand_multilabel_ledger(report, previous, ancestor=ancestor, decision=args.decision,
                                         nested_storage_decision=args.nested_storage_decision)
    elif args.expand_ledger:
        previous = json.loads(args.expand_ledger.read_text(encoding="utf-8"))
        review = expand_flexible_ledger(report, previous, decision=args.decision)
    elif args.reviewed:
        if not args.decision:
            parser.error("--reviewed requires --decision")
        reviewed = json.loads(args.reviewed.read_text(encoding="utf-8"))
        review = freeze_ledger(report, reviewed, decision=args.decision)
    else:
        review = review_candidates(report)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.reviewed or args.expand_ledger or args.expand_multilabel_ledger:
        print(json.dumps({"cases": review["case_count"], "profile_counts": review["profile_counts"]}))
    else:
        print(json.dumps({"candidates": len(review["cases"]), "rules": dict(Counter(
            rule for case in review["cases"] for rule in {e["rule"] for e in case["evidence"]}))}))


if __name__ == "__main__":
    main()
