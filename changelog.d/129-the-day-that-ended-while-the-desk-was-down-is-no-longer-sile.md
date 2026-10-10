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
