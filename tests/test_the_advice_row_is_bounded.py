"""straightedge#226: the `advice_turn` row was unbounded through `symbol`.

#216 bounded the degrade REASON and this is the same exposure one field over.
`symbol` is model-chosen on the advice path, which is the whole premise of
#197, and the row wrote it verbatim:

    5600-char ASCII symbol      ->  5838-byte row, `degraded` EMPTY
    3000-char non-ASCII symbol  ->  5754-byte row

**And #216's own `test_the_row_stays_bounded_and_carries_no_prose` passes while
that row is reachable**, because it varies `text` and `summary`, neither of
which is echoed. That is the second time a bound on this row was asserted by
FIXTURE rather than by construction, and the first was the defect #216 exists
to fix: my own pattern, recurring one field over.

So the fix is not "bound `symbol` too". It is to drive every model-reachable
field large AT ONCE and require the whole row under the documented bound, with
the field list derived from the code's own property table rather than from a
list someone maintains by hand. A field added to `ADVICE_PROPERTIES` tomorrow
is driven large by these tests without anyone remembering to add it.

## Two defects, measured

`symbol`: unbounded, above. `ticket`: `_int` did `int(_num(v))`, so

* `1e308` produced a **309 digit** integer and a 501 byte row on its own, which
  alone nearly breaches the bound;
* a 400 digit ticket string raised **`OverflowError`**, which derives from
  `ArithmeticError` and is therefore NOT in the
  `(ValueError, RuntimeError, OSError)` tuple `handle_command` catches, so it
  was an uncaught exception out of the command handler. Same family #219 found
  in `normalize_volume`, reached through a model reply instead of a command.
"""

from __future__ import annotations

import json
from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.journal import (
    RECORD_ROW_BOUND,
    RECORD_STRING_CHARS,
    clip_for_record,
)
from straightedge.llm import ADVICE_PROPERTIES, Advisor, parse_advice
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand

#: Taken from the code, not repeated here, so the documented bound and the
#: asserted bound cannot drift (straightedge#226).
ROW_BOUND = RECORD_ROW_BOUND


class FakeTransport:
    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None):
        del timeout, headers
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": []}
        if url.endswith("/sendMessage"):
            return {"ok": True, "result": {"message_id": 1}}
        return self.payloads.pop(0) if self.payloads else {}


def _claude_reply(obj: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


def _engine(tmp_path: Path, *payloads: dict, provider: str = "claude") -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.advice.provider = provider
    cfg.advice.claude_key = "k"
    cfg.advice.grok_key = "k"
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    advisor = Advisor(
        cfg.advice, transport=FakeTransport(*payloads), persist_path=tmp_path / "a.json"
    )
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), advisor=advisor)
    engine.start()
    return engine


def _turn(engine: Engine) -> dict:
    rows = [r for r in engine.journal.tail(5000) if r.get("event") == "advice_turn"]
    assert rows, "no advice turn was journalled"
    return rows[-1]


def _everything_large() -> dict:
    """A reply with EVERY schema field driven large, derived from the code.

    `ADVICE_PROPERTIES` is the table the request is built from and the one
    `_schema_violations` validates against, so a field added there is driven
    large here without anyone editing this fixture. That is the difference
    between a bound asserted by construction and one asserted by whichever
    fields the author happened to think of.
    """
    obj: dict[str, object] = {}
    for name, spec in ADVICE_PROPERTIES.items():
        types = spec.get("type")
        types = types if isinstance(types, list) else [types]
        if "string" in types:
            obj[name] = "E" * 6000
        elif "number" in types:
            obj[name] = "9" * 5000
        elif "integer" in types:
            obj[name] = "9" * 400
        else:
            obj[name] = "X" * 6000
    # Plus a model-named key, which is the channel #216 closed, so this fixture
    # drives every known field AND the unknown-field path at once.
    obj["Z" * 4000] = 1
    return obj


# --- 1. the bound, by construction -----------------------------------------


def test_every_model_reachable_field_driven_large_at_once_stays_in_bound(
    tmp_path: Path,
) -> None:
    """The case #216's version could not see. Red before this change.

    Every field of the schema large, plus a 4000 character model-named key, in
    one reply. The row has to stay under the documented bound, and no field may
    carry a model string whole.
    """
    engine = _engine(tmp_path, _claude_reply(_everything_large()))
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    row = _turn(engine)
    blob = json.dumps(row, sort_keys=True)
    assert len(blob) <= ROW_BOUND, (
        f"the row is {len(blob)} bytes with every field driven large: {blob[:200]}"
    )
    assert "EEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEEE" not in blob, (
        "a model string reached the row whole"
    )
    assert "ZZZZ" not in blob
    engine.stop()


def test_the_same_reply_through_the_bare_parser_is_also_in_bound(
    tmp_path: Path,
) -> None:
    """`grok` has no schema gate, so the parser is the only thing in front.

    The claude path forces `hold` on a schema violation, which could mask the
    bound by blanking fields; this path does not, so the row carries the
    parser's own output and is the harder case.
    """
    tail = {k: v for k, v in _everything_large().items() if k in ADVICE_PROPERTIES}
    tail["action"] = "buy"
    engine = _engine(
        tmp_path,
        {"choices": [{"message": {"content": "prose\n" + json.dumps(tail)}}]},
        provider="grok",
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    blob = json.dumps(_turn(engine), sort_keys=True)
    assert len(blob) <= ROW_BOUND, f"{len(blob)} bytes: {blob[:200]}"
    engine.stop()


# --- 2. the two fields that were unbounded, each on its own ----------------


def test_a_long_symbol_is_clipped_and_the_row_says_so(tmp_path: Path) -> None:
    """The #226 case as filed: 5600 characters, which gave a 5838 byte row.

    The marker matters as much as the bound. A silently truncated value reads
    as the whole value, so a reader could not tell `EURUSD` from a 5600
    character string beginning with it.
    """
    engine = _engine(
        tmp_path,
        {"choices": [{"message": {"content": "p\n" + json.dumps({
            "action": "buy", "symbol": "E" * 5600, "sl": 1.0, "tp": 1.2,
            "limit": None, "stop": None, "ticket": None, "summary": "s",
        })}}]},
        provider="grok",
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    row = _turn(engine)
    assert len(json.dumps(row)) <= ROW_BOUND
    assert "[+5552 chars]" in row["symbol"], (
        "the row does not say what it dropped: " + row["symbol"][:80]
    )
    engine.stop()


def test_a_huge_ticket_is_not_a_ticket(tmp_path: Path) -> None:
    """`1e308` gave a 309 digit integer and a 501 byte row on its own.

    A ticket is a venue handle, so a value no venue could have issued is not a
    ticket. Refusing it is the same rule as `_num` returning None for a
    non-number, and it keeps the digits of an exponent out of the record.
    """
    advice = parse_advice(
        "p\n" + json.dumps({"action": "buy", "symbol": "EURUSD", "ticket": 1e308})
    )
    assert advice.ticket is None
    assert parse_advice('p\n{"action": "buy", "ticket": 9223372036854775000}').ticket
    del tmp_path


def test_a_400_digit_ticket_does_not_raise_out_of_the_handler(
    tmp_path: Path,
) -> None:
    """`int(inf)` raises `OverflowError`, which the handler does NOT catch.

    `OverflowError` derives from `ArithmeticError`, so it is outside the
    `(ValueError, RuntimeError, OSError)` tuple `Desk.handle_command` catches:
    before this it left the handler as an uncaught exception. Driven through
    the command path rather than the parser, because that is where the escape
    mattered.
    """
    engine = _engine(
        tmp_path,
        {"choices": [{"message": {"content": "p\n" + json.dumps({
            "action": "buy", "symbol": "EURUSD", "ticket": "9" * 400,
        })}}]},
        provider="grok",
    )
    reply = engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    assert reply, "the handler returned nothing, so something escaped"
    assert _turn(engine)["ticket"] is None
    engine.stop()


# --- 3. the controls: nothing that fits is reshaped ------------------------


def test_a_real_symbol_is_never_reshaped() -> None:
    """Every real instrument name, vendor suffix and all, survives whole."""
    for name in ("EURUSD", "EURUSDm", "EURUSD.a", "EURUSD_i", "XAUUSD", "BTCUSD",
                 "EURUSDmicro", "US30", "MATICUSD"):
        assert clip_for_record(name) == name, name
    assert len("EURUSDmicro") < RECORD_STRING_CHARS


def test_an_ordinary_turn_is_byte_for_byte_what_it_was(tmp_path: Path) -> None:
    """The control that stops the bound being satisfied by mangling everything."""
    engine = _engine(
        tmp_path,
        _claude_reply({
            "text": "the book is flat", "action": "buy", "symbol": "EURUSD",
            "sl": 1.09, "tp": 1.11, "limit": None, "stop": None, "ticket": None,
            "summary": "take it",
        }),
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    row = _turn(engine)
    assert row["symbol"] == "EURUSD"
    assert row["sl"] == 1.09
    assert row["degraded"] == ""
    assert "[+" not in json.dumps(row)
    engine.stop()
