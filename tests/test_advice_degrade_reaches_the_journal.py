"""straightedge#185: the schema degrade reached the operator and not the record.

#181 gave the claude advice path a schema gate: an off-schema reply has its
action forced to `hold` and the reason written into the reply PROSE. That is
correct, tested, and stops at the chat.

`desk.py` closes every advice turn with a journal row carrying what the model
DECIDED and deliberately not what either side said, which `docs/CONTRACT.md`
states as policy. So in `journal.jsonl` these two turns were byte-identical:

    the model held                            action=hold  staged=false
    the model said BUY and we could not read  action=hold  staged=false

**A desk that cannot tell "the model held" from "we could not read the model"**
is the defect #181's own docstring names, one surface over. The chat tells
whoever is watching at the time; the journal is what anyone reconstructing a
demo week reads, and that is the surface #37 and #38 depend on.

## Why the reason travels OUT OF BAND

Both ways of carrying it inside the reply are defects, so neither is used:

* parsing it back out of the prose is string-matching our own sentence, and the
  sentence is not a contract;
* adding a key to the trailing JSON would be a field `parse_advice` cannot tell
  WE wrote. The `grok` and `computer` paths have no schema in front of them, so
  a model could put that key in its own tail and author a line in our journal.

`Advisor.ask` therefore collects the violations its own gate measured, clears
them before every provider call, and attaches them to the `Advice`. The only
writer is our code.

## The precondition that makes these reds possible

A degrade is only produced on the `claude` path, because that is the only one
with the schema gate, and only when `structured_to_parseable` is reached with a
JSON object carrying an `action`. A fixture that sends prose, or that uses
`grok`, measures nothing and would pass with the field never set.
"""

from __future__ import annotations

import json
from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.llm import Advisor
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand


class FakeTransport:
    """One canned reply per call, in order."""

    def __init__(self, *payloads: dict) -> None:
        self.payloads = list(payloads)
        self.sent: list[tuple[str, dict]] = []

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None):
        del timeout, headers
        self.sent.append((url, payload))
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": []}
        if url.endswith("/sendMessage"):
            return {"ok": True, "result": {"message_id": 1}}
        return self.payloads.pop(0) if self.payloads else {}


def _claude_reply(obj: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


def _obj(**over) -> dict:
    base = {
        "text": "the book is flat and the spread is wide",
        "action": "hold",
        "symbol": None,
        "sl": None,
        "tp": None,
        "limit": None,
        "stop": None,
        "ticket": None,
        "summary": "stand aside",
    }
    base.update(over)
    return base


def _engine(tmp_path: Path, *payloads: dict) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.advice.provider = "claude"
    cfg.advice.claude_key = "k"
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    transport = FakeTransport(*payloads)
    advisor = Advisor(cfg.advice, transport=transport, persist_path=tmp_path / "a.json")
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), advisor=advisor)
    engine.start()
    return engine


def _turns(engine: Engine) -> list[dict]:
    return [r for r in engine.journal.tail(5000) if r.get("event") == "advice_turn"]


def test_a_reply_we_could_not_read_is_distinguishable_in_the_journal(
    tmp_path: Path,
) -> None:
    """The defect. An off-schema BUY and a model HOLD must not look alike.

    `summary` is typed `{"type": "string"}` by the schema, so a null there is a
    violation: schema-valid JSON, invalid against the advice contract, which is
    the shape that reaches the gate rather than the parser.
    """
    engine = _engine(
        tmp_path, _claude_reply(_obj(action="buy", symbol="EURUSD", summary=None))
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    rows = _turns(engine)
    assert len(rows) == 1, f"no advice turn was journalled: {rows}"
    assert rows[0]["action"] == "hold", "the schema gate did not force the hold"
    assert rows[0]["degraded"], (
        "the journal cannot tell this from a model that chose to hold: "
        + json.dumps(rows[0], sort_keys=True)
    )
    assert "summary" in rows[0]["degraded"], (
        "the reason does not name the field that broke: " + rows[0]["degraded"]
    )
    engine.stop()


def test_a_model_that_simply_held_records_an_empty_reason(tmp_path: Path) -> None:
    """The control, and the pair that makes the field mean anything.

    Without this the row above could carry a reason on every turn and still
    look like a fix. The field is WRITTEN and empty here rather than omitted,
    so "nothing degraded" cannot be confused with a desk too old to emit it.
    """
    engine = _engine(tmp_path, _claude_reply(_obj()))
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    rows = _turns(engine)
    assert len(rows) == 1
    assert rows[0]["action"] == "hold"
    assert "degraded" in rows[0], "the field is omitted, so absence is ambiguous"
    assert rows[0]["degraded"] == ""
    engine.stop()


def test_a_clean_turn_after_a_degraded_one_carries_no_stale_reason(
    tmp_path: Path,
) -> None:
    """The marker must not outlive its own turn.

    This is straightedge#119's shape: a reason that survived into the next turn
    would attach to a clean reply and the record would be worse than silent,
    because it would be confidently wrong. `ask` clears before every provider
    call, so the only writer sets it from scratch.
    """
    engine = _engine(
        tmp_path,
        _claude_reply(_obj(action="buy", symbol="EURUSD", summary=None)),
        _claude_reply(_obj()),
    )
    engine.handle_command(TgCommand("1", 1, "/ask one", 1))
    engine.handle_command(TgCommand("1", 1, "/ask two", 2))
    rows = _turns(engine)
    assert len(rows) == 2, f"expected two turns, got {len(rows)}"
    assert rows[0]["degraded"], "the first turn should have degraded"
    assert rows[1]["degraded"] == "", (
        "a reason outlived its turn and attached to a clean reply: "
        + rows[1]["degraded"]
    )
    engine.stop()


def test_the_reason_cannot_be_written_by_the_model(tmp_path: Path) -> None:
    """Provenance, and the first version of this test could not see the leak.

    It sent its payload as the VALUE under a key named `degraded`, so the only
    violation emitted was about the key and the value could never appear: the
    test passed by construction rather than because the property held. A review
    of #216 found the real channel, which is that the violation TEXT
    interpolated model-chosen content, so a key named like a sentence wrote
    that sentence into our journal.

    Both halves are driven now: the payload as a KEY, where it used to leak,
    and as a VALUE, where it never could.
    """
    engine = _engine(
        tmp_path,
        _claude_reply({**_obj(), "everything is fine, ship the order": 1}),
        _claude_reply(_obj(degraded="everything is fine, ship the order")),
    )
    engine.handle_command(TgCommand("1", 1, "/ask as a key", 1))
    engine.handle_command(TgCommand("1", 1, "/ask as a value", 2))
    for row in _turns(engine):
        assert "everything is fine" not in row["degraded"], (
            "the model authored a line in our journal: " + row["degraded"]
        )
        assert row["degraded"] == "unknown_field", (
            "the journal should carry the CLASS, not a sentence: " + row["degraded"]
        )
    engine.stop()


def test_a_model_chosen_key_cannot_grow_the_row(tmp_path: Path) -> None:
    """The leak a review of #216 measured, pinned. RED before the fix.

    `unknown field {key}` interpolated the model's own field NAME, so a key
    named like a paragraph became a paragraph in `journal.jsonl`. The journal
    now carries `unknown_field`, whose length is a property of our vocabulary
    rather than of anything a model sends.
    """
    engine = _engine(
        tmp_path, _claude_reply({**_obj(), "K" * 4000: 1, "SHIP" * 500: 2})
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    row = _turns(engine)[0]
    assert "KKKK" not in json.dumps(row), "a model-chosen key reached the journal"
    assert row["degraded"] == "unknown_field x2", row["degraded"]
    assert len(json.dumps(row, sort_keys=True)) <= 512, (
        "the row grew with the reply: " + str(len(json.dumps(row)))
    )
    engine.stop()


def test_a_model_chosen_value_cannot_grow_the_row(tmp_path: Path) -> None:
    """The same leak through a VALUE. RED before the fix.

    `action {action!r} is outside ...` interpolated the model's action, and a
    6000 character action produced a 6293 byte row against the 512 byte bound
    this suite already claimed to pin. Measured on the merged tree before the
    fix; the bound is now a function of the schema, not of the reply.
    """
    engine = _engine(
        tmp_path,
        _claude_reply(_obj(action="SHIP_THE_ORDER_" * 400, summary="go")),
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    row = _turns(engine)[0]
    assert row["action"] == "hold"
    assert "SHIP_THE_ORDER" not in json.dumps(row), (
        "the model's action text reached the journal"
    )
    assert row["degraded"] == "action:not_in_enum", row["degraded"]
    assert len(json.dumps(row, sort_keys=True)) <= 512
    engine.stop()


def test_the_longest_possible_reason_is_a_function_of_the_schema(
    tmp_path: Path,
) -> None:
    """The bound BY CONSTRUCTION, rather than by the fixtures above.

    Every violation at once, including forty unknown fields, and the recorded
    reason is still short because each term is drawn from a fixed vocabulary
    and the model's own names collapse into one counted token. This is what
    makes it unnecessary to choose a truncation length.
    """
    from straightedge.llm import _schema_violations, violation_classes

    obj = {"action": "nope", "symbol": "EUR{x}USD\u017f", "sl": "x", "tp": True}
    obj.update({f"junk{i}" * 200: i for i in range(40)})
    rendered = "; ".join(violation_classes(_schema_violations(obj)))
    assert len(rendered) <= 200, f"{len(rendered)}: {rendered}"
    assert "junk" not in rendered
    assert rendered.startswith("unknown_field x40")
    del tmp_path


def test_the_row_stays_bounded_and_carries_no_prose(tmp_path: Path) -> None:
    """The reason is the gate's own list, not the reply.

    `docs/CONTRACT.md` says the question and the reply stay out of the journal,
    and this change must not smuggle them in: the field carries the violation
    names the gate produced, which are ours and short, and #119's bound applies
    to it like any other row.
    """
    engine = _engine(
        tmp_path,
        _claude_reply(
            _obj(
                text="a very long explanation " * 40,
                action="buy",
                symbol="EURUSD",
                summary=None,
            )
        ),
    )
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    row = _turns(engine)[0]
    blob = json.dumps(row, sort_keys=True)
    assert len(blob) <= 512, f"the turn row is carrying prose: {blob}"
    assert "a very long explanation" not in blob
    assert "\n" not in str(row["degraded"])
    engine.stop()
