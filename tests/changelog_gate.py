#!/usr/bin/env python3
"""A changelog entry is REQUIRED when a change touches `src/straightedge/`.

straightedge#259. Five recent merges were measured and the convention was
firing in BOTH directions against no trigger at all:

    | entry    | src/ touched: #250 | not touched: #241 |
    | no entry | src/ touched: #248, #257 | not touched: #253 |

One of the two carrying an entry touched `src/` and one did not; two of the
three without touched it. **The entry/no-entry split is uncorrelated with
`src/`, so there was nothing in force for a proxy to proxy for.** Three
different ratios were computed from that data by two seats before anyone
noticed it was not measuring a convention. A trigger that is inferred gets
applied when somebody remembers; this states it and gates it.

THE OPT-OUT IS EXPLICIT AND LIVES IN THE PULL REQUEST BODY, never in an
allowlist in this file. A scanner with a silent ignore list stops being a
denominator: the entries get added, nobody re-reads them, and the gate reports
green over a population it has quietly stopped covering. A line in the body is
read by a human at review time, which is the point.

    no-changelog: <why this change needs no entry>

The reason is REQUIRED and must be non-empty. A bare marker is an allowlist
with one entry, written in a different file.

WHAT THIS CANNOT SEE, said plainly because a gate that overstates its reach is
worse than none. `src/straightedge/` is a TRIGGER, not a definition of
"changed behaviour": a comment-only diff under `src/` trips it and a behaviour
change made through a workflow or a config default does not. The trigger is
stated here so it can be argued with, which is the whole difference from the
convention it replaces.

Usage, one measurement per invocation, status and output both meaningful:

    BASE_SHA=... HEAD_SHA=... PR_BODY="..." python3 tests/changelog_gate.py
    python3 tests/changelog_gate.py --self-test
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

TRIGGER_PREFIX = "src/straightedge/"
CHANGELOG = "CHANGELOG.md"
OPT_OUT = re.compile(r"^\s*no-changelog:\s*(\S.*)$", re.M | re.I)


def changed_files(base: str, head: str) -> list[str]:
    out = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...{head}"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(
            "FATAL: git diff failed for %s...%s, so this gate cannot report a "
            "verdict:\n%s" % (base, head, out.stderr.strip())
        )
    return [p for p in out.stdout.splitlines() if p.strip()]


def decide(files: list[str], body: str) -> tuple[int, list[str]]:
    """Return (exit_code, lines_to_print). Pure, so the self-test can drive it."""
    lines = []
    triggered = sorted(p for p in files if p.startswith(TRIGGER_PREFIX))
    has_entry = CHANGELOG in files
    opt = OPT_OUT.search(body or "")

    lines.append("files in range: %d" % len(files))
    lines.append("  %s files touched: %d" % (TRIGGER_PREFIX, len(triggered)))
    for p in triggered:
        lines.append("    %s" % p)
    lines.append("  %s touched: %s" % (CHANGELOG, "yes" if has_entry else "no"))
    lines.append("  opt-out in body: %s" % (("yes: " + opt.group(1).strip()) if opt else "no"))

    # AN EMPTY RANGE IS NOT A PASS. A gate that cannot see the diff reports that
    # it could not measure, rather than the reassuring answer.
    if not files:
        lines.append(
            "VERDICT: COULD NOT MEASURE. The diff range produced no files, so "
            "this gate observed nothing. That is not a clean result."
        )
        return 1, lines

    if not triggered:
        lines.append(
            "VERDICT: not triggered. Nothing under %s changed, so no entry is "
            "required." % TRIGGER_PREFIX
        )
        return 0, lines
    if has_entry:
        lines.append("VERDICT: ok. The change touches %s and carries an entry." % TRIGGER_PREFIX)
        return 0, lines
    if opt:
        lines.append(
            "VERDICT: ok by explicit opt-out. The reason is in the pull request "
            "body and a reviewer reads it there."
        )
        return 0, lines
    lines.append(
        "VERDICT: FAILED. This change touches %s and has no %s entry.\n"
        "  Add one, or put a line in the pull request body saying why not:\n"
        "    no-changelog: <why this change needs no entry>\n"
        "  A reason is required; a bare marker is an allowlist in another file."
        % (TRIGGER_PREFIX, CHANGELOG)
    )
    return 1, lines


def self_test() -> int:
    """Positive AND negative controls, because a gate nobody has seen fail is
    decoration. Drives `decide` directly: no git, no network, no PR."""
    cases = [
        ("src touched, entry present", ["src/straightedge/engine.py", "CHANGELOG.md"], "", 0),
        ("src touched, no entry", ["src/straightedge/engine.py"], "", 1),
        ("src touched, no entry, opt-out with reason", ["src/straightedge/engine.py"],
         "no-changelog: comment-only, no behaviour change", 0),
        ("src touched, no entry, BARE marker is not an opt-out",
         ["src/straightedge/engine.py"], "no-changelog:", 1),
        ("src touched, no entry, opt-out mid-body", ["src/straightedge/engine.py"],
         "Some prose.\n\nno-changelog: tests only moved\n\nMore prose.", 0),
        ("tests only", ["tests/test_x.py"], "", 0),
        ("docs only", ["docs/CONTRACT.md"], "", 0),
        ("nested src path triggers", ["src/straightedge/broker/mt4_live.py"], "", 1),
        ("a path merely CONTAINING src does not trigger",
         ["agent/src/index.ts"], "", 0),
        ("EMPTY RANGE is could-not-measure, not a pass", [], "", 1),
    ]
    failures = 0
    for name, files, body, want in cases:
        got, _ = decide(files, body)
        ok = got == want
        failures += not ok
        print("  %-62s want=%d got=%d %s" % (name, want, got, "ok" if ok else "FAIL"))
    print("cases_run=%d failed=%d" % (len(cases), failures))
    if failures:
        print("changelog_gate self-test: FAILED")
        return 1
    print("changelog_gate self-test: COMPLETE")
    return 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    base = os.environ.get("BASE_SHA", "").strip()
    head = os.environ.get("HEAD_SHA", "HEAD").strip() or "HEAD"
    if not base:
        raise SystemExit("FATAL: BASE_SHA is required; without it there is no range")
    code, lines = decide(changed_files(base, head), os.environ.get("PR_BODY", ""))
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
