### The advice row is bounded by construction, not by fixture (issue #226)

#216 bounded the degrade REASON and left the same exposure one field over.
`symbol` is model-chosen on the advice path, which is the premise of #197, and
the `advice_turn` row wrote it verbatim: a 5600 character symbol gave a **5838
byte row** against the 512 byte bound this repo asserts, with `degraded` empty.

**And #216's own bounded-row test passed while that row was reachable**, because
it varied `text` and `summary`, neither of which is echoed. That is the second
time a bound on this row was asserted by FIXTURE rather than by construction,
and the first was the defect #216 exists to fix. So the fix is not "bound
`symbol` too".

**Every schema field is driven large AT ONCE, from the code's own table.** The
fixture is derived from `ADVICE_PROPERTIES`, which is the table the request is
built from and the one `_schema_violations` validates against, so a field added
there tomorrow is driven large by these tests without anyone editing them. Both
providers are covered, and the `grok` path is the harder one: the claude path
forces `hold` on a violation, which could mask the bound by blanking fields,
while the bare parser carries its own output.

**A clipped string says what it dropped.** `clip_for_record` keeps
`RECORD_STRING_CHARS` and appends the count, because a silently truncated value
reads as the whole value and a reader could not tell `EURUSD` from a 5600
character string beginning with it. Every real instrument name, vendor suffix
and all, survives whole, asserted against a list of them.

**A second defect, found by enumerating rather than by reading.** `_int` did
`int(_num(v))`, so:

* `1e308` produced a **309 digit** ticket and a 501 byte row on its own, which
  alone nearly breaches the bound;
* a 400 digit ticket string raised **`OverflowError`**, which derives from
  `ArithmeticError` and is therefore NOT in the
  `(ValueError, RuntimeError, OSError)` tuple `Desk.handle_command` catches, so
  it left the handler uncaught. Same family #219 found in `normalize_volume`,
  reached through a model reply rather than an operator command.

A ticket is a venue handle, so a value no venue could have issued is not a
ticket: `_int` refuses non-finite and out-of-range values, which closes both.
The `except` clause is widened too, as the backstop rather than the check.

**The bound now has one home.** It lived only inside test assertions, in two
suites, each repeating 512. `journal.RECORD_ROW_BOUND` is the figure,
`docs/CONTRACT.md` states it, and both suites import it, so the documented bound
and the asserted bound cannot drift. A bound asserted in a test and stated
nowhere is a number nobody can check a change against.

**Four mutations, each red where it should be, and one of them only after a
second look.** Restoring the unclipped symbol reds 3; dropping the ticket guard
reds 1; making the clip silent reds 1. The 400 digit case stayed GREEN under the
ticket-guard mutation, because the widened `except` catches the raise on its
own: the isfinite guard and the except clause are each independently sufficient,
so that case is pinned only by restoring `_int`'s pre-fix body exactly, which
reds 4. An equivalent mutant hiding inside a guard I had just written is the
same trap as the two tests this issue is about.

**Two gaps the review found, both of them the PR's own subject one step over.**
The first: `advice_turn` was pinned and the `reject` row the SAME reply writes
was not, so removing the clip from the reject site alone left a 5753 byte row
with the whole suite green. The assertion is now over every row the turn
writes, not over a chosen event name, so a row added to the advice path is
covered without anyone editing the file. The second: the constant and the tests
agreed because the tests import it, but `docs/CONTRACT.md` carried its own
literal `512` and nothing read it, so the constant could be raised to 1024 with
the suite green while the document said 512. A test now reads the figure out of
that row and requires it to equal `RECORD_ROW_BOUND`, and it fails from either
side.

**Asserting over every row immediately found a false claim in the contract,
which is the point of asserting over a population rather than a sample.** The
row said "a single row stays under 512 bytes" and two rows do not:
`history_preflight` at 876 and `history_unavailable` at 1108 on a four-symbol
book, both written on a cold start with short H1 history. Neither carries
model-chosen content and both scale with the OPERATOR's symbol book, so they
are classified as exempt with what they scale with, the claim is narrowed to
what is measured, and whether they should be bounded or summarised is #236. The
exemption is not a blank cheque: an exempt row that starts carrying the
fixture's model text fails, a stale name in the map fails, and a THIRD
oversized row fails until a person classifies it.

**And the `except OverflowError` in `_int` is named as unreachable rather than
claimed as tested.** The review measured dropping it with the `isfinite` guard
kept: green, because the guard makes it unreachable. That is the right state
for a backstop, so the docstring says so instead of implying a test stands
behind it; a test that could only fail by removing the guard first would be
pinning the guard twice.

**A third round, and the finding is this PR's own shape one row out.** The
review measured a `reject` row at **5751 bytes** from the same turn whose
`advice_turn` row was 299 and reported nothing wrong: `_stage_close` wrote
`advice.symbol` raw from three sites, and a close is NEVER gated by
`advice_allows`, so the symbol is model-chosen with nothing in front of it.
`ADVICE_PROPERTIES["symbol"]` carries no `maxLength`, so a 5600 character
symbol is a valid string, no violation is raised and no hold is forced; this is
the default shape rather than a `grok`-only vantage. **The row these tests read
was clean and the row beside it was the defect**, which is #216 bounding a
field and this PR first bounding one row, a third time.

**Fixed at `_reject` rather than at the three call sites,** because that is the
single writer of every `reject` row: clipping three callers would have left the
fourth, and deriving the fixture's fields from the schema could not reach this
either, since the gap was PATH coverage and no derivation over fields finds a
row a fixture never writes. The already-clipped `shown` is kept for the CHAT
and the raw name passed to the record, so a value is clipped once rather than
producing a marker inside a marker.

**And a mutation found a SECOND unpinned branch in the same writer, which it
would have been wrong to call an unreachable backstop.** Removing the clip on
the `Signal` branch left the file green. It is reachable by an operator:
`/buy <5600 chars> sl=0.9 tp=1.3` with `min_rr` high refuses `rr_below_min` and
writes that row through the signal branch. The operator typed the symbol rather
than a model choosing it, which changes who to blame and changes nothing about
the row, because the bound exists for the machine reading it and a paste
breaches it as easily as a reply does. Pinned now, with the operator control
asserting that `EURUSD` still comes back whole from the same writer.

Three mutations on the round: the reject clip removed reds 3, the signal clip
removed reds 1, and the clip threshold lowered reds 6 including both controls,
so the controls can fail.

**The bound itself was four bytes wrong, and the fixture caught it before it
landed.** #250 landed `nonfinite:` markers in place while this PR was in review,
and with every schema field driven large AT ONCE and all four price fields
non-finite the row measures **516 bytes against the 512 this PR proposed**.
Those four marked values cost 97 bytes where a bare `null` would cost 53.

**`null` is not the repair, and that is a correction to my own lean.** `sl`,
`tp`, `limit` and `stop` are typed `["number", "null"]`, so `null` is a
LEGITIMATE value meaning the model supplied no stop. Nulling a non-finite field
would make "the model gave garbage" byte-identical at the field to "the model
gave nothing", which is exactly the defect #185 paid for once already with
`degraded`. #250's in-place marker is therefore not redundant with its
`nonfinite` list: the list says which fields, the marker makes the FIELD
self-describing to a consumer that reads `rec["sl"]` and never looks at the
list. Two different readers.

**So the figure is raised, and DERIVED rather than chosen:**

```
  516   measured worst case: every ADVICE_PROPERTIES field large at once, all
        four price fields non-finite, the symbol clipped, #216's per-field
        violation classes present
+  64   headroom for ONE more non-finite-capable numeric field on the row,
        priced at an 8 character name, longer than any of the four today,
        because such a field costs three places at once: the marked value, a
        `nonfinite` list entry and a `degraded` violation class. The existing
        four cost 44, 44, 50 and 53.
=  580
```

**What invalidates it is stated rather than left implicit**, because a derived
figure that does not say what breaks it is a magic number with a paragraph
attached: a SECOND such field, a name longer than 8 characters, a rise in
`RECORD_STRING_CHARS`, a violation class wider than `:not_a_number`, or a
longer `nonfinite:` spelling.

**And one measurement worth keeping: a field added to `ADVICE_PROPERTIES` alone
costs the row NOTHING.** Adding a synthetic `["number","null"]` field to the
schema and re-running the worst case measured **+0**, because the row's fields
are fixed in `desk.py` rather than derived from the schema. That is albini's
review caveat, now a number: the fixture drives INPUT, the bound answers for
the ROW, and the two are only connected where the desk journals a field.

Three mutations: raising the constant alone reds the drift gate, raising the
document alone reds it too, and setting the bound below the measured worst case
reds 6 including every by-construction case. Restored, 11 passed.
