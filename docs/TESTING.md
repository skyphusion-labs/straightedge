# Testing: what a green suite cannot see

`docs/CONTRACT.md` is the behaviour the suite enforces. This file is about the
suite itself, and it carries one lesson, measured four times in one sprint
across four different files and three authors.

Every instance produced a REASSURING result rather than an error. That is the
whole problem: the healthy state and the broken state rendered identically, so
nothing looked wrong at any point.

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

**straightedge#170, the freshly-sized position.** `_stop_guard` measures a
widening against `min(per_trade, loss_room)`. Deleting the halt-room half left
all 1187 tests green. The band needs `loss_room < per_trade` AND
`worst <= per_trade` at once, which on the default config means equity below
9849 and at or above 9900: **empty**. Only a position whose volume is smaller
than today's cap would size (a scale-out, or one carried from a lower-equity
day) escapes it.

**straightedge#169 and #176, currency resolution.** Every planted book was
three-plus-three, so one test (`DOGEUSD`) was the SOLE instrument for two
separate properties: the aggregation inside `exposure_text`, and
`risk.currency_exposure` resolving rather than splitting. Reverting the shared
function to a naive split red exactly ONE test. After one fixture leg was
changed to a non-three-plus-three symbol: four reds for one property, five for
the other, with different red sets.

**straightedge#157 and #164, the resting order at the cap.** The `#104`
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
failure.** The clamp `max(cap - abs(net), 0)` was unobserved by all eighteen
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

## An assertion over an empty collection passes for free

`all(...)` over an empty iterable is `True`, and a set-equality assertion
against an empty set holds when the thing under test produced nothing at all.
So any `all`-shaped or set-shaped assertion needs to be shown FAILING on the
empty case before it counts.

Worked example from this suite: `set(room.values()) == {0}` is sound precisely
because it also fails on an empty `room`, so it cannot pass by the exposure
block rendering no rows. That property is what separates it from
`all(v == 0 for v in room.values())`, which an empty book satisfies silently.

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
