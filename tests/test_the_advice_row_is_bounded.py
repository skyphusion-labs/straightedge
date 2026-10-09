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
import re
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


#: Rows that DO exceed the bound and carry no model-chosen content, each with
#: what it scales with, so the exemption is a decision on the record rather
#: than a hole. Measured on this fixture's four-symbol book:
#: `history_preflight` 876 bytes, `history_unavailable` 1108. Both are our own
#: diagnostics, both grow with the OPERATOR's symbol book rather than with
#: anything a model can say, and neither is reachable by a reply. Whether they
#: should be bounded or summarised is a separate question about operator
#: diagnostics, filed rather than decided here (straightedge#236).
#:
#: A row that exceeds the bound and is NOT in this map fails, so a new row on
#: the advice path has to be classified by a person.
ROWS_EXEMPT_FROM_THE_BOUND = {
    "history_preflight": "one entry per configured symbol",
    "history_unavailable": "our own prose, a paragraph per unusable symbol",
}


def _assert_every_row_in_bound(engine: Engine) -> None:
    """EVERY row the turn wrote, not only `advice_turn`.

    One model reply writes several rows. The same large-symbol reply that
    produced the headline 5838 byte `advice_turn` row ALSO writes a `reject`
    row carrying the symbol, and a review measured that removing the clip from
    the reject site alone left a 5753 byte row with the whole suite green,
    because every test here read one event name. A bound asserted on the row
    the author was thinking about is the fixture-shaped assertion this issue is
    about, one event over.

    So the assertion is over the journal rather than over a chosen row: a new
    row added to the advice path tomorrow is covered without anyone editing
    this file, exactly as a new schema field is.
    """
    rows = list(engine.journal.tail(5000))
    oversized = [
        (row.get("event"), len(json.dumps(row, sort_keys=True)))
        for row in rows
        if len(json.dumps(row, sort_keys=True)) > ROW_BOUND
        and row.get("event") not in ROWS_EXEMPT_FROM_THE_BOUND
    ]
    assert not oversized, (
        f"{len(oversized)} journal row(s) exceed the {ROW_BOUND} byte bound and "
        f"are not classified in ROWS_EXEMPT_FROM_THE_BOUND: {oversized!r}. "
        "Either the row carries model-chosen content and must be clipped, or "
        "it is a diagnostic that scales with something the operator set and "
        "needs a line in that map saying so."
    )

    # AND THE EXEMPTION IS NOT A BLANK CHEQUE. An exempt row that starts
    # carrying model text is the exact defect this file exists for, so the
    # marker strings the fixture drives are looked for in those rows too.
    for row in rows:
        if row.get("event") not in ROWS_EXEMPT_FROM_THE_BOUND:
            continue
        blob = json.dumps(row, sort_keys=True)
        for marker in ("E" * 50, "Z" * 50, "9" * 50):
            assert marker not in blob, (
                f"the exempt row {row.get('event')!r} is carrying model-chosen "
                "text, so its exemption no longer holds"
            )

    # And a name in the map that no longer appears is a stale exemption, which
    # would quietly widen the hole as rows are renamed.
    seen = {row.get("event") for row in rows}
    stale = sorted(set(ROWS_EXEMPT_FROM_THE_BOUND) - seen)
    assert not stale, (
        f"ROWS_EXEMPT_FROM_THE_BOUND names {stale!r}, which this turn did not "
        "write; a stale exemption widens the bound for nothing"
    )


def _rows_named(engine: Engine, event: str) -> list[dict]:
    """Every row of one event name, so a test can prove it reached a path."""
    return [r for r in engine.journal.tail(5000) if r.get("event") == event]


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
    _assert_every_row_in_bound(engine)
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
    _assert_every_row_in_bound(engine)
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

    # THE SAME REPLY WROTE A REJECT ROW. `E` * 5600 is not an allowed symbol,
    # so the desk also journals `symbol_not_allowed` with the symbol on it, and
    # that row was unpinned: measured at 5753 bytes with the reject clip
    # removed while this test still passed, because it read `advice_turn` only.
    rejects = _rows_named(engine, "reject")
    assert rejects, "the reply did not reach the reject path, so this proves nothing"
    assert rejects[-1].get("reason") == "symbol_not_allowed", (
        f"not the reject this case is about: {rejects[-1]!r}"
    )
    assert "[+5552 chars]" in rejects[-1]["symbol"], (
        "the reject row does not say what it dropped: "
        + str(rejects[-1]["symbol"])[:80]
    )
    _assert_every_row_in_bound(engine)
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


def test_the_documented_bound_is_the_asserted_bound(tmp_path: Path) -> None:
    """The figure in `docs/CONTRACT.md` is read, not trusted.

    The constant and the tests already agreed, because the tests import it.
    The DOCUMENT carried its own literal and nothing read it, so a review
    measured raising `RECORD_ROW_BOUND` to 1024 with the whole suite green
    while the contract still said 512. A bound stated in prose that nothing
    reads is the magic number this issue set out to remove, one file over.

    Anchored to the `Journal row size` ROW rather than to the file, for the
    reason #220 measured: a number mentioned anywhere would satisfy a
    file-wide search.
    """
    del tmp_path
    text = (Path(__file__).resolve().parents[1] / "docs" / "CONTRACT.md").read_text(
        encoding="utf-8"
    )
    rows = [ln for ln in text.splitlines() if ln.startswith("| Journal row size |")]
    assert len(rows) == 1, f"expected one `Journal row size` row, found {len(rows)}"
    found = re.findall(r"\((\d+) bytes\)", rows[0])
    assert found, "the row no longer states a byte figure: " + rows[0][:120]
    assert [int(n) for n in found] == [RECORD_ROW_BOUND] * len(found), (
        f"docs/CONTRACT.md states {found!r} bytes and journal.RECORD_ROW_BOUND "
        f"is {RECORD_ROW_BOUND}; the documented bound and the asserted bound "
        "have drifted"
    )


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
