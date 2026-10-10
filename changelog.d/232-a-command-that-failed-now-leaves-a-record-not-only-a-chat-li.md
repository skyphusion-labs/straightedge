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
