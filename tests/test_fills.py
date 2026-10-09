from dataclasses import replace
from datetime import datetime, timezone

from straightedge.broker.paper import PaperBroker, default_spec
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.models import Bar
from straightedge.sizing import money_per_lot_at_stop
from straightedge.synthetic import generate_bars
from straightedge.telegram import TelegramClient, TgCommand


class FakeTransport:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict]] = []
        self.updates: list[dict] = []

    def post_json(self, url: str, payload: dict, timeout: float = 10.0, headers=None) -> dict:
        del timeout
        self.sent.append((url, payload))
        if url.endswith("/getUpdates"):
            result = self.updates
            self.updates = []
            return {"ok": True, "result": result}
        return {"ok": True, "result": {"message_id": 1}}


def _engine(tmp_path, *, telegram=None) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.strategy.auto = False
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    broker.seed_bars("GBPUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=4))
    return Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        telegram=telegram,
        now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
    )


def _push_close(engine: Engine, symbol: str, close: float, dt: int = 3600) -> None:
    last = engine.broker.rates(symbol, engine.cfg.strategy.timeframe_id, 1)[-1]
    bars = engine.broker.rates(symbol, engine.cfg.strategy.timeframe_id, 200)
    spec = engine.broker.symbol(symbol)
    px = spec.normalize_price(close)
    engine.broker.seed_bars(
        symbol,
        bars
        + [
            Bar(
                time=last.time + dt,
                open=px,
                high=max(px, last.close),
                low=min(px, last.close),
                close=px,
            )
        ],
    )


def test_limit_buy_stages_pending_then_fills(tmp_path) -> None:
    tr = FakeTransport()
    tg = TelegramClient(
        token="t",
        chat_id="1",
        transport=tr,
        notify_events=frozenset({"start", "stop", "open", "close", "halt", "pending"}),
    )
    engine = _engine(tmp_path, telegram=tg)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl = spec.normalize_price(limit - 0.005)
    tp = spec.normalize_price(limit + 0.010)
    reply = engine.handle_command(
        TgCommand("1", 1, f"/buy EURUSD limit={limit} sl={sl} tp={tp}", 1)
    )
    assert "confirm buy EURUSD" in reply
    assert "limit" in reply
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert sent.startswith("sent")
    assert engine.broker.orders()
    assert not engine.broker.positions()
    listed = engine.handle_command(TgCommand("1", 1, "/orders", 3))
    assert "EURUSD" in listed
    ticket = engine.broker.orders()[0].ticket
    assert str(ticket) in listed
    _push_close(engine, "EURUSD", limit - 0.001)
    engine.step_all()
    assert engine.broker.orders() == []
    assert engine.broker.positions()
    texts = [payload.get("text", "") for _, payload in tr.sent]
    blob = " ".join(texts)
    assert "PENDING" in blob or "FILL" in blob or "OPEN" in blob
    engine.stop()


def test_cancel_ticket_removes_pending(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    limit = tick.ask - 0.002
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD limit={limit}", 1))
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert sent.startswith("sent")
    ticket = engine.broker.orders()[0].ticket
    reply = engine.handle_command(TgCommand("1", 1, f"/cancel {ticket}", 3))
    assert "cancelled" in reply
    assert str(ticket) in reply
    assert engine.broker.orders() == []
    engine.stop()


def test_halt_cancels_pending_and_positions(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert sent.startswith("sent")
    assert engine.broker.positions()
    tick = engine.broker.tick("GBPUSD")
    limit = tick.ask - 0.002
    engine.handle_command(TgCommand("1", 1, f"/buy GBPUSD limit={limit}", 3))
    sent2 = engine.handle_command(TgCommand("1", 1, "/confirm", 4))
    assert sent2.startswith("sent")
    assert engine.broker.orders()
    engine.handle_command(TgCommand("1", 1, "/halt", 5))
    assert engine.broker.orders() == []
    assert not engine.broker.positions()
    engine.stop()


def test_limit_and_stop_together_refused(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD limit=1.0 stop=1.2", 1))
    assert "not both" in reply
    engine.stop()


def test_market_sl_notifies_journal_and_telegram(tmp_path) -> None:
    tr = FakeTransport()
    tg = TelegramClient(
        token="t",
        chat_id="1",
        transport=tr,
        notify_events=frozenset({"start", "stop", "open", "close", "halt", "pending"}),
    )
    engine = _engine(tmp_path, telegram=tg)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert sent.startswith("sent")
    pos = engine.broker.positions()[0]
    _push_close(engine, "EURUSD", pos.sl - 0.005)
    engine.step_all()
    assert not engine.broker.positions()
    events = [rec.get("event") for rec in engine.journal.tail(30)]
    assert "close" in events or "fill" in events
    texts = [payload.get("text", "") for _, payload in tr.sent]
    blob = " ".join(texts).lower()
    assert "close" in blob or "fill" in blob
    engine.stop()


def test_step_all_fill_then_stop(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    limit = tick.ask - 0.01
    sl = limit - 0.01
    tp = limit + 0.015
    staged = engine.handle_command(
        TgCommand("1", 1, f"/buy EURUSD limit={limit} sl={sl} tp={tp}", 1)
    )
    assert "confirm buy EURUSD" in staged
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert sent.startswith("sent")
    assert engine.broker.orders()
    assert not engine.broker.positions()
    _push_close(engine, "EURUSD", limit - 0.002)
    engine.step_all()
    assert engine.broker.orders() == []
    assert engine.broker.positions()
    pos = engine.broker.positions()[0]
    _push_close(engine, "EURUSD", pos.sl - 0.005, dt=7200)
    engine.step_all()
    assert not engine.broker.positions()
    engine.stop()


def test_sell_stop_and_buy_limit_price_rules(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    assert "below ask" in engine.handle_command(
        TgCommand("1", 1, f"/buy EURUSD limit={tick.ask + 0.01}", 1)
    )
    assert "above ask" in engine.handle_command(
        TgCommand("1", 1, f"/buy EURUSD stop={tick.ask - 0.01}", 2)
    )
    assert "above bid" in engine.handle_command(
        TgCommand("1", 1, f"/sell EURUSD limit={tick.bid - 0.01}", 3)
    )
    assert "below bid" in engine.handle_command(
        TgCommand("1", 1, f"/sell EURUSD stop={tick.bid + 0.01}", 4)
    )
    engine.stop()


def test_replace_pending_price(tmp_path) -> None:
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
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert sent.startswith("sent")
    order = engine.broker.orders()[0]
    new_px = spec.normalize_price(limit - 0.001)
    reply = engine.handle_command(TgCommand("1", 1, f"/replace {order.ticket} {new_px}", 3))
    assert f"replace #{order.ticket}" in reply
    assert abs(engine.broker.orders()[0].price - new_px) < spec.point
    assert not engine.broker.positions()
    too_high = spec.normalize_price(tick.ask + 0.001)
    bad = engine.handle_command(TgCommand("1", 1, f"/replace {order.ticket} {too_high}", 4))
    assert "below ask" in bad
    # GBPUSD, not EURUSD: a working order on EURUSD is committed exposure and
    # `already_in_symbol` now refuses a market entry on top of it. This step only
    # needs A position, on any symbol, to check that `/replace` refuses one.
    engine.handle_command(TgCommand("1", 1, "/buy GBPUSD", 5))
    engine.handle_command(TgCommand("1", 1, "/confirm", 6))
    pos = engine.broker.positions()[0]
    assert "working orders" in engine.handle_command(
        TgCommand("1", 1, f"/replace {pos.ticket} {new_px}", 7)
    )
    assert "usage" in engine.handle_command(TgCommand("1", 1, "/replace", 8))
    engine.stop()


def test_replace_pending_risk_and_halt(tmp_path) -> None:
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
    wider = spec.normalize_price(limit + 0.001)
    assert wider < tick.ask
    risked = engine.handle_command(
        TgCommand("1", 1, f"/replace {order.ticket} {wider}", 3)
    )
    # The ENGINE /replace guard, not RiskManager: risk.py carries the same literal
    # on a line that cannot execute. See tests/test_refusal_reasons.py (issue 11).
    assert "size_exceeds_risk" in risked
    engine.risk.write_halt_file("operator")
    new_px = spec.normalize_price(limit - 0.001)
    halted = engine.handle_command(
        TgCommand("1", 1, f"/replace {order.ticket} {new_px}", 4)
    )
    assert "refused" in halted
    engine.stop()


def test_orders_empty_message(tmp_path) -> None:
    engine = _engine(tmp_path)
    engine.start()
    assert "no pending" in engine.handle_command(TgCommand("1", 1, "/orders", 1))
    engine.stop()


def test_sl_tp_modify_working_order(tmp_path) -> None:
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
    sent = engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert sent.startswith("sent")
    order = engine.broker.orders()[0]
    new_sl = spec.normalize_price(limit - 0.003)
    new_tp = spec.normalize_price(limit + 0.012)
    sl_reply = engine.handle_command(TgCommand("1", 1, f"/sl {order.ticket} {new_sl}", 3))
    assert f"sl #{order.ticket}" in sl_reply
    assert abs(engine.broker.orders()[0].sl - new_sl) < spec.point
    tp_reply = engine.handle_command(TgCommand("1", 1, f"/tp {order.ticket} {new_tp}", 4))
    assert f"tp #{order.ticket}" in tp_reply
    assert abs(engine.broker.orders()[0].tp - new_tp) < spec.point
    assert not engine.broker.positions()
    bad = engine.handle_command(
        TgCommand("1", 1, f"/sl {order.ticket} {spec.normalize_price(limit + 0.001)}", 5)
    )
    assert "sl #" not in bad
    assert "sl < entry" in bad or "fail" in bad.lower()
    engine.stop()


def test_utc_day_roll_sends_recap_not_a_trade(tmp_path) -> None:
    tr = FakeTransport()
    tg = TelegramClient(
        token="t",
        chat_id="1",
        transport=tr,
        notify_events=frozenset(
            {"start", "stop", "open", "close", "halt", "pending", "recap"}
        ),
    )
    clock = [datetime(2024, 1, 3, 12, tzinfo=timezone.utc)]
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.strategy.auto = False
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    engine = Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        telegram=tg,
        now_fn=lambda: clock[0],
    )
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    npos = len(engine.broker.positions())
    clock[0] = datetime(2024, 1, 4, 0, 5, tzinfo=timezone.utc)
    engine.step_all()
    assert len(engine.broker.positions()) == npos
    texts = [payload.get("text", "") for _, payload in tr.sent]
    blob = "\n".join(texts)
    assert "RECAP 2024-01-03" in blob
    assert "day_start=" in blob
    recaps = [t for t in texts if t.startswith("RECAP")]
    assert len(recaps) == 1
    engine.step_all()
    texts2 = [payload.get("text", "") for _, payload in tr.sent]
    recaps2 = [t for t in texts2 if t.startswith("RECAP")]
    assert len(recaps2) == 1
    engine.stop()


def test_replace_pending_refuses_for_lack_of_loss_room(tmp_path) -> None:
    """A replacement must not re-risk money the day no longer has (issue #104).

    `risk.evaluate` measures a NEW order against `min(per_trade, loss_room)`,
    and `_stop_guard` measures a widening on an OPEN position against the same
    pair. `replace_pending` measured a WORKING order against the per-trade half
    only, so a replacement could commit risk the daily-loss and drawdown
    budgets no longer had room for.

    THE WINDOW IS NOT THE ONE THE ISSUE DESCRIBES, and the difference is why
    this test sets the state it does. #104 reasoned that "once the day's budget
    is spent, loss_room is negative and replace_pending still allows a
    replacement". That state is unreachable: `circuit` trips `daily_loss` at
    `daily_loss >= day_start_equity * daily_loss_pct` and `loss_room` is that
    same comparison rearranged, so room reaches zero exactly when the circuit
    trips, and `replace_pending`'s own `circuit_reason` check already refuses
    there. `loss_room`'s docstring says so: "Positive whenever the circuit is
    clear".

    The reachable window is room POSITIVE but SMALLER than the per-trade cap:
    the day has spent most of its budget, no halt gate has tripped, and a
    replacement sized inside the per-trade cap still does not fit in what is
    left. Measured here: $13.80 of room, a $50 per-trade cap, and a
    replacement risking $40 that this guard accepted before the fix.
    """
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl = spec.normalize_price(limit - 0.005)
    tp = spec.normalize_price(limit + 0.010)
    # THE RESTING ORDER IS SIZED BELOW TODAY'S CAP ON PURPOSE (#164).
    #
    # These fixtures used to size the order AT the per-trade cap and propose
    # `limit - 0.001`, which moves the entry TOWARD the stop: worst 50.00 ->
    # 40.00, a REDUCTION. So the #104 pin asserted that a strictly
    # risk-REDUCING replacement is refused, which is the behaviour #164 ruled
    # wrong, and the increase direction #104 actually described was never
    # pinned. Direction was not a variable when this was written.
    #
    # Flipping the direction alone does not work: an order sized AT the cap has
    # an EMPTY band for "an increase that still fits the per-trade cap", since
    # any increase on it breaches `per_trade` too and the pre-#157 arithmetic
    # would refuse it as well. Same structural fact as #170's scaled-out
    # position. So the order is sized at 0.2% to leave the band open and the cap
    # is restored to 0.5% before the replacement is measured.
    engine.cfg.risk.risk_pct = 0.002
    engine.handle_command(
        TgCommand("1", 1, f"/buy EURUSD limit={limit} sl={sl} tp={tp}", 1)
    )
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    order = engine.broker.orders()[0]
    engine.cfg.risk.risk_pct = 0.005
    worst_resting = money_per_lot_at_stop(order.price, order.sl, spec) * order.volume

    account = engine.broker.account()
    r = engine.cfg.risk
    per_trade = account.equity * r.risk_pct * r.max_risk_multiple

    # The day opened higher than the account now stands, so most of the
    # daily-loss budget is spent. Written on the PERSISTED snapshot, which is
    # the state the sizer never sees and the only thing moved here; `observe`
    # rewrites day_start_equity only when the UTC day_key changes, so this
    # survives the `circuit_reason` call inside replace_pending.
    engine.risk.snapshot.day_start_equity = 10_190.0
    room = engine.risk.loss_room(account)

    # The window has to BE a window, or this test proves nothing: the circuit
    # must be clear (otherwise the existing circuit_reason check is what
    # refuses, not the cap) and the room must be positive but tighter than the
    # per-trade cap (otherwise min() picks per_trade and the two forms agree).
    assert engine.risk.circuit_reason(account, engine.now_fn()) == ""
    assert 0 < room < per_trade, f"no window: room={room} per_trade={per_trade}"

    # Entry moved AWAY from the stop, so this is an INCREASE: the direction
    # #104 is about and the one #164's carve-out must never cover.
    new_px = spec.normalize_price(limit + 0.0015)
    worst = money_per_lot_at_stop(new_px, order.sl, spec) * order.volume
    assert worst > worst_resting + 1e-6, (
        f"worst={worst} does not exceed the resting {worst_resting}; "
        "#164's carve-out would allow this regardless of the cap"
    )
    # The refusal must come from the loss-room term ALONE. If `worst` also
    # breached the per-trade cap, the old arithmetic would refuse too and this
    # test could not tell the fixed guard from the broken one.
    assert worst <= per_trade + 1e-6, f"worst={worst} already breaches per_trade={per_trade}"
    assert worst > room, f"worst={worst} fits in room={room}; nothing to refuse"

    reply = engine.handle_command(
        TgCommand("1", 1, f"/replace {order.ticket} {new_px}", 3)
    )
    assert "refused: size_exceeds_risk" in reply, reply
    # And the order is untouched, because a refusal that still moved the order
    # would be a refusal in name only.
    assert abs(engine.broker.orders()[0].price - order.price) < 1e-12
    engine.stop()


def test_replace_pending_still_allows_a_replacement_that_fits(tmp_path) -> None:
    """The loss-room term must not refuse everything (issue #104).

    The control for the test above. Same replacement, same arithmetic, with the
    day's budget untouched: it must still be ACCEPTED. A guard that refuses
    every replacement would pass the test above for the wrong reason.
    """
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl = spec.normalize_price(limit - 0.005)
    tp = spec.normalize_price(limit + 0.010)
    # THE RESTING ORDER IS SIZED BELOW TODAY'S CAP ON PURPOSE (#164).
    #
    # These fixtures used to size the order AT the per-trade cap and propose
    # `limit - 0.001`, which moves the entry TOWARD the stop: worst 50.00 ->
    # 40.00, a REDUCTION. So the #104 pin asserted that a strictly
    # risk-REDUCING replacement is refused, which is the behaviour #164 ruled
    # wrong, and the increase direction #104 actually described was never
    # pinned. Direction was not a variable when this was written.
    #
    # Flipping the direction alone does not work: an order sized AT the cap has
    # an EMPTY band for "an increase that still fits the per-trade cap", since
    # any increase on it breaches `per_trade` too and the pre-#157 arithmetic
    # would refuse it as well. Same structural fact as #170's scaled-out
    # position. So the order is sized at 0.2% to leave the band open and the cap
    # is restored to 0.5% before the replacement is measured.
    engine.cfg.risk.risk_pct = 0.002
    engine.handle_command(
        TgCommand("1", 1, f"/buy EURUSD limit={limit} sl={sl} tp={tp}", 1)
    )
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    order = engine.broker.orders()[0]
    engine.cfg.risk.risk_pct = 0.005
    worst_resting = money_per_lot_at_stop(order.price, order.sl, spec) * order.volume

    account = engine.broker.account()
    r = engine.cfg.risk
    per_trade = account.equity * r.risk_pct * r.max_risk_multiple
    # A full day's budget: 2% of equity against a 0.5% per-trade cap, so
    # min(per_trade, loss_room) is the per-trade half and nothing changes.
    assert engine.risk.loss_room(account) > per_trade

    worst = money_per_lot_at_stop(
        spec.normalize_price(limit + 0.0015), order.sl, spec
    ) * order.volume
    # An INCREASE that FITS. Without the first assert this control would
    # pass through #164's carve-out and stop controlling anything: a cap
    # that refused every increase would still leave it green.
    assert worst > worst_resting + 1e-6, (worst, worst_resting)
    assert worst <= per_trade + 1e-6, (worst, per_trade)

    # Entry moved AWAY from the stop, so this is an INCREASE: the direction
    # #104 is about and the one #164's carve-out must never cover.
    new_px = spec.normalize_price(limit + 0.0015)
    reply = engine.handle_command(
        TgCommand("1", 1, f"/replace {order.ticket} {new_px}", 3)
    )
    assert f"replace #{order.ticket}" in reply, reply
    assert abs(engine.broker.orders()[0].price - new_px) < spec.point
    engine.stop()


def _rest_at_cap(engine, tmp_path):
    """One resting buy limit sized AT the per-trade cap, with the day tight.

    #164's measured state: $50.00 resting, $13.80 of room, circuit clear. The
    order is at the cap on purpose here, which is what makes a reduction the
    only replacement the cap would otherwise refuse.
    """
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl = spec.normalize_price(limit - 0.005)
    tp = spec.normalize_price(limit + 0.010)
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD limit={limit} sl={sl} tp={tp}", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    order = engine.broker.orders()[0]
    engine.risk.snapshot.day_start_equity = 10_190.0
    account = engine.broker.account()
    r = engine.cfg.risk
    per_trade = account.equity * r.risk_pct * r.max_risk_multiple
    room = engine.risk.loss_room(account)
    assert engine.risk.circuit_reason(account, engine.now_fn()) == "", (
        "the circuit must be CLEAR or it refuses and the cap is not what is measured"
    )
    assert 0 < room < per_trade, (room, per_trade)
    return order, spec, limit, per_trade, room


def test_replace_pending_allows_a_strictly_reducing_replacement(tmp_path) -> None:
    """A replacement that LOWERS risk is always allowed (issue #164).

    `_stop_guard` already solved this shape the other way and documents the
    asymmetry; `replace_pending` gated reductions too, so two guards on one
    engine disagreed about whether de-risking needs permission and only one of
    them stated its position.

    #164's measurement: $50.00 resting, $40.00 proposed, $13.80 of room was
    REFUSED, leaving `/cancel` as the operator's only move. That removes the
    order outright rather than reducing it, so the cap left MORE risk resting
    than allowing the reduction would. A cap that refuses the one action which
    unconditionally lowers risk inverts its own purpose.
    """
    engine = _engine(tmp_path)
    engine.start()
    order, spec, limit, per_trade, room = _rest_at_cap(engine, tmp_path)

    worst_resting = money_per_lot_at_stop(order.price, order.sl, spec) * order.volume
    new_px = spec.normalize_price(limit - 0.001)
    worst = money_per_lot_at_stop(new_px, order.sl, spec) * order.volume

    # The three facts that make this the carve-out's test and not the cap's:
    # it REDUCES, and the cap on its own WOULD have refused it.
    assert worst < worst_resting - 1e-6, (worst, worst_resting)
    assert worst > min(per_trade, room) + 1e-6, (
        f"worst={worst} fits in min(per_trade={per_trade}, room={room}); the cap "
        "would allow this anyway and the carve-out is not what is being tested"
    )

    reply = engine.handle_command(TgCommand("1", 1, f"/replace {order.ticket} {new_px}", 3))
    engine.stop()

    assert f"replace #{order.ticket}" in reply, reply


def test_replace_pending_refuses_an_increase_in_the_same_state(tmp_path) -> None:
    """The asymmetry in ONE test: same book, same room, opposite direction.

    Paired with the test above so the two answers sit next to each other. Only
    the DIRECTION changes the verdict; the threshold never moves, which is what
    keeps #104's fail-open closed. A carve-out that leaked into the increase
    direction would red here.
    """
    engine = _engine(tmp_path)
    engine.start()
    order, spec, limit, per_trade, room = _rest_at_cap(engine, tmp_path)

    worst_resting = money_per_lot_at_stop(order.price, order.sl, spec) * order.volume
    new_px = spec.normalize_price(limit + 0.001)
    worst = money_per_lot_at_stop(new_px, order.sl, spec) * order.volume
    assert worst > worst_resting + 1e-6, (worst, worst_resting)

    reply = engine.handle_command(TgCommand("1", 1, f"/replace {order.ticket} {new_px}", 3))
    engine.stop()

    assert "refused: size_exceeds_risk" in reply, reply


def test_replace_pending_refuses_an_unmeasured_spec(tmp_path) -> None:
    """Fail CLOSED before the arithmetic (issue #161).

    `money_per_lot_at_stop` needs points, and `ticks_between` returns 0.0 when
    `trade_tick_size or point` is <= 0, so on a spec the broker never streamed
    `worst` was 0.0 and `0.0 > min(per_trade, loss_room)` was False: EVERY
    replacement passed the size guard and was sent. `risk.evaluate` and
    `_stop_guard` both carry this precondition; this was the third site with
    the same arithmetic and the only one without it.

    It also has to run BEFORE #164's carve-out, which compares two numbers from
    the same function: on an unmeasured spec BOTH are 0.0, so
    `worst <= worst_resting` is trivially true and the carve-out would be a
    second way to fail open. This refusal is what makes that comparison mean
    anything, which is why the two land together.
    """
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl = spec.normalize_price(limit - 0.005)
    tp = spec.normalize_price(limit + 0.010)
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD limit={limit} sl={sl} tp={tp}", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    order = engine.broker.orders()[0]

    blind = replace(spec, trade_tick_size=0.0, point=0.0,
                    unmeasured=frozenset({"point", "tick_size"}))
    engine.broker.symbol = lambda name, _b=blind: _b  # type: ignore[method-assign]

    # A REDUCTION, so the carve-out would cover it if this refusal were absent:
    # that is the interaction, not a hypothetical.
    reply = engine.handle_command(
        TgCommand("1", 1, f"/replace {order.ticket} {spec.normalize_price(limit - 0.001)}", 3)
    )
    assert "refused: spec_not_measured" in reply, reply
    assert "point" in reply and "tick_size" in reply, reply
    engine.stop()


def _rest_then_strip_the_stop(engine):
    """A resting order with `sl = 0`, which our OWN commands cannot produce.

    That is the reachability finding, not a shortcut. Measured on main:

    * `risk.py`'s `sl_required` refuses to STAGE one (`signal.sl <= 0`).
    * `_modify_pending` refuses to MODIFY one to zero (`sl required`).

    So the state only arrives from the venue side, and both live adapters pass
    it straight through: `mt4_live._ord` and `mt5_live.orders` each build
    `sl=float(d.get("sl", 0) or 0)` with no filter and no coercion. An order
    carrying our magic that we did not stage, or an operator removing the stop
    at the terminal, therefore reaches `replace_pending` as `order.sl == 0.0`.
    The fixture injects what only the venue can.
    """
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl = spec.normalize_price(limit - 0.005)
    tp = spec.normalize_price(limit + 0.010)
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD limit={limit} sl={sl} tp={tp}", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    order = engine.broker.orders()[0]
    # `PendingOrder` is frozen, so the venue-side state is injected by replacing
    # the stored object rather than mutating the one handed out.
    engine.broker._orders[order.ticket] = replace(order, sl=0.0)
    order = engine.broker.orders()[0]
    assert order.sl == 0.0
    return order, spec, limit


def test_our_own_commands_cannot_rest_an_order_without_a_stop(tmp_path) -> None:
    """The first half of #187's reachability question, so the answer is measured.

    If the desk could stage or modify one of these, the defect would be ours
    rather than the venue's, and the priority would be different.
    """
    engine = _engine(tmp_path)
    engine.start()
    tick = engine.broker.tick("EURUSD")
    spec = engine.broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)

    staged = engine.handle_command(
        TgCommand("1", 1, f"/buy EURUSD limit={limit} sl=0 tp={limit + 0.01}", 1)
    )
    assert "refused" in staged or "needs sl" in staged, staged

    order, _spec, _limit = _rest_then_strip_the_stop(engine)
    restored = engine._modify_pending(order, sl=0.0, tp=order.tp, price=order.price)
    assert not restored.ok
    assert "sl required" in restored.comment, restored.comment
    engine.stop()


def test_replace_on_an_unstopped_order_refuses_by_name(tmp_path) -> None:
    """#187. A missing stop is NOT a very large number.

    `money_per_lot_at_stop` computed `ticks_between(price, 0, spec)`, which is
    `price / tick_size` and astronomically large, and then arithmetic proceeded
    on it as though it were a measurement. `Position.risk_distance` already
    treats `sl <= 0` as not-measurable, so two functions disagreed about what a
    missing stop MEANS. That is the #161 and #172 family: a degenerate input
    producing a confident number instead of a refusal.

    Before the fix, moving the entry DOWN made `worst < worst_resting`, so
    #184's reduction carve-out was entered and THE CAP WAS NEVER CONSULTED;
    moving it UP refused with `size_exceeds_risk`, which blames the size for a
    missing stop. Neither sent an order, because `_modify_pending` refuses
    `sl <= 0` on the way out, so this is a defect in what the desk SAYS rather
    than in what it does. The operator is told the wrong thing about their own
    book, which is the whole reason the refusal vocabulary exists.
    """
    engine = _engine(tmp_path)
    engine.start()
    order, spec, limit = _rest_then_strip_the_stop(engine)

    for label, px in (
        ("entry down", spec.normalize_price(limit - 0.001)),
        ("entry up", spec.normalize_price(limit + 0.001)),
    ):
        reply = engine.handle_command(TgCommand("1", 1, f"/replace {order.ticket} {px}", 3))
        assert "refused: sl_required" in reply, f"{label}: {reply}"
        assert "size_exceeds_risk" not in reply, (
            f"{label}: a missing stop is not a size problem"
        )
    assert engine.broker.orders()[0].price == order.price, "the order must not move"
    engine.stop()


def test_money_per_lot_at_stop_refuses_a_missing_stop() -> None:
    """The fix lives in the shared function, not at the call site (#187).

    A third opinion about what a missing stop means is how this family spreads:
    the next caller inherits whichever of the three it happens to reach. So the
    disagreement is resolved where the number is produced.

    It RAISES rather than returning 0.0, and the difference matters: a 0.0 would
    make `worst` zero and pass every cap trivially, which is exactly the #161
    fail-open this repo has already paid for once. The two functions agree that
    a missing stop is not a measurement; they differ in mechanism because only
    one of them has a safe sentinel available.
    """
    from straightedge.sizing import MissingStop, money_per_lot_at_stop

    spec = default_spec("EURUSD")
    assert money_per_lot_at_stop(1.1000, 1.0950, spec) > 0
    for bad in (0.0, -1.0):
        try:
            money_per_lot_at_stop(1.1000, bad, spec)
        except MissingStop:
            continue
        raise AssertionError(f"sl={bad} produced a number instead of refusing")
