### The row bound's DERIVATION is gated, not described (issue #306)

**#283 added a row field, and #287 corrected the derivation to price it. Neither
figure in that corrected derivation was read by anything.**

`RECORD_ROW_BOUND` has been pinned against `docs/CONTRACT.md` since #226, which
measured the constant being raisable to 1024 with the whole suite green while
the document still said 512. #287 re-derived that bound as "538 measured for the
maximal row plus 42 bytes of flat margin" and handed the derivation two more
figures with exactly the property #226 removed from the first one: 538 and 42
lived in a comment, in a contract cell and in a changelog fragment, and nothing
measured either.

```
maximal advice_turn row, measured          538 bytes
the same row with no deferred rotation     516 bytes
what `rotate_deferred` costs                22 bytes
`RECORD_ROW_BOUND`                         580
```

**Three constants, and a test that measures the row against them.**
`journal.MAXIMAL_ROW_BYTES`, `journal.RECORD_ROW_MARGIN` and
`journal.ROTATE_DEFERRED_ROW_BYTES`. The maximal row is measured and asserted
EQUAL to the first, the two sum to the bound, and the derivation cell in
`docs/CONTRACT.md` is read back rather than trusted.

**Why this is not a second copy of the ceiling assertions.** `<= 580` answers
whether the row FITS. The derivation claims something stronger, that the row
measures 538 and the other 42 bytes are margin nobody has spent, and the unspent
margin is the entire reason the next field is safe to add before somebody
re-measures. A field could cost 60 bytes, the row would still fit, every ceiling
assertion would stay green, and the margin a later author reads about would be
gone. That is the shape `rotate_deferred` already had once.

**What `rotate_deferred` costs is asserted as a DELTA**, between the maximal row
and the same row with the rotation left alone, and also against
`len(json.dumps({"rotate_deferred": 1}))`, which is where 22 comes from: the
key, its quotes, the two default separators and the value. Pinning the
expression is what makes a rename or a widened value red with the new cost
named. Pricing the key inside the test instead would have been a second
definition of the worst case, and it would have forked from the fixture the
moment the row changed.

**`RECORD_ROW_BOUND` is still its own literal** rather than
`MAXIMAL_ROW_BYTES + RECORD_ROW_MARGIN`. A sum is true by construction, so it
can never go red, and the thing worth catching is a STALE measurement rather
than an arithmetic slip.

**One measurement detail, stated because it is a substitution.** `ts` is the
only variable-width value on the row, and `datetime.isoformat()` drops
`.ffffff` when `microsecond` is exactly 0, which makes the field seven bytes
shorter about one run in a million. The exact measurement substitutes the widest
legal timestamp and asserts the live one is never wider, because an exact gate
that is occasionally wrong about the worst case teaches its reader to re-run it
instead of believing it.

**Verified by mutation**, not by the suite passing: the deferral never firing,
the measured figure off by one, the margin off by one, the bound shrunk, the
field renamed, and an undeclared new row field added in `desk.py` were each
driven and each reds. The new-field case is the one these gates exist for, and
before this change it was green.
