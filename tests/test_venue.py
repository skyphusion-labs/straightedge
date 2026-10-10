from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig, SessionConfig
from straightedge.constants import (
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_INVALID_VOLUME,
    TRADE_RETCODE_PLACED,
    TRADE_RETCODE_POSITION_CLOSED,
)
from straightedge.engine import Engine
from straightedge.models import MarketOrder, Side, WorkingOrder
from straightedge.synthetic import generate_bars


class VenueSpy:
    """Forwards PaperBroker and records venue vs order_send calls."""

    def __init__(self, inner: PaperBroker) -> None:
        self._inner = inner
        self.calls: list[str] = []

    def cancel(self, ticket: int):
        self.calls.append("cancel")
        return self._inner.cancel(ticket)

    def order_send(self, request: dict):
        self.calls.append("order_send")
        return self._inner.order_send(request)

    def close_position(self, ticket: int, **kwargs):
        self.calls.append("close_position")
        return self._inner.close_position(ticket, **kwargs)

    def market(self, order: MarketOrder):
        self.calls.append("market")
        return self._inner.market(order)

    def working(self, order: WorkingOrder):
        self.calls.append("working")
        return self._inner.working(order)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _paper() -> PaperBroker:
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(10, start=1.10, drift=0.0, vol=0.0001, seed=1))
    return broker


def _buy_stops(broker: PaperBroker, entry: float) -> tuple[float, float]:
    spec = broker.symbol("EURUSD")
    sl = spec.normalize_price(entry - 0.005)
    tp = spec.normalize_price(entry + 0.010)
    return sl, tp


def _market_buy(broker: PaperBroker, volume: float = 0.10, magic: int = 1):
    tick = broker.tick("EURUSD")
    sl, tp = _buy_stops(broker, tick.ask)
    return broker.market(
        MarketOrder(
            symbol="EURUSD",
            side=Side.BUY,
            volume=volume,
            sl=sl,
            tp=tp,
            magic=magic,
        )
    )


def test_market_buy_opens_position() -> None:
    broker = _paper()
    res = _market_buy(broker)
    assert res.ok
    assert res.retcode == TRADE_RETCODE_DONE
    pos = broker.positions()
    assert len(pos) == 1
    assert pos[0].side is Side.BUY
    assert pos[0].volume == 0.10
    assert pos[0].ticket == res.order


def test_check_market_does_not_open() -> None:
    broker = _paper()
    tick = broker.tick("EURUSD")
    sl, tp = _buy_stops(broker, tick.ask)
    order = MarketOrder(
        symbol="EURUSD",
        side=Side.BUY,
        volume=0.10,
        sl=sl,
        tp=tp,
        magic=1,
    )
    check = broker.check_market(order)
    assert check.ok
    assert check.order == 0
    assert broker.positions() == []
    sent = broker.market(order)
    assert sent.ok
    assert len(broker.positions()) == 1


def test_market_rejects_invalid_volume() -> None:
    broker = _paper()
    tick = broker.tick("EURUSD")
    sl, tp = _buy_stops(broker, tick.ask)
    res = broker.market(
        MarketOrder(
            symbol="EURUSD",
            side=Side.BUY,
            volume=0.001,
            sl=sl,
            tp=tp,
            magic=1,
        )
    )
    assert not res.ok
    assert res.retcode == TRADE_RETCODE_INVALID_VOLUME
    assert broker.positions() == []


def test_working_limit_then_cancel() -> None:
    broker = _paper()
    spec = broker.symbol("EURUSD")
    tick = broker.tick("EURUSD")
    price = spec.normalize_price(tick.ask - 0.002)
    sl, tp = _buy_stops(broker, price)
    res = broker.working(
        WorkingOrder(
            symbol="EURUSD",
            side=Side.BUY,
            kind="limit",
            volume=0.10,
            price=price,
            sl=sl,
            tp=tp,
            magic=1,
        )
    )
    assert res.ok
    assert res.retcode == TRADE_RETCODE_PLACED
    assert len(broker.orders()) == 1
    assert broker.positions() == []
    gone = broker.cancel(res.order)
    assert gone.ok
    assert broker.orders() == []


def test_working_stop_stays_working() -> None:
    broker = _paper()
    spec = broker.symbol("EURUSD")
    tick = broker.tick("EURUSD")
    price = spec.normalize_price(tick.ask + 0.002)
    sl, tp = _buy_stops(broker, price)
    res = broker.working(
        WorkingOrder(
            symbol="EURUSD",
            side=Side.BUY,
            kind="stop",
            volume=0.10,
            price=price,
            sl=sl,
            tp=tp,
            magic=1,
        )
    )
    assert res.ok
    assert res.retcode == TRADE_RETCODE_PLACED
    rows = broker.orders()
    assert len(rows) == 1
    assert rows[0].ticket == res.order
    assert broker.positions() == []


def test_check_working_does_not_place() -> None:
    broker = _paper()
    spec = broker.symbol("EURUSD")
    tick = broker.tick("EURUSD")
    price = spec.normalize_price(tick.ask - 0.002)
    sl, tp = _buy_stops(broker, price)
    order = WorkingOrder(
        symbol="EURUSD",
        side=Side.BUY,
        kind="limit",
        volume=0.10,
        price=price,
        sl=sl,
        tp=tp,
        magic=1,
    )
    check = broker.check_working(order)
    assert check.ok
    assert check.order == 0
    assert broker.orders() == []
    placed = broker.working(order)
    assert placed.ok
    assert len(broker.orders()) == 1


def test_cancel_missing_ticket() -> None:
    broker = _paper()
    res = broker.cancel(999)
    assert not res.ok
    assert res.retcode == TRADE_RETCODE_POSITION_CLOSED


def test_modify_position_sets_sl_tp() -> None:
    broker = _paper()
    opened = _market_buy(broker)
    pos = broker.positions()[0]
    spec = broker.symbol("EURUSD")
    sl = spec.normalize_price(pos.sl - 0.001)
    tp = spec.normalize_price(pos.tp + 0.001)
    res = broker.modify_position(pos.ticket, sl, tp, symbol=pos.symbol)
    assert res.ok
    assert res.retcode == TRADE_RETCODE_DONE
    updated = broker.positions()[0]
    assert updated.sl == sl
    assert updated.tp == tp
    assert updated.ticket == opened.order


def test_close_position_full() -> None:
    broker = _paper()
    _market_buy(broker)
    pos = broker.positions()[0]
    tick = broker.tick(pos.symbol)
    res = broker.close_position(
        pos.ticket,
        symbol=pos.symbol,
        side=pos.side.value,
        volume=pos.volume,
        price=tick.bid,
        magic=pos.magic,
    )
    assert res.ok
    assert res.retcode == TRADE_RETCODE_DONE
    assert broker.positions() == []


def test_close_position_partial() -> None:
    broker = _paper()
    _market_buy(broker, volume=0.10)
    pos = broker.positions()[0]
    tick = broker.tick(pos.symbol)
    res = broker.close_position(
        pos.ticket,
        symbol=pos.symbol,
        side=pos.side.value,
        volume=0.05,
        price=tick.bid,
        magic=pos.magic,
    )
    assert res.ok
    left = broker.positions()
    assert len(left) == 1
    assert abs(left[0].volume - 0.05) < 1e-12


def test_engine_flatten_uses_cancel_not_order_send(tmp_path: Path) -> None:
    inner = _paper()
    spy = VenueSpy(inner)
    cfg = BotConfig()
    cfg.session = SessionConfig(enabled=False)
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    engine = Engine(cfg, spy, halt_dir=str(tmp_path))
    engine.start()
    magic = cfg.risk.magic
    spec = spy.symbol("EURUSD")
    tick = spy.tick("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl, tp = _buy_stops(inner, limit)
    placed = spy.working(
        WorkingOrder(
            symbol="EURUSD",
            side=Side.BUY,
            kind="limit",
            volume=0.10,
            price=limit,
            sl=sl,
            tp=tp,
            magic=magic,
        )
    )
    assert placed.ok
    opened = _market_buy(inner, magic=magic)
    assert opened.ok
    spy.calls.clear()
    engine.flatten("test")
    assert "cancel" in spy.calls
    assert "order_send" not in spy.calls
    assert "close_position" in spy.calls
    assert spy.orders() == []
    assert spy.positions() == []
    assert engine.halted
    engine.stop()


def test_side_has_no_mt5_type_ints() -> None:
    assert "order_type" not in Side.__dict__
    assert "close_type" not in Side.__dict__


def test_engine_does_not_import_mt5_retcodes() -> None:
    import inspect

    import straightedge.engine as eng
    from straightedge.models import OrderResult

    assert "TRADE_RETCODE" not in inspect.getsource(eng)
    done = OrderResult.unchanged()
    assert done.ok
    assert done.comment == "unchanged"
    bad = OrderResult.invalid_stops("sl_required")
    assert not bad.ok
    assert bad.comment == "sl_required"
