### The row bound now answers for the ROW: its fields are declared and gated (issue #287)

**The bound's derivation claimed coverage it did not have.** `docs/CONTRACT.md`
said the bound is "derived from `ADVICE_PROPERTIES` rather than from a
hand-kept list, so a field added there is covered without anyone remembering
to". Measured, a field added to `ADVICE_PROPERTIES` and nothing else
contributes **0 bytes** to the row:

```
worst-case advice_turn row, schema as shipped            516 bytes
schema + ADVICE_PROPERTIES['trail'] = number|null        516 bytes
delta                                                      0
'trail' present as a ROW key                           False
```

The reason is the last line. The fixture derives the INPUT space from the
schema; the ROW's fields were enumerated by hand in `desk.py`. **The hand-kept
list #226 argued against had been relocated into the writer, not removed**, and
that made it less visible than when it sat in a test, because a test at least
advertises itself as a list.

**The row's keys are now declared and gated in both directions.**
`journal.ADVICE_TURN_ROW_FIELDS`, `ROW_ENVELOPE_FIELDS` and
`CONDITIONAL_ROW_FIELDS` declare them next to the bound that governs them, and a
test asserts the real row against the declaration: a field journalled without
being declared reds, and a field declared that nothing journals reds too. The
second direction is not symmetry for its own sake. A declaration nobody checks
for emptiness decays into a wishlist, which is exactly how the hand-kept list
failed.

The schema is NOT the source, and forcing it would be wrong: the row carries
`event`, `ts`, `provider`, `session`, `staged`, `degraded` and `nonfinite`,
which no model may say, and the schema carries `text` and `summary`, which
nothing journals. They are legitimately different sets, in both directions.

**The gate asserts on the MAXIMAL row**, every schema field large at once plus
all four price fields non-finite plus the clipped symbol plus a deferred
rotation. Without the deferral the fixture cannot see `rotate_deferred` at all,
and that invisibility is what let a 15 character non-numeric field walk past an
invalidation list that enumerated only the numeric family.

**The invalidation list now names the general condition, ANY new row field.**
The special cases stay, because each names a cost that is easy to under-price,
but the list used to consist only of them and `rotate_deferred` matched none of
them while spending 22 bytes of margin. A reader who finds no matching entry
concludes there is nothing to re-derive, which is the list working as written
and failing at its job.

**And the margin is now FLAT rather than a per-field allowance.** 580 is
re-derived as 538 measured for the maximal row plus 42 bytes of flat margin,
replacing "516 plus 64 bytes of headroom for one more non-finite-capable
numeric field". **The number does not move.** The old wording read as a budget
that has to be tracked, and when `rotate_deferred` drew 22 of the 64 the
sentence still described the whole allowance, because nobody does the
arithmetic on the way past. A flat margin has no balance to keep, and
re-deriving now means measuring the maximal row again rather than adjusting a
remainder.

**The contract row is split into four.** The figure, its derivation, what
invalidates it, and the rows exempt from it were one 4.4K table cell, which is
how it came to hold a claim and its own refutation about 2,000 characters apart
with neither visible from the other. A reader checking one of four claims
should not have to read the other three.

Two things measured and deliberately NOT changed. `RECORD_ROW_BOUND` is still
correct and 538 is inside it, so nothing was nudged to fit. And the fixture gap
this issue reported for `rotate_deferred` was already closed by #283, which
drives a refused rotation and asserts the row stays in bound; this change
asserts that row's KEYS rather than its bytes, so the two do not overlap.
