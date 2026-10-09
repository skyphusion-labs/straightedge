"""A stop on an OPEN position may be tightened freely and never removed.

`Engine._modify` had no checks at all. Any value went straight to
`broker.modify_position`: no cap, no loss-room check, no rule against loosening,
and `0` -- the venue encoding for "no stop" -- passed as an ordinary price. So
`/sl <ticket> 0` silently un-protected a live position, and moving a stop far
enough away put a multiple of the intended risk on the book. The reviewer
measured both on a 0.55 lot position sized at 0.5% risk: the stop removed, and
27.5% of equity at risk after a 500 pip move.

The working-order path already refuses this. `_modify_pending` returns
`sl required` on `sl <= 0`, and `replace_pending` carries a circuit check, a
geometry check and a risk cap. The position path was the inconsistent one, so
this is aligning an asymmetry rather than inventing policy.

## Removal is not an operation this command offers

`/sl` sets a stop to a price, and `0` is not a price. It is refused outright
rather than gated behind a confirmation, and that is a deliberate choice:

* A position with no stop cannot be sized, managed or bounded by this desk, and
  the repo already has a name for one: `unmanaged_position`.
* The MT4 Expert goes to the length of ROLLING BACK a position it opened but
  could not attach a stop to (`RollbackPosition`). A desk that rolls back a
  position it cannot protect, while letting one token delete that same stop
  later, contradicts itself.
* The narrower surface is the defensible one. If deliberate un-protection is
  ever wanted it belongs in its own named command with the exact-phrase gate
  `/live on I-ACCEPT-RISK` already uses, not as a fall-through in a setter.

## The guard is ASYMMETRIC, and that is the whole design

A risk-REDUCING change must always pass, including when the circuit has tripped
and the desk is halted: an operator must never be prevented from tightening a
stop on a live position, and that is exactly the moment they most need to. So
only a change that INCREASES worst-case loss is measured against the cap.

Three consequences that a symmetric guard would get wrong:

* `trail`, `breakeven` and the strategy's `manage()` all tighten, so they pass
  without paying for a broker read. The distance comparison needs no `SymbolSpec`
  because both distances are on the same instrument, which is what keeps this off
  the per-tick hot path.
* A position already riskier than the cap (equity fell after it was opened) can
  still be tightened, even though the result is still above the cap. Refusing
  that would trap the operator in the worse state.
* Adding a stop to a position that has none is a reduction from unbounded to
  bounded, so it is always allowed. A cap must never stand between an operator
  and protecting a live position.

Nothing here is verified against a live MT4 or MT5 terminal.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine, _loss_distance
from straightedge.models import Position, Side
from straightedge.sizing import money_per_lot_at_stop
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand

WED_NOON = datetime(2024, 1, 3, 12, tzinfo=timezone.utc)


def _engine(tmp_path: Path, **kw) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    for key in ("risk_pct", "max_risk_multiple"):
        if key in kw:
            setattr(cfg.risk, key, kw[key])
    broker = PaperBroker(balance=kw.get("balance", 10_000))
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    return Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: WED_NOON)


def _open(engine: Engine):
    """One confirmed market buy, sized by the risk engine.

    Returns a DETACHED COPY. `Position` is a mutable dataclass and
    `PaperBroker.positions()` returns the stored objects themselves, so a
    reference held across a `/sl` tracks the book: `_live(...).sl == pos.sl`
    would compare an object with itself and pass even if the stop HAD been
    removed. That is the whole defect this file is about, so the fixture must not
    reproduce it.
    """
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    reply = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert "sent buy" in reply, reply
    positions = engine.broker.positions(magic=engine.cfg.risk.magic)
    assert len(positions) == 1, positions
    return replace(positions[0])


def _live(engine: Engine, ticket: int):
    for pos in engine.broker.positions(magic=engine.cfg.risk.magic):
        if pos.ticket == ticket:
            return pos
    raise AssertionError(f"#{ticket} is gone from the book")


def _worst(engine: Engine, pos, sl: float) -> float:
    spec = engine.broker.symbol(pos.symbol)
    return money_per_lot_at_stop(pos.price_open, sl, spec) * pos.volume


class TestRemovingAStopIsRefused:
    def test_sl_zero_does_not_remove_the_stop(self, tmp_path: Path) -> None:
        """The reported defect: `/sl <ticket> 0` un-protected a live position."""
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        assert pos.sl > 0, "the position must start protected"

        reply = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} 0", 3))
        engine.stop()

        assert _live(engine, pos.ticket).sl == pos.sl, (
            "the stop was changed by a request to remove it; the position is "
            "now unbounded on the downside"
        )
        assert "sl #" not in reply, reply
        assert "stop_removal_refused" in reply, reply

    def test_a_negative_stop_is_refused_too(self, tmp_path: Path) -> None:
        """`0` is not special. Anything at or below zero is not a price.

        The reason is asserted, not just the outcome: a negative stop may also be
        refused further down by the venue or by price geometry, and this file
        must show THIS guard fired rather than inherit someone else's accident.
        """
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)

        reply = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} -1", 3))
        engine.stop()

        assert "stop_removal_refused" in reply, reply
        assert _live(engine, pos.ticket).sl == pos.sl

    def test_the_refusal_is_journaled(self, tmp_path: Path) -> None:
        """An auditor reads the journal, not the chat."""
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} 0", 3))
        engine.stop()

        rec = engine.journal.last_event("modify_refused")
        assert rec is not None, "a refused stop modification left no record"
        assert rec.get("reason") == "stop_removal_refused", rec
        assert rec.get("ticket") == pos.ticket, rec


class TestANonFinitePriceIsNotAPrice:
    """NaN and infinity are not caught by ANY comparison in the guard.

    Every IEEE-754 comparison against NaN is False, so `sl <= 0`, the breakeven
    arm, the tighten comparison and the cap test all fall through together and
    the guard used to return "". `float("nan")` succeeds, so this is reachable
    from the desk with one typed command.
    """

    def test_nan_does_not_un_protect_a_position(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        assert pos.sl > 0

        reply = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} nan", 3))
        engine.stop()

        live = _live(engine, pos.ticket)
        assert live.sl == pos.sl, (
            f"the stop is now {live.sl}; a NaN stop is no stop, and the position "
            "is unbounded on the downside"
        )
        assert live.sl == live.sl, "the stop on the book is NaN"
        assert "sl #" not in reply, reply
        assert "stop_removal_refused" in reply, reply

    def test_positive_infinity_does_not_un_protect_a_position(
        self, tmp_path: Path
    ) -> None:
        """`+inf` fell through the BREAKEVEN arm, not the cap.

        `price_open - inf` is `-inf`, which is <= 0, so the guard read it as a
        stop at locked-in profit and allowed it.
        """
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)

        reply = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} inf", 3))
        engine.stop()

        assert _live(engine, pos.ticket).sl == pos.sl, "inf reached the book"
        assert "stop_removal_refused" in reply, reply

    def test_a_finite_tighten_still_applies(self, tmp_path: Path) -> None:
        """The allow side. The finiteness arm must not refuse a real price."""
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        tighter = pos.price_open - (pos.price_open - pos.sl) / 2

        reply = engine.handle_command(
            TgCommand("1", 1, f"/sl {pos.ticket} {tighter:.5f}", 3)
        )
        engine.stop()

        assert "sl #" in reply, reply
        assert _live(engine, pos.ticket).sl != pos.sl, "a real tighten was refused"


class TestTheCapFailsClosedOnAnUnmeasuredSpec:
    """A widening may not be waved through because the spec is unmeasured.

    `ticks_between` returns 0.0 when `trade_tick_size or point` is <= 0, so
    `worst` became 0.0 and `0.0 > min(per_trade, loss_room)` is False: every
    widening passed. The arithmetic was copied from `risk.evaluate` without its
    `spec_not_measured` precondition.
    """

    @staticmethod
    def _blind(engine: Engine, symbol: str) -> None:
        """Make the broker answer with a spec whose sizing fields are unmeasured."""
        real = engine.broker.symbol(symbol)
        blind = replace(
            real,
            trade_tick_size=0.0,
            point=0.0,
            unmeasured=frozenset({"point", "tick_size"}),
        )
        engine.broker.symbol = lambda name, _b=blind, _r=real: (  # type: ignore[method-assign]
            _b if name == symbol else _r
        )

    def test_a_widening_refuses_when_the_spec_is_unmeasured(
        self, tmp_path: Path
    ) -> None:
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        wide = pos.price_open - (pos.price_open - pos.sl) * 500
        self._blind(engine, pos.symbol)

        reply = engine.handle_command(
            TgCommand("1", 1, f"/sl {pos.ticket} {wide:.5f}", 3)
        )
        engine.stop()

        assert _live(engine, pos.ticket).sl == pos.sl, (
            "a 500x widening was applied because the spec could not be measured; "
            "the cap failed OPEN"
        )
        assert "spec_not_measured" in reply, reply

    def test_a_tighten_still_applies_when_the_spec_is_unmeasured(
        self, tmp_path: Path
    ) -> None:
        """The asymmetry has to survive the new refusal.

        A tightening returns before the spec read, so an operator must still be
        able to reduce risk on a symbol the broker has not streamed. If this
        refused, the fix would have traded one unbounded-loss path for another.
        """
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        tighter = pos.price_open - (pos.price_open - pos.sl) / 2
        self._blind(engine, pos.symbol)

        reply = engine.handle_command(
            TgCommand("1", 1, f"/sl {pos.ticket} {tighter:.5f}", 3)
        )
        engine.stop()

        assert "sl #" in reply, reply
        assert _live(engine, pos.ticket).sl != pos.sl, (
            "a tighten was refused on an unmeasured spec; the guard is no longer "
            "asymmetric and an operator cannot reduce risk"
        )


class TestTheDirectionIsRightOnAShort:
    """A short's stop sits ABOVE entry, so tighten and widen invert.

    The sign is pinned by `test_loss_distance_goes_negative_past_breakeven`, but
    nothing drove a short through the desk, so a guard that was backwards on
    shorts would have passed this file.
    """

    @staticmethod
    def _short(engine: Engine):
        engine.handle_command(TgCommand("1", 1, "/sell EURUSD", 1))
        reply = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
        assert "sent sell" in reply, reply
        positions = engine.broker.positions(magic=engine.cfg.risk.magic)
        assert len(positions) == 1, positions
        return replace(positions[0])

    def test_lowering_a_shorts_stop_is_a_tighten_and_applies(
        self, tmp_path: Path
    ) -> None:
        engine = _engine(tmp_path)
        engine.start()
        pos = self._short(engine)
        assert pos.sl > pos.price_open, "a short is stopped ABOVE entry"
        tighter = pos.price_open + (pos.sl - pos.price_open) / 2

        reply = engine.handle_command(
            TgCommand("1", 1, f"/sl {pos.ticket} {tighter:.5f}", 3)
        )
        engine.stop()

        assert "sl #" in reply, reply
        assert _live(engine, pos.ticket).sl < pos.sl, "a short tighten was refused"

    def test_raising_a_shorts_stop_past_the_cap_is_refused(
        self, tmp_path: Path
    ) -> None:
        engine = _engine(tmp_path)
        engine.start()
        pos = self._short(engine)
        wide = pos.price_open + (pos.sl - pos.price_open) * 500

        reply = engine.handle_command(
            TgCommand("1", 1, f"/sl {pos.ticket} {wide:.5f}", 3)
        )
        engine.stop()

        assert _live(engine, pos.ticket).sl == pos.sl, (
            "a short stop was WIDENED past the cap; the comparison is backwards "
            "on this side of the book"
        )
        assert "stop_exceeds_risk" in reply, reply


class TestWideningIsCapped:
    def test_a_stop_moved_far_enough_to_blow_the_cap_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The reviewer's second measurement: 27.5% of equity on a 0.5% position."""
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        distance = pos.price_open - pos.sl
        far = pos.price_open - distance * 10

        cap = engine.broker.account().equity * engine.cfg.risk.risk_pct
        assert _worst(engine, pos, far) > cap, "the test did not widen past the cap"

        reply = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {far:.5f}", 3))
        engine.stop()

        assert _live(engine, pos.ticket).sl == pos.sl, "the stop was widened past the cap"
        assert "stop_exceeds_risk" in reply, reply

    def test_a_widening_that_still_fits_the_cap_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """The guard is a CAP, not a ban on ever loosening a stop.

        Tighten to half the distance first, so there is room to widen back into
        without crossing the cap. Without this test, refusing every widening
        would pass the test above.
        """
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        distance = pos.price_open - pos.sl
        tight = pos.price_open - distance * 0.5
        engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {tight:.5f}", 3))
        assert _live(engine, pos.ticket).sl > pos.sl, "the tightening did not apply"

        back = pos.price_open - distance * 0.8
        reply = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {back:.5f}", 4))
        engine.stop()

        assert "sl #" in reply, reply
        assert abs(_live(engine, pos.ticket).sl - back) < 1e-5, reply


class TestReducingRiskAlwaysPasses:
    def test_tightening_is_allowed(self, tmp_path: Path) -> None:
        """Positive control: this passes before the guard exists and must keep doing so."""
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        tighter = pos.price_open - (pos.price_open - pos.sl) * 0.5

        reply = engine.handle_command(
            TgCommand("1", 1, f"/sl {pos.ticket} {tighter:.5f}", 3)
        )
        engine.stop()

        assert "sl #" in reply, reply
        assert abs(_live(engine, pos.ticket).sl - tighter) < 1e-5

    def test_tightening_is_allowed_with_no_loss_room_left(self, tmp_path: Path) -> None:
        """The asymmetry, stated as money.

        With the daily budget spent, `loss_room` is negative and every widening
        is refused by arithmetic alone. Tightening must be unaffected: this is
        precisely the moment an operator reaches for a stop.
        """
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        engine.risk.snapshot.day_start_equity = 1_000_000.0
        assert engine.risk.loss_room(engine.broker.account()) < 0

        tighter = pos.price_open - (pos.price_open - pos.sl) * 0.5
        ok = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {tighter:.5f}", 3))
        wider = pos.price_open - (pos.price_open - pos.sl) * 1.5
        refused = engine.handle_command(
            TgCommand("1", 1, f"/sl {pos.ticket} {wider:.5f}", 4)
        )
        engine.stop()

        assert "sl #" in ok, ok
        assert "stop_exceeds_risk" in refused, refused

    def test_adding_a_stop_to_an_unprotected_position_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """Unbounded to bounded is a reduction, whatever the cap says.

        A cap must never stand between an operator and protecting a live
        position, so this is allowed even though the resulting risk is far above
        `risk_pct`.
        """
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        engine.broker.modify_position(pos.ticket, 0.0, pos.tp, symbol=pos.symbol)
        assert _live(engine, pos.ticket).sl == 0.0, "the fixture did not un-protect it"

        far = pos.price_open - (pos.price_open - pos.sl) * 20
        reply = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {far:.5f}", 3))
        engine.stop()

        assert "sl #" in reply, reply
        assert abs(_live(engine, pos.ticket).sl - far) < 1e-5

    def test_setting_a_tp_on_an_unprotected_position_still_works(
        self, tmp_path: Path
    ) -> None:
        """A regression guard, not a new rule.

        `set_tp` passes `pos.sl` through unchanged. A guard that refused `sl <= 0`
        without asking what was already there would block an operator from
        putting a target on a position that has no stop.
        """
        engine = _engine(tmp_path)
        engine.start()
        pos = _open(engine)
        engine.broker.modify_position(pos.ticket, 0.0, 0.0, symbol=pos.symbol)

        target = pos.price_open + (pos.price_open - pos.sl) * 2
        reply = engine.handle_command(TgCommand("1", 1, f"/tp {pos.ticket} {target:.5f}", 3))
        engine.stop()

        assert "tp #" in reply, reply
        assert abs(_live(engine, pos.ticket).tp - target) < 1e-5
class TestTheMeasureIsSignedNotAbsolute:
    """`abs(entry - sl)` is not monotonic in risk, and the abs form refused a trail.

    These go at the guard directly rather than through the desk, because getting a
    position deep enough into profit for a breakeven-plus trail through `/sl`
    means driving the market too, and the property under test is arithmetic.

    The suite caught this for me: the abs form went red on
    `tests/test_desk.py::test_trail_on_manages_without_auto_entries`, where a
    winning position's trail was refused. These assertions are so that this file
    also covers it, instead of relying on another suite noticing.
    """

    def _pos(self, engine: Engine, side: str, entry: float, sl: float):
        return Position(
            ticket=99,
            symbol="EURUSD",
            side=Side.BUY if side == "buy" else Side.SELL,
            volume=0.10,
            price_open=entry,
            sl=sl,
            tp=0.0,
            price_current=entry,
            profit=0.0,
            magic=engine.cfg.risk.magic,
        )

    def test_loss_distance_goes_negative_past_breakeven(self) -> None:
        buy = Position(
            ticket=1, symbol="EURUSD", side=Side.BUY, volume=0.1,
            price_open=1.1000, sl=1.0950, tp=0.0, price_current=1.1000, profit=0.0,
        )
        assert _loss_distance(buy, 1.0950) > 0
        assert abs(_loss_distance(buy, 1.1000)) < 1e-12
        assert _loss_distance(buy, 1.1050) < 0, "a long stop above entry is profit"

        sell = Position(
            ticket=2, symbol="EURUSD", side=Side.SELL, volume=0.1,
            price_open=1.1000, sl=1.1050, tp=0.0, price_current=1.1000, profit=0.0,
        )
        assert _loss_distance(sell, 1.1050) > 0
        assert _loss_distance(sell, 1.0950) < 0, "a short stop below entry is profit"

    def test_a_stop_trailed_past_breakeven_is_allowed(self, tmp_path: Path) -> None:
        """The case the abs form refused.

        |entry - sl| GROWS here (0.0050 -> 0.0100) while the risk goes from a real
        loss to locked-in profit. Under the abs form this read as a widening and
        `/trail` on a winner stopped working.
        """
        engine = _engine(tmp_path)
        pos = self._pos(engine, "buy", 1.1000, 1.0950)

        assert abs(1.1000 - 1.1100) > abs(1.1000 - 1.0950), "the premise"
        assert engine._stop_guard(pos, 1.1100) == "", (
            "a stop moved 100 points ABOVE a long entry was refused; that is "
            "locked-in profit, not added risk"
        )

    def test_a_short_stop_trailed_past_breakeven_is_allowed(self, tmp_path: Path) -> None:
        engine = _engine(tmp_path)
        pos = self._pos(engine, "sell", 1.1000, 1.1050)

        assert engine._stop_guard(pos, 1.0900) == ""

    def test_giving_back_locked_profit_past_the_cap_is_still_refused(
        self, tmp_path: Path
    ) -> None:
        """Moving from locked profit back into real risk IS an increase.

        Without this, the `proposed <= 0` shortcut could be read as "anything goes
        once a stop has ever been past breakeven".
        """
        engine = _engine(tmp_path)
        # Stop is 100 points ABOVE entry: locked profit, loss distance negative.
        pos = self._pos(engine, "buy", 1.1000, 1.1100)
        far = 1.1000 - 0.0500  # 500 points of real risk on 0.10 lots

        assert engine._stop_guard(pos, far) == "stop_exceeds_risk", (
            "a stop dragged from locked profit back to 500 points of risk was "
            "allowed"
        )


class TestTheHaltRoomHalfOfTheCapIsLoadBearing:
    """`min(per_trade, loss_room)` has TWO bounds and only one had a test (#170).

    Measured by mutation, not by reading: change `engine.py` to
    `if worst > per_trade + 1e-6:`, dropping the halt-room half, and the whole
    suite stayed green at 1187 passed. The identical mutation on the sibling
    guard in `replace_pending` reds `test_fills.py::
    test_replace_pending_refuses_for_lack_of_loss_room` immediately. Same rule,
    three paths, and this was the one carrying it with nothing underneath.

    ## Both halves of why that was hard to see, because the next reader will
    ## run the obvious control and get nothing

    For a FRESHLY SIZED position the mutation genuinely IS equivalent, and the
    obvious control correctly comes back empty. `lots_for_risk` sizes so that
    `worst` at the opening stop is about `per_trade`, so any widening already
    breaks the per-trade bound and the `min()` term never binds. Reaching the
    band needs `loss_room < per_trade` AND `worst <= per_trade` at once, which
    for the default config means equity below 9849 and at or above 9900. Empty.
    **An equivalent mutant is not a missing test**, so stopping there would have
    been sound reasoning on a wrong conclusion.

    What breaks the equivalence is a position whose volume is SMALLER than
    today's cap would size, and that state is ordinary: a partial close, or a
    position carried from a day when equity was lower. This class builds it with
    the desk's own `/close TICKET VOLUME`, so the state is reachable through the
    product rather than by poking the dataclass.

    The scenario, all three gates clear so only this cap can refuse: equity
    9824.50, volume 0.05 after scaling 0.55 down, `per_trade` 49.1225,
    `loss_room` 24.5000. A widening to `worst` 25.00 fits the per-trade cap with
    24 to spare and does NOT fit what the day has left.
    """

    @staticmethod
    def _scaled_out_and_tight_on_room(engine: Engine):
        """0.05 lots left, circuit clear, and `loss_room` below `per_trade`.

        Returns the detached position, `per_trade` and `loss_room`, so every
        assertion below reads the numbers the engine itself computed rather
        than a literal that could drift away from the config.
        """
        pos = _open(engine)
        reply = engine.handle_command(
            TgCommand("1", 1, f"/close {pos.ticket} 0.50", 3)
        )
        assert reply.startswith("closed"), reply
        live = _live(engine, pos.ticket)
        assert live.volume == 0.05, live.volume

        # Equity below day_start by more than the per-trade cap but well inside
        # the 2% daily stop, so `loss_room` is positive and TIGHTER than
        # `per_trade`: the only state in which the second bound can bind.
        engine.broker._balance = 9_825.0
        account = engine.broker.account()
        engine.risk.observe(account, engine.now_fn())
        account = engine.broker.account()

        r = engine.cfg.risk
        per_trade = account.equity * r.risk_pct * r.max_risk_multiple
        room = engine.risk.loss_room(account)
        assert not engine.risk.circuit_reason(account, engine.now_fn()), (
            "the circuit must be CLEAR, or it refuses and this proves nothing"
        )
        assert 0 < room < per_trade, (room, per_trade)
        return replace(live), per_trade, room

    def test_a_widening_inside_the_per_trade_cap_is_refused_for_lack_of_room(
        self, tmp_path: Path
    ) -> None:
        """The per-trade bound ALLOWS this widening. The halt-room bound refuses it.

        That is what makes this the test the mutant cannot survive: assert
        `worst <= per_trade` explicitly, so a guard measuring only the
        per-trade half would have to let it through.
        """
        engine = _engine(tmp_path)
        engine.start()
        pos, per_trade, room = self._scaled_out_and_tight_on_room(engine)

        spec = engine.broker.symbol(pos.symbol)
        far = round(pos.price_open - spec.point * 500, 5)
        worst = _worst(engine, pos, far)

        assert worst > room, "the fixture does not exceed the remaining room"
        assert worst <= per_trade, (
            "the fixture must fit the PER-TRADE cap, or the halt-room half is "
            "not what refuses it and the mutant survives"
        )

        reply = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {far:.5f}", 4))
        engine.stop()

        assert "stop_exceeds_risk" in reply, reply
        assert _live(engine, pos.ticket).sl == pos.sl, (
            "the stop was widened past what the day has left"
        )

    def test_a_widening_that_fits_the_remaining_room_is_allowed(
        self, tmp_path: Path
    ) -> None:
        """Positive control. Without it, refusing EVERY widening on a scaled-out
        position would pass the test above and the suite would be measuring
        nothing but the scale-out."""
        engine = _engine(tmp_path)
        engine.start()
        pos, per_trade, room = self._scaled_out_and_tight_on_room(engine)

        spec = engine.broker.symbol(pos.symbol)
        near = round(pos.price_open - spec.point * 400, 5)
        worst = _worst(engine, pos, near)
        assert worst < room <= per_trade, (worst, room, per_trade)

        reply = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {near:.5f}", 4))
        engine.stop()

        assert "sl #" in reply, reply
        assert abs(_live(engine, pos.ticket).sl - near) < 1e-5, reply

    def test_the_boundary_is_the_room_exactly_consumed(self, tmp_path: Path) -> None:
        """Solve the boundary rather than inherit it from the epsilon.

        `worst` exactly equal to the remaining room is ALLOWED: the comparison
        is `worst > room + 1e-6`, so spending the budget to the cent is not an
        overdraft. Ten points further is refused. Both arms in one test,
        because a boundary asserted on one side only does not pin a boundary.
        """
        engine = _engine(tmp_path)
        engine.start()
        pos, _per_trade, room = self._scaled_out_and_tight_on_room(engine)
        spec = engine.broker.symbol(pos.symbol)

        exact = round(pos.price_open - spec.point * 490, 5)
        assert abs(_worst(engine, pos, exact) - room) < 1e-9, _worst(engine, pos, exact)
        allowed = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {exact:.5f}", 4))
        assert "sl #" in allowed, allowed

        over = round(pos.price_open - spec.point * 500, 5)
        refused = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {over:.5f}", 5))
        engine.stop()

        assert "stop_exceeds_risk" in refused, refused
        assert abs(_live(engine, pos.ticket).sl - exact) < 1e-5, (
            "the refusal must leave the stop where the allowed move put it"
        )
