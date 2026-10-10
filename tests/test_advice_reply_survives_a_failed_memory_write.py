"""straightedge#282: a failed advice-memory write discarded a paid-for reply.

`Advisor.ask` calls the provider, then records the turn:

    raw = self._claude(user)        # the provider has ANSWERED and billed
    advice = parse_advice(raw)
    self._remember("user", question)
    self._remember("assistant", advice.text or raw)
    return advice

`_remember` ends in `self.save()`, and no call site wrapped either one. So a
`save()` that raised took `ask()` with it and **the reply was discarded on the
way out**, after two things had already been spent:

* the provider call was made and billed;
* `Desk._ask` had already called `record_advice_turn()`, so
  `advice.max_turns_per_day` had counted the turn.

The operator got an error instead of the answer, and the daily advice budget
was one lower. **Persisting the conversation memory is a convenience; the reply
is the thing the operator paid for, and a failure of the convenience destroyed
the product.**

#251 routed the replace through `replace_retrying_on_share_conflict`, which
absorbs a concurrent reader refusing the replace. That makes this rarer. It
does not change what happens when the write genuinely cannot complete, and it
does not touch the other ways `save()` raises: a full disk, a revoked ACL, a
vanished directory, or the `tmp.write_text` that precedes the replace.

## The failures here are REAL, not monkeypatched

A stubbed `save` would encode this file's own assumption about how persistence
fails. Both failing layouts below are made on the real filesystem and the real
`save()` raises against them, portably:

* the persist path's PARENT is a regular file, so `path.parent.mkdir` raises
  before anything is written. That is the issue's "vanished directory" case.
* the `.tmp` sibling `save()` writes through is a DIRECTORY, so
  `tmp.write_text` raises after `mkdir` succeeded. That is the issue's
  "`tmp.write_text` that precedes the replace" case.

**Every test here first asserts that `save()` genuinely raises on its layout.**
Without that control a fix could be absent and these tests would still pass,
which is the absent-check-reads-like-a-passed-one shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

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
        self.calls = 0

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None):
        del payload, timeout, headers
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": []}
        if url.endswith("/sendMessage"):
            return {"ok": True, "result": {"message_id": 1}}
        self.calls += 1
        return self.payloads.pop(0) if self.payloads else {}


REPLY_TEXT = "the book is flat and the spread is wide, stand aside"


def _obj(**over) -> dict:
    base = {
        "text": REPLY_TEXT,
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


def _claude_reply(obj: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(obj)}]}


def parent_is_a_file(tmp_path: Path) -> Path:
    """A persist path whose parent is a regular file: `mkdir` raises."""
    blocker = tmp_path / "blocker"
    blocker.write_text("not a directory", encoding="utf-8")
    return blocker / "advice.json"


def tmp_sibling_is_a_directory(tmp_path: Path) -> Path:
    """A persist path whose `.tmp` sibling is a directory: the write raises."""
    path = tmp_path / "advice.json"
    (tmp_path / "advice.json.tmp").mkdir()
    return path


LAYOUTS = [
    pytest.param(parent_is_a_file, id="parent-is-a-file"),
    pytest.param(tmp_sibling_is_a_directory, id="tmp-sibling-is-a-directory"),
]


def _advisor(persist: Path, *payloads: dict) -> Advisor:
    cfg = BotConfig()
    cfg.advice.provider = "claude"
    cfg.advice.claude_key = "k"
    return Advisor(
        cfg.advice, transport=FakeTransport(*payloads), persist_path=persist
    )


def _engine(tmp_path: Path, persist: Path, *payloads: dict) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.advice.provider = "claude"
    cfg.advice.claude_key = "k"
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    advisor = Advisor(
        cfg.advice, transport=FakeTransport(*payloads), persist_path=persist
    )
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), advisor=advisor)
    engine.start()
    return engine


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_layout_really_does_break_save(layout, tmp_path: Path) -> None:
    """THE DENOMINATOR, and it runs first for a reason.

    If `save()` did not raise on these layouts, every test below would pass
    with the defect fully present. This asserts the instrument before anything
    is measured with it.
    """
    persist = layout(tmp_path)
    advisor = _advisor(persist)
    with pytest.raises(OSError):
        advisor.save()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_a_failed_memory_write_does_not_discard_the_reply(
    layout, tmp_path: Path
) -> None:
    """The defect, stated as the operator meets it.

    The provider answered. The operator paid for that answer. A file the desk
    could not write must not be able to take it away.
    """
    persist = layout(tmp_path)
    advisor = _advisor(persist, _claude_reply(_obj()))
    advice = advisor.ask("take a view", "context")
    assert advice.text == REPLY_TEXT, (
        "the reply the operator paid for did not survive the failed memory "
        f"write: {advice!r}"
    )
    assert advice.action == "hold"


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_failed_write_is_reported_rather_than_swallowed(
    layout, tmp_path: Path
) -> None:
    """Best-effort must not mean silent.

    A full disk or a revoked ACL is a real problem on a real-money box. The
    narrow fix (swallow at the `_remember` call site) hides exactly that, which
    is why the reply carries the reason out instead.
    """
    persist = layout(tmp_path)
    advisor = _advisor(persist, _claude_reply(_obj()))
    advice = advisor.ask("take a view", "context")
    assert advice.memory_error, (
        "the persistence failure was swallowed with nothing to say it happened"
    )


def test_a_healthy_persist_path_reports_no_memory_error(tmp_path: Path) -> None:
    """The control pair. Without it the field could be set on every turn.

    It is also the assertion that the happy path still WRITES: a fix that
    stopped saving altogether would satisfy every test above.
    """
    persist = tmp_path / "advice.json"
    advisor = _advisor(persist, _claude_reply(_obj()))
    advice = advisor.ask("take a view", "context")
    assert advice.memory_error == "", (
        f"a healthy save reported a failure: {advice.memory_error}"
    )
    assert persist.exists(), "the happy path did not persist the memory at all"
    saved = json.loads(persist.read_text(encoding="utf-8"))
    assert [t["role"] for t in saved["turns"]] == ["user", "assistant"]


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_in_process_memory_still_carries_the_turn(layout, tmp_path: Path) -> None:
    """The turn is not lost from the running desk, only from the disk.

    This is what makes the reply's wording honest: the next question in THIS
    process still has the context, and a restart is what loses it.
    """
    persist = layout(tmp_path)
    advisor = _advisor(persist, _claude_reply(_obj()))
    advisor.ask("take a view", "context")
    assert [t["role"] for t in advisor._memory] == ["user", "assistant"], (
        f"the failed disk write also dropped the in-process turn: {advisor._memory}"
    )


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_desk_journals_that_the_memory_did_not_persist(
    layout, tmp_path: Path
) -> None:
    """The durable surface, because stderr on the box goes to a file nobody reads.

    `journal.jsonl` is what anyone reconstructing a demo week reads. A
    persistence failure that reached only the chat would be invisible to it.
    """
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    persist = layout(state)
    engine = _engine(tmp_path, persist, _claude_reply(_obj()))
    try:
        engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
        rows = [
            r
            for r in engine.journal.tail(5000)
            if r.get("event") == "advice_memory_unsaved"
        ]
        assert len(rows) == 1, (
            "nothing in the journal says the advice memory did not persist: "
            + json.dumps([r.get("event") for r in engine.journal.tail(5000)])
        )
        assert rows[0]["error_type"], "the row carries no reason"
        assert rows[0]["turn_spent"] is True, (
            "the row does not say the operator paid for this turn"
        )
        turns = [
            r for r in engine.journal.tail(5000) if r.get("event") == "advice_turn"
        ]
        assert len(turns) == 1, "the turn itself was not journalled"
    finally:
        engine.stop()


def test_a_healthy_turn_journals_no_unsaved_row(tmp_path: Path) -> None:
    """The control pair for the row. A row on every turn would be noise."""
    persist = tmp_path / "state" / "advice.json"
    engine = _engine(tmp_path, persist, _claude_reply(_obj()))
    try:
        engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
        rows = [
            r
            for r in engine.journal.tail(5000)
            if r.get("event") == "advice_memory_unsaved"
        ]
        assert rows == [], f"a healthy save wrote a failure row: {rows}"
    finally:
        engine.stop()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_operator_is_told_in_the_reply(layout, tmp_path: Path) -> None:
    """The chat half. The operator is the one who will lose the context.

    An operator who restarts the desk and finds the conversation gone, with
    nothing having said so, is being surprised by a failure the desk already
    knew about.
    """
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    persist = layout(state)
    engine = _engine(tmp_path, persist, _claude_reply(_obj()))
    try:
        out = engine.desk.handle(TgCommand("1", 1, "/ask take a view", 1))
        assert REPLY_TEXT in out, (
            f"the reply the operator paid for is not in the chat answer: {out!r}"
        )
        assert "not saved" in out.lower() or "did not save" in out.lower(), (
            f"the operator is not told the memory failed to persist: {out!r}"
        )
    finally:
        engine.stop()


def test_a_healthy_turn_says_nothing_about_memory(tmp_path: Path) -> None:
    """The control pair for the chat line, so it cannot become boilerplate."""
    persist = tmp_path / "state" / "advice.json"
    engine = _engine(tmp_path, persist, _claude_reply(_obj()))
    try:
        out = engine.desk.handle(TgCommand("1", 1, "/ask take a view", 1))
        assert REPLY_TEXT in out
        assert "not saved" not in out.lower(), (
            f"a healthy turn told the operator about the memory file: {out!r}"
        )
    finally:
        engine.stop()


@pytest.mark.parametrize("layout", LAYOUTS)
def test_the_advice_budget_slot_bought_something(layout, tmp_path: Path) -> None:
    """The money half of the issue, asserted rather than argued.

    `Desk._ask` counts the turn BEFORE the provider call, on purpose, because
    the turn is billed whether or not it ends in an order. So the slot is spent
    by the time `save()` can fail. Before this fix the operator paid a slot and
    a provider call and received an error; the slot being spent is correct, and
    it has to buy the reply.
    """
    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    persist = layout(state)
    engine = _engine(tmp_path, persist, _claude_reply(_obj()))
    try:
        before = engine.risk.snapshot.advice_turns_today
        out = engine.desk.handle(TgCommand("1", 1, "/ask take a view", 1))
        after = engine.risk.snapshot.advice_turns_today
        assert after == before + 1, "the turn was not counted, so this proves nothing"
        assert REPLY_TEXT in out, (
            "a budget slot was spent and the operator got no reply: " + repr(out)
        )
    finally:
        engine.stop()


def test_a_clean_turn_after_a_failed_one_carries_no_stale_error(
    tmp_path: Path,
) -> None:
    """straightedge#119's shape, in this field.

    A marker that outlived its own turn would attach to a turn that saved
    perfectly well, and the record would be confidently wrong rather than
    merely silent. The layout is REPAIRED on the real filesystem between the
    two asks rather than stubbed, so the second turn genuinely does persist.
    """
    persist = parent_is_a_file(tmp_path)
    advisor = _advisor(persist, _claude_reply(_obj()), _claude_reply(_obj()))
    first = advisor.ask("one", "context")
    assert first.memory_error, "the first turn should have failed to persist"
    persist.parent.unlink()
    second = advisor.ask("two", "context")
    assert second.memory_error == "", (
        "a persistence failure outlived its turn and attached to a turn that "
        f"saved: {second.memory_error}"
    )
    assert persist.exists(), "the repaired layout did not actually persist"
