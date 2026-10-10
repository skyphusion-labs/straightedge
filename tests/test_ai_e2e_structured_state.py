"""Issue #35: the AI interface proven end to end, on structured state only.

The house rule in this repo is that a test asserts on a machine-readable
channel and never on English prose, because prose is the one part of the
system nobody promised to keep stable. Every assertion below reads a
`journal.jsonl` record. Not one of them matches the chat reply, and that is
the point of the file rather than a style preference: the replies here are
reworded freely, so a suite that reads them measures the copywriting.

WHAT WAS ALREADY TRUE WHEN THIS WAS WRITTEN, measured rather than assumed.
#11, #29 and #61 did most of the naming work, so this file deliberately does
NOT re-assert what they cover. The refusal reasons reachable as a `reject`
record number 12, of which 11 were already asserted against the structured
record (`tests/test_refusal_journal.py`, `test_advice_whitelist_and_cap.py`,
`test_venue_clock.py`), plus 6 sites that forward `decision.reason` and carry
the separately AST-gated `risk.py` roster through. Re-stating those would add
a passing test and zero discrimination.

Exactly ONE reason in that population was observable only through the chat
text, and the conversion is in this file:

- `orders_unmeasured` was asserted as `"orders_unmeasured" in reply`
  (test_working_orders_count_as_exposure.py). The structured record was always
  there, by way of the `RiskDecision` at engine.py:1383 forwarded through the
  desk's `_reject`; the test simply never read it. So the denominator this
  issue asks for is 1 going to 0, not the 15 the issue's description estimated
  before that work landed. No src change was needed for it.

THE THREE THINGS THAT WERE GENUINELY UNPROVEN, and are proven here:

(a) an advice turn ends in a STAGED order, and staging is not sending. The
    existing advice tests cover the refusal legs; none of them followed a
    successful turn through to the fill and showed the two states apart.
(b) the order that OPENED is the order that was STAGED, correlated by
    `client_id`. Without this, (a) and (c) both pass while the desk stages one
    order and sends a different one.
(c) the human approval step is real, asserted as an ORDER and not as a set of
    events that happen to be present. A refusal that fires after the send is a
    different system from one that fires before it, and a membership check
    cannot tell those apart.

ONE MEASURED TRAP, recorded because it would have produced a confident green.
Under `/approve always` the desk STILL writes `confirm_stage`; the bypass is
visible as an `approve_always` record standing BEFORE it, never as a missing
stage. A test written to the obvious guess (approve_always means no
confirm_stage) fails against the real desk, and a test written to assert the
bypass by absence would have passed while measuring nothing. The sequence was
observed first and the assertion written to what the desk actually does.

WHAT THE LIVE RUN FOUND, which is the argument for having done one. The
end-to-end run this issue asks for (a real paper desk and a real Advisor over
real HTTP against the Worker under `wrangler dev --local`) surfaced a hole no
test in this file can reach: `Desk.handle()` catches `ValueError` and
`RuntimeError`, returns the message to the chat, and journals NOTHING. On the
advice path the turn has already been counted against the daily cap by then,
so a failed turn spends the operator's budget and leaves no record that it was
ever attempted. Filed as #232 and deliberately NOT fixed here, because that is
a behaviour change on the money path and this is a test-only diff.

A fake transport returns a payload, so it can never raise an HTTP 502. That is
the whole reason the live run is part of done rather than a formality.
"""

from test_refusal_journal import FakeLlm, _advice_payload, _engine

from straightedge.broker.paper import PaperBroker
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand

ASK = "/ask what should I do"


def _events(engine) -> list[str]:
    """The event names, in the order the journal recorded them."""
    return [r.get("event") for r in engine.journal.tail(200)]


def _subsequence(names: list[str], wanted: tuple[str, ...]) -> bool:
    """Do `wanted` appear in `names` in that relative order?

    A strict subsequence, not a set: the whole point of (c) is that `open`
    standing BEFORE `confirm_stage` is a different and much worse system than
    `open` standing after it, and `set.issubset` reports those as identical.
    """
    it = iter(names)
    return all(any(n == w for n in it) for w in wanted)


def _one(engine, event: str) -> dict:
    rec = engine.journal.last_event(event)
    assert rec is not None, f"no structured {event!r} record was written"
    return rec


# -- (a) an advice turn stages an order, and staging is not sending ---------


def test_an_advice_turn_stages_an_order_and_names_advice_as_the_source(tmp_path) -> None:
    """The model's answer reaches the desk as a staged order, attributed.

    `source` is the load-bearing field: a staged order that cannot say it came
    from the model is indistinguishable from one the operator typed, and the
    advice whitelist and the advice cap both key off that distinction.
    """
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload(symbol="EURUSD")))
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))

    rec = _one(engine, "confirm_stage")
    assert rec["source"] == "advice"
    assert rec["signal"]["symbol"] == "EURUSD"
    assert rec["signal"]["kind"] == "buy"
    assert rec["volume"] > 0
    engine.stop()


def test_an_advice_turn_stages_and_sends_nothing(tmp_path) -> None:
    """Staging is not sending, asserted on the record and on the venue.

    Both halves are needed. The journal half would still pass if `open` were
    renamed; the position half would still pass if the desk sent an order it
    never journaled. Together they pin the state the approval gate exists to
    create: committed to nothing, recoverable from the record.
    """
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload(symbol="EURUSD")))
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))

    assert engine.journal.last_event("confirm_stage") is not None
    assert engine.journal.last_event("open") is None, (
        "an advice turn reached the venue with no human approval"
    )
    assert engine.journal.last_event("confirm_sent") is None
    assert engine.broker.positions(magic=engine.cfg.risk.magic) == []
    assert engine.desk.pending is not None, "nothing was left to approve"
    engine.stop()


# -- (b) the order sent is the order staged --------------------------------


def test_the_order_that_opened_is_the_order_that_was_staged(tmp_path) -> None:
    """Correlated by `client_id`, which is what makes this end TO end.

    Every other assertion in this file would hold if the desk staged EURUSD
    and sent something else: each record would be present, correctly shaped
    and in the right order. The identity between the two is the only thing
    that rules it out, and `client_id` is the field the desk already carries
    for its own idempotency guard.
    """
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload(symbol="EURUSD")))
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))
    staged = _one(engine, "confirm_stage")
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))

    opened = _one(engine, "open")
    assert opened["client_id"] == staged["client_id"]
    assert opened["symbol"] == staged["signal"]["symbol"]
    assert opened["volume"] == staged["volume"]
    assert opened["ok"] is True
    engine.stop()


# -- (b, the conversion) a named reason on the record, not in the reply ----


def test_an_unreadable_order_book_names_orders_unmeasured_on_the_record(tmp_path) -> None:
    """The one refusal in this population that was prose-only. #35.

    `test_working_orders_count_as_exposure.py` asserts this refusal as
    `"orders_unmeasured" in reply`. The reason was always on the structured
    record, forwarded from the `RiskDecision` at engine.py:1383, so this is a
    test-side conversion and not a behaviour change.

    COULD NOT MEASURE is not the same verdict as REFUSED, and `stage` is what
    says which leg stopped it, so both ride the assertion.
    """

    class BlindBroker(PaperBroker):
        def orders(self, magic: int | None = None):  # type: ignore[override]
            raise RuntimeError("mt4 bridge timeout")

    broker = BlindBroker(balance=10_000)
    broker.seed_bars(
        "EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3)
    )
    engine = _engine(tmp_path)
    engine.broker = broker
    engine.risk.broker = broker
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))

    rec = _one(engine, "reject")
    assert rec["reason"] == "orders_unmeasured"
    assert rec["source"] == "telegram"
    assert rec["stage"] == "stage"
    assert rec["symbol"] == "EURUSD"
    assert engine.broker.positions(magic=engine.cfg.risk.magic) == [], (
        "an order was sent while the working-order book could not be read"
    )
    engine.stop()


# -- (c) the approval step is real, and it is an ORDER ---------------------


def test_the_approval_sequence_is_stage_then_open_then_sent(tmp_path) -> None:
    """Asserted as a strict subsequence, because a set cannot see ordering.

    `open` before `confirm_stage` would mean the desk sent first and recorded
    the intent afterwards, which is the failure this gate exists to prevent,
    and it is invisible to a membership check over the same three names.
    """
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload(symbol="EURUSD")))
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))

    names = _events(engine)
    assert _subsequence(names, ("confirm_stage", "open", "confirm_sent")), names
    assert names.index("confirm_stage") < names.index("open"), (
        "the order reached the venue before the desk recorded staging it"
    )
    engine.stop()


def test_approve_always_bypasses_the_human_step_and_says_so_on_the_record(tmp_path) -> None:
    """The bypass is a RECORD standing before the stage, not a missing stage.

    Measured, because the obvious guess is wrong: `/approve always` still
    writes `confirm_stage`, so the human step cannot be shown absent by the
    absence of that record. What distinguishes the two modes is that an
    `approve_always` record precedes it and no `/confirm` was ever issued, yet
    the order still opened. A test asserting the bypass by absence passes while
    measuring nothing.
    """
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload(symbol="EURUSD")))
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/approve always", 1))
    engine.handle_command(TgCommand("1", 1, ASK, 2))

    names = _events(engine)
    assert _subsequence(
        names, ("approve_always", "confirm_stage", "open", "confirm_sent")
    ), names
    assert engine.journal.last_event("open") is not None, (
        "approve always did not actually bypass the confirm step"
    )
    assert _one(engine, "confirm_stage")["source"] == "advice"
    engine.stop()


def test_without_approve_always_the_same_turn_opens_nothing(tmp_path) -> None:
    """Negative control for the test above.

    Same advice turn, same fixture, `/approve always` not issued. If this one
    also opened a position, the bypass test would be asserting a sequence the
    desk produces either way, and would prove nothing about `/approve always`.
    """
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload(symbol="EURUSD")))
    engine.start()
    engine.handle_command(TgCommand("1", 1, ASK, 1))

    names = _events(engine)
    assert "confirm_stage" in names
    assert "open" not in names, names
    assert "approve_always" not in names
    engine.stop()


# -- the ordering helper's own discrimination ------------------------------


def test_the_subsequence_helper_rejects_the_wrong_order() -> None:
    """The ORDER half of (c), proven on the helper rather than on the desk.

    `_subsequence` is the only thing standing between "these three events
    happened" and "they happened in this order", so it gets the same treatment
    as any other gate here: a reachable input where it must say no. The desk
    cannot easily be made to send before it stages, so asserting the helper
    directly is the honest way to show the check can fire, instead of trusting
    that it would.
    """
    right = ["start", "confirm_stage", "open", "confirm_sent"]
    assert _subsequence(right, ("confirm_stage", "open", "confirm_sent"))

    # Sent before staged: the same three names, the catastrophic order.
    reversed_pair = ["start", "open", "confirm_stage", "confirm_sent"]
    assert not _subsequence(
        reversed_pair, ("confirm_stage", "open", "confirm_sent")
    ), "the helper accepted a send that preceded its own staging record"

    # A set-based check cannot tell those two apart, which is why this is not one.
    assert set(right) == set(reversed_pair)

    # A missing event is also a no, not a silent pass.
    assert not _subsequence(
        ["confirm_stage", "confirm_sent"], ("confirm_stage", "open", "confirm_sent")
    )
