"""One path for live, paper, and backtest: strategy proposes, risk sizes, broker sends."""

from __future__ import annotations

import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from straightedge.atomic import (
    REPLACE_RETRY_SECONDS,
    replace_retrying_on_share_conflict,
)
from straightedge.broker.base import Broker, venue_clock_of
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
    VenueClock,
    Bar,
    FlattenReport,
    MarketOrder,
    OrderResult,
    PendingOrder,
    Position,
    Signal,
    SignalKind,
    Tick,
    WorkingOrder,
)
from straightedge.risk import (
    SYMBOL_FX,
    RiskDecision,
    RiskManager,
    UnclassifiedSymbol,
    classify_symbol,
    currency_exposure,
    day_key,
)
from straightedge.sizing import (
    MissingStop,
    money_per_lot_at_stop,
    normalize_volume,
    unusable_price,
    unusable_stop,
    unusable_volume,
)
from straightedge.state import snapshot_path_for
from straightedge.strategy import TrendStrategy
from straightedge.telegram import TelegramClient, TgCommand
from straightedge import deployed, watchdog
from straightedge import __version__

#: Nothing longer than this reaches the journal from a fault message. It is the
#: clip `_reconnect_broker` already applied to its own `error`, now applied to
#: every recorded fault: see `Engine._fault_fields` for why a journal row that
#: records a cause has to stay bounded.
_FAULT_CHARS = 200
#: Fault enums (`op`, `transport`, `phase`, `withdrawal`) come from this repo's
#: own vocabulary in `broker/mt4_live.py` and are a few characters each. The clip
#: is insurance against a transport that one day reports something longer, not a
#: budget anybody is expected to reach.
_FAULT_ENUM_CHARS = 40

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

#: Longest rendering `history_text` gives any ONE journal row before it clips
#: and says so. The journal FILE keeps the whole row either way; this bounds
#: only what the chat renders.
#:
#: Measured, not chosen: the widest row an ordinary session writes is
#: `history_preflight` at 797 characters for a four-symbol desk, and it is the
#: only row whose width scales with configuration. 1000 keeps that headroom, so
#: a clip marker means 'this row is anomalous' and never 'this desk runs a lot
#: of symbols'.
#:
#: A bound on SIZE, deliberately not a denylist on the one field that caused
#: straightedge#119: the recap's stored `tail` is gone at the source, but
#: journals written before that fix still hold it, and the next oversized field
#: would re-open the same hole. One row cannot crowd the other fourteen out of
#: the message.
HISTORY_ROW_CHARS = 1000


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


#: The working-order book could not be READ, so commitment is unmeasured.
#:
#: `broker.orders()` raises on MT4 when the Expert answers an error, and the
#: gates that count commitments cannot run without it. Reading a failed read as
#: "no working orders" would fail OPEN in exactly the case those gates exist for,
#: so it refuses instead. Same partition as `OrderResult.measured`: PASSED,
#: REFUSED, COULD NOT MEASURE.
ORDERS_UNMEASURED = "orders_unmeasured"

#: The venue could not state its UTC offset, so WHEN it is cannot be measured.
#:
#: Same partition as `ORDERS_UNMEASURED` one line up, applied to the clock
#: instead of the book: PASSED, REFUSED, COULD NOT MEASURE. A bar's `time`
#: is the broker server's wall clock, every gate below the auto leg is
#: written in UTC, and the offset between the two is a measurement. When it
#: is missing the leg refuses, because the alternative is to assume UTC, and
#: assuming UTC IS straightedge#172.
VENUE_CLOCK_UNMEASURED = "venue_clock_unmeasured"

#: The clock was measured and the venue's OWN BARS contradict it.
#:
#: A measured offset says what the server's wall clock reads now. The
#: forming bar's open time is that same clock, from a different reply, and
#: a server cannot be forming a bar that has not opened yet. So if the
#: offset implies a server time EARLIER than the bar the venue just served,
#: the tick stamp the offset came from was stale and the offset is wrong by
#: that staleness.
#:
#: This NARROWS the one hole `VenueClock.measure` cannot close on its own; it
#: does not close it, and the difference is measured rather than argued
#: (straightedge#193). A staleness that is an exact multiple of the offset
#: grid lands on a grid point and looks perfect, and the civil timezone band
#: does not see it either (11h is 44 whole grid steps). Measured through the
#: real adapter during the straightedge#182 review: a stamp frozen 2h at
#: Friday's close on a UTC+3 server read as UTC+01:00, 5h as UTC-02:00, 11h
#: as UTC-08:00. Every one of those implies a server time hours before the
#: bar in hand, so every one of them refuses here.
#:
#: WHAT IT DOES NOT CATCH. The comparison is against the forming bar's OPEN,
#: so it sees a staleness only once that staleness exceeds the AGE of that
#: bar. It is therefore blind in the last moments before a bar closes:
#: measured at a bar 899s old on a 900s series, with the caller's bound
#: VIOLATED, a 900s-stale stamp is accepted and the instant is wrong by 900s.
#: The residual is bounded by one bar period and the hole only opens when the
#: measured bound is violated, which takes a defect in `step_symbol`'s own
#: bookkeeping rather than anything a venue can do. The boundary is pinned by
#: test rather than left to this sentence.
#:
#: It cannot produce a false refusal: a correct offset implies the server's
#: real `now`, and the forming bar opened at or before that instant by
#: definition. Both readings come from the same server clock, so our own
#: clock cancels out of the comparison entirely. Swept across bar ages and
#: staleness with an honest bound during the straightedge#182 review: 0 false
#: refusals in 32 combinations.
VENUE_CLOCK_BAR_DISAGREES = "venue_clock_bar_disagrees"

#: Slack on that comparison, for the integer rounding of the paired sample
#: only. It is NOT a staleness allowance: one second is below the grid by
#: three orders of magnitude, so it can absorb a rounding edge and nothing
#: else.
VENUE_CLOCK_BAR_SLACK_SEC = 1

#: How many ticks may attempt the `tick_gap_breach` write before the record is
#: declared lost (straightedge#217).
#:
#: BOUNDED, because the one-line fix is wrong. Setting the flag after the write
#: and leaving it at that retries on every tick for as long as the journal
#: stays broken, which is an unbounded retry inside the latency path this very
#: record exists to explain; `_write_heartbeat` already refuses a venue round
#: trip for exactly that reason.
#:
#: Three, because at the default `poll_seconds` that is about 45 seconds of
#: opportunity, which covers a reader holding the journal open across a
#: rotation, and because the cost of being wrong is three attempted file opens
#: rather than one. It is NOT a retry inside `Journal.write`: that would be a
#: change to every journal write on every path, and the four unguarded
#: write-tmp-then-replace sites are tracked separately (straightedge#251).
_HB_BREACH_WRITE_ATTEMPTS = 3

#: No clock has been recorded yet. Distinct from every real state, because
#: an unmeasured clock legitimately carries `offset_sec=None` and a tuple
#: built from it must not collide with "nothing recorded".
_CLOCK_UNRECORDED: tuple[Any, ...] = ("unrecorded",)

#: The heartbeat's retry window. The figure and the reasoning live in
#: `straightedge.atomic`; this name is kept because the heartbeat's own
#: tests pin the window against `poll_seconds` from here, and because a
#: reader of `_write_heartbeat` should find the window named at this level
#: rather than having to follow an import (straightedge#251).
HEARTBEAT_REPLACE_RETRY_SECONDS = REPLACE_RETRY_SECONDS

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
        #: When this symbol was last polled, on the MONOTONIC clock.
        #:
        #: This is the measured staleness bound for the venue clock, and it
        #: is the whole reason a bar-derived instant can be trusted. A bar
        #: advanced between the previous poll and this one, so a tick
        #: arrived in that interval, so `TimeCurrent()` cannot be older
        #: than the interval. Monotonic on purpose: a wall-clock step (NTP,
        #: a DST-confused host) must not be able to widen a safety bound.
        #:
        #: It is MEASURED rather than derived from `poll_seconds`, because
        #: this repo's own tick budget for a live MT4 config is 270s of
        #: bounded part with an explicitly unbounded tail, and
        #: `watchdog.py` publishes `tick_gap_max_s` precisely because that
        #: budget can be exceeded. A number the codebase already
        #: instruments because it can be false is not a bound
        #: (straightedge#182 review).
        self._last_poll_mono: dict[str, float] = {}
        #: The venue clock state this process has already RECORDED, as the
        #: comparable part of it (offset, unmeasured fields, source). A
        #: sentinel rather than None, so the first reading is always a change.
        #:
        #: IT GUARDS A FUTURE SHAPE, NOT A PRESENT COLLISION, and the
        #: distinction is the point: a comment asserting a hazard the code
        #: cannot reach is what `docs/TESTING.md` just shipped a section about.
        #: Today the state tuple has three elements and this has one, so
        #: equality is impossible by arity alone, and
        #: `VenueClock.__post_init__` refuses `offset_sec=None` beside an empty
        #: `unmeasured` while `not_measured` always names a field and
        #: `declared` always sets an offset, so no constructor can build the
        #: colliding state either. A new constructor, or a narrower state
        #: tuple, could: `None` is a legitimate `offset_sec` and an empty
        #: marker matching it would swallow the opening record.
        #:
        #: In memory on purpose (straightedge#186). A restart re-records at
        #: `start()`, which is the boundary an operator reads anyway, so a
        #: durable marker would guard nothing this does not already state.
        #: Nothing about the daily-recap marker's reasoning transfers here
        #: and it is not borrowed: that one had to survive a crash loop
        #: because its subject was a day that could be lost; this one's
        #: subject is re-measured on the next bar.
        self._clock_recorded: tuple[Any, ...] = _CLOCK_UNRECORDED
        self.halted = False
        #: The `day_key` the daily recap has already been emitted for.
        #:
        #: The recap needs a marker of its OWN because it cannot use the one
        #: thing that looks like one. `snap.day_key` is rolled by
        #: `RiskManager.observe`, reached through `_apply_circuit`, and
        #: `step_all` returns BEFORE that when `self.halted`. So a halted desk
        #: never rolled the day and re-emitted the recap on every single tick
        #: (straightedge#119). Moving the rollover earlier is NOT the fix:
        #: `day_start_equity` resets with it, and that is the baseline the
        #: daily-loss budget is measured against, so it would change refusal
        #: behaviour on the real-money path.
        #:
        #: In memory, and the argument for that is now NARROWER than it was.
        #: It used to read: `start()` calls `risk.observe()` before the first
        #: tick, so after any restart `snap.day_key` already equals today and
        #: `_maybe_daily_recap` cannot owe a recap for a day that ended before
        #: the restart, so a journal-restored marker would guard a state no
        #: restart can reach. The first half is still true and the conclusion
        #: was wrong: the state IS reachable, it just is not reachable through
        #: THIS marker. The day that ended while the desk was down is owed by
        #: nobody and was therefore silently lost (straightedge#129).
        #:
        #: So the two paths now have two markers, deliberately, because they
        #: guard two different things. `_maybe_daily_recap` is in-process and
        #: in-memory: it fires on a boundary this process watched, so its marker
        #: only has to outlive a tick. `_announce_unrecapped_day` reads the
        #: JOURNAL instead, because the thing it must not repeat is an
        #: announcement made by a PREVIOUS process, and a crash loop clears
        #: anything in memory. Neither marker is in the equity snapshot: that
        #: file is the money gate's input and a recap bookkeeping field has no
        #: business in it.
        self._recapped_day = ""
        #: Heartbeat bookkeeping. `_hb_gap_max_s` is the largest gap between two
        #: heartbeat writes this process has actually seen, published so the
        #: derived staleness threshold can be checked against reality instead of
        #: trusted. It is never used to widen the threshold: see
        #: `straightedge.watchdog`.
        self._hb_last_mono: float | None = None
        self._hb_gap_max_s = 0.0
        #: THE STDERR WARNING has been printed. Set before the print on
        #: purpose: a print is not durable, so losing one loses nothing that
        #: outlives the process, and a repeated warning every 15 seconds would
        #: be noise rather than information.
        self._hb_over_warned = False
        #: THE JOURNAL ROW has landed. A SEPARATE flag from the one above, and
        #: the whole of straightedge#217: the two surfaces have different
        #: durability, so one flag cannot govern both. This one is set ONLY
        #: after `journal.write` returns, because a flag set before a durable
        #: write loses the record precisely when writing is what failed.
        self._hb_breach_recorded = False
        #: What the breach MEASURED, captured at detection rather than re-read
        #: at write time. A retried write must record the breach that was
        #: detected, not a larger maximum that accumulated while the journal
        #: was unavailable; those would be the same row reporting a different
        #: fact.
        self._hb_breach_pending: dict[str, object] | None = None
        #: Bounded, because the one-line fix is wrong. Moving the flag after
        #: the write converts "lose the record once" into "attempt it on every
        #: tick for as long as the journal stays broken", which is an unbounded
        #: retry inside the latency path this record exists to explain, and
        #: `_write_heartbeat` already refuses a venue round trip for exactly
        #: that reason (straightedge#153, #190).
        self._hb_breach_attempts = 0
        #: Published in the heartbeat, because the channel designed to carry
        #: this is the one that failed. A different FILE with a live reader.
        self._hb_breach_rows_lost = 0
        #: The worst gap THIS BOX has ever published, carried across restarts.
        #: A DIFFERENT question from `_hb_gap_max_s` one line up, which is why
        #: it is a second field rather than a change to the first;
        #: `_restore_gap_ever` states which question each one answers, where a
        #: reader meets them.
        self._hb_gap_ever_s = 0.0
        self._hb_ever_restored = False
        #: This process's identity, published in the heartbeat so that a
        #: supervised restart is observable from OUTSIDE the process. Assigned
        #: in __init__ and never reassigned: a desk that re-read its own config
        #: and kept running is the same desk, and only a new process is a
        #: restart.
        #:
        #: Why it is needed at all. Before this field, the only trace a restart
        #: left in the heartbeat was the arming state falling back to
        #: `live_not_accepted`, and that is visible ONLY on a real-money desk.
        #: On a demo account nothing needs arming, so every field read
        #: identically either side of a crash and `watch` had nothing to
        #: compare: see straightedge#133 requirement 3.
        self._hb_run_id = watchdog.new_run_id()
        self._hb_started_at = self.now_fn()
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
        # BEFORE the roll. `observe()` advances `snapshot.day_key` to today
        # whenever it differs, and the ended day's identity and baseline live
        # nowhere else: once it has rolled, the only record that a day ended
        # while this desk was down is gone (straightedge#129).
        owed = self._owed_recap_day(self.now_fn())
        # Read before `observe()`, and NOT because correctness depends on it.
        # `_owed_recap_day` decides from the journal, which never rolls, so
        # there is no window here to preserve and no ordering for a future
        # edit to break: a boot that dies anywhere in `start()` leaves the next
        # one able to reach the same conclusion. What the pre-roll position
        # buys is the BASELINE: `day_start_equity` for the ended day exists
        # only until the roll overwrites it, so reading here attaches a real
        # number where one survives and the row names it unmeasured where it
        # does not. Review of #198 ruled this direction over ordering for
        # exactly that reason.
        self._announce_unrecapped_day(owed)
        self.risk.observe(acct, self.now_fn())
        self._emit(
            "start",
            # The engine's OWN day, which is what makes this row durable
            # evidence of a session for `_owed_recap_day`. `ts` is the wall
            # clock and the gates are not, so the two must not be conflated
            # (#182); `journal.last_session_day_before` reads THIS field.
            day=day_key(self.now_fn()),
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
            # Same argument as the two above, for the same kind of fact
            # (straightedge#139): which secrets this session took from
            # config.toml rather than the environment is invisible from the
            # config alone unless someone opens a 0600 file, so it goes in the
            # one record every session already writes. Key NAMES only; a value
            # must never reach the journal, and `login` right above is
            # redacted precisely because that record is rendered into the chat.
            settings_from_file=list(self.cfg.settings_from_file),
            settings_read_from_nowhere=list(self.cfg.settings_read_from_nowhere),
        )
        # Ask the venue for every configured symbol's series before anything
        # relies on it. On MT4 the ask IS the fix: a series exists per symbol AND
        # timeframe and the terminal builds one only when something requests it,
        # so a cold symbol becomes warm here without the operator opening
        # anything. What cannot be fixed is NAMED, here, at startup. It used to
        # surface as step_symbol() returning on an empty bar list with no record
        # (engine.py:466-468) -- a symbol that never traded and never said why.
        self.history = self.warm_history()
        # One venue-clock reading at the session boundary (straightedge#186),
        # AFTER `warm_history`, because that is what selects the symbols: a
        # tick for a symbol still absent from Market Watch is exactly the
        # reading that fails, and the #172 history work exists because cold
        # symbols are the normal startup state rather than the exception.
        #
        # `max_staleness_sec=None`: `start()` has no previous poll, so it cannot
        # bound the sample, and a venue that SAMPLES answers with an implication
        # that refuses conversion. That is the honest opening record and it is
        # the same thing `doctor --connect` prints.
        #
        # NEVER FATAL. It is an observation, so it must not add a way for the
        # desk to fail to start: the MT4 adapter raises when the Expert answers
        # an error, and before this line nothing in `start()` asked the venue
        # for a tick. A failed reading is recorded as unmeasured with the error
        # as its detail, which is the same partition every other venue read in
        # this file uses.
        self._record_startup_venue_clock()
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
        # `day` for the same reason as the `start` row: session evidence on
        # the engine's clock. A session that ran for days is better
        # evidenced by where it ENDED than by where it began.
        self._emit("stop", day=day_key(self.now_fn()))
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
        # (`replace_pending` measures a working-order replacement against this
        # same pair as of #104; all three paths now agree.)
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
        polled_at = time.monotonic()
        since_last_poll = polled_at - self._last_poll_mono.get(symbol, polled_at)
        self._last_poll_mono[symbol] = polled_at
        if prev is None:
            # First poll: pin the last bar so we do not dump-trade history.
            self.last_bar_time[symbol] = last_t
            return
        if last_t <= prev:
            return
        self.last_bar_time[symbol] = last_t
        # The gate above is what makes the bound below true, and the two have
        # to stay together: a bar advanced since the previous poll, so a tick
        # arrived inside `since_last_poll`, so the venue's stamp is no older
        # than that. Remove the advance gate and this bound becomes a guess.
        self._act(symbol, bars, max_staleness_sec=since_last_poll)

    def replay_symbol(self, symbol: str, bars: list[Bar]) -> None:
        """Act on bars handed in directly. The backtest and test seam.

        `max_staleness_sec=None`, deliberately: there is no previous poll here
        and no advance gate, so nothing on this path can bound how old a
        sampled venue stamp is. A venue that SAMPLES a server therefore cannot
        be measured through this entry point and the leg refuses by name,
        which is the honest outcome; a venue that DECLARES its offset (paper,
        and so every backtest) is unaffected. The straightedge#182 review
        named this caller as the one that would otherwise inherit the engine's
        bound without owning the gate that justifies it.
        """
        if self.halted:
            return
        self._act(symbol, bars, max_staleness_sec=None)

    def _record_startup_venue_clock(self) -> None:
        """Read and record the venue clock once at start. Never raises."""
        symbol = self.cfg.symbols[0] if self.cfg.symbols else "EURUSD"
        try:
            clock = venue_clock_of(self.broker, symbol, max_staleness_sec=None)
        except (RuntimeError, OSError, ValueError) as exc:
            clock = VenueClock.not_measured(
                "server_time",
                source=type(self.broker).__name__,
                detail=(
                    "the venue could not be read at start: "
                    + redact_text(str(exc))[:_FAULT_CHARS]
                ),
            )
        self._record_venue_clock(clock, symbol)

    def _record_venue_clock(self, clock: VenueClock, symbol: str) -> None:
        """Journal the venue clock when it CHANGES, and once at start.

        straightedge#186. #172 journals the FAILURE and nothing on success, so
        the record can say the desk did not know what time it was and can never
        say it thought it was UTC+3. The offset is measured, used to convert
        every bar, and then discarded; the only way to learn it was
        `doctor --connect` at the moment you asked, which answers for NOW and
        says nothing about the instant an order was placed. #37's evidence
        package has to answer that from the journal alone.

        WHAT COUNTS AS A CHANGE, and the two it must catch are the two that
        happen without anybody editing anything: a server-side DST roll
        (+10800 to +7200) and a reconnect that lands on a different server
        (+10800 to 0). The comparable state is the offset, the unmeasured field
        names, and the source. `detail` and `measured_at` are deliberately NOT
        in it: `detail` carries a raw figure that moves every sample and
        `measured_at` moves by construction, so including either would make
        every poll a change and the record would be noise instead of a
        boundary. Both are still WRITTEN on the row.

        JOURNAL ONLY, never a chat ping: `_format_event` returns empty for it.
        An offset that has not moved is not news, and the one an operator needs
        at 03:00 is in the file next to the orders it converted.

        It cannot alter a gate. It is called after the decision that uses the
        clock, takes no branch on its own result, and writes one row.
        """
        state = (
            clock.offset_sec,
            tuple(sorted(clock.unmeasured)),
            clock.source,
        )
        if state == self._clock_recorded:
            return
        previous = self._clock_recorded
        self._clock_recorded = state
        fields: dict[str, Any] = {
            "symbol": symbol,
            "venue": clock.source,
            "sampled": bool(clock.sampled),
        }
        if clock.offset_sec is not None:
            fields["offset_sec"] = int(clock.offset_sec)
        if clock.unmeasured:
            fields["unmeasured"] = sorted(clock.unmeasured)
        if clock.detail:
            fields["detail"] = clock.detail
        if clock.implied_offset_sec is not None:
            fields["implied_offset_sec"] = int(clock.implied_offset_sec)
        if clock.measured_at:
            fields["measured_at"] = int(clock.measured_at)
        if previous is not _CLOCK_UNRECORDED:
            # What it moved FROM, so one row states the transition and a reader
            # does not have to diff two of them. A DST roll is only legible as
            # a pair of numbers.
            #
            # OMITTED rather than null when there was no previous offset
            # (#225), so the whole row obeys one rule: a key present means a
            # measured value. `offset_sec` was already omitted that way, and a
            # field that can be absent OR null has two absent states that
            # nothing distinguishes, which is #206's version-skew shape. A
            # parser reaches for `in` on one convention and `is not None` on
            # the other, and the second misreads a row written without the
            # key. `previous_unmeasured` below already says what the previous
            # state was, so nothing is lost by leaving the number out.
            if previous[0] is not None:
                fields["previous_offset_sec"] = int(previous[0])
            if previous[1]:
                fields["previous_unmeasured"] = list(previous[1])
        else:
            fields["first_reading"] = True
        self._emit("venue_clock", **fields)

    def _bar_instant(
        self, symbol: str, bars: list[Bar], max_staleness_sec: float | None
    ) -> datetime | None:
        """The real UTC instant the auto leg is acting at, or None to refuse.

        THE one seam where a venue timestamp becomes a wall-clock instant.
        Everything below this line is written in UTC: `in_session`, the
        `day_key` the daily-loss budget is keyed on, `weekday()` for the
        weekend block and the Friday cutoff.

        What straightedge#172 was. This line read

            datetime.fromtimestamp(bars[-1].time, tz=timezone.utc)

        and a bar's `time` is the BROKER SERVER's wall clock (docs/VENUE.md),
        so `tz=timezone.utc` RELABELLED the instant instead of converting it.
        On the live UTC+3 desk that moved the configured 07:00-17:00 window to
        04:00-14:00, gave the auto leg a different day boundary from the desk
        and the recap (both of which use `now_fn`), and let a `daily_loss`
        halt clear up to the broker offset EARLY, because `observe()` releases
        it on the day-key change and the broker day crosses midnight first.

        The offset is MEASURED off the venue and is never configured or
        guessed; see `VenueClock`. When the venue cannot state it, this leg
        REFUSES and journals why. It does not fall back to UTC: a fallback
        here is the defect with a comment on it, and the standing rule from
        straightedge#68 is that an unmeasured spec refuses rather than
        defaults. Direction of harm is a missed auto entry.

        `bars` empty keeps the desk clock, unchanged: there is no venue
        timestamp in play, so there is nothing to convert and nothing to
        refuse.
        """
        if not bars:
            return self.now_fn()
        clock = venue_clock_of(
            self.broker, symbol, max_staleness_sec=max_staleness_sec
        )
        # Recorded BEFORE the refusal below returns, so a clock that goes
        # unmeasured is in the record as a state change and not only as a
        # stream of per-bar refusals (straightedge#186).
        self._record_venue_clock(clock, symbol)
        if not clock.measured:
            # journal.write, never _emit: a refusal is not broadcast to chat.
            # source and stage are what let one reject event name every path
            # apart, exactly as the ORDERS_UNMEASURED refusal below does.
            self.journal.write(
                "reject",
                source="auto",
                stage="signal",
                symbol=symbol,
                reason=VENUE_CLOCK_UNMEASURED,
                unmeasured=sorted(clock.unmeasured),
                venue=clock.source,
                detail=clock.detail,
            )
            return None
        # The venue's own bars, checked against the venue's own clock. See
        # VENUE_CLOCK_BAR_DISAGREES: this is what makes a stale stamp
        # unreachable rather than merely unlikely, and it is the only check
        # here that can see a staleness sitting exactly on the offset grid.
        # `clock.sampled` is the gate and `clock.measured_at` is the operand.
        # Those were one field before straightedge#193, with a zero timestamp
        # standing in for "nothing was sampled"; `VenueClock.__post_init__`
        # now guarantees a sampled clock carries a real one, so the arithmetic
        # below cannot be fed a sentinel.
        implied_server_now = clock.measured_at + (clock.offset_sec or 0)
        if clock.sampled and (
            implied_server_now + VENUE_CLOCK_BAR_SLACK_SEC < bars[-1].time
        ):
            self.journal.write(
                "reject",
                source="auto",
                stage="signal",
                symbol=symbol,
                reason=VENUE_CLOCK_BAR_DISAGREES,
                offset_sec=clock.offset_sec,
                venue=clock.source,
                bar_time=bars[-1].time,
                implied_server_now=implied_server_now,
            )
            return None
        return clock.to_utc(bars[-1].time)

    def _act(
        self, symbol: str, bars: list[Bar], *, max_staleness_sec: float | None
    ) -> None:
        now = self._bar_instant(symbol, bars, max_staleness_sec)
        if now is None:
            return
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
        orders, orders_measured = self._read_working(self.cfg.risk.magic)
        if not orders_measured:
            # The auto leg is unattended, so it is the LAST place that may read
            # an unreadable book as an empty one.
            self.journal.write(
                "reject",
                stage="auto",
                source="auto",
                reason=ORDERS_UNMEASURED,
                symbol=symbol,
            )
            return
        # The auto leg carries its OWN same-symbol check, independent of
        # `already_in_symbol`, and it was positions-only: auto could stack a
        # market entry on top of a working order it had placed itself.
        committed: list[Position | PendingOrder] = [*positions, *orders]
        if any(x.symbol == symbol for x in committed):
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
            orders=orders,
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

    def _require_quote(self, symbol: str, tick: Tick) -> None:
        """Refuse a quote that is absent OR unreadable (#211).

        One site, two callers. `market_signal` and `reverse_signal` both carried
        `if tick.bid <= 0 or tick.ask <= 0`, and `nan <= 0` is False, so the
        guard written to refuse a broken quote missed the most broken quote
        there is.

        IT WAS A FAIL-OPEN ON THE SEND PATH, not a reason defect, and the
        asymmetry is why it was easy to miss. A buy takes its entry from the
        ASK, so an unreadable BID never enters a geometry comparison and nothing
        downstream objects; a sell is the mirror. Measured on the pre-fix tree:
        `/buy` with `bid=nan` and a real ask reached `retcode=10009` with a
        full-size position on the book, and so did `bid=inf`, and so did
        `/sell` with `ask=nan`. The resulting position carried
        `price_current=nan, profit=nan`, so the corruption outlived the send.

        The spread gate could not save it either: `tick.spread` is `ask - bid`
        and therefore `nan`, and `risk.py`'s
        `tick.spread > max_spread_atr_frac * atr` is False for a nan, so the
        guard that refuses a blown-out spread is bypassed in the same move.

        UNREADABLE OUTRANKS ABSENT when both apply, because it is the more
        specific diagnosis: a zero tells you the feed is quiet, a nan tells you
        the field is corrupt, and only the second sends you to the wire.
        """
        for value in (tick.bid, tick.ask):
            if unusable_price(value) == "unreadable":
                raise RuntimeError(
                    f"unreadable tick for {symbol}: bid={tick.bid!r} ask={tick.ask!r}"
                )
        if unusable_price(tick.bid) or unusable_price(tick.ask):
            raise RuntimeError(f"no tick for {symbol}")

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
        self._require_quote(symbol, tick)
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
        orders, measured = self._read_working(self.cfg.risk.magic)
        if not measured:
            return RiskDecision(allowed=False, reason=ORDERS_UNMEASURED)
        if exclude_ticket is not None:
            positions = [p for p in positions if p.ticket != exclude_ticket]
            orders = [o for o in orders if o.ticket != exclude_ticket]
        return self.risk.evaluate(
            account=self.broker.account(),
            signal=signal,
            spec=self.broker.symbol(signal.symbol),
            tick=self.broker.tick(signal.symbol),
            positions=positions,
            orders=orders,
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
        self._require_quote(pos.symbol, tick)
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
        if volume is not None:
            # A NON-FINITE VOLUME IS NOT A MAGNITUDE PROBLEM (#210), and this
            # is the guard that has to catch it, because NOTHING downstream
            # does. That is what separates this from #187 and #208, where the
            # outcome was already safe and only the reason was wrong.
            #
            # Measured on the pre-fix tree, via the operator route: `nan <= 0`
            # is False, so `nan` passed the magnitude guard that used to be the
            # whole of this check. `paper.py::_close` then let it through both
            # of ITS comparisons for the same reason, since `nan > vol + 1e-12`
            # and `abs(nan - vol) < 1e-12` are both False, so it fell to the
            # partial-close branch and ran
            # `self._balance += pnl * (nan / pos.volume)` followed by
            # `pos.volume = round(pos.volume - nan, 8)`. The desk answered
            # `closed`; the position was still on the book with a `nan` volume
            # and the account balance was `nan`.
            #
            # The severity is the blinding, not the failed close. Every circuit
            # gate reads equity, and a `nan` equity compares False against the
            # daily-loss and drawdown bounds forever, so this one bad input
            # disables the guards that exist to catch the next one. An operator
            # told they are flat, who is not, then trades against a book they
            # believe is empty with the risk engine silently inert.
            bad = unusable_volume(volume)
            if bad is not None:
                raise ValueError(f"refused: {bad}")
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
        # SAME FAMILY, SECOND ENTRY POINT, AND THIS ONE ESCAPED THE DESK
        # (#210). `normalize_volume` computes
        # `math.floor(raw / spec.volume_step + 1e-12)`, and `math.floor` refuses
        # a non-finite argument with TWO different exceptions: ValueError for
        # `nan`, but **OverflowError for `inf`**. OverflowError derives from
        # ArithmeticError, not from ValueError, so it is not in the
        # `(ValueError, RuntimeError, OSError)` tuple that `handle_command` and
        # `poll_telegram` catch.
        #
        # Measured on the operator route pre-fix: `/tp TICKET PX inf` left the
        # command handler by an UNCAUGHT exception, and `/tp TICKET PX nan`
        # replied with the interpreter's own words, `cannot convert float NaN to
        # integer`. One is an availability defect and the other leaks internals
        # where a refusal belongs.
        #
        # So refuse before the arithmetic rather than widening an except clause
        # after it: the operator gets the same named refusal `/close` gives, and
        # the desk is not asked to survive an exception class it never
        # classified. Placed here, immediately before the arithmetic, rather
        # than at the top of the function, so the position lookup and the
        # circuit check above keep their precedence.
        #
        # WHAT THIS CHANGES AND WHAT IT DOES NOT (#222, correcting the comment
        # that shipped with #210). The version that shipped claimed no
        # reachable refusal changed, and that overclaims. The true and narrower
        # statement: no refusal changes its OUTCOME, since nothing is sent in
        # either version for any value, but TWO reachable values change their
        # reason WORD, from a magnitude message to the named one:
        #
        #   /tp T PX 0      volume below min lot  ->  refused: volume_unusable:0.0
        #   /tp T PX -0.5   volume below min lot  ->  refused: volume_unusable:-0.5
        #
        # The controls are part of the claim, because the next reader's first
        # question is whether it is confined to those two: `0.001` still replies
        # `volume below min lot`, `5` still `volume exceeds position`, `0.05`
        # still succeeds, and `docs/CONTRACT.md` documents the new word on the
        # `/tp` row. The non-finite cases are the only ones with no prior reply
        # worth keeping, `nan` having leaked the interpreter's own words and
        # `inf` having escaped the handler entirely.
        bad = unusable_volume(volume)
        if bad is not None:
            return f"refused: {bad}"
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
        # A NON-FINITE STOP IS NOT A GEOMETRY PROBLEM (#208), and this has to run
        # BEFORE the comparisons below or the operator is told the wrong thing.
        #
        # Measured on the pre-fix tree: with `sl = nan`, `nan < px` is False, so
        # the buy arm returned "buy needs sl < entry < tp" and the arithmetic was
        # never reached. The outcome was already safe, nothing was sent and the
        # order did not move, so the defect was entirely in the REASON: a corrupt
        # venue field reported as a stop/entry/target ordering mistake sends the
        # operator to re-read geometry they got right instead of to the venue.
        #
        # This consults `unusable_stop`, the same authority `money_per_lot_at_stop`
        # raises from, rather than comparing again here. This is about naming,
        # not about safety: the outcome was already a refusal.
        #
        # `_stop_guard` already carries the finiteness check on the POSITION path
        # (`if not math.isfinite(sl)`), earned by a measured -$58,058 run, so this
        # brings the working-order path level with it.
        bad_stop = unusable_stop(sl)
        if bad_stop is not None:
            return f"refused: {bad_stop}"
        if order.side.value == "buy" and not (sl < px and (tp <= 0 or px < tp)):
            return "buy needs sl < entry < tp"
        if order.side.value == "sell" and not (sl > px and (tp <= 0 or tp < px)):
            return "sell needs tp < entry < sl"
        # FAIL CLOSED BEFORE THE ARITHMETIC (#161), the precondition `risk.evaluate`
        # and `_stop_guard` both carry and this, the third site with the same
        # arithmetic, did not. `money_per_lot_at_stop` needs points and
        # `ticks_between` returns 0.0 when `trade_tick_size or point` is <= 0, so on
        # a spec the broker has not streamed `worst` was 0.0 and
        # `0.0 > min(per_trade, loss_room)` was False: EVERY replacement passed the
        # size guard and was sent. Reachable, not theoretical: `mt4_live` records
        # tick_size/point unmeasured when `MarketInfo` answers zero, which is what a
        # symbol outside Market Watch does, and #68 measured exactly that on gold.
        #
        # PRECISELY WHAT THIS ORDERING BUYS, because the stronger claim does not
        # survive measurement. Moving this refusal BELOW the cap decision was
        # mutated and the suite stayed green, correctly: wherever it sits above
        # `_modify_pending`, an unmeasured spec still refuses before anything is
        # sent, so the two placements are observationally equivalent and that is
        # an equivalent mutant rather than a missing test.
        #
        # What the position above the arithmetic buys is that the carve-out's
        # comparison is never evaluated on numbers that are both 0.0, which is
        # a readability and future-proofing argument, not a live fail-open: a
        # later refactor that returned early from the carve-out branch would
        # skip a refusal placed below it. The fail-open this CLOSES is the
        # 0.0-worst one above, which existed with or without the carve-out.
        not_measured = spec.unmeasured_for_sizing()
        if not_measured:
            return "refused: spec_not_measured:" + ",".join(sorted(not_measured))
        # A MISSING STOP IS NAMED, NOT PRICED (#187). `sl` here is the resting
        # order's own stop, which `/replace` does not change, so a venue-supplied
        # `sl = 0` reaches both of these. It is the one call site that can:
        # `_stop_guard` returns early on `sl <= 0` and `risk.evaluate` refuses
        # `sl_required` before its own call and before `lots_for_risk`.
        #
        # Measured before this refusal existed, on a 0.1 lot order resting with
        # no stop: `worst_resting` was 11,506.70 against a 50.00 cap, so moving
        # the entry DOWN entered #164's reduction carve-out and the cap was never
        # consulted, while moving it UP refused with `size_exceeds_risk`, which
        # blames the size for a missing stop. Neither sent anything, because
        # `_modify_pending` refuses `sl <= 0` on the way out, so this was a defect
        # in what the desk SAYS rather than what it does. Being told the wrong
        # thing about your own book is the whole reason the vocabulary exists.
        try:
            worst = money_per_lot_at_stop(px, sl, spec) * order.volume
            # WHAT IS ALREADY RESTING, measured the same way, because the question
            # the cap should ask is whether this replacement ADDS risk (#164).
            worst_resting = money_per_lot_at_stop(order.price, order.sl, spec) * order.volume
        except MissingStop as exc:
            # TRIPWIRE, NOT A GATE, and the same idiom `risk.currency_exposure`
            # uses for `UnclassifiedSymbol`. Both calls above receive the SAME
            # `sl` that `unusable_stop` validated a few lines up, so by
            # construction this cannot fire today, and a mutation that replaces
            # `exc.reason` with a hardcoded string leaves the whole suite green.
            # That is recorded rather than papered over: an unreachable branch
            # cannot be covered, and a test pretending to cover it would be the
            # decorative kind.
            #
            # It is kept because the NAME still comes from the exception rather
            # than from this call site. A future refactor that removes or
            # reorders the early check, or a new caller that reaches the
            # arithmetic another way, then produces the correct reason instead
            # of silently reverting to a hardcoded one.
            return f"refused: {exc.reason}"
        account = self.broker.account()
        r = self.cfg.risk
        per_trade = account.equity * r.risk_pct * r.max_risk_multiple
        # The same pair `risk.evaluate` measures a NEW order against, and the
        # same pair `_stop_guard` measures a widening against. A WORKING ORDER
        # IS COMMITTED EXPOSURE: it rests at the broker and becomes a position
        # without anyone being asked again, so a replacement has to fit in what
        # the day has LEFT, not merely in the per-trade cap.
        #
        # The window this closes is not "the budget is spent": `circuit` trips
        # `daily_loss` on the same comparison `loss_room` rearranges, so room
        # hits zero exactly when the circuit trips and the `circuit_reason`
        # check above already refuses there. It is the band where room is
        # POSITIVE but TIGHTER than the per-trade cap, which no gate here read.
        #
        # `loss_room` wants a current snapshot; `circuit_reason` above ran
        # `observe`, so it has one.
        #
        # A STRICTLY RISK-REDUCING REPLACEMENT IS ALWAYS ALLOWED (#164, resolution
        # 1). `_stop_guard` already solved this shape the other way at
        # `engine.py:640` and names the asymmetry at `:654`: a change that lowers
        # worst-case loss never pays for the cap, because an operator must never be
        # prevented from de-risking a live commitment, and that is exactly the
        # moment they most need to. Before this, the two guards on one engine
        # disagreed about whether de-risking needs permission and only one of them
        # documented its position, which is the version-skew shape: whichever a
        # reader checks first, they infer the other.
        #
        # Measured on #164: a $50.00 resting order, a $40.00 proposed replacement
        # and $13.80 of remaining room was REFUSED, leaving the operator `/cancel`
        # as their only move. That removes the order outright rather than reducing
        # it, so the cap left MORE risk resting than allowing the reduction would.
        # A cap that refuses the one action which unconditionally lowers risk
        # inverts the cap's own purpose.
        #
        # This does NOT widen the cap back toward `per_trade` alone, which would
        # reopen #104's fail-open ($40 of risk against $13.80 of room) and is
        # pinned by a test that reds on exactly that revert. An INCREASE is still
        # measured against `min(per_trade, loss_room)`; only the direction changes
        # the answer, never the threshold.
        if worst > worst_resting + 1e-6 and worst > min(
            per_trade, self.risk.loss_room(account)
        ) + 1e-6:
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
            return OrderResult.invalid_stops("sl_required")
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
        held = len(self.broker.positions(magic=r.magic))
        working, measured = self._read_working(r.magic)
        # COMMITTED, not "positions". This line read `positions=0/3` with three
        # working orders resting on the book, so the display an operator checks
        # before deciding the gate is fine corroborated the wrong number. An
        # unreadable book shows `?` rather than a total it cannot stand behind.
        total = f"{held + len(working)}" if measured else f"{held}+?"
        breakdown = f" ({held} open, {len(working)} working)" if measured else ""
        return (
            f"risk_pct={r.risk_pct:.2%}  "
            f"committed={total}/{r.max_positions}{breakdown}\n"
            # ROOM, not just used/cap. `llm.SYSTEM` tells the model it has
            # "daily_loss room" and "drawdown room"; this line gave it two
            # numbers to subtract instead, which is the same arithmetic-by-eye
            # that `exposure_text` exists to stop. Reporting only: no gate
            # reads this text.
            f"daily_loss={daily_loss:.2f}/{daily_cap:.2f} "
            f"room={max(daily_cap - daily_loss, 0.0):.2f}  "
            f"drawdown={dd:.2f}/{dd_cap:.2f} "
            f"room={max(dd_cap - dd, 0.0):.2f}\n"
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
            if len(extra) > HISTORY_ROW_CHARS:
                dropped = len(extra) - HISTORY_ROW_CHARS
                extra = (
                    extra[:HISTORY_ROW_CHARS]
                    + f" [truncated: {dropped} more chars in the journal]"
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

    def exposure_text(self) -> str:
        """Net currency exposure per code, the configured cap, and the room left.

        READ-ONLY. It calls the SAME function the gate calls
        (`risk.currency_exposure`) over the SAME committed set
        `RiskManager.evaluate` builds: our open positions plus our working
        orders, FX-classified only. Nothing here decides anything.

        It is deliberately not a second calculation. `llm.SYSTEM` asks the
        model to cover allocation, correlation and unused risk room, and the
        snapshot used to hand it raw positions only, so it had to net the book
        by eye. A separate aggregation written for the report would answer that
        and could then disagree with the gate that refuses the trade, which is
        the version-skew defect recorded in #142.

        `room` is how many further commitments that currency can absorb in the
        same direction before `evaluate` refuses with `currency_exposure`. The
        gate refuses at `abs(net) > cap`, so the room is `cap - abs(net)`, and
        `tests/test_advice_exposure.py` asserts that against the desk's actual
        refusal rather than against the string.
        """
        r = self.cfg.risk
        cap = r.max_currency_exposure
        held = self.broker.positions(magic=r.magic)
        working, measured = self._read_working(r.magic)
        committed: list[Position | PendingOrder] = [*held, *working]
        fx = [x for x in committed if classify_symbol(x.symbol) == SYMBOL_FX]
        excluded = sorted(
            {x.symbol for x in committed if classify_symbol(x.symbol) != SYMBOL_FX}
        )
        head = f"currency_exposure cap={cap}"
        if not measured:
            # A resting order is committed exposure, so a total computed
            # without the working orders UNDERSTATES the book. Understating
            # exposure to a model asked about unused room is the dangerous
            # direction to be wrong in, so it is declared rather than printed
            # as a confident number (same obligation as `risk_text`'s `held+?`).
            head += " INCOMPLETE: working orders unreadable"
        try:
            exposure = currency_exposure(fx)
        except UnclassifiedSymbol as exc:
            # TRIPWIRE, same as the gate's: `fx` is pre-filtered by
            # `classify_symbol`, so no broker symbol reaches this. Reported
            # loudly instead of raising, because a reporting path must not take
            # /ask down, and instead of being swallowed, because a silently
            # dropped leg is the issue #10 defect.
            return f"{head}\ncurrency_exposure=unmeasured symbol={exc}"
        lines = [head]
        for code in sorted(exposure):
            net = exposure[code]
            lines.append(f"{code} net={net:+d} room={max(cap - abs(net), 0)}")
        if len(lines) == 1:
            lines.append("no currency commitments")
        if excluded:
            # Not applicable is never silence (issue #10). These symbols
            # contribute nothing to the aggregate, which is correct rather than
            # an underestimate, but the model has to be told the number covers
            # less of the book than the position list does.
            lines.append("excluded_from_currency_limit=" + ",".join(excluded))
        return "\n".join(lines)

    def advice_context(self) -> str:
        lines = [
            self.status_text(),
            self.risk_text(),
            self.positions_text(),
            self.orders_text(),
            self.exposure_text(),
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
        # WHAT IS RUNNING, first line after the state. The live box sat 25
        # commits behind main for twelve days and no reading in this chat
        # distinguished it from a current one; `__version__` was printed by
        # `doctor` and by nothing an operator sees day to day. `unstamped` is a
        # real answer here, not a blank: see `straightedge.deployed`.
        return (
            f"straightedge {halt}\n"
            f"deployed={deployed.describe(self.journal.path)} pkg={__version__}\n"
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

    def _owed_recap_day(self, now: datetime) -> dict[str, Any] | None:
        """The day that ended with nobody announcing it, or None. DURABLE.

        DERIVED FROM THE JOURNAL, not from the equity snapshot, and that is
        what removes the window rather than relocating it. The first version of
        this read `snapshot.day_key` before `observe()` rolled it, which is
        correct only while nothing can die in between; `observe()` persists the
        roll the instant the durable tuple moves, so a process that died after
        the roll and before the announcement lost the day for good, and no
        later boot could know it was owed. On a desk that supervision restarts
        on a repeating trigger (#151) that is the EXPECTED failure, not a rare
        one. Measured in review of #198; ruled there that the fix is to make
        the owed day recoverable rather than to order two statements carefully.

        Two durable facts answer it, both from the journal, which never rolls:

        * `last_session_day_before(today)`: the most recent day a `start` or
          `stop` row names, which is evidence the desk was alive on a day that
          has since ended. Today's rows cannot erase it, because they are
          stamped today, so a boot that dies mid-`start()` leaves the next boot
          able to reach the same conclusion.
        * the last `recap` row's `day`: what has already been announced, by
          this process or any previous one.

        A day is owed when the first exists, is strictly before today, and the
        second does not already cover it. Nothing here consults the snapshot to
        DECIDE, so the decision survives any ordering, any crash point, and an
        unreadable state file.

        THE BASELINE IS ENRICHMENT, NOT EVIDENCE. `day_start_equity` for the
        ended day exists only in the pre-roll snapshot, so it is attached when
        the snapshot still names that day and is reported as unmeasured when it
        does not. That is the one thing the journal cannot supply, and the
        honest answer to it is the same as for the closing equity: name the
        field, never invent a number.
        """
        today = day_key(now)
        ended = self.journal.last_session_day_before(today)
        if not ended:
            # No durable evidence of a day that has ended. A first-ever start,
            # or a journal whose only rows are from today.
            return None
        previous = self.journal.last_event("recap")
        announced = str((previous or {}).get("day") or "")
        if announced and announced >= ended:
            return None
        snap = self.risk.snapshot
        owed: dict[str, Any] = {
            "day": ended,
            "days_skipped": (
                datetime.strptime(today, "%Y-%m-%d").replace(tzinfo=timezone.utc)
                - datetime.strptime(ended, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            ).days,
        }
        if snap.day_key == ended:
            owed["day_start"] = snap.day_start_equity
            owed["last_observed_equity"] = snap.equity
            owed["last_observed_at"] = snap.time
        return owed

    def _announce_unrecapped_day(self, owed: dict[str, Any] | None) -> None:
        """Say that a day ended unobserved. ONE row, no P&L, named unmeasured.

        It is a `recap` event rather than a new event name, and that is a
        delivery decision rather than a taxonomy one: every `config.toml`
        written before this change enumerates `notify_events` explicitly (the
        same argument `telegram.ALWAYS_NOTIFY_EVENTS` carries), so a new name
        would have reached nobody on the box this is for. The operator who
        needs this row is the one whose desk just restarted.

        IT CARRIES NO `pnl` AND NO `equity`. The ended day's closing equity was
        never observed: the persisted snapshot holds `equity` only as of its
        last durable write, which on a losing day is the last peak and is
        therefore ABOVE the real close, so a P&L computed from it would be
        wrong in the flattering direction. The issue is explicit that a recap
        with the wrong baseline is worse than none, so the two fields are
        ABSENT and `unmeasured` names them, in the shape `SymbolSpec` and
        `VenueClock` already use. A missing field cannot be misread; a zero can.

        `day_start` joins them when the snapshot has already rolled past the
        ended day, which happens exactly when a previous boot died between the
        roll and this row. The row then names three unmeasured fields instead
        of one, which is the honest reading of a day whose baseline no longer
        exists anywhere.

        ONE ROW FOR ANY OUTAGE. A box down for a week emits one row naming
        seven skipped boundaries, not seven recaps. The bound is the design,
        not a cap applied afterwards.

        THE MARKER IS THE JOURNAL, and with the derivation above it is the SAME
        fact rather than a second one: `_owed_recap_day` returns None once a
        `recap` row covers the day, so the row this writes is what stops the
        next boot repeating it. A crash loop therefore sends one message
        whatever state the snapshot is in, including a snapshot that cannot be
        read or cannot be written, where the roll never lands at all. Removing
        the marker makes every boot owe the same day again, which is
        straightedge#119's defect arriving by the other door.

        AT MOST ONCE, AND IT ERRS TOWARD SILENCE RATHER THAN REPETITION.
        `_emit` journals FIRST and notifies SECOND, and the journal row is also
        what suppresses a retry, so a send that dies between the two loses the
        ANNOUNCEMENT for good: the row survives for `/recap` and a journal
        read, and no later boot tells the operator, including a boot a
        supervisor brings up automatically. That asymmetry is deliberate.
        Marking the day done only after a successful notify would retry, and a
        desk whose Telegram stays broken would then send one message per boot,
        which is straightedge#119 arriving by the other door and the thing this
        marker exists to prevent. One lost announcement on a transport that is
        already failing is the cheaper error than an unbounded flood on one
        that keeps failing, so the design accepts the miss. Measured in review
        of #198.
        """
        if owed is None:
            return
        day = str(owed.get("day") or "")
        if not day:
            return
        unmeasured = ["equity", "pnl"]
        fields: dict[str, Any] = {
            "day": day,
            "days_skipped": int(owed.get("days_skipped") or 0),
            "reason": "desk_down_across_the_day_boundary",
        }
        if owed.get("day_start") is None:
            # The baseline died with the snapshot roll. Named, never invented.
            unmeasured.append("day_start")
        else:
            fields["day_start"] = round(float(owed["day_start"]), 2)
            if owed.get("last_observed_equity") is not None:
                fields["last_observed_equity"] = round(
                    float(owed["last_observed_equity"]), 2
                )
            if owed.get("last_observed_at"):
                fields["last_observed_at"] = int(owed["last_observed_at"])
        fields["unmeasured"] = sorted(unmeasured)
        self._emit("recap", **fields)
        # AFTER the emit, same rule as `_maybe_daily_recap`: a raising `_emit`
        # must not mark the day done.
        self._recapped_day = day

    def _maybe_daily_recap(self, acct, now) -> None:
        key = day_key(now)
        snap = self.risk.snapshot
        if not snap.day_key or snap.day_key == key:
            return
        if self._recapped_day == snap.day_key:
            return
        pnl = acct.equity - snap.day_start_equity
        # No `tail` here, and that is the whole of straightedge#119. A journal
        # row records FACTS about its own event; it must never store a
        # RENDERING of other rows. `history_text` renders every field of every
        # row it reads, so a stored `tail` was re-expanded into the next recap:
        # recap N contained recap N-1 contained recap N-2, compounding daily
        # until one message was chunked into dozens of Telegram sends and the
        # journal file grew without bound.
        #
        # Fixed at the source rather than by excluding `tail` inside
        # `history_text`: that would leave the compounding in the FILE, and it
        # would be a denylist that the next such field defeats. The day's
        # activity is not lost, it is a PULL now: `/recap` renders it live from
        # the journal. The nightly PUSH is the P&L summary, which is what makes
        # it bounded by construction instead of by a cap.
        self._emit(
            "recap",
            day=snap.day_key,
            equity=round(acct.equity, 2),
            day_start=round(snap.day_start_equity, 2),
            pnl=round(pnl, 2),
        )
        # AFTER the emit: a raising `_emit` must not mark the day done.
        self._recapped_day = snap.day_key

    def _fault_fields(
        self, exc: BaseException | None, *, prefix: str
    ) -> dict[str, Any]:
        """One fault, as bounded scalars, under `prefix`.

        Read with `getattr` rather than by importing `BridgeTimeout`: the engine
        does not depend on a venue module, and any future transport that carries
        the same four attributes is recorded without this file changing. An
        attribute that is empty or zero is OMITTED rather than written blank, so
        "the bridge did not report a phase" and "the phase was empty" cannot
        render alike.

        Bounded on purpose. #119 is open because a journal row stored a rendering
        of other rows and compounded daily; a row that records a fault must not
        become the next instance of that shape. What goes in is one clipped
        message, one type name, four short enums and one integer. Nothing here
        reads the journal.
        """
        if exc is None:
            return {}
        out: dict[str, Any] = {
            prefix: str(exc)[:_FAULT_CHARS],
            f"{prefix}_type": type(exc).__name__,
        }
        for attr in ("op", "transport", "phase", "withdrawal"):
            value = getattr(exc, attr, "")
            if isinstance(value, str) and value:
                out[f"{prefix}_{attr}"] = value[:_FAULT_ENUM_CHARS]
        req_id = getattr(exc, "req_id", 0)
        if isinstance(req_id, int) and req_id > 0:
            out[f"{prefix}_req_id"] = req_id
        return out

    def _reconnect_broker(self, cause: BaseException | None = None) -> bool:
        """Drop the venue link and build it again. Returns whether that worked.

        `cause` is the exception that TRIGGERED the reconnect, and recording it
        is the whole point of #127. Until it was passed in, `step_all` caught the
        trigger WITHOUT BINDING IT, so a recovered blip left `reconnect ok=True`
        and no record of what had broken. Conrad's live journal from the Vultr
        box is mostly lone `ok=True` lines for that reason, and the question he
        actually asked -- is this bridge flaky and expected on that box, or is it
        degrading -- cannot be put to records that threw the reason away.

        **The cause goes on the `reconnect` ROW, not onto an event of its own.**
        Four reasons, because the choice is not obvious:

        * the cause and the outcome are ONE fact. Two rows can be separated by a
          process exit, by the 10 MiB rotation, or by another write from the
          Telegram path, and a reader then holds a cause with no outcome or an
          outcome with no cause, which is the state this fix exists to leave.
        * `Journal.tail(n)` reads the last n ROWS. A second row per reconnect
          halves the history an operator sees for the same `n`, and doubles the
          volume of precisely the condition under investigation.
        * `reconnect` is already in the documented event list (`docs/RUNBOOK.md`,
          `docs/CONTRACT.md`) and in the watchdog's own advice. Added FIELDS need
          no consumer change; a new event NAME would be invisible until every one
          of those was updated, which is this defect again one level up.
        * `error` already means "this reconnect attempt itself failed", so the
          trigger needs its own prefix. Overloading `error` would make an
          `ok=True` row that carried one ambiguous.
        """
        fields = self._fault_fields(cause, prefix="cause")
        try:
            self.broker.disconnect()
        except (RuntimeError, OSError, ValueError):
            pass
        unselected: list[str] = []
        try:
            self.broker.connect()
            for name in self.cfg.symbols:
                try:
                    self.broker.select_symbol(name)
                except (RuntimeError, OSError, ValueError):
                    unselected.append(name)
                    continue
            if unselected:
                # A link that came back WITHOUT its symbols is not the same fact
                # as a clean reconnect, and the two rendered identically: the
                # `continue` discarded the error and `ok=True` was written
                # anyway. The COUNT goes on the row, which keeps it one bounded
                # integer whatever `cfg.symbols` holds; the NAMES go to stdout,
                # where the desk's other operator warnings already are.
                fields["unselected"] = len(unselected)
                print(
                    "reconnect: the venue link is back, but "
                    f"{len(unselected)} of {len(self.cfg.symbols)} symbols "
                    f"could not be reselected ({', '.join(unselected)}). "
                    "Those symbols cannot trade until they can be.",
                    flush=True,
                )
            self.journal.write("reconnect", ok=True, **fields)
            return True
        except (RuntimeError, OSError, ValueError) as exc:
            self.journal.write(
                "reconnect", ok=False, error=str(exc)[:_FAULT_CHARS], **fields
            )
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
        # BEFORE anything is written. This method REPLACES `dest`, so the only
        # moment the previous process's figure is still readable is here.
        self._restore_gap_ever(dest)
        ts = now or self.now_fn()
        mono = time.monotonic()
        if self._hb_last_mono is not None:
            self._hb_gap_max_s = max(self._hb_gap_max_s, mono - self._hb_last_mono)
        self._hb_last_mono = mono
        budget = watchdog.tick_budget_seconds(self.cfg)
        # Monotonic, by construction: this desk's own observations are part of
        # the box's history, so the published figure can never be lower than
        # what this process has itself seen.
        self._hb_gap_ever_s = max(self._hb_gap_ever_s, self._hb_gap_max_s)
        if self._hb_gap_max_s > budget:
            # The threshold is derived from config and this measurement says the
            # derivation is too tight for this book. Report it; do NOT widen it.
            #
            # TWO SURFACES, TWO FLAGS, AND THAT IS THE WHOLE OF straightedge#217.
            # The stderr warning is not durable, so setting its flag before the
            # print costs nothing if the print is lost. The JOURNAL ROW is
            # durable, and the flag that governs it was set before the write, so
            # a failed write lost the record permanently: `Journal.write` has no
            # exception handling, `__main__` catches `Exception` and keeps
            # looping, and the loop's own `loop_error` write is swallowed, so the
            # only trace was a stderr line on a box where stderr goes to a file
            # nobody reads. One flag could not govern both surfaces, because
            # losing a print and losing an append-only audit row are not the
            # same event.
            if self._hb_breach_pending is None:
                # CAPTURED AT DETECTION. A retried write records the breach that
                # was detected, not a larger maximum that accumulated while the
                # journal was unavailable.
                self._hb_breach_pending = {
                    "gap_s": round(self._hb_gap_max_s, 1),
                    "budget_s": budget,
                    "symbols": len(self.cfg.symbols),
                    "run_id": self._hb_run_id,
                }
            if not self._hb_over_warned:
                self._hb_over_warned = True
            # The JOURNAL, not only stdout. stdout on the deployed box goes to a
            # file nobody reads, so before straightedge#153 the only durable
            # trace of a breach was a heartbeat field the next restart
            # overwrote. The journal is append-only and already the audit log of
            # a real-money desk, and it is the only surface that can answer HOW
            # OFTEN this box breaches rather than how bad the worst one was.
            # Once per process per breach, exactly as the stdout warning was.
            #
            # `symbols` and NOT the position count, which is what the issue
            # floated: a position count means a venue round trip, and a venue
            # round trip inside the heartbeat writer would add latency to the
            # very path whose latency this record exists to explain. An
            # instrument must not perturb its own measurement. The symbol list
            # is the half of the book that is config, and therefore free.
                print(
                    f"heartbeat: a gap of {self._hb_gap_max_s:.1f}s between "
                    f"ticks exceeds the derived budget of {budget}s, so the "
                    "watchdog threshold can produce a false STALE. It was not "
                    "widened.",
                    flush=True,
                )
            if (
                not self._hb_breach_recorded
                and self._hb_breach_attempts < _HB_BREACH_WRITE_ATTEMPTS
            ):
                self._hb_breach_attempts += 1
                try:
                    self.journal.write(
                        "tick_gap_breach", **self._hb_breach_pending
                    )
                except OSError:
                    # NARROW, and `OSError` rather than `Exception`, because the
                    # failure this survives is the file being unavailable: a
                    # rotation refused by a reader holding the journal open, a
                    # vanished directory, a full disk. A serialisation defect is
                    # not a transient and must not be retried into silence;
                    # #250 made a non-finite value a marked string rather than
                    # a raise, so the remaining raises here are the filesystem.
                    if self._hb_breach_attempts >= _HB_BREACH_WRITE_ATTEMPTS:
                        # GIVING UP IS RECORDED, because the alternative is the
                        # defect this issue is about one level out: a bounded
                        # retry that goes quiet is a record lost with nothing
                        # saying so. Published in the heartbeat, a DIFFERENT
                        # file with a live reader, since the channel designed to
                        # carry this is the one that failed.
                        self._hb_breach_rows_lost += 1
                        print(
                            "heartbeat: the tick_gap_breach row could not be "
                            f"written after {_HB_BREACH_WRITE_ATTEMPTS} "
                            "attempts and is LOST. The breach happened; the "
                            "journal did not record it. See "
                            "breach_rows_lost in the heartbeat.",
                            flush=True,
                        )
                else:
                    self._hb_breach_recorded = True
        tmp.write_text(
            watchdog.render(
                ts,
                blocked=blocked,
                mode=self.cfg.mode,
                stale_after_s=watchdog.stale_after_seconds(self.cfg),
                tick_budget_s=budget,
                tick_gap_max_s=self._hb_gap_max_s,
                tick_gap_ever_s=self._hb_gap_ever_s,
                run_id=self._hb_run_id,
                started_at=self._hb_started_at.isoformat(),
                # Read on every write rather than cached at construction. A
                # deploy rewrites this file while the desk is DOWN, so a cached
                # value could only ever be right; but an operator who repairs a
                # stamp by hand on a running desk should see the repair, and a
                # file read next to a file write costs nothing.
                deployed=deployed.describe(self.journal.path),
                # Published on EVERY heartbeat, not only when non-zero, so the
                # field is one a reader can rely on being there
                # (straightedge#217).
                breach_rows_lost=self._hb_breach_rows_lost,
                # Read off the journal rather than mirrored onto the engine,
                # because the journal is the only thing that knows a rotation
                # was refused and a second copy of the figure could disagree
                # with it (straightedge#251).
                rotate_deferrals=self.journal.rotate_deferrals,
            ),
            encoding="utf-8",
        )
        os.chmod(tmp, 0o600)
        # NOT a bare `tmp.replace(dest)`: on Windows a concurrent reader of the
        # heartbeat makes that fail, which aborted the whole tick
        # (straightedge#242). The helper says why, and how narrowly.
        replace_retrying_on_share_conflict(tmp, dest)

    def _restore_gap_ever(self, dest: Path) -> None:
        """Carry the worst gap this BOX has seen across a restart. Once.

        TWO QUESTIONS, TWO FIELDS, and that is the whole design decision of
        straightedge#153, so it is written where a reader meets the fields.

        - `tick_gap_max_s` answers **is THIS PROCESS slow now**. It is per
          process on purpose and it is UNCHANGED by this issue: a desk whose
          book has shrunk must be able to report a clean budget again, and an
          indicator that can never go green is one an operator learns to
          ignore. That is the module's refusal to WIDEN the threshold on a
          breach, read from the other end.
        - `tick_gap_ever_s` answers **has THIS BOX ever been slow**. That is the
          question `over_budget` was installed for, because what it tests is
          whether `UNBOUNDED_TAIL_ALLOWANCE` is adequate for this book, and a
          restart resets neither the allowance nor the book.

        One field answering both is what made this a defect rather than a
        quirk. The live desk published `tick_gap_max_s=608.5 over_budget=1`,
        the 2026-10-08 deploy restarted it, and the same surface then published
        `7.7` and `0`. Both figures were correct for their process; the 608.5
        became unrecoverable from any live surface, while #143 was citing it as
        evidence. The reset is also in the dangerous direction, because a desk
        restarted after a bad episode reports its cleanest possible history.

        WHY THE HEARTBEAT IS THE STORE, having rejected the other two.

        The risk snapshot is out: it is money-path state that fails closed, and
        putting a diagnostic in it widens what a corrupt snapshot can halt.

        The JOURNAL is out as the STORE, which is less obvious and matters
        more, because the journal is where the breach RECORD goes. Rotation is
        a single generation (`Journal._rotate_if_needed` does one
        `path.replace(path + ".1")`) and `tail()` reads only the live file, so a
        maximum recovered by scanning the journal is a LOWER BOUND once a
        rotation has happened, and a lower bound published as a maximum is the
        defect this issue is about. The journal answers how OFTEN; it cannot
        soundly answer how BAD.

        So the store is the heartbeat itself: not risk state, not rotated,
        write-mostly, already 0600, already atomically replaced, and already
        the file this figure is published in, so the restore source and the
        published value are the same line. Reading a sidecar in this path is
        established rather than new: `deployed.describe` is read here on every
        write.

        WHAT THIS CANNOT SEE, stated rather than implied. The figure is a
        maximum over the heartbeats that SURVIVED, not over all time. Deleting
        `journal.heartbeat` resets the box's history and that is the one way to
        lose it; a desk upgrading from a build with no such field starts the
        chain at its own observations, and `watchdog.decide` says so rather
        than reading the absence as a clean history. The figure is never lower
        than what this process has itself observed, so it is always a true
        lower bound on every reading this desk WROTE.

        IT IS NOT A LOWER BOUND ON WHAT THIS DESK READ, and an earlier version
        of this paragraph claimed it was ("never an invented one"), which was
        false. straightedge#218 measured the damaged cases. `garbage`, `nan`,
        `-5`, an empty value and anything else that will not parse or will not
        survive `max` are discarded, and the chain restarts from this process's
        own observation, which is the behaviour the claim describes. **A
        non-finite value is not**: `inf` parses, survives `max`, is published
        as `inf`, sets `over_budget_ever=1`, makes `watchdog.decide` tell the
        operator this BOX has breached its budget, and STICKS, because every
        later write folds it with `max`. Only deleting the heartbeat clears it.
        That is a breach claim no process observed, so for a DAMAGED file the
        figure can be an invented one. All three properties are pinned by
        `tests/test_the_damaged_box_figure_and_the_both_over_note.py`, and
        whether this path should REFUSE a non-finite value is a behaviour
        change on a live operator surface, so it is straightedge#328 rather
        than taken there.

        Once per process: the first heartbeat write restores, and every later
        one only grows the figure. It happens HERE rather than in `start()` so
        that no call order can publish an unrestored field, because
        `_write_heartbeat` is reached from `step_all` and not only from a
        started desk.
        """
        if self._hb_ever_restored:
            return
        self._hb_ever_restored = True
        prior = watchdog.read(dest)
        if prior is None:
            return
        raw = prior.fields.get("tick_gap_ever_s", "")
        if not raw:
            # An older desk published no such field. Its history is genuinely
            # unrecoverable, so the chain starts here. `tick_gap_max_s` is
            # deliberately NOT read as a fallback: it is the previous PROCESS's
            # figure, and adopting it would answer the box question with the
            # process question's number, which is the conflation being fixed.
            return
        try:
            self._hb_gap_ever_s = max(self._hb_gap_ever_s, float(raw))
        except ValueError:
            # A field that will not parse is not a measurement, and this is the
            # desk's own file: a value it cannot read means the file was
            # damaged, never that the box was quiet.
            return

    def step_all(self) -> None:
        self.poll_telegram()
        try:
            ensure = getattr(self.broker, "ensure_connected", None)
            if callable(ensure):
                ensure()
            acct = self.broker.account()
        except (RuntimeError, OSError, ValueError) as exc:
            if not self._reconnect_broker(exc):
                return
            try:
                acct = self.broker.account()
            except (RuntimeError, OSError, ValueError) as after:
                # The link came back and the account still cannot be read. That
                # is a THIRD state, neither a blip nor a dead bridge, and it used
                # to return having written nothing at all, so the tick vanished
                # from the record entirely while `reconnect ok=True` sat above it
                # claiming recovery.
                self.journal.write(
                    "account_read_failed", **self._fault_fields(after, prefix="error")
                )
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
        unmeasured = list(fields.get("unmeasured") or [])
        if unmeasured:
            # FIRST, before anything reads `pnl`. `float(None or 0)` is 0.0, so
            # the line below would render `pnl=+0` for a day whose result was
            # never observed, which is the zero-standing-in-for-an-unanswered
            # question this repo keeps finding (straightedge#129).
            skipped = fields.get("days_skipped")
            tail = ""
            if fields.get("last_observed_equity") is not None:
                tail = f", last observed equity={fields.get('last_observed_equity')}"
            boundaries = (
                f" ({skipped} day boundary(s) missed)" if skipped is not None else ""
            )
            return (
                f"RECAP {fields.get('day')} day_start={fields.get('day_start')} "
                f"pnl=NOT MEASURED: the desk was down across the day boundary"
                f"{boundaries}{tail}"
            )
        pnl = float(fields.get("pnl") or 0)
        sign = "+" if pnl >= 0 else ""
        # Head only. The day's rows are a PULL (`/recap`), never pushed: see
        # `_maybe_daily_recap` for why a recap carries no rendered tail.
        return (
            f"RECAP {fields.get('day')} equity={fields.get('equity')} "
            f"day_start={fields.get('day_start')} pnl={sign}{pnl}"
        )
    if event == "venue_clock":
        # Journal-only. An offset that has not moved is not news, and the one an
        # operator needs at 03:00 is in the file beside the orders it converted
        # (straightedge#186). The LOUD clock events already exist: a refusal
        # journals `venue_clock_unmeasured` and `doctor --connect` prints the
        # reading on demand.
        return ""
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
