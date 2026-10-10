"""The unresolved-send refusal, asserted on the structured channel (#237).

`grep -rn send_refused_unresolved tests/` returned nothing: a journalled
refusal event with zero assertions anywhere. It is the structured record of the
one refusal class that exists to stop a DUPLICATE ORDER, so it is the last
record anybody should be taking on trust.

It carries no named `reason`, which is why a scan built from `_reject(` and
`journal.write("reject", ...)` sites cannot see it: it is outside that
population by construction rather than missed by the instrument.

THE BEHAVIOUR QUESTION IN THE ISSUE IS ANSWERED BY MEASUREMENT, AND THE ANSWER
IS THAT THE PREMISE WAS WRONG. The issue reads "it uses `_emit`, not
`journal.write`, so unlike every `reject` site this refusal IS broadcast to the
chat", and offers three options depending on whether that is intended. It is
not broadcast. `_emit` notifies only when `_format_event` returns a non-empty
string, `_format_event` has no arm for this event and returns `""`, and the
event is in neither `ALWAYS_NOTIFY_EVENTS` nor the default `notify_events`
allowlist. Measured through a real unresolved send across a restart: **zero
`sendMessage` calls, including with the event explicitly allowlisted**, because
the formatter is the binding guard. So the journal-only invariant holds, no
carve-out is needed, and no behaviour changes here.

TWO INDEPENDENT REASONS KEEP IT QUIET, and that is worth pinning rather than
celebrating: either one alone would be sufficient, so an arm added to
`_format_event` later would start broadcasting a refusal silently, with the
allowlist as the only thing left in the way. The last test in this file is the
one that would notice.

AND THE OPERATOR IS NOT LEFT UNINFORMED, which is the thing the issue was right
to worry about. On the DESK path they get `refused: unresolved send
<client_id>` as the reply, which is prose on purpose because it names one
in-flight send (pinned as prose by #220's scan). This event is the AUTO and
restart path, where nobody is waiting on a reply and the journal is the record,
exactly as every other auto refusal works.
"""

from __future__ import annotations

from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine, _format_event
from straightedge.models import OrderResult, Signal, SignalKind
from straightedge.synthetic import generate_bars
from straightedge.telegram import ALWAYS_NOTIFY_EVENTS, TelegramClient

EVENT = "send_refused_unresolved"
KEY = "K-237"


class CountingTransport:
    """Counts chat sends, so "journal only" is measured and not assumed."""

    def __init__(self) -> None:
        self.sent: list[dict] = []

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None):
        del timeout, headers
        if url.endswith("/getUpdates"):
            return {"ok": True, "result": []}
        if url.endswith("/sendMessage"):
            self.sent.append(payload)
            return {"ok": True, "result": {"message_id": len(self.sent)}}
        return {}


class NoVerdictBroker(PaperBroker):
    """A venue that takes the order and never answers.

    THE ONLY un-stubbed way to open a ledger entry. The entry closes on a
    VERDICT, so a rejection would close it and a fill would close it; the state
    this refusal exists for is the one where the venue says nothing at all, and
    only the broker boundary can produce that.
    """

    def market(self, order):
        del order
        return OrderResult.unknown("no verdict came back")


def _engine(tmp_path: Path, transport: CountingTransport, *, allow_event: bool = False) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker = NoVerdictBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    kwargs = {"notify_events": frozenset({EVENT})} if allow_event else {}
    tg = TelegramClient(token="t", chat_id="1", transport=transport, **kwargs)
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), telegram=tg)
    engine.start()
    return engine


def _signal() -> Signal:
    return Signal(
        kind=SignalKind.BUY, symbol="EURUSD", entry=1.0, sl=0.9, tp=1.3, atr=0.001
    )


def _rows(engine: Engine) -> list[dict]:
    return [r for r in engine.journal.tail(5000) if r.get("event") == EVENT]


def _leave_an_unresolved_send(tmp_path: Path) -> OrderResult:
    """One send that the venue never answers, then the process exits.

    The ledger lives beside the journal and is durable on purpose, so the
    second engine below is a RESTART rather than a second object: the desk's own
    in-memory `_already_attempted` guard is gone and the engine's control is the
    only thing standing between the operator and a duplicate order. That is the
    reachability this event exists for, and it is why the test spans two
    engines.
    """
    engine = _engine(tmp_path, CountingTransport())
    result = engine.submit(_signal(), 0.01, KEY)
    engine.stop()
    return result


def test_an_unanswered_send_leaves_the_ledger_open_and_writes_no_refusal(
    tmp_path: Path,
) -> None:
    """The precondition, asserted before anything else reads this state.

    If the first send resolved the entry, every assertion below would be about
    a path the fixture never reached.
    """
    result = _leave_an_unresolved_send(tmp_path)
    assert result.measured is False, f"the venue answered, so nothing is unresolved: {result}"
    assert result.transmitted is True, "the order never went out, which is a different state"

    engine = _engine(tmp_path, CountingTransport())
    assert not _rows(engine), "the refusal fired on the FIRST send, which would be a defect"
    engine.stop()


def test_the_second_attempt_is_refused_and_the_record_names_the_send(
    tmp_path: Path,
) -> None:
    """The structured record, field by field, correlated by `client_id`.

    `attempts` and `first_at` are what make the row answerable later: a bare
    "refused" cannot tell an operator whether this is the second attempt or the
    fifth, nor how long the unresolved send has been outstanding.
    """
    _leave_an_unresolved_send(tmp_path)

    transport = CountingTransport()
    engine = _engine(tmp_path, transport)
    result = engine.submit(_signal(), 0.01, KEY)

    assert result.transmitted is False, "a second order went to the venue"
    assert result.measured is False, "a refusal to send is COULD NOT MEASURE, not a verdict"

    rows = _rows(engine)
    assert len(rows) == 1, f"expected one refusal row, found {len(rows)}"
    row = rows[0]
    assert row["client_id"] == KEY, row
    assert row["symbol"] == "EURUSD", row
    assert row["attempts"] == 1, (
        "attempts must count the EARLIER unresolved send, so the row says which "
        f"attempt this is: {row}"
    )
    assert isinstance(row["first_at"], (int, float)) and row["first_at"] > 0, (
        f"first_at must say when the outstanding send began: {row}"
    )
    assert "reason" not in row, (
        "this event carries no named reason by design; adding one puts it in the "
        "`reject` vocabulary's population without a row in the contract table"
    )
    engine.stop()


def test_the_refusal_text_names_the_same_send_as_the_row(tmp_path: Path) -> None:
    """The prose and the record must name the SAME send, or neither is usable.

    The comment is what an operator reads in the terminal; the row is what a
    reconciliation reads afterwards. A `client_id` in one and not the other is
    two accounts of one event.
    """
    _leave_an_unresolved_send(tmp_path)
    engine = _engine(tmp_path, CountingTransport())
    result = engine.submit(_signal(), 0.01, KEY)
    assert KEY in (result.comment or ""), (
        f"the refusal text does not name the send: {result.comment!r}"
    )
    assert _rows(engine)[0]["client_id"] == KEY
    engine.stop()


def test_a_different_key_is_not_refused(tmp_path: Path) -> None:
    """THE CONTROL. A guard that refuses everything is not a guard.

    Without this, an unconditional refusal would satisfy every assertion above
    while blocking every order on the desk.
    """
    _leave_an_unresolved_send(tmp_path)
    engine = _engine(tmp_path, CountingTransport())
    result = engine.submit(_signal(), 0.01, "A-DIFFERENT-KEY")
    assert result.transmitted is True, (
        "an unrelated send was refused, so the ledger key is not being compared"
    )
    assert len(_rows(engine)) == 0, "an unrelated send wrote a refusal row"
    engine.stop()


def test_the_refusal_reaches_the_journal_and_never_the_chat(tmp_path: Path) -> None:
    """The invariant the issue asked about, measured rather than argued.

    TWO independent reasons keep it quiet, and this asserts the binding one.
    The event is in neither `ALWAYS_NOTIFY_EVENTS` nor the default allowlist,
    AND `_format_event` returns `""` for it while `_emit` notifies only on
    non-empty text. So the event is allowlisted EXPLICITLY here and the chat
    must STILL see nothing: that pins the formatter rather than the allowlist,
    which is what would otherwise let an arm added to `_format_event` later
    start broadcasting a refusal silently.
    """
    assert EVENT not in ALWAYS_NOTIFY_EVENTS
    assert EVENT not in TelegramClient(token="t", chat_id="1").notify_events
    assert _format_event(EVENT, {"client_id": KEY, "symbol": "EURUSD"}) == "", (
        "the formatter now renders this event, so the allowlist is the only "
        "thing keeping a refusal out of the chat"
    )

    _leave_an_unresolved_send(tmp_path)
    transport = CountingTransport()
    engine = _engine(tmp_path, transport, allow_event=True)
    before = len(transport.sent)
    engine.submit(_signal(), 0.01, KEY)

    assert len(transport.sent) == before, (
        "the refusal was broadcast into the chat even though the journal-only "
        f"invariant says it is journalled and not echoed: {transport.sent[before:]!r}"
    )
    assert len(_rows(engine)) == 1, "and it must still be in the journal"
    engine.stop()
