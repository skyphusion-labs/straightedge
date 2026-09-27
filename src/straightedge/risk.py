"""Account-level risk gates. Strategy does not size or send; this module does.

A halt (daily loss, max drawdown, kill file) is sticky until the next UTC day
for daily loss, and until the operator clears the halt file after drawdown. The
engine is expected to flatten when flatten=True.

Restart does NOT clear a halt, because it does not clear the state the halt is
computed from. The `EquitySnapshot` persists beside the journal (see
`state.py`): `day_start_equity` is keyed on `day_key`, so a genuine new UTC day
resets the loss budget and the same day does not, and `peak_equity` outlives the
process so the drawdown gate does not read zero after a crash. Every gate still
recomputes its verdict from the snapshot on every call; what is persisted is the
INPUT to that recomputation, never the verdict.

Two extra halt reasons come out of that persistence, and both fail CLOSED,
because a money gate that cannot measure must not trade:

- `state_unreadable`: the snapshot exists but is corrupt, truncated, mistyped or
  from an unknown version. The file is left untouched for the operator; clearing
  it is a deliberate act that also resets the peak.
- `state_unwritable`: the snapshot could not be written, so the next restart
  would lose it.

The last-line size guard in `evaluate` takes two caps. One re-derives what
`sizing.lots_for_risk` already applied and cannot fire against the current
sizer; it is the backstop for a future change that loosens it, and a test
breaks the sizer deliberately so it is seen firing. The other is the room left
before the daily-loss and drawdown halts, computed from the persisted
`EquitySnapshot` the sizer never sees, so it can DISAGREE with the sizer
instead of recomputing it. Before issue #55 only the first cap existed, with a
tolerance LOOSER than the sizer's own, which is why nothing could reach it.
"""

from __future__ import annotations

import functools
import math
import os
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path

from straightedge.config import BotConfig, SessionConfig
from straightedge.currencies import CURRENCY_CODES
from straightedge.models import (
    Account,
    PendingOrder,
    EquitySnapshot,
    Position,
    RiskDecision,
    Side,
    Signal,
    SignalKind,
    SymbolSpec,
    Tick,
)
from straightedge.sizing import lots_for_risk, money_per_lot_at_stop
from straightedge.state import (
    StateUnreadable,
    StateUnwritable,
    load_snapshot,
    save_snapshot,
    snapshot_path_for,
)


SYMBOL_FX = "fx"
SYMBOL_NOT_FX = "not_fx"

#: Refusal reason for an effective deviation the current spread cannot fit
#: inside. Prefix-shaped: the payload carries the measurement AND the config key
#: to change, because this string reaches the operator verbatim (desk.py answers
#: `refused: <reason>`) and "slippage too small" without the number to set is
#: not an actionable refusal.
DEVIATION_BELOW_SPREAD = "deviation_below_spread"

#: Multiple of the live spread the refusal RECOMMENDS as the deviation to set,
#: which is deliberately larger than the floor the gate ENFORCES
#: (`min_deviation_spread_multiple`, 1.0 by default).
#:
#: They are two different kinds of claim and they are kept apart on purpose.
#: The floor is arithmetic: below one spread a market order cannot fill, so it
#: may refuse. The recommendation is judgement: an operator who fixes the
#: config to the exact floor is rejected again by the first tick that widens
#: the spread by one point, so the advice has to carry headroom. Judgement is
#: allowed to shape ADVICE and is not allowed to refuse anyone's trade, so this
#: number appears only inside the reason string and never in the comparison.
#:
#: 3.0 is the figure under which the one measured instrument works out: gold at
#: a 45-point spread is advised to 135, and the 150 seeded in
#: `config.example.toml` clears both that and the floor.
DEVIATION_HEADROOM_MULTIPLE = 3.0



@functools.lru_cache(maxsize=8)
def _code_lengths(codes: frozenset[str]) -> tuple[int, ...]:
    return tuple(sorted({len(c) for c in codes}))


def resolve_pair(alpha: str, codes: frozenset[str]) -> tuple[str, str] | None:
    """The one split of `alpha` into two recognised codes, or None (issue #77).

    `alpha` is upper-case alphabetic. A split is a base that is a prefix of
    `alpha` and a quote that immediately follows it, BOTH in `codes`, at any
    length the table carries; whatever follows the quote is a suffix and is
    ignored. Where more than one split is valid, the rule is:

    1. the split that consumes the FEWEST characters wins, so a suffix letter
       is never absorbed into a code when a shorter reading exists. Every
       symbol that resolved under the old 3-and-3 split therefore resolves
       identically (six is the minimum), and with USDT in the table BTCUSDT
       would still read BTC/USD, the #66 reading;
    2. on an equal count, the LONGER base wins.

    A greedy longest-base parse is NOT the rule: with USDT in the table it
    would take USDT from USDTRY and be left with RY. Requiring both halves
    first is what keeps USDTRY as USD/TRY.
    """
    lengths = _code_lengths(codes)
    best: tuple[int, int, str, str] | None = None
    for i in lengths:
        base = alpha[:i]
        if len(base) < i or base not in codes:
            continue
        for j in lengths:
            quote = alpha[i:i + j]
            if len(quote) < j or quote not in codes:
                continue
            if best is None or (i + j, -i) < (best[0], best[1]):
                best = (i + j, -i, base, quote)
    return None if best is None else (best[2], best[3])


def parse_fx(symbol: str) -> tuple[str, str] | None:
    """Base and quote when the symbol is an FX pair, otherwise None.

    Non-alphabetic characters are dropped and the rest is split into two
    codes from CURRENCY_CODES by `resolve_pair`, which states the rule when
    more than one split is valid. Codes may be longer than three characters
    (DOGEUSD is DOGE/USD). The table CONFIRMS a pair; it never refuses a trade.

    None therefore means one thing only: the currency-exposure limit does not
    apply to this symbol. It covers an instrument that cannot be a pair (US30),
    a decoration that hides the pair (FXEURUSD, mEURUSD), and a pair whose code
    is missing from the table. The caller allows the trade and records the
    exclusion; it never treats None as zero exposure and never refuses on it.

    Dropping separators is safe BECAUSE the table confirms: EUR.USD resolves to
    EUR/USD, while US30.cash resolves to USCASH and is rejected by the codes
    rather than by the shape.
    """
    s = "".join(ch for ch in symbol if ch.isalpha()).upper()
    return resolve_pair(s, CURRENCY_CODES)


def classify_symbol(symbol: str) -> str:
    """Say whether the currency-exposure limit applies to this symbol.

    SYMBOL_FX: the alphabetic characters begin with two recognised currency
    codes (see `resolve_pair`), so the limit applies.

    SYMBOL_NOT_FX: everything else. The limit is NOT APPLICABLE, which is not
    the same as unmeasurable and gets the opposite answer: the caller ALLOWS
    the trade and records the exclusion. There is no third state, because
    cannot-tell and is-not-FX deserve the same treatment: do not pretend to
    measure currency exposure, do not block the trade, make it visible.

    Not applicable is never silence. Silence was the original defect.
    """
    return SYMBOL_FX if parse_fx(symbol) is not None else SYMBOL_NOT_FX


class UnclassifiedSymbol(ValueError):
    """Raised by currency_exposure for a symbol that is not an FX pair.

    TRIPWIRE, not a gate. evaluate() classifies first and never passes a
    non-FX symbol here, so this cannot fire from any broker symbol. It exists
    so a future caller cannot reintroduce the silent skip that was the defect
    in issue #10, and so caller/classifier divergence fails loudly.
    """

def currency_exposure(
    positions: Sequence[Position | PendingOrder],
    extra: tuple[str, Side] | None = None,
) -> dict[str, int]:
    """Net count of COMMITMENTS touching each currency. Buy EURUSD: +EUR, -USD.

    Takes working orders as well as positions. Only `symbol` and `side` are read,
    which both carry, and a resting order commits the same legs a filled one
    does. The parameter keeps its name because callers pass it positionally.

    One bucket per code and no other kind of bucket, so a crypto leg, a metal
    leg and a fiat leg land in the same place: buy BTCUSD and the USD leg is
    the same -1 that EURUSD and XAUUSD contribute. That is the issue #66
    decision, and `currencies.py` carries the argument for it, including why a
    separate crypto bucket was rejected.

    Raises UnclassifiedSymbol when a symbol cannot be resolved to a pair. An
    unclassified symbol is unknown exposure, so it must never be counted as
    zero and silently dropped from the limit.
    """
    counts: dict[str, int] = {}

    def apply(symbol: str, side: Side, sign: int = 1) -> None:
        pair = parse_fx(symbol)
        if pair is None:
            raise UnclassifiedSymbol(symbol)
        base, quote = pair
        if side is Side.BUY:
            counts[base] = counts.get(base, 0) + sign
            counts[quote] = counts.get(quote, 0) - sign
        else:
            counts[base] = counts.get(base, 0) - sign
            counts[quote] = counts.get(quote, 0) + sign

    for p in positions:
        apply(p.symbol, p.side)
    if extra is not None:
        apply(extra[0], extra[1])
    return counts


def _hhmm_to_min(s: str) -> int:
    h, m = s.split(":")
    return int(h) * 60 + int(m)


def in_session(ts: datetime, session: SessionConfig) -> bool:
    if not session.enabled:
        return True
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    ts = ts.astimezone(timezone.utc)
    minutes = ts.hour * 60 + ts.minute
    start = _hhmm_to_min(session.start_utc)
    end = _hhmm_to_min(session.end_utc)
    if start <= end:
        inside = start <= minutes < end
    else:
        inside = minutes >= start or minutes < end
    if not inside:
        return False
    if ts.weekday() == 4:  # Friday
        cutoff = _hhmm_to_min(session.skip_friday_after_utc)
        if minutes >= cutoff:
            return False
    if ts.weekday() >= 5:  # Sat/Sun
        return False
    return True


def day_key(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%d")


class RiskManager:
    def __init__(
        self,
        cfg: BotConfig,
        *,
        halt_dir: str | Path = ".",
        state_path: str | Path | None = None,
    ) -> None:
        self.cfg = cfg
        self.halt_dir = Path(halt_dir)
        self.state_path = (
            Path(state_path)
            if state_path is not None
            else snapshot_path_for(cfg.journal_path)
        )
        self.state_error = ""
        self.snapshot = EquitySnapshot(
            time=0,
            balance=cfg.initial_balance,
            equity=cfg.initial_balance,
            peak_equity=cfg.initial_balance,
            day_start_equity=cfg.initial_balance,
            day_key="",
        )
        self._halted = False
        self._halt_reason = ""
        # Set once, by _restore_state / _persist_state, and never overwritten by
        # circuit()'s halt_file check (#15 item 4). _halt_reason IS overwritten
        # by that check on every call while the operator HALT file exists, which
        # clobbers whatever reason was there before it. daily_loss and
        # max_drawdown recover from that because every gate recomputes them
        # fresh from the snapshot; state_unreadable and state_unwritable do not
        # self-heal that way (the file is read once, at start), so without a
        # separate slot, clear_operator_halt() could silently drop a COULD NOT
        # MEASURE halt with nothing left to prove it should not have.
        self._state_integrity_reason = ""
        self._restore_state()

    def _restore_state(self) -> None:
        """Load the persisted snapshot. Absent is clean; unreadable is closed."""
        try:
            restored = load_snapshot(self.state_path)
        except StateUnreadable as exc:
            # COULD NOT MEASURE. Not a clean state, and not a default. Refuse to
            # trade and leave the file alone so the operator can look at it.
            self.state_error = str(exc)
            self._halted = True
            self._halt_reason = "state_unreadable"
            self._state_integrity_reason = "state_unreadable"
            return
        if restored is not None:
            self.snapshot = restored

    def _persist_state(self) -> None:
        """Write the snapshot. A write failure halts; it is a measurement loss."""
        if self._state_integrity_reason == "state_unreadable":
            return  # the unreadable file is evidence; do not clobber it
        try:
            save_snapshot(self.state_path, self.snapshot)
        except StateUnwritable as exc:
            self.state_error = str(exc)
            self._state_integrity_reason = "state_unwritable"
            if not self._halted:
                self._halted = True
                self._halt_reason = "state_unwritable"

    def halt_path(self) -> Path:
        p = Path(self.cfg.risk.halt_file)
        return p if p.is_absolute() else self.halt_dir / p

    @property
    def halt_reason(self) -> str:
        return self._halt_reason

    @property
    def is_halted(self) -> bool:
        return self._halted or self.halt_path().exists()

    def write_halt_file(self, reason: str = "operator") -> Path:
        path = self.halt_path()
        path.write_text(reason + "\n", encoding="utf-8")
        os.chmod(path, 0o600)
        return path

    def _equity_halt_reason(self) -> str:
        """daily_loss / max_drawdown, recomputed fresh from the snapshot.

        Same condition circuit() checks, extracted so clear_operator_halt()
        can ask "is this independently still true" without trusting
        _halt_reason, which circuit() overwrites to "halt_file" on every call
        while the operator HALT file exists (#15 item 4).
        """
        s = self.snapshot
        r = self.cfg.risk
        daily_loss = s.day_start_equity - s.equity
        if daily_loss >= s.day_start_equity * r.daily_loss_pct:
            return "daily_loss"
        dd = s.peak_equity - s.equity
        if s.peak_equity > 0 and dd >= s.peak_equity * r.max_drawdown_pct:
            return "max_drawdown"
        return ""

    def clear_operator_halt(self) -> str:
        """Clear the HALT file. Leaves any independently-true halt in place.

        Returns the remaining halt reason, or empty string if the bot may
        resume. _halt_reason alone cannot answer this (see
        _equity_halt_reason and _state_integrity_reason): it is a single
        slot that circuit() overwrites to "halt_file" on every call while
        the operator HALT file exists, discarding whatever reason was
        there before. daily_loss / max_drawdown are re-derived from the
        snapshot; a state-integrity halt is re-asserted from the
        dedicated, never-overwritten slot.
        """
        path = self.halt_path()
        if path.exists():
            path.unlink()
        if self._state_integrity_reason:
            self._halted = True
            self._halt_reason = self._state_integrity_reason
            return self._state_integrity_reason
        sticky = self._equity_halt_reason()
        if sticky:
            self._halted = True
            self._halt_reason = sticky
            return sticky
        self._halted = False
        self._halt_reason = ""
        return ""

    def _durable(self) -> tuple[str, float, float, int, int]:
        """The fields a restart must not lose.

        The two counters are here for the same reason the loss budget is: if a
        send did not move this tuple, it would not be persisted, and a restart
        would hand out a fresh daily allowance.
        """
        s = self.snapshot
        return (
            s.day_key,
            s.day_start_equity,
            s.peak_equity,
            s.trades_today,
            s.advice_turns_today,
        )

    def observe(self, account: Account, now: datetime) -> None:
        before = self._durable()
        key = day_key(now)
        if self.snapshot.day_key != key:
            self.snapshot.day_start_equity = account.equity
            self.snapshot.day_key = key
            # A new UTC day is the only thing that clears the allowances.
            self.snapshot.trades_today = 0
            self.snapshot.advice_turns_today = 0
            if self._halt_reason == "daily_loss":
                self._halted = False
                self._halt_reason = ""
        self.snapshot.time = int(now.timestamp())
        self.snapshot.balance = account.balance
        self.snapshot.equity = account.equity
        if account.equity > self.snapshot.peak_equity:
            self.snapshot.peak_equity = account.equity
        if self._durable() != before:
            # Persist on every evaluation that MOVES the snapshot, not only on a
            # halt: a file written only at the halt has already lost the peak the
            # drawdown gate needs.
            self._persist_state()

    def record_trade(self) -> None:
        """One opening send actually reached the broker.

        Counted at the send, never at the decision: a preview, a refused
        confirm and an expired stage all call evaluate() and none of them is a
        trade. Persisted immediately, because the process can be killed between
        the send and the next observe().
        """
        self.snapshot.trades_today += 1
        self._persist_state()

    def record_advice_turn(self) -> None:
        """One advice turn was billed. Same reasoning, different budget."""
        self.snapshot.advice_turns_today += 1
        self._persist_state()

    def advice_turns_exhausted(self) -> bool:
        cap = int(getattr(self.cfg.advice, "max_turns_per_day", 0) or 0)
        return bool(cap) and self.snapshot.advice_turns_today >= cap

    def _halt(self, reason: str, flatten: bool = True) -> RiskDecision:
        self._halted = True
        self._halt_reason = reason
        return RiskDecision(allowed=False, reason=reason, halt=True, flatten=flatten)

    def circuit_reason(self, account: Account, now: datetime) -> str:
        """Why a new entry would be refused. Empty if the circuit is clear.

        Does not trip daily_loss / max_drawdown and does not flatten. It can
        report and latch `state_unwritable`, because that is a real measurement
        failure discovered while reading, not a verdict about the market.
        """
        self.observe(account, now)
        r = self.cfg.risk
        if self.halt_path().exists():
            return "halt_file"
        if self._halted:
            return self._halt_reason or "halted"
        if not account.trade_allowed or not account.trade_expert:
            return "trade_not_allowed"
        if self.cfg.mode in {"mt5", "mt4"} and account.trade_mode == 2 and not self.cfg.live_accepted:
            return "live_not_accepted"
        daily_loss = self.snapshot.day_start_equity - account.equity
        if daily_loss >= self.snapshot.day_start_equity * r.daily_loss_pct:
            return "daily_loss"
        dd = self.snapshot.peak_equity - account.equity
        if self.snapshot.peak_equity > 0 and dd >= self.snapshot.peak_equity * r.max_drawdown_pct:
            return "max_drawdown"
        return ""

    def circuit(self, account: Account, now: datetime) -> RiskDecision:
        """Account-level halt gates. No signal, no sizing."""
        self.observe(account, now)
        r = self.cfg.risk
        if self.halt_path().exists():
            return self._halt("halt_file")
        if self._halted:
            return RiskDecision(allowed=False, reason=self._halt_reason, halt=True, flatten=True)
        if not account.trade_allowed or not account.trade_expert:
            return RiskDecision(allowed=False, reason="trade_not_allowed")
        if self.cfg.mode in {"mt5", "mt4"} and account.trade_mode == 2 and not self.cfg.live_accepted:
            return RiskDecision(allowed=False, reason="live_not_accepted")
        daily_loss = self.snapshot.day_start_equity - account.equity
        if daily_loss >= self.snapshot.day_start_equity * r.daily_loss_pct:
            return self._halt("daily_loss")
        dd = self.snapshot.peak_equity - account.equity
        if self.snapshot.peak_equity > 0 and dd >= self.snapshot.peak_equity * r.max_drawdown_pct:
            return self._halt("max_drawdown")
        return RiskDecision(allowed=True, reason="ok")

    def loss_room(self, account: Account) -> float:
        """Money this account may still lose before a halt gate would trip.

        The daily-loss and drawdown budgets are computed from the persisted
        EquitySnapshot: day_start_equity is set once per UTC day and survives
        a restart, and peak_equity outlives the process. The sizer is handed
        neither, so a gate built on them can DISAGREE with the sizer. A gate
        that recomputes what the sizer already computed, from the inputs the
        sizer was already given, is not a second layer; it is a slower copy,
        and it cannot report anything the first layer did not.

        Positive whenever the circuit is clear: both halt gates fire at or
        before zero room, so evaluate reads this only after circuit passed.
        Call it after observe, so the snapshot is the current one.
        """
        r = self.cfg.risk
        s = self.snapshot
        room = s.day_start_equity * r.daily_loss_pct - (s.day_start_equity - account.equity)
        if s.peak_equity > 0:
            dd_room = s.peak_equity * r.max_drawdown_pct - (s.peak_equity - account.equity)
            room = min(room, dd_room)
        return room

    def evaluate(
        self,
        *,
        account: Account,
        signal: Signal,
        spec: SymbolSpec,
        tick: Tick,
        positions: list[Position],
        orders: Sequence[PendingOrder],
        now: datetime,
        manual: bool = False,
    ) -> RiskDecision:
        trip = self.circuit(account, now)
        if not trip.allowed:
            return trip
        r = self.cfg.risk

        if signal.kind is SignalKind.FLAT or signal.side is None:
            return RiskDecision(allowed=False, reason="no_signal")

        if not manual and not in_session(now, self.cfg.session):
            return RiskDecision(allowed=False, reason="outside_session")

        ours = [p for p in positions if p.magic == r.magic]
        # A WORKING ORDER IS COMMITTED EXPOSURE. It is not a hypothesis: it rests
        # at the broker and becomes a position without anyone being asked again,
        # so every gate that counts commitments has to count it. Reproduced
        # before this: four buy limits on one symbol, all accepted, 2% of equity
        # committed and past max_positions, because none had left a Position yet.
        #
        # `orders` has no default on purpose. A new caller that forgets it gets a
        # TypeError rather than the old fail-OPEN behaviour.
        ours_orders = [o for o in orders if o.magic == r.magic]
        committed: list[Position | PendingOrder] = [*ours, *ours_orders]
        if len(committed) >= r.max_positions:
            return RiskDecision(allowed=False, reason="max_positions")
        # Before sizing and before the spec gates: this is a budget on ACTIONS,
        # not on the market, so nothing about the instrument can change it.
        # `daily_loss_pct` only fires once the money is gone; this bounds the
        # churn that gets there.
        if r.max_trades_per_day and self.snapshot.trades_today >= r.max_trades_per_day:
            return RiskDecision(allowed=False, reason="max_trades_per_day")
        if any(x.symbol == signal.symbol for x in committed):
            return RiskDecision(allowed=False, reason="already_in_symbol")

        staged_fx = classify_symbol(signal.symbol) == SYMBOL_FX
        fx_ours = [x for x in committed if classify_symbol(x.symbol) == SYMBOL_FX]
        excluded = tuple(
            ([] if staged_fx else [signal.symbol])
            + [x.symbol for x in committed if classify_symbol(x.symbol) != SYMBOL_FX]
        )
        extra = (signal.symbol, signal.side) if staged_fx else None
        try:
            exposure = currency_exposure(fx_ours, extra=extra)
        # TRIPWIRE, not a gate: fx_ours is pre-filtered, so no broker symbol
        # reaches this. It catches caller/classifier divergence only. A non-FX
        # position contributes nothing to currency exposure, which is correct
        # and not an underestimate, so it must never trip this.
        except UnclassifiedSymbol:
            return RiskDecision(allowed=False, reason="exposure_unmeasured")
        if any(abs(v) > r.max_currency_exposure for v in exposure.values()):
            return RiskDecision(
                allowed=False,
                reason="currency_exposure",
                excluded_from_currency_limit=excluded,
            )

        if signal.sl <= 0 or signal.risk_distance <= 0:
            return RiskDecision(allowed=False, reason="sl_required")
        if signal.rr + 1e-9 < r.min_rr:
            return RiskDecision(allowed=False, reason="rr_below_min")

        # Before any gate reads the spec. `min_stop_distance()` is
        # stops_level * point, so an unmeasured point makes the next check pass
        # trivially, and `lots_for_risk` would refuse as `size_zero`, which
        # says the budget was too small. Nothing measured is a different fact
        # and it gets its own name.
        not_measured = spec.unmeasured_for_sizing()
        if not_measured:
            return RiskDecision(
                allowed=False,
                reason="spec_not_measured:" + ",".join(sorted(not_measured)),
            )

        min_dist = spec.min_stop_distance()
        if signal.risk_distance < min_dist:
            return RiskDecision(allowed=False, reason="stops_level")

        if signal.atr > 0 and tick.spread > r.max_spread_atr_frac * signal.atr:
            return RiskDecision(allowed=False, reason="spread_too_wide")

        # The operator's slippage tolerance against the live market, in POINTS.
        #
        # `deviation` is the maximum slippage the venue may apply to the fill,
        # and a point is instrument-specific: 20 points is 2 pips on a 5-digit
        # EURUSD and 20 cents on XAUUSD. Measured on the live MT4 rig
        # 2026-09-24, gold quoted a 45-point (45 cent) spread against the
        # global default of 20, so every gold send offered the venue less than
        # half of one spread of tolerance. OrderSend rejects that
        # intermittently, and nothing in the log named the cause: the symptom
        # reaching the operator was "trades randomly do not go through".
        #
        # Both sides are converted to points before comparing, so one multiple
        # is correct on every instrument and the gate never needs to know which
        # symbol it is holding.
        #
        # It refuses; it does NOT raise the deviation to a workable number.
        # Silently overriding an operator's risk figure is worse than refusing:
        # the number in force would then be neither what they set nor anything
        # they can read, and the next person to open the config would be
        # reading a fiction.
        #
        # The spread is read HERE, at evaluate time, from the same tick the
        # stop and sizing gates used. Not `spec.spread` (a venue-reported
        # figure the MT4 bridge does not populate) and not a configured
        # typical: a spread from a minute ago is not the one the send will
        # meet. That makes this gate as transient as the market, which is why
        # `spread_too_wide` runs FIRST -- a temporary blowout is named as a
        # wide spread, and only a spread the instrument carries under normal
        # conditions reaches here and is named as a misconfiguration.
        #
        # A spread of zero or less is a broken or crossed tick, not a free
        # pass: there is nothing to compare against, so this gate ABSTAINS and
        # says so here rather than reporting a pass it did not measure. No gate
        # currently owns a crossed tick; that is a separate finding, not
        # something this one should absorb quietly.
        dev = r.resolve_deviation(signal.symbol)
        spread_points = spec.points(tick.spread)
        floor_points = r.min_deviation_spread_multiple * spread_points
        if spread_points > 0 and dev.points < floor_points - 1e-9:
            # `floor` is what this gate demanded; `set` is what to actually
            # write, and it is larger. See DEVIATION_HEADROOM_MULTIPLE: fixing
            # the config to the exact floor is rejected again by the next tick
            # that widens the spread.
            floor = math.ceil(floor_points - 1e-9)
            advised = math.ceil(DEVIATION_HEADROOM_MULTIPLE * spread_points - 1e-9)
            return RiskDecision(
                allowed=False,
                reason=(
                    DEVIATION_BELOW_SPREAD
                    + f":{signal.symbol}"
                    + f",deviation={dev.points}"
                    + f",source={dev.source}"
                    + f",spread={spread_points:.0f}pt"
                    + f",floor={floor}pt"
                    + f",set=risk.symbol_deviation_points.{signal.symbol}>={advised}"
                ),
            )

        if account.equity > 0:
            free_frac = account.margin_free / account.equity if account.equity else 0
            if free_frac < r.min_free_margin_pct and account.margin > 0:
                return RiskDecision(allowed=False, reason="margin_buffer")

        lots = lots_for_risk(
            account.equity,
            r.risk_pct,
            signal.entry,
            signal.sl,
            spec,
            max_risk_multiple=r.max_risk_multiple,
        )
        if lots <= 0:
            return RiskDecision(allowed=False, reason="size_zero")

        # Last line, and two caps that are here for different reasons.
        #
        # per_trade re-derives the cap `lots_for_risk` already applied. It
        # cannot fire against today's sizer, which enforces the same
        # inequality with the same tolerance before it returns. It is kept as
        # the backstop for a future change that loosens the sizer, and a test
        # breaks the sizer on purpose so that half is watched firing: a term
        # that can only fire after a regression is a backstop, but only if
        # something can make it fire.
        #
        # loss_room is the live half, and the reason this is a second layer
        # rather than a slower copy of the first. It is derived from the
        # persisted snapshot, which the sizer is never given, so it can
        # DISAGREE: a full stop-out on this volume must not carry the account
        # through a halt it has not tripped yet.
        worst = money_per_lot_at_stop(signal.entry, signal.sl, spec) * lots
        per_trade = account.equity * r.risk_pct * r.max_risk_multiple
        if worst > min(per_trade, self.loss_room(account)) + 1e-9:
            return RiskDecision(allowed=False, reason="size_exceeds_risk")

        return RiskDecision(
            allowed=True,
            reason="ok",
            volume=lots,
            excluded_from_currency_limit=excluded,
        )
