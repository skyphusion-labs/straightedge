### One rule for reading a venue_clock row (issue #225)

The row had two conventions for "there is no offset": `offset_sec` was OMITTED
when the clock could not be read, while `previous_offset_sec` was written as
JSON **null** when the previous reading could not be read. Both are now omitted,
so the whole row obeys one rule: **a key that is present carries a measured
value.** `previous_unmeasured` already names what the previous reading was
missing, so no information leaves the record.

This matters because the row exists to be parsed after the fact. A parser tests
an omitted key with `in` and a nullable one with `is not None`, and the second
form silently misreads a row written by a desk that omitted the key: a field
that can be absent OR null has two absent states and nothing distinguishes
them, which is the version-skew shape #206 documents.

The only case that changes is the unmeasured-to-measured transition, the one
place the key appeared with no value. Measured-to-measured still states the
number, which is what makes a DST roll legible as a pair, and the existing
transition tests assert that unchanged, so the fix cannot have been applied too
widely. `docs/CONTRACT.md` and `docs/RUNBOOK.md` both state the rule now, the
runbook in the terms an operator reads the file in.

Driven red first, against the real row: the failure printed
`'previous_offset_sec': None` beside `'previous_unmeasured': ['server_time']`.
The test asserts the transition it is standing on before it asserts the absence,
because a fixture that never reaches the unmeasured-to-measured path would pass
the absence check by accident. Restoring the unconditional write reds exactly
that one test and nothing else.
