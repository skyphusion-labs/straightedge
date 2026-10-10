### A reply the operator paid for survives a failed memory write

- **`Advisor.ask` discarded the reply when `save()` raised.** `ask` calls the
  provider, parses the answer, and then records the turn through `_remember`,
  which ends in `self.save()`. No call site wrapped either one, so a persistence
  failure took `ask` with it and the reply was thrown away on the way out,
  **after the provider call had been made and billed and after `Desk._ask` had
  already spent a slot of `advice.max_turns_per_day`.** A failure of the
  convenience destroyed the product: the operator paid for an answer and
  received an exception, and the daily advice budget was one lower.
- **Measured, the operator did not even get an error sentence.** `Desk._ask`
  catches `(ValueError, RuntimeError)`, and an `OSError` out of `save()` is
  neither, so it escaped the desk's handler entirely. #251's retry absorbs a
  concurrent reader refusing the replace; it does nothing for a full disk, a
  revoked ACL, a vanished directory, or the `tmp.write_text` that precedes the
  replace.
- **Persistence is now best effort at exactly one call site.** `_remember`
  appends to the in-process memory first and then attempts the write inside a
  `try`, so a failed disk write never costs the running desk the turn as well;
  `save()` itself still raises for any direct caller. The catch is there rather
  than around the two `_remember` calls in `ask`, because wrapping both together
  would skip the second APPEND when the first SAVE failed. `TypeError` is
  deliberately not caught: the payload is `str` by construction, so one would be
  a defect in that file rather than a disk problem.
- **Best effort does not mean silent.** The failure travels out on
  `Advice.memory_error`, which carries the exception CLASS and never its message
  (the rule `docs/CONTRACT.md` already states for `advice_error`, and the channel
  #216 and #226 closed), is cleared before every provider call so it cannot
  outlive its turn, and reaches three surfaces: a new `advice_memory_unsaved`
  journal row with `provider`, `session`, `stage`, `error_type` and `turn_spent`;
  one extra line in the chat naming the exception class; and the full message,
  path and all, on stderr where nothing external reads it. The chat line names
  the DISK and the next restart rather than claiming the turn is gone, because
  the in-process memory still has it.
- **Its own event rather than a field on `advice_turn`**, because that row's key
  set is declared in `journal.ADVICE_TURN_ROW_FIELDS`, asserted in both
  directions and governed by `RECORD_ROW_BOUND`, so widening it for a diagnostic
  would be a contract change and not a fix.
