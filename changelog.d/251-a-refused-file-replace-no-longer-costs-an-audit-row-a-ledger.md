### A refused file replace no longer costs an audit row, a ledger entry or an offset (issue #251)

On Windows `MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)` fails with
`ERROR_ACCESS_DENIED` while the destination is open in a process that did not
ask for `FILE_SHARE_DELETE`, and CPython's `open()` does not ask. So any
concurrent reader can refuse a `write tmp, replace` publish. #242 guarded the
heartbeat and #257 guarded the risk snapshot; the four remaining sites are now
guarded too, and the decision is PER SITE because they do not share a
consequence.

- **`journal.py` rotation: OPERATOR-VISIBLE ROW CHANGE.** Rotation moves the
  live audit log aside, and a refusal used to raise out of `write`, so a
  housekeeping failure destroyed an audit record while the run loop caught the
  error and kept going. Rotation now retries, and a refusal that outlasts the
  retry window **defers the rotation and still writes the row**, which then
  carries `rotate_deferred: 1`. If you parse journal rows, that key is new. The
  file may briefly exceed its 10 MiB bound; the next write rotates it. A
  caller's own `rotate_deferred` field is never overwritten.
- **NEW HEARTBEAT FIELD: `rotate_deferrals=`.** A count of rotations deferred
  because a reader held the file past the retry window, published on every
  heartbeat like `breach_rows_lost=` and zero on a healthy desk. If you parse
  `journal.heartbeat`, that key is new. The row field `rotate_deferred` is the
  RECORD of which rows were affected; this count is the SURFACE that leaves the
  process, because the log is the worst channel for reporting that the log
  cannot rotate. It is PUBLISHED, not ALERTED: it is not a watchdog reason, so
  a holder that defers rotation forever is visible to a reader and pages
  nobody. Choosing a threshold is straightedge#288.
- **`inflight.py`** retries, and still FAILS CLOSED after the window: the
  ledger entry is written before the send, so a raise means the order never
  leaves. That is deliberate and tested.
- **`llm.py`** retries. A refused replace there discarded an advice reply the
  operator had already been billed for.
- **`telegram.py`** retries. Its only caller already swallowed the error, so a
  lost poll offset was silent; the test asserts the file's contents rather than
  the absence of an exception.
- **`broker/mt4_live.py` keeps its own retry loop on purpose.** The mailbox is
  the interface a customer installs against. A test now pins its spin figure to
  the shared helper's so the two copies cannot drift.
- **A census test reads the package as an AST** and fails on any new bare
  filesystem replace, on anything it cannot classify, and on a guarded site
  that silently loses its guard. A regex for `os\.replace` was blind to
  `tmp.replace(dest)`, which is the form most of these sites use, and that
  blindness is why the population was undercounted twice.
