### Changelog entries are now one file per change (issue #259)

**Where you write a changelog entry has moved.** Add
`changelog.d/<issue>-<slug>.md` instead of editing `## Unreleased` in
`CHANGELOG.md`. A `CHANGELOG.md` edit no longer satisfies the CI gate, and
`docs/RELEASING.md` is the contract.

`CHANGELOG.md` was a serialisation point: every pull request prepended to the
same section, so every merge conflicted with every other open branch. Measured
on this repository, after two merges three of four open pull requests went
dirty on nothing but that one file, and because the required checks are strict,
each resolution was a content push that dismissed an existing approval and cost
a re-review plus a CI cycle. Two pull requests adding different files cannot
conflict, so the cost is gone by construction rather than by discipline.

- **The 17 entries that were under `## Unreleased` moved into fragments**,
  byte for byte. Nothing was reworded, and the released sections of
  `CHANGELOG.md` are untouched.
- **The gate from #268 was retargeted, not duplicated.** It is the same job and
  the same file; its subject changed from a `CHANGELOG.md` edit to a fragment.
  A deleted fragment does not satisfy it either, because a release removes every
  file in the directory and a name-only diff cannot tell that apart from adding
  one.
- **`## Unreleased` stays, deliberately empty**, with a line saying where
  entries go. Two places to put an entry is how the old convention stopped being
  in force: the entry/no-entry split across five merges was uncorrelated with
  whether `src/` changed.
- **The release assembler has a test rather than a runbook step.** A release
  runs it about once a quarter, long enough to rot unnoticed, so
  `tests/test_changelog_assemble.py` drives the shipped tool, `--apply`
  included, on every pull request.
