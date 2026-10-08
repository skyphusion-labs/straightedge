"""Every advice reply carries the not-advice line. straightedge#130.

Why this file exists
--------------------
Before this, the only "(not financial advice)" string in the tree was inside
`HELP`, served by `/start` and `/help`. Free text goes straight to `_ask`, so a
user who never typed `/help` never saw it, and the message that PROPOSES a
trade, the one they answer with `/confirm`, carried nothing.

Two things are pinned here that are easy to get wrong in opposite directions.

**It is a PREFIX, not a footer.** `telegram.send()` chunks at `CHUNK_CHARS`
(3,900) and keeps at most `MAX_SEND_CHUNKS` (3), fitting the truncation notice
INSIDE the last chunk, so everything past roughly 11,700 characters is dropped
from the END. An advice reply is a model's `advice.text` plus the staging
lines, and the model's half is unbounded, so a trailing disclaimer would be
missing on exactly the longest replies. `test_survives_truncation` is that
case, asserted against what the transport was actually handed.

**It is NOT on `/buy` or `/sell`.** Those reach `_stage` directly. The operator
typed the instrument and the direction, nothing advised them, and a not-advice
line on a trade nobody proposed trains a reader to skip the line.

Proven red: deleting `NOT_ADVICE` from the `_ask` return fails all four
positive tests; moving it from the front of the list to the back fails
`test_is_the_first_line` and `test_survives_truncation` while the other two
stay green, which is the asymmetry the prefix exists for. A gate seen only
green is not a gate.
"""

from __future__ import annotations

from datetime import datetime, timezone

from straightedge.config import AdviceConfig, BotConfig
from straightedge.broker.paper import PaperBroker
from straightedge.engine import Engine
from straightedge.llm import Advisor
from straightedge.synthetic import generate_bars
from straightedge.telegram import CHUNK_CHARS, MAX_SEND_CHUNKS, NOT_ADVICE, TelegramClient, TgCommand


class FakeLlm:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None) -> dict:
        del url, payload, timeout, headers
        return self.payload


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None) -> dict:
        del timeout, headers
        self.sent.append((url, payload))
        return {"ok": True, "result": {"message_id": len(self.sent)}}


def _grok_payload(text: str, action: str = "hold", symbol: str | None = None) -> dict:
    sym = "null" if symbol is None else f'"{symbol}"'
    return {
        "choices": [
            {
                "message": {
                    "content": (
                        f"{text}\n"
                        f'{{"action":"{action}","symbol":{sym},"sl":null,"tp":null,'
                        '"limit":null,"stop":null,"ticket":null,"summary":"s"}'
                    )
                }
            }
        ]
    }


def _engine(tmp_path, payload: dict) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    advisor = Advisor(AdviceConfig(provider="grok", grok_key="x"), transport=FakeLlm(payload))
    return Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        advisor=advisor,
        now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
    )


def test_is_the_first_line(tmp_path) -> None:
    engine = _engine(tmp_path, _grok_payload("Hold for now."))
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/ask what now?", 1))
    assert reply.splitlines()[0] == NOT_ADVICE
    assert "Hold for now." in reply, "the model text was lost, not just prefixed"
    engine.stop()


def test_on_a_staged_advice_order(tmp_path) -> None:
    """The reply a user answers with /confirm is the one that most needs it."""
    engine = _engine(tmp_path, _grok_payload("Buy it.", action="buy", symbol="EURUSD"))
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/ask buy euro?", 1))
    assert NOT_ADVICE in reply
    assert "confirm" in reply.lower(), "nothing was staged: the assertion above is weak"
    engine.stop()


def test_on_a_reply_that_refuses_to_stage(tmp_path) -> None:
    engine = _engine(tmp_path, _grok_payload("Buy it.", action="buy", symbol="EURUSD"))
    engine.start()
    engine.risk.write_halt_file("operator")
    reply = engine.handle_command(TgCommand("1", 1, "/ask buy euro?", 1))
    assert NOT_ADVICE in reply
    assert "not staging buy" in reply, "the circuit did not refuse: wrong path measured"
    engine.stop()


def test_survives_truncation(tmp_path) -> None:
    """The case a footer would fail.

    A model reply past MAX_SEND_CHUNKS * CHUNK_CHARS is truncated from the end,
    so the disclaimer has to be at the start to be delivered at all. Asserted
    on what the transport was handed, not on the string the desk built.
    """
    overflow = "Y" * (CHUNK_CHARS * MAX_SEND_CHUNKS + 5_000)
    engine = _engine(tmp_path, _grok_payload(overflow))
    transport = FakeTransport()
    engine.telegram = TelegramClient(token="t", chat_id="1", transport=transport)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/ask go long?", 1))
    # `start` notifies and `_ask` posts a "seen. working..." ack, both through
    # this same transport. Drop them: the subject here is the one send that
    # carries the reply.
    transport.sent.clear()
    assert engine.telegram.send(reply) is True

    sends = [p["text"] for url, p in transport.sent if url.endswith("/sendMessage")]
    # The fixture must actually overflow, or this test proves nothing.
    assert len(sends) == MAX_SEND_CHUNKS
    assert "truncated" in sends[-1]
    assert NOT_ADVICE in sends[0]
    # And it is not merely present somewhere: it is the first thing read.
    assert sends[0].startswith(NOT_ADVICE)
    engine.stop()


def test_not_on_an_operator_typed_order(tmp_path) -> None:
    """The deliberate exclusion. /buy was the operator's own decision."""
    engine = _engine(tmp_path, _grok_payload("Hold."))
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert "confirm" in reply.lower(), "nothing was staged: the assertion below is vacuous"
    assert NOT_ADVICE not in reply
    engine.stop()


def test_help_keeps_its_own_disclaimer() -> None:
    from straightedge.telegram import HELP

    assert "not financial advice" in HELP
