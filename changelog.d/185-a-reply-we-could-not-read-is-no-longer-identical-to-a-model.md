### A reply we could not read is no longer identical to a model that held (issue #185)

#181 gave the claude advice path a schema gate: an off-schema reply has its
action forced to `hold` and the reason written into the reply PROSE. Correct,
tested, and it stops at the chat. `desk.py` closes every advice turn with a
journal row carrying what the model DECIDED and deliberately not what either
side said, so in `journal.jsonl` these two turns were byte-identical:

```
the model held                             action=hold  staged=false
the model said BUY and we could not read   action=hold  staged=false
```

A desk that cannot tell "the model held" from "we could not read the model" is
the defect #181's own docstring names, one surface over. The chat tells whoever
is watching at the time; the journal is what anyone reconstructing a demo week
reads, which is #37 and #38.

`advice_turn` now carries `degraded`: the gate's own violation names, empty when
nothing degraded. **Always present, never omitted**, because a field that
appears only on failure cannot be told from a desk too old to emit it, which is
the partition `survivor_ticket` and `history_error` exist for.

**The reason is a CLASS, not a sentence, and that took a second pass.** The
first version interpolated the reply's own content into the violation text,
which was right for the chat and wrong for the record: a review measured a key
named like a sentence writing that sentence into `journal.jsonl`, and a 6000
character `action` producing a 6053 character reason and a **6293 byte row
against the 512 byte bound this suite already pinned**. So a model could author
unbounded text in our journal through the one field added to make the journal
trustworthy.

`_schema_violations` now returns `(field, class)` pairs from a fixed vocabulary,
with two renderings over one producer. `violation_prose` keeps the operator
sentence #181 added and may echo a value, because the chat already shows the
model's own prose; `violation_classes` renders `field:class` for the journal and
collapses the model's own field NAMES into one counted token, so the longest
possible reason is a function of `ADVICE_PROPERTIES` and not of anything a model
sends. Truncating the interpolated string would also have worked and would have
left a judgement about "small enough" in the code; this leaves none. #181's
twenty cases are green throughout, which is what shows the chat contract did not
move.

**And both tests written to guard that threat could not observe it**, which is
the finding worth more than the fix. `test_the_reason_cannot_be_written_by_the_model`
sent its payload as the VALUE under a key named `degraded`, so the only
violation emitted was about the key and the value could never appear: it passed
by construction. The bounded-row case varied `text` and `summary`, neither of
which is echoed, so it could not see a 6KB row either. Both are rewritten to
drive the channel that leaked, a model-chosen KEY and a model-chosen VALUE, and
both red when the pre-fix rendering is restored. A fourth case asserts the bound
by construction rather than by fixture: forty unknown fields plus every other
violation at once renders under 200 characters.

**The reason travels OUT OF BAND, and both alternatives were defects.** Parsing
it back out of the prose is string-matching our own sentence, and the sentence
is not a contract. Adding a key to the trailing JSON would be a field
`parse_advice` cannot tell WE wrote: the `grok` and `computer` paths have no
schema in front of them, so a model could put that key in its own tail and
author a line in our journal. `Advisor.ask` collects what its own gate measured,
through a list the caller owns, which no model can reach. A test drives a model
reply containing `degraded` and asserts the recorded reason is ours.

**It is cleared before every provider call**, so a reason cannot outlive its own
turn and attach to the next clean reply. That is #119's stale-marker shape, and
a confidently wrong record is worse than a silent one; the mutation that stops
the clearing reds exactly the case written for it.

**The contract's own policy is unchanged and now says so explicitly.** The
question and the reply are still never written to `journal.jsonl`: the reason is
OUR sentence about the reply's SHAPE, not the reply. A test pins that a 1000
character prose reply still leaves the row under 512 bytes with none of that
prose in it.

Four mutations, each anchor-unique with its size delta measured non-zero so no
stale bytecode can match, verdicts from exit status: dropping the field,
attaching nothing, collecting nothing, and leaving the stale marker uncleared.
#181's own suite stays green through all four, which is what shows the chat
surface did not move.
