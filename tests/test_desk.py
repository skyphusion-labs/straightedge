import json
import re
import time
from datetime import datetime, timezone

from wincompat import assert_owner_mode
from straightedge.broker.paper import PaperBroker
from straightedge.config import AdviceConfig, BotConfig
from straightedge.constants import (
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    TRADE_ACTION_DEAL,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_INVALID,
    TRADE_RETCODE_INVALID_STOPS,
)
from straightedge.desk import pending_from_record
from straightedge.engine import Engine
from straightedge.llm import Advisor, parse_advice
from straightedge.models import OrderResult
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand


class FakeLlm:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.sent: list[tuple[str, dict]] = []

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None) -> dict:
        self.sent.append((url, payload))
        return self.payload


def _engine(tmp_path, *, llm=None) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    advisor = None
    if llm is not None:
        advisor = Advisor(AdviceConfig(provider="grok", grok_key="x"), transport=llm)
    return Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        advisor=advisor,
        now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
    )


def test_buy_confirm_opens(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert "confirm buy EURUSD" in reply
    assert not engine.broker.positions()
    reply = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert reply.startswith("sent buy")
    assert engine.broker.positions()
    engine.stop()


def test_quote_and_close(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    q = engine.handle_command(TgCommand("1", 1, "/quote EURUSD", 1))
    assert "bid=" in q
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 2))
    engine.handle_command(TgCommand("1", 1, "/confirm", 3))
    closed = engine.handle_command(TgCommand("1", 1, "/close EURUSD", 4))
    assert closed.startswith("closed")
    assert not engine.broker.positions()
    engine.stop()


def test_ask_stages_grok_trade(tmp_path) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Trend is up.\n"
                        '{"action":"buy","symbol":"EURUSD","sl":null,"tp":null,"summary":"join long"}'
                    )
                }
            }
        ]
    }
    engine = _engine(tmp_path, llm=FakeLlm(payload))
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "should I buy euro?", 1))
    assert "Trend is up" in reply
    assert "/confirm" in reply
    engine.stop()


def test_advice_circuit_blocks_buy_allows_close(tmp_path) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Buy.\n"
                        '{"action":"buy","symbol":"EURUSD","sl":null,"tp":null,'
                        '"limit":null,"stop":null,"ticket":null,"summary":"long"}'
                    )
                }
            }
        ]
    }
    engine = _engine(tmp_path, llm=FakeLlm(payload))
    engine.start()
    engine.risk.write_halt_file("operator")
    blob = engine.advice_context()
    assert "CIRCUIT would halt" in blob
    assert "halt_file" in blob
    assert "hold or close" in blob.lower()
    reply = engine.handle_command(TgCommand("1", 1, "/ask buy?", 1))
    assert "not staging buy" in reply
    assert "hold or close only" in reply
    assert engine.desk.pending is None
    engine.stop()


def test_advice_circuit_allows_close(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        llm=FakeLlm({"choices": [{"message": {"content": "x"}}]}),
    )
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    engine.advisor.transport.payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Out.\n"
                        f'{{"action":"close","symbol":"EURUSD","sl":null,"tp":null,'
                        f'"limit":null,"stop":null,"ticket":{pos.ticket},"summary":"out"}}'
                    )
                }
            }
        ]
    }
    engine.risk.write_halt_file("operator")
    reply = engine.handle_command(TgCommand("1", 1, "/ask flatten?", 3))
    assert f"confirm close #{pos.ticket}" in reply
    engine.stop()


def test_advice_daily_loss_blocks_buy_without_flatten(tmp_path) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Buy.\n"
                        '{"action":"buy","symbol":"EURUSD","sl":null,"tp":null,'
                        '"limit":null,"stop":null,"ticket":null,"summary":"long"}'
                    )
                }
            }
        ]
    }
    engine = _engine(tmp_path, llm=FakeLlm(payload))
    engine.start()
    engine.broker._balance = 9_700.0
    blob = engine.advice_context()
    assert "CIRCUIT would halt" in blob
    assert "daily_loss" in blob
    reply = engine.handle_command(TgCommand("1", 1, "/ask buy?", 1))
    assert "not staging buy" in reply
    assert engine.desk.pending is None
    assert engine.halted is False
    engine.stop()


def test_parse_advice_json() -> None:
    adv = parse_advice('Stay out.\n{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"range"}')
    assert adv.action == "hold"
    assert "Stay out" in adv.text


def test_live_arm_from_chat(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    assert "live=off" in engine.handle_command(TgCommand("1", 1, "/live", 1))
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/live on", 2))
    on = engine.handle_command(TgCommand("1", 1, "/live on I-ACCEPT-RISK", 3))
    assert "live armed" in on
    assert engine.cfg.live_accepted
    engine.handle_command(TgCommand("1", 1, "/live off", 4))
    assert not engine.cfg.live_accepted
    engine.stop()


def test_live_arm_does_not_survive_restart(tmp_path) -> None:
    """Arming is PER PROCESS (issue #7). A journal replay must not arm money.

    This test asserted the opposite until 1.1.3. That assertion was the defect:
    a crash loop kept real money armed from a /live on typed weeks earlier.
    """
    first = _engine(tmp_path)
    first.start()
    first.handle_command(TgCommand("1", 1, "/live on I-ACCEPT-RISK", 1))
    first.stop()
    second = _engine(tmp_path)
    second.start()
    assert second.cfg.live_accepted is False
    assert second.desk.live_expired is True
    status = second.handle_command(TgCommand("1", 1, "/live", 1))
    assert "live=off" in status
    assert "per process" in status
    second.stop()


def test_approve_always_sends_without_confirm(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    assert "approve=off" in engine.handle_command(TgCommand("1", 1, "/approve", 1))
    on = engine.handle_command(TgCommand("1", 1, "/approve always", 2))
    assert "approve always" in on
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 3))
    assert reply.startswith("sent buy")
    assert engine.broker.positions()
    assert engine.desk.pending is None
    engine.handle_command(TgCommand("1", 1, "/close all", 4))
    engine.handle_command(TgCommand("1", 1, "/approve off", 5))
    staged = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 6))
    assert "/confirm" in staged
    assert not engine.broker.positions()
    engine.stop()


def test_approve_always_survives_restart(tmp_path) -> None:
    first = _engine(tmp_path)
    first.start()
    first.handle_command(TgCommand("1", 1, "/approve always", 1))
    first.stop()
    second = _engine(tmp_path)
    second.start()
    assert second.desk.approve_always
    reply = second.handle_command(TgCommand("1", 1, "/buy EURUSD", 2))
    assert reply.startswith("sent buy")
    second.stop()


def test_approve_always_still_refuses_halt(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/approve always", 1))
    engine.handle_command(TgCommand("1", 1, "/halt", 2))
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 3))
    assert "refused" in reply
    assert not engine.broker.positions()
    engine.stop()


def test_auto_toggle(tmp_path) -> None:
    engine = _engine(tmp_path)
    assert engine.cfg.strategy.auto is False
    assert "auto on" in engine.handle_command(TgCommand("1", 1, "/auto on", 1))
    assert engine.cfg.strategy.auto is True


def test_trail_toggle_does_not_enable_auto(tmp_path) -> None:
    engine = _engine(tmp_path)
    assert engine.cfg.strategy.trail is False
    assert engine.cfg.strategy.auto is False
    assert "trail=off" in engine.handle_command(TgCommand("1", 1, "/trail", 1))
    assert "trail on" in engine.handle_command(TgCommand("1", 1, "/trail on", 2))
    assert engine.cfg.strategy.trail is True
    assert engine.cfg.strategy.auto is False
    assert "trail=on" in engine.handle_command(TgCommand("1", 1, "/trail", 3))
    assert "trail off" in engine.handle_command(TgCommand("1", 1, "/trail off", 4))
    assert engine.cfg.strategy.trail is False
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/trail nope", 5))


def test_trail_on_manages_without_auto_entries(tmp_path) -> None:
    from straightedge.models import Bar

    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    sl = spec.normalize_price(tick.ask - 0.005)
    tp = spec.normalize_price(tick.ask + 0.100)
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD sl={sl} tp={tp}", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    old_sl = pos.sl
    last = engine.broker.rates("EURUSD", engine.cfg.strategy.timeframe_id, 1)[-1]
    winner = spec.normalize_price(pos.price_open + 0.020)
    engine.broker.seed_bars(
        "EURUSD",
        engine.broker.rates("EURUSD", engine.cfg.strategy.timeframe_id, 200)
        + [
            Bar(
                time=last.time + 3600,
                open=last.close,
                high=max(last.close, winner),
                low=min(last.close, winner),
                close=winner,
            )
        ],
    )
    engine.step_all()
    assert engine.cfg.strategy.auto is False
    assert engine.last_bar_time == {}
    still = engine.broker.positions()[0]
    assert abs(still.sl - old_sl) < spec.point
    assert "trail on" in engine.handle_command(TgCommand("1", 1, "/trail on", 3))
    assert engine.cfg.strategy.auto is False
    engine.step_all()
    assert engine.last_bar_time == {}
    moved = engine.broker.positions()[0]
    assert moved.sl > old_sl
    assert len(engine.broker.positions()) == 1
    engine.stop()


def test_cancel_and_help(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert "cancelled" in engine.handle_command(TgCommand("1", 1, "/cancel", 2))
    assert "nothing to cancel" in engine.handle_command(TgCommand("1", 1, "/cancel", 3))
    assert "/buy" in engine.handle_command(TgCommand("1", 1, "/help", 4))
    engine.stop()


def test_close_ticket_and_stops(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    assert "sl #" in engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {pos.sl}", 3))
    assert "closed" in engine.handle_command(TgCommand("1", 1, f"/close {pos.ticket}", 4))
    engine.stop()


def test_model_and_missing_ask(tmp_path) -> None:
    engine = _engine(tmp_path)
    assert "no AI key" in engine.handle_command(TgCommand("1", 1, "/ask hi", 1))
    payload = {"content": [{"type": "text", "text": "Hold.\n{\"action\":\"hold\",\"symbol\":null,\"sl\":null,\"tp\":null,\"summary\":\"x\"}"}]}
    engine2 = _engine(tmp_path, llm=FakeLlm(payload))
    engine2.cfg.advice.provider = "claude"
    engine2.advisor.cfg.provider = "claude"
    engine2.advisor.cfg.claude_key = "x"
    reply = engine2.handle_command(TgCommand("1", 1, "/ask hold?", 1))
    assert "Hold" in reply
    assert "provider=claude" in engine2.handle_command(TgCommand("1", 1, "/model claude", 2))


def test_advice_context_includes_risk_and_orders(tmp_path) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Hold.\n"
                        '{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
                    )
                }
            }
        ]
    }
    llm = FakeLlm(payload)
    engine = _engine(tmp_path, llm=llm)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/ask what is risk?", 1))
    blob = str(llm.sent[0][1])
    assert "daily_loss=" in blob
    assert "no pending orders" in blob or "PENDING" in blob or "pending" in blob.lower()
    assert "no open positions" in blob or "#" in blob
    assert "bid=" in blob
    assert "approve always" in blob.lower()
    assert "/confirm" in blob.lower()
    engine.stop()


def test_parse_advice_bad_json() -> None:
    adv = parse_advice("just text {not json}")
    assert adv.action == "hold"
    assert "just text" in adv.text


def test_parse_advice_limit_stop_ticket() -> None:
    adv = parse_advice(
        'Join.\n{"action":"buy","symbol":"EURUSD","sl":1.07,"tp":1.09,'
        '"limit":1.08,"stop":null,"ticket":null,"summary":"limit long"}'
    )
    assert adv.action == "buy"
    assert adv.limit == 1.08
    assert adv.stop is None
    assert adv.ticket is None
    close = parse_advice(
        'Out.\n{"action":"close","symbol":"EURUSD","sl":null,"tp":null,'
        '"limit":null,"stop":null,"ticket":42,"summary":"flatten"}'
    )
    assert close.action == "close"
    assert close.ticket == 42
    stop = parse_advice(
        'Break.\n{"action":"sell","symbol":"EURUSD","sl":1.10,"tp":1.07,'
        '"limit":null,"stop":1.09,"ticket":null,"summary":"stop short"}'
    )
    assert stop.stop == 1.09
    assert stop.limit is None


def test_symbols_list_add_remove(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    listed = engine.handle_command(TgCommand("1", 1, "/symbols", 1))
    assert "EURUSD" in listed
    assert listed == engine.handle_command(TgCommand("1", 1, "/symbols list", 2))
    added = engine.handle_command(TgCommand("1", 1, "/symbols add nzdusd", 3))
    assert "added NZDUSD" in added
    assert "NZDUSD" in engine.cfg.symbols
    assert "already" in engine.handle_command(TgCommand("1", 1, "/symbols add NZDUSD", 4))
    assert "NZDUSD" in engine.handle_command(TgCommand("1", 1, "/quote", 5))
    removed = engine.handle_command(TgCommand("1", 1, "/symbols remove NZDUSD", 6))
    assert "removed NZDUSD" in removed
    assert "NZDUSD" not in engine.cfg.symbols
    assert "not in book" in engine.handle_command(TgCommand("1", 1, "/symbols remove NZDUSD", 7))
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/symbols nope", 8))
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/symbols add", 9))
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/symbols remove", 17))
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 10))
    engine.handle_command(TgCommand("1", 1, "/confirm", 11))
    assert "open positions" in engine.handle_command(
        TgCommand("1", 1, "/symbols remove EURUSD", 12)
    )
    pos = engine.broker.positions()[0]
    engine.handle_command(TgCommand("1", 1, f"/close {pos.ticket}", 13))
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD limit={limit}", 14))
    engine.handle_command(TgCommand("1", 1, "/confirm", 15))
    assert "working orders" in engine.handle_command(
        TgCommand("1", 1, "/symbols remove EURUSD", 16)
    )
    engine.stop()


def test_symbols_cannot_drop_last(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.cfg.symbols = ["EURUSD"]
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/symbols remove EURUSD", 1))
    assert "last symbol" in reply
    assert engine.cfg.symbols == ["EURUSD"]
    engine.stop()


def test_recap_command(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    text = engine.handle_command(TgCommand("1", 1, "/recap", 1))
    assert text.startswith("RECAP")
    assert "equity=" in text
    assert "day_start=" in text
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 2))
    engine.handle_command(TgCommand("1", 1, "/confirm", 3))
    text2 = engine.handle_command(TgCommand("1", 1, "/recap", 4))
    assert "open" in text2 or "start" in text2
    engine.stop()


def test_quote_usage(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    book = engine.handle_command(TgCommand("1", 1, "/quote", 1))
    assert "EURUSD" in book
    assert "bid=" in book
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/close", 2))
    assert "unknown" in engine.handle_command(TgCommand("1", 1, "/nope", 3))
    engine.stop()


def test_risk_and_trail(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    risk = engine.handle_command(TgCommand("1", 1, "/risk", 1))
    assert "risk_pct=" in risk
    assert "daily_loss=" in risk
    assert "trail=off" in engine.handle_command(TgCommand("1", 1, "/trail", 2))
    assert "no such ticket" in engine.handle_command(TgCommand("1", 1, "/trail 999", 3))
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/trail nope", 7))
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 4))
    engine.handle_command(TgCommand("1", 1, "/confirm", 5))
    pos = engine.broker.positions()[0]
    reply = engine.handle_command(TgCommand("1", 1, f"/trail {pos.ticket}", 6))
    assert reply.startswith("trail #")
    engine.stop()


def test_confirm_after_halt_does_not_open(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/halt", 2))
    reply = engine.handle_command(TgCommand("1", 1, "/confirm", 3))
    assert "sent" not in reply
    assert not engine.broker.positions()
    engine.stop()


def test_stage_refuses_overwrite(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    first = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert "confirm buy EURUSD" in first
    second = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 2))
    assert "pending buy EURUSD" in second
    assert "/cancel" in second
    engine.stop()


def test_ask_http_error_does_not_kill(tmp_path) -> None:
    class Boom:
        def post_json(self, url, payload, timeout=10.0, headers=None):
            raise RuntimeError("grok empty")

    engine = _engine(tmp_path, llm=Boom())
    reply = engine.handle_command(TgCommand("1", 1, "should I buy?", 1))
    assert "grok empty" in reply


def test_buy_wrong_side_sl_refused(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    reply = engine.handle_command(TgCommand("1", 1, f"/buy EURUSD sl={tick.ask + 0.01} tp={tick.ask + 0.02}", 1))
    assert "sl < entry < tp" in reply
    assert engine.desk.pending is None
    engine.stop()


def test_partial_close_and_history(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    opened = pos.volume
    half = round(opened / 2, 2)
    reply = engine.handle_command(TgCommand("1", 1, f"/close {pos.ticket} {half}", 3))
    assert reply.startswith("closed")
    left = engine.broker.positions()
    assert len(left) == 1
    assert left[0].volume == round(opened - half, 8)
    hist = engine.handle_command(TgCommand("1", 1, "/history", 4))
    assert "open" in hist or "close" in hist
    engine.stop()


def test_scale_out_tp_partial_close_on_hit(tmp_path) -> None:
    from straightedge.models import Bar

    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    sl = spec.normalize_price(tick.ask - 0.005)
    runner = spec.normalize_price(tick.ask + 0.100)
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD sl={sl} tp={runner}", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    opened = pos.volume
    half = round(opened / 2, 2)
    assert half >= spec.volume_min
    scale_px = spec.normalize_price(pos.price_open + 0.020)
    reply = engine.handle_command(
        TgCommand("1", 1, f"/tp {pos.ticket} {scale_px} {half}", 3)
    )
    assert f"tp #{pos.ticket}" in reply
    assert f"vol={half}" in reply
    still = engine.broker.positions()[0]
    assert abs(still.volume - opened) < 1e-12
    assert abs(still.tp - runner) < spec.point
    listed = engine.handle_command(TgCommand("1", 1, "/positions", 4))
    assert "scale=" in listed
    last = engine.broker.rates("EURUSD", engine.cfg.strategy.timeframe_id, 1)[-1]
    engine.broker.seed_bars(
        "EURUSD",
        engine.broker.rates("EURUSD", engine.cfg.strategy.timeframe_id, 200)
        + [
            Bar(
                time=last.time + 3600,
                open=last.close,
                high=scale_px + 0.001,
                low=min(last.close, scale_px),
                close=scale_px + 0.001,
            )
        ],
    )
    engine.step_all()
    left = engine.broker.positions()
    assert len(left) == 1
    assert abs(left[0].volume - round(opened - half, 8)) < 1e-9
    assert pos.ticket not in engine._scale_outs
    engine.stop()


def test_scale_out_halt_and_geometry(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    sl = spec.normalize_price(tick.ask - 0.005)
    tp = spec.normalize_price(tick.ask + 0.100)
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD sl={sl} tp={tp}", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    half = round(pos.volume / 2, 2)
    below = spec.normalize_price(pos.price_open - 0.001)
    assert "above entry" in engine.handle_command(
        TgCommand("1", 1, f"/tp {pos.ticket} {below} {half}", 3)
    )
    engine.risk.write_halt_file("operator")
    refused = engine.handle_command(
        TgCommand("1", 1, f"/tp {pos.ticket} {tp} {half}", 4)
    )
    assert "refused" in refused
    assert pos.ticket not in engine._scale_outs
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/tp", 5))
    engine.stop()


def test_be_missing_ticket_and_winner(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    assert "no such ticket" in engine.handle_command(TgCommand("1", 1, "/be 999", 1))
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 2))
    engine.handle_command(TgCommand("1", 1, "/confirm", 3))
    pos = engine.broker.positions()[0]
    last = engine.broker.rates("EURUSD", engine.cfg.strategy.timeframe_id, 1)[-1]
    from straightedge.models import Bar

    engine.broker.seed_bars(
        "EURUSD",
        engine.broker.rates("EURUSD", engine.cfg.strategy.timeframe_id, 200)
        + [
            Bar(
                time=last.time + 3600,
                open=last.close,
                high=last.close + 0.05,
                low=last.close,
                close=last.close + 0.04,
            )
        ],
    )
    reply = engine.handle_command(TgCommand("1", 1, f"/be {pos.ticket}", 4))
    assert reply.startswith("be #")
    updated = engine.broker.positions()[0]
    assert abs(updated.sl - updated.price_open) < 1e-9
    engine.stop()


def test_ask_stages_limit_then_confirm_places_order(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        llm=FakeLlm({"choices": [{"message": {"content": "x"}}]}),
    )
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl = spec.normalize_price(limit - 0.005)
    tp = spec.normalize_price(limit + 0.010)
    engine.advisor.transport.payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Limit long.\n"
                        f'{{"action":"buy","symbol":"EURUSD","sl":{sl},"tp":{tp},'
                        f'"limit":{limit},"stop":null,"ticket":null,"summary":"bid"}}'
                    )
                }
            }
        ]
    }
    reply = engine.handle_command(TgCommand("1", 1, "/ask buy a limit?", 1))
    assert "confirm buy EURUSD" in reply
    assert "limit" in reply
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert sent.startswith("sent buy")
    assert engine.broker.orders()
    assert not engine.broker.positions()
    engine.stop()


def test_ask_close_without_ticket_hints(tmp_path) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Out.\n"
                        '{"action":"close","symbol":"EURUSD","sl":null,"tp":null,'
                        '"limit":null,"stop":null,"ticket":null,"summary":"flatten"}'
                    )
                }
            }
        ]
    }
    engine = _engine(tmp_path, llm=FakeLlm(payload))
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/ask flatten euro?", 1))
    assert "/close EURUSD" in reply
    assert engine.desk.pending is None
    engine.stop()


def test_ask_limit_and_stop_refused(tmp_path) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Both.\n"
                        '{"action":"buy","symbol":"EURUSD","sl":null,"tp":null,'
                        '"limit":1.08,"stop":1.09,"ticket":null,"summary":"xor"}'
                    )
                }
            }
        ]
    }
    engine = _engine(tmp_path, llm=FakeLlm(payload))
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/ask both?", 1))
    assert "could not stage" in reply
    assert "not both" in reply
    assert engine.desk.pending is None
    engine.stop()


def test_ask_stages_close_ticket_then_confirm(tmp_path) -> None:
    engine = _engine(
        tmp_path,
        llm=FakeLlm({"choices": [{"message": {"content": "x"}}]}),
    )
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    engine.advisor.transport.payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Close it.\n"
                        f'{{"action":"close","symbol":"EURUSD","sl":null,"tp":null,'
                        f'"limit":null,"stop":null,"ticket":{pos.ticket},"summary":"out"}}'
                    )
                }
            }
        ]
    }
    reply = engine.handle_command(TgCommand("1", 1, "/ask flatten that?", 3))
    assert f"confirm close #{pos.ticket}" in reply
    assert engine.broker.positions()
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 4))
    assert sent.startswith(f"sent close #{pos.ticket}")
    assert not engine.broker.positions()
    engine.stop()


def test_advisor_memory_includes_prior_turn(tmp_path) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Noted.\n"
                        '{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
                    )
                }
            }
        ]
    }
    llm = FakeLlm(payload)
    engine = _engine(tmp_path, llm=llm)
    engine.handle_command(TgCommand("1", 1, "/ask first turn", 1))
    engine.handle_command(TgCommand("1", 1, "/ask second turn", 2))
    assert len(llm.sent) == 2
    second_msgs = llm.sent[1][1]["messages"]
    blob = str(second_msgs)
    assert "first turn" in blob


def test_advisor_memory_survives_restart(tmp_path) -> None:
    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Noted.\n"
                        '{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
                    )
                }
            }
        ]
    }
    first = _engine(tmp_path, llm=FakeLlm(payload))
    first.start()
    first.handle_command(TgCommand("1", 1, "/ask remember the EURUSD plan", 1))
    path = first.advisor.persist_path
    assert path is not None and path.exists()
    assert_owner_mode(path)
    first.stop()
    second = _engine(tmp_path, llm=FakeLlm(payload))
    second.start()
    second.handle_command(TgCommand("1", 1, "/ask what did I say", 2))
    blob = str(second.advisor.transport.sent[-1][1]["messages"])
    assert "remember the EURUSD plan" in blob
    second.stop()


def test_ask_sends_seen_ack(tmp_path) -> None:
    class Tg:
        enabled = True

        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, text: str) -> bool:
            self.sent.append(text)
            return True

    payload = {
        "choices": [
            {
                "message": {
                    "content": (
                        "Hold.\n"
                        '{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
                    )
                }
            }
        ]
    }
    engine = _engine(tmp_path, llm=FakeLlm(payload))
    tg = Tg()
    engine.telegram = tg
    engine.handle_command(TgCommand("1", 1, "/ask ping", 1))
    assert tg.sent and tg.sent[0] == "seen. working..."


def test_computer_ask_posts_session(tmp_path) -> None:
    payload = {
        "text": (
            "Hold.\n"
            '{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
        )
    }
    llm = FakeLlm(payload)
    engine = _engine(tmp_path, llm=llm)
    engine.advisor.cfg.provider = "computer"
    engine.advisor.cfg.computer_url = "https://example.test/ask"
    engine.advisor.cfg.computer_token = "tok"
    engine.handle_command(TgCommand("42", 7, "/ask remember copper", 1))
    url, body = llm.sent[-1]
    assert url == "https://example.test/ask"
    assert body["session"] == "42"
    assert body["question"] == "remember copper"
    assert "context" in body
    assert body["history"] == []


# The agent validates `session` before it addresses a Durable Object: one string,
# 1 to 64 characters, letters, digits, dot, underscore and hyphen only. That rule
# lives in `agent/src/session.ts` and is enforced by `agent/test/session.test.ts`.
# The two tests below are the CALLER half of the same contract. If what this
# client posts ever stops matching the rule, the desk loses its advice path, and
# it should fail here rather than at the agent.
AGENT_SESSION_RULE = re.compile(r"^[A-Za-z0-9._-]{1,64}\Z")


def _computer_advisor(tmp_path):
    payload = {
        "text": (
            "Hold.\n"
            '{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
        )
    }
    llm = FakeLlm(payload)
    engine = _engine(tmp_path, llm=llm)
    engine.advisor.cfg.provider = "computer"
    engine.advisor.cfg.computer_url = "https://example.test/ask"
    engine.advisor.cfg.computer_token = "tok"
    return engine, llm


def test_computer_session_matches_the_agent_rule(tmp_path) -> None:
    engine, llm = _computer_advisor(tmp_path)
    for chat_id in ("42", "-1001234567890", "0", "7" * 64):
        engine.handle_command(TgCommand(chat_id, 7, "/ask remember copper", 1))
        _url, body = llm.sent[-1]
        assert body["session"] == chat_id
        assert AGENT_SESSION_RULE.match(body["session"]), chat_id


def test_computer_session_never_posts_an_empty_key(tmp_path) -> None:
    engine, llm = _computer_advisor(tmp_path)
    engine.advisor.ask("q", "ctx", session="", history=[])
    _url, body = llm.sent[-1]
    assert body["session"] == "default"
    assert AGENT_SESSION_RULE.match(body["session"])


def test_computer_ask_posts_journal_history(tmp_path) -> None:
    payload = {
        "text": (
            "Hold.\n"
            '{"action":"hold","symbol":null,"sl":null,"tp":null,"summary":"x"}'
        )
    }
    llm = FakeLlm(payload)
    engine = _engine(tmp_path, llm=llm)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    engine.advisor.cfg.provider = "computer"
    engine.advisor.cfg.computer_url = "https://example.test/ask"
    engine.advisor.cfg.computer_token = "tok"
    engine.handle_command(TgCommand("42", 7, "/ask remember copper", 3))
    url, body = llm.sent[-1]
    assert url == "https://example.test/ask"
    assert isinstance(body["history"], list)
    assert body["history"]
    events = [r.get("event") for r in body["history"] if isinstance(r, dict)]
    assert "open" in events
    # advice_turn closes the turn, so it is written AFTER the model call: the
    # journal now holds exactly one record more than the history that was
    # posted. Comparing the two as equal was only true while the advice path
    # wrote nothing at all (#29).
    after = engine.advice_history()
    assert after[-1]["event"] == "advice_turn"
    assert after[:-1] == body["history"]
    engine.stop()


def _fail_send(broker, *, opens: bool = False, closes: bool = False, sltp: bool = False) -> None:
    if opens:
        broker.market = lambda order: OrderResult(  # type: ignore[method-assign]
            retcode=TRADE_RETCODE_INVALID, comment="nope"
        )
        broker.check_market = lambda order: OrderResult(  # type: ignore[method-assign]
            retcode=TRADE_RETCODE_DONE, comment="ok"
        )
    if closes:
        broker.close_position = lambda *a, **k: OrderResult(  # type: ignore[method-assign]
            retcode=TRADE_RETCODE_INVALID, comment="nope"
        )
    if sltp:
        broker.modify_position = lambda *a, **k: OrderResult(  # type: ignore[method-assign]
            retcode=TRADE_RETCODE_INVALID_STOPS, comment="stops_level"
        )


def test_confirm_repreview_halt_file_refuses(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    staged = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert "confirm buy EURUSD" in staged
    engine.risk.write_halt_file("operator")
    reply = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert not reply.startswith("sent")
    assert "refused" in reply
    assert not engine.broker.positions()
    engine.stop()


def test_confirm_send_failure_not_hardcoded_success(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    _fail_send(engine.broker, opens=True)
    reply = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert not reply.startswith("sent")
    assert "fail" in reply.lower() or "retcode" in reply.lower()
    assert not engine.broker.positions()
    engine.stop()


def test_sl_tp_success_only_when_applied(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert sent.startswith("sent ")
    pos = engine.broker.positions()[0]
    spec = engine.broker.symbol(pos.symbol)
    new_sl = spec.normalize_price(pos.price_open - abs(pos.price_open - pos.sl) * 0.5)
    ok = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {new_sl}", 3))
    assert "sl #" in ok
    assert abs(engine.broker.positions()[0].sl - new_sl) < spec.point
    other_sl = spec.normalize_price(pos.price_open - abs(pos.price_open - pos.sl) * 0.35)
    other_tp = spec.normalize_price(pos.price_open + abs(pos.tp - pos.price_open) * 0.7)
    _fail_send(engine.broker, sltp=True)
    bad_sl = engine.handle_command(TgCommand("1", 1, f"/sl {pos.ticket} {other_sl}", 4))
    assert "sl #" not in bad_sl
    assert "fail" in bad_sl.lower()
    bad_tp = engine.handle_command(TgCommand("1", 1, f"/tp {pos.ticket} {other_tp}", 5))
    assert "tp #" not in bad_tp
    assert "fail" in bad_tp.lower()
    engine.stop()


def test_reverse_confirm_flips_side(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    assert pos.side.value == "buy"
    risk_dist = abs(pos.price_open - pos.sl)
    reward_dist = abs(pos.tp - pos.price_open)
    reply = engine.handle_command(TgCommand("1", 1, f"/reverse {pos.ticket}", 3))
    assert f"confirm reverse #{pos.ticket} sell EURUSD" in reply
    assert "/confirm" in reply
    assert engine.broker.positions()[0].ticket == pos.ticket
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 4))
    assert sent.startswith(f"sent reverse #{pos.ticket} sell")
    rows = engine.broker.positions()
    assert len(rows) == 1
    flipped = rows[0]
    assert flipped.side.value == "sell"
    assert flipped.ticket != pos.ticket
    spec = engine.broker.symbol("EURUSD")
    assert abs(abs(flipped.price_open - flipped.sl) - risk_dist) < spec.point * 2
    assert abs(abs(flipped.tp - flipped.price_open) - reward_dist) < spec.point * 2
    assert flipped.tp < flipped.price_open < flipped.sl
    engine.stop()


def test_reverse_sell_to_buy(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/sell EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    assert pos.side.value == "sell"
    reply = engine.handle_command(TgCommand("1", 1, f"/reverse {pos.ticket}", 3))
    assert f"confirm reverse #{pos.ticket} buy EURUSD" in reply
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 4))
    assert sent.startswith(f"sent reverse #{pos.ticket} buy")
    flipped = engine.broker.positions()[0]
    assert flipped.side.value == "buy"
    assert flipped.sl < flipped.price_open < flipped.tp
    engine.stop()


def test_reverse_refuses_halt_and_usage(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    staged = engine.handle_command(TgCommand("1", 1, f"/reverse {pos.ticket}", 3))
    assert "confirm reverse" in staged
    blocked = engine.handle_command(TgCommand("1", 1, f"/reverse {pos.ticket}", 4))
    assert "pending" in blocked
    engine.handle_command(TgCommand("1", 1, "/cancel", 5))
    engine.risk.write_halt_file("operator")
    halted = engine.handle_command(TgCommand("1", 1, f"/reverse {pos.ticket}", 6))
    assert "refused" in halted
    assert engine.broker.positions()
    engine.risk.clear_operator_halt()
    engine.handle_command(TgCommand("1", 1, f"/reverse {pos.ticket}", 7))
    engine.risk.write_halt_file("operator")
    confirm_halt = engine.handle_command(TgCommand("1", 1, "/confirm", 8))
    assert "refused" in confirm_halt
    assert engine.broker.positions()[0].ticket == pos.ticket
    engine.risk.clear_operator_halt()
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/reverse", 9))
    assert "no such ticket" in engine.handle_command(TgCommand("1", 1, "/reverse 999", 10))
    engine.stop()


def test_reverse_pending_order_refused(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl = spec.normalize_price(limit - 0.005)
    tp = spec.normalize_price(limit + 0.010)
    engine.handle_command(
        TgCommand("1", 1, f"/buy EURUSD limit={limit} sl={sl} tp={tp}", 1)
    )
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    order = engine.broker.orders()[0]
    reply = engine.handle_command(TgCommand("1", 1, f"/reverse {order.ticket}", 3))
    assert "open positions" in reply
    assert engine.broker.orders()
    engine.stop()


def _hedge(engine, *, buy_vol=0.02, sell_vol=0.02, symbol="EURUSD"):
    spec = engine.broker.symbol(symbol)
    tick = engine.broker.tick(symbol)
    magic = engine.cfg.risk.magic
    buy = engine.broker.order_send(
        {
            "action": TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": buy_vol,
            "type": ORDER_TYPE_BUY,
            "sl": spec.normalize_price(tick.ask - 0.005),
            "tp": spec.normalize_price(tick.ask + 0.010),
            "magic": magic,
        }
    )
    sell = engine.broker.order_send(
        {
            "action": TRADE_ACTION_DEAL,
            "symbol": symbol,
            "volume": sell_vol,
            "type": ORDER_TYPE_SELL,
            "sl": spec.normalize_price(tick.bid + 0.005),
            "tp": spec.normalize_price(tick.bid - 0.010),
            "magic": magic,
        }
    )
    assert buy.ok and sell.ok
    return buy.order, sell.order


def test_closeby_offsets_opposite(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    buy, sell = _hedge(engine)
    reply = engine.handle_command(TgCommand("1", 1, f"/closeby {buy} {sell}", 1))
    assert reply == f"closed #{buy} by #{sell}"
    assert not engine.broker.positions()
    engine.stop()


def test_closeby_keeps_remainder(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    buy, sell = _hedge(engine, buy_vol=0.03, sell_vol=0.01)
    reply = engine.handle_command(TgCommand("1", 1, f"/closeby {buy} {sell}", 1))
    assert reply.startswith("closed #")
    rows = engine.broker.positions()
    assert len(rows) == 1
    assert rows[0].ticket == buy
    assert abs(rows[0].volume - 0.02) < 1e-12
    engine.stop()


def test_closeby_refuses_bad_pairs(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    buy, sell = _hedge(engine)
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/closeby", 1))
    assert "tickets must differ" in engine.handle_command(
        TgCommand("1", 1, f"/closeby {buy} {buy}", 2)
    )
    assert "no such ticket" in engine.handle_command(TgCommand("1", 1, "/closeby 1 999", 3))
    extra = engine.broker.order_send(
        {
            "action": TRADE_ACTION_DEAL,
            "symbol": "EURUSD",
            "volume": 0.01,
            "type": ORDER_TYPE_BUY,
            "sl": engine.broker.symbol("EURUSD").normalize_price(
                engine.broker.tick("EURUSD").ask - 0.005
            ),
            "tp": engine.broker.symbol("EURUSD").normalize_price(
                engine.broker.tick("EURUSD").ask + 0.010
            ),
            "magic": engine.cfg.risk.magic,
        }
    )
    assert extra.ok
    assert "sides must be opposite" in engine.handle_command(
        TgCommand("1", 1, f"/closeby {buy} {extra.order}", 4)
    )
    gbp = engine.broker.order_send(
        {
            "action": TRADE_ACTION_DEAL,
            "symbol": "GBPUSD",
            "volume": 0.01,
            "type": ORDER_TYPE_SELL,
            "sl": engine.broker.symbol("GBPUSD").normalize_price(
                engine.broker.tick("GBPUSD").bid + 0.005
            ),
            "tp": engine.broker.symbol("GBPUSD").normalize_price(
                engine.broker.tick("GBPUSD").bid - 0.010
            ),
            "magic": engine.cfg.risk.magic,
        }
    )
    assert gbp.ok
    assert "symbols must match" in engine.handle_command(
        TgCommand("1", 1, f"/closeby {buy} {gbp.order}", 5)
    )
    tiny_buy, tiny_sell = _hedge(engine, buy_vol=0.02, sell_vol=0.011)
    assert "remainder below volume_min" in engine.handle_command(
        TgCommand("1", 1, f"/closeby {tiny_buy} {tiny_sell}", 6)
    )
    engine.broker.close_by = lambda *a, **k: OrderResult(  # type: ignore[method-assign]
        retcode=TRADE_RETCODE_INVALID, comment="nope"
    )
    failed = engine.handle_command(TgCommand("1", 1, f"/closeby {buy} {sell}", 7))
    assert "closeby failed" in failed
    engine.stop()


def test_close_success_only_when_applied(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    _fail_send(engine.broker, closes=True)
    reply = engine.handle_command(TgCommand("1", 1, f"/close {pos.ticket}", 3))
    assert reply != "closed"
    assert "fail" in reply.lower() or "retcode" in reply.lower()
    assert engine.broker.positions()
    engine.stop()


def test_staged_confirm_survives_restart(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert "confirm buy EURUSD" in reply
    pending = engine.desk.pending
    assert pending is not None
    assert pending.signal is not None
    vol = pending.volume
    expires = pending.expires_at
    kind = pending.signal.kind
    symbol = pending.signal.symbol
    sl, tp, entry = pending.signal.sl, pending.signal.tp, pending.signal.entry
    rec = engine.journal.last_event("confirm_stage")
    assert rec is not None
    assert rec["event"] == "confirm_stage"
    assert rec["volume"] == vol
    engine.stop()

    engine2 = _engine(tmp_path)
    assert engine2.desk.pending is None
    engine2.start()
    restored = engine2.desk.pending
    assert restored is not None
    assert restored.signal is not None
    assert restored.signal.kind == kind
    assert restored.signal.symbol == symbol
    assert restored.signal.entry == entry
    assert restored.signal.sl == sl
    assert restored.signal.tp == tp
    assert restored.volume == vol
    assert restored.expires_at == expires
    assert restored.source == "telegram"
    reply = engine2.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert reply.startswith("sent buy")
    assert engine2.broker.positions()
    engine2.stop()


def test_cancelled_confirm_does_not_restore(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert "cancelled" in engine.handle_command(TgCommand("1", 1, "/cancel", 2))
    last = engine.journal.last_event("confirm_stage", "confirm_cancel", "confirm_sent")
    assert last is not None
    assert last["event"] == "confirm_cancel"
    engine.stop()
    engine2 = _engine(tmp_path)
    engine2.start()
    assert engine2.desk.pending is None
    engine2.stop()


def test_sent_confirm_does_not_restore(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    reply = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert reply.startswith("sent buy")
    last = engine.journal.last_event("confirm_stage", "confirm_cancel", "confirm_sent")
    assert last is not None
    assert last["event"] == "confirm_sent"
    engine.stop()
    engine2 = _engine(tmp_path)
    engine2.start()
    assert engine2.desk.pending is None
    engine2.stop()


def test_expired_staged_confirm_does_not_restore(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    rec = engine.journal.last_event("confirm_stage")
    assert rec is not None
    engine.journal.write(
        "confirm_stage",
        signal=rec["signal"],
        volume=rec["volume"],
        source=rec["source"],
        expires_at=1.0,
        close_ticket=rec.get("close_ticket"),
    )
    engine.stop()
    engine2 = _engine(tmp_path)
    engine2.start()
    assert engine2.desk.pending is None
    engine2.stop()


def test_halted_confirm_does_not_restore(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/halt", 2))
    assert engine.desk.pending is None
    last = engine.journal.last_event("confirm_stage", "confirm_cancel", "confirm_sent")
    assert last is not None
    assert last["event"] == "confirm_cancel"
    engine.stop()
    engine2 = _engine(tmp_path)
    engine2.start()
    assert engine2.desk.pending is None
    engine2.stop()


def test_staged_limit_survives_restart(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD limit={limit}", 1))
    assert engine.desk.pending is not None
    assert engine.desk.pending.signal is not None
    assert engine.desk.pending.signal.pending_kind == "limit"
    engine.stop()
    engine2 = _engine(tmp_path)
    engine2.start()
    restored = engine2.desk.pending
    assert restored is not None
    assert restored.signal is not None
    assert restored.signal.pending_kind == "limit"
    assert restored.signal.entry == limit
    engine2.stop()


def test_confirm_stage_journal_has_no_secrets(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    rec = engine.journal.last_event("confirm_stage")
    assert rec is not None
    blob = json.dumps(rec).lower()
    for needle in ("token", "password", "api_key", "grok_key", "claude_key"):
        assert needle not in blob
    assert rec.get("signal", {}).get("symbol") == "EURUSD"
    assert "expires_at" in rec
    engine.stop()


def test_pending_from_record_close_and_garbage() -> None:
    rec = {
        "event": "confirm_stage",
        "volume": 0.1,
        "source": "advice",
        "expires_at": time.time() + 60,
        "close_ticket": 7,
        "signal": None,
    }
    pending = pending_from_record(rec)
    assert pending is not None
    assert pending.signal is None
    assert pending.close_ticket == 7
    assert pending.volume == 0.1
    assert pending_from_record({"volume": "nope"}) is None
    assert pending_from_record({}) is None
    assert pending_from_record({"kind": "buy"}) is None


def test_journal_last_event_skips_garbage(tmp_path) -> None:
    engine = _engine(tmp_path)
    path = engine.journal.path
    path.write_text("{not json}\n\n", encoding="utf-8")
    assert engine.journal.last_event("confirm_stage") is None
    engine.journal.write("open", symbol="EURUSD")
    engine.journal.write("confirm_stage", volume=0.2, source="telegram", expires_at=9.0)
    engine.journal.write("open", symbol="GBPUSD")
    last = engine.journal.last_event("confirm_stage", "confirm_cancel", "confirm_sent")
    assert last is not None
    assert last["event"] == "confirm_stage"
    assert last["volume"] == 0.2
    assert engine.journal.last_event() is None

