# Branch protection: what actually gates `main`

Everything below was measured on 2026-10-10, not read off a settings page. Each claim names the
command that produces it. Re-measure before trusting any of it; ruleset state is mutable and
org-level, so it can change without a commit in this repo.

## Read the right endpoint

```
gh api repos/skyphusion-labs/straightedge/rules/branches/main   # what GATES this branch
gh api orgs/skyphusion-labs/rulesets                            # what merely EXISTS
```

**Use the first.** A ruleset can read `enforcement: active`, list a full set of rules, and gate
nothing, because a ruleset with no `ref_name` condition matches zero branches. That exact failure
was live on this org until 2026-09-26. The second endpoint shows such a ruleset; the first does not.

## Two org rulesets gate this branch, and rules AGGREGATE

`/rules/branches/main` returns rules from **both** `18677665` (`aviation-grade-main-org`) and
`24049804` (`aviation-grade-org`):

| rule | from 18677665 | from 24049804 |
|---|---|---|
| `required_status_checks` | `strict: true`, contexts `ci` + `coverage` | `strict: true`, contexts **empty** |
| `pull_request` | 1 approval, `dismiss_stale_reviews_on_push: true` | 0 approvals, no dismissal |
| `code_scanning` | CodeQL, medium-or-higher | not present |
| `non_fast_forward` | yes | yes |
| `deletion` | yes | not present |

Two consequences, and both have misled people here:

1. **Rulesets only ADD. A repo-level ruleset cannot subtract an org rule.** There is no way to relax
   a rule for this repo alone by writing a repo-level ruleset; relaxing it means editing the org
   ruleset (which hits every repo that carries it) or adding this repo to that ruleset's exclusion
   list and rebuilding the rest of its protection by hand. Deleting a tier to "simplify" removes
   protection silently.
2. **A change to one tier is not a change to the gate.** Both tiers carry
   `strict_required_status_checks_policy: true`, so dropping it from `18677665` alone leaves the
   flag set in `24049804`. Measured coverage, 2026-10-10:

```
# 43 non-archived repos scanned via `gh repo list skyphusion-labs --limit 100 --no-archived`
# then `gh api repos/skyphusion-labs/<r>/rules/branches/main` for each
non-archived repos scanned: 43
carry 18677665: 38
carry 24049804: 43        <- no exclusions at all
lack 18677665: fleet-chezmoi crew-secrets rockenhaus-litigation vivijure .github
```

## The effective gate, as measured

One approving review, `ci`, `coverage`, CodeQL at medium-or-higher, branch up to date with `main`,
no force-push, no branch deletion. Stale reviews are dismissed on push.

`coverage` here is a **real** test run, not the passthrough echo it is in most of this org's repos:
`.github/workflows/code-coverage.yml` runs `pytest -q --cov=straightedge --cov-fail-under=80`. So
it can go red, and a dropped suite reds it.

`ci` is an aggregate job (`.github/workflows/ci.yml`, job `ci`) with
`needs: [ci-matrix, lint, agent, agent-test, supervision-xml, changelog]`. The required context is
the aggregate, not the matrix legs.

## `require_extra_approval_for_unattributed_changes` does not mean "the approver wrote it"

The setting is `true` on both tiers. **It does not block self-approval of a commit you authored
yourself**, and the repo's merge habits have leaned on the belief that it does. See issue #298.

Measured on PR #281, whose newest commit was an `update-branch` authored by the same person who
then approved it as sole reviewer:

```
gh api "repos/skyphusion-labs/straightedge/rulesets/rule-suites?ref=refs/heads/main&per_page=30"
gh api repos/skyphusion-labs/straightedge/rulesets/rule-suites/4457387643
  after_sha 2e08d7678   actor skyphusion-mackaye   result: pass
  rule_evaluations: 18677665 pull_request -> pass        <- the rule ran and was satisfied
```

So this was **not** a bypass and nothing misfired. The reason is in the commit's attribution:

```
gh api repos/skyphusion-labs/straightedge/pulls/281/commits
  c148245d4  author.login = skyphusion-mackaye  mackaye@skyphusion.org  verified = true
```

The commit resolved to a GitHub account holding write access, so it was **attributed** and the
rule's trigger was never reached. "Unattributed" is about whether a commit can be tied to a
collaborator account, not about whether the approver wrote it.

**The setting for the case people assume is covered is `require_last_push_approval`, and it is
measured `false` on both tiers.** Nothing in the live configuration currently prevents approving a
branch whose newest commit you pushed. Do not cite this rule as the reason a procedure is safe.

What the rule gates *positively* is not established here; see issue #298 for why the obvious
experiment is harder than it looks.

## Repo admins bypass all of the above

```
gh api repos/skyphusion-labs/straightedge/collaborators
  skyphusion admin   skyphusion-mackaye admin   skyphusion-strummer admin
  skyphusion-rollins maintain   skyphusion-joan maintain
  skyphusion-ernst maintain     skyphusion-albini maintain
```

A single `PUT /repos/{owner}/{repo}/pulls/{n}/merge` from an admin merges an unreviewed, unchecked
pull request. Demonstrated by accident on the retired `crew-bus` repo, whose `pull_request` rules
are byte-identical to this repo's:

```
gh api "repos/skyphusion-labs/crew-bus/rulesets/rule-suites?ref=refs/heads/main&per_page=10"
  skyphusion-strummer  bypass  5bd577709
  skyphusion-strummer  bypass  fee23f9b8
```

Two things follow. **Do not admin-merge**; the gate will not stop you, so the gate is not what is
keeping this repo honest. And **a protection experiment run by an admin measures the admin's
bypass, not the rule**: run it as a `maintain` collaborator, or the positive and negative arms come
back identical and reassuring.

## Strict up-to-date serialises merges, one CI cycle per merge

`strict_required_status_checks_policy: true` renders every other open branch `BEHIND` on every
merge, whatever files it touched, so the invalidation is caused by the thing you were waiting for
succeeding. Measured cycle cost:

```
gh run list --repo skyphusion-labs/straightedge --workflow ci.yml --limit 50 \
  --json createdAt,updatedAt,conclusion
  ci.yml            n=45  min=179s  median=221s  max=374s
  code-coverage.yml n=47  min=91s   median=138s  max=151s
```

`ci` and `coverage` run in parallel, so one cycle is about `221s`, and a queue of depth N costs
about `N * 221s` of wall clock plus one human `update-branch` per step. See issue #292 for the
decision and its alternatives.

## If a merge queue is ever enabled, the workflows change FIRST

This is a deploy-ordering trap, and it bites permanently rather than loudly.

A queued pull request is tested by a run triggered on the **`merge_group`** event. Measured
2026-10-10, neither required workflow listens for it:

```
gh api "repos/skyphusion-labs/straightedge/contents/.github/workflows/ci.yml?ref=main"
gh api "repos/skyphusion-labs/straightedge/contents/.github/workflows/code-coverage.yml?ref=main"
  both:  on: push: branches: [main] / pull_request        <- no merge_group
```

A required context that never reports on `merge_group` never arrives, so the queued pull request
never merges and never fails. **Order: land the `merge_group` triggers on `main`, confirm both
contexts actually report on a queued run, and only then add a `merge_queue` rule to a ruleset.**

Two further items are known and unmeasured:

- The `ci.yml` `changelog` job branches on `github.event_name == 'pull_request'` and reads
  `github.event.pull_request.base.sha`, which does not exist on a `merge_group` event. That job
  already carries a comment about a skipped job failing the `ci` aggregate, so it needs an explicit
  `merge_group` path rather than a fall-through.
- Whether default-setup CodeQL reports on `merge_group` is **not measured**. The `code_scanning`
  rule is part of the gate, so this has to be settled before the queue is switched on, not after.

Merge queue eligibility: this repo is public and organization-owned, which is the eligible case on
the Team plan. Private repos in this org are **not** eligible on Team, so this is not an org-wide
answer.
