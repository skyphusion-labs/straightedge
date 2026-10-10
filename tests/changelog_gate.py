#!/usr/bin/env python3
"""A changelog FRAGMENT is REQUIRED when a change touches `src/straightedge/`.

RETARGETED, not duplicated (straightedge#259, fragment half). This gate shipped
in #268 asking for a `CHANGELOG.md` edit. The fragment change moved where an
entry lives, so the SUBJECT of this same gate moved with it: it now requires a
`changelog.d/<issue>-<slug>.md` file, and a `CHANGELOG.md` edit deliberately
does NOT satisfy it. Writing a second gate beside this one would have left two
checks asking one question in two spellings, which is the rebuilt-consumer
defect `docs/TESTING.md` carries an entry about.

A DELETED fragment does not count either. A release assembles the directory and
removes every file in it, and a `--name-only` diff cannot tell that apart from
adding one, so the fragment check reads the added-or-modified subset.

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

The release side of the same contract is `tests/changelog_assemble.py`, and
`docs/RELEASING.md` is the contract in prose.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

TRIGGER_PREFIX = "src/straightedge/"
CHANGELOG = "CHANGELOG.md"
FRAGMENT_DIR = "changelog.d/"
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


def added_or_modified(base: str, head: str) -> list[str]:
    """Paths ADDED or MODIFIED in the range.

    A DELETED fragment must not satisfy this gate: a release assembles the
    directory and removes every file in it, and a `--name-only` list cannot tell
    that apart from adding one. The narrower view is what the fragment check
    reads.
    """
    out = subprocess.run(
        ["git", "diff", "--name-only", "--diff-filter=AM", f"{base}...{head}"],
        capture_output=True,
        text=True,
    )
    if out.returncode != 0:
        raise SystemExit(
            "FATAL: git diff --diff-filter=AM failed for %s...%s:\n%s"
            % (base, head, out.stderr.strip())
        )
    return [p for p in out.stdout.splitlines() if p.strip()]


def is_fragment(path: str) -> bool:
    return path.startswith(FRAGMENT_DIR) and path.endswith(".md")


def decide(files: list[str], body: str, am: list[str] | None = None) -> tuple[int, list[str]]:
    """Return (exit_code, lines_to_print). Pure, so the self-test can drive it.

    `am` is the added-or-modified subset. It defaults to `files` so a caller that
    does not care keeps the simple behaviour, but CI passes the real subset.
    """
    lines = []
    if am is None:
        am = files
    triggered = sorted(p for p in files if p.startswith(TRIGGER_PREFIX))
    fragments = sorted(p for p in am if is_fragment(p))
    has_entry = bool(fragments)
    touched_changelog = CHANGELOG in files
    opt = OPT_OUT.search(body or "")

    lines.append("files in range: %d" % len(files))
    lines.append("  %s files touched: %d" % (TRIGGER_PREFIX, len(triggered)))
    for p in triggered:
        lines.append("    %s" % p)
    lines.append("  %s fragments added or modified: %d" % (FRAGMENT_DIR, len(fragments)))
    for p in fragments:
        lines.append("    %s" % p)
    lines.append("  %s touched: %s (does NOT satisfy this gate; see below)"
                 % (CHANGELOG, "yes" if touched_changelog else "no"))
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
        lines.append("VERDICT: ok. The change touches %s and adds a %s fragment."
                     % (TRIGGER_PREFIX, FRAGMENT_DIR))
        return 0, lines
    if opt:
        lines.append(
            "VERDICT: ok by explicit opt-out. The reason is in the pull request "
            "body and a reviewer reads it there."
        )
        return 0, lines
    detail = ""
    if touched_changelog:
        detail = (
            "\n  NOTE: %s WAS touched, and that deliberately does not count. "
            "Entries live\n  in %s only, one file per change: two homes is how "
            "the convention stopped\n  being in force (straightedge#259)."
            % (CHANGELOG, FRAGMENT_DIR)
        )
    lines.append(
        "VERDICT: FAILED. This change touches %s and adds no %s fragment.\n"
        "  Add %s<issue>-<slug>.md, or put a line in the pull request body "
        "saying why not:\n"
        "    no-changelog: <why this change needs no entry>\n"
        "  A reason is required; a bare marker is an allowlist in another file.%s"
        % (TRIGGER_PREFIX, FRAGMENT_DIR, FRAGMENT_DIR, detail)
    )
    return 1, lines


def self_test() -> int:
    """Positive AND negative controls, because a gate nobody has seen fail is
    decoration. Drives `decide` directly: no git, no network, no PR."""
    SRC = "src/straightedge/engine.py"
    FRAG = "changelog.d/259-a-slug.md"
    # (name, files, added_or_modified, body, want)
    cases = [
        ("src touched, fragment added", [SRC, FRAG], [SRC, FRAG], "", 0),
        ("src touched, no fragment", [SRC], [SRC], "", 1),
        # THE DISCRIMINATING CASE. Before #259 this passed; it must now fail, or
        # the subject did not actually move and there are still two homes.
        ("src touched, CHANGELOG.md edited but NO fragment",
         [SRC, "CHANGELOG.md"], [SRC, "CHANGELOG.md"], "", 1),
        # A release deletes every fragment. That must not read as adding one.
        ("src touched, fragment only DELETED", [SRC, FRAG], [SRC], "", 1),
        ("src touched, fragment MODIFIED rather than added", [SRC, FRAG], [SRC, FRAG], "", 0),
        ("src touched, no fragment, opt-out with reason", [SRC], [SRC],
         "no-changelog: comment-only, no behaviour change", 0),
        ("src touched, no fragment, BARE marker is not an opt-out",
         [SRC], [SRC], "no-changelog:", 1),
        ("src touched, no fragment, opt-out mid-body", [SRC], [SRC],
         "Some prose.\n\nno-changelog: tests only moved\n\nMore prose.", 0),
        ("tests only", ["tests/test_x.py"], ["tests/test_x.py"], "", 0),
        ("docs only", ["docs/CONTRACT.md"], ["docs/CONTRACT.md"], "", 0),
        ("nested src path triggers", ["src/straightedge/broker/mt4_live.py"],
         ["src/straightedge/broker/mt4_live.py"], "", 1),
        ("a path merely CONTAINING src does not trigger",
         ["agent/src/index.ts"], ["agent/src/index.ts"], "", 0),
        ("a fragment that is not .md does not count",
         [SRC, "changelog.d/notes.txt"], [SRC, "changelog.d/notes.txt"], "", 1),
        ("EMPTY RANGE is could-not-measure, not a pass", [], [], "", 1),
    ]
    failures = 0
    for name, files, am, body, want in cases:
        got, _ = decide(files, body, am)
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
    code, lines = decide(
        changed_files(base, head),
        os.environ.get("PR_BODY", ""),
        added_or_modified(base, head),
    )
    print("\n".join(lines))
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
