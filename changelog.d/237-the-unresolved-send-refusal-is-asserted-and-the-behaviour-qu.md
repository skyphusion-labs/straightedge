### The unresolved-send refusal is asserted, and the behaviour question it was filed with was a false premise (issue #237)

```
$ grep -rn "send_refused_unresolved" tests/
(nothing)
```

A journalled refusal event with zero assertions anywhere, and it is the
structured record of the one refusal class that exists to stop a DUPLICATE
ORDER. It carries no named `reason`, so a scan built from `_reject(` and
`journal.write("reject", ...)` sites could not see it: outside that population
by construction rather than missed by the instrument.

**The issue said this refusal is broadcast to the chat, unlike every `reject`
site, and offered three options depending on whether that is intended. It is
not broadcast, so none of the three applies.** `_emit` notifies only when
`_format_event` returns a non-empty string; the formatter has no arm for this
event and returns `""`; and the event is in neither `ALWAYS_NOTIFY_EVENTS` nor
the default `notify_events` allowlist. Measured through a real unresolved send
across a restart: **zero `sendMessage` calls, including with the event
explicitly allowlisted**, because the formatter is the binding guard. So the
journal-only invariant holds with no exception, nothing needed a carve-out, and
no behaviour changed here.

**Two independent reasons keep it quiet, which is worth pinning rather than
celebrating.** Either alone is sufficient, so an arm added to `_format_event`
later would start broadcasting a refusal silently with only the allowlist left
in the way. The test allowlists the event ON PURPOSE and requires the chat to
stay empty, which pins the formatter rather than the allowlist; adding a
formatter arm reds it.

**And the operator is not left uninformed, which is the thing the issue was
right to worry about.** On the DESK path the reply is `refused: unresolved send
<client_id>`, prose on purpose because it names one in-flight send (#220 pins
it as prose). This event is the AUTO and restart path, where nobody is waiting
on a reply and the journal is the record, exactly like every other auto
refusal.

**Driven through a real unresolved send across a RESTART, never by calling
`not_sent()`.** The ledger is durable beside the journal, so the second engine
is a restart rather than a second object: the desk's in-memory
`_already_attempted` guard is gone and the engine's control is the only thing
between the operator and a duplicate order. The only un-stubbed way to open a
ledger entry is a venue that takes the order and never answers, since a
rejection and a fill both CLOSE the entry, so the fake sits at the broker
boundary and nothing inside the desk is stubbed.

Seven mutations, each red where it should be: the record removed reds 3, the
duplicate guard removed reds 3, `attempts` dropped reds 1, `first_at` dropped
reds 1, the row no longer naming the send reds 2, the key comparison dropped
(so every send is refused) reds 4 including the control, and a formatter arm
that broadcasts reds 1. Restored, 5 passed.
