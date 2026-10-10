from pathlib import Path

from straightedge.broker.paper import PaperBroker, default_spec
from straightedge.config import BotConfig, SessionConfig
from straightedge.engine import Engine, run_backtest
from straightedge.models import Side
from straightedge.strategy import TrendStrategy
from straightedge.synthetic import generate_bars, generate_ranging
from wincompat import assert_owner_mode


class FlakyBroker:
    def __init__(self, inner: PaperBroker) -> None:
        self._inner = inner
        self.fail_account = 0
        self.fail_connect = False
        self.connects = 0
        self.ensure_calls = 0
        self.call_order: list[str] = []

    def connect(self) -> None:
        self.connects += 1
        if self.fail_connect:
            raise RuntimeError("mt5.initialize failed: (1, 'no ipc')")
        self._inner.connect()

    def disconnect(self) -> None:
        self._inner.disconnect()

    def ensure_connected(self) -> None:
        self.ensure_calls += 1
        self.call_order.append("ensure")

    def account(self):
        self.call_order.append("account")
        if self.fail_account > 0:
            self.fail_account -= 1
            raise RuntimeError("account_info failed: IPC timeout")
        return self._inner.account()

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _cfg(**kw) -> BotConfig:
    cfg = BotConfig()
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = kw.get("symbols", ["EURUSD"])
    cfg.initial_balance = kw.get("balance", 10_000.0)
    cfg.journal_path = kw.get("journal", "journal.jsonl")
    return cfg


def test_signal_reprice_keeps_distance() -> None:
    from straightedge.models import Signal, SignalKind

    spec = default_spec("EURUSD")
    sig = Signal(SignalKind.BUY, "EURUSD", 1.10000, 1.09500, 1.11250, 0.003)
    moved = sig.reprice(1.10100, spec)
    assert abs((moved.entry - moved.sl) - (sig.entry - sig.sl)) < spec.point
    assert abs((moved.tp - moved.entry) - (sig.tp - sig.entry)) < spec.point


def test_strategy_buys_uptrend() -> None:
    bars = generate_bars(250, drift=0.0006, vol=0.0002, seed=7)
    sig = TrendStrategy(BotConfig().strategy).signal("EURUSD", bars, default_spec("EURUSD"))
    assert sig.side is Side.BUY
    assert sig.sl < sig.entry < sig.tp
    assert sig.rr >= 1.5


def test_strategy_flat_on_dead_market() -> None:
    from straightedge.models import Bar, SignalKind

    bars = [
        Bar(time=i * 3600, open=1.1, high=1.10005, low=1.09995, close=1.1) for i in range(250)
    ]
    sig = TrendStrategy(BotConfig().strategy).signal("EURUSD", bars, default_spec("EURUSD"))
    assert sig.kind is SignalKind.FLAT


def test_paper_roundtrip_stop() -> None:
    broker = PaperBroker(balance=10_000)
    bars = generate_bars(10, start=1.10, drift=0.0, vol=0.0001, seed=1)
    broker.seed_bars("EURUSD", bars)
    spec = default_spec("EURUSD")
    tick = broker.tick("EURUSD")
    from straightedge.constants import (
        ORDER_TYPE_BUY,
        TRADE_ACTION_DEAL,
        TRADE_RETCODE_DONE,
    )

    sl = spec.normalize_price(tick.ask - 0.005)
    tp = spec.normalize_price(tick.ask + 0.010)
    res = broker.order_send(
        {
            "action": TRADE_ACTION_DEAL,
            "symbol": "EURUSD",
            "volume": 0.10,
            "type": ORDER_TYPE_BUY,
            "price": tick.ask,
            "sl": sl,
            "tp": tp,
            "magic": 1,
        }
    )
    assert res.retcode == TRADE_RETCODE_DONE
    assert broker.positions()
    # Drive price through the stop.
    from straightedge.models import Bar

    last = bars[-1]
    crash = Bar(
        time=last.time + 3600,
        open=last.close,
        high=last.close,
        low=sl - 0.001,
        close=sl - 0.0005,
    )
    closed = broker.on_bar("EURUSD", crash)
    assert closed
    assert not broker.positions()
    assert broker.account().balance < 10_000


def test_backtest_trending_not_ruin(tmp_path: Path) -> None:
    cfg = _cfg(journal=str(tmp_path / "j.jsonl"))
    series = {"EURUSD": generate_bars(1200, drift=0.00035, vol=0.0004, seed=11)}
    result = run_backtest(cfg, series, journal_path=cfg.journal_path)
    assert result["equity"] > cfg.initial_balance * 0.90  # did not blow up
    assert result["open_positions"] <= 1


def test_backtest_trending_positive(tmp_path: Path) -> None:
    cfg = _cfg(journal=str(tmp_path / "j.jsonl"))
    series = {"EURUSD": generate_bars(2000, drift=0.0005, vol=0.00025, seed=21)}
    result = run_backtest(cfg, series, journal_path=cfg.journal_path)
    assert result["equity"] > cfg.initial_balance


def test_backtest_range_survives(tmp_path: Path) -> None:
    cfg = _cfg(journal=str(tmp_path / "j.jsonl"))
    series = {"EURUSD": generate_ranging(1500, seed=12)}
    result = run_backtest(cfg, series, journal_path=cfg.journal_path)
    # Whipsaw is allowed; ruin is not.
    assert result["equity"] > cfg.initial_balance * 0.85


def test_engine_respects_halt_file(tmp_path: Path) -> None:
    cfg = _cfg(journal=str(tmp_path / "j.jsonl"))
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker = PaperBroker(balance=10_000)
    bars = generate_bars(100, drift=0.0005, seed=1)
    broker.seed_bars("EURUSD", bars)
    engine = Engine(cfg, broker, halt_dir=tmp_path)
    engine.start()
    (tmp_path / "HALT").write_text("x", encoding="utf-8")
    engine.step_all()
    assert engine.halted
    engine.stop()


def test_unknown_and_positions_commands(tmp_path: Path) -> None:
    from straightedge.telegram import TgCommand

    cfg = _cfg(journal=str(tmp_path / "j.jsonl"))
    engine = Engine(cfg, PaperBroker(balance=10_000), halt_dir=str(tmp_path))
    engine.start()
    assert "unknown" in engine.handle_command(TgCommand("1", 1, "/nope", 1))
    assert "no open" in engine.handle_command(TgCommand("1", 1, "/positions", 2))
    assert "status" in engine.handle_command(TgCommand("1", 1, "/help", 3))
    engine.stop()


def test_filling_choice() -> None:
    from straightedge.constants import (
        ORDER_FILLING_FOK,
        ORDER_FILLING_IOC,
        ORDER_FILLING_RETURN,
        choose_filling,
    )

    assert choose_filling(1) == ORDER_FILLING_FOK
    assert choose_filling(2) == ORDER_FILLING_IOC
    assert choose_filling(3) == ORDER_FILLING_FOK
    assert choose_filling(0) == ORDER_FILLING_RETURN


def test_step_all_reconnects_after_account_drop(tmp_path: Path) -> None:
    cfg = _cfg(journal=str(tmp_path / "j.jsonl"))
    cfg.risk.halt_file = str(tmp_path / "HALT")
    inner = PaperBroker(balance=10_000)
    inner.seed_bars("EURUSD", generate_bars(80, drift=0.0004, seed=3))
    broker = FlakyBroker(inner)
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()
    started = broker.connects
    broker.fail_account = 1
    engine.step_all()
    assert broker.connects == started + 1
    assert not engine.halted
    events = [rec.get("event") for rec in engine.journal.tail(20)]
    assert "reconnect" in events
    rec = [r for r in engine.journal.tail(20) if r.get("event") == "reconnect"][-1]
    assert rec.get("ok") is True
    assert_owner_mode(tmp_path / "j.heartbeat")
    engine.stop()


def test_step_all_skips_tick_if_reconnect_fails(tmp_path: Path) -> None:
    cfg = _cfg(journal=str(tmp_path / "j.jsonl"))
    cfg.risk.halt_file = str(tmp_path / "HALT")
    inner = PaperBroker(balance=10_000)
    inner.seed_bars("EURUSD", generate_bars(80, drift=0.0004, seed=3))
    broker = FlakyBroker(inner)
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()
    broker.fail_account = 1
    broker.fail_connect = True
    engine.step_all()
    assert not engine.halted
    rec = [r for r in engine.journal.tail(20) if r.get("event") == "reconnect"][-1]
    assert rec.get("ok") is False
    assert "initialize" in str(rec.get("error") or "")
    engine.stop()


def test_step_all_calls_ensure_connected_before_account(tmp_path: Path) -> None:
    cfg = _cfg(journal=str(tmp_path / "j.jsonl"))
    cfg.risk.halt_file = str(tmp_path / "HALT")
    inner = PaperBroker(balance=10_000)
    inner.seed_bars("EURUSD", generate_bars(80, drift=0.0004, seed=3))
    broker = FlakyBroker(inner)
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()
    broker.call_order.clear()
    broker.fail_account = 1
    engine.step_all()
    assert broker.ensure_calls >= 1
    assert broker.call_order[0] == "ensure"
    assert "account" in broker.call_order
    assert broker.call_order.index("ensure") < broker.call_order.index("account")
    assert not engine.halted
    rec = [r for r in engine.journal.tail(20) if r.get("event") == "reconnect"][-1]
    assert rec.get("ok") is True
    engine.stop()
