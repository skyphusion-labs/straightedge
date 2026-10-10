### A breach record is no longer lost when the journal write fails (issue #217)

`_hb_over_warned = True` was set BEFORE the `tick_gap_breach` journal write, so
a write that raised lost the row for the life of the process: `Journal.write`
has no exception handling, the run loop catches `Exception` and keeps going,
and the loop's own `loop_error` write is swallowed. **A flag set before a
durable write loses the record precisely when writing is what failed.**

- **Two surfaces, two flags.** The stderr warning is not durable, so its flag
  is still set before the print. The journal row is, so its flag is set only
  after the write returns. One flag could not govern both, because losing a
  print and losing an append-only audit row are not the same event.
- **The retry is BOUNDED**, at three ticks, roughly 45 seconds at the default
  `poll_seconds`. Moving the flag after the write and stopping there would
  retry on every tick for as long as the journal stayed broken, which is an
  unbounded retry inside the latency path the record exists to explain.
- **The figures are captured at DETECTION**, so a retried write records the
  breach that was detected rather than a larger maximum that accumulated while
  the journal was unavailable.
- **Giving up is recorded.** The heartbeat now carries `breach_rows_lost=`, a
  count of breaches this process detected and could not write. It is the only
  surface that reports a hole in the audit log, because the channel designed to
  carry a breach is the journal and this field exists for when the journal is
  what failed. Zero on a healthy desk and published on every heartbeat.
- **The heartbeat format is now pinned to its documentation.** Nothing
  asserted that `docs/CONTRACT.md`'s field list matched what the renderer
  emits, so the two could drift; a test reads the list out of the paragraph
  that enumerates it and compares both directions.
