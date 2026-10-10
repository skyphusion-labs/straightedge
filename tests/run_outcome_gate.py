"""Decision logic for surfacing a `main` run that did not succeed.

Separate from the workflow so the comparison itself is importable and testable.
A guard nobody has watched fail is decoration, so `--self-test` drives `decide`
red on purpose and `tests/test_run_outcome_gate.py` asserts the same cases.

WHY THIS CANNOT BE A STEP IN `ci.yml` OR `code-coverage.yml`
A cancelled run cannot report its own cancellation: the step that would do the
reporting is cancelled along with everything else. So the observer has to be a
separate workflow triggered on `workflow_run`, which fires on completion
whatever the conclusion.

WHY THE CONDITION IS `conclusion != "success"` AND NOT `conclusion == "failure"`
Measured on `main` before the #329 concurrency fix: of 153 commits with a push
run, 21 never had a successful `ci` run, and 18 of those 21 were `cancelled`
rather than `failed`. A detector keyed on "failure" would have missed 18 of 21.
`cancelled` is not the only one either: `timed_out`, `startup_failure`, `stale`
and `action_required` all mean the check did not pass and none of them is
"failure". Enumerating the bad outcomes is the mistake; the condition is the
absence of the one good one.

Do not simplify this comparison back to `== "failure"`. That is the defect this
file exists to prevent, and it reads like a tidy-up.
"""

from __future__ import annotations

import argparse
import json
import sys

#: The one conclusion that means the check actually passed.
GOOD = "success"

#: Conclusions that mean the run has not finished yet. `workflow_run` with
#: `types: [completed]` should never deliver these, so seeing one is a signal
#: that the trigger is wired wrong rather than that `main` is unhealthy.
PENDING = ("in_progress", "queued", "requested", "waiting", "pending", "")

#: The branch this gate speaks for. A run on any other ref is somebody's PR and
#: is their problem, not an unverified commit on the trunk.
BRANCH = "main"

SYNTHETIC_PREFIX = "[SYNTHETIC TEST, NOT A REAL ALERT] "

TRACKER_TITLE = "main has commits whose required runs did not succeed"


def decide(conclusion: str, branch: str) -> tuple[bool, str]:
    """Return (should_alert, reason).

    `reason` is returned for both answers on purpose: a gate that explains only
    its alarms cannot be debugged when it stays silent, and staying silent
    wrongly is this gate's failure mode.
    """
    c = (conclusion or "").strip().lower()
    b = (branch or "").strip()
    if b != BRANCH:
        return False, "branch is %r, not %r: not this gate's business" % (b, BRANCH)
    if c in PENDING:
        return False, (
            "conclusion is %r, which means the run has not finished; "
            "a completed trigger should never deliver this, so check the wiring" % c
        )
    if c == GOOD:
        return False, "conclusion is %r: the commit is verified" % c
    return True, (
        "conclusion is %r, which is not %r, so this commit on %s carries no "
        "completed verification" % (c, GOOD, BRANCH)
    )


def render(conclusion: str, workflow: str, sha: str, url: str,
           synthetic: bool) -> tuple[str, str]:
    """Build the issue title and body.

    The synthetic marker goes in the TITLE, not only in the body, so it is
    unmissable in a notification list without opening anything, and synthetic
    alerts land on their own tracker rather than polluting the real one.
    """
    prefix = SYNTHETIC_PREFIX if synthetic else ""
    title = prefix + TRACKER_TITLE
    lines = []
    if synthetic:
        lines += [
            "**This is a SYNTHETIC alert from a deliberate `workflow_dispatch` "
            "test of this notifier. It describes no real run and `main` is not "
            "affected.** It exists because a `workflow_run` workflow cannot be "
            "exercised from a pull request, and letting `main` actually fail to "
            "test a detector is not acceptable.",
            "",
        ]
    lines += [
        "A required workflow on `%s` finished without succeeding, so that commit "
        "has no completed verification." % BRANCH,
        "",
        "| field | value |",
        "| --- | --- |",
        "| workflow | `%s` |" % workflow,
        "| conclusion | `%s` |" % conclusion,
        "| commit | `%s` |" % sha,
        "| run | %s |" % (url or "not supplied"),
        "",
        "**A `cancelled` conclusion is the common case and reads as "
        "\"not red\" everywhere a human looks**, which is why this exists: before "
        "the #329 concurrency fix, 18 of the 21 unverified commits on `main` were "
        "cancelled rather than failed.",
        "",
        "Recovery is `gh run rerun <run-id>`, which makes the loss recoverable "
        "rather than permanent.",
        "",
        "This is one tracking issue updated per occurrence rather than a new "
        "issue each time, because a burst that floods the channel is how a "
        "notifier gets muted.",
    ]
    return title, "\n".join(lines)


def self_test() -> int:
    cases = [
        # (conclusion, branch, expect_alert, label)
        ("success", "main", False, "a passing run says nothing"),
        ("failure", "main", True, "a failed run alerts"),
        ("cancelled", "main", True, "a CANCELLED run alerts: 18 of 21 were these"),
        ("timed_out", "main", True, "a timed-out run alerts"),
        ("startup_failure", "main", True, "a startup failure alerts"),
        ("stale", "main", True, "a stale run alerts"),
        ("action_required", "main", True, "action_required alerts"),
        ("SUCCESS", "main", False, "conclusion comparison is case-insensitive"),
        ("cancelled", "some-pr-branch", False, "another branch is not our business"),
        ("in_progress", "main", False, "an unfinished run is a wiring signal"),
        ("", "main", False, "an empty conclusion is registration lag, not D=0"),
    ]
    bad = 0
    for conclusion, branch, expect, label in cases:
        got, reason = decide(conclusion, branch)
        ok = got == expect
        if not ok:
            bad += 1
        print("%s  %-44s expected=%-5s got=%-5s  %s"
              % ("ok  " if ok else "FAIL", label, expect, got, reason))
    # The renderer has its own observable property: the synthetic marker must
    # reach the TITLE, because a body-only marker is invisible in a list.
    t_syn, b_syn = render("cancelled", "ci", "deadbeef", "http://x", True)
    t_real, _ = render("cancelled", "ci", "deadbeef", "http://x", False)
    for label, cond in (
        ("synthetic marker is in the title", t_syn.startswith(SYNTHETIC_PREFIX)),
        ("synthetic marker is in the body too", "SYNTHETIC" in b_syn),
        ("a real alert carries no marker", not t_real.startswith(SYNTHETIC_PREFIX)),
        ("synthetic and real titles differ", t_syn != t_real),
    ):
        if not cond:
            bad += 1
        print("%s  %s" % ("ok  " if cond else "FAIL", label))
    print("%d cases, %d failed" % (len(cases) + 4, bad))
    return 1 if bad else 0


def main(argv: list[str]) -> int:
    if "--self-test" in argv:
        return self_test()
    p = argparse.ArgumentParser()
    p.add_argument("--conclusion", required=True)
    p.add_argument("--workflow", default="unknown")
    p.add_argument("--branch", required=True)
    p.add_argument("--sha", default="unknown")
    p.add_argument("--url", default="")
    p.add_argument("--synthetic", action="store_true")
    a = p.parse_args(argv)
    alert, reason = decide(a.conclusion, a.branch)
    title, body = render(a.conclusion, a.workflow, a.sha, a.url, a.synthetic)
    json.dump({"alert": alert, "reason": reason, "title": title, "body": body},
              sys.stdout)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
