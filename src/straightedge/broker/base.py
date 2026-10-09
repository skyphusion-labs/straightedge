from __future__ import annotations

from typing import Protocol

from straightedge.models import (
    Account,
    Bar,
    MarketOrder,
    OrderResult,
    PendingOrder,
    Position,
    SymbolSpec,
    Tick,
    VenueClock,
    WorkingOrder,
)


class Broker(Protocol):
    """Venue-neutral execution API. Paper, MT5, and MT4 implement this.

    Engine, desk, and risk never send MT5 request dicts. A new venue
    implements these methods. close_by may return unsupported.
    """

    def connect(self) -> None: ...
    def disconnect(self) -> None: ...
    def account(self) -> Account: ...
    def symbol(self, name: str) -> SymbolSpec: ...
    def tick(self, name: str) -> Tick: ...
    def rates(self, name: str, timeframe: str, count: int) -> list[Bar]: ...
    def venue_clock(
        self, name: str, *, max_staleness_sec: float | None
    ) -> VenueClock: ...
    def positions(self, magic: int | None = None) -> list[Position]: ...
    def orders(self, magic: int | None = None) -> list[PendingOrder]: ...
    def select_symbol(self, name: str) -> bool: ...
    def check_market(self, order: MarketOrder) -> OrderResult: ...
    def market(self, order: MarketOrder) -> OrderResult: ...
    def check_working(self, order: WorkingOrder) -> OrderResult: ...
    def working(self, order: WorkingOrder) -> OrderResult: ...
    def modify_position(self, ticket: int, sl: float, tp: float, symbol: str = "") -> OrderResult: ...
    def modify_working(
        self,
        ticket: int,
        *,
        price: float | None = None,
        sl: float | None = None,
        tp: float | None = None,
        symbol: str = "",
        volume: float = 0.0,
        side: str = "",
        kind: str = "",
    ) -> OrderResult: ...
    def cancel(self, ticket: int) -> OrderResult: ...
    def close_position(
        self,
        ticket: int,
        *,
        symbol: str,
        side: str,
        volume: float,
        price: float,
        comment: str = "",
        magic: int = 0,
        deviation: int = 20,
    ) -> OrderResult: ...
    def close_by(self, ticket: int, other: int, symbol: str = "") -> OrderResult: ...


#: What `venue_clock_of` reports when the venue has no such method at all.
VENUE_CLOCK_ABSENT = "venue_clock"


def venue_clock_of(
    broker: object, name: str, *, max_staleness_sec: float | None
) -> VenueClock:
    """The venue's clock, with the caller's own bound on the sample's staleness.

    `max_staleness_sec` is REQUIRED and has no default, and `None` is a
    deliberate value rather than an omission: it means this caller cannot
    measure how old the venue's stamp is. A venue that SAMPLES a server then
    answers with an implication that refuses every conversion
    (`VenueClock.implied`), and a venue that stamps its own bars still answers
    with a measurement (`VenueClock.declared`), because it has no staleness to
    bound. The straightedge#182 review is why the argument exists at all: a
    bound that lives in a docstring gets inherited by the next caller, and one
    of the three callers here genuinely has no bound to give.

    Read through `getattr` rather than called directly, for the same reason
    `startup_connect` and `history_probe` are (see `Engine.start` and
    `Mt4Broker.history_probe`): a venue that predates the method still has to
    get an answer, and the only safe answer is NOT MEASURED. Reading an absent
    method as "this venue stamps UTC" is the straightedge#172 defect itself,
    just relocated into the adapter layer.

    The Protocol above declares the method, so a real adapter that forgets it
    is a typecheck failure as well; this is the runtime half of the same rule.
    """
    ask = getattr(broker, "venue_clock", None)
    if not callable(ask):
        return VenueClock.not_measured(
            VENUE_CLOCK_ABSENT,
            source=type(broker).__name__,
            detail="this venue cannot state its UTC offset",
        )
    return ask(name, max_staleness_sec=max_staleness_sec)
