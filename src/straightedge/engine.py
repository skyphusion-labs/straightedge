"""One path for live, paper, and backtest: strategy proposes, risk sizes, broker sends."""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from typing import Any

from straightedge.broker.base import Broker
from straightedge.config import BotConfig
from straightedge.constants import MT4_SEND_TIMEOUT_UNKNOWN
from straightedge.desk import Desk
from straightedge.history import HistoryReport, preflight
from straightedge.indicators import adx, ema, last_closed
from straightedge.indicators import atr as atr_bars
from straightedge.inflight import InflightLedger, new_key, stamped_comment
from straightedge.journal import Journal, redact_text
from straightedge.llm import Advisor, advice_path_for
from straightedge.models import (
    Bar,
    FlattenReport,
    MarketOrder,
    OrderResult,
    PendingOrder,
    Position,
    Signal,
    SignalKind,
    WorkingOrder,
)
from straightedge.risk import RiskDecision, RiskManager, day_key
from straightedge.sizing import money_per_lot_at_stop, normalize_volume
from straightedge.state import snapshot_path_for
from straightedge.strategy import TrendStrategy
from straightedge.telegram import TelegramClient, TgCommand
from straightedge import watchdog

_ORDER_TYPE_NAME = {
    "limit": "LIMIT",
    "stop": "STOP",
}

# Lot volumes are broker-rounded to 8 places at most; this is a float-compare guard,
# not a tolerance for a genuinely short fill.
_VOLUME_EPS = 1e-9


def _pending_label(order: PendingOrder) -> str:
    suffix = _ORDER_TYPE_NAME.get(order.kind)
    if suffix:
        return f"{order.side.value.upper()}_{suffix}"
    return order.side.value


#: A stop modification refused on an OPEN position.
#:
#: Deliberately NOT the sizing refusal (spelled size-exceeds-risk here, with
#: hyphens, because `tests/test_refusal_reasons.py` counts occurrences of that
#: exact token per module and a mention in a comment counts). That reason names a
#: SEND the sizer produced and has exactly one live site; an operator who typed
#: `/sl` and was handed it would go looking for a sizing problem that is not
#: there. Two words here rather than one, because removing a stop and widening
#: one past the cap are different acts with different fixes.
STOP_REMOVAL_REFUSED = "stop_removal_refused"
STOP_EXCEEDS_RISK = "stop_exceeds_risk"


def _loss_distance(pos: Position, sl: float) -> float:
    """How far `sl` sits on the LOSING side of the entry. Negative past breakeven.

    `abs(price_open - sl)` is the obvious form and it is WRONG, because it is not
    monotonic in risk: as a long's stop rises toward the entry the distance
    shrinks and so does the risk, but once the stop passes the entry the distance
    grows again while the risk has become locked-in PROFIT. An abs() comparison
    therefore reads a breakeven-plus trail as a widening. Measured: it refused
    `/trail` on a winning position (`test_trail_on_manages_without_auto_entries`).

    Signed, so one comparison covers tightening, breakeven and beyond.
    """
    if pos.side.value == "buy":
        return pos.price_open - sl
    return sl - pos.price_open


class Engine:
    def __init__(
        self,
        cfg: BotConfig,
        broker: Broker,
        *,
        journal: Journal | None = None,
        halt_dir: str = ".",
        now_fn: Any = None,
        telegram: TelegramClient | None = None,
        advisor: Advisor | None = None,
    ) -> None:
        self.cfg = cfg
        self.broker = broker
        self.journal = journal or Journal(cfg.journal_path)
        # The snapshot follows the journal that is actually in use, not
        # cfg.journal_path: run_backtest() passes a Journal of its own.
        self.risk = RiskManager(
            cfg,
            halt_dir=halt_dir,
            state_path=snapshot_path_for(self.journal.path),
        )
        self.strategy = TrendStrategy(cfg.strategy)
        self.now_fn = now_fn or (lambda: datetime.now(timezone.utc))
        self.last_bar_time: dict[str, int] = {}
        self.halted = False
        #: Heartbeat bookkeeping. `_hb_gap_max_s` is the largest gap between two
        #: heartbeat writes this process has actually seen, published so the
        #: derived staleness threshold can be checked against reality instead of
        #: trusted. It is never used to widen the threshold: see
        #: `straightedge.watchdog`.
        self._hb_last_mono: float | None = None
        self._hb_gap_max_s = 0.0
        self._hb_over_warned = False
        self.telegram = telegram
        if self.telegram is not None and self.telegram.audit_fn is None:
            self.telegram.audit_fn = self._audit_telegram
        persist = advice_path_for(self.journal.path)
        if advisor is not None:
            self.advisor = advisor
            if self.advisor.persist_path is None:
                self.advisor.persist_path = persist
                self.advisor.load()
        else:
            self.advisor = Advisor(cfg.advice, persist_path=persist)
        #: Sends that have LEFT but whose outcome is unknown, keyed by client
        #: order id and durable across a process exit. See `inflight.py`; the
        #: short version is that a timeout is not evidence the order did not
        #: reach the broker, so the record is written BEFORE the send and cleared
        #: only by an outcome.
        self.inflight = InflightLedger(self.journal.path)
        self.desk = Desk(self, self.advisor)
        self._seen_pos: set[int] | None = None
        #: Filled by start(). None means the preflight has not run, which is
        #: NOT the same as "every symbol is fine": a caller that reads this
        #: before start() must see the difference.
        self.history: HistoryReport | None = None
        self._opened_this_step: set[int] = set()
        self._closed_this_step: set[int] = set()
        self._scale_outs: dict[int, tuple[float, float]] = {}

    def _report_survivor(self, symbol: str, result: OrderResult) -> None:
        """A send that failed may still have left something on the book.

        This is the one case where a failure report is not the whole truth:
        the desk is told the send failed while a position is live and has no
        stop. Nothing else in the system reconciles that state, so it is
        named here rather than left to a human noticing the terminal.
        """
        survivor = result.survivor_ticket
        if survivor is None:
            self._emit(
                "survivor_unknown",
                symbol=symbol,
                retcode=result.retcode,
                comment=result.comment,
            )
            return
        if survivor:
            self._emit(
                "unmanaged_position",
                symbol=symbol,
                ticket=int(survivor),
                retcode=result.retcode,
                comment=result.comment,
            )

    def _audit_telegram(self, event: str, fields: dict[str, Any]) -> None:
        # Journal only: a refusal is never echoed back to the chat.
        self.journal.write(event, **fields)

    def _emit(self, event: str, **fields: Any) -> None:
        self.journal.write(event, **fields)
        if self.telegram is None:
            return
        text = _format_event(event, fields)
        if text:
            self.telegram.notify(event, text)

    def start(self) -> None:
        # A venue whose terminal can take tens of seconds to become answerable
        # says so by offering `startup_connect`, and MT4 is one: it is a GUI
        # application that a boot-triggered scheduled task races. Every other
        # `connect()` in this file (`_reconnect_broker`, and `ensure_connected`
        # from `step_all`) deliberately keeps the short steady-state budget, so
        # the long wait is spent exactly once, here, before the desk is holding
        # anything.
        #
        # Duck-typed rather than added to the `Broker` Protocol for the same
        # reason `ensure_connected` is (see `step_all`): it is one venue's own
        # property, not something every venue must implement.
        # BEFORE the venue is touched. An unresolved send is a question about
        # money that is already at risk, and it has to be in the journal even if
        # the connect below fails: a desk that cannot reach the terminal is
        # exactly the state in which an operator most needs to know that an
        # earlier order's outcome was never established.
        self.report_unresolved_sends()
        opener = getattr(self.broker, "startup_connect", None)
        if callable(opener):
            opener()
        else:
            self.broker.connect()
        for name in self.cfg.symbols:
            self.broker.select_symbol(name)
        acct = self.broker.account()
        if self.risk.state_error:
            # COULD NOT MEASURE, and the gate is already closed. Say so at start
            # rather than at the first refusal, so the journal shows the cause.
            self._emit(
                "risk_state_error",
                reason=self.risk.halt_reason,
                error=self.risk.state_error,
                path=str(self.risk.state_path),
            )
        self.risk.observe(acct, self.now_fn())
        self._emit(
            "start",
            mode=self.cfg.mode,
            login=acct.login,
            equity=acct.equity,
            server=acct.server,
            symbols=self.cfg.symbols,
            # The handover posture (#25) is invisible from the config alone
            # unless someone reads it. Put it in the one record every
            # session already writes, so a journal reader can tell after
            # the fact which posture a session ran under, and so it is
            # never silently missing the way an unread config key would be.
            approve_always_allowed=self.cfg.telegram.allow_approve_always,
            auto_allowed=self.cfg.telegram.allow_auto,
        )
        # Ask the venue for every configured symbol's series before anything
        # relies on it. On MT4 the ask IS the fix: a series exists per symbol AND
        # timeframe and the terminal builds one only when something requests it,
        # so a cold symbol becomes warm here without the operator opening
        # anything. What cannot be fixed is NAMED, here, at startup. It used to
        # surface as step_symbol() returning on an empty bar list with no record
        # (engine.py:466-468) -- a symbol that never traded and never said why.
        self.history = self.warm_history()
        self._seen_pos = {
            p.ticket for p in self.broker.positions(magic=self.cfg.risk.magic)
        }
        self.desk.restore_from_journal(self.journal)
        self.advisor.load()

    def warm_history(self, symbols: list[str] | None = None) -> HistoryReport:
        """Warm and measure the configured symbols, then journal the result.

        Two records on purpose, modelled on flatten / flatten_incomplete:
        `history_preflight` is journal-only and carries every symbol's numbers
        whether they are good or not, because the healthy denominator is what
        makes a later regression visible. `history_unavailable` is the loud one
        and fires only when a symbol cannot trade, so the chat gets a named
        failure and never a routine all-clear.
        """
        report = preflight(
            self.broker,
            self.cfg.symbols if symbols is None else symbols,
            timeframe=self.cfg.strategy.timeframe,
            needed=self.strategy.needed_bars(),
            atr_period=self.cfg.strategy.atr_period,
        )
        self._emit(
            "history_preflight",
            timeframe=report.timeframe,
            needed=report.needed,
            attempts=report.attempts,
            measured=len(report.symbols),
            unusable=report.names(),
            symbols=report.rows(),
        )
        if not report.ok:
            self._emit(
                "history_unavailable",
                timeframe=report.timeframe,
                unusable=report.names(),
                measured=len(report.symbols),
                text=report.text(),
            )
        return report

    def stop(self) -> None:
        self._emit("stop")
        self.broker.disconnect()

    def flatten(self, reason: str) -> FlattenReport:
        """Close everything, count what actually closed, and say so out loud.

        Three rules this path exists to enforce:

        1. Never raise. A circuit breaker that dies mid sweep leaves exposure open
           and skips the halt. Every broker call here is contained, and the sweep
           finishes even when one leg fails.
        2. Never trust a flag. RETCODE_OK admits DONE_PARTIAL, so completeness is
           decided by comparing filled volume against the volume requested, and then
           cross-checked against a fresh post-sweep read. Two instruments; either one
           can add a survivor, neither can remove one.
        3. COULD NOT MEASURE is INCOMPLETE. An unreadable broker response on a
           flatten is residual exposure until proven otherwise, never a clean sweep.

        `halted` is set regardless of the outcome: a failed flatten must still stop
        new entries. The bug this fixes was the silence, not the halt. Survivors are
        never folded into `_seen_pos`, so `_detect_fills` alerts on them again.
        """
        desk = getattr(self, "desk", None)
        if desk is not None:
            clearer = getattr(desk, "_clear_pending", None)
            if callable(clearer):
                clearer("confirm_cancel")
            else:
                desk.pending = None

        magic = self.cfg.risk.magic

        orders_before, orders_readable = self._read_working(magic)
        requested_orders = [o.ticket for o in orders_before]
        cancelled: set[int] = set()
        for ticket in requested_orders:
            try:
                result = self.broker.cancel(ticket)
            except (RuntimeError, OSError, ValueError) as exc:
                self._emit("cancel_failed", ticket=ticket, reason=reason, error=str(exc))
                continue
            if result.ok:
                cancelled.add(ticket)
            else:
                self._emit(
                    "cancel_failed",
                    ticket=ticket,
                    reason=reason,
                    retcode=result.retcode,
                    comment=result.comment,
                )

        positions_before, positions_readable = self._read_open(magic)
        requested = [p.ticket for p in positions_before]
        confirmed: set[int] = set()
        residual: set[int] = set()
        for pos in positions_before:
            # Snapshot the requested volume BEFORE the close. Position is a mutable
            # dataclass and PaperBroker hands out live references, so a partial close
            # rewrites pos.volume in place; comparing against it afterwards would make
            # every partial look complete.
            asked = pos.volume
            try:
                result = self._close(pos, reason)
            except (RuntimeError, OSError, ValueError) as exc:
                self._emit(
                    "close_failed",
                    ticket=pos.ticket,
                    symbol=pos.symbol,
                    reason=reason,
                    error=str(exc),
                )
                continue
            if not result.ok:
                continue
            if result.volume + _VOLUME_EPS >= asked:
                confirmed.add(pos.ticket)
            else:
                # DONE_PARTIAL, or any ok result that filled short. Residual exposure.
                residual.add(pos.ticket)
                self._emit(
                    "close_partial",
                    ticket=pos.ticket,
                    symbol=pos.symbol,
                    reason=reason,
                    requested=asked,
                    filled=result.volume,
                    retcode=result.retcode,
                )

        self._scale_outs.clear()
        # Set before the verification reads: the halt must survive a failing broker.
        self.halted = True

        open_after_list, open_readable = self._read_open(magic)
        working_after_list, working_readable = self._read_working(magic)
        open_after = {p.ticket for p in open_after_list}
        working_after = {o.ticket for o in working_after_list}
        measured = (
            positions_readable and orders_readable and open_readable and working_readable
        )

        if measured:
            survivors = open_after | residual
            closed_elsewhere = set(requested) - confirmed - residual - open_after
            order_survivors = working_after
            orders_gone = set(requested_orders) - cancelled - working_after
        else:
            # COULD NOT MEASURE. The broker whose read just failed is the same broker
            # that claimed those closes, so a volume confirmation from it is not
            # evidence either. Every requested ticket is treated as still open; the
            # count is an upper bound and the alert says so.
            survivors = set(requested) | open_after
            closed_elsewhere = set()
            order_survivors = set(requested_orders) | working_after
            orders_gone = set()

        # A survivor is never marked already-seen; _detect_fills must re-announce it.
        self._seen_pos = open_after - survivors

        report = FlattenReport(
            reason=reason,
            positions_requested=len(requested),
            positions_confirmed_closed=len(confirmed),
            positions_closed_elsewhere=len(closed_elsewhere),
            survivors=tuple(sorted(survivors)),
            residual=tuple(sorted(residual)),
            orders_requested=len(requested_orders),
            orders_confirmed_cancelled=len(cancelled),
            orders_gone_elsewhere=len(orders_gone),
            order_survivors=tuple(sorted(order_survivors)),
            measured=measured,
        )
        fields = {
            "reason": reason,
            "requested": report.positions_requested,
            "confirmed_closed": report.positions_confirmed_closed,
            "closed_elsewhere": report.positions_closed_elsewhere,
            "survivor_count": report.survivor_count,
            "survivors": list(report.survivors),
            "residual": list(report.residual),
            "orders_requested": report.orders_requested,
            "orders_cancelled": report.orders_confirmed_cancelled,
            "order_survivors": list(report.order_survivors),
            "measured": report.measured,
            "complete": report.complete,
        }
        self._emit("flatten", **fields)
        if not report.complete:
            self._emit("flatten_incomplete", **fields)
        return report

    def _read_open(self, magic: int) -> tuple[list[Position], bool]:
        """Positions, plus whether the read succeeded. Unreadable is not empty."""
        try:
            return list(self.broker.positions(magic=magic)), True
        except (RuntimeError, OSError, ValueError) as exc:
            self._emit("positions_read_failed", error=str(exc))
            return [], False

    def _read_working(self, magic: int) -> tuple[list[PendingOrder], bool]:
        try:
            return list(self.broker.orders(magic=magic)), True
        except (RuntimeError, OSError, ValueError) as exc:
            self._emit("orders_read_failed", error=str(exc))
            return [], False

    def _apply_circuit(self, acct, now) -> str:
        """The halt reason that stopped this tick, or empty when it did not.

        Returns the REASON rather than a bool so the heartbeat can name it. Both
        `if self._apply_circuit(...)` call sites read exactly as before, because
        an empty reason is falsey and a named one is not.
        """
        trip = self.risk.circuit(acct, now)
        if not trip.halt:
            return ""
        self._emit("halt", reason=trip.reason, equity=acct.equity)
        if trip.flatten:
            self.flatten(trip.reason)
        return trip.reason or "halted"

    def _close(self, pos: Position, reason: str, volume: float | None = None) -> OrderResult:
        tick = self.broker.tick(pos.symbol)
        vol = pos.volume if volume is None else volume
        # Per symbol, same as an open. A close is never GATED on the deviation
        # (a control that can stop you reducing exposure is not a risk control),
        # but it still has to be sendable, and gold needs the same tolerance on
        # the way out as on the way in.
        dev = self.cfg.risk.resolve_deviation(pos.symbol)
        result = self.broker.close_position(
            pos.ticket,
            symbol=pos.symbol,
            side=pos.side.value,
            volume=vol,
            price=tick.bid if pos.side.value == "buy" else tick.ask,
            comment=reason[:31],
            magic=self.cfg.risk.magic,
            deviation=dev.points,
        )
        self._closed_this_step.add(pos.ticket)
        if result.ok:
            self._scale_outs.pop(pos.ticket, None)
        self._emit(
            "close",
            reason=reason,
            ticket=pos.ticket,
            symbol=pos.symbol,
            ok=result.ok,
            retcode=result.retcode,
            comment=result.comment,
            price=result.price,
            volume=vol,
            side=pos.side.value,
            deviation=dev.points,
            deviation_source=dev.source,
        )
        return result

    def _stop_guard(self, pos: Position, sl: float) -> str:
        """Whether this stop may be applied to an OPEN position. "" means yes.

        ASYMMETRIC by design, and that is the whole of it. A change that REDUCES
        worst-case loss always passes, including when the circuit has tripped and
        the desk is halted, because an operator must never be prevented from
        tightening a stop on a live position and that is exactly the moment they
        most need to. Only an INCREASE is measured against the cap.

        The three arms are in this order for a reason each:

        1. An ALREADY-UNPROTECTED position cannot be made worse from here. Any
           real stop is a reduction from unbounded to bounded, so a cap must
           never stand between an operator and protecting a live position. It
           also keeps `set_tp`, which passes `pos.sl` straight through, working
           on a position that has no stop.
        2. `sl <= 0` on a PROTECTED position REMOVES the protection. `/sl` sets a
           stop to a price and 0 is not a price, it is the venue encoding for "no
           stop". `_modify_pending` has always refused this for working orders,
           so the position path was the inconsistent one and this aligns them
           rather than inventing policy.
        3. Anything NOT WIDER than what is already there is a tightening. It is
           decided on raw DISTANCES, which needs no `SymbolSpec` because both
           distances are on the same instrument. That is what keeps `trail`,
           `breakeven` and the strategy `manage()` off the broker-read path: they
           only ever tighten, and on MT4 a `symbol()` call is a mailbox round
           trip that would then be paid on every managed position every step.

        Only a genuine widening pays for the spec and the account read.
        """
        # FIRST, before the unprotected-position arm. NaN is not a price and it
        # is not caught by any comparison below: every IEEE-754 comparison
        # against NaN is False, so `sl <= 0`, `proposed <= 0`, the tighten
        # comparison and the cap test ALL fall through and the guard returns "".
        # `float("nan")` succeeds, so `/sl <ticket> nan` reached the broker and
        # the desk answered `sl #1 -> nan`, which reads as success. `+inf` fell
        # through the breakeven arm the same way (price_open - inf is -inf).
        # Measured: an unstopped long ran to -$58,058 on a $10,000 account.
        # This arm has to precede arm 1 because on an UNPROTECTED position arm 1
        # returns "" and would wave NaN straight through.
        if not math.isfinite(sl):
            return STOP_REMOVAL_REFUSED
        if pos.sl <= 0:
            return ""
        if sl <= 0:
            return STOP_REMOVAL_REFUSED
        proposed = _loss_distance(pos, sl)
        # At or beyond breakeven there is no loss left to bound, so nothing about
        # a cap applies. This arm has to come BEFORE the comparison: past
        # breakeven `money_per_lot_at_stop` still reports a positive number,
        # because `ticks_between` is unsigned.
        if proposed <= 0:
            return ""
        if proposed <= _loss_distance(pos, pos.sl) + 1e-12:
            return ""
        spec = self.broker.symbol(pos.symbol)
        # Fail CLOSED before the arithmetic, exactly as `risk.evaluate` does at
        # its own spec read. This gate needs points, and `ticks_between` returns
        # 0.0 when `trade_tick_size or point` is <= 0, so an unmeasured spec
        # makes `worst` 0.0 and `0.0 > min(per_trade, loss_room)` False: every
        # widening passed. The arithmetic here was copied from `risk.evaluate`
        # and the precondition was left behind, which is how a correct rule
        # becomes a fail-open. `models.SymbolSpec.points` states the invariant
        # that every gate needing points runs after this refusal, and CLAUDE.md
        # states the rule: unmeasured specs refuse, they never default.
        #
        # The asymmetry survives: a tightening returns above without ever
        # reaching this read, so a stop can still be tightened on a symbol whose
        # specs the broker has not streamed. Only a WIDENING refuses.
        not_measured = spec.unmeasured_for_sizing()
        if not_measured:
            return "spec_not_measured:" + ",".join(sorted(not_measured))
        account = self.broker.account()
        r = self.cfg.risk
        worst = money_per_lot_at_stop(pos.price_open, sl, spec) * pos.volume
        per_trade = account.equity * r.risk_pct * r.max_risk_multiple
        # The same pair `risk.evaluate` measures a NEW order against. Carrying
        # the loss-room term here is what makes a spent daily budget refuse a
        # widening by arithmetic alone rather than by a second circuit check: once
        # the budget is gone `loss_room` is negative, so no wider stop fits.
        # (`replace_pending` uses only the per-trade half; that gap is filed
        # separately and is not widened by matching the stricter form here.)
        if worst > min(per_trade, self.risk.loss_room(account)) + 1e-6:
            return STOP_EXCEEDS_RISK
        return ""

    def _modify(self, pos: Position, sl: float, tp: float) -> OrderResult:
        if abs(sl - pos.sl) < 1e-12 and abs((tp or 0) - (pos.tp or 0)) < 1e-12:
            return OrderResult.unchanged()
        reason = self._stop_guard(pos, sl)
        if reason:
            # Journaled with BOTH stops, because "refused" is only auditable
            # next to what was already on the position.
            self.journal.write(
                "modify_refused",
                ticket=pos.ticket,
                symbol=pos.symbol,
                sl=sl,
                tp=tp,
                current_sl=pos.sl,
                reason=reason,
            )
            return OrderResult.invalid_stops(reason)
        result = self.broker.modify_position(pos.ticket, sl, tp, symbol=pos.symbol)
        self.journal.write(
            "modify",
            ticket=pos.ticket,
            symbol=pos.symbol,
            sl=sl,
            tp=tp,
            ok=result.ok,
            retcode=result.retcode,
        )
        return result

    def _pretrade_ok(self, check: OrderResult, symbol: str) -> bool:
        """Whether a pre-trade check clears the order to send.

        One gate for every send path. Three outcomes, not two: PASSED,
        the broker REFUSED, and COULD NOT MEASURE. The last one used to be
        indistinguishable from PASSED and the order went out (issue #8).
        """
        if not check.measured:
            # Nothing was checked, so there is no verdict to trust. Abort.
            self._emit(
                "order_check_fail",
                reason="not_measured",
                measured=False,
                symbol=symbol,
                retcode=check.retcode,
                comment=check.comment,
            )
            return False
        # order_check reports a passed check as retcode 0, so 0 is a pass here.
        # Never widen this whitelist to cover a locally synthesized code.
        if check.retcode != 0 and not check.ok:
            self._emit(
                "order_check_fail",
                reason="broker_refused",
                measured=True,
                symbol=symbol,
                retcode=check.retcode,
                comment=check.comment,
            )
            return False
        return True

    def _unresolved(self, client_id: str, symbol: str) -> OrderResult | None:
        """Refuse a send whose key already has an open attempt against it.

        This is the whole of the duplicate-order fix, and it is a REFUSAL rather
        than a reconciliation on purpose. Reconciliation is attempted (below, and
        at startup) and it can only ever produce two answers: a position that
        matches, or silence. Silence is not proof: the Expert may be mid-ladder,
        the fill may be seconds away, the position may have opened and closed. So
        the desk refuses, says why, and leaves the judgement to a human with the
        terminal in front of them. Refusing costs an order; sending twice costs
        money and cannot be undone.
        """
        if not client_id:
            return None
        entry = self.inflight.get(client_id)
        if entry is None:
            return None
        self._emit(
            "send_refused_unresolved",
            client_id=client_id,
            symbol=symbol,
            attempts=int(entry.get("attempts", 0)),
            first_at=entry.get("at"),
        )
        return OrderResult.not_sent(
            f"unresolved send {client_id}: nothing was transmitted this time. An "
            "earlier attempt for this order left no verdict, so it may already be "
            "on the book. Reconcile in the terminal (/positions), then /cancel "
            "and re-stage if nothing moved."
        )

    def _after_unresolved_send(
        self,
        client_id: str,
        symbol: str,
        exc: BaseException | None = None,
        *,
        detail: str = "",
    ) -> None:
        """One book read after a send that answered nothing, and an honest report.

        Two callers, one money question. `exc` is the send that RAISED (a bridge
        timeout, an OSError, a Ctrl-C). `exc=None` with `detail` set is the send
        that RETURNED an unmeasured `OrderResult`, which is what an adapter
        produces when the request went out and no reply came back. The adapter
        choosing `raise` or `return` says nothing about the money, so it must not
        change what gets recorded.

        Not a resolution. A matching position is a POSITIVE and is reported as an
        unmanaged position, because a send that never returned also never got its
        stop confirmed. Nothing found is reported as UNRESOLVED, never as clean:
        the desk's own budget expired while the Expert may still have been inside
        `SendRetry`, so an empty book is the expected reading of the dangerous
        case.
        """
        withdrawal = getattr(exc, "withdrawal", "") or "unknown"
        found: list[int] = []
        read_failed = ""
        if exc is None or isinstance(exc, Exception):
            # The probe costs a full read budget, and it is skipped for the
            # BaseException arms (KeyboardInterrupt, SystemExit): the operator is
            # stopping the process and adding a five second book read to a Ctrl-C
            # buys nothing. The LEDGER ENTRY and the journal record below are
            # written either way, which is what makes the send unresolved rather
            # than forgotten.
            try:
                for pos in self.broker.positions(magic=self.cfg.risk.magic):
                    if (
                        pos.symbol == symbol
                        and client_id
                        and client_id in (pos.comment or "")
                    ):
                        found.append(int(pos.ticket))
            except (RuntimeError, OSError, ValueError) as probe:
                read_failed = str(probe)
        else:
            read_failed = "not attempted: the process is shutting down"
        self._emit(
            "send_unresolved",
            client_id=client_id,
            symbol=symbol,
            request=withdrawal,
            matched=found,
            book_read_failed=read_failed,
            detail=detail or str(exc),
        )
        for ticket in found:
            # It carries our key, so it IS this send. It also never had its stop
            # confirmed, which is what `unmanaged_position` means.
            self._emit("unmanaged_position", symbol=symbol, ticket=ticket, retcode=0,
                       comment=f"unresolved send {client_id}")

    def report_unresolved_sends(self) -> int:
        """Announce every still-open attempt. Called by `start()`.

        It fires on EVERY start, not once, and that is deliberate: an unresolved
        send is a standing money question, and a report that stops repeating is a
        report that gets forgotten. An unparseable ledger is reported too, because
        "no open sends" and "I could not read the file" render identically.
        """
        if not self.inflight.readable():
            self._emit("inflight_unreadable", path=str(self.inflight.path))
            return 0
        entries = self.inflight.open_entries()
        for key, entry in sorted(entries.items(), key=lambda kv: kv[1].get("at", 0)):
            self._emit(
                "send_unresolved",
                client_id=key,
                symbol=entry.get("symbol", ""),
                side=entry.get("side", ""),
                volume=entry.get("volume", 0),
                attempts=int(entry.get("attempts", 0)),
                request=entry.get("request", "unknown"),
                detail="still unresolved at startup",
            )
        return len(entries)

    def _open(
        self, signal: Signal, volume: float, client_id: str | None = None
    ) -> OrderResult:
        side = signal.side
        assert side is not None
        # Which deviation applied, and whether it came from the per-symbol map
        # or the global default, is journaled on every send. The number alone
        # cannot tell an operator whether their override was consulted, missed
        # on a decorated broker symbol, or never written.
        dev = self.cfg.risk.resolve_deviation(signal.symbol)
        key = client_id or new_key()
        refusal = self._unresolved(key, signal.symbol)
        if refusal is not None:
            return refusal
        order = MarketOrder(
            symbol=signal.symbol,
            side=side,
            volume=volume,
            sl=signal.sl,
            tp=signal.tp,
            comment=stamped_comment(self.cfg.comment, key),
            magic=self.cfg.risk.magic,
            deviation=dev.points,
            client_id=key,
        )
        check = self.broker.check_market(order)
        if not self._pretrade_ok(check, signal.symbol):
            return check
        # The ledger entry is written BEFORE the send and is the only durable
        # trace that exists until a reply comes back. A crash between these two
        # statements leaves an open entry for an order that never left, which
        # refuses one re-send; the other order of these two statements loses the
        # record for an order that DID leave. Those costs are not symmetrical.
        self.inflight.begin(
            key,
            symbol=signal.symbol,
            side=side.value,
            volume=volume,
            sl=signal.sl,
            tp=signal.tp,
            op="market",
        )
        try:
            result = self.broker.market(order)
        except BaseException as exc:
            # Deliberately not `except Exception`. Whatever stopped the send --
            # a bridge timeout, an OSError, a KeyboardInterrupt from the
            # operator -- the money question is identical and the entry must
            # stay open. Re-raised unchanged; this clause adds a report, it does
            # not swallow anything.
            self._after_unresolved_send(key, signal.symbol, exc)
            raise
        # The entry closes on a VERDICT only, and `measured` is what tells the
        # two apart. A rejection IS a verdict and closes it; an unmeasured result
        # is the venue not answering, which is the state the ledger exists to
        # hold open. Filing it as "rejected" deleted the only record that the
        # order might be on the book, and it disabled BOTH duplicate controls at
        # once, because `Engine._unresolved` and `Desk._already_attempted` read
        # this one file.
        if result.measured:
            self.inflight.resolve(key, "ok" if result.ok else "rejected")
        else:
            self._after_unresolved_send(
                key, signal.symbol, detail=result.comment or "no verdict"
            )
        if result.ok:
            self.risk.record_trade()
            ticket = int(result.order or result.deal or 0)
            if ticket:
                self._opened_this_step.add(ticket)
        else:
            self._report_survivor(signal.symbol, result)
        self._emit(
            "open",
            symbol=signal.symbol,
            side=side.value,
            volume=volume,
            sl=signal.sl,
            tp=signal.tp,
            ok=result.ok,
            retcode=result.retcode,
            order=result.order,
            price=result.price,
            reason=signal.reason,
            adx=signal.adx,
            atr=signal.atr,
            deviation=dev.points,
            deviation_source=dev.source,
            client_id=key,
        )
        return result

    def step_symbol(self, symbol: str) -> None:
        if self.halted:
            return
        need = self.strategy.needed_bars() + 2
        bars = self.broker.rates(symbol, self.cfg.strategy.timeframe, need)
        if not bars:
            return
        last_t = bars[-1].time
        prev = self.last_bar_time.get(symbol)
        if prev is None:
            # First poll: pin the last bar so we do not dump-trade history.
            self.last_bar_time[symbol] = last_t
            return
        if last_t <= prev:
            return
        self.last_bar_time[symbol] = last_t
        self._act(symbol, bars)

    def replay_symbol(self, symbol: str, bars: list[Bar]) -> None:
        if self.halted:
            return
        self._act(symbol, bars)

    def _act(self, symbol: str, bars: list[Bar]) -> None:
        now = datetime.fromtimestamp(bars[-1].time, tz=timezone.utc) if bars else self.now_fn()
        acct = self.broker.account()
        if self._apply_circuit(acct, now):
            return
        spec = self.broker.symbol(symbol)
        tick = self.broker.tick(symbol)
        positions = self.broker.positions(magic=self.cfg.risk.magic)

        for pos in positions:
            if pos.symbol != symbol:
                continue
            new_sl, new_tp = self.strategy.manage(pos, bars, spec)
            self._modify(pos, new_sl, new_tp)

        positions = self.broker.positions(magic=self.cfg.risk.magic)
        if any(p.symbol == symbol for p in positions):
            return

        sig = self.strategy.signal(symbol, bars, spec)
        if sig.kind is SignalKind.FLAT or sig.side is None:
            return
        entry = tick.ask if sig.kind is SignalKind.BUY else tick.bid
        sig = sig.reprice(entry, spec)
        decision = self.risk.evaluate(
            account=acct,
            signal=sig,
            spec=spec,
            tick=tick,
            positions=positions,
            now=now,
        )
        if decision.excluded_from_currency_limit:
            self.journal.write(
                "currency_limit_not_applicable",
                symbol=symbol,
                excluded=list(decision.excluded_from_currency_limit),
            )
        if not decision.allowed:
            # journal.write, never _emit: a refusal is not broadcast to chat.
            # source and stage are what let one reject event name every path
            # apart, so the auto leg has to carry them too.
            self.journal.write(
                "reject",
                source="auto",
                stage="signal",
                symbol=symbol,
                reason=decision.reason,
                kind=sig.kind.value,
                rr=sig.rr,
            )
            return
        self._open(sig, decision.volume)

    def market_signal(
        self,
        kind: SignalKind,
        symbol: str,
        sl: float | None = None,
        tp: float | None = None,
        limit: float | None = None,
        stop: float | None = None,
    ) -> Signal:
        symbol = symbol.upper()
        spec = self.broker.symbol(symbol)
        self.broker.select_symbol(symbol)
        tick = self.broker.tick(symbol)
        if tick.bid <= 0 or tick.ask <= 0:
            raise RuntimeError(f"no tick for {symbol}")
        if limit is not None and stop is not None:
            raise RuntimeError("use limit= or stop=, not both")
        bars = self.broker.rates(
            symbol, self.cfg.strategy.timeframe, self.strategy.needed_bars()
        )
        a0 = 0.0
        if bars:
            vals = [v for v in atr_bars(bars, self.cfg.strategy.atr_period) if v == v]
            if vals:
                a0 = vals[-1]
        pending_kind = ""
        if limit is not None:
            pending_kind = "limit"
            entry = limit
            if kind is SignalKind.BUY and not (limit < tick.ask):
                raise RuntimeError("buy limit must be below ask")
            if kind is SignalKind.SELL and not (limit > tick.bid):
                raise RuntimeError("sell limit must be above bid")
        elif stop is not None:
            pending_kind = "stop"
            entry = stop
            if kind is SignalKind.BUY and not (stop > tick.ask):
                raise RuntimeError("buy stop must be above ask")
            if kind is SignalKind.SELL and not (stop < tick.bid):
                raise RuntimeError("sell stop must be below bid")
        else:
            entry = tick.ask if kind is SignalKind.BUY else tick.bid
        if sl is None:
            if a0 <= 0:
                raise RuntimeError("no ATR and no sl; pass sl=")
            dist = self.cfg.strategy.atr_stop_mult * a0
            sl = entry - dist if kind is SignalKind.BUY else entry + dist
        if tp is None:
            if a0 <= 0:
                raise RuntimeError("no ATR and no tp; pass tp=")
            dist = self.cfg.strategy.atr_tp_mult * a0
            tp = entry + dist if kind is SignalKind.BUY else entry - dist
        entry = spec.normalize_price(entry)
        sl = spec.normalize_price(sl)
        tp = spec.normalize_price(tp)
        if kind is SignalKind.BUY and not (sl < entry < tp):
            raise RuntimeError("buy needs sl < entry < tp")
        if kind is SignalKind.SELL and not (tp < entry < sl):
            raise RuntimeError("sell needs tp < entry < sl")
        return Signal(
            kind=kind,
            symbol=symbol,
            entry=entry,
            sl=sl,
            tp=tp,
            atr=a0,
            reason="manual",
            pending_kind=pending_kind,
        )

    def preview(
        self,
        signal: Signal,
        *,
        manual: bool = True,
        exclude_ticket: int | None = None,
    ) -> RiskDecision:
        positions = self.broker.positions(magic=self.cfg.risk.magic)
        if exclude_ticket is not None:
            positions = [p for p in positions if p.ticket != exclude_ticket]
        return self.risk.evaluate(
            account=self.broker.account(),
            signal=signal,
            spec=self.broker.symbol(signal.symbol),
            tick=self.broker.tick(signal.symbol),
            positions=positions,
            now=self.now_fn(),
            manual=manual,
        )

    def reverse_signal(
        self,
        ticket: int,
        sl: float | None = None,
        tp: float | None = None,
    ) -> Signal:
        if self._order(ticket) is not None:
            raise RuntimeError("reverse is for open positions")
        pos = self._pos(ticket)
        if pos is None:
            raise RuntimeError("no such ticket")
        kind = SignalKind.SELL if pos.side.value == "buy" else SignalKind.BUY
        spec = self.broker.symbol(pos.symbol)
        tick = self.broker.tick(pos.symbol)
        if tick.bid <= 0 or tick.ask <= 0:
            raise RuntimeError(f"no tick for {pos.symbol}")
        entry = tick.ask if kind is SignalKind.BUY else tick.bid
        bars = self.broker.rates(
            pos.symbol, self.cfg.strategy.timeframe, self.strategy.needed_bars()
        )
        a0 = 0.0
        if bars:
            vals = [v for v in atr_bars(bars, self.cfg.strategy.atr_period) if v == v]
            if vals:
                a0 = vals[-1]
        risk_dist = abs(pos.price_open - pos.sl) if pos.sl > 0 else 0.0
        reward_dist = abs(pos.tp - pos.price_open) if pos.tp > 0 else 0.0
        if sl is None:
            if risk_dist <= 0:
                raise RuntimeError("position has no sl; pass sl=")
            sl = entry - risk_dist if kind is SignalKind.BUY else entry + risk_dist
        if tp is None:
            if reward_dist <= 0:
                raise RuntimeError("position has no tp; pass tp=")
            tp = entry + reward_dist if kind is SignalKind.BUY else entry - reward_dist
        entry = spec.normalize_price(entry)
        sl = spec.normalize_price(sl)
        tp = spec.normalize_price(tp)
        if kind is SignalKind.BUY and not (sl < entry < tp):
            raise RuntimeError("buy needs sl < entry < tp")
        if kind is SignalKind.SELL and not (tp < entry < sl):
            raise RuntimeError("sell needs tp < entry < sl")
        return Signal(
            kind=kind,
            symbol=pos.symbol,
            entry=entry,
            sl=sl,
            tp=tp,
            atr=a0,
            reason="reverse",
        )

    def submit(
        self, signal: Signal, volume: float, client_id: str | None = None
    ) -> OrderResult:
        """Send one order. `client_id` is the idempotency key.

        The desk passes the key it minted when it STAGED the order, so the key
        survives a `/confirm` that timed out and a process restart in between; the
        auto leg passes nothing and gets a fresh key per signal, because each bar
        produces a genuinely new order rather than a retry of an old one.
        """
        if signal.pending_kind:
            return self._place_pending(signal, volume, client_id)
        return self._open(signal, volume, client_id)

    def _place_pending(
        self, signal: Signal, volume: float, client_id: str | None = None
    ) -> OrderResult:
        side = signal.side
        assert side is not None
        # Same resolution as `_open`, from the same call, and that sameness is
        # the point: `risk.evaluate()` gates a pending signal on
        # `deviation_below_spread` using `resolve_deviation`, so a pending send
        # that resolved it any other way (or not at all, which is what #92
        # found) makes the gate an opinion about a number the order never
        # carried.
        dev = self.cfg.risk.resolve_deviation(signal.symbol)
        key = client_id or new_key()
        refusal = self._unresolved(key, signal.symbol)
        if refusal is not None:
            return refusal
        order = WorkingOrder(
            symbol=signal.symbol,
            side=side,
            kind=signal.pending_kind or "limit",
            volume=volume,
            price=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            comment=stamped_comment(self.cfg.comment, key),
            magic=self.cfg.risk.magic,
            deviation=dev.points,
            client_id=key,
        )
        check = self.broker.check_working(order)
        if not self._pretrade_ok(check, signal.symbol):
            return check
        self.inflight.begin(
            key,
            symbol=signal.symbol,
            side=side.value,
            volume=volume,
            price=signal.entry,
            op="working",
        )
        try:
            result = self.broker.working(order)
        except BaseException as exc:
            self._after_unresolved_send(key, signal.symbol, exc)
            raise
        # Same partition as `_open`; see the comment there.
        if result.measured:
            self.inflight.resolve(key, "ok" if result.ok else "rejected")
        else:
            self._after_unresolved_send(
                key, signal.symbol, detail=result.comment or "no verdict"
            )
        if result.ok:
            self.risk.record_trade()
        else:
            self._report_survivor(signal.symbol, result)
        self._emit(
            "pending",
            symbol=signal.symbol,
            side=side.value,
            kind=signal.pending_kind,
            volume=volume,
            price=signal.entry,
            sl=signal.sl,
            tp=signal.tp,
            ok=result.ok,
            retcode=result.retcode,
            order=result.order,
            deviation=dev.points,
            deviation_source=dev.source,
            client_id=key,
        )
        return result

    def orders_text(self) -> str:
        rows = self.broker.orders(magic=self.cfg.risk.magic)
        if not rows:
            return "no pending orders"
        return "\n".join(
            f"#{o.ticket} {o.symbol} {_pending_label(o)} "
            f"{o.volume} @ {o.price} sl={o.sl} tp={o.tp}"
            for o in rows
        )

    def cancel_order(self, ticket: int) -> str:
        for order in self.broker.orders(magic=self.cfg.risk.magic):
            if order.ticket == ticket:
                result = self.broker.cancel(ticket)
                if not result.ok:
                    return f"cancel failed retcode={result.retcode} {result.comment}".strip()
                return f"cancelled #{ticket}"
        return "not found"

    def _resolve_pending(self) -> None:
        fn = getattr(self.broker, "resolve_pending", None)
        if not callable(fn):
            fn = getattr(self.broker, "resolve_pending_tick", None)
        if not callable(fn):
            return
        fn()

    def _manage_open(self) -> None:
        for pos in list(self.broker.positions(magic=self.cfg.risk.magic)):
            bars = self.broker.rates(
                pos.symbol, self.cfg.strategy.timeframe, self.strategy.needed_bars()
            )
            spec = self.broker.symbol(pos.symbol)
            new_sl, new_tp = self.strategy.manage(pos, bars, spec)
            self._modify(pos, new_sl, new_tp)

    def _check_stops(self) -> None:
        for pos in list(self.broker.positions(magic=self.cfg.risk.magic)):
            tick = self.broker.tick(pos.symbol)
            sl_hit = False
            tp_hit = False
            if pos.side.value == "buy":
                sl_hit = pos.sl > 0 and tick.bid <= pos.sl
                tp_hit = pos.tp > 0 and tick.bid >= pos.tp
            else:
                sl_hit = pos.sl > 0 and tick.ask >= pos.sl
                tp_hit = pos.tp > 0 and tick.ask <= pos.tp
            if sl_hit:
                self._close(pos, "sl")
                continue
            scale = self._scale_outs.get(pos.ticket)
            if scale is not None:
                px, vol = scale
                scale_hit = (
                    (pos.side.value == "buy" and tick.bid >= px)
                    or (pos.side.value == "sell" and tick.ask <= px)
                )
                if scale_hit:
                    self._close(pos, "tp", vol)
                    continue
            if tp_hit:
                self._close(pos, "tp")

    def _detect_fills(self) -> None:
        now = {p.ticket for p in self.broker.positions(magic=self.cfg.risk.magic)}
        if self._seen_pos is None:
            self._seen_pos = now
            return
        appeared = now - self._seen_pos
        vanished = self._seen_pos - now
        for ticket in sorted(appeared):
            if ticket in self._opened_this_step:
                continue
            pos = self._pos(ticket)
            self._emit(
                "open",
                fill=True,
                ticket=ticket,
                symbol=pos.symbol if pos else "",
                side=pos.side.value if pos else "",
                volume=pos.volume if pos else 0,
                price=pos.price_open if pos else 0,
                sl=pos.sl if pos else 0,
                tp=pos.tp if pos else 0,
                ok=True,
                reason="fill",
            )
        for ticket in sorted(vanished):
            if ticket in self._closed_this_step:
                continue
            self._emit("close", fill=True, ticket=ticket, reason="fill", ok=True)
        self._seen_pos = now

    def symbols_text(self) -> str:
        return " ".join(self.cfg.symbols) if self.cfg.symbols else "no symbols"

    def add_symbol(self, name: str) -> str:
        name = name.upper()
        if name in self.cfg.symbols:
            return f"already in book: {name}"
        if not self.broker.select_symbol(name):
            return f"broker rejected {name}"
        self.cfg.symbols.append(name)
        # A symbol added mid-run is as cold as one added to the config, and it
        # misses the startup warm entirely. Warm it now and answer with what was
        # measured: "added" on its own would report a symbol that cannot trade
        # as a success, which is the same silence this whole path exists to end.
        added = self.warm_history([name])
        head = f"added {name}"
        if not added.ok:
            head = f"{added.text()}\nadded {name} anyway"
        return f"{head}\n{self.symbols_text()}"

    def remove_symbol(self, name: str) -> str:
        name = name.upper()
        if name not in self.cfg.symbols:
            return f"not in book: {name}"
        if len(self.cfg.symbols) <= 1:
            return "cannot remove the last symbol"
        magic = self.cfg.risk.magic
        if any(p.symbol == name for p in self.broker.positions(magic=magic)):
            return f"{name} has open positions; /close first"
        if any(o.symbol == name for o in self.broker.orders(magic=magic)):
            return f"{name} has working orders; /cancel first"
        self.cfg.symbols = [s for s in self.cfg.symbols if s != name]
        return f"removed {name}\n{self.symbols_text()}"

    def quote_text(self, symbol: str) -> str:
        symbol = symbol.upper()
        tick = self.broker.tick(symbol)
        parts = [f"{symbol} bid={tick.bid} ask={tick.ask} spread={tick.spread:.6f}"]
        bars = self.broker.rates(symbol, self.cfg.strategy.timeframe, self.strategy.needed_bars())
        if bars:
            closes = [b.close for b in bars]
            f0 = last_closed(ema(closes, self.cfg.strategy.fast_ema))
            s0 = last_closed(ema(closes, self.cfg.strategy.slow_ema))
            a0 = last_closed(atr_bars(bars, self.cfg.strategy.atr_period))
            x0 = last_closed(adx(bars, self.cfg.strategy.adx_period)[0])
            bits = []
            if a0 == a0:
                bits.append(f"atr={a0:.6f}")
            if x0 == x0:
                bits.append(f"adx={x0:.1f}")
            if f0 == f0:
                bits.append(f"fast={f0:.5f}")
            if s0 == s0:
                bits.append(f"slow={s0:.5f}")
            if bits:
                parts.append(" ".join(bits))
        return " ".join(parts)

    def close_by(self, ticket: int, other: int) -> str:
        if ticket == other:
            return "tickets must differ"
        a = self._pos(ticket)
        b = self._pos(other)
        if a is None or b is None:
            return "no such ticket"
        if a.symbol != b.symbol:
            return "symbols must match"
        if a.side == b.side:
            return "sides must be opposite"
        spec = self.broker.symbol(a.symbol)
        vol = min(a.volume, b.volume)
        rem_a = round(a.volume - vol, 8)
        rem_b = round(b.volume - vol, 8)
        if rem_a > 1e-12 and rem_a < spec.volume_min - 1e-12:
            return "remainder below volume_min"
        if rem_b > 1e-12 and rem_b < spec.volume_min - 1e-12:
            return "remainder below volume_min"
        result = self.broker.close_by(ticket, other, symbol=a.symbol)
        if result.ok:
            self._closed_this_step.add(ticket)
            self._closed_this_step.add(other)
            if rem_a <= 1e-12:
                self._scale_outs.pop(ticket, None)
            if rem_b <= 1e-12:
                self._scale_outs.pop(other, None)
        self._emit(
            "close",
            reason="closeby",
            ticket=ticket,
            position_by=other,
            symbol=a.symbol,
            ok=result.ok,
            retcode=result.retcode,
            comment=result.comment,
            volume=vol,
        )
        if not result.ok:
            return f"closeby failed retcode={result.retcode} {result.comment}"
        return f"closed #{ticket} by #{other}"

    def close_ticket(self, ticket: int, reason: str, volume: float | None = None) -> str:
        pos = self._pos(ticket)
        if pos is None:
            return "no such ticket"
        if volume is not None and volume <= 0:
            raise ValueError("volume must be > 0")
        result = self._close(pos, reason, volume)
        if not result.ok:
            return f"close failed retcode={result.retcode} {result.comment}"
        return "closed"

    def close_symbol(self, symbol: str, reason: str) -> int:
        n = 0
        for pos in list(self.broker.positions(magic=self.cfg.risk.magic)):
            if pos.symbol == symbol:
                result = self._close(pos, reason)
                if result.ok:
                    n += 1
        return n

    def close_all(self, reason: str) -> int:
        n = 0
        for pos in list(self.broker.positions(magic=self.cfg.risk.magic)):
            result = self._close(pos, reason)
            if result.ok:
                n += 1
        return n

    def set_sl(self, ticket: int, price: float) -> str:
        pos = self._pos(ticket)
        if pos is not None:
            result = self._modify(pos, price, pos.tp)
            if not result.ok:
                return f"sl failed retcode={result.retcode} {result.comment}"
            return f"sl #{ticket} -> {price}"
        order = self._order(ticket)
        if order is None:
            return "no such ticket"
        result = self._modify_pending(order, sl=price, tp=order.tp)
        if not result.ok:
            return f"sl failed retcode={result.retcode} {result.comment}"
        return f"sl #{ticket} -> {price}"

    def set_tp(self, ticket: int, price: float, volume: float | None = None) -> str:
        if volume is not None:
            return self._set_scale_out(ticket, price, volume)
        pos = self._pos(ticket)
        if pos is not None:
            result = self._modify(pos, pos.sl, price)
            if not result.ok:
                return f"tp failed retcode={result.retcode} {result.comment}"
            return f"tp #{ticket} -> {price}"
        order = self._order(ticket)
        if order is None:
            return "no such ticket"
        result = self._modify_pending(order, sl=order.sl, tp=price)
        if not result.ok:
            return f"tp failed retcode={result.retcode} {result.comment}"
        return f"tp #{ticket} -> {price}"

    def _set_scale_out(self, ticket: int, price: float, volume: float) -> str:
        pos = self._pos(ticket)
        if pos is None:
            return "scale-out needs an open position"
        trip = self.risk.circuit(self.broker.account(), self.now_fn())
        if not trip.allowed:
            return f"refused: {trip.reason}"
        spec = self.broker.symbol(pos.symbol)
        vol = normalize_volume(volume, spec)
        if vol <= 0:
            return "volume below min lot"
        if vol > pos.volume + 1e-12:
            return "volume exceeds position"
        remaining = round(pos.volume - vol, 8)
        if remaining > 1e-12 and remaining < spec.volume_min - 1e-12:
            return "remainder below volume_min"
        px = spec.normalize_price(price)
        if pos.side.value == "buy" and not (px > pos.price_open):
            return "buy tp must be above entry"
        if pos.side.value == "sell" and not (px < pos.price_open):
            return "sell tp must be below entry"
        if remaining <= 1e-12:
            result = self._modify(pos, pos.sl, px)
            if not result.ok:
                return f"tp failed retcode={result.retcode} {result.comment}"
            return f"tp #{ticket} -> {px}"
        self._scale_outs[ticket] = (px, vol)
        self.journal.write(
            "modify",
            ticket=ticket,
            symbol=pos.symbol,
            tp=px,
            volume=vol,
            scale_out=True,
        )
        return f"tp #{ticket} {px} vol={vol}"

    def _order(self, ticket: int) -> PendingOrder | None:
        for order in self.broker.orders(magic=self.cfg.risk.magic):
            if order.ticket == ticket:
                return order
        return None

    def replace_pending(self, ticket: int, price: float) -> str:
        if self._pos(ticket) is not None:
            return "replace is for working orders"
        order = self._order(ticket)
        if order is None:
            return "no such ticket"
        reason = self.risk.circuit_reason(self.broker.account(), self.now_fn())
        if reason:
            return f"refused: {reason}"
        spec = self.broker.symbol(order.symbol)
        tick = self.broker.tick(order.symbol)
        px = spec.normalize_price(price)
        if order.kind == "limit":
            if order.side.value == "buy" and not (px < tick.ask):
                return "buy limit must be below ask"
            if order.side.value == "sell" and not (px > tick.bid):
                return "sell limit must be above bid"
        elif order.kind == "stop":
            if order.side.value == "buy" and not (px > tick.ask):
                return "buy stop must be above ask"
            if order.side.value == "sell" and not (px < tick.bid):
                return "sell stop must be below bid"
        else:
            return "working kind must be limit or stop"
        sl, tp = order.sl, order.tp
        if order.side.value == "buy" and not (sl < px and (tp <= 0 or px < tp)):
            return "buy needs sl < entry < tp"
        if order.side.value == "sell" and not (sl > px and (tp <= 0 or tp < px)):
            return "sell needs tp < entry < sl"
        worst = money_per_lot_at_stop(px, sl, spec) * order.volume
        cap = self.broker.account().equity * self.cfg.risk.risk_pct * self.cfg.risk.max_risk_multiple
        if worst > cap + 1e-6:
            return "refused: size_exceeds_risk"
        result = self._modify_pending(order, sl=sl, tp=tp, price=px)
        if not result.ok:
            return f"replace failed retcode={result.retcode} {result.comment}"
        return f"replace #{ticket} -> {px}"

    def _modify_pending(
        self,
        order: PendingOrder,
        sl: float,
        tp: float,
        price: float | None = None,
    ) -> OrderResult:
        spec = self.broker.symbol(order.symbol)
        sl_n = spec.normalize_price(sl) if sl else 0.0
        tp_n = spec.normalize_price(tp) if tp else 0.0
        if sl_n <= 0:
            return OrderResult.invalid_stops("sl required")
        entry = spec.normalize_price(price) if price is not None else order.price
        if order.side.value == "buy" and not (sl_n < entry and (tp_n <= 0 or entry < tp_n)):
            return OrderResult.invalid_stops("buy needs sl < entry < tp")
        if order.side.value == "sell" and not (sl_n > entry and (tp_n <= 0 or tp_n < entry)):
            return OrderResult.invalid_stops("sell needs tp < entry < sl")
        kind = order.kind if order.kind in _ORDER_TYPE_NAME else "limit"
        result = self.broker.modify_working(
            order.ticket,
            price=entry,
            sl=sl_n,
            tp=tp_n,
            symbol=order.symbol,
            volume=order.volume,
            side=order.side.value,
            kind=kind,
        )
        self.journal.write(
            "modify",
            ticket=order.ticket,
            symbol=order.symbol,
            price=entry,
            sl=sl_n,
            tp=tp_n,
            pending=True,
            ok=result.ok,
            retcode=result.retcode,
        )
        return result

    def breakeven(self, ticket: int) -> str:
        pos = self._pos(ticket)
        if pos is None:
            return "no such ticket"
        entry = pos.price_open
        tick = self.broker.tick(pos.symbol)
        if pos.side.value == "buy":
            if pos.sl > 0 and pos.sl >= entry - 1e-12:
                return "be would loosen sl"
            if tick.bid < entry:
                return "not in profit"
        else:
            if pos.sl > 0 and pos.sl <= entry + 1e-12:
                return "be would loosen sl"
            if tick.ask > entry:
                return "not in profit"
        result = self._modify(pos, entry, pos.tp)
        if not result.ok:
            return f"be failed retcode={result.retcode} {result.comment}"
        return f"be #{ticket} sl -> {entry}"

    def trail(self, ticket: int) -> str:
        pos = self._pos(ticket)
        if pos is None:
            return "no such ticket"
        bars = self.broker.rates(
            pos.symbol, self.cfg.strategy.timeframe, self.strategy.needed_bars()
        )
        spec = self.broker.symbol(pos.symbol)
        new_sl, new_tp = self.strategy.manage(pos, bars, spec)
        if abs(new_sl - pos.sl) < 1e-12 and abs((new_tp or 0) - (pos.tp or 0)) < 1e-12:
            return f"trail #{ticket} unchanged"
        result = self._modify(pos, new_sl, new_tp)
        if not result.ok:
            return f"trail failed retcode={result.retcode} {result.comment}"
        return f"trail #{ticket} sl -> {new_sl}"

    def risk_text(self) -> str:
        acct = self.broker.account()
        self.risk.observe(acct, self.now_fn())
        snap = self.risk.snapshot
        r = self.cfg.risk
        daily_loss = snap.day_start_equity - acct.equity
        daily_cap = snap.day_start_equity * r.daily_loss_pct
        dd = snap.peak_equity - acct.equity
        dd_cap = snap.peak_equity * r.max_drawdown_pct if snap.peak_equity else 0.0
        n = len(self.broker.positions(magic=r.magic))
        return (
            f"risk_pct={r.risk_pct:.2%}  positions={n}/{r.max_positions}\n"
            f"daily_loss={daily_loss:.2f}/{daily_cap:.2f}  "
            f"drawdown={dd:.2f}/{dd_cap:.2f}\n"
            f"equity={acct.equity:.2f} peak={snap.peak_equity:.2f} "
            f"day_start={snap.day_start_equity:.2f}"
            + (
                f"\nrisk state {self.risk.halt_reason}: {self.risk.state_error}"
                if self.risk.state_error
                else ""
            )
        )

    def history_text(self, n: int = 15) -> str:
        rows = self.journal.tail(n)
        if not rows:
            return "no history"
        lines = []
        for rec in rows:
            ev = rec.get("event", "")
            ts = str(rec.get("ts", ""))[:19]
            extra = " ".join(
                f"{k}={v}"
                for k, v in rec.items()
                if k not in {"ts", "event"} and v not in (None, "")
            )
            lines.append(f"{ts} {ev} {extra}".strip())
        return "\n".join(lines)

    def _pos(self, ticket: int) -> Position | None:
        for pos in self.broker.positions(magic=self.cfg.risk.magic):
            if pos.ticket == ticket:
                return pos
        return None

    def advice_circuit_reason(self) -> str:
        return self.risk.circuit_reason(self.broker.account(), self.now_fn())

    def advice_history(self, n: int = 40) -> list[dict[str, Any]]:
        return self.journal.tail(n)

    def advice_context(self) -> str:
        lines = [
            self.status_text(),
            self.risk_text(),
            self.positions_text(),
            self.orders_text(),
            f"symbols={','.join(self.cfg.symbols)} risk_pct={self.cfg.risk.risk_pct}",
            f"auto={self.cfg.strategy.auto} trail={self.cfg.strategy.trail} "
            f"provider={self.cfg.advice.provider}",
            "Advice may stage a trade. Default send is /confirm. "
            "/approve always sends after risk preview.",
        ]
        reason = self.advice_circuit_reason()
        if reason:
            lines.append(
                f"CIRCUIT would halt ({reason}). Action must be hold or close. "
                "Do not buy or sell."
            )
        else:
            lines.append(
                "If daily_loss or drawdown room is gone, action must be hold or close."
            )
        for name in self.cfg.symbols:
            try:
                lines.append(self.quote_text(name))
            except RuntimeError:
                continue
        return "\n".join(lines)

    def status_text(self) -> str:
        acct = self.broker.account()
        reason = self.risk.halt_reason or ("halt_file" if self.risk.halt_path().exists() else "")
        halt = "HALTED " + reason if (self.halted or reason) else "running"
        return (
            f"straightedge {halt}\n"
            f"mode={self.cfg.mode} server={acct.server}\n"
            f"equity={acct.equity:.2f} {acct.currency}  "
            f"balance={acct.balance:.2f}  peak={self.risk.snapshot.peak_equity:.2f}\n"
            f"positions={len(self.broker.positions(magic=self.cfg.risk.magic))}  "
            f"risk={self.cfg.risk.risk_pct:.2%}"
        )

    def positions_text(self) -> str:
        rows = self.broker.positions(magic=self.cfg.risk.magic)
        if not rows:
            return "no open positions"
        lines = []
        for p in rows:
            extra = ""
            scale = self._scale_outs.get(p.ticket)
            if scale is not None:
                extra = f" scale={scale[1]} @{scale[0]}"
            lines.append(
                f"#{p.ticket} {p.symbol} {p.side.value} {p.volume} "
                f"@ {p.price_open} sl={p.sl} tp={p.tp} pnl={p.profit:.2f}{extra}"
            )
        return "\n".join(lines)

    def handle_command(self, cmd: TgCommand) -> str:
        return self.desk.handle(cmd)

    def poll_telegram(self) -> None:
        if self.telegram is None:
            return
        timeout = max(0, int(self.cfg.poll_seconds))
        try:
            cmds = self.telegram.poll_commands(timeout=timeout)
        except (ValueError, RuntimeError, OSError):
            return
        for cmd in cmds:
            try:
                self.telegram.send(self.desk.handle(cmd))
            except (ValueError, RuntimeError, OSError) as exc:
                try:
                    self.telegram.send(f"error: {redact_text(str(exc))}")
                except (ValueError, RuntimeError, OSError):
                    pass
            finally:
                self.telegram.ack(cmd.update_id)

    def recap_text(self) -> str:
        acct = self.broker.account()
        snap = self.risk.snapshot
        day = snap.day_key or day_key(self.now_fn())
        pnl = acct.equity - snap.day_start_equity
        sign = "+" if pnl >= 0 else ""
        head = (
            f"RECAP {day} equity={acct.equity:.2f} "
            f"day_start={snap.day_start_equity:.2f} pnl={sign}{pnl:.2f}"
        )
        tail = self.history_text(8)
        if tail and tail != "no history":
            return f"{head}\n{tail}"
        return head

    def _maybe_daily_recap(self, acct, now) -> None:
        key = day_key(now)
        snap = self.risk.snapshot
        if not snap.day_key or snap.day_key == key:
            return
        pnl = acct.equity - snap.day_start_equity
        tail = self.history_text(8)
        self._emit(
            "recap",
            day=snap.day_key,
            equity=round(acct.equity, 2),
            day_start=round(snap.day_start_equity, 2),
            pnl=round(pnl, 2),
            tail="" if tail == "no history" else tail,
        )

    def _reconnect_broker(self) -> bool:
        try:
            self.broker.disconnect()
        except (RuntimeError, OSError, ValueError):
            pass
        try:
            self.broker.connect()
            for name in self.cfg.symbols:
                try:
                    self.broker.select_symbol(name)
                except (RuntimeError, OSError, ValueError):
                    continue
            self.journal.write("reconnect", ok=True)
            return True
        except (RuntimeError, OSError, ValueError) as exc:
            self.journal.write("reconnect", ok=False, error=str(exc)[:200])
            return False

    def _write_heartbeat(self, now: datetime | None = None, *, blocked: str = "") -> None:
        """Publish liveness AND whether this desk would trade.

        `blocked` is the gate's own named reason, passed in from the one place
        that already evaluated it. The file carried a timestamp ALONE from 1.0.0
        until the watchdog landed, which made a desk that came back DISARMED
        after a restart indistinguishable from one that was trading: the
        operator saw a fresh timestamp either way. Line one is still that
        timestamp, byte for byte.
        """
        dest = watchdog.heartbeat_path_for(self.journal.path)
        tmp = dest.with_name(dest.name + ".tmp")
        ts = now or self.now_fn()
        mono = time.monotonic()
        if self._hb_last_mono is not None:
            self._hb_gap_max_s = max(self._hb_gap_max_s, mono - self._hb_last_mono)
        self._hb_last_mono = mono
        budget = watchdog.tick_budget_seconds(self.cfg)
        if self._hb_gap_max_s > budget and not self._hb_over_warned:
            # The threshold is derived from config and this measurement says the
            # derivation is too tight for this book. Report it; do NOT widen it.
            self._hb_over_warned = True
            print(
                f"heartbeat: a gap of {self._hb_gap_max_s:.1f}s between ticks "
                f"exceeds the derived budget of {budget}s, so the watchdog "
                "threshold can produce a false STALE. It was not widened.",
                flush=True,
            )
        tmp.write_text(
            watchdog.render(
                ts,
                blocked=blocked,
                mode=self.cfg.mode,
                stale_after_s=watchdog.stale_after_seconds(self.cfg),
                tick_budget_s=budget,
                tick_gap_max_s=self._hb_gap_max_s,
            ),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        tmp.replace(dest)

    def step_all(self) -> None:
        self.poll_telegram()
        try:
            ensure = getattr(self.broker, "ensure_connected", None)
            if callable(ensure):
                ensure()
            acct = self.broker.account()
        except (RuntimeError, OSError, ValueError):
            if not self._reconnect_broker():
                return
            try:
                acct = self.broker.account()
            except (RuntimeError, OSError, ValueError):
                return
        now = self.now_fn()
        self._maybe_daily_recap(acct, now)
        if self.halted:
            self._write_heartbeat(now, blocked=self.risk.halt_reason or "halted")
            return
        tripped = self._apply_circuit(acct, now)
        if tripped:
            self._write_heartbeat(now, blocked=tripped)
            return
        self._opened_this_step.clear()
        self._closed_this_step.clear()
        self._resolve_pending()
        self._check_stops()
        if self.cfg.strategy.trail:
            self._manage_open()
        if self.cfg.strategy.auto:
            for symbol in self.cfg.symbols:
                if self.halted:
                    break
                self.step_symbol(symbol)
        self._detect_fills()
        # Reaching here does NOT mean the desk would trade. `circuit()` returns
        # not-allowed-WITHOUT-halt for `live_not_accepted` and
        # `trade_not_allowed`, so `_apply_circuit` above let the tick through
        # while a send would still be refused. Asking the gate itself is what
        # separates "ticking and armed" from "ticking, and disarmed by the
        # restart that fc34 requires". It re-observes the same acct at the same
        # `now`, so the snapshot cannot move and nothing is persisted twice.
        self._write_heartbeat(now, blocked=self.risk.circuit_reason(acct, now))


def _format_event(event: str, fields: dict[str, Any]) -> str:
    if event == "start":
        approve = "allowed" if fields.get("approve_always_allowed", True) else "disabled"
        auto = "allowed" if fields.get("auto_allowed", True) else "disabled"
        return (
            f"start mode={fields.get('mode')} equity={fields.get('equity')} "
            f"symbols={fields.get('symbols')} approve_always={approve} auto={auto}"
        )
    if event == "stop":
        return "stop"
    if event == "open":
        tag = "FILL/OPEN" if fields.get("fill") or fields.get("reason") == "fill" else "OPEN"
        return (
            f"{tag} {fields.get('side')} {fields.get('symbol')} "
            f"vol={fields.get('volume')} @ {fields.get('price')} "
            f"sl={fields.get('sl')} tp={fields.get('tp')} ok={fields.get('ok')}"
        )
    if event == "close":
        tag = "FILL/CLOSE" if fields.get("fill") or fields.get("reason") == "fill" else "CLOSE"
        return (
            f"{tag} {fields.get('symbol')} #{fields.get('ticket')} "
            f"reason={fields.get('reason')} ok={fields.get('ok')}"
        )
    if event == "halt":
        return f"HALT {fields.get('reason')} equity={fields.get('equity')}"
    if event == "flatten":
        # Journal-only: the denominator of a clean sweep needs a record, not a ping.
        return ""
    if event == "flatten_incomplete":
        count = int(fields.get("survivor_count") or 0)
        lines = [
            f"FLATTEN INCOMPLETE: {count} still open (reason={fields.get('reason')})",
            f"positions requested={fields.get('requested')} "
            f"confirmed_closed={fields.get('confirmed_closed')} "
            f"closed_elsewhere={fields.get('closed_elsewhere')}",
        ]
        survivors = list(fields.get("survivors") or [])
        if survivors:
            lines.append("still open: " + ", ".join(f"#{t}" for t in survivors))
        residual = list(fields.get("residual") or [])
        if residual:
            lines.append(
                "partial fill left residual volume on "
                + ", ".join(f"#{t}" for t in residual)
            )
        orders = list(fields.get("order_survivors") or [])
        if orders:
            lines.append(
                f"{len(orders)} working order(s) not cancelled: "
                + ", ".join(f"#{t}" for t in orders)
            )
        if not fields.get("measured", True):
            lines.append(
                "COULD NOT MEASURE the post-sweep state; every requested ticket is "
                "counted as still open (upper bound)"
            )
        lines.append("HALTED; no new entries. Check the terminal.")
        return "\n".join(lines)
    if event == "order_check_fail":
        if fields.get("reason") == "not_measured":
            return (
                f"order_check NOT MEASURED {fields.get('symbol')} "
                f"no pre-trade answer, order NOT sent ({fields.get('comment')})"
            )
        return (
            f"order_check_fail {fields.get('symbol')} "
            f"broker refused retcode={fields.get('retcode')}"
        )
    if event == "pending":
        return (
            f"PENDING {fields.get('kind')} {fields.get('side')} {fields.get('symbol')} "
            f"vol={fields.get('volume')} @ {fields.get('price')} "
            f"ok={fields.get('ok')} order={fields.get('order')}"
        )
    if event == "recap":
        pnl = float(fields.get("pnl") or 0)
        sign = "+" if pnl >= 0 else ""
        head = (
            f"RECAP {fields.get('day')} equity={fields.get('equity')} "
            f"day_start={fields.get('day_start')} pnl={sign}{pnl}"
        )
        tail = str(fields.get("tail") or "")
        return f"{head}\n{tail}".strip()
    if event == "history_preflight":
        # Journal-only. The all-clear is a denominator, not news.
        return ""
    if event == "history_unavailable":
        return str(fields.get("text") or "")
    if event == "unmanaged_position":
        return (
            f"UNMANAGED POSITION #{fields.get('ticket')} {fields.get('symbol')}: the send "
            f"reported FAILED (retcode={fields.get('retcode')}) but this ticket is still "
            f"open and has NO STOP. Close or protect it in the terminal now."
        )
    if event == "survivor_unknown":
        # TWO states reach this event and they need different instructions.
        #
        # A send TIMEOUT is a CURRENT Expert answering honestly: it put the
        # request on the wire, the reply was lost, and it omits
        # `survivor_ticket` because absence is this ICD's encoding for "could
        # not measure". Telling that operator to update the Expert is wrong
        # three ways -- the send did not fail, the reason WAS reported, and the
        # Expert is already current -- and acting on it means detaching the
        # Expert while an unstopped position may be live. The order may be on
        # the book, so the only correct instruction is to look.
        #
        # Absence with NO reason is the other state: an Expert older than the
        # `survivor_ticket` contract, which cannot answer at all. There
        # updating it is exactly the fix.
        if str(fields.get("comment") or "") == MT4_SEND_TIMEOUT_UNKNOWN:
            return (
                f"COULD NOT MEASURE {fields.get('symbol')}: the send TIMED OUT, so it may "
                f"already be on the book (retcode={fields.get('retcode')}). It will NOT be "
                f"sent again. Check the terminal for this order before you re-stage it."
            )
        return (
            f"COULD NOT MEASURE {fields.get('symbol')}: a send failed "
            f"(retcode={fields.get('retcode')}) and the Expert did not say whether it left "
            f"a position open. Update Mt4RiskBot.mq4, then check the terminal."
        )
    return ""


def run_backtest(cfg: BotConfig, series: dict[str, list[Bar]], *, journal_path: str) -> dict:
    from straightedge.broker.paper import PaperBroker

    broker = PaperBroker(balance=cfg.initial_balance)
    for name, bars in series.items():
        if bars:
            broker.seed_bars(name, bars[:1])
    engine = Engine(cfg, broker, journal=Journal(journal_path))
    engine.start()
    maxlen = max((len(v) for v in series.values()), default=0)
    names = list(series.keys())
    for i in range(1, maxlen):
        if engine.halted:
            break
        for name in names:
            bars = series[name]
            if i >= len(bars):
                continue
            broker.on_bar(name, bars[i])
            engine.replay_symbol(name, bars[: i + 1])
    engine.stop()
    acct = broker.account()
    return {
        "balance": acct.balance,
        "equity": acct.equity,
        "peak": engine.risk.snapshot.peak_equity,
        "halted": engine.halted,
        "open_positions": len(broker.positions()),
    }
