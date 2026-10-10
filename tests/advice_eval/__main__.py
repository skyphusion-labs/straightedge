"""CLI for the advice-eval harness.

    PYTHONPATH=src:tests python -m advice_eval cost
    PYTHONPATH=src:tests python -m advice_eval arms --snapshot daily_loss_room
    PYTHONPATH=src:tests python -m advice_eval controls

`cost` is the subcommand that produces the figure the spend decision needs.
`arms` prints an arm so a reader can see for themselves what the ablation
removed, rather than taking this package's word for it.

THERE IS NO `run` SUBCOMMAND. Running the matrix for real needs a live
transport, and this harness ships none (see `runner.py`). That is the spend
boundary, and it is structural rather than a flag someone can set by accident.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

from advice_eval import cost
from advice_eval.arms import ARMS
from advice_eval.questions import METRIC_LABELS, QUESTIONS
from advice_eval.snapshots import build_all


def _snapshots():
    # A throwaway directory: every snapshot is rebuilt from a pinned seed, so
    # nothing here is worth keeping between runs.
    return build_all(Path(tempfile.mkdtemp(prefix="advice-eval-")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="advice_eval", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("cost", help="the upper-bound cost of the full matrix")
    arms_cmd = sub.add_parser("arms", help="print the arm texts for one snapshot")
    arms_cmd.add_argument("--snapshot", default="currency_at_cap")
    arms_cmd.add_argument("--arm", default="", choices=["", *ARMS])
    sub.add_parser("questions", help="the question list and which metrics each grades")
    sub.add_parser("set", help="the graded set, with each planted reference value")

    args = parser.parse_args(argv)

    if args.command == "cost":
        print(cost.render(_snapshots()))
        return 0

    if args.command == "arms":
        snapshots = _snapshots()
        if args.snapshot not in snapshots:
            print(f"no such snapshot: {args.snapshot}", file=sys.stderr)
            return 2
        snapshot = snapshots[args.snapshot]
        wanted = [args.arm] if args.arm else list(ARMS)
        for arm in wanted:
            print(f"===== {snapshot.name} arm {arm} =====")
            print(snapshot.arms[arm])
            print()
        return 0

    if args.command == "questions":
        for question in QUESTIONS:
            metrics = ", ".join(METRIC_LABELS[m] for m in question.metrics)
            print(f"{question.qid}\t{question.snapshot}\t[{metrics}]\t{question.text}")
        return 0

    snapshots = _snapshots()
    for name, snapshot in snapshots.items():
        print(f"{name}\tcircuit={snapshot.circuit_reason or 'clear'}")
        for note in snapshot.notes:
            print(f"\tnote: {note}")
        for quantity, reference in snapshot.references.items():
            print(f"\t{quantity} = {reference.value:.4f}  (from {reference.source})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
