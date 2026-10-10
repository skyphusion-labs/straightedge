# Testing: what a green suite cannot see

`docs/CONTRACT.md` is the behaviour the suite enforces. This file is about the
suite itself, and it carries one lesson, measured four times in one sprint
across four different files and three authors.

Every instance produced a REASSURING result rather than an error. That is the
whole problem: the healthy state and the broken state rendered identically, so
nothing looked wrong at any point.

**SCOPE.** That sentence is also the boundary: everything here is about a check
that reports the reassuring state. **Nothing in this file covers the opposite
failure, a gate that REDS on a healthy system**, which is a real and separate
defect; we shipped one this sprint, a liveness audit that red on two tasks
installed thirty seconds earlier. A false alarm is found by the person it
interrupts, which is why it needs no document; a false reassurance is found by
nobody.

## The check

**For any fixture, name a reachable state where the right implementation and the
wrong implementation give DIFFERENT answers. If you cannot, the fixture proves
nothing.**

Run it before writing the assertion, not after the suite goes green. A suite
that cannot distinguish two implementations reports the reassuring one.

## The tell: the fixture sits at a boundary the code itself produced

This is the part that is hard to reconstruct from first principles, and it is
why the defect keeps recurring among people who already know the rule.

The fixture is not chosen badly. It is chosen by the SAME logic as the code, so
the two cannot differ there by construction:

- `lots_for_risk` sizes an order so its worst-case loss lands AT the per-trade
  cap. A fixture built by opening an order therefore sits exactly on the
  boundary every cap test wants to probe.
- `parse_fx` agrees with a naive `symbol[:3]` / `symbol[3:6]` split on every
  three-plus-three symbol. A fixture built from ordinary FX pairs therefore
  cannot tell a real lookup from a split.

In both cases the obvious fixture is the natural one, the suite goes green, and
the agreement is invisible.

## The four measured instances

**Each is labelled by how its BLINDNESS claim was established**, because a
document arguing that a property stated without its measurement is the problem
cannot itself mix two kinds of evidence silently. MUTATION means a guard was
removed and the suite observed; ARITHMETIC means the blindness was derived and
never observed as a red.

**straightedge#170, the freshly-sized position. [MUTATION, plus ARITHMETIC for
the empty band.]** `_stop_guard` measures a
widening against `min(per_trade, loss_room)`. Deleting the halt-room half left
all 1187 tests green. The band needs `loss_room < per_trade` AND
`worst <= per_trade` at once, which on the default config means equity below
9849 and at or above 9900: **empty**. Only a position whose volume is smaller
than today's cap would size (a scale-out, or one carried from a lower-equity
day) escapes it.

**straightedge#169 and #176, currency resolution. [MUTATION.]** Every planted book was
three-plus-three, so one test (`DOGEUSD`) was the SOLE instrument for two
separate properties: the aggregation inside `exposure_text`, and
`risk.currency_exposure` resolving rather than splitting. Reverting the shared
function to a naive split red exactly ONE test. After one fixture leg was
changed to a non-three-plus-three symbol: four reds for one property, five for
the other, with different red sets.

**straightedge#157 and #164, the resting order at the cap. [ARITHMETIC for the
empty band, with NO observed red; MEASUREMENT for the direction defect.]** The `#104`
regression pin proposed `limit - 0.001`, which moves the entry TOWARD the stop:
worst 50.00 -> 40.00, a REDUCTION. A test named for refusing a risk-INCREASING
replacement was asserting that a reduction is refused, and the increase
direction it was written for was never pinned. Flipping the direction is not
enough either, because an order sized AT the cap has no band for "an increase
that still fits the per-trade cap": any increase breaches `per_trade` too, so
the pre-fix arithmetic refuses it as well. The repair sizes the resting order
below the cap, which opens a real band (resting 20.00, `per_trade` 50.00,
proposal 26.00) and asserts the direction so a future reduction fixture cannot
pass through the carve-out unnoticed.

**straightedge#175, one layer out: the INSTRUMENT could not express the
failure. [MUTATION, with a control.]** The clamp `max(cap - abs(net), 0)` was unobserved by all eighteen
mutants run against #169. Its test helper matched rows with `room=(\d+)`, which
cannot represent a negative number at all, so removing the clamp made the row
stop matching and the test red on a `KeyError`. Measured both ways: both forms
go red, so a careless reviewer approves either, and **only the widened form
names the defect** (`reported room -1 for USD; the clamp did not hold` versus
`KeyError: 'USD'`). Reverting the widening alone leaves the clean suite green,
so it cannot hide a bug on its own. `risk_text`'s `room=` had the same shape:
asserted by a regex on the SHAPE, which a hardcoded `room=0.00` satisfies.

So: **a parser that cannot represent the broken state cannot observe the guard
that prevents it.** Same idea as the empty band moved one layer out, where the
FIXTURE cannot reach the discriminating case and the INSTRUMENT cannot express
it.

## An equivalent mutant is not a missing test

The check above is not a licence to manufacture a test for every surviving
mutant. Sometimes the obvious control correctly returns nothing because the
mutation is observationally equivalent, and then the right output is a recorded
reason, not a test.

Measured: moving `replace_pending`'s unmeasured-spec refusal BELOW the cap
decision leaves the suite green, and correctly so. Wherever that refusal sits
above `_modify_pending`, an unmeasured spec still refuses before anything is
sent, so the two placements cannot be told apart by any observation. Keeping it
above the arithmetic buys readability and future-proofing, not a live defect,
and the comment there says exactly that rather than claiming more.

Distinguishing the two cases is the skill. "Nothing reds" means either the
suite is blind or the change does not matter, and those want opposite responses.

**But "equivalent" is a property of the mutation AND the reachable fixture
space, never of the mutation alone.** This is the trap, and it is the one this
whole document exists to stop, so it is worth stating twice.

The halt-room mutation in #170 genuinely IS equivalent for a freshly-sized
position. Sizing puts worst-case loss AT the per-trade cap, so any widening
breaks the per-trade bound first and the `min()` term can never decide the
answer. Run the obvious control on the obvious fixture and nothing reds, and
that nothing is CORRECT. The same mutation is NOT equivalent once the volume is
smaller than today's cap would size, which a scale-out or a position carried
from a lower-equity day produces, and there the engine's verdict flips.

So a clean nothing does not license "equivalent, move on". It licenses exactly
one conclusion: **equivalent OVER THE FIXTURES I TRIED.** Before recording a
mutation as equivalent, say which states you reached and name the state class
you did not. #170 was recorded as a real gap only because someone widened the
fixture space after the first control came back empty; stopping at the clean
nothing was the near-miss, and it was one step away.


## If every row of a swept parameter gives the same answer, the parameter is not reaching the code

The section above is the case where nothing reds. This is the case where
EVERYTHING reds, identically, and it is a reviewer's instrument rather than a
test: the same failure reaches a probe written to settle a review, and that
probe has no suite to protect it.

**The uniformity IS the reading.** Measured while reviewing #182: a sweep
hunting a false refusal returned 32 refusals out of 32, at every bar age and
every staleness. The probe was wrong, not the code. `synthetic.generate_bars`
steps 3600 by default and the probe strided `start_ts` by 900, which put
`bars[-1]` eight days in the FUTURE, so every reading refused for a reason
unrelated to the subject. Trusting it would have filed a 32-case regression
against a correct safety check.

An all-refuse result that does not MOVE with its inputs is a broken instrument,
not a finding. So a probe wants two controls, not one: a case that must pass and
a case that must fail. With both present, a uniform answer is visibly a broken
instrument rather than a result, and the fixture's own premise is worth
asserting out loud, because the premise is what was wrong here and nothing in
the output said so.

**The same family reaches the plumbing around a probe, not only its fixtures**,
and it is worth naming because each instance looked like a clean negative:

- A mutation runner piped its summary through `head -14`. A widely-red mutation
  printed a long failure list, the summary scrolled past the cut, and 18
  failures read as NO RESULT. Fixed by printing the summary FIRST, so a
  truncated tail can never be mistaken for an empty one.
- A harness displayed the enclosing function of each mutation by matching on
  line CONTENT, so it confidently named the wrong function. Nothing was wrong
  with the mutation; the label was. Dropped in favour of `git diff -U0`, which
  cannot be wrong about what it changed.
- A census grep excluded string-method `.replace()` calls with
  `\.replace\(.[a-z]+., ` and thereby excluded `os.replace(tmp, self.path)`,
  dropping a filesystem call site from a population published as complete
  (straightedge#251). **A filter written to remove noise removed a member of
  the population.** Print the denominator and enumerate the exclusions: 19
  `.replace(` occurrences, eight `datetime.replace(tzinfo=...)`, five string
  operations, **one a docstring QUOTE of code**, seven real call sites. A count
  with its domain and its exclusions stated cannot hide a wrong filter; a bare
  "six sites" can.

- **A guard that PRINTS its verdict has not asserted it.** A pre-apply check
  computed `ok=0`, and the next line was `echo "SAFE: $ok"` chained to the
  apply with `&&`. **`echo` succeeds whatever it prints**, so the apply ran
  against a guard that had just said unsafe. The same guard was also the wrong
  instrument for its subject, being line-oriented over files that are one
  paragraph per line, and the correctness of what it let through was only
  established by re-measuring at word level afterwards. Two defects stacked:
  the verdict was never tested, and the verdict was wrong. Chain the ACTION
  behind the comparison, never behind a line that reports it, and remember that
  `$?` after a pipe is the PIPE's status, so a guard that pipes needs
  `${PIPESTATUS[0]}`.

Every one of those made a reviewer's instrument report the reassuring state,
which is this file's subject applied to the tools the reviewer brought rather
than to the code under review.
## A clean textual merge is not a passing merge

Two changes can auto-merge with no conflict and produce a failing suite. Git
tells you the text composed; only the suite tells you the meaning did.

Measured: #189 and #194 merged cleanly, and the combined
`tests/test_advice_exposure.py` failed, because one test asserted
`set(room) == {"EUR", "GBP", "USD"}` and the other renamed a fixture leg. The
assertion was coupled to a detail it did not care about. It now derives the
codes from the engine's own computation, so it moves with the fixture instead of
breaking on it.

When two branches touch one test file, run the combined suite before either
merges. `git merge --no-commit --no-ff <other>` then the suite, then
`git merge --abort`.


**The index is a third state.** A census that reads `git ls-files` sees the
INDEX, not the working tree and not the ref. Measured: after resolving a
`CHANGELOG` conflict in the working tree WITHOUT staging it, six
`test_venue_vocabulary.py` counts came back inflated, because `git ls-files`
emits a conflicted path once per unmerged stage, so the file was counted twice.
The census was right and the tree was half-merged. `main` being green on the
same tests is what separated the two readings, and a per-file count diff against
it located the duplicate in one step.
## An observation taken downstream of your own mutation is not evidence

Four of this sprint's wrong readings were the same act: looking at something
AFTER changing it and treating the result as independent.

- A working tree read while a mutation harness was still running against it. The
  file showed `_stop_guard` with the halt-room term missing and the comment
  still above it, which is exactly the regression one issue describes. It was
  the harness, mid-run, not yet reverted. **A working tree is not a measurement
  while something else is writing to it**; check the instrument EXITED before
  believing the artifact, the same way you check an exit code rather than
  trusting piped output.
- A pull request's `reviewDecision` read AFTER pushing to it. It said
  `REVIEW_REQUIRED`, which was the output of that very push dismissing an
  approval given fourteen minutes earlier. Read review state BEFORE pushing,
  never after.
- A revert-based mutation sweep run over UNCOMMITTED work. `git checkout -- src/`
  restores to HEAD, so every mutation after the first measured the unpatched
  tree, and the implementation was destroyed. A sweep that reverts must refuse a
  dirty `src/`.
- A mutation whose anchor did not land where it was aimed: one matched a line
  inside a COMMENT, another matched two sites and changed the wrong one, leaving
  a guard untouched while the run read as "verified". **A mutation you did not
  confirm landed on the line you meant is not a measurement.** Assert the
  enclosing function, or that the anchor matches exactly once, before writing.

- A correctly-aimed mutation measured through a **mis-specified baseline**.
  A reviewer established a real 6-to-1 change in a diff and attributed it to the
  wrong branch, because `git diff A..B` compares two TREES: on a branch that is
  BEHIND, every commit `main` has and the branch lacks appears INVERTED, as
  though the branch removed it. `A...B` is the changeset and matches GitHub's
  files endpoint.

  ```
  git diff --stat origin/main..<head>    6 files, 148 insertions, 207 deletions
  git diff --stat origin/main...<head>   2 files, 144 insertions   <- the changeset
  ```

  The measurement was real; the baseline was not. Its own diagnosis is the
  transferable part: it verified the MUTATION carefully, with correct aim and
  reproducible counts, and **inherited the SETUP without checking it.** Verifying
  the interesting half of a procedure while assuming the boring half is how a
  careful person gets a confident wrong answer.

A related trap with the same shape but no mutation of your own: **the record
lags the artifact.** Immediately after a push, a pull request can report the OLD
head and a stale `reviewDecision`. Poll until the reported head matches the sha
you pushed, then read the rest.

## A comment asserting a property the code does not have

Four instances this sprint, in four files, which makes it a pattern rather than
a coincidence. **A wrong comment has a longer half-life than wrong code,
because the code gets re-read and the comment gets believed.**

- `ADVICE_ACTIONS` said "both read this name so the two gates cannot drift
  apart". `parse_advice` did not read it; it carried its own literal set, so the
  documented invariant was unimplemented and widening the literal was invisible
  to every test.
- `_stop_guard`'s cap comment claimed the halt-room term "makes a spent daily
  budget refuse a widening by arithmetic alone", while no test could observe
  that half at all.
- A comment added with `replace_pending`'s carve-out claimed the unmeasured-spec
  refusal "has to come BEFORE" it or the carve-out becomes a second fail-open.
  Measured, the two placements are observationally equivalent; the narrower true
  claim replaced it.
- `VENUE_CLOCK_BAR_DISAGREES` claimed it makes a stale stamp "unreachable" when
  measured it only NARROWS, accepting an instant wrong by 900s at `bar_age=899`
  under a violated bound, because it compares against the bar's OPEN and is
  blind just before a bar closes.

A fifth, in a README rather than code, shows the cost directly: it stated that
IMPORTING a module moves the bundle numbers, when a bare import is tree-shaken
and only a retained use does. Anyone re-checking by that procedure sees zero
change and concludes the instrument cannot fire.

So when a comment states a property, ask what would red if the property stopped
holding. If the answer is nothing, the comment is a claim rather than a
guarantee, and it should say which.

**And check the claim at its EDGE, not its middle.** Every instance above holds
comfortably in the middle of its range and fails at a boundary:
`VENUE_CLOCK_BAR_DISAGREES` is correct for most of a bar and blind in the
instant before one closes; the cap comments are right while the book is
ordinary and silent on a book sized below the cap; the README claim is true of a
retained use and false of a bare import. A reading taken mid-range cannot
distinguish "holds" from "holds here", so test the first and last value a bound
admits, and the value just past it.

## An assertion over an empty collection passes for free

`all(...)` over an empty iterable is `True`, and a set-equality assertion
against an empty set holds when the thing under test produced nothing at all.
So any `all`-shaped or set-shaped assertion needs to be shown FAILING on the
empty case before it counts.

Worked example from this suite: `set(room.values()) == {0}` is sound precisely
because it also fails on an empty `room`, so it cannot pass by the exposure
block rendering no rows. That property is what separates it from
`all(v == 0 for v in room.values())`, which an empty book satisfies silently.

## The emptiness half of a both-directions gate is a bill, not a free check

A declaration that can rot gets repaired the same way every time: assert that
nothing UNDECLARED appears, and assert that nothing DECLARED is absent. Without
the second half the declaration decays into a list of things somebody once
intended, and the gate reports green over it. `KNOWN_BLIND_SEAMS` is asserted
for equality for exactly this reason, so progress updates it rather than hiding
in it.

**The second half obliges a FIXTURE for every declared member, and that cost is
invisible when you add it.** A member whose condition no fixture reaches cannot
be found absent, so it sits in the declaration unverified. The check that was
added to stop a wishlist is the one the wishlist entry now hides behind.

Worked instance, straightedge#287. The `advice_turn` row's keys are declared,
split into always-present and conditional, and asserted both ways. One
conditional member appears only when a journal rotation is refused. A fixture
driving every schema field large cannot see it at all, so the emptiness half
would have passed straight over it: present-and-undeclared would still red,
declared-and-missing would not. The gate therefore asserts on a MAXIMAL row,
with the rotation forced to fail, so every declared condition fires at once.
That is not completeness for its own sake; without it one declared member is
decoration.

**The rule: a conditional member may be declared only if its condition is
reachable from a fixture.** If it is not reachable, say so at the declaration
and do not declare it. Declaring it buys a green that proves nothing, and it
costs more than saying nothing would, because the gate standing over it implies
somebody checked.

**And do not build the escape hatch first.** The obvious mechanism is an
exemption set for unreachable members. An unused exemption set is an exemption
with no instance attached, which is worse than one with an instance, because
nobody can judge whether it is justified and there is nothing to re-read it
against. #287 needed none and got none. Add the mechanism when a real member
forces it, and name that member at the exemption.

This is the same family as two entries above. "An assertion over an empty
collection passes for free" is the degenerate case, where the collection is
empty and nothing is checked at all; this is the partial case, where most
members are checked and one is not, which is harder to see precisely because
the gate is doing real work everywhere else. And "a comment asserting a
property the code does not have" is what the unverified member becomes once
somebody reads the declaration as a guarantee.

## Two traps specific to this codebase

**Always probe a config value at a NON-DEFAULT setting.** Every exposure fixture
once ran at the default `max_currency_exposure = 2`, so replacing the config
read with a literal `2` satisfied the whole suite. Four constant-substitution
mutants survived. A fixture at the default cannot tell a read from a constant.

**Every non-three-character currency code in the table is crypto** (`AVAX`,
`DOGE`, `LINK`, `MATIC`, `SHIB`). So any fixture that discriminates a real
lookup from a three-plus-three split must use a crypto symbol. That is a
property of the code table, not a preference, and it is why "tidying" an
unprofessional-looking symbol out of a fixture silently removes the
discrimination. If you rename one, re-run the naive-split mutation and confirm
more than one test still reds.

## Assert a required check by NAME, never by the absence of failures

`[.check_runs[] | select(.conclusion != "success")] | length == 0` is computed
over the checks PRESENT. An empty list of failures and an empty list of checks
give the same answer, so that expression cannot see a required check that never
ran. Measured: the required `ci` context was ABSENT from a sha for four polls
while every `ci-matrix` leg was already green, and the pull request sat
`BLOCKED` with no failing check to point at.

The required contexts on `main` are `ci` and `coverage`, read from
`/rules/branches/main`. Assert both are PRESENT and successful, by name.

**An absence and a clean result are the same reading unless you name what must
be present.** This one is worth more than the others because it was found in a
CERTIFYING instrument rather than a measuring one: the others corrupted a
measurement, this corrupted the check that signs measurements off. Two seats
were narrating a count all day while the merge gate itself was sound, because
GitHub computes `mergeStateStatus` against the required contexts. The gate was
right and the narration was lucky, which is the distinction worth keeping: a
correct outcome does not retroactively make the reading that accompanied it
evidence.


**This entry had no mechanism when it shipped, and the honest resolution is
that the ENFORCEMENT already exists and is not ours: GitHub computes
`mergeStateStatus` against the required contexts, and a `BLOCKED` pull request
cannot be merged whatever any script of ours believes.** What has no mechanism,
and what this entry is actually about, is the hand-rolled SECOND reading that
gets built next to it. So the rule is: do not hand-roll it, and here is the
command, because an entry telling a reader to assert something without saying
how is the rot this file is about.

```sh
R=skyphusion-labs/straightedge
SHA=$(gh pr view "$PR" --repo "$R" --json headRefOid --jq .headRefOid)
# The contexts that GATE the branch, read from the rule rather than guessed:
gh api "repos/$R/rules/branches/main" \
  --jq '[.[]|select(.type=="required_status_checks")
         |.parameters.required_status_checks[].context]'
# Each one, by NAME, with ABSENT distinguished from PENDING and from a failure:
for c in $(…that list…); do
  gh api "repos/$R/commits/$SHA/check-runs" --jq \
    "[.check_runs[]|select(.name==\"$c\")|.conclusion]
     |if length==0 then \"ABSENT\" else .[0] // \"PENDING\" end"
done
```

**Measured, and this is the control that makes the rule worth a section.** On a
payload with no checks at all, which is exactly the four-poll window the
paragraph above describes, the two expressions disagree:

```
$ echo '{"check_runs":[]}' | jq '[.check_runs[]|select(.conclusion!="success")]|length == 0'
true
```

The broken expression reports **all-clear** on a sha where nothing has run, and
the by-name form reports `coverage=ABSENT ci=ABSENT`. One command, offline,
reproducible, and it is the whole argument: **the failure is not that the
expression is subtly wrong, it is that its answer is identical in the clean case
and in the empty case.**

**FIVE distinguishable states, measured, not two.** This entry first named
ABSENT and PENDING. Seven independent measurements by five seats over one
evening found three more, and a count is wrong in BOTH directions across them:

| state | what a rollup shows | how a count reads it |
| --- | --- | --- |
| ABSENT | the row is not there at all | green, because zero failures |
| QUEUED | row present, not started | green, same reason |
| present-but-IN_PROGRESS | row present, running | green, same reason |
| present-but-PENDING | row present, unfinished, no conclusion | green, same reason |
| COMPLETED | row present, conclusion set | the only one that is actually green |

The longest measured run of the first state was **twelve consecutive polls
reading zero failures while `ci` was simply not in the rollup**, roughly four
minutes. And the opposite reassurance was measured on the same pull request one
poll earlier: **every row present and none finished**, 12 of 12 not completed,
which reads as twelve rows of progress rather than as nothing having run.

**So neither a pass count nor a row count is sufficient, and they fail
differently.** A row count misses ABSENT; a pass count misses all four. That is
the reason to PRINT the row count as context.

**But do not compare it to a constant, and this is where a first version of
this entry was wrong.** The row count is a property of the BRANCH's workflow
file; the required contexts are a property of the RULESET. **Those are different
objects and only the second one gates.**

**One reading defeats both candidate floors at once**, measured on a currented
branch after the workflow gained a fifteenth job:

    rows=14  changelog=present  ci=ABSENT  coverage=SUCCESS  non_completed=2

Fourteen rows **with the new row already present**, and a required context
absent. A floor of 14 passes that while it is incomplete; a floor of 15 fails
it while it is legitimately mid-flight. **And the new row being present rescues
neither**, because its presence says which workflow the branch runs and nothing
about whether the run finished. The two objects visibly diverge in that single
line: the new row is a property of the workflow file and it is there, `ci` is a
property of the ruleset and it is not. Measured across the open pull requests
at one instant: three different row counts were live simultaneously, because
each branch runs the workflow its own head carries. A branch whose head predates
a workflow change keeps producing the old count until it currents, and that is
correct rather than a missing row.

A second counterexample shows the two criteria reaching the SAME verdict for
different reasons, which is the sharper half. A dependency bump whose head
carries an older `ci.yml` with five jobs **legitimately produces TEN rows**:

- **`rows >= 14` rejects it on the COUNT**, before anything is known about its
  conclusions, and would reject an identical branch that was passing.
- **`ci` present-and-SUCCESS rejects it because `ci` is FAILURE**, which is the
  actual defect: three of its rows fail on an upstream incompatibility.

**Same verdict, different reason, and only one of them generalises.** Note that
this branch is NOT passing, and that is the point rather than a caveat: the
count criterion happens to be right about it for a reason that has nothing to
do with why it should be rejected. A criterion that is accidentally right is
the hardest kind to retire.

**So the criterion is the by-NAME half on its own:** the required contexts
PRESENT and SUCCESS, the language-specific `Analyze` jobs COMPLETED, and zero
non-COMPLETED rows. Read which contexts are required from
`/rules/branches/main` rather than hard-coding them, as above.

**One measured edge on the conclusion half.** A row can be COMPLETED with a
conclusion that is neither success nor failure: on that same dependency bump,
`CodeQL` reads `status=completed conclusion=neutral`, measured through
`repos/{o}/{r}/commits/{sha}/check-runs` rather than off the rollup label. So a
gate asserting every row is SUCCESS rejects a branch that is fine, while
`zero non-COMPLETED` accepts it correctly. **Treat `neutral` and `skipped` as
acceptable conclusions and assert SUCCESS only on the contexts the ruleset
actually requires.**

**And `NEUTRAL` is the mirror of the defect this section opens with.** That
section's case is `select(.conclusion != "success") | length == 0`, satisfied by
an EMPTY list. The mirror is `conclusion != "failure"`, which a NEUTRAL row
satisfies while having neither run nor succeeded. Measured on that bump: seven
rows have a conclusion other than `failure`, and one of those seven is the
neutral one. **So a not-failure formulation passes a required context that did
not succeed, and the by-name criterion survives precisely because it asserts
SUCCESS rather than not-failure.**

That row was first described to this file as a sixth state, `skipping`, present
and never running. **It is not: it is COMPLETED with a neutral conclusion, and
the table above stays at five.** `skipping` is the CLI's one-word rendering of
two fields, which is why the instruction is to read `status` and `conclusion`
separately rather than trusting the single column. Recorded because the measurement is the only
reason to know that, and a sixth state added on one unmeasured observation
would have been this entry's own subject.

**And one field that cannot be used as a check in either direction.**
`closingIssuesReferences` on a pull request is empty for PRs that do close an
issue (measured: one closed its issue with the field empty), and a populated
field does not promise the close either, because the squash body is editable at
merge time. **So neither emptiness nor content is evidence**, and the only
reliable check is reading the ISSUE state back after the merge. That is the same
record-versus-artifact distinction as everything else here: the pull request
object is not the issue.

`repos/{owner}/{repo}/rules/branches/main` needs no elevated scope: it answers
for a plain collaborator token, which is why reading the rule is preferable to
hard-coding `ci` and `coverage` into a script that then cannot notice the rule
changing. Read **`/rules/branches/main`**, which answers "what GATES this
branch", and never `/rulesets`, which answers only "what EXISTS" and lists
rulesets that match no branch.

**What a CI mechanism would take, and why there is not one.** A workflow job
cannot assert its own siblings: inside the same run `ci` and `coverage` are
pending by construction, so the job would have to be keyed on the completion of
the others and would then be asserting, at merge time, the thing GitHub's own
branch rule already computes and already enforces. **A second implementation of
a gate that works is not a second gate, it is a second thing that can be
wrong** -- and it would be the one with no enforcement behind it. So this entry
is deliberately a PRACTICE with a runnable command, not a check, and it says so
rather than leaving a reader to discover it.
## A monitor outliving its subject reports a confident false green

A watcher that polls until something goes green has two ways to be measuring the
wrong thing, and neither announces itself. Both were hit in one night.

**The subject was IMPLICIT.** A watcher polling a pull request's checks read the
sha from the working tree. One clone served several branches, and checking out
another branch moved HEAD out from under it:

```
head=e42799ed...        <- this is origin/main, not the pull request
ci completed success / coverage completed success
rows NOT completed/success: (none)
all_required_green=yes
GREEN after 28 polls
```

It measured main, found main green, and reported the pull request green. **Every
individual reading is true and the conclusion is worthless**, which is the worst
shape a measurement can take, because there is nothing in the output to argue
with. Attribution is the sha, never the tree.

**The subject was PINNED and OBSOLETE.** Passing the sha as an argument fixes the
case above and not this one. A watcher armed at a sha that a later current
replaced is still polling, its checks still in flight, and on completion it
announces green for a commit that is no longer the head. **A sha-pinned watcher
is correct about a commit nobody cares about any more.**

**And the part worth keeping: the instrument had already been fixed for the first
reason, in the abstract, before it bit. The OLD COPY still in flight is what
bit.** Fixing a design does nothing while an old instance runs, and nothing in
the fix tells you an old instance exists. The leftover was found by enumerating
processes, not by remembering.

So the rule is the one `CLAUDE.md` already states for wake channels, and it
applies to any poller: **enumerate what runs against your own subject, reap
what is watching a subject that is gone, and arm exactly ONE PER SUBJECT.**
Re-arm means REPLACE, not add.

**"Arm exactly one" is the wrong spelling and the difference is not pedantic.**
Read as a count, it tells you to reap a live watcher on a DIFFERENT subject in
order to satisfy the number, which destroys a correct instrument to tidy a
total. Two watchers on two pull requests is the correct state and reads
identically to the broken one from a count alone. The defect is two on ONE
subject, where the stale one reports first.

So the check before arming is not "how many are running" but **"what is each
running one watching"**. That question also answers the first failure above,
because a watcher reading the tree cannot tell you its subject at all.

**Zero armed is a correct state** too, once every subject is measured and
nothing is left to watch.

This sits with the attribution entries rather than with the gate entries. A gate
that cannot fail is decorative; a monitor measuring the wrong subject is worse,
because it actively reports the answer you wanted about something you did not
ask about.

## An empty answer is the commonest disguise for a broken question

**An instrument that could not have produced a positive answer, reporting a
negative one.** Six of these in one evening, from five different tools, and in
every case the tempting reading was that the CLAIM was wrong rather than that
the QUESTION was.

| instrument | why it returned a confident nothing |
|---|---|
| `gh pr diff -- <path>` | returned empty for a path that WAS in the diff |
| a `grep` | case sensitive against content that differed in case |
| two `merge-base --is-ancestor` checks | unfetched object, so the ancestor was UNKNOWN, not absent |
| a sweep pattern | `os\.replace` missed `tmp.replace(dest)` |
| a mutation harness | printed `changed outcome: 0` while its mutation had FAILED |
| a dash check using `grep -P` | the flag does not exist on this seat's `grep`, so the check could not run |

The last one is the sharpest, because it was measuring this exact defect. Its
anchor no longer existed after the commit it was testing, so the mutation phase
re-ran unmutated code and **compared it with itself**. A zero meaning "I measured
nothing" presented as a zero meaning "nothing changed", inside a change whose
whole subject was assertions that pass vacuously.

**The last row is a different animal from the five above it, and the difference
is the useful part.** Those five are tools asked the wrong question. That one is
a tool that COULD NOT RUN, whose failure was converted into a pass by the shell
idiom wrapped around it:

```sh
check && echo ok || echo none          # never write this around a check
```

`cmd && echo ok || echo none` renders an unavailable flag, a missing file, a
typo in the pattern and a genuine clean result **identically**. It is not a
measurement error; it is an error-handling idiom that turns failure into the
reassuring branch by construction.

**And it is INVISIBLE FROM THE SEAT THAT WROTE IT.** The same command is a
working instrument where `grep` resolves to one that supports `-P`, and a
false-negative generator where it resolves to BSD grep, decided entirely by that
account's PATH. Measured both ways: on the authoring seat it found a planted
dash; on the other seat, given a file whose bytes are verifiably `e2 80 94` and
`e2 80 93`, the same command printed `none`. An author who writes and controls a
check on their own seat **cannot discover this defect**, because it passes its
own control exactly where it was written and manufactures passes only for
whoever inherits it.

So the control has a sharper requirement than "prove the instrument can return a
positive": **a shared check's control must run on the seat that RUNS it, not on
the seat that wrote it.** A control proven once by the author proves nothing
about anybody else's PATH. This is the copied-tool fork with no copy involved;
the thing that forked is the environment.

Three prescriptions, and they are the same failure at three distances: the
INSTRUMENT, the TOOL that reports it, and the REVIEWER who accepts it. **Each is
invisible from the position of the one before it:** a reviewer cannot see a
vacuous zero, a harness cannot see a reviewer's checklist, and an instrument
cannot see either. That is why these are one entry and not three unrelated
cautions.

**1. The instrument.** A negative result is evidence only after you confirm the
instrument could have produced a positive one. That is already doctrine here;
what it misses is that **an empty answer is the most comfortable possible
disguise for a broken question**, because it looks like good news and it costs
nothing to accept. So the check is adversarial on the INSTRUMENT, never on the
claim: before accepting a nothing, make the instrument find something you
already know is there. A scan that cannot find a planted needle is not a clean
scan.

**2. The tool.** A harness must **REFUSE TO REPORT when its own precondition did
not hold.** This is stronger than telling readers to check their instruments,
because **it moves the obligation from the reader to the tool**, and the reader
is the person least able to discharge it: a vacuous zero is indistinguishable
from a real one at the point of reading. The repaired harness greps the file to
prove its mutation landed before it compares anything, and exits rather than
printing a number it cannot stand behind.

**3. The reviewer.** **A document's payload IS its prescription, so review the
instruction and not only its hygiene.** Recorded as a review failure on the pull
request that landed the monitor entry above, because the instance is what makes
it checkable. Every hygiene check run on that review was the CORRECT check and
all of them passed: zero dashes, correct siting against the two sections it
generalises, no retracted wordings, a present-tense claim verified against
`main`, scope confirmed. **Nothing in that list reads the payload.** The entry
shipped telling a reader to reap a correct watcher to satisfy a count, and a
reviewer could run that whole checklist, pass it honestly, and still ship it.
That is what makes it a gap rather than a lapse.

## A control beats a second opinion

Two instruments agreeing is CORROBORATION. A control showing the instrument can
produce the other answer is PROOF. They are not the same strength and it is easy
to spend effort on the weaker one.

Worked example, from verifying one of the retractions above. Two independent
instruments said a patch did not touch a fixture: the files endpoint listed only
the two expected documentation files, and the raw patch contained zero lines
matching `LINKUSD|GBPUSD`. Both agreeing is consistent with the patch being
clean, and equally consistent with the grep being wrong. **The control is what
settled it: the same grep against `main`'s copy of that file finds six matches.**
So the instrument can fire, and its zero means absence rather than inability.

This is the same argument this file makes for tests, one level up, and it
applies to verification of any kind. If your evidence is "I checked it two
ways", ask what either way would have printed had the thing been true.

## A substitute that cannot fail the way the real thing does

Every other section here is about a check that cannot go red. This is the other
side of that family, and it is the one that cost the most: a **double** that
cannot fail the way the thing it replaces fails. The suite is green, every
assertion is real, and an entire class of defect is unreachable because the
stand-in cannot enter the state where it lives.

Measured, straightedge#232. `Desk.handle()` ended in
`except (ValueError, RuntimeError): return redact_text(str(exc))`. The operator
got a sentence; the journal got nothing. On the advice path
`record_advice_turn()` fires BEFORE `advisor.ask()`, deliberately, so a turn
that then raised had been charged against the operator's daily cap with no
record it was ever attempted: a budget that shrinks with nothing to point at,
in the money lane.

**1457 tests were green and none of them could see it.** Not because they were
weak. Because every transport double in the suite RETURNS A PAYLOAD, and a
payload cannot be an HTTP 502. The defect lived in the error class, not in a
value, so the entire suite exercised the parse-and-decide path and nothing
exercised the path where the parse never happens. It was found by a live run
against a real Worker under `wrangler dev`, which is the whole argument for
the live run being part of done rather than ceremony.

### The question to ask, which is not the one that comes naturally

The instinct is to ask whether the double is REALISTIC. That question is
unbounded and has no answer. Ask instead:

> **For each failure path a test claims to cover, can the double actually
> ENTER that state?**

Bounded, and answerable in a minute: read the real implementation, list what it
does when it fails, then check which of those your double can produce. From
#232, `UrlLibTransport.post_json`:

| mode | real failure | what it raises |
| --- | --- | --- |
| 1 | HTTP non-2xx | `TelegramError(f"telegram http {status}", status=, retry_after=)` |
| 2 | `URLError` (refused, DNS, timeout) | `TelegramError("telegram http failed")` |
| 3 | body is not JSON | `TelegramError("telegram non-json")` |
| 4 | provider returned an error field | `RuntimeError(str(data["error"]))` |
| 5 | reply empty | `RuntimeError("computer empty")` |

A double raising bare `RuntimeError` reproduces 4 and 5 exactly, because those
genuinely are bare `RuntimeError`s raised in `llm.py`. It only APPROXIMATES
1, 2 and 3: the real class is `TelegramError`, a `RuntimeError` **subclass**
carrying `status` and `retry_after`. Close enough to pass, and it proves the
handler journals when something raises **without** proving the handler is
reachable by the thing that actually raises. Those are different claims.

The repair needed no double at all: point the real `UrlLibTransport` at a real
closed loopback port, bound and released so it is certainly closed, and mode 2
happens for real inside real urllib. Offline, immediate, and the exception is
the real class (measured: `TelegramError`, mro
`TelegramError -> RuntimeError -> Exception`, message `telegram http failed`).
**When the real thing can be made to fail cheaply, that beats any double.**

### Three honest answers, and only three

When a double cannot enter a failure path:

1. **Make it enter the state** -- ideally by using the real implementation, as
   above, since a closed port and a bad payload are usually free.
2. **Disclaim the path in the test** -- say which modes are covered and which
   are not. A named gap is a finding; an unnamed one is a false claim.
3. **Cite a live run** -- a recorded run against the real thing is a peer of a
   test here, not a lesser substitute, and for some paths it is the only
   instrument.

What is not an answer is leaving the claim unqualified. The suite must not
claim coverage of a path no double in it can reach.

### The mechanism, and exactly how far it reaches

`tests/double_census.py`. It reads the seam names from the `Protocol` classes
in `src/` (so adding a method to `Broker` or `Transport` widens the census
rather than silently leaving a new seam uncounted), finds every class in
`tests/` implementing one, and reports which of those doubles contain a `raise`
at any depth.

```
$ python3 tests/double_census.py
seam methods implemented by a test double: 14
   post_json              doubles= 21  can_raise=  4
 * working                doubles=  3  can_raise=  0
...
SEAMS WITH NO FAILING DOUBLE (3):
  cancel, check_working, working
```

Exit 1 when some seam has doubles but none that can fail, 0 when every
implemented seam has at least one, and **9 when it found no `Protocol` classes
at all**, because a census that measured nothing is not a clean result.

A double with no `raise` is **not** automatically a defect. Most tests
exercise a happy path and should. What the census makes checkable is the
CLAIM: if no double for a seam can fail, **no DOUBLE in this suite** covers
a failure path through that seam, and one of the three answers above is owed.
Not "nothing covers it": a test that makes the REAL implementation fail covers
the path and the census cannot see it, which is the limit stated below.

**Now the limit, stated because this document is about claims that outlive
their evidence and a mechanism shipped with an unverified claim would be this
file committing its own subject.** The census would **not** have caught #232.
Measured: `post_json` already had four raising doubles at the time
(`test_desk.py:Boom`, `test_telegram.py:FakeTransport`, `SeqTransport`,
`Boom`), so the seam was not blind by this census's definition. What was
missing was narrower: no test drove the desk's error path and asserted the
record. So this census detects a strictly weaker condition than the defect
that motivated it -- a seam where NO double can fail -- and it is necessary,
not sufficient.

Catching #232's exact shape mechanically would need the CLAIM to be
machine-readable: a test that covers a failure path through a seam would have
to declare which mode it covers, and the census would then check that a double
can enter each declared mode. That is a real design and it is not built,
because it needs suite-wide adoption rather than one file. Until then the
question at the top of this section is applied by the author, and the census
only catches the blind-seam case. **Three seams are blind today**
(`cancel`, `check_working`, `working`), which is a finding this file is
raising, not one it has fixed.


## And the dual: a substitute that cannot SUCCEED the way the real thing does

The section above is a double too SOFT to fail. A double can also be too HARSH
to pass, and that one is harder to spot because it presents as a red: the test
fails, the fix looks wrong, and the thing that is actually wrong is the stand-in
demanding behaviour the subject must not have.

Measured, straightedge#242. The fix retries a `replace` that a concurrent
Windows reader refused. The first version of the `windows-latest` test held the
destination open across the whole call, so the retry spun for its full window
against a reader that never released, and then re-raised. **It failed on the
branch WITH the fix**, and it was the test that was wrong: a retry absorbs a
RACE, never a CONDITION, and a retry that waited out a permanent hold would
convert a rare dropped tick into a stopped desk. A sibling test in the same
file REQUIRED exactly the behaviour this one forbade, so the two halves of one
file asserted opposite outcomes.

**Neither POSIX run could have arbitrated, because on POSIX the conflict does
not exist at all** -- a rename over an open file succeeds there. Only the
platform could say which half was right, which is the same argument as the
section above from the other end: a double's fidelity is not reviewable from
the side that cannot reach the state.

Two things it changes about how to write one:

- **Split the two cases and assert both outcomes.** A hold that outlasts the
  window must SURFACE; a hold that lets go must be ABSORBED. A file whose two
  halves pin opposite results cannot be satisfied by making the retry
  unbounded, which is the repair a single over-harsh test invites.
- **Make the stand-in release on a SIGNAL, not a timer**, and only after a
  positive control has observed the real refusal. Then the test cannot pass by
  the conflict never having occurred, and it cannot fail by demanding the
  impossible.
## A consumer that rebuilds what it should call looks maintained and is not

Six instances in one evening, five from one seat and one independent, and **none
of them was found by running the suite.** Every one was a correct line of code
sitting beside another correct line of code that said the same thing, which is
the shape that survives review: there is nothing wrong to see.

| instance | what looked maintained | what was actually true |
| --- | --- | --- |
| a scanner gained a `composed:` output bucket | the gate filtered `("prose: ", "interpolated: ")` and all 20 tests passed | a concatenated refusal was surfaced by the instrument and dropped by the gate |
| the filter was derived from the declaration | one correct spelling | FOUR call sites each spelled it, so mutating one mutated nothing the others called |
| the comparison was shared via a helper | the pin and the injection test both called `non_word_sites` | each then built its own comparison against the pin, `==` in one and `!=` in the other |
| a probe measured an injected line | a delta against the real module | the baseline moved when another case injected into it, so a probe redded on a change it was not about |
| a verify-before-push script | it reported the in-tree bytecode count | it PRINTED the count and said `VERIFIED` regardless |
| another seat's pre-apply guard | it printed `SAFE: 0` | the apply ran anyway |

The fifth is the rule proving itself on its author: it was fixed in one script
and left in its sibling, **instance closed, class open**, on the same evening
that author was cataloguing the pattern in the gates. The sixth happened
independently, which is what makes this a mechanism rather than one person's
habit.

**It took three iterations to fix the first one, and each fix was correct.** The
filter was derived; then the four spellings became one predicate; then the two
comparisons became one function. Each closed the instance and left the class one
level up.

### Scoping is a property of the PAIR, not of either side

The sharpest version, because the same defect appeared in both halves of one
read/write pair on one night:

- **One gate scoped its WRITER and not its READER.** Its coverage check asked
  `word not in contract_text`, which passes on a mention anywhere in the file.
  15 of its 28 words were also named in prose, so deleting a table row left the
  suite green.
- **Another scoped its READER and not its WRITER.** Its `table_words` is
  section-anchored, correctly and for a stated reason, while its control removed
  a row with a whole-file `re.sub(..., count=1)`. Once a second table documented
  the same word EARLIER in the document, the removal deleted *that* row, the
  section kept its own, and the control redded with "the row was not removed"
  **while nothing was wrong with either table.** Diagnosed by position: rows at
  two offsets, the section spanning a third range, the deleted one outside it.

**A scope stated on one side of a read/write pair is not a scope.** Both sides
have to take it from one place, which is this rule again: the second control
rebuilt the bounds instead of calling the reader's. The repair extracted the
bounds into a helper, because a second spelling of them would have been the
same defect a third time.

### Why it survives review: the data is the missing discriminator

**A gate whose subject does not occur in the data cannot be validated by running
it.**

The `composed:` case took three iterations because **the repository held no
composed refusal site**, so every version, including the broken ones, behaved
identically on it. Only an injected site discriminated them. The missing
discriminator was the data, not the code, and no amount of care running the
suite could have supplied it.

That explains all six instances, which is why this is one rule rather than six:
the thing being gated was absent from the repository, or present only in another
test's injection, or benign on every run that happened.

**The same rule has a second face, pointing the other way.** A gate whose
subject is NON-DETERMINISTIC cannot be INVALIDATED by running it either: one
green proves nothing and one red proves nothing, which is exactly why a re-run
feels like evidence. And a third, measured on this repo's own counts: **a
denominator whose corpus everyone edits incidentally is stale by default.** A
repo-wide fenced-block count moved while its own pull request sat, because a
`CHANGELOG.md` entry landed carrying a fenced block. **Every PR touches that
file and none of their authors believe they are editing a counted corpus.**

So the instruction is **state the corpus, not the number.** A number needs
re-deriving by every reader; a corpus definition does not.

### What to do instead

1. **Call the shared body, never rebuild an equivalent one.** A second correct
   spelling is still a second source of truth, and it diverges silently on the
   day the subject first appears.
2. **Derive both halves of a read/write pair from one place.** A
   section-scoped reader with a whole-file writer is not scoped.
3. **Measure by injection, not by running the gate.** If the gate's subject is
   absent from the repository, a green run is not evidence; inject the subject
   and watch the gate red.
4. **Assert, do not print.** A step that echoes a number and continues cannot
   fail. Prefer the invariant you actually need: `before == after` is usually
   it, not `== 0`, because a zero assertion reds on pre-existing state and
   tempts a cleanup the harness may refuse anyway.
5. **State the corpus rather than the count**, for any figure drawn from a
   corpus that other work edits incidentally.

### The honest reach of this rule

It does not say duplication is always visible. It says a duplicate that
**diverges** is invisible while a gate **deleted** shows in the diff, and that
the two want different responses: the first needs a single body, the second
needs review. Conflating them produces a meta-test, which is another body,
which is the defect again.

**And the single-body fix has a measured limit.** On the gate above, hand-copying
the filter inside any shared body reds; a caller that stops calling the chain
(`drift = ()`) does not, and leaves every test green. That is recorded in the
docstring rather than closed, because closing it needs the extra body the shared
one exists to remove. **So the claim is the narrow one: there is no second
comparison to drift, not that the gate cannot be removed.**

## Execute the documentation, do not review it

Every procedure in a document rots silently, because reading one cannot tell a
correct instruction from a stale one. The instances in this file include a
README whose stated procedure produced zero and would have convinced a
re-checker that the instrument could not fire.

Reviewing prose catches wording. Only running it catches rot. So when a document
states a command, run the command; when it states a count, reproduce the count.

**But measure whether your documents contain anything a script may safely run,
before trusting a script to check them.** And **state what you counted over**:
a count without its domain is not a measurement, which is the
lines-versus-occurrences trap one level up. Both domains, counted with the same
regex the script below uses, over the files each `git ls-files` names, on this
branch:

| domain | command naming the domain | files | with fenced blocks | blocks |
| --- | --- | --- | --- | --- |
| `docs/*.md` plus `README.md` | `git ls-files 'docs/*.md' README.md` | 11 | 6 | 45 |
| `**/*.md` across the repo | `git ls-files '*.md'` | 18 | 12 | 69 |

**Those figures are MEASURED AT THE TIP THAT CARRIES THEM, not predicted for a
future merge, and that change is deliberate.** Earlier versions of this
paragraph pinned the counts to a `main` that did not yet contain this file and
then stated the delta merging it would produce. That design has now rotted
three times in a row, so the table states what the commit it ships in actually
contains, and there is no arithmetic left to go stale.

**The three readings are worth keeping, because together they are the argument
for the rule rather than an anecdote.** At `f98984b` the row read 18/11/57 and
predicted 59 after this file merged. It merged, and the repo-wide figure was
**62**. While the pull request adding the section above was open, #240 landed
and it became **64**. It reached **68**, and then **69** while the pull request
carrying THIS sentence was open, because a `CHANGELOG.md` entry arrived
carrying a fenced block. **That is a fourth movement with nothing in this file
changing, and it came from the file nobody thinks of as documentation.**
The `docs/` row, meanwhile, hit its predicted 40 exactly
and has only moved by this file's own additions. So the broad domain moved
three times without this file changing at all, and the narrow one never moved
except when it did: **a markdown commit may move the count and may not, which
means you can infer neither staleness from the fact that documentation changed
nor freshness from the fact that this file did not.** Re-derive, or quote a ref.

Past that, do not read the number, run the command, because **both rows count
THIS file, so both move when it does.** That is the controls table's
self-inclusion one domain wider.

**An unpinned corpus count is stale by default, and this one went stale twice
while the pull request was open.** An earlier version of this row read 56,
correctly, until `#205` landed three fenced blocks in `agent/README.md` (2 to
5, so 56 to 59) with nothing in this file changing. Then `#206` touched
`docs/DEPLOY.md`, which is in both domains, and moved nothing at all: that file
holds 0 blocks at both refs. So a markdown commit MAY move the count and may
not, which means you cannot infer staleness from the fact that documentation
changed, nor freshness from the fact that this file did not. Re-derive, or
quote a ref. The row that used to sit here read 41 and 19 labelled as measured
on `main`, and matched no tree, because it was taken from a working tree.

In both, the command-shaped lines inside fences are `python -m straightedge run`
and its siblings (`backtest`, `watch`, `supervision`), which start or drive a
desk; `launchctl` and `powershell -File Install-Supervision.ps1`, which mutate
the machine; `export` and `source`, which set credentials; and desk chat
commands such as `/trail`, which are not shell at all. **Not one is safe to
execute automatically.** A verifier that ran them would be far worse than none.

One precision, because the first version of this paragraph was loose: **bare
`doctor` IS offline and read-only**, so it would be safe, but it appears only in
PROSE, in README's numbered steps, and never inside a fenced block. The script
reads fences only, so it never sees it. `doctor --connect` opens a venue
connection and `run` starts a desk; attributing that to `doctor` was wrong.

So on this repo the automated half checks read-only commands (`git`, `pytest`,
`ruff`, `mypy`) and legitimately finds none to run, which is a property of the
documents rather than of the tool. The manual half is the one that applies
here: when a document states a command you cannot safely automate, run it
yourself and reproduce its stated output, and when it states a count,
re-derive the count.

The script below is still worth having, both for the documents that do carry
read-only commands and because its exit code makes "nothing was checked"
distinguishable from "nothing was wrong":

```
python3 - "$DOC" <<'EOF'
import re, subprocess, sys, textwrap
src = open(sys.argv[1]).read()
# INDENTED fences count. The first version of this script anchored the fence at
# column 0 and silently skipped every block nested in a list, which is most of
# them. See the note below.
blocks = re.findall(r"^[ \t]*```[a-zA-Z]*\n(.*?)^[ \t]*```", src, re.S | re.M)
print(f"fenced blocks: {len(blocks)}")
ran = failures = 0
for i, b in enumerate(blocks, 1):
    for line in (l.strip() for l in textwrap.dedent(b).splitlines()):
        if not line.startswith(("git ", "pytest", "ruff ", "mypy ")):
            continue
        if "<" in line:   # illustrative: carries a <placeholder>, not runnable
            print(f"block {i}: SKIP (placeholder)  {line}")
            continue
        r = subprocess.run(line, shell=True, capture_output=True, text=True)
        ran += 1
        failures += r.returncode != 0
        print(f"block {i}: exit={r.returncode}  {line}")
print(f"command lines executed: {ran}")
# NOTHING CHECKED IS NOT A CLEAN RESULT. Without these two exits the script
# prints its own definition of broken and returns 0, which is the defect this
# section describes, in the tool this section ships.
if ran == 0:
    sys.exit("no command was executed: this is a broken check, not a clean document")
sys.exit(1 if failures else 0)
EOF
```

**A third defect, found by the reviewer EXECUTING it rather than reading it.**
Run as written on the document it ships in, the first version printed
`command lines executed: 0` and exited 0, which is this section's own
definition of a broken check reported as a clean one. The reviewer also ran it
against every other document in the repo and got zero executions in all of
them, so the claim that a block "can be lifted and executed directly" was not
demonstrated for a single document here. Its positive control, a scratch
document holding three passing commands and one failing one, returned 2 blocks
and 4 executions with the failure showing `exit=128`: **the instrument can
fire, and the repo simply has nothing it may safely run.** Hence the exits
above, and the survey before the script rather than after it.

**Print the denominator, and this script is why.** Its first version anchored
the fence at the start of a line, so it matched exactly ONE block in this file:
every other one is nested in a list item and therefore indented. It executed
zero commands and reported no problems. A reader would have concluded the
document was verified; what actually happened is that the instrument could not
see what it was pointed at. That is this file's own subject, reproduced in the
tool this file ships, within minutes of writing it. Hence the two counts in the
output: **a run that executes zero commands is a broken verifier, not a clean
document.**

**Running it found a second defect in it**, which is the section's argument
again: with the fence fixed it executed both lines of the `git diff` example and
reported two failures, because those lines carry a `<head>` placeholder and were
never runnable. A verifier whose only output on a healthy document is two false
alarms gets ignored, and an ignored check is a decorative one. Hence the
placeholder skip, and hence the SKIP lines in the output: what it declines to
run is as much a part of the reading as what it ran.

**Run as written, with all four controls, which is the standard this section
sets for itself:**

| target | rc | reading |
| --- | --- | --- |
| `TESTING.md` | 1 | 2 blocks, 2 SKIP, 0 executed -> broken check, not a clean document |
| `control.md` | 1 | 2 blocks, 4 executed, deliberate failure shows `exit=128` |
| `unsafe.md` | 1 | 1 block, 0 executed (`launchctl`, `python -m straightedge run`) |
| `all_pass.md` | 0 | 2 blocks, 2 executed, both `exit=0` |

**That table is not a fenced block, and it used to be.** While it was one, this
file held THREE blocks and the row above reported two, so **the act of
documenting the denominator changed the denominator.** A stale count, in the
section whose rule is print the denominator, produced by printing it. Caught by
a reviewer running the shipped script against the shipped file. It is now a
markdown table, which both renders better and keeps the row describing the file
rather than describing itself; if you add a fence here, re-run and update the
row.

**It went stale AGAIN, and this time nothing in this paragraph changed.** The
sentence above used to read "the 2 are the `git diff` example far above and the
script just above, and that is the whole population". **Measured on
`origin/main` before this change: THREE.** #244 added a
`python3 tests/double_census.py` output block in the section on substitutes, and
the count here was not part of that diff, so the row describing this file was
wrong the moment a section was appended to it. That is the third time this
figure has rotted, and the second time it rotted while every word about it
stayed put: the de-fenced table removed the self-reference and did not remove
the dependence on the rest of the file.

**So stop asserting the number in prose.** The blocks are enumerated below
because an enumeration is checkable line by line, where a bare integer is only
checkable by re-deriving it, and re-derive is exactly what nobody does:

In file order, which is the order the script numbers them in:

| # | what it is | command-shaped lines | executed |
| --- | --- | --- | --- |
| 1 | the `git diff --stat` pair, an example with its output | 2 | 0, both carry `<placeholder>` |
| 2 | the required-contexts `gh` commands | 0 | 0, `gh`/`for`/assignments are not recognised |
| 3 | the empty-payload control, a `jq` line and its output | 0 | 0 |
| 4 | the `double_census.py` output | 0 | 0 |
| 5 | the verifier itself | 0 | 0 |
| 6 | the verifier's own `exit=1` output, quoted above | 0 | 0 |
| 7 | the domain command below, added so this file is not a document the verifier refuses | 1 | **1** |

**Seven, and only the last one runs.** That ratio is the honest reach of this
tool restated: it recognises four command prefixes and skips anything carrying a
placeholder, so most of what looks like a command in a document is correctly
invisible to it. The row to watch is the last column summing to at least 1.

**AND THE SHIPPED SCRIPT EXITED 1 ON THIS FILE, which is worse than a wrong
count.** Run verbatim against `docs/TESTING.md` at `origin/main`:

```
fenced blocks: 3
block 1: SKIP (placeholder)  git diff --stat origin/main..<head> ...
block 1: SKIP (placeholder)  git diff --stat origin/main...<head> ...
command lines executed: 0
no command was executed: this is a broken check, not a clean document
exit=1
```

Every command-shaped line in the file carried a `<placeholder>`, so the
script's own `ran == 0` guard fired and reported the document broken. **The
guard was right and the subject was this file.** A verifier that cannot pass on
the document it ships in teaches a reader to ignore its exit code, which is the
one thing a self-checking document cannot afford. One runnable line fixes it,
and it is the command the domain table already names:

```sh
git ls-files 'docs/*.md' README.md
``` Name the method as well, because this file is a
case where both obvious ones lie: `grep -c` on the fence marker reports 5,
since it counts matching LINES and one line carries two markers; `grep -o` of
the same marker piped to `wc -l` reports 6, since it counts OCCURRENCES and two
of them are a string literal inside the script rather than a fence. The script
counts PAIRS anchored at line start, which is the right domain for a fenced
BLOCK and the only one of the three that returns 2.

The last row is the one most easily skipped: **a check that cannot return 0 is a
gate that can never pass**, which is as useless as one that can never fail, and
the only way to know is to build a document it should pass on.

Two honest limits that remain, because a verifier overstating its reach is also
this file's subject. It only runs lines it recognises as commands, so
prose-with-numbers is untouched; and a non-zero exit is a prompt to look, not
proof of rot. It catches the command that no longer parses or no longer exists,
which is the failure mode that actually occurred.
