"""flatten must fail loudly: survivors counted, named, alerted, never marked seen.

Issue #9. The circuit breaker's failure mode was silence on the path that runs
when the account is already losing money. Every test here asserts the reason and
the named text, not an exit status, and prints its own denominator
(requested / confirmed closed / survivors) so a count that stops moving is visible.
"""

from __future__ import annotations

import json
from pathlib import Path

from straightedge.broker.mt4_live import BridgeTimeout
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig, SessionConfig
from straightedge.constants import (
    TRADE_RETCODE_DONE_PARTIAL,
    TRADE_RETCODE_POSITION_CLOSED,
    TRADE_RETCODE_REJECT,
)
from straightedge.engine import Engine
from straightedge.models import MarketOrder, OrderResult, Side, WorkingOrder
from straightedge.synthetic import generate_bars
from straightedge.telegram import TelegramClient, TgCommand

MAGIC = BotConfig().risk.magic


class FakeTransport:
    """Records every outbound Telegram payload. The named alert path ends here."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None) -> dict:
        del timeout, headers
        self.sent.append((url, payload))
        if url.endswith("/getUpdates"):
            out = self.updates
            self.updates = []
            return {"ok": True, "result": out}
        return {"ok": True, "result": {"message_id": len(self.sent)}}

    def texts(self) -> list[str]:
        return [p.get("text", "") for _, p in self.sent]


class SweepBroker:
    """PaperBroker with a scriptable flatten sweep.

    close_reject      ticket -> the close is NOT executed and a reject is returned.
    close_partial     ticket -> only this volume is really closed; the result claims
                      DONE_PARTIAL, which OrderResult.ok reports as True.
    close_raise       ticket -> close_position raises, mid sweep.
    close_vanished    ticket -> an SL/TP filled between the read and our close: the
                      position really goes away and we get POSITION_CLOSED back.
    cancel_reject     order ticket -> the cancel is NOT executed.
    cancel_raise      order ticket -> cancel RAISES, the shape a mailbox timeout
                      has. Distinct from cancel_reject: a reject is a VERDICT
                      from the venue, a raise is COULD NOT MEASURE, and the
                      order may still be resting on the book (#264).
    hide_after        after this many positions() calls, report no positions
                      (a broker whose post-sweep read is wrong).
    raise_after       after this many positions() calls, raise (COULD NOT MEASURE).
    """

    def __init__(self, inner: PaperBroker) -> None:
        self._inner = inner
        self.close_reject: set[int] = set()
        self.close_partial: dict[int, float] = {}
        self.close_raise: set[int] = set()
        self.close_vanished: set[int] = set()
        self.cancel_reject: set[int] = set()
        self.cancel_raise: set[int] = set()
        self.hide_after: int | None = None
        self.raise_after: int | None = None
        self.positions_calls = 0
        self.close_calls: list[tuple[int, float]] = []
        self.cancel_calls: list[int] = []

    def arm(self, *, hide_after: int | None = None, raise_after: int | None = None) -> None:
        self.positions_calls = 0
        self.hide_after = hide_after
        self.raise_after = raise_after

    def positions(self, magic: int | None = None):
        self.positions_calls += 1
        if self.raise_after is not None and self.positions_calls > self.raise_after:
            raise RuntimeError("positions_get failed: IPC timeout")
        if self.hide_after is not None and self.positions_calls > self.hide_after:
            return []
        return self._inner.positions(magic=magic)

    def close_position(self, ticket: int, **kw):
        self.close_calls.append((ticket, float(kw.get("volume", 0.0))))
        if ticket in self.close_raise:
            raise RuntimeError("order_send failed: IPC timeout")
        if ticket in self.close_reject:
            return OrderResult(retcode=TRADE_RETCODE_REJECT, comment="broker refused")
        if ticket in self.close_vanished:
            gone = self._inner.close_position(ticket, **kw)
            assert gone.ok, "vanish script must really remove the position"
            return OrderResult(retcode=TRADE_RETCODE_POSITION_CLOSED, comment="gone")
        if ticket in self.close_partial:
            part = self.close_partial[ticket]
            done = self._inner.close_position(ticket, **{**kw, "volume": part})
            assert done.ok, "partial script must really close its slice"
            return OrderResult(
                retcode=TRADE_RETCODE_DONE_PARTIAL,
                comment="Done partially",
                volume=part,
                price=done.price,
            )
        return self._inner.close_position(ticket, **kw)

    def cancel(self, ticket: int):
        self.cancel_calls.append(ticket)
        if ticket in self.cancel_raise:
            # The REAL class the mailbox bridge raises, not a stand-in: the
            # sweep's arm is `except (RuntimeError, OSError, ValueError)` and
            # `BridgeTimeout` is a `RuntimeError` subclass, so using the real
            # one proves the real exception reaches the real handler.
            raise BridgeTimeout("mt4 bridge timeout")
        if ticket in self.cancel_reject:
            return OrderResult(retcode=TRADE_RETCODE_REJECT, comment="cancel refused")
        return self._inner.cancel(ticket)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _paper() -> PaperBroker:
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(10, start=1.10, drift=0.0, vol=0.0001, seed=1))
    return broker


def _open(broker: PaperBroker, volume: float = 0.10) -> int:
    tick = broker.tick("EURUSD")
    spec = broker.symbol("EURUSD")
    res = broker.market(
        MarketOrder(
            symbol="EURUSD",
            side=Side.BUY,
            volume=volume,
            sl=spec.normalize_price(tick.ask - 0.005),
            tp=spec.normalize_price(tick.ask + 0.010),
            magic=MAGIC,
        )
    )
    assert res.ok, f"setup open failed: {res.retcode} {res.comment}"
    return res.order


def _pending(broker: PaperBroker) -> int:
    tick = broker.tick("EURUSD")
    spec = broker.symbol("EURUSD")
    price = spec.normalize_price(tick.ask - 0.002)
    res = broker.working(
        WorkingOrder(
            symbol="EURUSD",
            side=Side.BUY,
            kind="limit",
            volume=0.10,
            price=price,
            sl=spec.normalize_price(price - 0.005),
            tp=spec.normalize_price(price + 0.010),
            magic=MAGIC,
        )
    )
    assert res.ok, f"setup pending failed: {res.retcode} {res.comment}"
    return res.order


def _engine(tmp_path: Path, broker, *, telegram: TelegramClient | None = None) -> Engine:
    cfg = BotConfig()
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    return Engine(cfg, broker, halt_dir=str(tmp_path), telegram=telegram)


def _events(tmp_path: Path, name: str) -> list[dict]:
    path = tmp_path / "j.jsonl"
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        if rec.get("event") == name:
            out.append(rec)
    return out


def _denominator(report) -> str:
    if report is None:
        return "NO REPORT: flatten returned None, so there is no denominator at all"
    return (
        f"requested={report.positions_requested} "
        f"confirmed_closed={report.positions_confirmed_closed} "
        f"closed_elsewhere={report.positions_closed_elsewhere} "
        f"survivors={list(report.survivors)} "
        f"orders_requested={report.orders_requested} "
        f"orders_cancelled={report.orders_confirmed_cancelled} "
        f"order_survivors={list(report.order_survivors)} "
        f"measured={report.measured}"
    )


def _format_incomplete_text(tmp_path: Path) -> str:
    from straightedge.engine import _format_event

    ev = _events(tmp_path, "flatten_incomplete")[0]
    fields = {k: v for k, v in ev.items() if k not in ("ts", "event")}
    return _format_event("flatten_incomplete", fields)


# --- 1. a failed close must not mark the survivor seen -----------------------


def test_failed_close_leaves_survivor_alertable(tmp_path: Path) -> None:
    inner = _paper()
    broker = SweepBroker(inner)
    doomed = _open(inner)
    survivor = _open(inner)
    broker.close_reject.add(survivor)
    engine = _engine(tmp_path, broker)
    engine.start()
    broker.arm()

    report = engine.flatten("daily_loss")
    print("DENOMINATOR:", _denominator(report))

    # The defect: the survivor of a failed sweep was folded into _seen_pos and
    # never alerted again. _detect_fills computes appeared = now - _seen_pos.
    assert engine._seen_pos is not None
    assert survivor not in engine._seen_pos, (
        f"survivor #{survivor} marked already-seen; it can never alert again. "
        f"_seen_pos={sorted(engine._seen_pos)}"
    )
    assert not (set(report.survivors) & engine._seen_pos), "no survivor may be seen"

    # Denominator, and it is an identity, not a vibe.
    assert report.positions_requested == 2
    assert report.positions_confirmed_closed == 1
    assert report.survivors == (survivor,)
    assert (
        report.positions_confirmed_closed
        + report.positions_closed_elsewhere
        + len([t for t in report.survivors if t in (doomed, survivor)])
        == report.positions_requested
    )
    assert not report.complete

    # The halt is not the bug. It must still happen.
    assert engine.halted

    ev = _events(tmp_path, "flatten_incomplete")
    assert len(ev) == 1, "exactly one incomplete alert per failed sweep"
    assert ev[0]["survivor_count"] == 1
    assert ev[0]["survivors"] == [survivor]
    assert ev[0]["reason"] == "daily_loss"
    assert ev[0]["requested"] == 2
    assert ev[0]["confirmed_closed"] == 1


# --- 2. DONE_PARTIAL is not a close ----------------------------------------


def test_partial_close_is_not_counted_as_closed(tmp_path: Path) -> None:
    inner = _paper()
    broker = SweepBroker(inner)
    ticket = _open(inner, volume=0.10)
    broker.close_partial[ticket] = 0.05
    engine = _engine(tmp_path, broker)
    engine.start()
    broker.arm()

    report = engine.flatten("max_drawdown")
    print("DENOMINATOR:", _denominator(report))

    # RETCODE_OK contains DONE_PARTIAL, so result.ok is True here on purpose.
    assert report.positions_requested == 1
    assert report.positions_confirmed_closed == 0, (
        "a partial fill left residual volume open; ok=True must not count as closed"
    )
    assert report.survivors == (ticket,)
    assert not report.complete
    assert ticket not in (engine._seen_pos or set())

    left = inner.positions(magic=MAGIC)
    assert len(left) == 1 and abs(left[0].volume - 0.05) < 1e-12

    ev = _events(tmp_path, "flatten_incomplete")
    assert len(ev) == 1
    assert ev[0]["survivor_count"] == 1
    assert ev[0]["residual"] == [ticket]


def test_partial_close_caught_even_when_broker_read_hides_residual(tmp_path: Path) -> None:
    """Two instruments, not one. Adding the volume check can only raise the count."""
    inner = _paper()
    broker = SweepBroker(inner)
    ticket = _open(inner, volume=0.10)
    broker.close_partial[ticket] = 0.05
    engine = _engine(tmp_path, broker)
    engine.start()
    # Pre-sweep read is call 1 and must be truthful; every later read reports none.
    broker.arm(hide_after=1)

    report = engine.flatten("daily_loss")
    print("DENOMINATOR:", _denominator(report))

    assert report.survivors == (ticket,), (
        "the post-sweep read said flat; only the volume comparison can see this"
    )
    assert report.positions_confirmed_closed == 0
    assert not report.complete
    assert _events(tmp_path, "flatten_incomplete")


# --- 3. /halt reports the actual outcome -----------------------------------


def test_halt_command_reports_survivor_count_not_a_fixed_string(tmp_path: Path) -> None:
    inner = _paper()
    broker = SweepBroker(inner)
    survivor = _open(inner)
    broker.close_reject.add(survivor)
    tr = FakeTransport()
    tg = TelegramClient(token="t", chat_id="1", transport=tr)
    engine = _engine(tmp_path, broker, telegram=tg)
    engine.start()
    broker.arm()

    reply = engine.handle_command(TgCommand("1", 1, "/halt", 1))
    print("HALT REPLY:", reply)

    assert reply != "flattened and halted. /resume clears the operator HALT file."
    assert "FLATTEN INCOMPLETE: 1 still open" in reply, reply
    assert str(survivor) in reply, reply
    assert "HALTED" in reply, reply
    assert engine.halted
    assert (tmp_path / "HALT").exists()


def test_halt_command_reports_a_clean_sweep_with_its_counts(tmp_path: Path) -> None:
    inner = _paper()
    broker = SweepBroker(inner)
    _open(inner)
    _open(inner)
    engine = _engine(tmp_path, broker)
    engine.start()
    broker.arm()

    reply = engine.handle_command(TgCommand("1", 1, "/halt", 1))
    print("HALT REPLY:", reply)

    assert "flattened" in reply
    assert "INCOMPLETE" not in reply
    assert "2/2" in reply, f"the reply must carry its denominator: {reply}"
    assert broker.positions(magic=MAGIC) == []
    assert not _events(tmp_path, "flatten_incomplete")


# --- 4. the alert reaches the human by a named path ------------------------


def test_incomplete_flatten_alert_reaches_telegram_transport(tmp_path: Path) -> None:
    """Named path: Engine._emit -> _format_event -> TelegramClient.notify -> send -> sendMessage."""
    inner = _paper()
    broker = SweepBroker(inner)
    survivor = _open(inner)
    broker.close_reject.add(survivor)
    tr = FakeTransport()
    tg = TelegramClient(token="t", chat_id="1", transport=tr)
    engine = _engine(tmp_path, broker, telegram=tg)
    engine.start()
    broker.arm()

    engine.flatten("max_drawdown")
    texts = tr.texts()
    print("TELEGRAM TEXTS:", texts)

    hits = [t for t in texts if "FLATTEN INCOMPLETE" in t]
    assert len(hits) == 1, f"expected one incomplete alert, got {texts}"
    assert "FLATTEN INCOMPLETE: 1 still open" in hits[0]
    assert f"#{survivor}" in hits[0]
    assert "max_drawdown" in hits[0]


def test_incomplete_alert_is_not_silenced_by_an_operator_notify_filter(tmp_path: Path) -> None:
    """Every existing config.toml lists notify_events without this event name."""
    inner = _paper()
    broker = SweepBroker(inner)
    survivor = _open(inner)
    broker.close_reject.add(survivor)
    tr = FakeTransport()
    tg = TelegramClient(
        token="t",
        chat_id="1",
        transport=tr,
        notify_events=frozenset({"start", "stop", "open", "close", "halt"}),
    )
    engine = _engine(tmp_path, broker, telegram=tg)
    engine.start()
    broker.arm()

    engine.flatten("daily_loss")
    assert any("FLATTEN INCOMPLETE" in t for t in tr.texts()), tr.texts()


# --- 5. could not measure is an incomplete sweep, never a clean one --------


def test_unreadable_post_sweep_read_is_incomplete(tmp_path: Path) -> None:
    inner = _paper()
    broker = SweepBroker(inner)
    a = _open(inner)
    b = _open(inner)
    engine = _engine(tmp_path, broker)
    engine.start()
    broker.arm(raise_after=1)

    report = engine.flatten("daily_loss")
    print("DENOMINATOR:", _denominator(report))

    assert report.measured is False
    assert not report.complete
    assert set(report.survivors) == {a, b}, (
        "an unreadable broker response is COULD NOT MEASURE, which for a flatten "
        "is an incomplete sweep"
    )
    assert engine.halted
    assert not (engine._seen_pos or set())
    ev = _events(tmp_path, "flatten_incomplete")
    assert len(ev) == 1
    assert ev[0]["measured"] is False
    assert ev[0]["survivor_count"] == 2


def test_close_that_raises_does_not_abort_the_sweep(tmp_path: Path) -> None:
    inner = _paper()
    broker = SweepBroker(inner)
    first, second, third = _open(inner), _open(inner), _open(inner)
    broker.close_raise.add(second)
    engine = _engine(tmp_path, broker)
    engine.start()
    broker.arm()

    report = engine.flatten("daily_loss")
    print("DENOMINATOR:", _denominator(report))

    tried = [t for t, _ in broker.close_calls]
    assert tried == [first, second, third], f"sweep stopped early: {tried}"
    assert report.survivors == (second,)
    assert report.positions_confirmed_closed == 2
    assert engine.halted, "an exception mid-sweep must still stop new entries"


# --- 6. pending orders have the same discarded-result shape ----------------


def test_failed_cancel_is_reported(tmp_path: Path) -> None:
    inner = _paper()
    broker = SweepBroker(inner)
    order = _pending(inner)
    broker.cancel_reject.add(order)
    engine = _engine(tmp_path, broker)
    engine.start()
    broker.arm()

    report = engine.flatten("daily_loss")
    print("DENOMINATOR:", _denominator(report))

    assert report.orders_requested == 1
    assert report.orders_confirmed_cancelled == 0
    assert report.order_survivors == (order,)
    assert not report.complete
    ev = _events(tmp_path, "flatten_incomplete")
    assert len(ev) == 1
    assert ev[0]["order_survivors"] == [order]
    assert "1 working order" in _format_incomplete_text(tmp_path)


def test_cancel_that_raises_does_not_abort_the_sweep(tmp_path: Path) -> None:
    """A cancel that could not be MEASURED is residual exposure. #264.

    The twin of `test_close_that_raises_does_not_abort_the_sweep` for the
    pending half, and the distinction from `test_failed_cancel_is_reported`
    one function up is the whole point: a REJECT is a verdict from the venue,
    so the order is known to still be resting; a RAISE is COULD NOT MEASURE,
    and the order may or may not have been pulled. The sweep must treat both
    as survivors, and it must say which happened, because only one of them
    means the venue was reachable.

    Why this needed a double that can fail: straightedge#264 measured `cancel`
    as one of three seams whose only doubles could not raise, so the sweep's
    `except (RuntimeError, OSError, ValueError)` arm around `broker.cancel`
    was never taken by any test. `flatten` is the SAFETY path, reached by
    daily-loss and drawdown, and `flatten_incomplete` is the one event in
    `telegram.ALWAYS_NOTIFY_EVENTS`, so a sweep that silently reported clean
    here is the worst available failure.

    Two orders, and the raise is on the FIRST, so "did the sweep stop early"
    is observable rather than assumed.
    """
    inner = _paper()
    broker = SweepBroker(inner)
    first, second = _pending(inner), _pending(inner)
    broker.cancel_raise.add(first)
    engine = _engine(tmp_path, broker)
    engine.start()
    broker.arm()

    report = engine.flatten("daily_loss")
    print("DENOMINATOR:", _denominator(report))

    assert broker.cancel_calls == [first, second], (
        f"the sweep stopped at the raising cancel: {broker.cancel_calls}"
    )
    assert report.orders_requested == 2
    assert report.orders_confirmed_cancelled == 1, (
        "the second order was reachable and must still be counted cancelled"
    )
    assert report.order_survivors == (first,)
    assert not report.complete, (
        "a cancel nobody could measure was reported as a clean sweep"
    )
    assert engine.halted, "an exception mid-sweep must still stop new entries"

    failed = _events(tmp_path, "cancel_failed")
    assert len(failed) == 1, f"cancel_failed rows: {failed}"
    row = failed[0]
    assert row["ticket"] == first
    # COULD NOT MEASURE carries the exception; a VERDICT carries a retcode.
    # Keying the operator's record on which arm ran is what separates "the
    # venue refused" from "we never heard back".
    assert row.get("error"), f"the raise left no error on the record: {row}"
    assert "retcode" not in row, (
        "a cancel that raised reported a retcode, which claims a venue verdict "
        "that never arrived"
    )
    assert len(_events(tmp_path, "flatten_incomplete")) == 1


# --- 7. the clean path still records its denominator -----------------------


def test_clean_flatten_records_the_full_denominator(tmp_path: Path) -> None:
    inner = _paper()
    broker = SweepBroker(inner)
    a = _open(inner)
    b = _open(inner)
    order = _pending(inner)
    engine = _engine(tmp_path, broker)
    engine.start()
    broker.arm()

    report = engine.flatten("telegram")
    print("DENOMINATOR:", _denominator(report))

    assert report.complete
    assert report.positions_requested == 2
    assert report.positions_confirmed_closed == 2
    assert report.survivors == ()
    assert report.orders_requested == 1
    assert report.orders_confirmed_cancelled == 1
    assert report.order_survivors == ()
    assert sorted(t for t, _ in broker.close_calls) == sorted([a, b])
    assert broker.cancel_calls == [order]

    rec = _events(tmp_path, "flatten")
    assert len(rec) == 1, "the clean sweep is recorded too; the count needs a floor"
    assert rec[0]["requested"] == 2
    assert rec[0]["confirmed_closed"] == 2
    assert rec[0]["survivor_count"] == 0
    assert not _events(tmp_path, "flatten_incomplete")


def test_position_closed_elsewhere_is_not_a_false_alarm(tmp_path: Path) -> None:
    """SL hit between the read and the sweep: gone, never confirmed by us, no alarm."""
    inner = _paper()
    broker = SweepBroker(inner)
    ticket = _open(inner)
    # The race has to land between flatten's pre-sweep read and its close call,
    # otherwise the read never sees the position and the denominator is 0, not 1.
    broker.close_vanished.add(ticket)
    engine = _engine(tmp_path, broker)
    engine.start()
    broker.arm()

    report = engine.flatten("daily_loss")
    print("DENOMINATOR:", _denominator(report))

    assert report.positions_requested == 1
    assert report.positions_confirmed_closed == 0
    assert report.positions_closed_elsewhere == 1
    assert report.survivors == ()
    assert report.complete, "no residual exposure and no alarm fatigue"
    assert not _events(tmp_path, "flatten_incomplete")
