"""Shared domain types. Broker adapters map MT5 namedtuples onto these."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from straightedge.constants import (
    VENUE_CLOCK_GRID_SEC,
    VENUE_CLOCK_MAX_OFFSET_SEC,
    VENUE_CLOCK_MIN_OFFSET_SEC,
)


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


class VenueClockUnmeasured(RuntimeError):
    """A venue timestamp was converted while the venue's offset was unknown.

    The backstop, not the control. Every caller is expected to check
    `VenueClock.measured` and refuse with a named reason; this raise is what
    stops a FUTURE caller getting a plausible-looking instant out of a
    measurement that was never taken.
    """


@dataclass(frozen=True)
class VenueClock:
    """How the venue STAMPS time, measured against real UTC.

    MT4 and MT5 stamp bars and ticks with the broker server's own wall clock,
    and nothing on the wire says what that clock's offset from UTC is. A desk
    that reads one of those integers as UTC is not off by a rounding error, it
    is off by the server's offset: the live desk runs on a UTC+3 server, so
    before straightedge#172 its session window, its daily-loss day boundary
    and its weekend block all ran three hours early.

    `offset_sec` is `server wall clock - UTC`, so UTC+3 is +10800. Convert
    with `to_utc`, never by hand.

    MEASURED, never configured, and that is the whole design. The offset is a
    per-server property that moves with the SERVER's DST rules, so a number in
    a config file is a guess that outlives the first DST change after somebody
    wrote it. That is straightedge#68 (an instrument-blind constant that
    silently mis-sized gold) with a clock in place of a tick value, and the
    standing rule from that issue applies unchanged: an unmeasured spec
    REFUSES, it never defaults.

    So `offset_sec` is `None` exactly when the measurement failed, and
    `unmeasured` names what could not be measured. There is deliberately no
    "assume UTC" path: zero is a PLAUSIBLE offset, because a UTC-stamped
    server is ordinary, which means a defaulted zero is an absent measurement
    wearing a measurement's clothes. That is the `SymbolSpec.unmeasured` rule
    applied to the clock, and the two are kept in the same shape on purpose.

    AN EXACT OFFSET WITH AN ERROR BAR IT DOES NOT CARRY. `offset_sec` is an
    int and `measured` is a bool, so nothing downstream can know that the
    instant derived from them is approximate, and the gates it feeds compare
    exactly. The error is the venue terminal's own drift from its timezone:
    bar stamps and `TimeCurrent()` carry that drift identically, so it cancels
    out of a raw difference, and the grid snap then removes it from the OFFSET
    while the bar stamp still carries it. The snap is therefore the only error
    source in the measured path, and the residual check caps the surviving
    error at the sample's uncertainty, which `2u < VENUE_CLOCK_GRID_SEC` holds
    strictly under half a grid step. Measured through a real engine: at the
    widest legal bound a 449s drift is absorbed and a 450s bound measures
    nothing at all, while on the desk's own path, where the bound is the
    measured poll gap, a 90s drift already refuses. A gate reading an instant
    from this is exact to within that, never more. See `docs/CONTRACT.md`.

    THREE WAYS TO GET ONE, and which one a caller may use is decided by what
    that caller can measure, not by what it would like to report:

    - `measure(...)`, for a caller that can BOUND how stale the venue's stamp
      is. The bound is a required argument precisely so that a caller without
      one cannot obtain a measured clock (the straightedge#182 review).
    - `implied(...)`, for a caller that cannot. It returns the offset the
      stamp WOULD imply if the stamp were current, as `implied_offset_sec`,
      on a clock that is UNMEASURED and says so. Nothing may convert with it.
    - `declared(...)`, for a venue that is not sampling a server at all. The
      paper venue stamps its own bars, so it states its offset rather than
      reading one, and no freshness question exists.
    """

    offset_sec: int | None
    #: The UTC epoch the sample was taken at. Zero means the venue answered
    #: from its own construction rather than from a reading.
    measured_at: int = 0
    #: Which venue and which reading. For the journal; never parsed.
    source: str = ""
    #: WIRE FIELD names that could not be measured, same shape and same rule
    #: as `SymbolSpec.unmeasured`. Empty means this is a measurement.
    unmeasured: frozenset[str] = frozenset()
    #: Why the measurement failed, in words, when `unmeasured` is non-empty.
    detail: str = ""
    #: The offset the venue's stamp would imply IF the stamp were current.
    #:
    #: Diagnostic only, and set only on an UNMEASURED clock. It exists so an
    #: operator can be shown something actionable ("the stamp implies UTC+3,
    #: and freshness is not established") instead of a bare refusal, and so
    #: that showing it cannot be confused with vouching for it: `offset_sec`
    #: is still None, `measured` is still False, and `to_utc` still raises.
    #: NEVER convert with this field.
    implied_offset_sec: int | None = None
    #: Whether this clock came from READING a server, as opposed to a venue
    #: STATING what it stamps with.
    #:
    #: It exists because `measured_at` was carrying this fact as a sentinel:
    #: `declared()` left it zero, and `Engine._bar_instant` gated its
    #: bar-disagreement check on `if clock.measured_at`, so a zero timestamp
    #: meant "nothing was sampled". That is two meanings on one field, which is
    #: what `__post_init__` below objects to for `offset_sec`, and the sentinel
    #: could not simply be swapped out because `measured_at` is ALSO an operand
    #: in that check (`measured_at + offset_sec`). So the gate reads this and
    #: the arithmetic reads `measured_at`, and the invariant below keeps the
    #: second one real wherever the first one is true (straightedge#193).
    sampled: bool = False

    def __post_init__(self) -> None:
        """A half-measured clock cannot be built at all.

        Two fields carry the same fact (`offset_sec is None`, and a non-empty
        `unmeasured`), and two fields carrying one fact is how a caller comes
        to check the wrong one. They are asserted consistent HERE, at
        construction, so that no call site has to. `implied_offset_sec` is held
        to the same rule from the other side: it may exist only where there is
        no measurement to confuse it with.
        """
        if (self.offset_sec is None) is not bool(self.unmeasured):
            raise ValueError(
                "VenueClock is inconsistent: offset_sec="
                f"{self.offset_sec!r} with unmeasured={sorted(self.unmeasured)!r}. "
                "An unmeasured clock has offset_sec None and names the field; a "
                "measured one has an offset and names nothing."
            )
        if self.implied_offset_sec is not None and self.offset_sec is not None:
            raise ValueError(
                "VenueClock carries both a measurement and an implication: "
                f"offset_sec={self.offset_sec!r}, "
                f"implied_offset_sec={self.implied_offset_sec!r}. An implication "
                "is what there is INSTEAD of a measurement, never beside one."
            )
        if self.sampled and not self.measured_at:
            # Not a new rule, the existing one applied to a third field. A
            # SAMPLED clock was paired with our clock at a real instant, and
            # `Engine._bar_instant` uses `measured_at` as an OPERAND, so a
            # sampled clock with a zero timestamp would compute an implied
            # server time of `0 + offset` and compare that against a bar. The
            # zero-check that used to gate it excluded this case by accident;
            # this states it instead.
            raise ValueError(
                "VenueClock is sampled but carries no measured_at: a sampled "
                "clock was paired with our clock at a real instant, and "
                f"measured_at={self.measured_at!r} cannot be one. A venue that "
                "STATES its offset is declared, not sampled."
            )

    @property
    def measured(self) -> bool:
        return not self.unmeasured

    def to_utc(self, venue_epoch: int) -> datetime:
        """The real UTC instant a venue timestamp names."""
        if self.offset_sec is None:
            raise VenueClockUnmeasured(
                "refusing to convert a venue timestamp: this venue's UTC "
                f"offset is not measured ({sorted(self.unmeasured)}, "
                f"source={self.source!r}, detail={self.detail!r})"
            )
        return datetime.fromtimestamp(int(venue_epoch) - self.offset_sec, tz=timezone.utc)

    @classmethod
    def not_measured(
        cls,
        *fields: str,
        source: str = "",
        measured_at: int = 0,
        detail: str = "",
        implied_offset_sec: int | None = None,
        sampled: bool = False,
    ) -> VenueClock:
        return cls(
            offset_sec=None,
            measured_at=measured_at,
            source=source,
            unmeasured=frozenset(fields or ("offset_sec",)),
            detail=detail,
            implied_offset_sec=implied_offset_sec,
            sampled=sampled,
        )

    @classmethod
    def declared(cls, offset_sec: int, *, source: str, measured_at: int = 0) -> VenueClock:
        """A venue that stamps its own bars, STATING the offset it stamps with.

        For the paper venue, which has no server to sample: its bars carry
        whatever the simulator or the backtest put there. No staleness exists,
        so no bound is needed and none is accepted.

        `sampled` stays FALSE, which is what keeps `Engine._bar_instant` from
        running its bar-disagreement check against a venue that never read a
        clock. Before straightedge#193 that exclusion rode on `measured_at`
        being zero here, which worked but said nothing.
        """
        return cls(offset_sec=int(offset_sec), measured_at=measured_at, source=source)

    @classmethod
    def implied(
        cls, venue_epoch: int, utc_epoch: float, *, source: str
    ) -> VenueClock:
        """What the stamp would imply, from a caller that cannot bound it.

        The venue stamp on every reply is `TimeCurrent()` on MT4 and
        `symbol_info_tick().time` on MT5, and both are the time of the LAST
        TICK rather than of now. The difference against our clock is therefore
        `offset - staleness`, and with no bound on staleness the two cannot be
        separated at all: 2h stale on a UTC+3 server is indistinguishable from
        a fresh UTC+1 server, and 11h stale is indistinguishable from UTC-8.
        Both were measured through the real adapter during the straightedge#182
        review, and the civil band rejected neither.

        So this returns an UNMEASURED clock that carries the implication for
        DISPLAY and refuses every conversion. A caller holding one may print
        it, next to the fact that freshness is not established; it may not act
        on it.

        THE CIVIL BAND APPLIES TO THE VALUE, NOT TO THE MEASUREMENT STATE, and
        that distinction is the whole of straightedge#193's second item. Past
        the band there is no offset to imply at all: UTC-17:00 and UTC-45:00
        are not timezones, and printing them as implications lost the one
        reading that separated "stale" from "absurd", which the pre-#182 gate
        used to give. So `implied_offset_sec` is left ABSENT there.
        What it must NOT do is refuse differently. `unmeasured` stays exactly
        `{"freshness"}`, because `__main__.venue_clock_check` keys its exit
        code on that set: returning `not_measured("offset_sec", ...)` here
        would send doctor down the NOT MEASURED branch and exit NON-ZERO past
        about 15h of staleness, which is a red `doctor` every weekend and the
        exact outcome straightedge#182 decided against. A correction applied
        through the wrong seam re-creates the thing it was correcting.
        """
        taken_at = int(utc_epoch)
        if int(venue_epoch) <= 0:
            return cls.not_measured(
                "server_time",
                source=source,
                measured_at=taken_at,
                detail="the venue sent no server timestamp",
                sampled=True,
            )
        raw = float(venue_epoch) - float(utc_epoch)
        snapped = int(round(raw / VENUE_CLOCK_GRID_SEC)) * VENUE_CLOCK_GRID_SEC
        in_band = VENUE_CLOCK_MIN_OFFSET_SEC <= snapped <= VENUE_CLOCK_MAX_OFFSET_SEC
        return cls.not_measured(
            "freshness",
            source=source,
            measured_at=taken_at,
            detail=(
                "the venue stamps its LAST TICK and this caller cannot bound "
                "how old that is, so offset and staleness cannot be separated"
            )
            if in_band
            else (
                f"the stamp is {abs(snapped)}s from our clock, outside the civil "
                "timezone band, so it implies no offset at all: the venue clock "
                "is frozen or wildly stale rather than merely unbounded"
            ),
            implied_offset_sec=snapped if in_band else None,
            sampled=True,
        )

    @classmethod
    def measure(
        cls,
        venue_epoch: int,
        utc_epoch: float,
        *,
        source: str,
        max_staleness_sec: float,
        round_trip_sec: float = 0.0,
    ) -> VenueClock:
        """One paired sample plus a BOUND on how stale the venue's half is.

        `max_staleness_sec` has no default on purpose. It is the precondition
        that makes a sample into a measurement, and a precondition that can be
        omitted is a precondition that will be inherited: the straightedge#182
        review found exactly that, a bound argued correctly for ONE of three
        callers and then written down unconditionally in three documents. A
        caller that cannot measure the bound cannot call this method, and uses
        `implied()` instead.

        WHAT THE BOUND MUST BE. The largest possible age of the venue's stamp
        at the moment it was read, MEASURED by the caller, not derived from a
        config value and not assumed. `Engine.step_symbol` measures it as the
        elapsed time since its own previous poll of that symbol: a bar
        advanced between those two polls, so a tick arrived in that interval,
        so the stamp cannot be older than it. A derived poll cadence is NOT
        acceptable here: this repo's own tick budget for a live MT4 config is
        270s of bounded part with an explicitly unbounded tail, which is why
        `watchdog.py` publishes `tick_gap_max_s` rather than trusting the
        budget. A number that the codebase already instruments because it can
        be false is not a bound.

        HOW IT IS USED. The sample's total uncertainty is the bound plus the
        round trip it was read across. Grid points are `VENUE_CLOCK_GRID_SEC`
        apart, so an interval narrower than one grid step can contain at most
        one of them, and the nearest is then the only offset consistent with
        the sample. The test applied here, `2 * uncertainty` under one grid
        step, is SUFFICIENT for that rather than necessary: it treats the
        uncertainty as two-sided when staleness is in fact one-sided, so it
        refuses some samples that could in principle be placed. That is
        deliberate, because the direction of the error is a refusal and a
        missed auto entry, never a wrong instant. The residual check then
        catches a venue that is not on the grid at all, or a bound that was
        violated by a non-multiple of the grid.

        WHAT NO VERSION OF THIS CAN DETECT, stated rather than implied: a
        stamp stale by an exact multiple of 900s lands on a grid point and
        looks perfect. The band does not see it either (11h is 44 grid steps).
        Nothing here detects that case, and nothing is claimed to: the only
        defence is that the bound is MEASURED, so on the engine's path a stamp
        that old is impossible rather than merely unlikely.
        """
        # Every return below is `sampled=True`: this method exists only for a
        # caller that READ a server, so even its refusals are refusals about a
        # reading that happened.
        taken_at = int(utc_epoch)
        if int(venue_epoch) <= 0:
            return cls.not_measured(
                "server_time",
                source=source,
                measured_at=taken_at,
                detail="the venue sent no server timestamp",
                sampled=True,
            )
        bound = max(0.0, float(max_staleness_sec))
        uncertainty = bound + max(0.0, float(round_trip_sec))
        if 2.0 * uncertainty >= VENUE_CLOCK_GRID_SEC:
            return cls.not_measured(
                "offset_sec",
                source=source,
                measured_at=taken_at,
                detail=(
                    f"uncertainty {uncertainty:.0f}s (staleness bound {bound:.0f}s "
                    f"plus round trip) is too wide for the {VENUE_CLOCK_GRID_SEC}s "
                    "grid, so more than one offset fits this sample"
                ),
                sampled=True,
            )
        raw = float(venue_epoch) - float(utc_epoch)
        snapped = int(round(raw / VENUE_CLOCK_GRID_SEC)) * VENUE_CLOCK_GRID_SEC
        if abs(raw - snapped) > uncertainty:
            return cls.not_measured(
                "offset_sec",
                source=source,
                measured_at=taken_at,
                detail=(
                    f"raw offset {raw:.0f}s is {abs(raw - snapped):.0f}s off the "
                    f"{VENUE_CLOCK_GRID_SEC}s grid, wider than the {uncertainty:.0f}s "
                    "this sample allows, so it is not an offset"
                ),
                sampled=True,
            )
        if not VENUE_CLOCK_MIN_OFFSET_SEC <= snapped <= VENUE_CLOCK_MAX_OFFSET_SEC:
            return cls.not_measured(
                "offset_sec",
                source=source,
                measured_at=taken_at,
                detail=(
                    f"offset {snapped}s is outside the civil timezone band, so the "
                    "venue clock is frozen or stale rather than offset"
                ),
                sampled=True,
            )
        return cls(
            offset_sec=snapped, measured_at=taken_at, source=source, sampled=True
        )


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
