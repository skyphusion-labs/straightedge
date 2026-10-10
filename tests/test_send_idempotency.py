"""A staged order is transmitted at most once, whatever the first attempt did.

The defect this file exists to close: `BridgeTimeout` subclasses `RuntimeError`,
`Desk.handle` catches `RuntimeError`, and `Desk._confirm` only clears the pending
on a definitive result. So a `/confirm` whose bridge call timed out left the
order staged, and the operator's natural next move -- `/confirm` again --
transmitted it a SECOND time, with the first possibly already filled.

A timeout is not evidence the order did not reach the broker. That is the whole
trap, so nothing in here may treat "no reply" as "nothing happened". The mirror
of it matters just as much and is what made the first draft of this file pass
against the unpatched code: an EMPTY BOOK is not evidence either. The desk times
out at its own budget while the Expert is still inside `SendRetry`, so the
position that the send is about to create is not on the book yet when the desk
looks. A guard that only fires once the fill is visible is a coincidence, not a
control.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from straightedge.broker.mt4_live import BridgeTimeout
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.constants import TRADE_RETCODE_REJECT
from straightedge.models import (
    MarketOrder,
    OrderResult,
    Signal,
    SignalKind,
    WorkingOrder,
)
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand


class InFlightBroker(PaperBroker):
    """A broker that swallows the order and answers nothing.

    `market()` records the order and raises `BridgeTimeout` WITHOUT filling.
    That is the real shape of the case: the request is with the Expert, the
    Expert is still laddering `OrderSend`, the desk's budget expires first, and
    the fill lands after the desk has already written the attempt off. Every
    book read the desk can make at that moment returns nothing.

    `fill_after` lets a test put the fill on the book at a chosen attempt, to
    show that the refusal does not depend on being able to see it.

    `working()` is the SAME shape for a pending order, and it is here because
    this docstring used to claim the raised shape was covered while only
    `market` implemented it. straightedge#264 measured that: `working` was one
    of three seams with no double that could fail, so `_place_pending`'s
    `except BaseException` arm, which is what keeps the ledger entry open, had
    nothing exercising it. Both methods raise the REAL `BridgeTimeout` rather
    than a stand-in `RuntimeError`, because the engine's arms are typed
    (`except (RuntimeError, OSError, ValueError)` in the sweep) and a
    stand-in would prove the handler journals without proving the real
    exception reaches it.
    """

    def __init__(self, balance: float, *, timeouts: int = 1, fill_after: int | None = None) -> None:
        super().__init__(balance=balance)
        self.sends: list[MarketOrder | WorkingOrder] = []
        self.timeouts = timeouts
        self.fill_after = fill_after

    def market(self, order: MarketOrder):  # type: ignore[override]
        self.sends.append(order)
        n = len(self.sends)
        if self.timeouts > 0:
            self.timeouts -= 1
            if self.fill_after is not None and n >= self.fill_after:
                super().market(order)
            raise BridgeTimeout("mt4 bridge timeout")
        return super().market(order)

    def working(self, order: WorkingOrder):  # type: ignore[override]
        self.sends.append(order)
        if self.timeouts > 0:
            self.timeouts -= 1
            raise BridgeTimeout("mt4 bridge timeout")
        return super().working(order)


class CheckTimeoutBroker(PaperBroker):
    """The pre-trade CHECK times out, so nothing is ever sent.

    The other half of the asymmetry `InFlightBroker` covers, and the reason
    both live in this file: `_place_pending` calls `check_working` BEFORE
    `inflight.begin()` and `working` AFTER it. So a raise from the first must
    leave NO ledger entry, and a raise from the second must leave one OPEN.
    Those are opposite correct answers, and straightedge#264 found neither
    pinned because no double for either seam could raise.

    `check_working` is a READ (`sends=False` in the MT4 adapter, and
    `constants.py` says the Expert returns a verdict without touching the
    book), so a timeout here cannot have changed anything. `working` is a
    SEND, so a timeout there is genuinely ambiguous.
    """

    def __init__(self, balance: float) -> None:
        super().__init__(balance=balance)
        self.sends: list[WorkingOrder] = []

    def check_working(self, order: WorkingOrder):  # type: ignore[override]
        raise BridgeTimeout("mt4 bridge timeout")

    def working(self, order: WorkingOrder):  # type: ignore[override]
        # Records so a test can assert NOTHING was sent. A check that timed out
        # must not be followed by a send.
        self.sends.append(order)
        return super().working(order)


def _engine(tmp_path, broker) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    return Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
    )


def _sent(broker) -> list[tuple[str, str, float]]:
    return [(o.symbol, o.side.value, o.volume) for o in broker.sends]


def test_a_confirm_whose_send_timed_out_is_never_transmitted_twice(tmp_path) -> None:
    """THE deliverable. Two `/confirm`s, one order on the wire, empty book.

    The second `/confirm` must not reach the broker at all. Reaching it and
    being rejected there would still be a second order on the wire, and on MT4
    nothing on the far side can tell the two apart.
    """
    broker = InFlightBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    first = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert not broker.positions(), "the fill must NOT be visible, that is the case under test"
    second = engine.handle_command(TgCommand("1", 1, "/confirm", 3))
    engine.stop()
    assert len(broker.sends) == 1, (
        f"the same staged order reached the broker {len(broker.sends)} times; "
        f"first reply {first!r}, second reply {second!r}; orders {_sent(broker)}"
    )


def _buy_limit() -> Signal:
    return Signal(
        kind=SignalKind.BUY,
        symbol="EURUSD",
        entry=1.0900,
        sl=1.0850,
        tp=1.1025,
        atr=0.0030,
        reason="test",
        pending_kind="limit",
    )


def test_a_pending_send_that_timed_out_is_never_transmitted_twice(tmp_path) -> None:
    """The `market` guarantee above, for the WORKING-order path. #264.

    `_place_pending` has its own `inflight.begin()` and its own
    `except BaseException` arm, and until #264 no double for the `working`
    seam could raise, so that arm was never taken. The ledger exists to stop a
    second transmission of one staged order, and a pending order is committed
    exposure the moment it rests on the book (#107), so the pending path needs
    the guarantee as much as the market path.

    The key is passed explicitly because that is what the desk does: it mints
    the key when it STAGES, so the same key survives a `/confirm` whose send
    timed out.
    """
    broker = InFlightBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    key = "staged-pending-1"

    with pytest.raises(BridgeTimeout):
        engine.submit(_buy_limit(), 0.10, key)

    assert key in engine.inflight.open_entries(), (
        "the ledger entry was closed by a send that produced no verdict, so "
        "the next confirm of the same staged order would transmit again"
    )
    assert not broker.orders(magic=engine.cfg.risk.magic), (
        "the pending order must NOT be visible: that is the case under test"
    )

    second = engine.submit(_buy_limit(), 0.10, key)

    assert len(broker.sends) == 1, (
        f"the same staged pending order reached the broker {len(broker.sends)} "
        f"times; second reply {second.comment!r}"
    )
    assert not second.ok
    assert not second.measured, (
        "an unresolved earlier send is COULD NOT MEASURE, not a rejection: "
        "filing it as a verdict is what #232 and the unknown-send work "
        "removed from this path"
    )


def test_a_check_that_timed_out_leaves_no_ledger_entry_and_sends_nothing(
    tmp_path,
) -> None:
    """The opposite correct answer, one call earlier. #264.

    `check_working` runs BEFORE `inflight.begin()`, so a timeout there must
    leave the ledger EMPTY: there is no ambiguity to carry, because a check is
    a read and nothing was transmitted. Leaving an entry open here would
    refuse the operator's next perfectly good attempt for a send that never
    happened, which is the mirror defect of closing one that did.

    This is the assertion that makes the pair meaningful. Either test alone
    reads as "the engine does something sensible with an exception"; together
    they pin that WHERE the exception comes from decides which answer is
    correct.
    """
    broker = CheckTimeoutBroker(balance=10_000)
    engine = _engine(tmp_path, broker)

    with pytest.raises(BridgeTimeout):
        engine.submit(_buy_limit(), 0.10, "staged-pending-2")

    assert engine.inflight.open_entries() == {}, (
        "a check that never sent anything left an in-flight entry behind, so "
        "the next attempt for this order would be refused as unresolved"
    )
    assert broker.sends == [], (
        "the send ran after its own pre-trade check had timed out"
    )
    assert not broker.orders(magic=engine.cfg.risk.magic)


def test_the_refusal_is_not_the_already_in_symbol_rule(tmp_path) -> None:
    """The guard must be the send ledger, not an exposure rule that happens to fire.

    With the fill invisible there is no position, so `already_in_symbol` cannot
    refuse; the reply must name the unresolved send instead. A reply that says
    `already_in_symbol` here means the book was the control, and the book goes
    quiet in exactly the case this is protecting against.
    """
    broker = InFlightBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    second = engine.handle_command(TgCommand("1", 1, "/confirm", 3))
    engine.stop()
    assert "already_in_symbol" not in second
    assert "unresolved" in second, second


def test_the_key_survives_a_process_restart(tmp_path) -> None:
    """The observed failure was a SILENT RESTART, so this is the case that matters.

    The live box restarted with no traceback at 2026-09-26T01:56:49Z and the 5
    minute scheduled task picked it back up. A staged order survives that by
    design (the journal's `confirm_stage` record), so the guard has to survive it
    too: an in-memory set would be empty in exactly the process that most needs
    to know an order may already be on the book.

    Same journal path, same ledger path, a brand new Engine and Desk.
    """
    broker = InFlightBroker(balance=10_000)
    first = _engine(tmp_path, broker)
    first.start()
    first.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    key = first.desk.pending.client_id
    first.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert len(broker.sends) == 1
    first.stop()

    revived = _engine(tmp_path, broker)
    revived.start()
    revived.desk.restore_from_journal(revived.journal)
    assert revived.desk.pending is not None, "the staged order did not survive"
    assert revived.desk.pending.client_id == key, "the key did not survive the restart"
    reply = revived.handle_command(TgCommand("1", 1, "/confirm", 3))
    revived.stop()
    assert len(broker.sends) == 1, (
        f"the restarted desk re-sent the order; reply {reply!r}, "
        f"orders {_sent(broker)}"
    )
    assert "unresolved" in reply


def test_the_ledger_entry_is_on_disk_before_the_send_leaves(tmp_path) -> None:
    """Written before, not after. A crash between the write and the send costs one
    refused re-send; the other order costs the record for an order that DID go."""
    seen: list[list[str]] = []

    class Watching(InFlightBroker):
        def market(self, order):  # type: ignore[override]
            seen.append(sorted(engine.inflight.open_entries()))
            return super().market(order)

    broker = Watching(balance=10_000, timeouts=0)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    key = engine.desk.pending.client_id
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()
    assert seen == [[key]], seen
    # A verdict came back, so the ambiguity is gone and the entry is closed.
    assert engine.inflight.open_entries() == {}


def test_a_rejected_send_closes_the_entry_because_it_is_a_verdict(tmp_path) -> None:
    """A venue REJECTION is not an ambiguity. Only silence is.

    Leaving the entry open on a clean rejection would make the desk refuse a
    perfectly re-sendable order, which is the failure direction that looks safe
    and is really just broken.
    """
    class Rejecting(InFlightBroker):
        def market(self, order):  # type: ignore[override]
            self.sends.append(order)
            return OrderResult(
                retcode=TRADE_RETCODE_REJECT, comment="off quotes", survivor_ticket=0
            )

    broker = Rejecting(balance=10_000, timeouts=0)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    reply = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()
    assert reply.startswith("send failed")
    assert engine.inflight.open_entries() == {}


def test_the_auto_leg_gets_a_fresh_key_for_every_signal(tmp_path) -> None:
    """Each bar is a NEW order, not a retry of an old one, so the guard must not
    refuse the strategy's next entry because an earlier one is unresolved."""
    broker = InFlightBroker(balance=10_000, timeouts=0)
    engine = _engine(tmp_path, broker)
    engine.start()
    sig = engine.market_signal(SignalKind.BUY, "EURUSD")
    engine.submit(sig, 0.01)
    engine.submit(sig, 0.01)
    engine.stop()
    keys = {o.client_id for o in broker.sends}
    assert len(broker.sends) == 2
    assert len(keys) == 2, f"two auto entries shared one key: {keys}"


def test_an_unresolved_send_is_announced_on_every_start(tmp_path) -> None:
    """A standing money question, re-stated every start. A report that stops
    repeating is a report that gets forgotten."""
    broker = InFlightBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()
    again = _engine(tmp_path, broker)
    assert again.report_unresolved_sends() == 1
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "j.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert events.count("send_unresolved") >= 2


def test_an_unreadable_ledger_is_reported_and_not_read_as_empty(tmp_path) -> None:
    """"No open sends" and "I could not read the file" render identically."""
    broker = InFlightBroker(balance=10_000, timeouts=0)
    engine = _engine(tmp_path, broker)
    engine.inflight.path.write_text("{ this is not json", encoding="utf-8")
    assert engine.inflight.readable() is False
    assert engine.report_unresolved_sends() == 0
    events = [
        json.loads(line)["event"]
        for line in (tmp_path / "j.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert "inflight_unreadable" in events


def test_an_interrupt_leaves_the_entry_open_without_probing_the_book(tmp_path) -> None:
    """Ctrl-C during a send is still an unresolved send, and the record proves it.

    The book probe costs a full read budget and is skipped here: the operator is
    stopping the process. What must NOT be skipped is the ledger entry, because a
    process interrupted mid-send has exactly the same money question as one that
    timed out.
    """
    class Interrupting(InFlightBroker):
        #: Set by `market()`, so `positions()` can tell a book read that happens
        #: BEFORE the send (risk preview does two) from one that happens after it.
        sent = False

        def market(self, order):  # type: ignore[override]
            self.sends.append(order)
            self.sent = True
            raise KeyboardInterrupt

        def positions(self, magic=None):  # type: ignore[override]
            assert not self.sent, "the book was probed during a shutdown"
            return super().positions(magic)

    broker = Interrupting(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    key = engine.desk.pending.client_id
    with pytest.raises(KeyboardInterrupt):
        engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert key in engine.inflight.open_entries(), "the interrupt lost the record"
    events = [
        json.loads(line) for line in (tmp_path / "j.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    unresolved = [r for r in events if r["event"] == "send_unresolved"]
    assert unresolved and unresolved[-1]["book_read_failed"].startswith("not attempted")


def test_a_book_read_that_itself_fails_is_reported_and_not_silent(tmp_path) -> None:
    """The wedged mailbox that lost the send is the one most likely to lose this
    read too, so "I could not look" must be a recorded outcome."""

    class Blind(InFlightBroker):
        #: The reads the risk preview makes BEFORE the send have to succeed, or
        #: the order never reaches the broker and this test proves nothing. Only
        #: the probe that follows the failed send is blinded.
        sent = False

        def market(self, order):  # type: ignore[override]
            try:
                return super().market(order)
            finally:
                self.sent = True

        def positions(self, magic=None):  # type: ignore[override]
            if self.sent:
                raise BridgeTimeout("mt4 bridge timeout")
            return super().positions(magic)

    broker = Blind(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()
    events = [
        json.loads(line) for line in (tmp_path / "j.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    unresolved = [r for r in events if r["event"] == "send_unresolved"]
    assert unresolved and "timeout" in unresolved[-1]["book_read_failed"]


def test_a_matched_position_is_reported_as_unmanaged(tmp_path) -> None:
    """A position carrying our key IS this send, and it never had its stop
    confirmed, which is exactly what `unmanaged_position` means."""
    broker = InFlightBroker(balance=10_000, fill_after=1)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    key = engine.desk.pending.client_id
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()
    events = [
        json.loads(line) for line in (tmp_path / "j.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    unresolved = [r for r in events if r["event"] == "send_unresolved"][-1]
    assert unresolved["matched"], f"the fill carrying {key} was not matched"
    assert any(r["event"] == "unmanaged_position" for r in events)
