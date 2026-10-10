### A damaged box figure says so instead of claiming a breach nobody observed (issue #328)

A `journal.heartbeat` carrying `tick_gap_ever_s=inf` published a box-breach
claim no process ever made. `inf`, `Infinity` and `1e400` all become `inf`
through `float`, so the value parsed, survived `max`, was published as a
number, set `over_budget_ever=1`, and made `watchdog.decide` tell the operator:

```
NOTE: THIS PROCESS is inside its budget, but this BOX has breached it before
(tick_gap_ever_s=inf against tick_budget_s=214)
```

It also STUCK, because every later write folds the figure with `max`, and the
only way to clear it was deleting `journal.heartbeat`, which **also discards
the box history straightedge#190 exists to preserve**. So the only remedy
destroyed the thing being remedied. Every other damaged value already behaved
correctly: `garbage`, `1.2.3`, `None`, `nan`, `-5` and empty are discarded and
the chain restarts from the live process's own observation.

**Two shapes, because they close different directions.** A non-finite figure is
refused at the WRITE, so nothing non-finite can be published whatever its
source rather than only when it came from a file, and both box fields publish
`damaged`:

```
tick_gap_ever_s=damaged
over_budget_ever=damaged
```

`over_budget_ever` is `damaged` as well, and that is the point rather than
tidiness: a figure that is not a measurement cannot answer whether the box
breached, and publishing `1` or `0` from it would invent the answer in one
direction or the other. `watchdog.decide` now says the history is UNREADABLE
and says neither of the two things it could otherwise say.

**The word is `damaged` and not the `unmeasured` this file already uses for
`deployed`.** "No history yet" and "history destroyed" lead an operator to
different actions: the first says start the chain here, the second says the
figure in this file cannot be trusted and clearing it costs you the box
history. Collapsing them to reuse an existing constant would hide the thing the
field exists to convey.

**The shape that was refused, recorded so it is not re-proposed.** Refusing the
non-finite at the RESTORE instead is a smaller change and it makes
`_restore_gap_ever`'s original docstring claim true, but it converts a damaged
file into a CLEAN box history, and reading absence as clean is the one thing
straightedge#190 refuses everywhere else.

**A write-side refusal alone gets it wrong, and the repin caught that.**
`damaged` will not parse as a float, so without a read-side branch it fell into
the existing `except ValueError`, the chain restarted from the live process's
observation, and ONE RESTART laundered a damaged box history into a clean one,
which is the refused shape arrived at by accident. `_restore_gap_ever` now
recognises `damaged` and carries it as a non-finite figure, so the existing
`max` monotonicity keeps it sticky with no new state. Deleting
`journal.heartbeat` is still the one way out, because a process with no prior
starts the chain at its own observation.

The PER-PROCESS pair is unaffected in every case, which was already true and is
the property that would make a regression here worse than the defect.
`tick_gap_max_s` and `over_budget` come from this process's own monotonic
readings, which have no path from a file.

`tests/test_the_damaged_box_figure_and_the_both_over_note.py` pinned the old
behaviour on purpose, recorded rather than endorsed, so this change could not
be silent: it reds that test. The repin asserts five properties including the
stick, the deletion way out, and that neither the breach note nor the
upgrade-gap note is emitted, the latter being the clean-history reading.
`docs/CONTRACT.md` and `docs/RUNBOOK.md` carry the third value and the
distinction between `damaged` and a MISSING field.
