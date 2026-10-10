"""Structured refusal records on the desk and advice paths (#29).

The estate rule is to assert on a machine-readable channel, never on English
prose. Every test here reads a journal.jsonl record and compares the NAMED
reason string. Two invariants ride along:

- a refusal is journaled and never echoed back into the chat it came from,
  the precedent PR #40 set for command_rejected. That holds for a refusal with
  no named reason too, and `send_refused_unresolved` is the one of those: it
  goes through `_emit` rather than `journal.write`, which LOOKS like an
  exception to this rule and is not one, because `_format_event` returns empty
  for it and `_emit` notifies only on non-empty text. Asserted in
  `tests/test_unresolved_send_record.py` (#237), including with the event
  explicitly allowlisted, so the invariant has no unrecorded exception;
- COULD NOT MEASURE stays a different event from REFUSED. A stage that could
  not be built is not a rule saying no.
"""

import json
from datetime import datetime, timezone

from straightedge.broker.paper import PaperBroker
from straightedge.config import AdviceConfig, BotConfig
from straightedge.desk import Desk
from straightedge.engine import Engine
from straightedge.journal import Journal
from straightedge.llm import Advisor
from straightedge.models import Signal, SignalKind
from straightedge.synthetic import generate_bars
from straightedge.telegram import TelegramClient, TgCommand

QUESTION = "is the euro a buy right now"
PROSE = "Model prose body about the trend."
SUMMARY = "one line of model prose"


class FakeLlm:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def post_json(self, url, payload, timeout=10.0, headers=None) -> dict:
        del url, payload, timeout, headers
        return self.payload


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def post_json(self, url, payload, timeout=10.0, headers=None) -> dict:
        del timeout, headers
        self.sent.append((url, payload))
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": []}
        return {"ok": True, "result": {"message_id": 1}}


def _advice_payload(action="buy", symbol="EURUSD", ticket=None) -> dict:
    tail = {
        "action": action,
        "symbol": symbol,
        "sl": None,
        "tp": None,
        "limit": None,
        "stop": None,
        "ticket": ticket,
        "summary": SUMMARY,
    }
    body = PROSE + "\n" + json.dumps(tail)
    return {"choices": [{"message": {"content": body}}]}


def _engine(tmp_path, *, llm=None, telegram=None, min_rr=None) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    if min_rr is not None:
        cfg.risk.min_rr = min_rr
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
        telegram=telegram,
        now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
    )


# --- 1. the desk path: a gate that says no must name the reason -----------


def test_desk_buy_refusal_names_the_reason(tmp_path) -> None:
    engine = _engine(tmp_path, min_rr=99.0)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert reply == "refused: rr_below_min"
    rec = engine.journal.last_event("reject")
    assert rec is not None, "a desk refusal left no structured record"
    assert rec["reason"] == "rr_below_min"
    assert rec["source"] == "telegram"
    assert rec["stage"] == "stage"
    assert rec["symbol"] == "EURUSD"
    assert rec["kind"] == "buy"
    engine.stop()


def test_confirm_risk_refusal_is_a_second_stage(tmp_path) -> None:
    """Staging passed and the confirm leg refused. Different gate, same event."""
    engine = _engine(tmp_path)
    engine.start()
    assert "confirm buy EURUSD" in engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.cfg.risk.min_rr = 99.0
    reply = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert reply == "refused: rr_below_min"
    rec = engine.journal.last_event("reject")
    assert rec is not None
    assert rec["reason"] == "rr_below_min"
    assert rec["stage"] == "confirm"
    assert rec["source"] == "telegram"
    engine.stop()


def test_confirm_while_halted_names_halted(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.halted = True
    assert engine.handle_command(TgCommand("1", 1, "/confirm", 2)) == "refused: halted"
    rec = engine.journal.last_event("reject")
    assert rec is not None
    assert rec["reason"] == "halted"
    assert rec["stage"] == "confirm"
    engine.stop()


def test_reverse_refusal_names_the_stage_and_the_ticket(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    ticket = engine.broker.positions()[0].ticket
    engine.cfg.risk.min_rr = 99.0
    reply = engine.handle_command(TgCommand("1", 1, "/reverse " + str(ticket), 3))
    assert reply == "refused: rr_below_min"
    rec = engine.journal.last_event("reject")
    assert rec is not None
    assert rec["reason"] == "rr_below_min"
    assert rec["stage"] == "reverse"
    assert rec["ticket"] == ticket
    engine.stop()


def test_pending_order_blocks_a_second_stage_with_a_named_reason(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    reply = engine.handle_command(TgCommand("1", 1, "/sell EURUSD", 2))
    assert "/cancel first" in reply
    rec = engine.journal.last_event("reject")
    assert rec is not None
    assert rec["reason"] == "pending_exists"
    assert rec["stage"] == "stage"
    engine.stop()


def test_auto_path_reject_is_tagged_auto(tmp_path) -> None:
    """The one path that already journaled must stay distinguishable."""
    engine = _engine(tmp_path, min_rr=99.0)
    engine.start()
    bars = generate_bars(250, drift=0.0006, vol=0.0002, seed=7)
    engine.broker.seed_bars("EURUSD", bars)
    engine.replay_symbol("EURUSD", bars)
    rec = engine.journal.last_event("reject")
    assert rec is not None
    assert rec["reason"] == "rr_below_min"
    assert rec["source"] == "auto"
    assert rec["stage"] == "signal"
    engine.stop()


# --- 2. the advice path: same event, different source ----------------------


def test_advice_buy_refusal_is_sourced_to_advice(tmp_path) -> None:
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload()), min_rr=99.0)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/ask " + QUESTION, 1))
    assert "refused: rr_below_min" in reply
    rec = engine.journal.last_event("reject")
    assert rec is not None, "an advice refusal left no structured record"
    assert rec["reason"] == "rr_below_min"
    assert rec["source"] == "advice"
    assert rec["stage"] == "stage"
    engine.stop()


def test_advice_circuit_block_is_structured(tmp_path) -> None:
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload()))
    engine.start()
    engine.risk.write_halt_file("operator")
    reply = engine.handle_command(TgCommand("1", 1, "/ask " + QUESTION, 1))
    assert "not staging buy" in reply
    rec = engine.journal.last_event("advice_circuit_block")
    assert rec is not None
    assert rec["reason"] == "halt_file"
    assert rec["action"] == "buy"
    assert rec["symbol"] == "EURUSD"
    engine.stop()


def test_advice_turn_records_the_decision_not_the_words(tmp_path) -> None:
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload()))
    engine.start()
    engine.handle_command(TgCommand("1", 1, QUESTION, 1))
    rec = engine.journal.last_event("advice_turn")
    assert rec is not None, "the advice turn left no structured record"
    assert rec["provider"] == "grok"
    assert rec["session"] == "1"
    assert rec["action"] == "buy"
    assert rec["symbol"] == "EURUSD"
    assert rec["staged"] is True
    blob = json.dumps(rec)
    assert QUESTION not in blob, "the question reached the journal"
    assert PROSE not in blob, "the model reply reached the journal"
    assert SUMMARY not in blob, "the model summary reached the journal"
    engine.stop()


def test_advice_hold_is_recorded_as_not_staged(tmp_path) -> None:
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload(action="hold", symbol=None)))
    engine.start()
    engine.handle_command(TgCommand("1", 1, QUESTION, 1))
    rec = engine.journal.last_event("advice_turn")
    assert rec is not None
    assert rec["action"] == "hold"
    assert rec["staged"] is False
    engine.stop()


def test_advice_close_of_an_unknown_ticket_names_the_reason(tmp_path) -> None:
    payload = _advice_payload(action="close", symbol=None, ticket=9999)
    engine = _engine(tmp_path, llm=FakeLlm(payload))
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, QUESTION, 1))
    assert "no such ticket" in reply
    rec = engine.journal.last_event("reject")
    assert rec is not None
    assert rec["reason"] == "no_such_ticket"
    assert rec["stage"] == "stage_close"
    assert rec["source"] == "advice"
    assert rec["ticket"] == 9999
    engine.stop()


def test_advice_close_without_a_ticket_names_the_reason(tmp_path) -> None:
    payload = _advice_payload(action="close", symbol="EURUSD", ticket=None)
    engine = _engine(tmp_path, llm=FakeLlm(payload))
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, QUESTION, 1))
    assert "/close EURUSD" in reply
    rec = engine.journal.last_event("reject")
    assert rec is not None
    assert rec["reason"] == "close_needs_ticket"
    assert rec["stage"] == "stage_close"
    engine.stop()


# --- 3. COULD NOT MEASURE is not REFUSED -----------------------------------


def test_a_stage_that_could_not_be_built_is_not_a_reject(tmp_path) -> None:
    """GBPUSD has no bars, so there is no ATR to derive a stop from.

    Nothing was measured and no rule said no. That is a different event, and
    a test for #11 must not be able to count it as a named refusal reason.

    Was GBPJPY until #13. That symbol is outside the default book, so the
    advice whitelist now refuses it BEFORE the build is attempted and the
    reply is a named refusal rather than an unmeasured one. GBPUSD is in the
    book and still has no bars, so it reaches the path this test is about.
    """
    engine = _engine(tmp_path, llm=FakeLlm(_advice_payload(symbol="GBPUSD")))
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, QUESTION, 1))
    assert "could not stage trade" in reply
    rec = engine.journal.last_event("advice_stage_failed")
    assert rec is not None
    assert rec["measured"] is False
    assert rec["action"] == "buy"
    assert rec["symbol"] == "GBPUSD"
    assert engine.journal.last_event("reject") is None, (
        "COULD NOT MEASURE was recorded as a rule refusing"
    )
    engine.stop()


# --- 4. a refusal is never echoed back into the chat ------------------------


def test_refusal_does_not_go_through_the_broadcast_path(tmp_path) -> None:
    """The instrument watches the CALL, not the outcome.

    _format_event returns nothing for an unknown event name, so a refusal
    wrongly routed through Engine._emit would still send no message today and
    a sent-message count would stay green. Counting messages cannot go red
    here; spying on _emit can.
    """
    engine = _engine(tmp_path, min_rr=99.0)
    engine.start()
    seen: list[str] = []
    real_emit = engine._emit

    def spy(event, **fields):
        seen.append(event)
        real_emit(event, **fields)

    engine._emit = spy
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert engine.journal.last_event("reject") is not None
    assert "reject" not in seen, "the refusal was routed through the chat broadcast"
    engine.stop()


def test_refusal_sends_no_telegram_message(tmp_path) -> None:
    tr = FakeTransport()
    tg = TelegramClient(
        token="t",
        chat_id="1",
        transport=tr,
        notify_events=frozenset({"reject", "advice_circuit_block", "advice_turn"}),
    )
    engine = _engine(tmp_path, telegram=tg, min_rr=99.0)
    engine.start()
    before = len(tr.sent)
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert engine.journal.last_event("reject") is not None
    assert len(tr.sent) == before, "the refusal was broadcast into the chat"
    engine.stop()


# --- 5. with no journal the control still says it fired ---------------------


class _RefusingDecision:
    allowed = False
    reason = "spread_too_wide"
    halt = False
    volume = 0.0


class _TgCfg:
    confirm_seconds = 60


class _Cfg:
    def __init__(self) -> None:
        self.telegram = _TgCfg()


class _JournallessEngine:
    """No journal at all. A control that goes quiet here cannot be audited."""

    journal = None

    def __init__(self) -> None:
        self.cfg = _Cfg()

    def preview(self, signal, manual=True, exclude_ticket=None):
        del signal, manual, exclude_ticket
        return _RefusingDecision()


def test_refusal_reports_on_stderr_when_no_journal_is_configured(capsys) -> None:
    desk = Desk(_JournallessEngine())
    sig = Signal(
        kind=SignalKind.BUY,
        symbol="EURUSD",
        entry=1.1,
        sl=1.09,
        tp=1.13,
        atr=0.001,
    )
    assert desk._stage(sig, "telegram") == "refused: spread_too_wide"
    err = capsys.readouterr().err
    assert "reject" in err
    assert "spread_too_wide" in err


# --- 6. arming the autonomous trader is audited like arming live -------------


class _RealMoneyAccount:
    trade_mode = 2


class _RealMoneyBroker:
    def account(self):
        return _RealMoneyAccount()


class _RealMoneyEngine:
    mode = "mt5"

    def __init__(self, journal) -> None:
        self.journal = journal
        self.cfg = BotConfig()
        self.cfg.mode = "mt5"
        self.cfg.live_accepted = False
        self.broker = _RealMoneyBroker()


def test_approve_always_on_a_real_money_terminal_names_the_reason(tmp_path) -> None:
    journal = Journal(tmp_path / "j.jsonl")
    desk = Desk(_RealMoneyEngine(journal))
    reply = desk._approve("always")
    assert "I-ACCEPT-RISK" in reply
    assert desk.approve_always is False
    rec = journal.last_event("reject")
    assert rec is not None
    assert rec["reason"] == "live_not_accepted"
    assert rec["stage"] == "approve"
    assert rec["command"] == "approve"


def test_auto_arming_is_journaled_like_live_and_approve(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/auto on", 1))
    assert engine.journal.last_event("auto_on") is not None
    engine.handle_command(TgCommand("1", 1, "/auto off", 2))
    rec = engine.journal.last_event("auto_on", "auto_off")
    assert rec is not None
    assert rec["event"] == "auto_off"
    engine.stop()

