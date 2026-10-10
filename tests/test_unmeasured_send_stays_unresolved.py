"""An UNMEASURED send is not a rejected send, so its ledger entry stays open.

Found while fixing the MT5 double-send (item 1 of the fdaf92c walkthrough), in
the very mechanism built to stop it. `Engine._open` and `Engine._place_pending`
both close the in-flight entry like this:

    # Every path from here has a VERDICT from the venue, including the
    # rejections, so the ambiguity is gone and the entry is closed.
    self.inflight.resolve(key, "ok" if result.ok else "rejected")

The comment is wrong, and only for one input. `OrderResult.unknown` is RETURNED
from an adapter, not raised: `_result(None, request)` hands it back whenever the
venue produced no reply at all. Its `retcode` is `RETCODE_UNKNOWN`, which is not
in `RETCODE_OK`, so `result.ok` is False and the branch above files it as
`"rejected"` and DELETES the entry.

That is the exact substitution `inflight.py` exists to forbid -- "cleared only by
a VERDICT (success or a venue rejection)" -- and an unknown is neither. The cost
is the whole guard: with the entry gone, the next `/confirm` of the same staged
order passes `_unresolved` and transmits a second time, which is what the ledger
was written to prevent. Before item 1 this was mostly masked, because the MT5
adapter RAISED after its second send and the exception path keeps the entry
open. Fixing the adapter to return an unknown instead of sending twice turns
this from latent into live, which is why the two changes belong together.

`OrderResult.measured` is the discriminator and it already exists;
`Engine._pretrade_ok` has consulted it since issue #8. Using it here makes the
three outcomes the same three everywhere in the engine: PASSED, REFUSED, and
COULD NOT MEASURE.
"""

from __future__ import annotations

from datetime import datetime, timezone

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.inflight import inflight_path_for
from straightedge.models import MarketOrder, OrderResult, WorkingOrder
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand


class NoReplyBroker(PaperBroker):
    """A venue that ANSWERS NOTHING, and says so instead of raising.

    This is the returned-unknown shape, which is what an adapter produces when
    the request went out and no reply came back. `InFlightBroker` in
    `tests/test_send_idempotency.py` covers the RAISED shape; the engine handles
    that one correctly already, and the difference between the two files is
    exactly the difference between `raise` and `return`.

    Nothing is filled, because a send whose reply was lost may or may not have
    filled and the desk cannot see which.
    """

    def __init__(self, balance: float) -> None:
        super().__init__(balance=balance)
        self.sends: list[MarketOrder | WorkingOrder] = []

    def market(self, order: MarketOrder):  # type: ignore[override]
        self.sends.append(order)
        return OrderResult.unknown("no result: IPC send failed")

    def working(self, order: WorkingOrder):  # type: ignore[override]
        self.sends.append(order)
        return OrderResult.unknown("no result: IPC send failed")


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


def _open_keys(engine: Engine) -> dict:
    return engine.inflight.open_entries()


def test_an_unmeasured_market_send_leaves_its_entry_open(tmp_path) -> None:
    """The ledger must still hold the attempt after a reply that never came."""
    broker = NoReplyBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()

    assert len(broker.sends) == 1, broker.sends
    assert _open_keys(engine), (
        "the in-flight entry was cleared by an UNMEASURED result; nothing now "
        "records that this order may be on the book"
    )


def test_a_second_confirm_after_an_unmeasured_send_is_refused(tmp_path) -> None:
    """The money consequence, stated as money: one staged order, one send."""
    broker = NoReplyBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    first = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    second = engine.handle_command(TgCommand("1", 1, "/confirm", 3))
    engine.stop()

    assert len(broker.sends) == 1, (
        f"the same staged order reached the venue {len(broker.sends)} times "
        f"after an unmeasured first reply; first {first!r}, second {second!r}"
    )
    assert "unresolved" in second, second


def test_an_unmeasured_pending_send_leaves_its_entry_open(tmp_path) -> None:
    """`_place_pending` carries the identical branch, so it gets the same test."""
    broker = NoReplyBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    price = broker.tick("EURUSD").ask - 0.0050
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD limit={price:.5f}", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()

    assert len(broker.sends) == 1, broker.sends
    assert _open_keys(engine), "the pending path cleared an unmeasured attempt"


def test_a_rejected_send_still_closes_its_entry(tmp_path) -> None:
    """The positive control: a real REJECTION must still resolve.

    Without this, keeping every entry open would pass the tests above while
    turning one bad order into a desk that refuses forever. A venue that
    answered "no" IS a verdict, the ambiguity is genuinely gone, and the entry
    must go with it.
    """
    broker = NoReplyBroker(balance=10_000)
    engine = _engine(tmp_path, broker)

    def rejected(order):
        broker.sends.append(order)
        return OrderResult.invalid_stops("sl_required")

    broker.market = rejected  # type: ignore[assignment]
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()

    assert len(broker.sends) == 1, broker.sends
    assert not _open_keys(engine), (
        "a venue REJECTION left the entry open; that refuses every later "
        "re-stage of an order the venue definitively declined"
    )


def test_the_open_entry_survives_into_a_new_process(tmp_path) -> None:
    """A restart must still see the question. The file is the record, not RAM."""
    broker = NoReplyBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()

    path = inflight_path_for(str(tmp_path / "j.jsonl"))
    assert path.exists(), path
    fresh = _engine(tmp_path, NoReplyBroker(balance=10_000))
    assert fresh.report_unresolved_sends() == 1
def test_an_unmeasured_send_is_not_reported_as_a_venue_rejection(tmp_path) -> None:
    """The operator's sentence is part of the safety, not decoration.

    "send failed retcode=-1" is a claim that the VENUE declined the order. An
    unmeasured send carries no retcode to make that claim with, and the order
    may be filling while the operator reads it. `Desk._confirm` already carries
    this exact reasoning in a comment, for the sibling branch; the unmeasured
    result fell past it into the wrong sentence.
    """
    broker = NoReplyBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    reply = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.stop()

    assert "send failed" not in reply, reply
    assert "unresolved" in reply, reply
    assert "will NOT be sent again" in reply, reply
def test_not_sent_and_unknown_are_both_unmeasured_but_not_the_same_fact() -> None:
    """The distinction is structural, so it is asserted on the model itself.

    Both are unmeasured, which is what makes every `measured` gate treat them
    alike and correctly. Only one of them can have moved money.
    """
    lost = OrderResult.unknown("no result: IPC send failed")
    declined = OrderResult.not_sent("unresolved send abc: nothing was transmitted")

    assert not lost.measured and not declined.measured
    assert not lost.ok and not declined.ok
    assert lost.transmitted is True, "a lost reply DID go out; that is the point"
    assert declined.transmitted is False
    assert OrderResult(retcode=10009).transmitted is True


def test_a_refused_second_confirm_is_worded_as_a_refusal(tmp_path) -> None:
    """The engine's own duplicate control must not borrow a lost reply's words.

    `Desk._already_attempted` is dropped here so the reply comes from the engine
    guard, which is the only control the auto leg and any later caller pass
    through. The operator has to be told nothing was transmitted THIS time.
    """
    import straightedge.desk as desk_mod

    broker = NoReplyBroker(balance=10_000)
    engine = _engine(tmp_path, broker)
    original = desk_mod.Desk._already_attempted
    desk_mod.Desk._already_attempted = lambda self, key: False  # type: ignore[assignment]
    try:
        engine.start()
        engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
        engine.handle_command(TgCommand("1", 1, "/confirm", 2))
        second = engine.handle_command(TgCommand("1", 1, "/confirm", 3))
        engine.stop()
    finally:
        desk_mod.Desk._already_attempted = original  # type: ignore[assignment]

    assert len(broker.sends) == 1, broker.sends
    assert second.startswith("refused:"), second
    assert "nothing was transmitted this time" in second.lower(), second
    assert "no verdict came back" not in second, second
