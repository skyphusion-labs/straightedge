### The two paths #190 left unpinned are pinned, and one claim in the code was false

- **The `except ValueError` branch in `Engine._restore_gap_ever` had no test**,
  and a reviewer mutation confirmed it: hardcoding the branch left the suite
  green. It now has one, driven from a real engine over a damaged heartbeat.
  Four separate mechanisms, one published outcome: `garbage`, `1.2.3` and `None`
  take the `except ValueError` branch, `nan` and `-5` parse and are discarded by
  `max`, and an empty value never reaches `float`, and in every case the desk
  discards the damaged figure and restarts the chain from **its own
  observation**. Removing the branch so the parse escapes
  `_write_heartbeat` now reds, which matters because a desk that stops
  publishing a heartbeat is read by `straightedge-watch` as a dead desk and
  restarted in a loop, over one unreadable line in a diagnostic field.
- **`#218`'s own figure for those cases was wrong and is corrected here.** It
  records that they publish `0.0`; measured on a real engine they publish the
  live process's own observation (`2.0` for a process that saw a 2.0s gap).
  `0.0` is only what a process that observed nothing would publish. The
  behaviour CLASS in the issue is right; the number was an artefact of its
  fixture, and pinning the class with the wrong number would have been a test
  passing for a reason that is not the reason.
- **A claim in `_restore_gap_ever`'s own docstring was false, and is corrected.**
  It said the figure "is always a true lower bound on the box's history and
  never an invented one". For a DAMAGED file it can be invented: `inf` parses,
  survives `max`, publishes as `inf`, sets `over_budget_ever=1`, makes
  `watchdog.decide` tell the operator **this BOX has breached it before**, and
  **sticks**, because every later write folds it with `max`, so only deleting
  `journal.heartbeat` clears it. All four properties are now pinned, including
  that the deletion clears it, so the next change to any of them has to be
  deliberate. **No behaviour change**: whether the restore should refuse a
  non-finite value is a change on a live operator surface, and is `#328`.
- **The watcher's box-breach note is pinned in the BOTH-over case.** The note
  opens "THIS PROCESS is inside its budget, but this BOX has breached it
  before", and when both figures are over budget that first clause is FALSE. The
  `over_budget != "1"` clause is the only thing suppressing it, and a reviewer
  mutation removing that clause left the suite green. Both directions are now
  asserted from a real engine: suppressed when this process has breached too,
  and required once the process is clean again.
- **A `CHANGELOG` sentence cited a suite count no run reproduces.** `1394
  passed` went stale when other PRs merged, for the second time in that same
  entry. It now states that the mutated and unmutated runs AGREE and carries no
  absolute number, because the agreement is the actual finding and the count was
  the only part of it that could rot.
