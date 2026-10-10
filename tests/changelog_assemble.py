#!/usr/bin/env python3
"""Fold `changelog.d/` fragments into `CHANGELOG.md` at release time.

straightedge#259. `CHANGELOG.md` was a serialisation point: every pull request
prepended under `## Unreleased`, so every merge dirtied every other open branch
and each resolution dismissed an approval. Measured there: three of four PRs
went dirty on nothing but that one file. One file per change removes the
conflict BY CONSTRUCTION rather than by discipline, because two pull requests
adding different files cannot conflict.

THE ONE-PLACE INVARIANT IS THE WHOLE POINT, and `--check` enforces it. If
`CHANGELOG.md` keeps a live `## Unreleased` section AND fragments exist, there
are two places an entry can go, and a convention with two homes is not in
force. That is not a hypothetical: the entry/no-entry split across five merges
was measured as UNCORRELATED with whether `src/` changed, which is what an
unenforced convention looks like from the outside. So `--check` fails if any
`### ` entry appears under `## Unreleased`, and the gate in
`tests/changelog_gate.py` requires a FRAGMENT rather than a `CHANGELOG.md`
edit. Those two together are what make the single home real.

NAMING: `changelog.d/<issue>-<slug>.md`, lowercase slug, hyphen separated.
The issue number comes first so the directory sorts by issue and so a reader
can find the discussion. `0000` is the sentinel for a change with no issue;
it is deliberately ugly, because work with no issue is undispatchable and
uncreditable, and the filename should say so.

WHY THE ASSEMBLER HAS A TEST RATHER THAN A RUNBOOK SECTION. A release runs this
once a quarter, which is exactly long enough for it to rot unnoticed. The test
in `tests/test_changelog_assemble.py` is collected by `pytest` on every pull
request (`testpaths = ["tests"]`), so the release path is exercised constantly
rather than at the moment it is needed.

Usage, one measurement per invocation, status and output both meaningful:

    python3 tests/changelog_assemble.py --check
    python3 tests/changelog_assemble.py --version 1.9.0           # dry run
    python3 tests/changelog_assemble.py --version 1.9.0 --apply
    python3 tests/changelog_assemble.py --self-test
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys

FRAGMENT_DIR = "changelog.d"
CHANGELOG = "CHANGELOG.md"
UNRELEASED = "## Unreleased"
NO_ISSUE = "0000"

# `<issue>-<slug>.md`. The slug is lowercase alphanumeric words joined by single
# hyphens, so the name cannot carry shell metacharacters or case that differs
# between a case-sensitive CI runner and a case-insensitive laptop filesystem.
NAME = re.compile(r"^(\d{1,6})-([a-z0-9]+(?:-[a-z0-9]+)*)\.md$")

# A fragment must open with its own `### ` heading, because the assembled output
# is a concatenation: a fragment with no heading would silently merge its prose
# into the entry above it.
HEADING = re.compile(r"^### \S")


class Fragment:
    def __init__(self, path: pathlib.Path, issue: int, slug: str, text: str):
        self.path = path
        self.issue = issue
        self.slug = slug
        self.text = text

    @property
    def key(self) -> tuple[int, str]:
        return (self.issue, self.slug)


def read_fragments(root: pathlib.Path) -> tuple[list[Fragment], list[str]]:
    """Return (fragments, errors). Never raises on bad input: a release tool that
    dies on the first problem makes the operator fix them one run at a time."""
    errors: list[str] = []
    found: list[Fragment] = []
    d = root / FRAGMENT_DIR
    if not d.is_dir():
        return [], ["%s/ does not exist" % FRAGMENT_DIR]

    for path in sorted(d.iterdir()):
        if path.name in (".gitkeep", "README.md"):
            continue
        if path.is_dir():
            errors.append("%s/%s is a directory; fragments are flat files" % (FRAGMENT_DIR, path.name))
            continue
        m = NAME.match(path.name)
        if not m:
            errors.append(
                "%s/%s does not match <issue>-<slug>.md (lowercase slug, "
                "hyphen separated; use %s for no issue)" % (FRAGMENT_DIR, path.name, NO_ISSUE)
            )
            continue
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            errors.append("%s/%s is empty" % (FRAGMENT_DIR, path.name))
            continue
        if not HEADING.match(text.lstrip("\n")):
            errors.append(
                "%s/%s does not start with a '### ' heading, so assembling it "
                "would fold its prose into the entry above" % (FRAGMENT_DIR, path.name)
            )
            continue
        found.append(Fragment(path, int(m.group(1)), m.group(2), text))

    seen: dict[tuple[int, str], str] = {}
    for f in found:
        if f.key in seen:
            errors.append("duplicate fragment key %s: %s and %s" % (f.key, seen[f.key], f.path.name))
        seen[f.key] = f.path.name

    found.sort(key=lambda f: f.key)
    return found, errors


def unreleased_entries(changelog: str) -> list[str]:
    """The `### ` headings currently living under `## Unreleased`.

    Non-empty means an entry has TWO possible homes, which is the condition
    that makes the convention unenforceable.
    """
    lines = changelog.splitlines()
    out: list[str] = []
    inside = False
    for line in lines:
        if line.startswith("## "):
            inside = line.strip() == UNRELEASED
            continue
        if inside and line.startswith("### "):
            out.append(line.strip())
    return out


def assemble(version: str, frags: list[Fragment]) -> str:
    """The block that replaces `## Unreleased`. Verbatim fragment text, in key
    order, with exactly one blank line between entries."""
    bodies = [f.text.strip("\n") for f in frags]
    return "## %s\n\n%s\n" % (version, "\n\n".join(bodies))


def apply_release(root: pathlib.Path, version: str, frags: list[Fragment]) -> list[str]:
    """Insert the assembled block and delete the fragments. Returns a log."""
    log: list[str] = []
    path = root / CHANGELOG
    text = path.read_text(encoding="utf-8")
    marker = "\n%s\n" % UNRELEASED
    if marker not in text:
        raise SystemExit("FATAL: %s has no '%s' line to replace." % (CHANGELOG, UNRELEASED))

    # The Unreleased heading STAYS, empty, above the new version. Removing it
    # would make the next author wonder where entries go, which is the question
    # this whole change exists to answer once.
    replacement = "\n%s\n\n%s\n" % (UNRELEASED, assemble(version, frags).rstrip("\n"))
    path.write_text(text.replace(marker, replacement, 1), encoding="utf-8")
    log.append("%s: inserted %d entr%s under ## %s"
               % (CHANGELOG, len(frags), "y" if len(frags) == 1 else "ies", version))
    for f in frags:
        f.path.unlink()
        log.append("removed %s/%s" % (FRAGMENT_DIR, f.path.name))
    return log


def cmd_check(root: pathlib.Path) -> int:
    frags, errors = read_fragments(root)
    print("%s/: %d fragment(s)" % (FRAGMENT_DIR, len(frags)))
    for f in frags:
        print("  %s" % f.path.name)

    stray = unreleased_entries((root / CHANGELOG).read_text(encoding="utf-8"))
    print("'### ' entries under %s in %s: %d" % (UNRELEASED, CHANGELOG, len(stray)))
    for s in stray:
        print("  %s" % s)

    if stray:
        errors.append(
            "%s carries %d entr%s under %s. Entries live in %s/ only: two homes "
            "means the convention is not in force (straightedge#259)."
            % (CHANGELOG, len(stray), "y" if len(stray) == 1 else "ies", UNRELEASED, FRAGMENT_DIR)
        )

    for e in errors:
        print("ERROR: %s" % e)
    print("VERDICT: %s" % ("ok" if not errors else "FAILED, %d problem(s)" % len(errors)))
    return 0 if not errors else 1


def cmd_release(root: pathlib.Path, version: str, apply: bool) -> int:
    frags, errors = read_fragments(root)
    for e in errors:
        print("ERROR: %s" % e)
    if errors:
        print("VERDICT: FAILED, refusing to assemble over %d problem(s)" % len(errors))
        return 1
    if not frags:
        print("VERDICT: FAILED, no fragments to assemble. A release with no "
              "entries is more likely a mistake than a quiet quarter.")
        return 1
    block = assemble(version, frags)
    if not apply:
        print("# dry run, %d fragment(s). Re-run with --apply to write." % len(frags))
        print(block)
        return 0
    for line in apply_release(root, version, frags):
        print(line)
    print("VERDICT: ok")
    return 0


def self_test() -> int:
    """Positive AND negative controls against a real temporary tree.

    Drives the same functions a release drives, including `--apply`, because the
    risky half of this tool is the mutation and a test that only exercises the
    dry run proves the easy half.
    """
    import tempfile

    cases_run = 0
    failures: list[str] = []
    #: One owner per temporary tree. `tree()` used `tempfile.mkdtemp`, which
    #: returns a PATH and nothing else: no object owns the directory, so
    #: nothing ever removes it and every self-test run left one tree per case
    #: behind. Measured before this change, with an isolated TMPDIR so nothing
    #: else could contribute: ONE `--self-test` run leaked 12 directories, and
    #: it runs under `pytest` and again directly in `ci.yml`, so the rate is
    #: per-run on every machine rather than one developer's.
    #:
    #: `TemporaryDirectory` has an owner. Holding the objects here keeps each
    #: tree alive for as long as its case needs it, `cleanup()` below makes the
    #: removal deterministic, and even on an exception the objects carry a
    #: finalizer that removes the tree, which `mkdtemp` has no equivalent of.
    #:
    #: Why it is worth fixing at all, since it is only disk: these trees are
    #: identically shaped, so they make a close-out audit of `/tmp` read old
    #: sediment as current debris, and the correct response to a directory you
    #: cannot attribute is to leave it alone. See straightedge#326.
    holds: list[tempfile.TemporaryDirectory] = []

    def check(label: str, cond: bool) -> None:
        nonlocal cases_run
        cases_run += 1
        if cond:
            print("  ok   %s" % label)
        else:
            failures.append(label)
            print("  FAIL %s" % label)

    def tree(fragments: dict[str, str], unreleased_body: str = "") -> pathlib.Path:
        hold = tempfile.TemporaryDirectory()
        holds.append(hold)
        d = pathlib.Path(hold.name)
        (d / FRAGMENT_DIR).mkdir()
        for name, text in fragments.items():
            (d / FRAGMENT_DIR / name).write_text(text, encoding="utf-8")
        (d / CHANGELOG).write_text(
            "# Changelog\n\n%s\n%s\n## 1.8.0\n\n### An older entry\n\n- old\n"
            % (UNRELEASED, unreleased_body), encoding="utf-8"
        )
        return d

    good = {
        "217-a-breach-record.md": "### A breach record (issue #217)\n\n- one\n",
        "9-earlier-issue.md": "### An earlier issue (issue #9)\n\n- two\n",
        "0000-no-issue-at-all.md": "### No issue at all\n\n- three\n",
    }

    # 1. ordering is by issue NUMBER, not by filename string. "9" must sort
    #    before "217", which a lexical sort gets wrong.
    frags, errors = read_fragments(tree(good))
    check("three valid fragments parse with no errors", not errors and len(frags) == 3)
    check("ordered numerically: 0000, 9, 217 (a lexical sort would give 0000, 217, 9)",
          [f.issue for f in frags] == [0, 9, 217])

    # 2. every refusal, each its own case.
    for name, text, why in [
        ("217_bad_separator.md", "### x\n", "underscore instead of hyphen"),
        ("217-Mixed-Case.md", "### x\n", "uppercase in the slug"),
        ("no-issue-number.md", "### x\n", "no leading issue number"),
        ("217-ok.txt", "### x\n", "not a .md file"),
    ]:
        _, errs = read_fragments(tree({name: text}))
        check("refused (%s): %s" % (why, name), bool(errs))

    _, errs = read_fragments(tree({"217-empty.md": "\n  \n"}))
    check("refused: an empty fragment", bool(errs))

    _, errs = read_fragments(tree({"217-no-heading.md": "just prose, no heading\n"}))
    check("refused: a fragment with no '### ' heading", bool(errs))

    # 3. the ONE-PLACE invariant, in both directions.
    clean = tree(good)
    check("clean tree: no stray entries under Unreleased",
          unreleased_entries((clean / CHANGELOG).read_text(encoding="utf-8")) == [])
    dirty = tree(good, unreleased_body="\n### A stray entry put straight in the file\n\n- x\n")
    stray = unreleased_entries((dirty / CHANGELOG).read_text(encoding="utf-8"))
    check("detected: an entry written straight into CHANGELOG.md", len(stray) == 1)
    check("and the older released entry is NOT counted as stray",
          all("older" not in s for s in stray))

    # 4. assemble preserves fragment text VERBATIM. Checked by containment of
    #    each body rather than by comparing a whole rendered string, so the test
    #    does not pin the blank-line layout it is not trying to specify.
    frags, _ = read_fragments(tree(good))
    block = assemble("1.9.0", frags)
    check("assembled block opens with the version heading", block.startswith("## 1.9.0\n"))
    check("every fragment body survives verbatim",
          all(f.text.strip("\n") in block for f in frags))

    # 5. --apply: the mutation, which is the half a dry-run test would miss.
    d = tree(good)
    frags, _ = read_fragments(d)
    apply_release(d, "1.9.0", frags)
    after = (d / CHANGELOG).read_text(encoding="utf-8")
    check("apply: the Unreleased heading SURVIVES, empty", UNRELEASED in after)
    check("apply: the new version heading is present", "## 1.9.0" in after)
    check("apply: the older released section is untouched", "### An older entry" in after)
    check("apply: every fragment file is gone",
          not any(p.name.endswith(".md") for p in (d / FRAGMENT_DIR).iterdir()))
    check("apply: the assembled file has no stray Unreleased entries",
          unreleased_entries(after) == [])
    check("apply: entries appear in key order in the file",
          after.index("No issue at all") < after.index("An earlier issue") < after.index("A breach record"))

    # 6. refusing to assemble nothing, which is the state after a release.
    d2 = tree({})
    rc = cmd_release(d2, "1.9.1", apply=False)
    check("refuses a release with zero fragments", rc == 1)

    # Deterministic removal, rather than waiting for the finalizer. Sequenced
    # BEFORE the verdict print so a reader sees the count and the cleanup in
    # one place, and so a cleanup that raised could not be mistaken for a case
    # failure.
    for hold in holds:
        hold.cleanup()

    print("\nchangelog_assemble self-test: %d case(s), %d failure(s)"
          % (cases_run, len(failures)))
    return 0 if not failures else 1


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="validate fragments and the one-place invariant")
    p.add_argument("--self-test", action="store_true", help="positive and negative controls")
    p.add_argument("--version", help="assemble the fragments under this version heading")
    p.add_argument("--apply", action="store_true", help="write the changes (default is a dry run)")
    p.add_argument("--root", default=".", help="repository root (default: .)")
    args = p.parse_args(argv)

    root = pathlib.Path(args.root).resolve()
    if args.self_test:
        return self_test()
    if args.check:
        return cmd_check(root)
    if args.version:
        return cmd_release(root, args.version, args.apply)
    p.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
