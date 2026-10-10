### A journal that stopped rotating now ALERTS (issue #288)

**#283 published `rotate_deferrals` on the heartbeat and nothing read it.** A
scanner, a backup agent or an editor holding `journal.jsonl` open refuses every
rotation, the file grows past its 10 MiB bound without limit, every row carries
`rotate_deferred=1`, the count climbs on every heartbeat, and nobody is told.
Arriving-but-unwatched, which is the failure this estate finds every time it
looks.

**THE ALERT IS NOT KEYED ON THE COUNT, and that is the finding inside the
finding.** `rotate_deferrals` is a monotonic per-process count with no clock
and no clearing. A backup pass that held the journal once an hour ago leaves it
at 1 for the life of the process; a holder still attached leaves it at 1 too.
From ONE heartbeat no threshold on it tells those apart, and one heartbeat is
the deployed shape (`watch` with no `--loop`, from a scheduled task), so a delta
between observations is not available either. Any N would fire on the transient
case and never clear, and a muted alarm is worse than none.

**Two published readings instead, and no threshold this watcher invented.** The
heartbeat gains `journal_bytes=` and `rotate_bytes=`, the pair, always both. In
healthy operation the live file NEVER exceeds that bound, because
`Journal._rotate_if_needed` rotates before the write that would cross it. So one
comparison means "a rotation was attempted and did not happen", with no duration
to wait out and no number to defend. The desk publishes its own bound beside the
reading judged against it, exactly as it already publishes `stale_after_s`
beside the timestamp, so the watcher holds no copy of the figure.

**It is also the more general instrument**, which is what #288 asked for. With
`rotate_deferrals=0` and the file over its bound, the rotation is not being
refused by a holder: it is failing, or not being attempted, for something nobody
has thought of. An alert on the cause reads that as healthy. The count is still
READ, and it is what the alert uses to say which of the two it is.

**A fourth state, because the three that existed would each have been a lie.**
`ALIVE DEGRADED`, reason `rotation_stuck`, exit 6. Not `ALIVE ARMED`: `ok` drives
both the chat and the repeat, so a condition that cannot make `ok` false is told
once and never again, which is how this field came to be published and watched
by nobody. Not `ALIVE NOT TRADING`: the desk IS trading, no order is affected,
and that state's remedy is to re-arm, which would be the wrong instruction for a
holder on a file. Exit 6 is additive: a caller testing non-zero already treats it
as bad, and a caller testing `== 4` for `STALE` is unchanged.

**Precedence is asserted, not left to the order of two branches.** A desk that
will not trade is the louder condition and reports first; the rotation reading
rides that report as a note, and a `NOT TRADING` or `STALE` report is already not
`ok`, so it repeats on the desk's own threshold and the note repeats with it. The
only state this can hide behind is one that is already being told.

**The operator action is named in the alert and in the runbook**, because a
`reason` with nothing to do is a notification. Find the holder and stop it:
`handle64.exe journal.jsonl`, or Resource Monitor, CPU, Associated Handles,
searched for the filename. Rotation resumes on the next write after the holder
lets go, with no restart and no re-arm. Restarting the desk does NOT clear it,
since the holder is the other process, and removing the tree does not either: a
process holding a deleted file keeps the inode alive and keeps running. The rows
written during the episode carry `rotate_deferred=1`, so the affected span stays
recoverable from the log.

**A desk too old to publish the pair is UNMEASURED, never healthy**, the same
rule this file already applies to `over_budget_ever` and `run_id`. A
`rotate_bytes` of 0 is read that way too, rather than as a bound of zero that
would make every nonzero size an alert.

**The pin that said this was not an alert is RETIRED**, which is what it asked
for: it required whoever made this a `reason` to retire #288 and the docs with
it rather than leave two statements that disagree.

**Verified by mutation**, not by the suite passing: the alert branch disabled,
the state reported `ARMED`, the comparison weakened, the unmeasured-bound guard
dropped, and the desk publishing a zero size, a zero bound or a zero live file
were each driven and each reds. A transient holder that let go stays `ALIVE
ARMED` with a note, which is the false positive the design is shaped to avoid.
