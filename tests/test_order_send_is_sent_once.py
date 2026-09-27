"""A non-idempotent order reaches the wire EXACTLY once, whatever the reply did.

The defect this file exists to close. `mt5_live.order_send` went through
`_with_reconnect`, whose rule is "a falsy result means reconnect and call
again". That rule is right for a read and catastrophic for a send, because the
two states it cannot tell apart are:

  the terminal never saw the request        repeating it costs nothing
  the terminal ACCEPTED it and the reply    repeating it opens a second
  died on the way back                      position and doubles the risk

An IPC error says the link is down. It does not say which side of the request
the link went down on, and no amount of looking at `last_error()` can recover
that. So the send is made once and a lost reply is reported as
`OrderResult.unknown` (`measured` False), which is COULD NOT MEASURE and is
NOT a retcode a caller can retry on.

Why the existing suite reported the resend as correct. `FakeMt5._dead()` in
`tests/test_mt5_adapter.py` answers None *before* appending to `sends`, so that
fake can only model the benign half, and `test_order_send_reconnects_on_none`
asserted a resend was fine because in that fake it genuinely was. A fake that
cannot represent the dangerous state reports the reassuring one. The fake below
accepts the order FIRST and loses the reply SECOND, and that ordering is the
entire reason this file is separate from the adapter suite.

The allowlist is fail-closed on purpose. `IDEMPOTENT_TRADE_ACTIONS` names the
actions whose repeat is the SAME request rather than a second one (set a stop,
move an order, remove an order: all of them state a target, so arriving twice
leaves the same book). Everything else, including an action added later by
someone who never read this file, is single-attempt. The last test here is the
one that keeps that property honest.
"""

from __future__ import annotations

from test_mt5_adapter import FakeMt5, _nt

from straightedge.broker.mt5_live import Mt5Broker
from straightedge.constants import (
    IDEMPOTENT_TRADE_ACTIONS,
    ORDER_TYPE_BUY,
    ORDER_TYPE_BUY_LIMIT,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_MODIFY,
    TRADE_ACTION_PENDING,
    TRADE_ACTION_REMOVE,
    TRADE_ACTION_SLTP,
    TRADE_RETCODE_DONE,
)


class LostReplyMt5(FakeMt5):
    """A terminal that ACCEPTS the send and then loses the reply.

    The order is recorded BEFORE the failure is raised, because that is what
    "the broker already has it" means. The link is marked down afterwards so
    `last_error()` reports the IPC family, which is exactly what the adapter saw
    in the field and exactly what used to trigger the second send.
    """

    def __init__(self, *, lose: int = 1, **kw) -> None:
        super().__init__(**kw)
        self.lose = lose

    def order_send(self, request):
        self.sends.append(dict(request))
        if len(self.sends) <= self.lose:
            self.disconnected = True
            return None
        self.disconnected = False
        return _nt(
            retcode=TRADE_RETCODE_DONE,
            comment="Done",
            deal=501,
            order=601,
            volume=float(request.get("volume", 0) or 0),
            price=1.1,
            bid=1.1,
            ask=1.1001,
        )


def _broker(**kw) -> tuple[LostReplyMt5, Mt5Broker]:
    fake = LostReplyMt5(**kw)
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    return fake, broker


def _market_request() -> dict:
    return {
        "action": TRADE_ACTION_DEAL,
        "symbol": "EURUSD",
        "volume": 0.55,
        "type": ORDER_TYPE_BUY,
        "price": 1.1001,
        "sl": 1.0951,
        "tp": 1.1101,
        "type_filling": 0,
    }


def test_a_market_send_whose_reply_is_lost_is_not_repeated() -> None:
    """The headline case: 0.55 lots must not become 1.10."""
    fake, broker = _broker()

    result = broker.order_send(_market_request())

    assert len(fake.sends) == 1, (
        "the order reached the terminal "
        f"{len(fake.sends)} times; every send after the first is a second "
        f"position: {fake.sends}"
    )
    assert not result.measured, (
        "a lost reply was reported as a measured verdict "
        f"(retcode {result.retcode}); a caller may retry on a verdict"
    )
    assert not result.ok


def test_a_pending_send_whose_reply_is_lost_is_not_repeated() -> None:
    """A working order is not idempotent either: two orders rest on the book."""
    fake, broker = _broker()

    result = broker.order_send(
        {
            "action": TRADE_ACTION_PENDING,
            "symbol": "EURUSD",
            "volume": 0.2,
            "type": ORDER_TYPE_BUY_LIMIT,
            "price": 1.08,
            "type_filling": 0,
        }
    )

    assert len(fake.sends) == 1, fake.sends
    assert not result.measured


def test_a_close_whose_reply_is_lost_is_not_repeated() -> None:
    """A repeated PARTIAL close closes volume the operator still wanted open.

    A close is a DEAL, so it lands on the same side of the allowlist as an
    entry. The failure is quieter than a double entry and costs money the same
    way.
    """
    fake, broker = _broker()

    result = broker.order_send(
        {
            "action": TRADE_ACTION_DEAL,
            "symbol": "EURUSD",
            "volume": 0.25,
            "position": 4242,
            "type": ORDER_TYPE_BUY,
            "price": 1.1,
            "type_filling": 0,
        }
    )

    assert len(fake.sends) == 1, fake.sends
    assert not result.measured


def test_an_unmeasured_send_carries_no_ticket() -> None:
    """Nothing downstream may read a ticket out of an unmeasured send."""
    _fake, broker = _broker()

    result = broker.order_send(_market_request())

    assert result.order == 0
    assert result.deal == 0


def test_setting_a_stop_is_still_retried_after_a_reconnect() -> None:
    """The positive control: the idempotent lane KEEPS its reconnect.

    Without this, disabling every retry would pass the tests above while
    breaking the thing `_with_reconnect` was added for. Arriving twice with
    `sl=1.09` leaves the same stop at 1.09, so the repeat is the same request.
    """
    fake, broker = _broker()

    result = broker.order_send(
        {"action": TRADE_ACTION_SLTP, "position": 77, "sl": 1.09, "tp": 1.12}
    )

    assert result.ok, result
    assert len(fake.sends) == 2, (
        "the idempotent action did not retry after the reconnect: " f"{fake.sends}"
    )


def test_an_unknown_action_is_treated_as_non_idempotent() -> None:
    """Fail-closed: an action nobody has classified is single-attempt.

    This is the test that outlives all of us. A new `TRADE_ACTION_*` added by
    someone who never read this file must default to the safe lane, so the
    allowlist has to be an allowlist and never a denylist of known-bad actions.
    """
    fake, broker = _broker()
    unclassified = 4242
    assert unclassified not in IDEMPOTENT_TRADE_ACTIONS

    result = broker.order_send({"action": unclassified, "volume": 0.1})

    assert len(fake.sends) == 1, fake.sends
    assert not result.measured


def test_the_allowlist_holds_only_state_setting_actions() -> None:
    """The membership itself is the claim, so it is asserted, not assumed.

    DEAL and PENDING create or consume volume and must never appear here. The
    three that do each state a TARGET, which is what makes a duplicate arrival
    a no-op rather than a second event.
    """
    assert IDEMPOTENT_TRADE_ACTIONS == frozenset(
        {TRADE_ACTION_SLTP, TRADE_ACTION_MODIFY, TRADE_ACTION_REMOVE}
    )
    assert TRADE_ACTION_DEAL not in IDEMPOTENT_TRADE_ACTIONS
    assert TRADE_ACTION_PENDING not in IDEMPOTENT_TRADE_ACTIONS
