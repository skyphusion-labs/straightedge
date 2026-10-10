"""CLI for the advice-eval harness.

    PYTHONPATH=src:tests python -m advice_eval cost
    PYTHONPATH=src:tests python -m advice_eval arms --snapshot daily_loss_room
    PYTHONPATH=src:tests python -m advice_eval set

`cost` is the subcommand that produces the figure the spend decision needs.
`arms` prints an arm so a reader can see for themselves what the ablation
removed, rather than taking this package's word for it.

THERE IS NO `run` SUBCOMMAND. Running the matrix for real needs a live
transport, and this harness ships none (see `runner.py`). That is the spend
boundary, and it is structural rather than a flag someone can set by accident.

THE SNAPSHOT DIRECTORY IS CLEANED UP, via `TemporaryDirectory` rather than
`mkdtemp`. Building the graded set writes a journal per snapshot, and the first
version of this file leaked one directory per invocation into `/tmp`: measured,
because the sprint's own close-out enumerated 42 joan-owned entries in
`/private/tmp` and two of them were this command's. `mkdtemp` has no owner, so
nothing ever removes it. Everything the snapshots need is read during the build
and the `Snapshot` objects are pure data afterwards, so the directory can go as
soon as the build returns.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from advice_eval import cost
from advice_eval.arms import ARMS
from advice_eval.questions import METRIC_LABELS, QUESTIONS
from advice_eval.snapshots import Snapshot, build_all


def _cmd_cost(snapshots: Mapping[str, Snapshot], args: argparse.Namespace) -> int:
    print(cost.render(snapshots))
    return 0


def _cmd_arms(snapshots: Mapping[str, Snapshot], args: argparse.Namespace) -> int:
    if args.snapshot not in snapshots:
        print(f"no such snapshot: {args.snapshot}", file=sys.stderr)
        return 2
    snapshot = snapshots[args.snapshot]
    for arm in [args.arm] if args.arm else list(ARMS):
        print(f"===== {snapshot.name} arm {arm} =====")
        print(snapshot.arms[arm])
        print()
    return 0


def _cmd_set(snapshots: Mapping[str, Snapshot], args: argparse.Namespace) -> int:
    for name, snapshot in snapshots.items():
        print(f"{name}\tcircuit={snapshot.circuit_reason or 'clear'}")
        for note in snapshot.notes:
            print(f"\tnote: {note}")
        for quantity, reference in snapshot.references.items():
            print(f"\t{quantity} = {reference.value:.4f}  (from {reference.source})")
    return 0


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

    # `questions` needs no snapshot, so it does not pay to build one.
    if args.command == "questions":
        for question in QUESTIONS:
            metrics = ", ".join(METRIC_LABELS[m] for m in question.metrics)
            print(f"{question.qid}\t{question.snapshot}\t[{metrics}]\t{question.text}")
        return 0

    handlers = {"cost": _cmd_cost, "arms": _cmd_arms, "set": _cmd_set}
    # Every snapshot is rebuilt from a pinned seed, so nothing in here is worth
    # keeping between runs and the directory is removed on the way out.
    with tempfile.TemporaryDirectory(prefix="advice-eval-") as root:
        return handlers[args.command](build_all(Path(root)), args)


if __name__ == "__main__":
    raise SystemExit(main())
