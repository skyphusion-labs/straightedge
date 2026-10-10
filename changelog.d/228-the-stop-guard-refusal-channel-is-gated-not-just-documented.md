### The stop-guard refusal channel is gated, not just documented (issue #228)

#220 closed the `refused: <word>` channel with a scan that has a denominator.
`_stop_guard`'s words are just as operator-visible and sat outside every scanner
in `tests/refusal_scan.py`: they do not follow `refused: `, and no
`RiskDecision` carries them. The guard returns a bare word, `_modify` hands it
to `OrderResult.invalid_stops`, and the desk renders
`sl failed retcode=10016 <word>`.

**The record was already complete and the GATE was missing, so no
operator-visible behaviour moves here.** All three words were documented in the
`/sl` row. Nothing read that documentation, so a fourth word, or a rename of any
of the three, would have reded nothing.

**Three words, and the issue said two.** The issue measured by grepping for
`stop_removal_refused` and `stop_exceeds_risk`, the two words named by module
constants. `_stop_guard` also returns `spec_not_measured:<fields>`, built by
concatenation, so a grep keyed on known names could not have found it. An
enumeration that starts from the names somebody already knows is not a
denominator, which is the same failure one level up from the one the issue was
filed about.

**FIVE replies can carry one of these words, not just `/sl`.** `_modify` is
reached by `/sl`, `/tp`, `/replace`, `/be` and `/trail`, so the same refusal
arrives behind five labels. The test derives that set from the source (a method
qualifies when it both calls a modifier and renders `<verb> failed retcode=`)
and pins it, so a sixth command reaching the guard fails there. `cancel`,
`close` and `closeby` render the same way, cannot carry one of these words, and
are pinned as the negative half, because a derivation returning every render
would satisfy the positive assertion and prove nothing. On the auto path
(`_act`, `_manage_open`) there is no reply at all and the refusal is journaled
as `modify_refused`; `docs/CONTRACT.md` now says to reconcile that from the
journal rather than from chat.

**The gate is anchored to its own table, and this channel is where that stops
being a precaution.** All three words appear in the `/sl` row's PROSE, so a
check asking `word in contract_text` passes with the new table deleted
entirely: measured 3 of 3 True, where the table-anchored check reports all
three as undocumented. #220 measured the same shape at 15 of 28. The section is
a LEVEL-2 heading on purpose, because #220's scan runs from
`### Refusal reasons` to the next `## `, and a `###` table here would have been
read as part of that enumeration, surfacing every word below as a row with no
code behind it.

**Prose is reconciled, not pattern-matched.** `_modify_pending` refuses with
three sentences (`sl required`, `buy needs sl < entry < tp`,
`sell needs tp < entry < sl`). Nothing asks whether a comment LOOKS like a
word: the population of `invalid_stops` comments minus the guard's vocabulary
must equal the pinned set exactly, so a new comment is either a word that needs
a row or prose that needs a decision, and it cannot be neither.

**One condition turned out to have two spellings**, `refused: sl_required` on
the order path and `sl failed retcode=10016 sl required` on the working-order
modify path. Both are documented. Renaming one changes an operator-visible
reply, so it is filed as #228's follow-up (#233) rather than slipped in beside
a gate.

Six mutations, each red where it should be and nowhere else: a deleted table row
reds 2, a renamed word reds 3, a fourth word reds 2, a new prose comment reds 1,
a verb that stops reaching a modifier reds 1, and deleting the whole section
reds 3. Restored, 7 passed. Run with `PYTHONDONTWRITEBYTECODE=1` and a fresh
`PYTHONPYCACHEPREFIX` per run, with the `.pyc` count as corroboration rather
than as the guard, and each verdict read from `returncode`.

**A review measured six spellings the gate was blind to, and all six now fail
rather than pass as covered.** The scan reads a return that is a literal, a
module constant, a literal-prefixed concatenation or a literal-first f-string.
It cannot read a word returned through a local variable, a `"".join([...])`, a
`str(...)` call, or a concatenation whose left side is not a literal. The repair
is not to teach it every spelling: each of those lands in the scanner's
`forwarded` set rather than being dropped, so `forwarded` is pinned EMPTY for
the guard and pinned to exactly the one hand-off site for the factory. An
unreadable return is now a failure that says it could not read, which is the
honest state for a scanner, where "green" would have been a false claim of
coverage.

The sixth was a result built with the `OrderResult` constructor instead of a
factory, invisible to the comment scan by construction. That is closed
structurally rather than excluded: `engine.py` reaches every result through
`measured`, `unchanged`, `not_sent` or `invalid_stops`, zero direct
constructions, and a test keeps it that way.

Each spelling verified by injection, not by argument: a local variable, a
`join`, a call, a non-literal concatenation, a second forwarded comment, and a
direct construction, six mutations, each red and none of them green. Both the
test module and the contract section now state what the gate reads and what it
cannot, per the ruling on #220's scope note.
