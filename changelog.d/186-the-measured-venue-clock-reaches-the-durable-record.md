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
