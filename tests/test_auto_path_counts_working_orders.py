"""straightedge#207: the AUTO path's risk evaluation is unverified on `orders`.

`risk.evaluate` is handed `orders=orders` from two sites, and only one of them
has a test:

    engine.py:1192  in _act()      <- the AUTO tick path
    engine.py:1307  in preview()   <- the MANUAL path (desk /buy, /sell, ...)

Measured on `main`, mutating each to `[]`:

    preview's   orders=orders -> []   working-orders suite: 5 failed, rc=1
    risk.py's   ours_orders   -> []   working-orders suite: 5 failed, rc=1
    _act's      orders=orders -> []   working-orders suite: 9 passed, rc=0

**Dropping working orders from the auto path's evaluation was caught by
nothing.** `risk.py` says the `orders` parameter "has no default on purpose. A
new caller that forgets it gets a TypeError rather than the old fail-OPEN
behaviour", and that is true and insufficient: it protects against a caller
OMITTING the argument, not against one passing the wrong thing, and the auto
path was the caller with no test.

If it regressed, resting working orders would stop counting toward
`max_positions`, `already_in_symbol` and the currency limit **for auto entries
only**, which is the exact defect `tests/test_working_orders_count_as_exposure.py`
exists to prevent, reproduced in the one path that suite does not reach.

## Which gate this asserts, and why not `already_in_symbol`

`_act` carries its OWN same-symbol check before it ever calls `evaluate`:

    committed = [*positions, *orders]
    if any(x.symbol == symbol for x in committed):
        return

So on the auto path a same-symbol order is refused BEFORE the risk engine sees
it, and a test built on `already_in_symbol` would pass whatever `orders=` was
handed to `evaluate`: it would be measuring that earlier check instead. The
order therefore rests on a DIFFERENT symbol here, and the gate under test is
`max_positions`, which counts committed exposure across symbols and is reached
only through the parameter in question.

## The precondition that makes the red possible

Stated because a mutation on the auto path is EQUIVALENT on any fixture that
never reaches that path, and because an implicit precondition on evidence is
its own defect. These cases need all three:

1. a working order actually resting on the book (placed through the desk, not
   fabricated), on a symbol OTHER than the one the auto leg acts on;
2. an auto tick that reaches `evaluate`, which means a bar series that produces
   a signal and a venue clock that can be measured;
3. `max_positions` low enough that the resting order alone fills it.

Drop any one and the mutation is equivalent and the suite is green for a reason
that has nothing to do with the gate.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand

WED_NOON = datetime(2024, 1, 3, 12, tzinfo=timezone.utc)
#: The auto leg acts on this one; the resting order sits on the other.
ACTS_ON = "EURUSD"
RESTS_ON = "GBPUSD"
#: A third pair that is also SHORT USD when bought. The first version of
#: the currency case used `USDCHF`, which is LONG USD and therefore
#: CANCELLED the resting order's own leg: the cap was never reached and the
#: case failed with no refusal at all, which is the fixture testing a state
#: it did not intend rather than the gate being wrong.
ALSO_SHORT_USD = "AUDUSD"
#: Known to produce a signal through `TrendStrategy`, same parameters the
#: session case in `tests/test_refusal_reasons.py` uses for the same reason.
SIGNAL_BARS = dict(drift=0.0006, vol=0.0002, seed=7)


def _engine(tmp_path: Path, **kw) -> tuple[Engine, list]:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.risk.max_positions = int(kw.get("max_positions", 1))
    cfg.symbols = [ACTS_ON, RESTS_ON, ALSO_SHORT_USD]
    broker = PaperBroker(balance=10_000)
    bars = generate_bars(250, **SIGNAL_BARS)
    broker.seed_bars(ACTS_ON, bars)
    broker.seed_bars(RESTS_ON, generate_bars(250, drift=0.0004, vol=0.0002, seed=4))
    broker.seed_bars(
        ALSO_SHORT_USD, generate_bars(250, drift=0.0004, vol=0.0002, seed=5)
    )
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: WED_NOON)
    return engine, bars


def _rest_an_order(engine: Engine, symbol: str, n: int = 1) -> None:
    """Place a real working order through the desk and prove it is resting."""
    price = engine.broker.tick(symbol).ask - 0.0050
    staged = engine.handle_command(
        TgCommand("1", 1, f"/buy {symbol} limit={price:.5f}", n)
    )
    assert "refused" not in staged, f"the fixture could not place its order: {staged}"
    engine.handle_command(TgCommand("1", 1, "/confirm", n + 1))
    resting = engine.broker.orders(magic=engine.cfg.risk.magic)
    assert [o.symbol for o in resting] == [symbol], (
        f"precondition 1 failed: nothing is resting on {symbol}: {resting}"
    )


def _rejects(engine: Engine) -> list[dict]:
    return [r for r in engine.journal.tail(5000) if r.get("event") == "reject"]


def _auto_reasons(engine: Engine) -> list[str]:
    return [
        str(r.get("reason", ""))
        for r in _rejects(engine)
        if r.get("source") == "auto"
    ]


def test_a_resting_order_fills_the_slot_limit_for_an_auto_entry(
    tmp_path: Path,
) -> None:
    """The gap, closed. One resting order on another symbol, max_positions=1.

    This is the case that mutating `_act`'s `orders=orders` to `[]` leaves
    green on `main`: the auto leg would size and send an entry while a working
    order already filled the only slot.
    """
    engine, bars = _engine(tmp_path, max_positions=1)
    engine.start()
    _rest_an_order(engine, RESTS_ON)

    engine.replay_symbol(ACTS_ON, bars)

    assert "max_positions" in _auto_reasons(engine), (
        "the auto leg did not count a resting working order toward the slot "
        f"limit: {_auto_reasons(engine)}"
    )
    rec = [r for r in _rejects(engine) if r.get("reason") == "max_positions"][-1]
    assert rec["source"] == "auto", "the refusal came from the manual path"
    assert rec["stage"] == "signal"
    assert rec["symbol"] == ACTS_ON
    assert not [
        p
        for p in engine.broker.positions(magic=engine.cfg.risk.magic)
        if p.symbol == ACTS_ON
    ], "the auto leg opened a position past the slot limit"
    engine.stop()


def test_without_the_resting_order_the_same_auto_tick_is_allowed(
    tmp_path: Path,
) -> None:
    """The control, and the refusal above is unattributable without it.

    Same engine, same bars, same slot limit, same instant. The ONE difference
    is whether an order is resting, so the refusal is attributable to the order
    rather than to the fixture, the session, the circuit or the strategy.
    """
    engine, bars = _engine(tmp_path, max_positions=1)
    engine.start()
    engine.replay_symbol(ACTS_ON, bars)
    assert "max_positions" not in _auto_reasons(engine), (
        "the slot limit refused with nothing committed, so the case above "
        f"proves nothing: {_auto_reasons(engine)}"
    )
    engine.stop()


def test_the_auto_leg_reaches_the_risk_engine_at_all(tmp_path: Path) -> None:
    """Precondition 2, asserted rather than assumed.

    A bar series that produces no signal, or a venue clock that cannot be
    measured, would make `_act` return before `evaluate` and every assertion
    about `orders=` would be vacuous. Raising `max_positions` so nothing blocks
    the entry, the same tick must OPEN, which is only reachable through
    `evaluate`.
    """
    engine, bars = _engine(tmp_path, max_positions=5)
    engine.start()
    engine.replay_symbol(ACTS_ON, bars)
    opened = engine.journal.last_event("open")
    assert opened is not None, (
        "the auto tick never reached the risk engine, so this file's other "
        f"cases would be vacuous: {_auto_reasons(engine)}"
    )
    assert opened["symbol"] == ACTS_ON
    assert opened["reason"] != "fill", "that was a pending fill, not an auto entry"
    engine.stop()


def test_a_resting_order_counts_toward_the_currency_limit_on_the_auto_path(
    tmp_path: Path,
) -> None:
    """The second gate through the same parameter, since one is a single point.

    `max_positions` and `currency_exposure` read the same `orders` argument by
    different routes, so a change that satisfied one and not the other would be
    caught here and not above. Two legs of USD exposure are already committed,
    one of them unfilled, and a third would be three deep on one currency.
    """
    engine, bars = _engine(tmp_path, max_positions=5)
    engine.cfg.risk.max_currency_exposure = 2
    engine.start()
    _rest_an_order(engine, RESTS_ON)
    # A second SHORT-USD leg, filled this time, so the cap of two is reached
    # only by counting the resting order as well.
    staged = engine.handle_command(TgCommand("1", 1, f"/buy {ALSO_SHORT_USD}", 5))
    assert "refused" not in staged, staged
    engine.handle_command(TgCommand("1", 1, "/confirm", 6))
    assert len(engine.broker.positions(magic=engine.cfg.risk.magic)) == 1

    engine.replay_symbol(ACTS_ON, bars)
    assert "currency_exposure" in _auto_reasons(engine), (
        "the auto leg did not count the resting order's currency leg: "
        f"{_auto_reasons(engine)}"
    )
    engine.stop()
