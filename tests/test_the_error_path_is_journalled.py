"""A command that FAILS leaves a record, not only a chat line (#232).

`Desk.handle` ended with `except (ValueError, RuntimeError) as exc: return
redact_text(str(exc))`. The sentence reached the chat and nothing reached the
journal, so every command that failed this way was invisible to the one channel
an operator can audit, `/history` can show, and a test is allowed to assert on.

MEASURED ON `main` BEFORE THIS CHANGE, with a transport that raises the way a
real HTTP hop does (a fake that returns a payload can never find this, which is
why the issue came out of a live run):

    /ask view  ->  'telegram http 502'
    journal    ->  start, history_preflight, history_unavailable, venue_clock

No row for the turn. And on the advice path that silence costs the operator
something real: `record_advice_turn()` runs BEFORE the provider call, which is
deliberate and pinned elsewhere because the turn is billed whether or not it
ends in an order, so the failed turn SPENT a slot off the daily cap and left
nothing saying it was ever attempted. The operator watches their advice budget
shrink and the record cannot tell them why.

WHAT THE ROW CARRIES, and what it deliberately does not. The row carries the
exception CLASS, the command, the source and `measured=false`. It does NOT
carry the exception's sentence, and that is the #216 discipline rather than an
omission: a message can be authored by a model or echoed from a venue, and a
model-authored sentence in the record is exactly the defect #216 and #226
closed. The chat already has the sentence. A clipped detail field is possible
once `clip_for_record` is on `main` (#226) and is left as a follow-up rather
than hand-rolled here, because a second clipper in this file would be a second
opinion about one bound.

COULD NOT MEASURE stays distinct from REFUSED. `reject` means a gate said no;
these rows mean the desk could not reach an answer, so they are their own event
names with `measured=false`, and a reason count can never read a crash as a
rule saying no.
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


class RaisingTransport:
    """Raises the way a real HTTP hop does, and only on the provider call."""

    def __init__(self, exc: Exception) -> None:
        self.exc = exc

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None):
        del payload, timeout, headers
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": []}
        if url.endswith("/sendMessage"):
            return {"ok": True, "result": {"message_id": 1}}
        raise self.exc


def _engine(tmp_path: Path, exc: Exception | None = None) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.advice.provider = "grok"
    cfg.advice.grok_key = "k"
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    advisor = None
    if exc is not None:
        advisor = Advisor(
            cfg.advice, transport=RaisingTransport(exc), persist_path=tmp_path / "a.json"
        )
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), advisor=advisor)
    engine.start()
    return engine


def _rows(engine: Engine, event: str) -> list[dict]:
    return [r for r in engine.journal.tail(5000) if r.get("event") == event]


# --- 1. the advice path, where the failure also costs a cap slot ------------


def test_a_failed_advice_turn_is_recorded_with_the_slot_it_spent(
    tmp_path: Path,
) -> None:
    """The measured case: the provider raises, the chat answers, the record was empty.

    The assertion that matters is not only that a row exists: it is that the
    row says the turn was SPENT, because that is the part the operator cannot
    reconstruct from anything else. The count is read from the risk snapshot
    rather than asserted as a constant, so the test is about the row agreeing
    with the budget rather than about the number 1.
    """
    engine = _engine(tmp_path, RuntimeError("telegram http 502"))
    before = engine.risk.snapshot.advice_turns_today

    reply = engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))

    # The chat behaviour must not move. This is a record change only.
    assert reply == "telegram http 502", f"the chat reply changed: {reply!r}"

    after = engine.risk.snapshot.advice_turns_today
    assert after == before + 1, (
        "the cap was not spent, so this fixture is not exercising the case: "
        f"{before} -> {after}"
    )

    rows = _rows(engine, "advice_error")
    assert len(rows) == 1, (
        "the failed advice turn left no record of itself: "
        f"{[r.get('event') for r in engine.journal.tail(5000)]}"
    )
    row = rows[0]
    assert row["error_type"] == "RuntimeError", row
    assert row["source"] == "advice", row
    assert row["turn_spent"] is True, (
        "the row does not say the daily cap was spent, which is the part no "
        f"other row can say: {row}"
    )
    assert row["measured"] is False, (
        "a crash is COULD NOT MEASURE, never a refusal: " + json.dumps(row)
    )
    assert not _rows(engine, "reject"), (
        "a crash was recorded as a gate saying no, which corrupts every reason "
        "count built on `reject`"
    )
    engine.stop()


def test_the_failed_turn_carries_no_sentence_from_the_provider(
    tmp_path: Path,
) -> None:
    """The row is a CLASS, not a message, and the message can be anybody's.

    #216 measured a model-authored sentence reaching `journal.jsonl` through a
    field added to make the journal trustworthy. An exception message is the
    same channel one layer down: a provider can put its own text in it, and a
    provider error can quote a model reply. So the chat gets the sentence and
    the record gets the class.
    """
    secret = "PROVIDER SAID " + "Q" * 400
    engine = _engine(tmp_path, RuntimeError(secret))
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    blob = json.dumps(_rows(engine, "advice_error")[0], sort_keys=True)
    assert "QQQQ" not in blob, "the provider's sentence reached the row: " + blob[:200]
    assert len(blob) <= 512, f"the row is {len(blob)} bytes: {blob[:200]}"
    engine.stop()


def test_a_value_error_from_the_provider_is_recorded_as_its_own_class(
    tmp_path: Path,
) -> None:
    """Both arms of the caught tuple, because one of them is a different bug.

    A `ValueError` out of a provider is a reply we could not read; a
    `RuntimeError` is a hop that failed. They want different actions from the
    operator, so the class is the field and not a flag.
    """
    engine = _engine(tmp_path, ValueError("not json"))
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    rows = _rows(engine, "advice_error")
    assert len(rows) == 1 and rows[0]["error_type"] == "ValueError", rows
    engine.stop()


# --- 2. every other command, which never spends a cap ----------------------


def test_a_command_that_raises_is_recorded(tmp_path: Path) -> None:
    """The generic arm, driven through a real command rather than the handler.

    `handle()`'s except is generic by design, so the test drives it by making
    one command's implementation raise, which is what a venue read does when
    the terminal goes away mid-command.
    """
    engine = _engine(tmp_path)

    def boom() -> str:
        raise RuntimeError("terminal went away")

    engine.desk.engine.status_text = boom  # type: ignore[method-assign]
    reply = engine.handle_command(TgCommand("1", 1, "/status", 1))
    assert reply == "terminal went away", f"the chat reply changed: {reply!r}"

    rows = _rows(engine, "command_error")
    assert len(rows) == 1, (
        "the failed command left no record: "
        f"{[r.get('event') for r in engine.journal.tail(5000)]}"
    )
    row = rows[0]
    assert row["command"] == "status", row
    assert row["error_type"] == "RuntimeError", row
    assert row["measured"] is False, row
    assert row["source"] == "telegram", row
    assert "turn_spent" not in row, (
        "a command that spends no advice slot must not claim one: " + json.dumps(row)
    )
    engine.stop()


def test_free_text_that_raises_names_the_path_it_took(tmp_path: Path) -> None:
    """Bare text is the advice path, so it records as one.

    A message with no command name goes straight to `_ask`, so a failure there
    is an `advice_error` with a spent turn rather than a `command_error`, and
    the two must not be confused: one costs the operator a cap slot and the
    other does not.
    """
    engine = _engine(tmp_path, RuntimeError("telegram http 502"))
    engine.handle_command(TgCommand("1", 1, "is the euro a buy", 1))
    assert len(_rows(engine, "advice_error")) == 1, "free text did not take the ask path"
    assert not _rows(engine, "command_error"), (
        "free text was recorded as a command error, which hides the spent turn"
    )
    engine.stop()


# --- 3. the controls --------------------------------------------------------


def test_a_command_that_succeeds_writes_no_error_row(tmp_path: Path) -> None:
    """The control. A row that is always written says nothing.

    Without this, an unconditional write would satisfy every assertion above.
    """
    engine = _engine(tmp_path)
    reply = engine.handle_command(TgCommand("1", 1, "/status", 1))
    assert "mode" in reply.lower() or reply, "the command did not answer"
    assert not _rows(engine, "command_error"), "a successful command wrote an error row"
    assert not _rows(engine, "advice_error"), "a successful command wrote an advice error"
    engine.stop()


def test_a_refusal_is_still_a_refusal(tmp_path: Path) -> None:
    """The partition, from the other side.

    A gate saying no must still write `reject` and must NOT write an error row,
    or the two populations merge and every reason count built on `reject`
    becomes unreadable.
    """
    engine = _engine(tmp_path, RuntimeError("never reached"))
    engine.cfg.advice.max_turns_per_day = 1
    engine.risk.record_advice_turn()
    reply = engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    assert reply == "refused: max_advice_turns_per_day", reply
    assert _rows(engine, "reject"), "the refusal stopped being recorded as a refusal"
    assert not _rows(engine, "advice_error"), "a refusal was recorded as a crash"
    engine.stop()
