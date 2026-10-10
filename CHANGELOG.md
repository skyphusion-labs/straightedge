# Changelog

NOTE: Operator docs from 1.0.0 use 8th-grade Simplified Technical English.
Do not treat older changelog wording as the operator contract.
See README.md and docs/CONTRACT.md.

## Unreleased

### A non-finite number no longer reaches the journal as a number (issue #231)

`_num("9" * 5000)` returns `inf`, so a model reply could put `sl`, `tp`,
`limit` or `stop` into an `advice_turn` row as a non-finite value, and `rr`
reached `reject` rows the same way. The order path already refused such
values (#208, #211, #219, #221); this was the RECORD, which is the evidence
the reconciliation work depends on.

- **The value is written as `nonfinite:<value>`, a string**, and the row
  carries a `nonfinite` list naming the dotted paths converted. Merged with
  a caller-supplied list rather than replacing it; the first version of the
  fix clobbered it, which bites only when a caller uses the key AND the row
  carries a non-finite value.
- **Applied by the WRITER, after redaction and immediately before
  serialisation**, so it covers every row and every field rather than one
  producer. `rr: Infinity` already reached `reject` rows that `_num` does
  not author, so a producer-side fix would have passed its own test while
  the same token kept shipping.
- **Recursive**, which the issue did not name: `_jsonable` is applied per
  top-level field and does not descend, so a value nested in a dict or list
  reached disk untouched. Measured on the real `history_preflight` shape,
  where `symbols[1].atr` is a path a non-finite value can occupy.
- **`allow_nan=False` makes the claim structural**: a value that escaped
  marking cannot reach the bytes. It degrades to a row naming the event and
  the failure rather than raising into the desk, because losing an
  append-only audit row is worse than writing a degraded one.
- **Not clipped and not omitted.** `jq` parses a bare `Infinity`, reports
  `isinfinite` true, and serialises `1.7976931348623157e+308`, so a
  reconciliation through the obvious tool reports a price the desk never
  saw with no error anywhere. Clipping is what jq already does. Omitting
  cannot be told from the model saying nothing.
- The gate reads the written BYTES and uses `node` and `jq` as well as
  Python, because `json.loads` ACCEPTS the bad line: a Python-only
  round-trip passes today and would have passed before the fix.

### Claims that overstate their code, and a missing adapter note (issue #193)

Residue from straightedge#182's approving review. Every item is a sentence, a
one-branch change or a test; no behaviour on the trading path moves.

- **`VENUE_CLOCK_BAR_DISAGREES` said it made a stale stamp "unreachable rather
  than merely unlikely". It NARROWS.** The comparison is against the forming
  bar's OPEN, so it sees a staleness only once that staleness exceeds the
  bar's AGE, which leaves it blind in the last moments before a bar closes:
  measured at a bar 899s old on a 900s series with the caller's bound violated,
  a 900s-stale stamp is accepted and the instant is wrong by 900s. The wording
  now says narrows, names the residual, and the boundary is pinned by test
  (`898` refuses, `899` does not) rather than left to the sentence.
- **`implied()` offered an offset outside the civil timezone band.** A clock
  frozen 48h on a UTC+3 server was reported as implying `UTC-45:00`, which is
  not a timezone, and the pre-#182 gate did say "outside the civil timezone
  band" at that staleness, so the display had lost the one reading that
  separated stale from absurd. Past the band `implied_offset_sec` is now
  absent and `doctor` says the stamp implies NO offset.
  **The band applies to the VALUE and not to the measurement state**, which is
  the part worth keeping: `venue_clock_check` keys its EXIT CODE on
  `unmeasured == {"freshness"}`, so refusing differently here would send
  doctor down the NOT MEASURED branch and exit non-zero past about 15h of
  staleness. That is a red `doctor` every weekend, which #182 decided against.
  A correction applied through the wrong seam re-creates the thing it was
  correcting, and a test pins the exit code at zero.
- **`measured_at` was carrying two meanings.** `declared()` left it zero and
  `Engine._bar_instant` gated its cross-check on `if clock.measured_at`, so a
  zero timestamp meant "nothing was sampled". That is what `__post_init__`
  already objects to for `offset_sec`. There is now a `sampled` flag for the
  gate, and the sentinel could not simply be dropped because `measured_at` is
  ALSO an operand in that check (`measured_at + offset_sec`), so
  `__post_init__` asserts that a sampled clock carries a real timestamp. The
  existing rule stated about a third field, not a new one: no current test or
  live path can produce the shape it rejects.
- **`Broker.venue_clock` is a contract change for a third-party adapter**, and
  this was missing from 1.7.0's entry. The method was added in 1.7.0 and takes
  a keyword-only `max_staleness_sec`. An adapter written against 1.6.0 has no
  such method and reads as NOT MEASURED through `venue_clock_of`, which
  refuses rather than assuming UTC; an adapter that added the 1.7.0 method
  without that argument now fails on the CALL rather than on the `getattr`.
  Either way the auto leg refuses every signal and `doctor --connect` is where
  it shows.
- **The uncertainty rule is conservative by design**, and said so as though it
  were exact. `2 * uncertainty` under one grid step is SUFFICIENT rather than
  necessary, because it treats a one-sided staleness as two-sided; it refuses
  some samples that could in principle be placed, and the direction of that
  error is a refusal rather than a wrong instant. Code unchanged.

### A regression guard that could not go red

- **`test_no_staleness_is_reported_as_a_measured_offset` passed six ways under
  a mutation making `implied()` return a measured clock.** It asserted
  `"measured" not in out`, and that mutation sends `doctor` down the
  `if clock.measured:` branch, which prints "declared by the venue": the word
  never appeared, so the guard named for the #182 blocking finding stayed green
  while doctor confidently asserted a wrong offset. **It pinned the WORD, not
  the CLAIM.** It now asserts the clock's state and the branch taken, so
  rephrasing either print line cannot make it decorative again.

### The day that ended while the desk was down is no longer silent (issue #129)

`Engine.start()` calls `risk.observe()` before the first tick, which rolls
`snapshot.day_key` to today. `_maybe_daily_recap` only emits when
`snap.day_key` differs from today, so after any restart across a UTC midnight
the condition was already false and **the recap for the day that just ended was
never sent at all.** No journal row, no message, and nothing to tell "that day
was recapped" from "that day's recap was swallowed by a restart". A desk
restarted nightly, by a supervisor, a deploy, a VPS reboot or a crash loop, lost
its P&L summary on exactly the days something went wrong.

The counterpart to #119, not a duplicate: that one was too many recaps.

**The ordering in `start()` is not the bug and is unchanged.** Every gate must
have an observed snapshot before anything is evaluated, so the fix reads what
the roll is about to discard: the ended day's key and its `day_start_equity`
live nowhere else once `observe()` has rolled.

**The announcement carries NO P&L, and that is the design.** The ended day's
closing equity was never observed. The persisted snapshot holds `equity` only as
of its last DURABLE write (`_durable()` is day key, day start, peak, and the two
counters, so an ordinary equity move does not write), which on a losing day is
the last peak and therefore ABOVE the real close: a P&L computed from it would
be wrong in the flattering direction. #129 says in as many words that a recap
reporting the wrong baseline is worse than no recap, so `equity` and `pnl` are
ABSENT from the row and `unmeasured` names them, in the shape `SymbolSpec` and
`VenueClock` already use. A missing field cannot be misread; a zero can. The
notify line reads `pnl=NOT MEASURED: the desk was down across the day boundary`,
and the formatter branches on `unmeasured` BEFORE it reads `pnl`, because
`float(None or 0)` is `0.0` and would have rendered `pnl=+0` for a day nobody
measured.

**One row per restart, not one per missed day.** A box down for a week emits a
single row naming the last day it observed and `days_skipped=7`. The bound is
the design rather than a cap applied afterwards.

**The owed day is derived from the JOURNAL, which is what makes it
recoverable at all.** The first version read it from the equity snapshot before
`observe()` rolled, and `observe()` persists the roll the instant the durable
tuple moves, so a process that died between the roll and the announcement lost
the day for good: every later boot read the rolled key, owed nothing, and no row
or message ever named it. That is this issue's own symptom surviving in a
narrower window, and it is the EXPECTED failure mode here rather than a rare
one, because supervision restarts this desk on a repeating trigger (#151) and a
crash loop lands in that window on every pass. Found in review of #198, which
ruled the direction: make the owed day recoverable rather than order two
statements carefully, because ordering moves the hazard to wherever the next
person inserts a line.

Two durable journal facts answer it, and neither rolls: the most recent `day` on
a `start` or `stop` row, which is evidence the desk was alive on a day that has
since ended, and the last `recap` row's `day`, which is what has already been
announced. Today's rows cannot erase yesterday's, so a boot that dies anywhere
in `start()` leaves the next boot able to reach the same conclusion. The `start`
and `stop` rows now carry the ENGINE's day rather than relying on `ts`, because
`ts` is the wall clock and every gate here runs on the injected clock; #182 is
the whole lesson about conflating those two.

**The baseline is enrichment, not evidence.** `day_start_equity` for the ended
day exists only in the pre-roll snapshot, so it is attached when the snapshot
still names that day and is named in `unmeasured` when it does not, which is
exactly the state a previous boot's death leaves behind. Three unmeasured fields
instead of one is the honest reading of a day whose baseline no longer exists
anywhere.

**One case got strictly better rather than merely safer.** A desk whose snapshot
cannot be READ announced nothing at all under the snapshot-derived version,
because `_persist_state` returns early there and `day_key` stays empty: #129's
silent miss surviving in the state where an operator most needs the record. The
journal does not care, so the day is announced with its baseline named
unmeasured.

**Announced once, and the marker is the JOURNAL, for a narrower reason than
"memory does not survive a restart".** On the ordinary path the PERSISTED
day_key is what stops a second announcement, because boot 2 owes nothing before
the marker is ever read; the same review measured the crash-loop case passing
with the marker removed, and that test now says so rather than claiming credit.
The state the marker uniquely guards is a desk whose snapshot cannot be
WRITTEN: `_persist_state` halts on `StateUnwritable`, the roll never lands,
every boot restores the same day_key and owes the same day, and without the
journal row three boots send three messages. `state_unreadable` does NOT reach
it, which inverts the obvious reading: an unreadable snapshot leaves `day_key`
empty, so nothing is owed at all. Both states now have a case.

The equity snapshot deliberately gains no field and no `SNAPSHOT_VERSION` bump:
that file is the money gate's input and recap bookkeeping has no business in
it. The known cost is stated rather than hidden: a journal rotation (10MB)
landing between two boots could allow one duplicate message, because
`Journal.last_event` scans the current file and never `.1`, which is a better
failure than versioning the money file.

**It is a `recap` event rather than a new event name, and that is a delivery
decision.** Every `config.toml` written before this change enumerates
`notify_events` explicitly, which is the same argument
`telegram.ALWAYS_NOTIFY_EVENTS` carries, so a new name would have reached nobody
on the live box. The operator who needs this row is the one whose desk just
restarted.

**Two markers now, for two different things, and the old rationale is
corrected in place.** The comment on `_recapped_day` argued that a
journal-restored marker "would guard a state no restart can reach". The first
half of that reasoning was true and the conclusion was wrong, and
`tests/test_recap_bounds.py::test_a_restart_while_halted_does_not_re_send_the_recap`
documented the same conclusion in its docstring. Both now say what is actually
true: the in-process marker guards a boundary this process watched, and the
restart case has its own marker.

Six cases pin it, including both controls: a same-day restart announces nothing
and a first-ever start announces nothing, so the new row cannot fire on an
ordinary boot. A clock that moved BACKWARDS across a boundary is deliberately
not reported as a missed recap; that is a different fault.
**Two claims beside mechanisms now red when the mechanism goes, and one
accepted window is named.** Review of #198 measured three gaps, none of them
behavioural, all of them the shape `docs/TESTING.md` describes: a comment
correct about intent with no reachable state in which its removal is visible.
`last_session_day_before` reads the rotated `.1` journal and said why, and
removing that read left the whole suite green; a rotation between two boots now
has a case, and it is the direction that costs a MISSED day rather than the
duplicate the marker's own rotation bound already accepts. `SESSION_EVENTS`
excludes `recap` so an announcement cannot be evidence for itself, and adding
`"recap"` to it also left the suite green; a case now pins it with the recap row
naming a LATER day than the session rows, which is the only shape in which the
scoping is observable. Measured: a journal whose `start` row has rotated away
re-announces an already-recapped day with `"recap"` counted, and does not
without it. Third, `_announce_unrecapped_day` now names the window it accepts
on the notify path: `_emit` journals before it notifies and the row is also
what suppresses a retry, so a send dying in between loses the ANNOUNCEMENT
permanently. That is the deliberate side of the trade, because retrying on a
persistently broken transport is one message per boot, which is #119 by the
other door.

### A transform on a model-chosen symbol can no longer manufacture one (issue #197, p0-safety)

`"EURUſD".upper()` is `"EURUSD"`. Unicode uppercasing maps U+017F LATIN SMALL
LETTER LONG S onto ASCII `S`, so a model reply naming an instrument that does
not exist reached the desk as a staged BUY on one that does. Measured through
the shipped functions on merged `main`:

```
sent='EURUſD'  ->  parsed='EURUSD'  action='buy'  advice_allows=True
```

The string is a valid `["string","null"]`, carries no brace, raises no schema
violation and survives `_JSON_TAIL`. The ligatures `ff`, `fi` and `st`, the
dotless `i`, and `ß` (which expands to `SS`) all transform the same way.

**The same shape as the brace defect #181 fixed, one character different:** a
repair on `symbol` turns a NAMED REFUSAL into an order.

**#181's own guard could not see it, and that is worth knowing because the
guard reads like a general safety net.**
`test_a_braced_symbol_is_not_more_permissive_than_the_bare_parser` compares the
structured path against the bare parser, and both transform identically here:
a comparison between two paths is blind to a defect they share. Fixing this by
extending that comparison would have produced a green test over a live defect.

**The rule is ASCII BEFORE the transform, applied at every site that can
transform, and it is not a codepoint blocklist.** `isascii()` separates every
member of this family from every legitimate instrument name, and the next
case-mapping character is always one nobody enumerated. Four sites, each
sufficient on its own to have kept the defect alive:

- `config.advice_allows` refuses a non-ASCII name itself, rather than trusting a
  caller to have checked. The issue measured it returning True with no parser
  involved at all.
- `llm.parse_advice` leaves the name EXACTLY as sent instead of uppercasing it.
  `grok` and `computer` have no schema gate, so the parser is the only gate
  they have. The name is not blanked either: a `None` symbol makes the desk skip
  its staging block in silence, and the whitelist refusal is the loud answer.
- `llm._schema_violations` reports `symbol ... is not ASCII` and forces `hold`,
  so the structured path SAYS what was wrong instead of leaving an operator to
  infer it from a refusal further down. A model emitting a name outside the
  instrument vocabulary it was given is also evidence the schema constraint did
  not apply, which is that function's whole subject.
- `desk` names the string the MODEL sent in the `symbol_not_allowed` record and
  in the chat line. It used to log `advice.symbol.upper()`, so the refusal for
  `EURUſD` would have read `EURUSD`: a named refusal for an instrument that IS
  allowed, which looks like a bug in the whitelist rather than a rejected reply.

**The operator's own whitelist is held to the same rule**, because the claim in
`advice_allows` was not true without it: the config loader did
`[str(x).upper() for x in advice_names]`, so a typo of `EURUſD` in
`advice.symbols` became `EURUSD` before any gate could filter it, silently
widening the list to a symbol nobody typed. It now stays as typed and matches
nothing, which is a visible failure rather than an invisible widening.

`eurusd` still works throughout. An ASCII case fold is the same instrument, and
that is why the rule is ASCII-before-transform rather than no transform at all.

Thirty cases pin it, including the controls that keep the benign path alive and
a pin on the two-path agreement that made #181's comparison silent. One of the
six characters in the corpus reaches a symbol in the SHIPPED whitelist and that
is said out loud in the corpus comment rather than left to look like six
exploits; all six share the parser transform, which is the mechanism the rule
binds.

### The auto path's working-order check now has a test (issue #207)

`risk.evaluate` is handed `orders=orders` from two sites and only one of them
had a test. Measured on `main` at `937d0d6`, mutating each to `[]`:

| mutation | stop-guard | working-orders | new auto-path |
| --- | --- | --- | --- |
| `preview`'s `orders=orders` (engine.py:1307) | green | **RED** | green |
| `_act`'s `orders=orders` (engine.py:1192) | green | green | **RED** |
| `risk.py`'s `ours_orders` (risk.py:553) | green | **RED** | **RED** |

The second row is the gap: **dropping working orders from the AUTO path's risk
evaluation was caught by nothing.** The first and third rows are what make the
second one a gap rather than a guess, because each suite reds on exactly one
call site and the shared point reds both, so the two suites are blind to each
other's caller and the new one genuinely drives `_act`.

If it regressed, resting working orders would stop counting toward
`max_positions`, `already_in_symbol` and the currency limit **for auto entries
only**, which is the defect
`tests/test_working_orders_count_as_exposure.py` exists to prevent, reproduced
in the one path that suite does not reach. Not urgent: `auto` is off by default
and separately gated, so no operator was exposed. It is a shipped feature on a
repo pointed at real money and the cost of closing it was one test.

**The gate under test is `max_positions`, and `already_in_symbol` could not be
used.** `_act` carries its own same-symbol check before it ever calls
`evaluate`, so a same-symbol order is refused earlier and a test built on that
reason would measure the earlier check instead, passing whatever `orders=` was
handed over. The resting order therefore sits on a different symbol.
`currency_exposure` is covered too, because one assertion through one gate is a
single point and the two read the same argument by different routes.

**The preconditions that make each red possible are stated in the suite**, not
left implicit: a real resting order placed through the desk on another symbol,
an auto tick that actually reaches `evaluate`, and a slot limit the resting
order alone fills. Drop any one and the mutation is EQUIVALENT and the suite is
green for a reason unrelated to the gate. One case asserts precondition two
directly, by raising the limit and requiring the same tick to open, since every
other assertion here is vacuous if `_act` returns early.

**One fixture bug, caught by its own case rather than by review.** The currency
case first committed a second leg with `/buy USDCHF`, which is LONG USD and
therefore CANCELLED the resting order's short-USD leg: the cap was never
reached and the case failed with no refusal at all. It uses `AUDUSD` now, and
the comment says why, because a fixture that tests a state it did not intend is
the failure this repo keeps finding.

### A reply we could not read is no longer identical to a model that held (issue #185)

#181 gave the claude advice path a schema gate: an off-schema reply has its
action forced to `hold` and the reason written into the reply PROSE. Correct,
tested, and it stops at the chat. `desk.py` closes every advice turn with a
journal row carrying what the model DECIDED and deliberately not what either
side said, so in `journal.jsonl` these two turns were byte-identical:

```
the model held                             action=hold  staged=false
the model said BUY and we could not read   action=hold  staged=false
```

A desk that cannot tell "the model held" from "we could not read the model" is
the defect #181's own docstring names, one surface over. The chat tells whoever
is watching at the time; the journal is what anyone reconstructing a demo week
reads, which is #37 and #38.

`advice_turn` now carries `degraded`: the gate's own violation names, empty when
nothing degraded. **Always present, never omitted**, because a field that
appears only on failure cannot be told from a desk too old to emit it, which is
the partition `survivor_ticket` and `history_error` exist for.

**The reason is a CLASS, not a sentence, and that took a second pass.** The
first version interpolated the reply's own content into the violation text,
which was right for the chat and wrong for the record: a review measured a key
named like a sentence writing that sentence into `journal.jsonl`, and a 6000
character `action` producing a 6053 character reason and a **6293 byte row
against the 512 byte bound this suite already pinned**. So a model could author
unbounded text in our journal through the one field added to make the journal
trustworthy.

`_schema_violations` now returns `(field, class)` pairs from a fixed vocabulary,
with two renderings over one producer. `violation_prose` keeps the operator
sentence #181 added and may echo a value, because the chat already shows the
model's own prose; `violation_classes` renders `field:class` for the journal and
collapses the model's own field NAMES into one counted token, so the longest
possible reason is a function of `ADVICE_PROPERTIES` and not of anything a model
sends. Truncating the interpolated string would also have worked and would have
left a judgement about "small enough" in the code; this leaves none. #181's
twenty cases are green throughout, which is what shows the chat contract did not
move.

**And both tests written to guard that threat could not observe it**, which is
the finding worth more than the fix. `test_the_reason_cannot_be_written_by_the_model`
sent its payload as the VALUE under a key named `degraded`, so the only
violation emitted was about the key and the value could never appear: it passed
by construction. The bounded-row case varied `text` and `summary`, neither of
which is echoed, so it could not see a 6KB row either. Both are rewritten to
drive the channel that leaked, a model-chosen KEY and a model-chosen VALUE, and
both red when the pre-fix rendering is restored. A fourth case asserts the bound
by construction rather than by fixture: forty unknown fields plus every other
violation at once renders under 200 characters.

**The reason travels OUT OF BAND, and both alternatives were defects.** Parsing
it back out of the prose is string-matching our own sentence, and the sentence
is not a contract. Adding a key to the trailing JSON would be a field
`parse_advice` cannot tell WE wrote: the `grok` and `computer` paths have no
schema in front of them, so a model could put that key in its own tail and
author a line in our journal. `Advisor.ask` collects what its own gate measured,
through a list the caller owns, which no model can reach. A test drives a model
reply containing `degraded` and asserts the recorded reason is ours.

**It is cleared before every provider call**, so a reason cannot outlive its own
turn and attach to the next clean reply. That is #119's stale-marker shape, and
a confidently wrong record is worse than a silent one; the mutation that stops
the clearing reds exactly the case written for it.

**The contract's own policy is unchanged and now says so explicitly.** The
question and the reply are still never written to `journal.jsonl`: the reason is
OUR sentence about the reply's SHAPE, not the reply. A test pins that a 1000
character prose reply still leaves the row under 512 bytes with none of that
prose in it.

Four mutations, each anchor-unique with its size delta measured non-zero so no
stale bytecode can match, verdicts from exit status: dropping the field,
attaching nothing, collecting nothing, and leaving the stale marker uncleared.
#181's own suite stays green through all four, which is what shows the chat
surface did not move.

### The measured venue clock reaches the durable record (issue #186)

#172 journals the FAILURE and nothing on success. A refusal writes
`venue_clock_unmeasured`, and #182 later added `venue_clock_bar_disagrees`, so
`journal.jsonl` could say the desk did not know what time it was and could never
say it thought it was UTC+3. The offset was measured, used to convert every bar,
and discarded: the only way to learn it was `doctor --connect` at the moment you
asked, which answers for NOW and says nothing about the instant an order was
placed. #37's evidence package has to answer that from the journal alone, and
the only reason we know the live desk was on +3 is an inference from the shape
of three refusals.

**One `venue_clock` row at `start()`, one more on every CHANGE, nothing while it
holds.** Journal-only, never a chat ping: the loud clock events already exist
and an offset that has not moved is not news.

**The change detector is the only part with real design in it**, and the two
cases it must catch are the two that happen with nobody editing anything: a
server-side DST roll (+10800 to +7200) and a reconnect that lands on a different
server (+10800 to 0). The comparable state is the offset, the unmeasured field
names and the source. `detail` and `measured_at` are deliberately NOT in it,
because both move on every sample and including either would make every poll a
change: the record would be a per-poll log, which is the shape #119 exists to
forbid. Both are still written on the row. A transition carries
`previous_offset_sec`, so one row states what it moved FROM and a DST roll is
legible without diffing two rows.

**The opening row is honest about being an implication.** `start()` has no
previous poll, so it cannot bound the sample, and a SAMPLING venue therefore
answers with an implication that refuses conversion; the row carries
`implied_offset_sec` and names `freshness` as unmeasured, which is the same
thing `doctor --connect` prints. A DECLARING venue (paper, and so every
backtest) records a real offset there.

**It cannot stop the desk starting, and that needed care rather than a
docstring.** Before this, nothing in `start()` asked the venue for a tick, and
the MT4 adapter RAISES when the Expert answers an error. A tick for a symbol
still absent from Market Watch is exactly the reading that fails, and cold
symbols are the normal startup state, which is why `warm_history` exists. So the
reading happens after `warm_history`, which is what selects the symbols, and a
failure is recorded as unmeasured with the error as its detail rather than
propagating. A mutation that lets the error escape reds the case that pins it.

**A WESTWARD move is recorded late, by about its own size, and that is named
rather than papered over.** Bar stamps move back with the server, so
`step_symbol`'s `if last_t <= prev: return` holds until they climb past their
previous high: a three-hour reconnect westwards is invisible to the auto leg for
about three hours, whatever this record does. The row states what the desk
measured when it next ACTED, which is the only instant it has evidence for, and
both directions are tested because only one of them is delayed.

**Held to the exclusions the issue wrote in**, each asserted rather than
promised: no translation layer and no row restating another's instant in venue
time, `ts` stays our own clock, no config knob, and no gate change. The last one
is measured from inside `_bar_instant`, which is the seam the refusals come out
of, so a lost clock must still refuse by name and a measured one must still not.

### A model-chosen symbol, and what the ASCII rule does NOT cover (#197 residue)

One sentence, where a reader would look for it. The ASCII-before-transform rule
covers a MODEL-chosen name and deliberately is not a global invariant:
`desk.parse_kv` and `engine.market_signal` still case-fold what an operator
typed, by the same reasoning that exempts a human `/buy` from the advice
whitelist. An operator pasting `EURUſD` out of a model's prose receives an order
on the instrument the string LOOKS like, which is the one they chose. Recorded
so nobody reads the rule as wider than it is and nobody files it as a defect.

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

### A command that FAILED now leaves a record, not only a chat line (issue #232)

`Desk.handle` ended with `except (ValueError, RuntimeError) as exc: return
redact_text(str(exc))`. The sentence reached the chat and nothing reached the
journal, so every command that failed this way was invisible to the one channel
an operator can audit, `/history` can show, and a test is allowed to assert on.
The chat said something had happened and the record said nothing did, which is
a silent degrade with a message attached.

**On the advice path the silence cost the operator something real.**
`record_advice_turn()` runs BEFORE the provider call, deliberately, because the
turn is billed whether or not it ends in an order (pinned by
`test_the_advice_cap_refuses_before_the_provider_is_billed`). So a raise from
`ask` left a daily cap slot SPENT with nothing saying it had been attempted:
the operator watched the budget shrink and the journal could not say why.
`advice_error` carries `turn_spent`, which no other row can supply.

**Measured on `main` with a transport that raises the way a real HTTP hop
does**, which is why this could only come out of the live end-to-end run #35
requires: a fake transport returns a payload and never raises.

```
/ask take a view  ->  'telegram http 502'
journal           ->  start, history_preflight, history_unavailable, venue_clock
```

**The row carries the exception CLASS and deliberately not its message.** A
message can be authored by a provider or quote a model reply, and a
model-authored sentence in `journal.jsonl` is exactly the channel #216 and #226
closed; the chat already has the sentence. A clipped `detail` field becomes
possible once `clip_for_record` reaches `main` (#226) and is left as a
follow-up rather than hand-rolled here, because a second clipper would be a
second opinion about one bound. The no-sentence rule is asserted, not claimed:
a 400 character provider message must not appear in the row.

**COULD NOT MEASURE stays distinct from REFUSED.** Both rows carry
`measured=false` and neither is a `reject`, so a reason count built on `reject`
can never read a crash as a rule saying no. The partition is asserted from both
sides: a crash must not write `reject`, and a real refusal (a spent advice cap)
must still write `reject` and must not write an error row.

**The caught tuple is NOT widened.** `(ValueError, RuntimeError)` stays as it
was: widening what is caught is a behaviour change on the money path, and this
change adds a record without moving behaviour. The chat reply is asserted
byte-for-byte unchanged in both paths.

Six mutations, each red where it should be: removing the `command_error` write
reds 1, removing the `advice_error` write reds 4, recording a crash as
`measured=true` reds 1, recording it as `reject` reds 4, dropping `turn_spent`
reds 1, and adding the provider's sentence to the row reds 1. Restored, 7
passed, and the two controls (a successful command writes no error row, a
refusal is still a refusal) are green before and after by construction.

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

## 1.8.0

### The worst tick gap this box has seen now outlives the process (issue #153)

`tick_gap_max_s` answered one question while the runbook asked it another, and
a restart erased the answer. The figure is the largest gap between heartbeat
writes that the live PROCESS has observed, and `over_budget` is derived from
it. But what `over_budget` was installed to test is whether the derived
staleness allowance is adequate for this BOOK, and a restart changes neither
the allowance nor the book.

Measured on the live desk, not theorised. Before the 2026-10-08 deploy:

```
stale_after_s=428   tick_budget_s=214   tick_gap_max_s=608.5   over_budget=1
```

After the restart that deploy required:

```
stale_after_s=428   tick_budget_s=214   tick_gap_max_s=7.7     over_budget=0
```

Both figures are correct for their process. The 608.5 then existed nowhere a
live surface could reach, while #143 was citing it as evidence that the
deployed desk's single 5000ms budget could not clear #84's derived 7060ms worst
case. The reset is also in the dangerous direction: a desk restarted after a
bad episode publishes its cleanest possible history, so `over_budget=0` on a
fresh restart is the reassuring half of a two-part answer.

**Two questions, two pairs of fields, and the heartbeat now carries both.**

- `tick_gap_max_s` and `over_budget` are unchanged and stay PER PROCESS. A desk
  whose book has shrunk must be able to report a clean budget again; an
  indicator that can never go green is one an operator learns to ignore, which
  is the module's refusal to widen the threshold read from the other end.
- `tick_gap_ever_s` and `over_budget_ever` are new and are carried across
  restarts. `watch` says so in plain words when the box has breached and the
  live process has not, because a durable figure published to nobody is not an
  improvement.

**The store is the heartbeat itself, and the other two were rejected on the
record.** The risk snapshot is money-path state that fails closed, so a
diagnostic there widens what a corrupt snapshot can halt. The JOURNAL is where
the breach RECORD goes but cannot be the store for the maximum: rotation is a
single generation and `tail` reads only the live file, so a maximum recovered
by scanning the journal is a LOWER BOUND once a rotation has happened, and a
lower bound published as a maximum is the defect being fixed. The heartbeat is
not risk state, is not rotated, is already 0600 and atomically replaced, and is
already the file the figure is published in, so no new sidecar was added.

**What it cannot see, stated rather than implied.** The figure is a maximum
over the heartbeats that SURVIVED. Deleting `journal.heartbeat` resets the box
history and that is the only way to lose it. A desk upgrading from a build
before 1.8.0 starts the chain at its own observations: the previous process's
`tick_gap_max_s` is deliberately NOT adopted, because that is the other
question's number and adopting it would reintroduce the conflation. `watch`
reports a missing `over_budget_ever` as unknown and never as clean.

**Every breach is also journaled**, as `tick_gap_breach` with `gap_s`,
`budget_s`, `symbols` and `run_id`, once per process per breach. The heartbeat
holds the worst gap; only the journal can answer how OFTEN this box breaches.
The stdout warning stays, and on the deployed box it goes to a file nobody
reads, which is why it was never the durable record. `symbols` and not the
position count the issue floated: a position count means a venue round trip
inside the heartbeat writer, which would add latency to the very path the
record exists to explain, and an instrument must not perturb its own
measurement.

**Red first.** `tests/test_tick_gap_survives_restart.py` drives two real desk
processes over a monkeypatched monotonic clock and lets `_write_heartbeat`
measure the 608.5 itself; nothing assigns the figure. Six mutations were run
and each produced a named failure: no restore at all, the live process not
contributing to the box figure, a restore that adopts the previous process's
`tick_gap_max_s`, the watcher note removed, a missing field read as clean, and
the breach printed but not journaled.

**One mutation was run and is NOT in that list, because it cannot fire.**
Replacing the RESTORE's own `max(self._hb_gap_ever_s, float(raw))` with a plain
assignment leaves the whole suite green (1394 passed), and correctly so: the
restore happens once, before any live write, when the carried figure is still
`0.0`, and a gap is never negative, so the two forms cannot be told apart by
any reachable input. That `max` is defensive rather than load-bearing. The
load-bearing one is in the WRITER, where the live process's own observation is
folded in, and dropping THAT reds four tests. An earlier draft of this entry
described the equivalent one as though it were the proof, which would have
claimed a red-proof that is not one; recorded here rather than silently
corrected, because an equivalent mutant wants a stated reason and not a test
(`docs/TESTING.md`).

### Fix-forward, in the same change

- **The heartbeat field list was incomplete in both docs.** `docs/CONTRACT.md`
  did not name `deployed=`, and `docs/RUNBOOK.md` named neither `run_id=`,
  `started_at=` nor `deployed=`, all three shipped in 1.6.0. Both lists are now
  complete. A format contract that omits fields the renderer emits is a
  contract a reader cannot reproduce the file from.

## 1.7.0

### One clock: a broker bar stamp is not UTC, and the offset is measured (issue #172)

Found while pulling the #37 demo evidence off the live box, 2026-10-09, and
measured there rather than theorised. `C:\bot-state\journal.jsonl` carried three
auto refusals that could not be right:

```
2026-10-09T14:00:04Z reject source=auto stage=signal symbol=XAUUSD reason=outside_session
2026-10-09T15:00:03Z reject source=auto stage=signal symbol=XAUUSD reason=outside_session
2026-10-09T16:00:02Z reject source=auto stage=signal symbol=XAUUSD reason=outside_session
```

The live window is `start_utc = "07:00"` / `end_utc = "17:00"` and the box clock
is UTC. 14:00 and 15:00 are squarely inside it. `in_session` was not the bug;
the timestamp handed to it was.

`engine.py` built the auto leg's instant as
`datetime.fromtimestamp(bars[-1].time, tz=timezone.utc)`, and an MT4 bar time is
BROKER SERVER time. `tz=timezone.utc` does not convert it, it RELABELS it. The
desk is on a UTC+3 server, so every gate below that line ran three hours off.

- **The session window silently moved by the broker offset.** 07:00-17:00 UTC
  became 04:00-14:00 UTC, and nothing printed either number, so the operator
  read the config and believed it. The #37 evidence week was collected through
  a window nobody chose.
- **One daily-loss budget had two day boundaries.** The auto path rolled
  `day_key` on the BROKER day; the desk and the recap roll on the real UTC day.
  Which boundary applied depended on whether a human or the regime fired.
- **A `daily_loss` halt could release up to the broker offset EARLY.** The halt
  clears on the day-key change and the broker day crosses midnight first, so a
  gate whose whole job is to stop trading for the rest of the day handed the
  budget back while it was still that day in the units the config is written in.
  This is the sharp one.
- **`weekday()` came from the same relabelled value**, so the Saturday/Sunday
  block and the Friday cutoff landed on the wrong wall-clock instants. Near the
  weekend edges that is a DAY-sized error: on a UTC+10 server, 22:00 UTC Sunday
  stamps the bar Monday 08:00, which is a weekday inside the window, so the
  weekend block did not fire during the illiquid Sunday open.

**The offset is MEASURED off the venue and is never configured.** It is a
per-server property that moves with the SERVER's DST, so a number in
`config.toml` is a guess that outlives the first DST change after somebody wrote
it: that is #68 (an instrument-blind constant that silently mis-sized gold) with
a clock in place of a tick value, and the rule from #68 applies unchanged. A new
`VenueClock` carries `SymbolSpec`'s exact partition, applied to a clock:
`offset_sec` is `None` and `unmeasured` names the field when the measurement
failed, `to_utc` RAISES rather than returning a plausible instant, and the type
cannot be constructed half-measured at all. There is no "assume UTC" fallback,
because zero is a perfectly ordinary offset and a defaulted zero cannot be told
from a measured one.

**No Expert change and no reattach.** `TickReply` has carried
`time=TimeCurrent()` since the first version of the ICD, so
`Mt4Broker.venue_clock` measures the offset from a reply the Expert already
sends, pairing it with the desk's own clock read either side of the round trip.
The fix reaches the live desk as a Python upgrade. An Expert that does not send
`time` reads as NOT MEASURED and refuses, never as UTC.

**The staleness bound is an ARGUMENT with no default, and that is the whole
safety property.** `TimeCurrent()` is the time of the LAST TICK, not of now, so
a sample reads `offset - staleness` and the two cannot be separated without a
bound on the second. The bound is therefore supplied by the caller and REQUIRED:
`Engine.step_symbol` measures it as the elapsed time since its own previous poll
of that symbol, which is sound because a bar advanced between those two polls,
so a tick arrived inside that interval, so the stamp cannot be older than it.
`replay_symbol` and `doctor` own no such gate, pass `None`, and are therefore
structurally unable to obtain a measurement from a venue that samples a server.

It is not derived from a config value, and that is deliberate: this repo's own
tick budget for a live MT4 config is 270s of bounded part with an explicitly
unbounded tail, and `watchdog.py` publishes `tick_gap_max_s` precisely because
that budget can be exceeded. A number the codebase already instruments because
it can be false is not a bound.

A sample is read as the nearest quarter-hour grid point only when twice its
uncertainty (the bound plus the measured round trip) stays under one grid step,
because otherwise more than one offset fits it. A chosen 180s tolerance in the
first version of this change is gone: it was a number nobody derived, in a repo
whose `watchdog.py` says in as many words that a threshold is derived and never
chosen.

**The one case the grid cannot see is closed by a second reading.** A stamp
stale by an exact multiple of 900s lands on a grid point and looks perfect, and
the civil timezone band does not see it either (11h is 44 whole grid steps).
`VenueClock.measure` says so, and the limit of the type is pinned by a test. The
ENGINE then catches it with a reading the type does not have:
`Engine._bar_instant` compares the measured offset against the venue's own
forming bar and refuses with `venue_clock_bar_disagrees`, because a server
cannot be forming a bar its own clock says has not opened yet. The review's
whole frozen-Friday sweep (2h, 5h, 11h, 15h) is driven through the engine and
refuses there; both readings come from the same server clock, so our clock
cancels out and a correct offset cannot be refused by it. A positive control
pins that: it caught a fixture of this change's own, which had seeded a bar an
hour ahead of the venue's clock and was therefore observing its own refusal
rather than the defect's.

**The session window is exact only to within the clock's uncertainty, and it
now says so.** `offset_sec` is an int and `measured` is a bool, so nothing
downstream could know the instant carries an error bar while the gate reading it
compares exactly. The error is the venue terminal's own drift: bar stamps and
`TimeCurrent()` carry it identically, so it cancels from the measured
difference, and the grid snap then removes it from the offset while the bar
stamp keeps it, which makes the snap the only error source left in the measured
path. The residual check caps it at the sample's uncertainty, held strictly
under half a grid step. Measured through a real engine: at the widest legal
bound a 449s drift is absorbed and a 450s bound measures nothing at all, while
on the desk's own path, where the bound is the measured poll gap, a 90s drift
refuses. Under the chosen 180s tolerance this change started with, that same
90s silently moved a 17:00:00 instant to 16:58:30 and the session gate did not
fire, leaving the desk armed up to three minutes past its configured close.
`VenueClock` and `docs/CONTRACT.md` state it, four tests pin it, and the number
has one home in `VENUE_CLOCK_GRID_SEC`.

**`day_key` is the UTC day, decided and written down** (`docs/CONTRACT.md`, "One
clock"). It was accidentally both before, which is the actual defect. The
operator's config is written in UTC, the desk and recap already rolled on it,
and a broker day would key the money budget to something that moves without
anyone editing anything.

**MT5 was checked rather than assumed.** `copy_rates_from_pos` and
`symbol_info_tick` return the trade server's clock with no API that states the
offset, the adapter passed both through unconverted, and the engine seam is
shared, so the MT5 path carried the identical defect and is fixed identically.
Verified by reading the adapter and by a unit test against a fake binding; NOT
verified against a live MT5 terminal, which this repo has never claimed for the
MT5 path.

**Breaking for a third-party venue adapter.** `Broker` gains
`venue_clock(name, *, max_staleness_sec)`. An adapter that does not implement it
reads as NOT MEASURED, which refuses rather than trading on a guessed clock, so
an out-of-tree adapter keeps working for every manual command and stops the auto
leg until it answers. Paper, MT4 and MT5 in this repo implement it.

**What an operator sees, and what `doctor` deliberately does NOT claim.**
`doctor` has no previous poll and no bar advance, so it cannot bound staleness
and never prints a measurement for a venue that samples a server. It prints the
two facts separately:

```
venue clock: stamp implies UTC+03:00, freshness NOT established (...)
```

and exits ZERO on that, because it is the normal state of a closed market and
an operator who sees a red `doctor` every weekend learns to ignore `doctor`. It
exits non-zero only when the clock cannot be READ at all (no stamp, or a venue
that cannot answer), and, from config alone with no terminal involved, when
`engine.poll_seconds` is too slow for any sample to be bounded.

The first version of this gate printed `server UTC+03:00 measured` and was
wrong about it: the straightedge#182 review froze `TimeCurrent()` at Friday's
close on a genuinely UTC+3 server and the gate reported `UTC+01:00 measured` at
2h stale, `UTC-02:00` at 5h and `UTC-08:00` at 11h, each with exit 0, to a
human deciding whether to start a live loop. The civil band rejected nothing
until past 15h. That whole sweep is now a test.

The auto refusal is journaled as `reject` with
`reason=venue_clock_unmeasured` plus the `unmeasured` field and a `detail`;
`docs/RUNBOOK.md` says what to do about it. Manual `/buy` and `/sell` are
unaffected throughout: they time themselves off the bot's clock and never off a
bar.

**The test had to go red first, and 1222 green tests could not see this.** Every
existing fixture seeds bars whose timestamps ARE UTC, so the suite agreed with
the defect. `tests/test_bar_clock_is_not_utc.py` supplies the one input none of
them had, a venue whose clock is not UTC, and asserts the session window, the
day boundary, the halt release and the weekend block against the real UTC
instant. All four fail on `main` and pass here.

### Fix-forward, in the same change

- **`broker/base.py` is no longer excluded from coverage.** It was omitted as
  pure type declarations; it now holds `venue_clock_of`, the rule that turns a
  venue with no clock into a refusal, and a safety rule in an omitted file is a
  rule whose coverage cannot be measured.
- **The venue fakes in `test_cli.py`, `test_login_not_leaked.py` and
  `test_history_preflight.py` now state a clock.** Without it the doctor gate
  went red for two reasons at once in the history test, which would have made
  its red unattributable.

## 1.6.0

### The supervision audit asks Task Scheduler what the task will DO (issue #151)

The audit shipped in #146 read the task DECLARATION and never asked what would
happen next. Re-measured while taking #151, and the blind spot was worse than
the issue said: against the exact shape the live box carried on 2026-09-26, a
`Repetition PT5M` hung on the desk task's `LogonTrigger` with every other field
correct, the audit returned **zero findings and exit 0** while that desk had not
restarted in twelve days. Not a nearly-right reason; the wrong answer. The
suite's own "the incident, reproduced" fixture carried no repetition at all,
which is the easy case any trigger check catches, so nothing ever ran the shape
that happened.

The live defect is fixed on the box (a `TimeTrigger` was added beside the
`LogonTrigger`, 2026-10-09, `NextRunTime` now five minutes out). This closes the
instrument so the next one is caught by a control instead of by an outage.

- **Two instruments that fail independently.** `CLOCK_TRIGGERS` judges the
  trigger TYPE under a repetition from the XML alone, so the shape that actually
  happened is caught with no export and the shipped templates are covered too.
  `NextRunTime`, from a new `<task>.info.json` sidecar, catches what a
  declaration cannot settle: a future `StartBoundary`, an expired `EndBoundary`,
  an elapsed `Duration`, a task the live system has disabled behind an XML that
  says enabled.
- **`Export-Tasks.ps1` writes the sidecar** from `Get-ScheduledTaskInfo`, still
  read-only. Times are explicit UTC resolved ON THE BOX, and `_parse_utc`
  REFUSES a naive timestamp rather than assuming one: relabelling a local time
  as UTC is #172's defect with a different clock in it.
- **A real dump with no sidecar is `liveness_unmeasured`, a FAIL**, by the same
  rule `task_unreadable` follows. A shipped template is exempt because it is
  registered with nothing; the artifact declares which it is via `REPLACE_ME`
  rather than an operator remembering a flag, since a forgotten flag would turn
  a live audit into a declaration audit silently.
- **No single live field is safe, so the audit judges a tuple.**
  `LastTaskResult = 0x800710E0` is the HEALTHY steady state of the desk's task:
  the trigger fires, finds the desk alive, and `IgnoreNew` refuses the
  duplicate. Flagging non-zero would report the desk broken every five minutes
  forever; ignoring the field would miss a task erroring every cycle. The
  measured-healthy reading is the DEFAULT fixture for every declaration test, so
  getting that reading wrong reds the whole file rather than one test.
- **The control has a control.** `supervision-xml` now registers the pre-fix
  shape against real Task Scheduler on `windows-latest`, dumps it, and requires
  the audit to red AND to name `repetition_not_on_a_clock`. It prints the
  sidecar first, so if Task Scheduler ever reports that shape differently, that
  is visible rather than hidden behind a bare red.
- **The control caught a defect in the audit on its first run, which is the
  point of having it.** `supervision-xml` red on the freshly registered tasks:
  `LastTaskResult` is `267011` (`0x00041303 SCHED_S_TASK_HAS_NOT_RUN`) on a task
  that has never run, and the check read every non-zero result as a failed
  launch. That would have red on every first install, which is the "a gate that
  reds on a healthy box is a gate that gets ignored" failure. Fixed by judging
  the HRESULT severity BIT rather than an allow-list of codes to forgive, since
  an allow-list encodes which members of the family the author happened to see.
  `0x00041307 SCHED_S_TASK_NO_VALID_TRIGGERS` stays a FAIL, being the scheduler's
  own verdict that a task cannot fire. The runner's sidecar is now pinned
  VERBATIM as a fixture: it is the only input in that test file not written by
  hand, and the hand-written healthy one could not have produced this shape
  because it carries what the LIVE DESK reports, not what a fresh install does.
- #151's own body asserted that `StopAtDurationEnd` with an empty `Duration` was
  the mechanism. That was an inference about XML semantics and nothing measured
  it; the documented schema says an absent `Duration` repeats indefinitely. What
  WAS measured three times is the trigger TYPE, so the trigger type is what this
  judges, and the window is reported as measured without a reading imposed on it.

### A git deploy procedure, and a desk that can say what it is running (issue #147)

Conrad's requirement: "I wanted the deployment method of this bot on the box to
be via git." The box was already a checkout, so what was missing was a
controlled, repeatable, reversible UPDATE procedure, and an answer to "what is
running" that does not need someone to log into the box. The cost of not having
one was measured: the live desk ran twelve days at 25 commits behind `main` and
no reading an operator could get said so.

- **`docs/DEPLOY.md`** is the decision record: what gets deployed and why it is
  an immutable ref rather than `main`, the ordered stop and start, the rollback,
  and what is deliberately out of scope. The decision to deploy is #143 and
  Conrad's; the Expert is not touched by a desk deploy, ruled on #73; and #142's
  "no version handshake" boundary is respected, so this is observability only.
- **`deploy/windows/Deploy-Desk.ps1`** executes it and REFUSES at the two
  judgement gates rather than automating them: nothing changes without `-Apply`,
  and it stops dead without `-BookIsFlat`, because a script can report the
  ledger but cannot decide that stopping a live autonomous desk right now is
  acceptable. It records the rollback point BEFORE anything changes, refuses
  outright on an open inflight entry, and never runs `git clean`.
- **The first step is not the obvious one.** Supervision's repeating trigger
  fires regardless of what is being done to the working tree, so a `git
  checkout` with the task enabled can start a desk on a half-updated tree. The
  TRIGGER is disabled, not just the process, and `straightedge-watch` goes down
  with it rather than paging the chat about a deliberate outage. Both are
  re-enabled at the end and then PROVEN enabled, because a deploy that leaves
  supervision disabled returns the box to the exact state #133 existed to fix,
  silently.
- **The state directory being a SIBLING of the checkout is now a requirement
  with its consequence stated, not a happy accident.** It holds the journal, the
  inflight ledger, the equity snapshot and the heartbeat: the audit log of a
  real-money desk. The procedure performs a hard detached checkout inside the
  tree, so if that directory were ever a child, every trade this desk has made
  would be deleted and no other step would notice. The script refuses to run in
  that configuration and CI watches the refusal fire.
- **`/status` and the heartbeat now carry what is running.** The deploy writes
  `journal.deployed.json` with the ref and the full 40-character OID; the desk
  reads it (`src/straightedge/deployed.py`) and publishes it in both places,
  because a person in the chat and a process on the box are different readers.
  `unstamped` is an ANSWER and never a blank, and it never falls back to
  `__version__`: a hand-maintained version spanning 18 changelog sections would
  be a confident wrong answer in place of an honest absent one.
- **A UTF-8 BOM can no longer stop the desk.** Measured live on the real-money
  box: an edit made with Windows PowerShell 5.1's `Set-Content -Encoding UTF8`
  wrote a BOM, which is invisible in an editor and to `Get-Content`, and
  `tomllib` refused the whole file with `Invalid statement (at line 1, column
  1)`. The desk would not have started at its next restart; it was caught by
  validating through the loader and reverted from a backup. `load_config` now
  decodes `utf-8-sig` and reports the BOM on stderr naming the remedy, nothing
  this repo writes writes a BOM, and `deploy/windows/assert-config-loads.py` is
  the pre-restart check that validates through the LOADER rather than by reading
  the file back, which is the check that cannot see this defect.

### Docs: advice data, retention, and maturity claims (issue #91)

Docs only; no behaviour change.

- `docs/DATA.md` (new) says what the desk stores, where, for how long, and what an advice turn sends to each provider: `journal.advice.json` holds the last 40 advice turns in prose and is never expired; the agent workspace `log.md` holds every question and reply with no cap; `compose.yaml` defaults to the agent on the maintainer Worker.
- `docs/CONTRACT.md` and `docs/RUNBOOK.md` no longer say the question and reply are never journaled; they say which file they are not in and which files they are in.
- The Production/Stable classifier is scoped to the bot in README; the agent is a preview because `@cloudflare/computer` is one (its README: "provided as a preview for feedback").
- "Advice is not financial advice" joins the README and RUNBOOK WARNING blocks. The reply footer is a code change tracked separately.
- `SECURITY.md` lists `journal.advice.json` with the other 0600 files.
### The desk now has a supervised restart, and a restart is visible (issue #133)

Measured on the live box 2026-10-08: `mt4-terminal-supervisor` had a time
trigger and ran every two minutes, while `straightedge-desk` had a logon
trigger only, `RestartCount=0`, no next run, and had run exactly once in twelve
days. The MT4 terminal, which holds no risk state, was supervised. The desk,
which owns halt, daily-loss, drawdown, stop management and every refusal gate,
was not. `straightedge-watch` did not exist, so `watchdog.py` (PR#80) was a
reader that nothing ran.

`docs/RUNBOOK.md` had documented the correct two-task arrangement the whole
time. **That is the finding: the procedure was prose, it was typed once, and
nothing ever compared the box to it again.** A runbook is not a control.

- **`deploy/windows/` declares both tasks** as Task Scheduler XML, with
  `Install-Supervision.ps1` (registers them, recording the previous definitions
  FIRST so a rollback is a rollback) and `Export-Tasks.ps1` (read-only dump).
  `deploy/windows/README.md` is the decision record.
- **`python -m straightedge supervision` is the control.** It reads live task
  definitions and goes red: no repeating trigger, a disabled task, a repeat
  slower than the derived staleness threshold, `MultipleInstancesPolicy`
  `StopExisting` (which would make the task END the healthy desk every
  interval), the `schtasks` default `ExecutionTimeLimit` of `PT72H` (which ends
  a healthy desk three days in), a non-interactive principal for a task that
  has to see an MT4 GUI, `--i-accept-risk` in a task (fc34), and an action
  whose arguments are hidden in a wrapper so neither of the last two can be
  checked AT ALL. Read-only: it never registers, starts, stops or edits a task,
  never touches `journal.lock` and never sends to Telegram, so it is safe
  mid-session. `tests/test_supervision.py` reproduces the measured 2026-10-08
  state and asserts it FAILS.
- **The heartbeat carries `run_id` and `started_at`**, one value per desk
  process, so `watch --loop` reports a RESTART whatever the state and counts
  them. Before this, the only trace a restart left was the arming state falling
  back to `live_not_accepted`, and that exists ONLY on a real-money desk: on a
  demo account nothing needs arming, every field read the same either side of a
  crash, and **a crash loop was silent in exactly the configuration the end
  user is shown**. Close restarts are named as a `CRASH LOOP`, because a
  supervisor that papers over repeated crashes converts a loud failure into a
  slow one. A desk too old to publish `run_id` is reported as such, never as
  unchanged.
- The restart interval stays DERIVED: it must be at or under
  `watchdog.stale_after_seconds` computed from the config the desk runs with
  (`#68` is the standing reminder). With no Telegram configured that figure is
  missing its long-poll term, so the audit reports the interval rather than
  judging it against a number no running desk can produce.

Not changed, and deliberately: a desk whose process is alive and whose ticks
have stopped is ALARMED as `STALE`, never killed. Telling alive-but-stalled
apart from dead needs `journal.lock`, and a watcher that can hold that lock for
a moment can make a restarting desk exit `already running`. That boundary is
PR#80's.
### A reconnect records what broke, and a timeout names where (issue #127)

`Engine.step_all` caught the exception that triggers every venue reconnect
WITHOUT BINDING IT, so `_reconnect_broker` wrote `reconnect ok=True` and the
reason was gone. The live journal from the Vultr box over 2026-09-26 to
2026-10-07 is mostly lone `ok=True` lines because of it, and the question that
prompted this (is the bridge flaky and expected on that box, or is it degrading)
could not be put to twelve days of records. The cause of a reconnect is not an
extra detail; without it, a recovered blip and a failing bridge render
identically.

- **The trigger is journaled on the `reconnect` row**, under a `cause` prefix:
  `cause`, `cause_type`, and `cause_op`, `cause_transport`, `cause_phase`,
  `cause_withdrawal`, `cause_req_id` when the venue reports them. Absent means
  the exception did not carry it, never blank and never guessed. `error` on that
  row still means the reconnect ATTEMPT failed, which is a different fault, and
  the two are never merged. One row rather than two events, because a cause and
  its outcome separated by a process exit or the 10 MiB rotation is the state
  this fix exists to leave, and because `tail(n)` counts ROWS.
- **The payload is bounded**: one clipped message, one type name, four short
  enums, one integer, asserted by a test. Issue #119 is open because a journal
  row stored a rendering of other rows; a row written on every blip is the
  obvious next instance of that shape.
- **`BridgeTimeout` names its `transport` and its `phase`**, as attributes first
  and in its message second. Five conditions used to reach the journal as the one
  string `mt4 bridge timeout`: the file mailbox going quiet, an HTTP call that
  got no answer, one that began and stopped, and the shim's own `504` and `503`.
  Measured before and after on the same probe: **1 distinct `(transport, phase)`
  pair out of 5, then 5 of 5.** The vocabulary has one home in
  `broker/mt4_live.py`, and the test compares the set it can produce against
  `BRIDGE_TIMEOUT_PHASES`, so a sixth phase with no case goes red.
- **Two defects adjacent to that one, fixed in the same pass.** A reconnect that
  came back WITHOUT its symbols wrote `ok=True` anyway (`select_symbol` failures
  were swallowed by a bare `continue`); it now carries `unselected` and names the
  symbols on the desk log. And a tick whose account was still unreadable after a
  successful reconnect returned having written NOTHING; it is now
  `account_read_failed`.
- **The Expert is untouched.** Nothing here needs it.

### The read budget is unchanged, and the reason given for it was wrong

`mt4.timeout_ms` stays at 5000. What changed is the claim attached to it.
`config.py` said "205ms p50 and 223ms max, so 5000 is a 22x margin" and
`docs/MT4.md` said "p50 205ms, p90 206ms, max 223ms, and zero round trips over
1000ms", nine lines above its own paragraph explaining that nothing above the
median was trustworthy. `tests/live_measurements.py` is the one home for that
rig's numbers and records the median only. Both restatements are corrected: the
honest statement is 5000ms against a measured p50 of 205ms, 24x the median, with
the TAIL UNKNOWN, and a budget is sized against the tail.

- **The instrument that found the original defect is fixed rather than
  replaced.** `mt4/tools/measure-mailbox.ps1` paired each request with the next
  reply, so one unanswered request shifted every later sample. It now holds the
  open request and EXCLUDES one that got no reply, which is sound because the
  mailbox is a strict singleton, and it reports p50, p90, p99 and max beside
  `paired` and `unpaired`.
- **Pairing by the request id would have been the obvious fix and is the wrong
  one.** Reading the request file means a read handle on the shared name, which
  on Windows can make the Expert's claiming `FileMove` fail: the instrument would
  manufacture the `ERR_CANNOT_OPEN_FILE` failure that #82 exists to fix. The
  script stays read-only over event names.
- **"The median is trustworthy" was luck, not a property of the algorithm.** All
  three losses in the 2026-09 window fell in its last 18%, so about 82% of
  samples were never shifted. Verified by replaying both algorithms over a
  synthetic log built with the desk's real cadence (8 back-to-back ops per step,
  a 15s long poll between steps): with the losses in their measured position the
  old pairing reads p50 212ms / p90 452ms / p99 15639ms against a truth of 205 /
  239 / 1854; with one loss moved to 20% in it reads **p50 418ms against a true
  206ms**. The new pairing matches the injected truth exactly under both.
- **The PowerShell fix is UNRUN against a live terminal** and says so in its own
  header. There is no Windows host and no MT4 on the machine it was written on.
  Its arithmetic was verified by porting both algorithms to Python; its
  PowerShell syntax was not. Do not quote a tail number from this repo until
  Conrad has run it on the box.

### Tickers longer than three characters resolve as pairs (issue #77)

`parse_fx` took the first six alphabetic characters and split them 3 and 3, so
only a three-character ticker could ever resolve, whatever the table carried.
`DOGEUSD` read as DOG/EUS and was allowed-and-recorded as
`currency_limit_not_applicable`, the same as `US30`, so a stack of DOGE, AVAX,
LINK and SHIB longs was four short-USD tickets the limit could not see. Adding
the codes to the table alone was measured and changes nothing: all five still
returned None until the split changed.

- `risk.resolve_pair` matches a base and a quote of any length the table
  carries, both recognised. **Ambiguity rule**, stated in `docs/CONTRACT.md`:
  the split consuming the FEWEST letters wins, then the LONGER base. `USDTRY` is
  USD/TRY and stays so with `USDT` injected into the table (a greedy
  longest-base parse would take USDT and be left with RY). The shipped table is
  prefix-free, pinned by a test, so no real symbol reaches the rule today.
- `AVAX DOGE LINK MATIC SHIB` join the crypto codes. `USDT` is still declined;
  `currencies.py` says why now that it would no longer be inert.
- A symbol no split resolves stays allowed-and-recorded, never refused
  (`PEPEUSD` is the control). A prefix decoration still hides the pair
  (`mEURUSD`, `FXEURUSD`): the base is anchored at the first letter, so those
  were never a 3-and-3 defect and #77 does not change them.
- **The #60 sweep, re-run and widened.** Old denominator 878,800 (every
  three-letter stem x 25 suffix conventions x base and quote position; 9,650
  recognised, 869,150 not). Its formula also assumed every code is three
  letters. Every one of those cases is still swept, against two oracles: an
  independent brute force over every split length, and the pre-#77 3-and-3
  resolver verbatim, which the new one must AGREE with wherever the old one
  resolved. Of the 878,800, 878,800 pass, and exactly two change, both from
  None: `EURDOGe` and `EURDOGecn` now read EUR/DOGE because the suffix letter
  completes the code. The CI sweep adds every longer stem within one letter of a
  code, 257,700 cases, all passing, for a new CI denominator of 1,136,500 (3,504
  newly resolved, every one through a code longer than three letters, pinned).
  Offline, exhaustively: every four-letter stem, 22,848,800 cases, 0 failed, 202
  newly resolved; every five-letter stem, 594,068,800 cases, 0 failed, 2,650
  newly resolved. Those are not in CI because a sweep nobody tolerates there
  gets deleted (#55).
- The pin that asserted these tickers stay None
  (`test_crypto_tickers_longer_than_three_characters_stay_not_applicable`) is
  replaced by `..._resolve`, asserting the new contract.

### The deviation gate judged a number the limit/stop path never sent (issue #92)

Found while working #68, and it is the live residue of #68's fix: that fix had no
reach on the limit/stop path at any layer. `risk.evaluate()` gates EVERY new entry
on `deviation_below_spread`, a signal carrying a `pending_kind` included. The
number was then never transmitted. `WorkingOrder` had no `deviation` field at all,
so there was never a line to drop; `Engine._place_pending` never called
`resolve_deviation`; `_working_payload` put no `deviation` key on the wire; and
`CheckWorking` in `mt4/Experts/Mt4RiskBot.mq4` passed the Expert's own
`input int Slippage = 30` straight to `SendRetry`, while the market and close
handlers both resolved the desk's figure out of the request body.

- **Two harms, and only one of them is certain.** Certain, and it needs no venue
  semantics at all: the gate could REFUSE a limit order over a tolerance that
  order would never have carried, and the remediation the refusal prints (set
  `risk.symbol_deviation_points.<symbol>`) changed nothing on that path. A gate
  judging a value it does not transmit cannot be right. Direction certain,
  magnitude NOT established: the venue was offered 30 points on an instrument
  that measured a 45-point spread, which is the exact condition #68 was filed
  about ("trades randomly do not go through").
- **What is still owed is a LIVE MEASUREMENT, not a code change.** MT4 documents
  the `OrderSend` slippage parameter as IGNORED for pending order types. Nothing
  in this repo can measure that, and nothing here tried. If it holds, the second
  harm is nil for the initial placement and what was left was a record claiming a
  number never offered; if it does not hold, it was a live rejection source on
  gold. **This change must not be read as "gold rejections on limit orders are
  fixed."** A unit test can prove the number is transmitted; it can never prove
  the venue used it.
- `WorkingOrder` gains `deviation: int = 20`, the field and the default
  `MarketOrder` already carried. `_place_pending` resolves it through the same
  `resolve_deviation` call the gate reads, so the number judged and the number
  sent cannot drift apart, and `_working_payload` puts it on the wire.
- The `pending` journal record now carries `deviation` and `deviation_source`, for
  the reason #68 gave for `open` and `close`: a number in a journal cannot tell an
  operator whether their override was consulted, mis-keyed, or never written. It
  carried NEITHER field before, so a limit order's tolerance was unreadable both
  live and after the fact.
- The Expert's pending handler resolves the deviation from the request body and
  keeps the `<= 0` fallback to its own input, the same three lines the market and
  close handlers have. The fallback now covers exactly one case: an older desk
  that sends no `deviation` key at all.
- **Making the gate SKIP pending signals was considered and rejected.** It removes
  the false refusal and leaves the venue receiving 30, which trades a loud wrong
  answer for a silent one.
- `risk.deviation_points` is validated `> 0`. The per-symbol map has had that floor
  since #68 and the global default never did, and the asymmetry was not cosmetic:
  the Expert reads <= 0 as "use my own `input int Slippage`", so a 0 did not
  disable a tolerance, it moved the operator's risk figure to a number configured
  on the other side of the bridge where nothing on this side can read it. The
  entry under #68's "Not fixed here" that recorded this is now resolved.
- **Proven on the WIRE, not on an internal call.** `tests/test_mt4_wire.py`
  asserts the `deviation` key and its value in the `check_working` and `working`
  requests the Expert actually reads, and counts the send ops that carry a
  tolerance: 3 of 3, and it was 2 of 3 (`market` and `close`).
  `tests/test_deviation.py` pins the gate refusing a pending signal, the journal
  record, the config floor in both directions (a bad value refused AND a good one
  accepted), and the shipped `.mq4` (no CI runner has an MQL4 compiler, so a
  source guard over that file is the only gate available there, and it is not
  evidence about a running terminal). Every new guard was driven RED against the
  unpatched source before it was trusted.
- Not changed, deliberately: the MT5 and paper adapters still leave `deviation`
  out of a pending request. MT5's `TRADE_ACTION_PENDING` is a different venue API
  with its own unmeasured semantics, and inventing wire content for a venue nobody
  has measured is the defect this repo keeps finding. The field now exists on the
  venue-neutral model, so adding it there is a one-line change the day somebody
  can measure the answer.

### Three money-safety P0s on the MT4 send path (issue #37)

Found while instrumenting the live desk on 2026-09-25 and recorded on #37 rather
than patched, because rewriting order-submission semantics hours before real money
looked like the wrong risk posture. It was the wrong call: none of the three is
ambiguous about what correct behaviour is, and all three are reachable the first
time gold moves.

#### `/confirm` could send a DUPLICATE market order

`BridgeTimeout` subclasses `RuntimeError`, `Desk.handle` catches `RuntimeError`,
and `Desk._confirm` cleared the pending only on a definitive result. So a
`/confirm` whose bridge call timed out returned the bare string `mt4 bridge
timeout` to the operator with the order still staged and still confirmable.
Reproduced before the fix, in `tests/test_idempotency_guard_can_go_red.py`: two
identical 0.55 lot market orders on the wire, the second reporting success. There
was no client order id, no dedupe token and no in-flight record anywhere, and
`magic` and `comment` were identical on every send, so nothing on the MT4 side
could have rejected the second one either.

- A staged order now carries a client order id, minted once at stage time, kept on
  the `confirm_stage` journal record, and therefore unchanged across a `/confirm`
  that timed out AND across a desk restart in between. The observed failure was a
  silent process restart, so an in-memory set would have been empty in exactly the
  process that most needed to know.
- `src/straightedge/inflight.py` records the attempt to
  `<journal stem>.inflight.json` BEFORE the send (flush, fsync, atomic replace,
  chmod 0600) and clears it only on a VERDICT: success, or a venue rejection. A
  send whose key already has an open record is REFUSED, by `Engine._unresolved`
  for every caller and by `Desk._already_attempted` on the `/confirm` path.
- **Refusal, not reconciliation, and that is the decision this change makes.** A
  timeout is not evidence the order did not reach the broker, and an EMPTY BOOK is
  not evidence either: the desk's budget expires while the Expert may still be
  inside `SendRetry`, so the position the send is about to create is not visible
  yet. Reconciliation is attempted and reported (`Engine._after_unresolved_send`
  reads the book once and names any position whose comment carries the key), but
  silence is never read as "nothing happened". Refusing costs an order; sending
  twice costs money and cannot be undone.
- The key is prefixed into the order comment, so a position on the book is
  attributable to one send. That is corroboration and NOT the dedupe mechanism:
  MT4 brokers append to and overwrite `OrderComment`, so a key MISSING from the
  book proves nothing, and a dedupe built on looking it up would fail in the
  dangerous direction.
- A CLOSE is deliberately not guarded this way. The ticket is already the key at
  the venue, so closing #N twice fails the second time; an OPEN has no equivalent.
- New journal events: `send_unresolved`, `send_refused_unresolved`,
  `confirm_unresolved`, `inflight_unreadable`. `Engine.start()` re-announces every
  open record on EVERY start, before the venue is touched.

#### A timed-out request stayed in the mailbox and the Expert could execute it LATER

`FileBridge._exchange` raised with no cleanup, leaving the `.req` file on the
shared name addressed to an Expert that had not claimed it. The Expert polls every
100ms and executes whatever it finds, so a trade the operator was told had FAILED
could fire minutes afterwards, unattended. Across a desk process exit nothing
bounded "later" at all, and a silent restart with no traceback was observed on the
live box at 2026-09-26T01:56:49Z. The old behaviour was pinned on purpose by
`test_the_request_stays_in_the_mailbox_after_a_timeout`, whose reasoning ("a desk
that keeps going is fine") holds for a read and not for `op=market`; that test and
its duplicate in the net-transport suite now pin the opposite.

- The desk WITHDRAWS the request before raising, and `BridgeTimeout` states which
  outcome it got: `withdrawn` (provably gone, cannot fire), `claimed` (the Expert
  already has it and may be executing it now), `locked` (still there, could not be
  removed). Best effort is never reported as a guarantee.
- Every request carries `ttl_ms` and the Expert refuses one that is older,
  answering `retcode=4109 error=request_expired survivor_ticket=0`. This is the
  layer that survives the desk not being there any more, and it is the only one
  that does. A DURATION and not a deadline: with `mt4.mailbox_url` the desk and
  the terminal are different hosts, and a wall-clock deadline crossing that
  boundary makes a stale request look FRESH whenever the terminal's clock runs
  behind.
- The Expert MEASURES the file-time offset at `OnInit` by writing its own probe
  file, because MQL4 does not state whether `FILE_MODIFY_DATE` is local time or
  UTC and the two wrong answers fail in opposite directions: one stops the desk
  working, the other silently removes the fence. An UNMEASURABLE age refuses a
  SEND and allows a READ.
- New Expert input `FenceGraceSec = 2`, which absorbs the one-second resolution of
  a filesystem timestamp in the direction that cannot kill a live request.

#### One `timeout_ms` covered reads AND sends, and the Expert could outlast it

A read that times out is cheap. A send that times out is the ambiguous case above,
and the desk giving up while the Expert is still laddering `OrderSend` is how that
case is reached: 6 of the 88 desk timeout events on 2026-09-25 had no Expert-side
drop behind them.

- `mt4.timeout_ms` (default 5000, UNCHANGED, and unchanged on purpose: it was
  never the defect and `watchdog.venue_timeout_seconds` plus
  `tests/test_mt4_claim_open_retry.py` both derive numbers from it) is now the READ
  budget. New `mt4.send_timeout_ms` covers the ops that can change the book.
- The default is DERIVED, not chosen, and `constants.derive_send_timeout_ms()` is
  its only home: 1000ms transport ceiling + 950ms of `Sleep` inside the Expert's
  three retry ladders + 360ms of claim-open retries + 19 broker round trips at a
  250ms allowance = **7060ms**.
- Two of those terms are worth reading twice. The transport ceiling is about 5x the
  p50 of 205ms over 2166 requests, and NOTHING above that median is quoted: the
  pairing used to compute latency shifts by one after every unanswered request and
  three went unanswered, so p90 and above are unreliable and
  `tests/live_measurements.py` says so. And the claim-open term is TWO ladders, not
  one: #82 retried the claim read, #83 then gave the reply write the same bounded
  retry on the same knob, and both sit between the desk's request and its reply, so
  both spend the send budget. That term moved from 180 to 360 when #83 landed while
  this change was in review, which is exactly the drift the test now counts the
  loops to catch.
- The 250ms per broker call is labelled an ALLOWANCE because no `OrderSend` latency
  against the live OANDA account exists yet; the first market-hours window
  measures it.
- `cfg.validate()` refuses a send budget at or below the read budget, and one
  inside the Expert's `Sleep` total.
- **The requirement is now mechanical rather than documented.** The Expert's retry
  bounds are `input` parameters, so the attached Expert can differ from this
  repo's copy with nothing saying so. It therefore declares `ladder_ms`,
  `broker_calls` and `fence` on every ping reply; `Mt4Broker` checks the budget
  against the declaration and warns loudly at connect, `send_fence_report()` names
  the verdict, and `doctor --connect` exits non-zero on `TOO SHORT`. An Expert too
  old to declare reports `NOT MEASURED`, never `ok`.
- Over the network the shim is never the end that gives up first:
  `FileBridge.exchange` sizes its mailbox wait from the request's own `ttl_ms`,
  clamped to its configured ceiling.

#### What this does and does not establish

MQL4 does not compile in CI or on the developer seat, so the Expert half of the
second and third fixes is covered by SOURCE GUARDS only and has been executed
nowhere. It must be recompiled in MetaEditor and re-attached on the terminal host
before it is trusted. Every fix degrades safely against an un-updated Expert and
says which half is missing: the duplicate-order fix is entirely desk-side and works
unchanged, the stale-request fix keeps only the desk-side withdrawal, and the split
budgets work while the declaration check reports `NOT MEASURED`.

### The heartbeat has a reader, and it distinguishes three states (issue #38)

`journal.heartbeat` has been written on every successful `step_all` since 1.0.0,
`docs/RUNBOOK.md` named it as the watchdog target in two places, and NOTHING in
this repo ever read it. A desk that died on Tuesday was discovered on Friday, and
every indicator an operator could see read healthy in between. `docs/MT4.md`
already said the honest half out loud: an in-process startup wait "does not cover
MT4 taking longer than the budget, or MT4 dying later, because a process that has
exited cannot retry anything".

- `python -m straightedge watch` reads the file in a SECOND process and names the
  state: `ALIVE ARMED` (exit 0), `ALIVE NOT TRADING` with the gate's own reason
  (exit 3), `STALE` (exit 4), `UNKNOWN` (exit 5). `--loop` alerts the locked chat
  on the first check and on every change after it, and `--ok-every` confirms a
  healthy desk on a cadence so that silence becomes a signal too. It is a
  separate process because a desk cannot report its own death, and an in-process
  staleness check is an instrument that fails together with its subject.
- **Three states, because two is the defect.** Live arming is per process on
  purpose (fc34, and `Desk.restore_from_journal` refuses to re-arm from a
  `live_on` record), so a crash restart brings the desk back ticking and
  DISARMED. An up-or-down watchdog calls that healthy, and it is "your bot
  silently stopped trading", which is the exact failure an unattended week
  produces. Nothing here makes live survive a restart and no config key was added
  that could: the reader is read-only and holds no arming path at all.
- `blocked=` in the heartbeat is the string `RiskManager.circuit_reason` returned
  for that same account at that same instant, so the file cannot claim the desk is
  armed while a send would be refused. It is not a second copy of the gate.
  `_apply_circuit` now returns the halt reason instead of a bool so the halted
  path can name itself too; both call sites read identically.
- **The threshold is derived from config, never chosen.** `poll_seconds`, the
  Telegram retry ceiling (`RETRY_TRIES` attempts at `poll_seconds` plus
  `POLL_TIMEOUT_MARGIN_S`, with gaps up to `RETRY_CAP_S`) and two venue commands
  at the mode's own `timeout_ms`, plus `NET_GRACE_SEC` when `mt4.mailbox_url`
  points at a remote shim. The shipped example config derives 428s on MT4 and
  648s on MT5; one constant could not have been right for both, which is the
  `deviation_points` finding (#68) on the time axis. There is deliberately no
  config key for the alarm window.
- **The one term that is not a measurement says so, and is checked.** The work
  after `account()` scales with the book and no config value bounds it, so the
  budget is doubled to cover it. The desk publishes `tick_gap_max_s`, the longest
  gap it has really observed, and sets `over_budget=1` when that passes the
  budget. It does NOT widen its own threshold: a gate that relaxes itself until it
  stops firing can no longer go red.
- `STALE` is not called DEAD. A desk whose venue link is down writes nothing,
  exactly like one that exited, and separating them needs `journal.lock`. This
  command will not take that lock even briefly, because holding it can make a
  restart exit `already running`, which is a watchdog that can kill the desk. The
  alert names both readings and points at `reconnect` in the journal.
- It never calls `getUpdates`. Two pollers on one bot token steal each other's
  commands, so the client is built with no offset path, and a test asserts that a
  whole watch run issues zero `getUpdates` requests.
- **The scheduled task is documented, with the half a restart cannot fix in the
  same breath.** `docs/RUNBOOK.md` gains "Watchdog" and "Unattended (Windows
  scheduled task)": two tasks, a repeating trigger AS the restart-on-failure
  (Task Scheduler does not start a second instance, and `journal.lock` is the
  second barrier), the fc34 warning restated where the task is created, and an
  acceptance drill that has the operator kill the desk and watch the alarm fire
  before leaving it alone for a week.
- Heartbeat format: LINE ONE is still the bare ISO timestamp, byte for byte. The
  `key=value` lines come after it, so every older reader and every older doc stays
  true. `docs/CONTRACT.md` carries the format.
- `src/straightedge/watchdog.py` ships with a per-file coverage floor. It is the
  only thing that tells an operator the desk is alive AND armed, and it runs in a
  process nobody else is watching.

### Found on main while doing the above: the package reported the wrong version

`pyproject.toml` read `version = "1.5.0"` and `src/straightedge/__init__.py` read
`__version__ = "1.4.2"`. `pip` reports the first and `doctor` prints the second,
so a 1.5.0 install told its operator 1.4.2, which is the number that goes into a
handover checklist and into any bug report the end user files. No test read either
declaration, so one of the two was always going to be missed in a release commit.
`__init__.py` is corrected to 1.5.0 and `tests/test_version.py` pins the two
together.

### The desk comes off the MetaTrader 4 host (#73)

Conrad ruled 2026-09-25 that the EA transport moves before the first paying
customer. It has moved, and the decision record is **`docs/TRANSPORT.md`**.

The desk had to run on the customer's Windows box because the only transport was
MT4's `FILE_COMMON` mailbox, which the desk read directly. That is why the
reboot on 2026-09-25 could kill it: two processes had to be co-resident and
correctly ordered on a machine we do not own.

**What was chosen, and what was rejected.** Three shapes were weighed, not two.
The EA calling `WebRequest` for its own decisions is rejected outright: MQL4's
`WebRequest` is synchronous, so it would put our risk engine and our model call
inside a blocking call on the customer's chart thread, and it would move the
halt, approve, auto and live gates behind their terminal's polling. A gate that
is only enforced while the customer is polling is not a gate. Making the EA a
dumb transport client is the right destination and is deferred: that code lands
in the one artifact a customer installs, no CI runner has an MQL4 compiler, and
it needs a rendezvous service and a manual per-terminal allowed-URL whitelist
first. What shipped is the reversible, verifiable step in that direction.

- **`straightedge mt4-shim`** runs on the MT4 host and serves that host's
  mailbox over one authenticated `POST /mt4/call`. It holds no risk logic, no
  prompts, no model keys and no journal.
- **`mt4.mailbox_url`** on the desk switches the transport. `Mt4Broker` already
  took a `call` seam, so this is another implementation of it: the engine, the
  risk gates and `mt4/Experts/Mt4RiskBot.mq4` are unchanged. The Expert is
  **byte-for-byte the same file** and still issues zero `WebRequest` calls, so
  the synchronous-WebRequest problem is not solved here, it is not incurred.
- **Nothing inverts.** The desk is still the initiator, so `HALT`, daily loss,
  drawdown, currency exposure, sizing, `approve always`, `/auto` and the
  real-money fuse all stay in the desk's process and on the desk's clock. The
  inbound listener is on the CUSTOMER's host; the desk binds nothing.
- **One wire format.** The HTTP body is the same `key=value` mailbox block
  `docs/MT4.md` specifies, carried opaquely and never re-serialized, so the
  golden transcripts still describe the bytes that cross the network and the
  desk's request id travels end to end untranslated.
- **Auth is `MT4_MAILBOX_TOKEN` on both ends, environment-only, minimum 32
  characters, with no off switch.** The shim checks it before the path, before
  the method and before the body; every unauthenticated request gets `401`
  whatever it asked for, so the surface is not mappable, and the tests assert
  the strong property: after a refusal the mailbox directory is EMPTY. The
  listener binds `127.0.0.1` and refuses a routable address without
  `--i-understand-plaintext`, because `http.server` has no TLS; the supported
  exposure is a Cloudflare Tunnel, which opens no inbound port at all.
- **The `startup_connect()` partition survives the hop, and the standard
  library's default would have broken it.** `urllib.error.HTTPError` subclasses
  `OSError`, which `startup_connect()` retries, so a `401` would have been
  retried in silence for the whole 180 second budget: the same defect class as
  the boot bug #74 fixed. The shim answers `504` / `503` for "the Expert has not
  replied" and "the mailbox is not writable", the desk turns those back into
  `BridgeTimeout`, and every other status is a `RuntimeError` that is never
  retried. The shim's mailbox budget is `mt4.timeout_ms` and the desk allows
  that plus 2 seconds, so the shim gives up first and the desk learns which end
  did.
- **Both gates were driven red on purpose.** Deleting the `HTTPError` arm makes
  a `401` burn the full 8 second test budget across 5 retries; deleting the
  `do_*` catch-all in the handler makes an unauthenticated `PROPFIND` leak a
  501 page naming the method before auth runs.
- `FileBridge` gained `exchange(body, req_id)`, the raw round trip the shim
  serves. It returns the Expert's own bytes rather than a decoded dict, because
  `decode` coerces types and a round trip through it is not the identity.
- `doctor` now prints which transport the config will use and presence-checks
  the token without printing it. `journal.py` redacts `mailbox_token` by name,
  because the existing match is exact-key and `token` alone would not catch it.

**What this does not do.** It does not remove Python from the customer's host,
only the risk engine, the model access, the prompts, the secrets and the
journal. It does not remove auto-logon, the Windows box or MT4, which are
required by MT4 itself and which no transport choice changes. And off-box, a
network partition is a new way for the desk to be unable to flatten: what
protects a position in that window is the broker-side stop the Expert attaches
on every entry, not the desk. `docs/TRANSPORT.md` states all of it.
### Crypto pairs count toward the currency-exposure limit (issue #66)

`max_currency_exposure` applied only when BOTH halves of a symbol resolved to a
recognised code, and the recognised set was ISO 4217 plus the four metal codes
ISO assigns. Crypto codes are not in ISO 4217, so `BTCUSD` did not resolve, the
limit DID NOT APPLY, and the USD leg of a crypto position did not count. Holding
`BTCUSD`, `ETHUSD` and `EURUSD` showed the limit one USD leg where three
existed. Conrad ruled: add the crypto pairs.

- The recognised table now carries the crypto majors
  (`ADA BCH BNB BTC DOT EOS ETC ETH LTC SOL TRX XBT XLM XMR XRP XTZ ZEC`),
  the same way it already carries `XAU` and `XAG`. One table, one code path.
- **A crypto code shares the ONE bucket per code.** A buy of `BTCUSD` is +BTC
  and -USD, so its USD leg is counted identically to that of `EURUSD` and
  `XAUUSD`, and its BTC leg caps `BTCUSD` against `BTCJPY`. The gate counts
  TICKETS, never money, so crypto volatility differing from FX volatility is not
  what it compares; per-unit risk is equalised by sizing and by the daily-loss
  and drawdown gates. A separate crypto bucket was rejected because it would let
  a fourth short-USD ticket in unseen.
- **Behaviour change for a crypto book.** A crypto position now consumes
  currency exposure against FX positions, so a book that stacked several long
  crypto pairs against USD will start seeing `currency_exposure` refusals, and
  `currency_limit_not_applicable` is no longer journaled for a crypto symbol.
- No venue spelling is hardcoded: `BTCUSD`, `BTCUSDT`, `XBTUSD`, `BTC/USD`,
  `BTCUSD.m` and `#BTCUSD` all resolve through the existing first-six rule.
  `BTCUSDT` folds the Tether leg into USD, which is the intended reading.
- KNOWN LIMIT, documented rather than implied: the resolver splits the first six
  alphabetic characters 3 and 3, so only a three-character ticker can resolve.
  `DOGEUSD`, `AVAXUSD`, `LINKUSD`, `MATICUSD` and `SHIBUSD` remain
  allowed-and-recorded. Widening the split changes how every symbol resolves and
  belongs in its own unit.

### The desk survives a reboot (MT4 startup wait)

The Windows host was rebooted for the first time since setup. MT4 came back
healthy, `Mt4RiskBot` was attached to XAUUSD,H1 with the smiley, and the
mailbox path was correct. The desk was dead, and had to be started by hand.

`Engine.start()` called `Mt4Broker.connect()`, which sent exactly ONE `ping` on
the 5 second per-command timeout. MT4 is a GUI application that needs tens of
seconds to launch, load the terminal, log in to the broker and reach the
Expert's first timer tick, so a desk started from a boot-triggered scheduled task
loses that race. The ping raised `mt4 bridge timeout`, the process exited, and
nothing retried. **After any reboot, MT4 looked perfectly healthy and the bot
was silently gone**, with no indicator anywhere showing it, and an operator
with no shell on the host could neither notice nor fix it.

- `Mt4Broker.startup_connect()` retries the ping with a growing gap (1s,
  doubling to 5s) until the Expert answers or `mt4.startup_wait_sec` is spent.
  Default 180 seconds, about twice the slowest cold start observed by hand, and
  biased long on purpose: too short kills the desk on every reboot, while too
  long only delays the report of a real misconfiguration.
- **Bounded, never infinite.** Worst case is the budget plus one `timeout_ms`.
  An unbounded wait would turn a wrong `files_dir` or a detached Expert into a
  process that hangs forever looking busy, which is not better than a crash.
- **It says it is waiting.** Every attempt prints to stdout with its elapsed
  time, flushed per line, because a redirected log block-buffers and three
  minutes of silence is indistinguishable from a hang. Giving up names the
  attempt count, the elapsed time and what to check.
- **The steady-state budget is untouched, and that is the point.**
  `Engine.step_all()` calls `ensure_connected()` on every step and
  `Engine._reconnect_broker()` calls `connect()` on the trading path. Both keep
  the 5 second budget, so a transient blip cannot become a multi-minute stall
  while the desk holds live positions. `doctor --connect` keeps it too:
  interactive diagnosis should fail fast. Startup tolerance and steady-state
  tolerance are different numbers, so they are different methods.
- **An `ok=0` reply is not retried.** A live Expert stating a diagnosis is not
  a bridge that has not appeared yet, and waiting cannot fix it. The partition
  is a type: `FileBridge` now raises `BridgeTimeout`, a `RuntimeError`
  subclass, so every existing `except RuntimeError` caller is unaffected while
  the retry can key on the transport failure specifically rather than on the
  wording of an error string.
- New optional `[mt4] startup_wait_sec`. Omitted means the adapter default;
  `0` means one ping and no wait, the previous behaviour. An unset key stays
  `None` in `Mt4Config` rather than becoming a copy of 180, so the default has
  exactly one declaration, in the module that implements the wait.

Not the bug, and checked before the fix: the Expert was attached and running
the whole time, the mailbox path was correct, and the autostart chain works
(proven by two reboots). Only the desk's startup impatience was broken.

`tests/test_mt4_startup_wait.py` drives all of it against a real mailbox
directory and real wall-clock time, including the give-up path, because a retry
that cannot give up is the same defect wearing a fix's clothes.

### A symbol with no price series is named, not silently untradeable (issue #33)

MT4 keeps a price series per symbol AND timeframe and builds one only when
something asks for it. With H4 charts open and the desk on H1, the rig measured
`EURUSD bars=0 ATR=nan`, `USDJPY bars=0 ATR=nan`, `XAUUSD bars=200 ATR=17.2188`;
XAUUSD was the only charted symbol. Nothing crashed. `step_symbol` asked for
bars, got none, and returned with no journal record, so two configured symbols
were untradeable and the operator-visible symptom was "the bot will not trade
EURUSD" with no cause anywhere. (The spread gate is not what refused:
`risk.py:523` reads `signal.atr > 0`, which is False for nan.)

- **The operator opens no charts.** The desk asks the terminal for every
  configured symbol's series at startup, which is what makes MT4 request it from
  the server, and waits up to ten attempts one second apart. The ask is the fix,
  so the ordinary cold start is repaired rather than reported.
- `Mt4RiskBot.mq4` `RatesReply` now selects the symbol itself, touches the series
  when it is short of the requested count, and reports `bars_total`, `selected`
  and `history_error` on every reply. Previously `iBars() == 0` set the row count
  to zero, the row loop never ran, and no price-series function was reached at
  all, so no amount of asking could warm a cold symbol. **Recompile and reattach
  the Expert.** An older Expert still works; its three fields read as `None`,
  which is reported as COULD NOT MEASURE rather than as zeros.
- `doctor --connect` prints bars and ATR per symbol and exits non-zero when any
  configured symbol cannot produce a signal. Plain `doctor` prints
  `history: NOT MEASURED`, because an absent check reads exactly like a passed
  one. #33 B1 makes `doctor` exit 0 part of the pass condition before a run, and
  `[symbols] names` is the scan list, so a name in it is a symbol the desk will
  try to trade.
- `run` refuses to start when a symbol is still unusable after the warm-up. It
  names every such symbol on stderr and exits 2. Ordered preference is
  it-just-works first and a named failure second; a silent non-trade is on
  neither list.
- `history_error` separates two worlds the wire could not tell apart: `4066`
  `ERR_HISTORY_WILL_UPDATED` (downloading, wait) from `4073`
  `ERR_NO_HISTORY_DATA` and from `0` (the terminal is not fetching, so the symbol
  name or the broker is the problem). `ok=1 n=0` used to mean all three at once.
- Partial history is called out separately. `Strategy.signal` returns FLAT
  `warmup` below `needed_bars()`, so a half-downloaded series produces an ATR,
  looks alive, and never trades.
- **Nothing is filled in.** A missing series gets no synthetic bar, no default
  ATR and nothing borrowed from another symbol; a missing ATR is `None`, not 0.0
  and not nan. `broker/mt4_live.py:294-308` is what that cost last time.
- `/symbols add` warms and measures the new name, and answers with the result
  instead of a bare `added`.

Advice-staged symbols are whitelisted, and there are daily caps on sends and on advice turns (issue #13).

### The model no longer picks the instrument unchecked

- `cfg.symbols` is a SCAN list: it drives `/quote` and the auto scan. `market_signal` accepts anything the broker knows, so with `/approve always` the model chose the instrument and no gate checked it.
- A symbol the MODEL picked is now checked against `advice.symbols`, falling back to `symbols.names` when that is empty. Empty never means "allow anything". A miss is `reject` with reason `symbol_not_allowed`.
- The whitelist is a separate knob from the scan list on purpose: an operator who scans three pairs may still want to act on a fourth BY HAND. A human `/buy` is therefore NOT gated by it. The control exists because the model chose the symbol, not because the symbol is unusual.
- It gates OPENS only. An advice `close` on an unlisted symbol is still allowed: a control that can stop you reducing exposure is not a risk control.
- Precedence is explicit: the whitelist runs before the order is built, so an unlisted symbol reports `symbol_not_allowed` rather than `advice_stage_failed`. A rule genuinely did say no, and it avoids spending broker calls on an order that was never permitted.

### Two daily caps, because they bound two different things

- `risk.max_trades_per_day` counts OPENING sends across auto, telegram and advice, and refuses with `max_trades_per_day`. Churn was previously bounded only by `max_positions` plus `daily_loss_pct`, and `daily_loss_pct` fires after the money is gone. Counted at the send, never at the decision: a preview, a refused confirm and an expired stage all call `evaluate()` and none of them is a trade. Closes never count and are never capped.
- `advice.max_turns_per_day` counts advice turns and refuses BEFORE the provider is called. This is a COST control as much as a risk one: hosted inference is billed per turn, and a turn costs money whether or not it ends in an order, so the send cap cannot see that spend at all. A check placed after the call would cost exactly what it is meant to save.
- Both counters are durable beside the journal, in the same snapshot as the loss budget, for the same reason: a cap a crash loop can clear is not a cap. They reset only on a new UTC day.
- Both default to `0`, which disables them, so no existing config changes behaviour.

### Snapshot schema

- `journal.equity.json` is version 2, carrying `trades_today` and `advice_turns_today`. Version 1 still loads, with the counters restored as 0: a reader that rejected the version it wrote yesterday would fail closed on every existing install, which is a self-inflicted outage rather than a safety property. An unknown version is still refused.

### Slippage tolerance is per symbol, and a deviation the spread cannot fit is refused

Measured by Conrad on the live MT4 rig, 2026-09-24. `[risk] deviation_points` was a single global, default 20, and it is the maximum tolerated slippage in POINTS. A point is instrument-specific, which is the defect: on a 5-digit EURUSD 20 points is 2 pips, and on XAUUSD it is 20 cents against a measured spread of 45 points ($0.45), on an instrument whose ATR(14) H1 was $17.22. Every gold send was therefore offering the venue less than half of one spread of tolerance. `OrderSend` rejects that intermittently and nothing in the log named the cause, so what reached the operator was "trades randomly do not go through". End to end the number ran `engine.py` -> `mt4_live.py` -> `Mt4RiskBot.mq4`, which uses the value passed and falls back to its own `input int Slippage = 30` only when that value is <= 0.

- `[risk.symbol_deviation_points]` overrides `deviation_points` per symbol. Resolution is symbol-specific first, then the global default, and the answer carries its own provenance: every `open` and `close` record in `journal.jsonl` now has `deviation` and `deviation_source` (`symbol` or `default`), because 20 points read in a journal cannot tell an operator whether their override was consulted, mis-keyed, or never written. Closes resolve per symbol too and are never gated on it: a control that can stop you reducing exposure is not a risk control.
- Matching is case-insensitive and otherwise exact. A venue that calls gold `XAUUSD.m` must be keyed `XAUUSD.m`, because stripping a decoration means guessing which decorations name the same instrument. A near-miss falls back to the global default, which is a failure the map alone cannot catch, which is why the map is the smaller half of this change.
- **The gate.** A new entry whose effective deviation is below `min_deviation_spread_multiple` (default 1.0) times the LIVE spread is refused as `deviation_below_spread` before the order is sent. A misconfiguration that produced intermittent venue rejections is now one named refusal at our own gate. The reason names the symbol, the effective deviation, its source, the measured spread, the floor enforced, and the config key and value to set: it reaches the operator verbatim (`refused: <reason>`), and a refusal without the number to set is one an operator switches off rather than acts on.
- It refuses; it does NOT raise the deviation. Silently overriding an operator's risk figure would leave a number in force that is neither what they set nor anything they can read.
- **The default multiple is 1.0, and a larger one was tried first and is wrong.** 1.0 is the only multiple that needs no measurement: a market order crosses the bid/ask gap, so a tolerance below the spread can only be rejected. At 3.0 this gate refused 88 tests in this suite, every one of them encoding the paper broker's own EURUSD: a 10-point spread against the 20-point default, a 2x ratio that is ordinary retail FX and fills routinely. The evidence bounds the real boundary between 0.44x (gold, measured rejecting) and 2.0x (working) and does not locate it, so a default above 1.0 would encode a guess as though it had been measured, and would refuse trades on the strength of it. The multiple is configurable for an operator who wants margin, and `validate()` refuses anything below 1.0: there is no value that switches the gate off.
- The headroom the refusal RECOMMENDS is a separate constant (`DEVIATION_HEADROOM_MULTIPLE`, 3.0) from the floor it ENFORCES, because they are different kinds of claim. The floor is arithmetic and may refuse a trade. The headroom is judgement, it exists so an operator who writes the exact floor is not refused again by the first tick that widens the spread by one point, and judgement is not allowed to refuse anybody's trade.
- The spread is read at evaluate time, from the same tick the stop and sizing gates used, not from `spec.spread` (which the MT4 bridge does not populate) and not from a configured typical. That makes the gate as transient as the market, so `spread_too_wide` is applied FIRST: a temporary blowout is named as a wide spread, and only a spread the instrument carries under normal conditions is named as a misconfiguration. A spread of zero or less is a broken tick, and the gate abstains rather than reporting a pass it did not measure.
- `config.example.toml` and `config.handover.toml` seed `XAUUSD = 150` with the measurement in a comment. `XAGUSD = 150` is seeded because it has the same SHAPE and is labelled as NOT measured. No other symbol gets a value, and the measured numbers live in one place, `tests/live_measurements.py`, which separates what was read off the rig from what was merely chosen to make a fixture runnable.

### The refusal-reason roster is a denominator again (issue #61, closed)

- The roster asserted itself against `risk.py` by regex, and the regex required a closing quote. `reason="spec_not_measured:" + join(...)` therefore matched nothing, so the roster read GREEN while the module carried a reason no case covered. The instrument had silently stopped measuring its subject, which is the class this repo keeps finding.
- It now reads the AST (`tests/refusal_scan.py`), so a concatenation, an f-string, a module constant and a conditional all resolve. **The fix that matters is not the parser: it is that a site the scanner cannot read is REPORTED, not skipped.** `unresolved` is asserted empty before any count is compared, because a denominator built on a partial read is not a denominator.
- Sites that forward a reason rather than name one (`reason=self._halt_reason`) are returned and PINNED, not ignored. A silent ignore list is the same defect in a better costume, so a new forwarding form fails the suite and gets looked at.
- The count went 22 -> 24 the moment the scanner could see. The two that were missing are `spec_not_measured`, the reason #61 named, and `deviation_below_spread`, which was written in the SAME concatenated shape on purpose: rewriting it into a friendlier form would have left the fix unproven against the form that broke the scanner.
- `test_refusal_scan.py` feeds synthetic source, because a test that could only run against today's `risk.py` is pinned to the shapes that file happens to contain, which is how the hole opened. It includes the superseded regex as an executable assertion, so "the old scanner was blind to this" is measured rather than asserted in prose.

### Not fixed here

- `tests/test_refusal_reasons.py` runs most of its cases on a default tick of 20 points against the 20-point default deviation, i.e. EXACTLY at the new floor. It passes, and it is pinned by a boundary test so a later loosening to `<=` fails by name rather than turning half the suite red for no stated reason. The fixture itself is left alone.
- The paper broker's `default_spec` has no metals branch: `default_spec("XAUUSD")` returns a 5-digit FX spec with `point` 0.00001, so paper-mode gold is not gold. Fixing it needs a contract size and tick value nobody has read off a live venue, so it is reported rather than invented.
- No gate owns a crossed or zero-spread tick. `deviation_below_spread` abstains on one and says so, rather than absorbing a second defect quietly.
- `deviation_points = 0` is still accepted by `validate()`, and the MT4 Expert reads <= 0 as "use my own `input int Slippage`". That silent handover to a number configured on the other side of the bridge is now CAUGHT by the deviation gate rather than by validation, which is enough to stop a send, but the config key itself is still unvalidated. RESOLVED by issue #92 above: `validate()` now refuses it and names the key.
- Nothing here ran against the rig, which is SSH-keyed from the lead's laptop only. That the gate refuses the configuration measured rejecting is proven in the suite; that a gold send at 150 points then FILLS is COULD NOT MEASURE from here.


### Two attached Experts can no longer both send the same order

- `Process()` in `mt4/Experts/Mt4RiskBot.mq4` claimed a request by reading the shared name and deleting it AFTERWARDS, with `FILE_SHARE_READ|FILE_SHARE_WRITE` on the open and only a per-instance `gBusy` flag for exclusion. Two attached Experts both read the whole body and both called `OrderSend`: one requested trade, two positions, double the sized risk, and only one of them journalled, which corrupts every drawdown, equity-peak and exposure figure derived from the journal. The only mitigation was the sentence "attach to one chart", written four times across the docs. That is a documented rule with no mechanism, on the order-execution path.
- The Expert now CLAIMS the request before reading it. It takes a terminal-wide mutex (`GlobalVariableSetOnCondition`, the one primitive MQL4 documents as atomic, and documents for exactly this use), renames `mt4_risk_bot.req` to `mt4_risk_bot.req.claim.<chart id>`, and reads the body from the claimed path. A loser finds the source gone, logs `claim lost`, and executes nothing. `FileMove` is the second barrier, never the guarantee: MQL4 does not document it as atomic.
- `OnInit()` takes a second terminal-wide lock and returns `INIT_FAILED` when another instance holds it, so a duplicate attach refuses to start and says why in the Experts log instead of silently competing. The holder refreshes the lock on every timer tick and market tick and releases it in `OnDeinit`; a crashed instance frees it after `SingletonStaleSeconds` (default 15), and both locks are temporary globals that MT4 deletes at terminal shutdown, so neither can survive a crash on disk and block a legitimate restart.
- An orphaned claim file is not the mailbox and blocks nothing; the same chart overwrites it on its next claim. The orphaned request is deliberately NOT replayed, because replaying a claimed order after a restart is the duplicate this change removes.
- Scope stated plainly in `docs/MT4.md`: MQL4's atomic guarantee is per TERMINAL. Two MT4 terminals on one host share Common Files, and there the rename is the only barrier.
- `mt4/README.md` and `docs/MT4.md` no longer ask the operator to remember "one chart". They state what the software enforces and what happens if two are attached.

## 1.5.0

The last-line size guard can fire (issue #55). It was dead. It recomputed the cap `lots_for_risk` had already applied, from the same entry, stop, spec and equity, with a LOOSER tolerance (`1e-6` against the sizer `1e-9`), so every input that would have tripped it had already been turned into 0 lots and reported as `size_zero`. A 497,664-case sweep reached the line 114,840 times and tripped it zero times.

- The guard now takes TWO caps and refuses on the tighter of them. The first is the old per-trade cap, kept as the backstop for a future change that loosens the sizer, with its tolerance brought into line with the sizer own (`1e-9`, not `1e-6`). The second is `RiskManager.loss_room`: what the account may still lose before the daily-loss or max-drawdown halt trips. That figure is derived from the persisted `EquitySnapshot` (`day_start_equity`, `peak_equity`), which `lots_for_risk` is never given, so the gate can DISAGREE with the sizer instead of recomputing it. A second layer that reads the first layer inputs is not a second layer.
- **Behaviour change an operator will see.** A trade whose full stop-out would carry the account through the daily-loss or drawdown halt is now refused as `size_exceeds_risk` instead of being sent. The halt used to fire after the loss; it now also refuses the size that would cause it. A configuration where one trade risks more than the whole daily loss budget (`risk_pct` times `max_risk_multiple` above `daily_loss_pct`) refuses every entry rather than sending trades the daily loss limit cannot absorb.
- The same 497,664-case sweep now reaches the line 114,840 times and trips it 54,111 times. A smaller sweep of the same shape ships as a test (2,880 cases, 1,080 reached, 444 refused, 636 allowed) and asserts BOTH counts: a guard that refuses everything is as useless as one that refuses nothing. The shipped grid is a quarter of the measured one because each case rewrites the persisted snapshot, which took the Windows CI leg from 65s to 4m37s at 11,520 cases; a sweep nobody tolerates in CI gets deleted.
- Independence is asserted as an experiment, not as an argument. Every input `lots_for_risk` receives is held exactly constant, only the persisted snapshot moves, and the verdict flips from `ok` to `size_exceeds_risk`.
- The per-trade half still cannot fire against the current sizer, by construction. It is exercised by a test that loosens the sizer by 1.5x on purpose and watches the refusal, so the term is a backstop and not decoration.
- `size_zero` and `size_exceeds_risk` stay two different words for two different situations, asserted on one manager and one account with only the stop distance changing.
- **The pin that held this line dead did NOT fail when the line became reachable, and that is the second finding.** `test_size_exceeds_risk_is_dominated_by_size_zero` said it would FAIL the day the guard could fire. It re-implemented the guard old arithmetic from `lots_for_risk` instead of calling `evaluate`, so what it measured was the SIZER, which this change does not touch, and it stayed green through the whole of it. It is deleted on purpose, named in the PR that deletes it, and replaced by a case that exercises the reason. A pin on a dead line has to call the line.
- Not fixed here, and still open: `size_zero` remains one word for two situations (a degenerate input, and a broker minimum lot that would risk more than the budget). Splitting it is a separate change to the reason vocabulary.


## 1.4.1

Tests and findings only. The shipped product is unchanged: no file under `src/` has a behaviour edit in this release, and the version moves only so these findings have a place to be recorded.

- Every refusal reason `RiskManager` can name now has a test that asserts the NAMED reason. `allowed is False` cannot tell you a control has stopped testing anything. The roster is `tests/test_refusal_reasons.py`, and it measures its own denominator against `risk.py`, so a reason added to the module without a case fails the suite instead of quietly lowering the count.
- The count was 20 reasons, not 15. Issue #11 reported 15 and 5 of them named; the measured figures are 20 reasons, 5 asserted by name, 11 refusal returns never executed (6 of those inside `evaluate`). After this change 19 of 20 are asserted by name and 1 refusal return is still unexecuted, for the reason below.
- Each guard was mutated so its refusal could not fire, and each test was watched going red before being trusted. 19 of 20 went red. The 20th did not, which is the first finding.

### Three pieces of `risk.py` cannot execute. Reported, not fixed.

None of these is a behaviour defect today and none is changed here. Each is pinned by a test that FAILS if it ever becomes reachable, so no test in this repo is left passing against a line that cannot run.

- **`size_exceeds_risk` cannot fire.** The last-line size guard recomputes exactly what `lots_for_risk` already checked, from the same entry, stop and spec, but with a looser tolerance (`1e-6` against the inner `1e-9`). Anything that would trip it has already been turned into 0 lots by the tighter inner check and is reported as `size_zero`. Deleting the guard outright leaves the whole suite green, which is how this was confirmed rather than argued. The reason string is still live in the product, but only from `engine.py` on the `/replace` path, which keeps the volume the broker already accepted and never calls `lots_for_risk`. That is the refusal an operator can actually receive, and it now has a test naming the site.
- **The `halted` fallback cannot fire.** `circuit_reason` reads `self._halt_reason or "halted"`. Every site that raises the halt flag sets a reason in the same block, so no public call can leave the flag up with an empty reason.
- **The zero-equity branch of the margin gate cannot be taken.** `if account.equity > 0:` guards a division, so the interesting case is a wiped account, and no wiped account reaches that line: the daily-loss gate fires first for every one of them, because a fresh day sets `day_start_equity` to the account equity and `0 >= 0` is true. The gate fails CLOSED on a wiped account, which is correct; the branch under it is simply unreachable.

- `circuit_reason`, the gate deciding whether the model may stage at all, had no test on four of its branches (`halted`, `trade_not_allowed`, `live_not_accepted`, `max_drawdown`). All four are now named. `circuit_reason` is also asserted NOT to latch a market verdict, which `circuit` does and it must not.
- `no_signal` and `already_in_symbol` have no production caller that can reach them: the auto leg returns before both (`engine.py`), and the desk only ever builds BUY or SELL. `already_in_symbol` is reachable from the desk and is tested there; `no_signal` is a defensive guard on a public method and is tested at that method.
- `outside_session` is reachable on the auto leg only. The desk passes `manual=True`, which bypasses the session window by design, so no operator command can produce it. Tested on the auto leg, off the bar clock.
- Where a reason is reachable through the desk or the auto leg, the assertion is the structured `reject` record from 1.2.0 rather than the return value, because the record is what an operator and an auditor read after the fact.


## 1.4.0


- Tests and gates only. No runtime behaviour changes, so this consumes no release number.
- MT4 golden wire transcripts. `tests/test_mt4_wire.py` drives the adapter through `FileBridge`, a real mailbox on disk, and a stand-in Expert that answers with the byte-exact text `mt4/Experts/Mt4RiskBot.mq4` emits. Every op has a transcript. The existing suite drove the adapter through a stub that returned native Python dicts, so the pipe-separated decoding never ran through the broker at all.
- The transcripts distinguish a MEASURED value from a DEFAULTED one. `Transcript.keys_sent()` answers whether the Expert put a field on the wire, and `value_sent()` gives the raw string it sent. A field the Expert never emits is absent, so the value reported for it is the adapter's own default.
- Pinned, not changed: the Expert sends 11 of the 15 keys the symbol reader consumes. `trade_mode`, `currency_base`, `currency_profit` and `currency_margin` are never on the wire, so a symbol spec always reports trade mode 4 (full). Nothing in `src/` reads `SymbolSpec.trade_mode` yet, so the consequence is latent.
- Pinned, not changed: a `tick_value` measured as zero is replaced by 1.0 and becomes indistinguishable from a genuine 1.0. The Expert emits four decimals, so any real value below 0.00005 arrives as zero.
- Field order is now checked. The Expert's pipe-join order for positions, orders and bars is asserted against the adapter's field tuples. A dict-returning stub is indifferent to order, so reordering one field used to leave the suite green.
- The reply-id match is now covered. A reply carrying a foreign id is not consumed, and the request is left in the mailbox after a timeout.
- Per-file coverage floors, declared in `[tool.straightedge.coverage_floors]` in `pyproject.toml`. `broker/mt4_live.py` has a floor of 97%. A package-wide `--cov-fail-under` cannot go red for one file.
- `broker/mt4_live.py` coverage: 86.94% to 97.59%. Tests touching it: 18 to 114. Suite: 305 to 415.
Smaller items batch (#15), in priority order. No version bump encoded here
(several PRs open today already claim conflicting numbers); assigned at
merge.

- **Redaction gap, and it was an exfiltration path, not just a logging
  gap.** `journal.redact_text` covered the BotFather token pattern only.
  Advice turns persist to `journal.advice.json` and are replayed verbatim
  into `Advisor._memory` on every subsequent provider call, so a
  `sk-ant-...` or `xai-...` key pasted once into a chat question (or
  echoed back in a model reply) was written to disk unredacted and
  resent to the third-party provider on every following turn. Verified
  the replay claim directly: the raw key showed up in the SECOND
  outbound HTTP payload in a test before this fix. Widened the pattern
  list; `_remember` (write) and `load` (read, so an already-persisted
  legacy turn is cleaned on the next process start too) both already
  routed through the one function, so one fix closes both the disk and
  the replay side.
- **`desk.py` vs `risk.py` mode-gate mismatch.** `Desk._live_needs_flag`
  gated the "arm live first" warning on `mode != "mt5"`; `risk.py`'s real
  send gate uses `mode in {"mt5", "mt4"}`. The desk side was wrong: on an
  MT4 real account, `/approve always` armed with no warning at all. The
  send was still refused downstream (`live_not_accepted`, risk.py's gate
  is correct), so this was never a path to an unwarned send, but the desk
  lied about the precondition until that refusal. Fixed to match risk.py's
  set form.
- **`/resume` message, and a sharper finding underneath it.** The issue
  named `daily_loss`: `clear_operator_halt` trusted a single `_halt_reason`
  slot that `circuit()` overwrites to `"halt_file"` on every call while the
  operator HALT file exists, so a poll tick between `/halt` and `/resume`
  made `/resume` claim "trading may resume" during a live `daily_loss`
  halt. For `daily_loss` / `max_drawdown` this really was message-only:
  every gate recomputes them fresh from the snapshot, so the next gate
  call re-halts. It is NOT message-only for `state_unreadable` /
  `state_unwritable`: neither is recomputed the same way (the state file
  is read once, at start), so the same clobbering let `clear_operator_halt`
  silently drop a COULD NOT MEASURE halt with nothing to re-derive it
  from, and let `_persist_state`'s evidence-preservation guard reopen and
  overwrite the corrupt file it exists to protect. Fixed with a dedicated,
  never-clobbered slot for the state-integrity reason, and daily_loss /
  max_drawdown re-derived from the snapshot instead of trusted from the
  stale slot.
- **Model pin.** `claude-sonnet-4-5` -> `claude-sonnet-5` in all 3 places
  it appears (`config.py` dataclass default, `config.py` loader default,
  `config.example.toml`). `grok_model` / `computer_model` checked and are
  current; not touched. Nothing asserted on the old string.
- **`ruff` and `mypy` in CI**, as a new `lint` job feeding the `ci`
  aggregator's `needs:`. `ruff` selects `E4,E7,E9,F` deliberately (real
  bugs: unused imports, undefined names, syntax-adjacent issues), not the
  rest of `E`/`W`: this codebase's own idiom runs long, dense lines, and a
  line-length gate would be a rewrite, not "a cheap win". `mypy` runs
  against `src/straightedge` with two narrow, documented per-module
  overrides (`desk.py`'s deliberately `object`-typed `engine`; the MT5
  binding's `Any | None` optional-import pattern in `mt5_live.py`), plus
  four small `dict[str, object]` annotations and two `PaperBroker`
  narrowing asserts in `__main__.py` that were genuine (if minor) type
  gaps, not gate suppressions. Both tools verified clean against `src/` at
  this commit before landing.

Docs corrected to match current code (#14). No behaviour change.

- `SECURITY.md` said real money is refused unless the bot started with
  `--i-accept-risk`. That was never the only path: `/live on I-ACCEPT-RISK`
  in the locked Telegram chat arms it too, and always has. The restart
  half of the original defect (a journal-restored `/live on` re-arming a
  fresh process) was already fixed by #17; this corrects the doc to name
  both arming paths and their per-process, not-restart-restored behaviour,
  instead of naming only one.
- `README.md` named `the gateway` (Cloudflare AI Gateway) alongside `the
  bot` / `the desk` / `the agent` with no scope. Only `AI_PROVIDER=computer`
  (the agent) routes through it; the default `grok` and `claude` are BYOK
  straight to `api.x.ai` / `api.anthropic.com`, with none of the gateway's
  billing, caching, rate limiting, or observability. Both the intro and
  the Names table now say so.
An MT4 market order can no longer be left open with no stop while the desk is told the send failed (issue #23).

- An MT4 market send is two calls: `OrderSend` with no stop, then `OrderModify`. If the second call failed, the Expert attempted one unchecked `OrderClose` and reported a plain failure either way. If that close also failed, or if the Expert could not even SELECT the ticket, the position stayed open with no stop and the desk was told the send failed. Nothing on either side reconciled that state.
- The rollback is now verified against the book rather than against a return value. The Expert re-reads the ticket and only reports a clean failure when `OrderCloseTime()` confirms it is gone. A failed `OrderSelect` now rolls back instead of abandoning the ticket.
- When the rollback cannot be verified, the reply carries `survivor_ticket=<ticket>` and `error=sl_modify_failed_position_live`. `Fail()` had no ticket field at all, so the wire previously could not express this state even in principle.
- `OrderResult.survivor_ticket`: `0` nothing survived, a positive ticket is live exposure the desk was told did not exist, `None` is COULD NOT MEASURE. Venues that attach the stop with the entry, including MT5, have no such window and report `0`.
- An Expert older than 1.2.0 does not send the field. That reads as `None`, never as `0`. An unanswered question is not an all clear.
- Two journal events: `unmanaged_position` and `survivor_unknown`. Both are also sent to Telegram in words, because a live unstopped position the desk does not know about is not a journal-only condition.
- Working orders get the same treatment, with `OrderDelete` and `sl_modify_failed_order_live`.
- `OnInit` scans the book at startup and prints every position that is open with no stop. New `ReconcileMagic` input filters the scan; `0` reports all. The Expert reports these and does not adopt them.
- `docs/MT4.md` documented the two-step entry as an ECN feature and stated that the Expert closes the ticket on a modify failure. It did not close it reliably. The section now states the three outcomes and which one leaves money at risk.

Ships the handover config gate (#25). `/approve always` and `/auto on` are the
two paths to a real-money send with no human keystroke: `/approve always`
sends inside the same Telegram `handle()` call as the advice turn, and
`/auto on` trades from the EMA signal with no confirm step at all.

- `telegram.allow_approve_always` and `telegram.allow_auto` in `config.toml`
  (env: `TELEGRAM_ALLOW_APPROVE_ALWAYS`, `TELEGRAM_ALLOW_AUTO`). Both default
  true, so an existing deployment that never sets these keys is unaffected.
- Set either false and the matching command is refused with a named reason
  (`approve_always_disabled`, `auto_disabled`), journaled as `reject`
  (`source=telegram`, `stage=approve`/`auto`), and never answered in chat.
  With no journal reachable the refusal still prints to stderr.
  `/approve off` and `/auto off` are never refused.
- A value that is present but not a clean boolean parses to false, never to
  the default: a config typo or a string-valued env var (`bool("false")` is
  `True` in Python) can only ever remove the capability, never grant it.
- `config.handover.toml`, a new file, ships with both set false. Copy it to
  `config.toml` for a handed-over desk.
- The default stays true on purpose (flipping it would silently change every
  existing deployment); the resulting gap is closed by observability, not a
  stricter default. `doctor` and `run` print the posture
  (`approve always: allowed|disabled`, `auto: allowed|disabled`), and every
  `start` journal record carries `approve_always_allowed` / `auto_allowed`,
  so a session's posture is readable both live and after the fact from
  `journal.jsonl`.

Version chosen, not assumed: `main` is 1.1.5, and PR #40 (1.1.6) and PR #49
(1.2.0) are both open. This adds new config surface rather than fixing a
defect in existing behaviour, so MINOR under the project rule; 1.3.0 avoids
colliding with either open lane's claimed number. Whoever merges last still
needs to renumber deliberately; a clean merge is not evidence the version is
right (see #38's "version trap").


## 1.3.2

The MT4 symbol reader no longer fabricates values it never measured (issue #30, superseding #12).

- All 15 reads in `Mt4Broker.symbol` used `d.get(key, DEFAULT) or DEFAULT`. `or` fires on a legitimate ZERO as well as on absence, so a broker-reported zero became a EURUSD-shaped default that nothing downstream could tell from a measurement. Two real producers of zero: `MarketInfo` answers 0 for a symbol that is not in Market Watch, and the Expert truncated `tick_value` to four decimals so any real value below 0.00005 arrived as zero.
- `SymbolSpec.unmeasured` records the fields that were not measured. An unmeasured field is left at a value that cannot be mistaken for usable, never at a plausible default.
- The risk gate refuses with `spec_not_measured:<fields>` BEFORE any gate reads the spec. Previously an unusable `volume_step` produced `size_zero`, which says the budget was too small: a different fact. The last-line guard could not catch the tick-value case at all, because `risk.py` recomputes `money_per_lot_at_stop` from the same corrupt spec.
- `1.0` was not a conservative default. Below 1.0 it undersizes, which is safe; above 1.0 it oversizes by exactly the ratio, so a 2.5 tick value spends 2.5x the intended budget. MQL4 has ONE tick-value identifier, `MODE_TICKVALUE`, with no loss-leg variant, so the MT5 remedy does not transfer and the honest fix is to refuse.
- The 4 fields the Expert cannot send (`trade_mode`, `currency_base`, `currency_profit`, `currency_margin`) are marked unmeasured instead of invented. `trade_mode` no longer defaults to 4 (full trading), which had meant a close-only symbol presented as fully tradable. That was LATENT rather than live: nothing in `src/` reads `SymbolSpec.trade_mode`, only `Account.trade_mode` is consumed. Closed so it cannot become live later.
- Zero remains a real reading where zero is real: `digits` on an instrument quoted in whole points, and `stops_level`, `freeze_level` and `spread`. Only fields where zero is impossible are treated as failed measurements.
- The Expert now serialises `volume_min`, `volume_max`, `volume_step` and `tick_value` with 8 decimals instead of 2 and 4. A 0.001 lot step used to arrive as `0.00` and refuse every order with no explanation. This removes TRUNCATION as a producer of zero; it does not remove zero itself, and that one is still a refusal.
- `docs/MT4.md` now states field by field what MT4 can and cannot supply, and says outright that MT4 has one tick value by design, so the loss-leg field is not re-proposed.

Currency-exposure limit: confirm the pair, or say the limit does not apply (issue #10).

- The pair is the first six alphabetic characters after non-alphabetic characters are dropped, and BOTH halves must be recognised currency codes (ISO 4217 plus the metal codes ISO assigns, so `XAUUSD` and `XAGUSD` parse). The table CONFIRMS a pair; it never refuses a trade.
- Pairs whose base or quote contains an M now parse. `USDMXN`, `MXNJPY`, `EURMXN`, `GBPMXN` and `CADMXN` previously did not, so those positions were absent from the per-currency limit.
- Every vendor suffix convention resolves (`EURUSDm`, `EURUSD.a`, `EURUSD_i`, `EURUSDmicro`, `EURUSD-5`), and so does a separator inside the pair (`EUR.USD`, `EUR/USD`).
- One rule, two outcomes. If either half is not a recognised code, or six alphabetic characters do not exist, the limit DOES NOT APPLY: the trade is ALLOWED and the exclusion is recorded as `currency_limit_not_applicable` with the excluded symbols. That covers an instrument that cannot be a pair (`US30`, `USOIL`, `GER40.cash`), decoration that hides the pair (`FXEURUSD`, `mEURUSD`), and a code missing from the table.
- There is no third state. Cannot-tell and is-not-FX get the same treatment, because the honest answer to both is the same: do not pretend to measure currency exposure, do not block the trade, make the exclusion visible.
- Not applicable is never silent. Silence was the original defect.
- A code missing from the table degrades to allowed-and-recorded, never to refused, so completeness is desirable rather than a safety property. Crypto codes are not in the table, so `BTCUSD` is recorded as excluded.
- `currency_exposure` raises for a symbol that is not an FX pair, so the silent skip cannot be reintroduced by a future caller. `evaluate` classifies first, so the `exposure_unmeasured` refusal is a tripwire against caller/classifier divergence and cannot fire from a broker symbol. A non-FX position contributes nothing to currency exposure, which is correct rather than an underestimate.

## 1.3.0

Sender-level authorization for Telegram commands (GHSA-9fg6-2x5f-3jvp).

- `TELEGRAM_ALLOW_SENDERS`, or `telegram.allow_senders` in `config.toml`, lists the Telegram sender ids that may command the desk. Every inbound command is checked against it, read-only commands included. A comma separates ids in the environment variable.
- An update whose sender cannot be read is refused. An identity that was not measured is not an authorized one.
- A group, supergroup, or channel chat id is negative. On a negative chat id with an empty allow-list, `run` and `doctor` exit non-zero instead of starting.
- An empty allow-list on a private chat id is unchanged behaviour. An existing single-operator deployment needs no config edit.
- A refused command is journaled as `command_rejected` with the sender id, the chat id, and the command name. It is never answered in chat.


## 1.1.5

- Safety fix. A pre-trade check that never ran is no longer treated as a check that passed. MQL5 `order_check` reports a PASSED check as retcode `0`, and the MT5 adapter used to synthesize retcode `0` when the terminal call returned nothing, so the engine guard let the failure through and sent the order. A call that returns nothing now yields `RETCODE_UNKNOWN` (`-1`) and `OrderResult.measured` is false. The engine aborts before sending.
- `order_check_fail` now carries `reason`: `broker_refused` (the venue rejected the check) or `not_measured` (the venue returned nothing, so the check never ran). An operator can tell "the broker said no" from "we never asked".
- MT4: a mailbox reply carrying no `ok` and no `retcode` was reported as `REJECT`, which said the broker refused when the Expert had in fact answered nothing. It is now `not_measured`. Both abort, so this changes the reason, not the outcome.
- A genuine `order_check` retcode `0` still passes, and a genuine venue rejection still reports `broker_refused`.


## 1.1.4

- `flatten` can fail loudly. It counts positions requested, positions confirmed closed, positions closed elsewhere, and survivors, and returns a `FlattenReport`. Every count goes to `journal.jsonl` as `flatten`.
- A sweep that leaves risk open also writes `flatten_incomplete` and alerts `FLATTEN INCOMPLETE: n still open` with the tickets. `notify_events` cannot silence that alert (`telegram.ALWAYS_NOTIFY_EVENTS`).
- Survivors are no longer folded into `_seen_pos`. A position that outlived a flatten alerts again instead of being marked already-seen.
- A close is confirmed only when the filled volume covers the whole position. `RETCODE_OK` includes `DONE_PARTIAL`, so `ok=True` with residual volume now counts as a survivor, not a close. `RETCODE_OK` itself is unchanged.
- An unreadable book on a flatten is COULD NOT MEASURE, reported as an incomplete sweep with the survivor count as an upper bound. Never a clean one.
- `flatten` no longer raises. A broker call that fails mid sweep is journaled (`close_failed`, `cancel_failed`, `close_partial`), the sweep finishes, and `halted` is still set. Before this, an exception on one close skipped the halt entirely.
- Cancelling working orders checks its results too. A refused cancel is a survivor.
- `/halt` reports what happened, with counts, instead of the fixed string `flattened and halted.`


## 1.1.3

Restart no longer restores the permissive state and discards the protective one (issue #7).

- Risk state persists to `journal.equity.json` next to the journal: `day_key`, `day_start_equity`, and `peak_equity`. `start` reads it back. A restart inside the same UTC day does not hand out a new loss budget, and the drawdown gate does not read a zeroed peak. A genuine new UTC day still resets the daily budget; the peak is not daily.
- The snapshot is written whenever one of those three fields moves, not only at a halt. A file written only at the halt has already lost the peak.
- The write is atomic (temp file, fsync, rename). A kill mid-write cannot leave a truncated file.
- What is persisted is the INPUT the gates recompute from, never a stored verdict. No halt is made sticky by this file.
- A corrupt, truncated, mistyped, non-finite, or newer-version snapshot halts with reason `state_unreadable` and the file is left alone for inspection. A snapshot that cannot be written halts with reason `state_unwritable`. Both are COULD NOT MEASURE and both fail closed; neither is treated as a clean start.
- To reset the peak, stop the bot and delete `journal.equity.json`. Point the bot at a different account and delete it too.
- Two new journal events: `live_not_restored` and `risk_state_error`. `/risk` also prints the state error, so COULD NOT MEASURE is visible at start and in chat, not only at the first refusal.
- `live_accepted` is now PER PROCESS. `start` never arms real money from a `live_on` journal record; it writes `live_not_restored` and `/live` says arming was not restored. Re-arm with `/live on I-ACCEPT-RISK`. A crash loop can no longer keep real money armed from a `/live on` typed weeks earlier.


## 1.1.2

- Tests compare paths with `pathlib.Path`, not slash strings. Windows `\tmp\...` vs `/tmp/...` is not a failure.


## 1.1.1

- Windows can run the bot next to MT4. `journal.lock` uses `msvcrt.locking` on Windows and `flock` on Unix. `import fcntl` no longer happens at module load.
- MT4 mailbox retries `unlink` / `replace` on `PermissionError` (NTFS sharing). Writes LF even on Windows. Reads FILE_ANSI via `mbcs`.
- Empty `mt4.files_dir` on Windows defaults to `%APPDATA%\\MetaQuotes\\Terminal\\Common\\Files`. `%APPDATA%` in the path expands.
- Expert opens the mailbox with `FILE_SHARE_READ|FILE_SHARE_WRITE` and writes `.res` via `.res.tmp` + `FileMove`.


## 1.1.0

- MetaTrader 4 is a third venue. `account.mode = "mt4"` selects `Mt4Broker`.
- MT4 has no official Python package. The owned ICD is a Common Files mailbox (`mt4_risk_bot.req` / `.res`) spoken by `mt4/Experts/Mt4RiskBot.mq4`. See `docs/MT4.md`.
- `run --mode mt4`. `doctor --connect` pings that mailbox when mode is `mt4`.
- Real-money MT4 (`trade_mode=2`) uses the same fuse as MT5: `--i-accept-risk` or `/live on I-ACCEPT-RISK`.
- `MT4_FILES_DIR` / `mt4.files_dir` is the Common Files path. Not a secret.


## 1.0.0

- Development Status Production/Stable. Production bar holds: exclusive `journal.lock` (second `run --loop` exits 2), `journal.heartbeat` each successful `step_all`, journal rotate to `journal.jsonl.1` at 10 MiB, CI pytest on Python 3.12 and 3.13 plus `doctor`, launchd `KeepAlive` / `Umask` 63 / heartbeat path, pytest and PR CI coverage >= 80%.
- Paper is still the default. No profit guarantee.
- `run` takes an exclusive flock on `journal.lock` next to the journal. A second `run --loop` on the same journal exits 2 with stderr `already running`.
- launchd example: `KeepAlive`, `Umask` 63 (077), `journal.heartbeat` path comment. Secrets stay `REPLACE_ME`.
- Advice conversation persists in `journal.advice.json` (last 40 turns, chmod 0600) and restores on restart. This is the desk context, not an in-memory buffer.
- `AI_PROVIDER=computer` sends `/ask` to a Cloudflare Computer Durable Object. Working memory is the workspace filesystem (`notes.md`, `log.md`, `snapshot.md`, `history.json` from `journal.tail`). Inference is AI Gateway Unified Billing (`CF_AIG_TOKEN`), not provider BYOK.
- `broker_for(cfg)` selects PaperBroker or Mt5Broker from `account.mode`. `PendingOrder.kind` is `limit` or `stop`; engine lists and replaces from that string, not MT5 type ints.
- `PendingOrder.kind` is `limit` or `stop`. Engine and desk never read MT5 `type_code`.
- `Side` has no MT5 order type integers. Adapters map buy/sell for `order_send`.
- Engine uses `OrderResult.unchanged` and `OrderResult.invalid_stops`. It does not import MT5 retcode integers.
- Advice send: default is `/confirm`. `/approve always` sends after risk preview. README and advice context match CONTRACT.
- Runtime journal siblings (`journal.advice.json`, `journal.heartbeat`, `journal.tg_offset`, `journal.jsonl.1`) are gitignored.


## 0.3.0

- Development Status Beta. Production bar holds: Telegram 429/5xx retry and persisted `getUpdates` offset, MT5 reconnect, journaled confirm restore, secret redaction and chat_id lock, doctor paper plus `--connect` fail-closed, launchd, HALT, `--i-accept-risk`, `run --loop` survives a bad `step_all`, config validation on start, pytest and CI coverage >= 80%, journal and offset chmod 0600, close-by hedge-only with a netting fake.
- Paper is still the default. No profit guarantee.


## 0.2.0

- Telegram is the desk: /buy /sell /close /sl /tp /be /trail /history /risk /confirm.
- `/confirm` reprices market orders, re-runs risk, honors halt, reports retcode.
- Partial close `/close TICKET VOL`. Staged confirm is not overwritten.
- Limit/stop working orders (`limit=` / `stop=`), `/orders`, `/cancel TICKET`.
- Tick fill alerts and SL/TP checks run even when `/auto` is off.
- `/quote` with no symbol lists the configured book. `/trail` never loosens.
- `/trail on|off` manages existing positions every tick without EMA entries. Default off.
- `/sl` `/tp` TICKET modify a working order (`TRADE_ACTION_MODIFY`) as well as a position.
- `/symbols list|add|remove` edits the configured book at runtime.
- `/tp TICKET PRICE VOL` scales out VOL at PRICE; circuit still refuses.
- UTC day roll sends a recap notify (equity vs day_start, journal tail). `/recap` dumps it. Not a trade.
- `doctor` pings Telegram (skip if unset) and paper `/buy` `/confirm` `/close` with no live terminal.
- `doctor --connect` is non-zero if the MT5 binding is missing or login fails.
- Live `Mt5Broker.orders` maps `orders_get` onto `PendingOrder` (covered without a terminal).
- If the circuit would halt, advice is hold/close only; buy/sell is not staged.
- `/replace TICKET PRICE` moves a working order; circuit and risk_pct still refuse.
- `/reverse TICKET` stages close plus opposite market. `/confirm` is two market sends. Circuit and risk_pct still refuse.
- `/closeby TICKET OTHER` offsets opposite positions (`TRADE_ACTION_CLOSE_BY`). Hedge-only on live; paper always hedges. Paper P/L is not live.
- `run --loop` retries Telegram 429/5xx with backoff, resumes `getUpdates` at the same offset, and re-`initialize`s a dropped MT5 IPC. One bad tick is journaled (`reconnect` / `loop_error`).
- `step_all` calls `ensure_connected` before `account`.
- `getUpdates` offset is persisted as `journal.tg_offset` after each handled or skipped update. Restart does not replay or drop commands.
- `journal.jsonl` and `journal.tg_offset` are chmod 0600.
- HALT file is chmod 0600. umask 077 at process start.
- Journal, `loop_error` stderr, and Telegram chat echoes redact BotFather tokens (`[REDACTED]`).
- Staged `/confirm` is journaled (`confirm_stage`) and restored on `start` if the TTL has not expired.
- Grok (xAI) and Claude (Anthropic) via env keys. Last 6 turns kept. Advice never auto-sends.
- Advice JSON may stage `limit=` / `stop=` or close TICKET. `/ask` context includes `/risk`, orders, positions, quotes.
- Auto EMA regime is off until `/auto on`.


## 0.1.0

- Risk-first engine: 0.5% per trade, daily-loss circuit, drawdown circuit, HALT file.
- Paper broker and synthetic/CSV backtest. Live adapter for MetaTrader5 / mt5-mac.
- Telegram alerts and /status /positions /halt /resume.
- CI jobs `ci` and `coverage` (80% fail-under) for the org `main` gate.
