# Releasing: changelog fragments

The contract for recording a change and for cutting a release. Reproducible from
this file alone: every rule here is enforced by a command named here, and every
command here runs in CI.

## Where an entry goes

**One file per change, in `changelog.d/`.** Never edit `## Unreleased` in
`CHANGELOG.md`.

```
changelog.d/<issue>-<slug>.md
```

- `<issue>` is the issue number, digits only. **`0000` means the change has no
  issue**, which is deliberately ugly: work with no issue is undispatchable and
  uncreditable, and the filename should say so.
- `<slug>` is lowercase alphanumeric words joined by single hyphens. Uppercase
  and underscores are refused, because a name that differs only by case behaves
  differently on a case-sensitive CI runner and a case-insensitive laptop.
- The file **must open with its own `### ` heading**. A release concatenates
  fragments, so a fragment with no heading folds its prose into the entry above.

Write the entry exactly as it should appear under a version heading:

```markdown
### A reply an operator reads has changed (issue #233)

**What changed, in the first sentence.** Then why, then the detail. Operator
docs from 1.0.0 use 8th-grade Simplified Technical English; match that.
```

## Why files instead of a shared list

`CHANGELOG.md` was a **serialisation point**. Every pull request prepended under
`## Unreleased`, so every merge conflicted with every other open branch.
Measured on straightedge#259: after two merges, three of four open PRs went
dirty on **nothing but that one file**, and because the required checks use
`strict_required_status_checks_policy`, each resolution was a content push that
**dismissed an existing approval** and cost a re-review plus a CI cycle. That is
O(N^2) in queue size.

Two pull requests adding **different files cannot conflict**, so the cost is
removed by construction rather than by discipline. The proof is in #259's pull
request: two branches each adding a fragment trial-merge with zero conflicts,
and the same two branches each prepending to `CHANGELOG.md` produce one.

## The one-place invariant, and what enforces it

**An entry has exactly one home.** If `CHANGELOG.md` kept a live `## Unreleased`
section while fragments existed, there would be two places to put an entry, and
a convention with two homes is not in force. That is not hypothetical: across
five merges the entry/no-entry split was measured as **uncorrelated** with
whether `src/` changed, which is what an unenforced convention looks like from
outside.

Two commands hold it:

```sh
python3 tests/changelog_assemble.py --check
```

Fails if any `### ` entry appears under `## Unreleased`, if a fragment name is
malformed, if a fragment is empty or has no heading, or if two fragments share a
key. Run by `pytest` on every pull request via
`tests/test_changelog_assemble.py`.

```sh
BASE_SHA=... HEAD_SHA=... PR_BODY=... python3 tests/changelog_gate.py
```

Fails if a change touches `src/straightedge/` and adds no fragment. This is the
**same gate** that shipped in #268 asking for a `CHANGELOG.md` edit; its subject
moved rather than a second gate being written beside it. **A `CHANGELOG.md` edit
no longer satisfies it**, and a *deleted* fragment does not either, because a
release removes every file in the directory and a `--name-only` diff cannot tell
that apart from adding one.

## The opt-out

A change under `src/straightedge/` that genuinely needs no entry says so **in
the pull request body**:

```
no-changelog: <why this change needs no entry>
```

**The reason is required and a bare marker is refused.** A bare marker would be
an allowlist with one entry written in a different file, and a scanner with a
silent ignore list stops being a denominator: entries accumulate, nobody
re-reads them, and it reports green over a population it quietly stopped
covering. A line in the body is read by a human at review time, which is the
point.

## What the gate cannot see

`src/straightedge/` is a **trigger**, not a definition of changed behaviour. A
comment-only diff under `src/` trips it, and a behaviour change made through a
workflow or a config default does not. Stated so it can be argued with, which is
the whole difference from the convention it replaced.

## Cutting a release

The project bumps version and changelog by hand; this slots into that step.

```sh
# 1. see what would be written, change nothing
python3 tests/changelog_assemble.py --version 1.9.0

# 2. write it: inserts the assembled block and DELETES the fragments
python3 tests/changelog_assemble.py --version 1.9.0 --apply

# 3. confirm the invariant still holds afterwards
python3 tests/changelog_assemble.py --check
```

Entries are assembled in **issue-number order**, numerically. `9` sorts before
`217`; a lexical sort would reverse them and a reader would never notice in the
rendered file.

The empty `## Unreleased` heading **survives** a release, above the new version
heading. Removing it would leave the next author wondering where entries go,
which is the question this contract exists to answer once.

A release with **zero** fragments is refused: more likely a mistake than a quiet
quarter.

## Why the assembler has a test rather than a runbook step

A release runs the assembler about once a quarter, which is exactly long enough
for it to rot unnoticed between uses. `tests/test_changelog_assemble.py` drives
the **shipped** tool, including `--apply` against a temporary tree, on every
pull request and on both supported Pythons. It deliberately does not
re-implement the checks: a test that rebuilds what it should call can agree with
a broken original (see `docs/TESTING.md`).
