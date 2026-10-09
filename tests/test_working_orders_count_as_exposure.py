"""A working order is committed exposure and counts toward the limits.

`RiskManager.evaluate` consumed `positions` and nothing else, and
`Engine.preview` passed only `broker.positions()`. So `already_in_symbol`,
`max_positions` and `currency_exposure` were all blind to orders resting on the
book. Reproduced by the reviewer: four `/buy EURUSD limit=...` plus `/confirm`
were every one accepted, 2% of equity committed (the whole daily-loss budget) and
past `max_positions = 3`, because none of the first three had left a `Position`.

A pending order is not a hypothesis. It is an instruction sitting at the broker
that will become a position without anyone being asked again, so for a gate that
asks "how much am I committed to" it counts exactly as a position does.

## Scope, stated plainly

This brings working orders to PARITY with positions on the three gates that
COUNT. It deliberately does not add a committed-risk reservation against
`loss_room`, and that is not an oversight: `loss_room` does not sum the risk of
OPEN positions either. It measures realised and marked equity movement, so
summing unfilled orders into it would be a new control, applying to both kinds of
exposure, and a different decision from this one. Filed separately.

## Fail-closed on an unmeasured read

`broker.orders()` can raise (MT4 `_require` turns an EA error into a
`RuntimeError`), and a gate that read that as "no working orders" would fail OPEN
in exactly the case it exists for. `Engine._read_working` already reports whether
the read succeeded, so a failed read REFUSES rather than proceeding on an
assumption. That is the same partition as `OrderResult.measured`: PASSED, REFUSED,
COULD NOT MEASURE.

## What this suite does NOT cover (straightedge#123)

It pins working orders counting toward the limits (`Engine.preview` handing
`orders` to `RiskManager.evaluate`, and the fail-closed unmeasured read). It is
BLIND to the stop-modification guard, which `tests/test_stop_modification_guard.py`
pins, and that file is equally blind to this one. Neither is a control on the
other.

Measured by mutation, first during the #107 rebase and re-measured on `main` at
`b8d7d74` (the counts moved, the property did not):

| mutant | what it deletes | this file | the stop-guard file |
| --- | --- | --- | --- |
| A | `reason = self._stop_guard(pos, sl)` -> `reason = ""` in `Engine._modify` | 9 passed, exit 0 | 11 failed, 12 passed, exit 1 |
| B | `ours_orders = [o for o in orders if o.magic == r.magic]` -> `ours_orders = []` in `RiskManager.evaluate` (`src/straightedge/risk.py`, NOT `engine.py`) | 5 failed, 4 passed, exit 1 | 23 passed, exit 0 |

Orientation note: the original issue's table printed mutant B's failure in the
stop-guard column; the measurement puts it in the working-orders column, as here.

So each file is green while the other's guard is deleted. After touching either
guard, run BOTH files:

    pytest -q --override-ini addopts= tests/test_stop_modification_guard.py tests/test_working_orders_count_as_exposure.py

Nothing here is verified against a live MT4 or MT5 terminal.
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
SYMBOLS = ("EURUSD", "GBPUSD", "AUDUSD", "USDCHF", "USDJPY")


def _engine(tmp_path: Path, broker: PaperBroker | None = None, **kw) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    for key in ("max_positions", "max_currency_exposure", "risk_pct"):
        if key in kw:
            setattr(cfg.risk, key, kw[key])
    broker = broker or PaperBroker(balance=kw.get("balance", 10_000))
    for i, sym in enumerate(SYMBOLS):
        broker.seed_bars(sym, generate_bars(120, drift=0.0004, vol=0.0002, seed=3 + i))
    cfg.symbols = list(SYMBOLS)
    return Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: WED_NOON)


def _reply(engine: Engine, staged: str, n: int) -> str:
    """Whichever reply carries the verdict.

    `Desk._stage` runs `engine.preview` and refuses THERE, so a refused command
    never leaves a pending and the follow-up `/confirm` answers "nothing to
    confirm". Reading only the `/confirm` reply would hide every refusal behind
    that string, which is how a gate test passes for the wrong reason.
    """
    if "refused" in staged:
        return staged
    return engine.handle_command(TgCommand("1", 1, "/confirm", n + 1))


def _place_limit(engine: Engine, symbol: str, n: int) -> str:
    """Stage and confirm one buy limit below the market."""
    price = engine.broker.tick(symbol).ask - 0.0050
    staged = engine.handle_command(
        TgCommand("1", 1, f"/buy {symbol} limit={price:.5f}", n)
    )
    return _reply(engine, staged, n)


def _place_market(engine: Engine, symbol: str, n: int) -> str:
    staged = engine.handle_command(TgCommand("1", 1, f"/buy {symbol}", n))
    return _reply(engine, staged, n)


def _working(engine: Engine) -> list:
    return engine.broker.orders(magic=engine.cfg.risk.magic)


class TestTheSameSymbolTwice:
    def test_a_second_limit_on_a_symbol_that_already_has_one_is_refused(
        self, tmp_path: Path
    ) -> None:
        """The reviewer's reproduction, at its first step."""
        engine = _engine(tmp_path)
        engine.start()
        first = _place_limit(engine, "EURUSD", 1)
        assert "sent buy" in first, first
        assert len(_working(engine)) == 1, _working(engine)

        second = _place_limit(engine, "EURUSD", 3)
        engine.stop()

        assert "already_in_symbol" in second, second
        assert len(_working(engine)) == 1, (
            "a second working order rests on EURUSD; the desk is committed twice "
            "to one symbol"
        )

    def test_a_market_entry_is_refused_on_top_of_a_working_order(
        self, tmp_path: Path
    ) -> None:
        """The stacking case: a limit resting, then a market buy on the same symbol."""
        engine = _engine(tmp_path)
        engine.start()
        _place_limit(engine, "EURUSD", 1)

        reply = _place_market(engine, "EURUSD", 3)
        engine.stop()

        assert not engine.broker.positions(magic=engine.cfg.risk.magic), (
            "a market position was opened on a symbol that already had a working "
            "order"
        )
        assert "already_in_symbol" in reply, reply


class TestTheSlotLimit:
    def test_working_orders_fill_the_position_slots(self, tmp_path: Path) -> None:
        """`max_positions = 3`, so the fourth commitment is refused."""
        engine = _engine(tmp_path, max_positions=3, max_currency_exposure=99)
        engine.start()
        for i, sym in enumerate(SYMBOLS[:3]):
            reply = _place_limit(engine, sym, 1 + i * 2)
            assert "sent buy" in reply, (sym, reply)
        assert len(_working(engine)) == 3, _working(engine)

        fourth = _place_limit(engine, SYMBOLS[3], 9)
        engine.stop()

        assert "max_positions" in fourth, fourth
        assert len(_working(engine)) == 3, (
            f"{len(_working(engine))} working orders rest on the book against a "
            "max_positions of 3"
        )

    def test_a_position_and_a_working_order_share_the_slots(
        self, tmp_path: Path
    ) -> None:
        """Mixed exposure. Two slots used means one left, not two."""
        engine = _engine(tmp_path, max_positions=2, max_currency_exposure=99)
        engine.start()
        assert "sent buy" in _place_market(engine, "EURUSD", 1)
        assert "sent buy" in _place_limit(engine, "GBPUSD", 3)

        third = _place_limit(engine, "AUDUSD", 5)
        engine.stop()

        assert "max_positions" in third, third

    def test_another_desks_working_orders_do_not_count(self, tmp_path: Path) -> None:
        """Positive control: the magic filter still applies.

        Without this, counting every order on the terminal would pass the tests
        above and refuse a desk that shares a terminal with anything else.
        """
        broker = PaperBroker(balance=10_000)
        engine = _engine(tmp_path, broker, max_positions=1, max_currency_exposure=99)
        engine.start()
        price = broker.tick("EURUSD").ask - 0.0050
        from straightedge.models import Side, WorkingOrder

        broker.working(
            WorkingOrder(
                symbol="GBPUSD",
                side=Side.BUY,
                kind="limit",
                volume=0.10,
                price=price,
                sl=price - 0.0050,
                magic=engine.cfg.risk.magic + 1,
            )
        )
        reply = _place_limit(engine, "EURUSD", 1)
        engine.stop()

        assert "sent buy" in reply, reply


class TestCurrencyExposure:
    def test_working_order_legs_count_toward_currency_exposure(
        self, tmp_path: Path
    ) -> None:
        """Two USD-quote limits committed, so a third USD leg is refused."""
        engine = _engine(tmp_path, max_positions=9, max_currency_exposure=2)
        engine.start()
        assert "sent buy" in _place_limit(engine, "EURUSD", 1)
        assert "sent buy" in _place_limit(engine, "GBPUSD", 3)

        third = _place_limit(engine, "AUDUSD", 5)
        engine.stop()

        assert "currency_exposure" in third, third


class TestAnUnmeasuredReadRefuses:
    def test_a_failed_working_order_read_refuses_the_send(self, tmp_path: Path) -> None:
        """COULD NOT MEASURE is not "no working orders".

        MT4 turns an EA error into a RuntimeError here. Reading that as an empty
        book would fail OPEN in exactly the case the gate exists for.
        """

        class BlindBroker(PaperBroker):
            def orders(self, magic: int | None = None):  # type: ignore[override]
                raise RuntimeError("mt4 bridge timeout")

        engine = _engine(tmp_path, BlindBroker(balance=10_000))
        engine.start()
        reply = _place_market(engine, "EURUSD", 1)
        engine.stop()

        assert not engine.broker.positions(magic=engine.cfg.risk.magic), (
            "an order was sent while the working-order book could not be read"
        )
        assert "orders_unmeasured" in reply, reply

    def test_a_readable_empty_book_still_allows_the_send(self, tmp_path: Path) -> None:
        """Positive control: an EMPTY book is not an unmeasured one.

        Without this, refusing whenever `orders()` returned nothing would pass the
        test above and stop the desk trading at all.
        """
        engine = _engine(tmp_path)
        engine.start()
        assert _working(engine) == []
        reply = _place_market(engine, "EURUSD", 1)
        engine.stop()

        assert "sent buy" in reply, reply


class TestTheOperatorsOwnDisplay:
    def test_risk_text_counts_working_orders(self, tmp_path: Path) -> None:
        """`/risk` said `positions=0/3` with three commitments on the book.

        A status display that corroborates the wrong number is worse than no
        display: it is what an operator checks before deciding the gate is fine.
        """
        engine = _engine(tmp_path, max_positions=3, max_currency_exposure=99)
        engine.start()
        _place_limit(engine, "EURUSD", 1)
        text = engine.risk_text()
        engine.stop()

        assert "0/3" not in text, (
            "the operator is shown zero commitments while a working order rests "
            f"on the book: {text}"
        )
        assert "1/3" in text, text
