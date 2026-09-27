"""Shared domain types. Broker adapters map MT5 namedtuples onto these."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> Side:
        return Side.SELL if self is Side.BUY else Side.BUY


class SignalKind(str, Enum):
    BUY = "buy"
    SELL = "sell"
    FLAT = "flat"


@dataclass(frozen=True)
class MarketOrder:
    """Venue-neutral market send. Adapters map this to MT5, IBKR, etc."""

    symbol: str
    side: Side
    volume: float
    sl: float = 0.0
    tp: float = 0.0
    comment: str = ""
    magic: int = 0
    deviation: int = 20
    ticket: int | None = None
    #: The idempotency key for this send, minted when the order was STAGED and
    #: unchanged across a retry or a process restart. A typed field rather than
    #: something parsed back out of `comment`, because the venue may rewrite the
    #: comment and the contract must not depend on the venue preserving it.
    client_id: str = ""


@dataclass(frozen=True)
class WorkingOrder:
    """Venue-neutral working (limit/stop) send."""

    symbol: str
    side: Side
    kind: str
    volume: float
    price: float
    sl: float = 0.0
    tp: float = 0.0
    comment: str = ""
    magic: int = 0
    #: Maximum tolerated slippage in POINTS, same field and same default as
    #: `MarketOrder.deviation`. It was ABSENT here until #92, while
    #: `risk.evaluate()` gated limit and stop signals on `deviation_below_spread`
    #: exactly as it gates a market signal: the gate judged a number this order
    #: had no way to carry, so its refusal could not be a true statement about
    #: the send it refused, and the config key its advice names changed nothing
    #: on this path. Whether a venue APPLIES a slippage tolerance to a pending
    #: order is a separate question, and one no test in this repo can answer;
    #: transmitting the operator's figure instead of a number nobody configured
    #: on this side does not depend on the answer.
    deviation: int = 20
    ticket: int | None = None
    #: Same key, same reason as `MarketOrder.client_id`.
    client_id: str = ""


@dataclass(frozen=True)
class Bar:
    time: int
    open: float
    high: float
    low: float
    close: float
    tick_volume: int = 0
    spread: int = 0
    real_volume: int = 0


@dataclass(frozen=True)
class Tick:
    time: int
    bid: float
    ask: float
    last: float = 0.0
    volume: int = 0

    @property
    def mid(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread(self) -> float:
        return self.ask - self.bid


@dataclass(frozen=True)
class Account:
    login: int
    balance: float
    equity: float
    margin: float
    margin_free: float
    profit: float
    leverage: int
    currency: str
    trade_allowed: bool = True
    trade_expert: bool = True
    server: str = ""
    name: str = ""
    fifo_close: bool = False
    credit: float = 0.0
    margin_level: float = 0.0
    trade_mode: int = 0  # 0 demo, 1 contest, 2 real


#: The spec fields the sizer and the risk gates actually read. A spec that
#: could not measure one of these cannot be sized against at all, so the gate
#: refuses by NAME instead of letting `normalize_volume` return zero and
#: reporting `size_zero`, which says the budget was too small: a different fact.
SPEC_SIZING_FIELDS = frozenset(
    {"point", "tick_size", "tick_value", "volume_step", "volume_min", "volume_max"}
)


@dataclass(frozen=True)
class SymbolSpec:
    name: str
    digits: int
    point: float
    trade_tick_size: float
    trade_tick_value: float
    trade_contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    trade_stops_level: int
    trade_freeze_level: int
    filling_mode: int
    currency_base: str
    currency_profit: str
    currency_margin: str
    trade_mode: int = 4  # full
    visible: bool = True
    spread: int = 0
    #: Wire field names this spec could NOT measure. Empty means every field is
    #: a measurement. A name here means the venue either did not send the field
    #: or sent a value that cannot be a measurement, for instance a zero where
    #: zero is impossible. The field itself is then left at a value that cannot
    #: be mistaken for usable, never at a plausible default: a fabricated
    #: default is an absent measurement wearing a measurement's clothes.
    unmeasured: frozenset[str] = frozenset()

    def unmeasured_for_sizing(self) -> frozenset[str]:
        """The unmeasured fields that sizing and the risk gates depend on."""
        return self.unmeasured & SPEC_SIZING_FIELDS

    def normalize_price(self, price: float) -> float:
        return round(price, self.digits)

    def min_stop_distance(self) -> float:
        return self.trade_stops_level * self.point

    def points(self, price_distance: float) -> float:
        """A price distance expressed in venue POINTS.

        This is the unit conversion that makes an instrument-specific number
        comparable across instruments. 0.0002 on a 5-digit EURUSD (point
        0.00001) is 20 points; 0.45 on XAUUSD (point 0.01) is 45 points. A
        limit configured in points can therefore be compared against the live
        market without the gate knowing which symbol it holds.

        0.0 when `point` is not a measurement. A caller must not read that as
        a small distance: every gate that needs points runs AFTER the
        `spec_not_measured` refusal, which names `point` (it is in
        SPEC_SIZING_FIELDS) and stops before any such gate is reached.
        """
        if self.point <= 0:
            return 0.0
        return price_distance / self.point


@dataclass(frozen=True)
class PendingOrder:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price: float
    sl: float
    tp: float
    magic: int = 0
    comment: str = ""
    kind: str = ""  # "limit"|"stop"
    time: int = 0


@dataclass
class Position:
    ticket: int
    symbol: str
    side: Side
    volume: float
    price_open: float
    sl: float
    tp: float
    price_current: float
    profit: float
    swap: float = 0.0
    magic: int = 0
    comment: str = ""
    time: int = 0
    identifier: int = 0

    @property
    def risk_distance(self) -> float:
        if self.sl <= 0:
            return 0.0
        return abs(self.price_open - self.sl)


@dataclass(frozen=True)
class OrderResult:
    retcode: int
    comment: str = ""
    deal: int = 0
    order: int = 0
    volume: float = 0.0
    price: float = 0.0
    bid: float = 0.0
    ask: float = 0.0
    request: dict[str, Any] = field(default_factory=dict)
    # Exposure left behind by a send that reported failure.
    #   0    the venue checked and nothing survived
    #   N    ticket N is still on the book and the desk was told otherwise
    #   None the venue did not answer, which is COULD NOT MEASURE
    # Venues that attach the stop in the same call as the entry have no
    # such window and leave this at 0.
    survivor_ticket: int | None = 0
    #: Did this request leave the process?
    #:
    #: Only ever False together with `measured` False, and the pair is not a
    #: redundancy: "the venue did not answer" and "we declined to ask" are
    #: different facts and only one of them can have moved money. Reporting a
    #: refusal in the words of a lost reply tells an operator to go hunting for a
    #: position that cannot exist; reporting a lost reply in the words of a
    #: refusal is far worse, because it says nothing is on the book when
    #: something may be. Defaults True, so every ordinary result is what it was
    #: before: something went out and the venue answered it.
    transmitted: bool = True

    @property
    def ok(self) -> bool:
        from straightedge.constants import RETCODE_OK

        return self.retcode in RETCODE_OK

    @property
    def measured(self) -> bool:
        """True when the broker actually answered.

        False means the call produced no result, so nothing was measured and
        neither `ok` nor `retcode` carries a verdict. A caller that gates on a
        pre-trade check must abort on this, because an unmeasured check is not
        a passed check.
        """
        from straightedge.constants import RETCODE_UNKNOWN

        return self.retcode != RETCODE_UNKNOWN

    @classmethod
    def unknown(cls, comment: str, request: dict[str, Any] | None = None) -> OrderResult:
        """The request WENT OUT and no result came back: COULD NOT MEASURE.

        This is the expensive one. It may have filled, so nothing downstream may
        read it as a rejection, resolve an in-flight entry on it, or retry it.
        """
        from straightedge.constants import RETCODE_UNKNOWN

        return cls(retcode=RETCODE_UNKNOWN, comment=comment, request=request or {})

    @classmethod
    def not_sent(cls, comment: str) -> OrderResult:
        """We DECLINED to transmit. No verdict, and nothing on the wire.

        Unmeasured like `unknown`, because there is still no verdict to act on,
        and every safety rule that keys off `measured` applies unchanged. It is
        separate so an operator is told which of the two happened; `comment`
        carries the reason and is shown verbatim.
        """
        from straightedge.constants import RETCODE_UNKNOWN

        return cls(retcode=RETCODE_UNKNOWN, comment=comment, transmitted=False)

    @classmethod
    def unchanged(cls) -> OrderResult:
        from straightedge.constants import TRADE_RETCODE_DONE

        return cls(retcode=TRADE_RETCODE_DONE, comment="unchanged")

    @classmethod
    def invalid_stops(cls, comment: str) -> OrderResult:
        from straightedge.constants import TRADE_RETCODE_INVALID_STOPS

        return cls(retcode=TRADE_RETCODE_INVALID_STOPS, comment=comment)


@dataclass(frozen=True)
class FlattenReport:
    """What a flatten sweep actually achieved. Counts, never a bare boolean.

    positions_requested         open positions read before the sweep (the denominator)
    positions_confirmed_closed  closes whose filled volume covered the whole position
    positions_closed_elsewhere  absent from the post-sweep read and never confirmed by
                                us, i.e. an SL/TP or a manual close landed between the
                                read and the sweep. Not exposure, so not an alarm.
    survivors                   tickets that may still carry exposure: present in the
                                post-sweep read, or a close that reported residual
                                volume, or unverifiable because the read failed
    residual                    the subset of survivors whose close claimed success
                                while filling less volume than requested
    measured                    False when the post-sweep read could not be taken. For
                                a flatten, COULD NOT MEASURE is an incomplete sweep,
                                never a clean one: every requested ticket is counted as
                                a survivor, because the broker that could not be read is
                                the same broker whose close confirmations would have to
                                be believed. The count is then an upper bound.

    The denominator is an identity, not a vibe:
    confirmed_closed + closed_elsewhere + |survivors from the requested set| == requested.
    Each instrument can only move survivors up, never down.
    """

    reason: str
    positions_requested: int = 0
    positions_confirmed_closed: int = 0
    positions_closed_elsewhere: int = 0
    survivors: tuple[int, ...] = ()
    residual: tuple[int, ...] = ()
    orders_requested: int = 0
    orders_confirmed_cancelled: int = 0
    orders_gone_elsewhere: int = 0
    order_survivors: tuple[int, ...] = ()
    measured: bool = True

    @property
    def survivor_count(self) -> int:
        return len(self.survivors)

    @property
    def complete(self) -> bool:
        return (
            self.measured
            and not self.survivors
            and not self.order_survivors
            and self.positions_confirmed_closed + self.positions_closed_elsewhere
            == self.positions_requested
            and self.orders_confirmed_cancelled + self.orders_gone_elsewhere
            == self.orders_requested
        )

    def summary(self) -> str:
        """One line a human can act on. Never a fixed string."""
        if self.complete:
            extra = ""
            if self.positions_closed_elsewhere:
                extra = f" ({self.positions_closed_elsewhere} closed elsewhere)"
            return (
                f"flattened {self.positions_confirmed_closed}/{self.positions_requested} "
                f"positions{extra}, cancelled "
                f"{self.orders_confirmed_cancelled}/{self.orders_requested} orders; halted."
            )
        bits = [f"FLATTEN INCOMPLETE: {self.survivor_count} still open"]
        if self.survivors:
            bits.append("(" + ", ".join(f"#{t}" for t in self.survivors) + ")")
        bits.append(
            f"positions requested={self.positions_requested} "
            f"confirmed_closed={self.positions_confirmed_closed}."
        )
        if self.residual:
            bits.append(
                "partial fill left residual volume on "
                + ", ".join(f"#{t}" for t in self.residual)
                + "."
            )
        if self.order_survivors:
            bits.append(
                f"{len(self.order_survivors)} working order(s) not cancelled: "
                + ", ".join(f"#{t}" for t in self.order_survivors)
                + "."
            )
        if not self.measured:
            bits.append("COULD NOT MEASURE the post-sweep state; treated as incomplete.")
        bits.append("HALTED; no new entries. Check the terminal.")
        return " ".join(bits)


@dataclass(frozen=True)
class Signal:
    kind: SignalKind
    symbol: str
    entry: float
    sl: float
    tp: float
    atr: float
    reason: str = ""
    fast_ema: float = 0.0
    slow_ema: float = 0.0
    adx: float = 0.0
    pending_kind: str = ""

    @property
    def side(self) -> Side | None:
        if self.kind is SignalKind.BUY:
            return Side.BUY
        if self.kind is SignalKind.SELL:
            return Side.SELL
        return None

    @property
    def risk_distance(self) -> float:
        return abs(self.entry - self.sl)

    @property
    def reward_distance(self) -> float:
        return abs(self.tp - self.entry)

    @property
    def rr(self) -> float:
        if self.risk_distance <= 0:
            return 0.0
        return self.reward_distance / self.risk_distance

    def reprice(self, entry: float, spec: SymbolSpec) -> Signal:
        """Keep ATR distances, move entry to the live bid/ask."""
        if self.kind is SignalKind.BUY:
            sl = spec.normalize_price(entry - (self.entry - self.sl))
            tp = spec.normalize_price(entry + (self.tp - self.entry))
        elif self.kind is SignalKind.SELL:
            sl = spec.normalize_price(entry + (self.sl - self.entry))
            tp = spec.normalize_price(entry - (self.entry - self.tp))
        else:
            return self
        return Signal(
            kind=self.kind,
            symbol=self.symbol,
            entry=spec.normalize_price(entry),
            sl=sl,
            tp=tp,
            atr=self.atr,
            reason=self.reason,
            fast_ema=self.fast_ema,
            slow_ema=self.slow_ema,
            adx=self.adx,
            pending_kind=self.pending_kind,
        )


@dataclass
class RiskDecision:
    allowed: bool
    reason: str
    volume: float = 0.0
    halt: bool = False
    flatten: bool = False
    excluded_from_currency_limit: tuple[str, ...] = ()


@dataclass
class EquitySnapshot:
    time: int
    balance: float
    equity: float
    peak_equity: float
    day_start_equity: float
    day_key: str
    #: Opening sends and advice turns already spent inside `day_key`. Durable
    #: for the same reason the loss budget is: a cap a restart clears is not a
    #: cap, and a crash loop would hand out a fresh allowance every time.
    trades_today: int = 0
    advice_turns_today: int = 0
