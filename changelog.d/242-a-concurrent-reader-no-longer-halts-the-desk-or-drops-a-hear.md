### A concurrent reader no longer halts the desk, or drops a heartbeat tick (issues #242, #251)

On Windows `MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)` fails with
`ERROR_ACCESS_DENIED` while the destination is open in a process that did not
ask for `FILE_SHARE_DELETE`, and CPython's `open()` does not ask. So any
concurrent READER of a file this desk publishes made the publishing `replace`
fail. Observed once on the deployed desk, on the heartbeat, in two weeks of
`loop_error` records; the mechanism is certain and **which reader held the
handle is not measured and is not claimed.**

- **The heartbeat (#242) aborted the tick.** The exception escaped
  `_write_heartbeat`, so the rest of that management cycle did not run, and
  the stamp did not advance, which makes a watchdog read in that window
  report a STALE caused by its own read.
- **The risk snapshot (#251) HALTED the desk.** `save_snapshot` raising
  `StateUnwritable` makes `_persist_state` set `_halt_reason =
  "state_unwritable"`, and a halted desk returns from `step_all` before
  `_resolve_pending`, `_check_stops` and `_manage_open`. It does not flatten,
  unlike the gates that normally halt. **Bounded to an availability defect by
  one fact: an opening order carries `sl` and `tp` to the VENUE, so protective
  stops survive a halted desk.** Trailing and scale-outs do not.
- **Both retry, narrowly.** `straightedge.atomic`
  `replace_retrying_on_share_conflict` spins at 20ms inside a 0.5s window on
  `PermissionError` ONLY, never a bare `OSError`, because a retry absorbs a
  RACE and never a CONDITION: a full disk, a vanished directory and a revoked
  ACL are states waiting cannot fix. **A hold that outlasts the window still
  raises, and for `state.py` still halts**, which is the module's contract that
  a write it cannot complete is COULD NOT MEASURE.
- **The gate runs on `windows-latest`, because no POSIX run can reach it**: a
  rename over an open file succeeds there. Each case holds a real handle,
  releases it on a signal only after a positive control has observed a genuine
  refusal, and the POSIX leg asserts the conflict does not exist rather than
  pretending to cover it.
- **The idiom is now named once.** Seven sites in this package publish
  write-tmp-then-replace and two retried; the five non-mailbox sites that
  should share one helper are being converted per site, since adopting it
  where no guard exists is a behaviour change rather than a rename.
  `broker/mt4_live.py` keeps its own loop: the mailbox is the interface a
  customer installs against.
