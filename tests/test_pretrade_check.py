"""A pre-trade check that never ran must never read as a check that passed.

MT5 `order_check` uses retcode 0 for PASSED. When the terminal call returns
None nothing was measured at all, so COULD NOT MEASURE and PASSED must not
share a code, and the engine must abort rather than send.

These tests drive the shipped `Mt5Broker` against the shipped fake terminal
from `test_mt5_adapter`, wired into a real `Engine`. The only stubbed seam is
the MetaTrader5 module itself, so the adapter's own synthesis of a retcode is
under test rather than assumed.
"""

from __future__ import annotations

import json
from pathlib import Path

from straightedge.broker.mt5_live import Mt5Broker
from straightedge.config import BotConfig, SessionConfig
from straightedge.engine import Engine
from straightedge.models import Signal, SignalKind
from straightedge.broker.mt4_live import Mt4Broker
from test_mt4_adapter import FakeMt4
from test_mt5_adapter import FakeMt5


class NullCheckMt5(FakeMt5):
    """A terminal whose pre-trade check answers nothing at all.

    This is the live failure mode behind issue #8: an IPC hiccup makes
    `order_check` return None while `order_send` still works.
    """

    def order_check(self, request):
        del request
        return None


def _engine(tmp_path: Path, fake: FakeMt5) -> Engine:
    cfg = BotConfig()
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "journal.jsonl")
    return Engine(cfg, Mt5Broker(mt5=fake), halt_dir=str(tmp_path))


def _events(tmp_path: Path, name: str) -> list[dict]:
    path = tmp_path / "journal.jsonl"
    if not path.exists():
        return []
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    return [r for r in rows if r.get("event") == name]


def _buy() -> Signal:
    return Signal(
        kind=SignalKind.BUY,
        symbol="EURUSD",
        entry=1.1000,
        sl=1.0950,
        tp=1.1125,
        atr=0.0030,
        reason="test",
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


def test_null_pretrade_check_sends_no_market_order(tmp_path: Path) -> None:
    fake = NullCheckMt5()
    engine = _engine(tmp_path, fake)

    result = engine.submit(_buy(), 0.10)

    assert fake.sends == [], (
        f"market orders that reached the terminal: {len(fake.sends)} (want 0)"
    )
    assert not result.ok


def test_null_pretrade_check_journals_not_measured_not_a_refusal(tmp_path: Path) -> None:
    fake = NullCheckMt5()
    engine = _engine(tmp_path, fake)

    engine.submit(_buy(), 0.10)

    fails = _events(tmp_path, "order_check_fail")
    assert fails, "no order_check_fail event was journalled"
    assert fails[-1].get("reason") == "not_measured", (
        "operator cannot tell 'we never asked' from 'the broker said no': "
        f"reason={fails[-1].get('reason')!r}"
    )
    assert fails[-1].get("measured") is False
    assert "no result" in str(fails[-1].get("comment"))


def test_null_pretrade_check_sends_no_working_order(tmp_path: Path) -> None:
    fake = NullCheckMt5()
    engine = _engine(tmp_path, fake)

    result = engine.submit(_buy_limit(), 0.10)

    assert fake.sends == [], (
        f"working orders that reached the terminal: {len(fake.sends)} (want 0)"
    )
    assert not result.ok
    fails = _events(tmp_path, "order_check_fail")
    assert fails and fails[-1].get("reason") == "not_measured"


def test_null_result_retcode_is_not_a_passing_code(tmp_path: Path) -> None:
    del tmp_path
    from straightedge.models import MarketOrder, Side

    broker = Mt5Broker(mt5=NullCheckMt5())
    check = broker.check_market(
        MarketOrder(symbol="EURUSD", side=Side.BUY, volume=0.10, sl=1.0950, tp=1.1125)
    )

    assert check.retcode != 0, "a null result is wearing order_check's PASSED code"
    assert not check.measured
    assert not check.ok


def test_genuine_passing_check_still_sends(tmp_path: Path) -> None:
    """Control: a real order_check retcode 0 must keep passing."""
    fake = FakeMt5()
    engine = _engine(tmp_path, fake)

    result = engine.submit(_buy(), 0.10)

    assert len(fake.sends) == 1, f"orders sent: {len(fake.sends)} (want 1)"
    assert result.ok
    assert _events(tmp_path, "order_check_fail") == []


class VerdictlessCheckMt4(FakeMt4):
    """An MT4 EA whose pre-trade check answers with no verdict field at all.

    The mailbox reply matched the request id, so the bridge returns it, but it
    carries neither `ok` nor `retcode`. Nothing was measured.
    """

    def call(self, op: str, payload: dict) -> dict:
        if op in {"check_market", "check_working"}:
            return {"id": 1}
        return super().call(op, payload)


def _mt4_engine(tmp_path: Path, fake: FakeMt4) -> Engine:
    cfg = BotConfig()
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "journal.jsonl")
    return Engine(cfg, Mt4Broker(fake.call), halt_dir=str(tmp_path))


def test_mt4_verdictless_check_is_not_measured() -> None:
    from straightedge.constants import TRADE_RETCODE_REJECT
    from straightedge.models import MarketOrder, Side

    broker = Mt4Broker(VerdictlessCheckMt4().call)
    check = broker.check_market(
        MarketOrder(symbol="EURUSD", side=Side.BUY, volume=0.10, sl=1.0950, tp=1.1125)
    )

    assert not check.measured
    assert not check.ok
    assert check.retcode != TRADE_RETCODE_REJECT, (
        "an unanswered check is being reported as a broker refusal"
    )
    assert "no result" in check.comment


def test_mt4_verdictless_check_sends_no_order(tmp_path: Path) -> None:
    fake = VerdictlessCheckMt4()
    engine = _mt4_engine(tmp_path, fake)

    result = engine.submit(_buy(), 0.10)

    assert fake.positions == [], (
        f"market orders that reached the terminal: {len(fake.positions)} (want 0)"
    )
    assert not result.ok
    fails = _events(tmp_path, "order_check_fail")
    assert fails and fails[-1].get("reason") == "not_measured"


def test_mt4_genuine_check_still_sends(tmp_path: Path) -> None:
    """Control: a real MT4 ok=True check must keep passing."""
    fake = FakeMt4()
    engine = _mt4_engine(tmp_path, fake)

    result = engine.submit(_buy(), 0.10)

    assert len(fake.positions) == 1, f"orders sent: {len(fake.positions)} (want 1)"
    assert result.ok


def test_operator_line_separates_never_asked_from_refused() -> None:
    from straightedge.engine import _format_event

    unmeasured = _format_event(
        "order_check_fail",
        {"reason": "not_measured", "symbol": "EURUSD", "retcode": -1, "comment": "no result: ipc"},
    )
    refused = _format_event(
        "order_check_fail",
        {"reason": "broker_refused", "symbol": "EURUSD", "retcode": 10019, "comment": "No money"},
    )

    assert "NOT MEASURED" in unmeasured
    assert "NOT sent" in unmeasured
    assert "NOT MEASURED" not in refused
    assert "refused" in refused
    assert unmeasured != refused
