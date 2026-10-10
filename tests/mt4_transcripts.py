"""Golden transcripts of the real MT4 wire.

Every byte in this module is derived from `mt4/Experts/Mt4RiskBot.mq4`, which is
the other end of the contract and therefore the authority on what actually
reaches the adapter. Nothing here returns a native Python dict: the adapter is
driven through `FileBridge`, so `encode`, `decode`, `parse_rows` and
`_split_row` all execute on real pipe-separated text.

Why this exists. `tests/test_mt4_adapter.py` drives the adapter through
`FakeMt4.call`, which hands back dicts. That takes the fast path in
`parse_rows` (`mt4_live.py:140-141`) and the wire decoding is never reached
through the broker at all. It also hardcodes `tick_value: 1.0`, the exact value
`mt4_live.py:286` substitutes when the measurement is zero, so a dict stub
cannot tell a measured 1.0 from a defaulted one.

MEASURED versus DEFAULTED is the distinction this module exists to express.
A `Transcript` carries the exact `.res` text, so `keys_sent()` answers "did the
Expert put this field on the wire at all?" and `value_sent()` answers "what
string did it put there?". A field the Expert never emits is *absent*, and the
value the adapter reports for it is its own default, not a measurement. Tests
assert that partition explicitly instead of trusting a stub to reproduce it.

Expert line citations are to `mt4/Experts/Mt4RiskBot.mq4` at 69a8219, and they
are pinned to that sha rather than maintained. One handler has changed shape
since: `RatesReply` now selects the symbol, touches the series to make the
terminal fetch it, and emits `bars_total`, `selected` and `history_error`
alongside the bars (#33). Read that handler by name, not by the line numbers
below.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from straightedge.broker.mt4_live import REQ_NAME, RES_NAME

# Every op `Handle()` answers, in source order (Mt4RiskBot.mq4:237-271).
# Seventeen, not sixteen: `docs/MT4.md:88-103` describes them in fourteen table
# rows because three rows carry two ops each.
EA_OPS = (
    "ping",
    "account",
    "tick",
    "symbol",
    "select",
    "rates",
    "positions",
    "orders",
    "check_market",
    "market",
    "check_working",
    "working",
    "modify_position",
    "modify_working",
    "cancel",
    "close",
    "close_by",
)

# Pipe-join order of each row-bearing reply, read off the Expert's emitters.
# These are the Expert's order, not the adapter's tuples; a test asserts the two
# agree, which is the check a dict-returning stub cannot make because a dict is
# indifferent to order.
EA_BAR_EMIT = ("time", "open", "high", "low", "close", "volume")  # :351-356
EA_POS_EMIT = (  # :397-409
    "ticket",
    "symbol",
    "side",
    "volume",
    "price_open",
    "sl",
    "tp",
    "price_current",
    "profit",
    "magic",
    "comment",
    "swap",
    "time",
)
EA_ORD_EMIT = (  # :383-393
    "ticket",
    "symbol",
    "side",
    "kind",
    "volume",
    "price",
    "sl",
    "tp",
    "magic",
    "comment",
    "time",
)

# Keys `AccountReply` puts on the wire, in emit order (:279-292).
EA_ACCOUNT_KEYS = (
    "login",
    "balance",
    "equity",
    "margin",
    "margin_free",
    "profit",
    "currency",
    "leverage",
    "trade_mode",
    "trade_allowed",
    "trade_expert",
    "name",
    "server",
)

# Keys `SymbolReply` puts on the wire, in emit order (:313-324).
EA_SYMBOL_KEYS = (
    "digits",
    "point",
    "volume_min",
    "volume_max",
    "volume_step",
    "tick_value",
    "tick_size",
    "contract_size",
    "stops_level",
    "freeze_level",
    "spread",
)


def ea_kv(body: str, key: str) -> str:
    """Faithful port of the Expert's `KV()` (Mt4RiskBot.mq4:63-81).

    Deliberately not `mt4_live.decode`. The adapter's encoder has to be judged
    by the reader that actually consumes it, not by the adapter's own decoder,
    which would only restate the Python-side assumption. Prefix match, leading
    and trailing CR stripped, empty string when the key is absent.
    """
    prefix = key + "="
    for raw in body.split("\n"):
        line = raw
        if line.startswith("\r"):
            line = line[1:]
        if line.endswith("\r"):
            line = line[:-1]
        if line.find(prefix) == 0:
            return line[len(prefix) :]
    return ""


def ea_ok(*lines: str) -> str:
    """`Ok(id)` plus trailing fields (Mt4RiskBot.mq4:83-86)."""
    body = "id={id}\nok=1\n"
    return body + "".join(line + "\n" for line in lines)


def ea_fail(err: int, msg: str) -> str:
    """`Fail(id, err, msg)` (Mt4RiskBot.mq4:88-91).

    Note what is absent: no `op`. The adapter's decoder already lists `op` in
    `_TEXT_FIELDS` (`mt4_live.py:90`) but no Expert reply emits it, so the id is
    the only thing a reply can be matched on.
    """
    return "id={id}\nok=0\nretcode=" + str(err) + "\nerror=" + msg + "\n"


def ea_row(order: tuple[str, ...], values: dict[str, object]) -> str:
    """Join one row the way the Expert does: `|` between fields, no sanitation.

    `_wire()` (`mt4_live.py:93-95`) strips pipes on the way out. The Expert does
    not do the same on the way back for `OrderComment()` (:392, :407),
    `AccountName()` or `AccountServer()` (:291-292), so a pipe arriving from the
    terminal is joined straight into the row.
    """
    return "|".join(str(values[name]) for name in order)


def ea_rows_reply(order: tuple[str, ...], rows: list[dict[str, object]]) -> str:
    """`BookReply` shape: `n=` then `row0=`.. (:411)."""
    lines = ["n=" + str(len(rows))]
    for i, row in enumerate(rows):
        lines.append("row" + str(i) + "=" + ea_row(order, row))
    return ea_ok(*lines)


#: The history-state fields `RatesReply` emits alongside the bars, in emit
#: order. They are on EVERY rates reply, the healthy one included, because a
#: field that appears only on failure is indistinguishable from an Expert too
#: old to emit it at all -- the partition `survivor_ticket` exists for.
EA_RATES_STATE = ("bars_total", "selected", "history_error")


def ea_rates_reply(
    rows: list[dict[str, object]],
    *,
    bars_total: int | None = None,
    selected: bool = True,
    history_error: int = 0,
    state: bool = True,
) -> str:
    """`RatesReply` shape: `n=`, the three state fields, then `row0=`..

    `state=False` reproduces the Expert as it shipped before #33, which emitted
    `n=` and the rows and said nothing about the series. That version is still
    attachable in a terminal, so the adapter has to read it as "did not answer"
    rather than as zeros, and only a transcript without the fields can prove it
    does.
    """
    total = len(rows) if bars_total is None else bars_total
    lines = ["n=" + str(len(rows))]
    if state:
        lines.append("bars_total=" + str(total))
        lines.append("selected=" + ("1" if selected else "0"))
        lines.append("history_error=" + str(history_error))
    for i, row in enumerate(rows):
        lines.append("row" + str(i) + "=" + ea_row(EA_BAR_EMIT, row))
    return ea_ok(*lines)


def d(value: float, digits: int) -> str:
    """`DoubleToString(value, digits)`: fixed decimals, no exponent."""
    return f"{value:.{digits}f}"


@dataclass(frozen=True)
class Transcript:
    """One recorded request/response pair on the real wire.

    `response` is the `.res` text exactly as the Expert writes it, with `{id}`
    standing in for the echoed request id. `note` records which Expert lines it
    came from.
    """

    op: str
    response: str
    note: str = ""

    def render(self, req_id: int) -> str:
        return self.response.replace("{id}", str(req_id))

    def keys_sent(self) -> set[str]:
        """The keys the Expert actually put on the wire.

        This is what makes "the Expert sent nothing for this field" expressible.
        A key missing from this set is one the adapter can only default.
        """
        out: set[str] = set()
        for raw in self.render(1).split("\n"):
            line = raw.strip()
            if not line or "=" not in line:
                continue
            out.add(line.split("=", 1)[0])
        return out

    def value_sent(self, key: str) -> str | None:
        """The raw string emitted for `key`, or None when it was not emitted.

        Raw on purpose. `"0.0000"` and `"1.0000"` are different measurements;
        both become a float the adapter may then replace with its own default,
        and only the wire string can tell you which happened.
        """
        for raw in self.render(1).split("\n"):
            line = raw.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            if k == key:
                return v
        return None


# --------------------------------------------------------------------------
# One transcript per op. Values are plausible broker measurements, formatted
# the way the Expert formats them.
# --------------------------------------------------------------------------

DIGITS_EURUSD = 5


def t_ping(now: int = 1758700000) -> Transcript:
    return Transcript("ping", ea_ok("time=" + str(now)), "Mt4RiskBot.mq4:238")


def t_account(
    *,
    login: int = 51234567,
    balance: float = 10000.0,
    equity: float = 9987.5,
    margin: float = 212.34,
    margin_free: float = 9775.16,
    profit: float = -12.5,
    currency: str = "USD",
    leverage: int = 100,
    trade_mode: int = 0,
    trade_allowed: int = 1,
    trade_expert: int = 1,
    name: str = "Gil Test",
    server: str = "Broker-Demo01",
) -> Transcript:
    return Transcript(
        "account",
        ea_ok(
            "login=" + str(login),
            "balance=" + d(balance, 2),
            "equity=" + d(equity, 2),
            "margin=" + d(margin, 2),
            "margin_free=" + d(margin_free, 2),
            "profit=" + d(profit, 2),
            "currency=" + currency,
            "leverage=" + str(leverage),
            "trade_mode=" + str(trade_mode),
            "trade_allowed=" + str(trade_allowed),
            "trade_expert=" + str(trade_expert),
            "name=" + name,
            "server=" + server,
        ),
        "Mt4RiskBot.mq4:274-293",
    )


def t_symbol(
    *,
    digits: int = DIGITS_EURUSD,
    point: float = 0.00001,
    volume_min: float = 0.01,
    volume_max: float = 500.0,
    volume_step: float = 0.01,
    tick_value: str = "1.00000000",
    tick_size: float = 0.00001,
    contract_size: float = 100000.0,
    stops_level: int = 10,
    freeze_level: int = 0,
    spread: int = 12,
) -> Transcript:
    """`SymbolReply` (:307-325).

    `tick_value` is a raw string on purpose: the exact bytes are the evidence.

    The Expert emitted `DoubleToString(..., 4)` until 1.3.2, so four decimals
    was the whole resolution the adapter ever saw and a real measurement below
    0.00005 arrived as `"0.0000"`. It now emits 8, which removes TRUNCATION as
    a producer of zero. It does not remove zero itself: `MarketInfo` still
    answers 0 for a symbol that is not in Market Watch, and that zero is now a
    refusal rather than a substituted 1.0.
    """
    return Transcript(
        "symbol",
        ea_ok(
            "digits=" + str(digits),
            "point=" + d(point, digits),
            "volume_min=" + d(volume_min, 8),
            "volume_max=" + d(volume_max, 8),
            "volume_step=" + d(volume_step, 8),
            "tick_value=" + tick_value,
            "tick_size=" + d(tick_size, digits),
            "contract_size=" + d(contract_size, 0),
            "stops_level=" + str(stops_level),
            "freeze_level=" + str(freeze_level),
            "spread=" + str(spread),
        ),
        "Mt4RiskBot.mq4:307-325",
    )


def t_tick(
    *, bid: float = 1.10012, ask: float = 1.10024, now: int = 1758700000, digits: int = DIGITS_EURUSD
) -> Transcript:
    return Transcript(
        "tick",
        ea_ok("bid=" + d(bid, digits), "ask=" + d(ask, digits), "time=" + str(now)),
        "Mt4RiskBot.mq4:295-305",
    )


def t_select(ok: bool = True) -> Transcript:
    if not ok:
        return Transcript("select", ea_fail(1, "select"), "Mt4RiskBot.mq4:332")
    return Transcript("select", ea_ok(), "Mt4RiskBot.mq4:333")


BARS_EURUSD_H1 = [
    {
        "time": 1758693600,
        "open": "1.10010",
        "high": "1.10180",
        "low": "1.09960",
        "close": "1.10120",
        "volume": 8412,
    },
    {
        "time": 1758697200,
        "open": "1.10120",
        "high": "1.10240",
        "low": "1.10080",
        "close": "1.10200",
        "volume": 7733,
    },
]


def bar_rows(
    count: int,
    *,
    start: int = 1758693600,
    step: int = 3600,
    base: float = 1.10000,
    digits: int = DIGITS_EURUSD,
) -> list[dict[str, object]]:
    """`count` plausible H1 rows, formatted the way the Expert formats them.

    Each bar has a real range and a real gap from the previous close, so Wilder
    ATR over them is finite and non-zero. A flat series would make "ATR could
    not be computed" and "ATR is zero" the same observation, and those are the
    two things this module is here to keep apart.
    """
    out: list[dict[str, object]] = []
    price = base
    for i in range(count):
        close = price + 0.00040
        out.append(
            {
                "time": start + i * step,
                "open": d(price, digits),
                "high": d(price + 0.00120, digits),
                "low": d(price - 0.00090, digits),
                "close": d(close, digits),
                "volume": 5000 + i,
            }
        )
        price = close
    return out


def t_rates(
    rows: list[dict[str, object]] | None = None,
    *,
    bars_total: int | None = None,
    selected: bool = True,
    history_error: int = 0,
    state: bool = True,
) -> Transcript:
    return Transcript(
        "rates",
        ea_rates_reply(
            BARS_EURUSD_H1 if rows is None else rows,
            bars_total=bars_total,
            selected=selected,
            history_error=history_error,
            state=state,
        ),
        "Mt4RiskBot.mq4 RatesReply",
    )


def t_rates_cold(*, history_error: int = 0, selected: bool = True) -> Transcript:
    """The defect's own shape: `ok=1`, `n=0`, no rows.

    This is what the live rig answered for EURUSD and USDJPY while XAUUSD, the
    one charted symbol, answered 200 bars. Before #33 it was the whole reply, so
    `ok=1 n=0` meant "downloading now", "not served under this name" and
    "genuinely empty" at once, and the desk could only read the reassuring one.
    """
    return t_rates([], bars_total=0, selected=selected, history_error=history_error)


def pos_row(
    *,
    ticket: int = 80051234,
    symbol: str = "EURUSD",
    side: str = "buy",
    volume: float = 0.17,
    price_open: float = 1.10015,
    sl: float = 1.09815,
    tp: float = 1.10415,
    price_current: float = 1.10120,
    profit: float = 17.85,
    magic: int = 770077,
    comment: str = "rb-1",
    swap: float = -0.42,
    time_: int = 1758694000,
    digits: int = DIGITS_EURUSD,
) -> dict[str, object]:
    """One `BookReply` position row, formatted as the Expert formats it (:397-409)."""
    return {
        "ticket": ticket,
        "symbol": symbol,
        "side": side,
        "volume": d(volume, 2),
        "price_open": d(price_open, digits),
        "sl": d(sl, digits),
        "tp": d(tp, digits),
        "price_current": d(price_current, digits),
        "profit": d(profit, 2),
        "magic": magic,
        "comment": comment,
        "swap": d(swap, 2),
        "time": time_,
    }


def ord_row(
    *,
    ticket: int = 80051299,
    symbol: str = "EURUSD",
    side: str = "buy",
    kind: str = "limit",
    volume: float = 0.09,
    price: float = 1.09500,
    sl: float = 1.09300,
    tp: float = 1.09900,
    magic: int = 770077,
    comment: str = "rb-2",
    time_: int = 1758695000,
    digits: int = DIGITS_EURUSD,
) -> dict[str, object]:
    """One `BookReply` pending row (:383-393)."""
    return {
        "ticket": ticket,
        "symbol": symbol,
        "side": side,
        "kind": kind,
        "volume": d(volume, 2),
        "price": d(price, digits),
        "sl": d(sl, digits),
        "tp": d(tp, digits),
        "magic": magic,
        "comment": comment,
        "time": time_,
    }


def t_positions(rows: list[dict[str, object]] | None = None) -> Transcript:
    return Transcript(
        "positions",
        ea_rows_reply(EA_POS_EMIT, [pos_row()] if rows is None else rows),
        "Mt4RiskBot.mq4:363-415 (pending=false)",
    )


def t_orders(rows: list[dict[str, object]] | None = None) -> Transcript:
    return Transcript(
        "orders",
        ea_rows_reply(EA_ORD_EMIT, [ord_row()] if rows is None else rows),
        "Mt4RiskBot.mq4:363-415 (pending=true)",
    )


def t_check_market(price: float = 1.10024, digits: int = DIGITS_EURUSD) -> Transcript:
    return Transcript(
        "check_market",
        ea_ok("ticket=0", "price=" + d(price, digits)),
        "Mt4RiskBot.mq4:440 (send=false)",
    )


def t_market(
    *, ticket: int = 80051234, volume: float = 0.17, price: float = 1.10024, digits: int = DIGITS_EURUSD
) -> Transcript:
    return Transcript(
        "market",
        ea_ok("ticket=" + str(ticket), "volume=" + d(volume, 2), "price=" + d(price, digits)),
        "Mt4RiskBot.mq4:459-462 (send=true)",
    )


def t_market_ticket_only(ticket: int = 80051234) -> Transcript:
    """The order went on, then `OrderSelect` failed, so volume and price are absent (:456-457)."""
    return Transcript("market", ea_ok("ticket=" + str(ticket)), "Mt4RiskBot.mq4:456-457")


def t_check_working() -> Transcript:
    return Transcript("check_working", ea_ok("ticket=0"), "Mt4RiskBot.mq4:486 (send=false)")


def t_working(
    *, ticket: int = 80051299, price: float = 1.09500, digits: int = DIGITS_EURUSD
) -> Transcript:
    return Transcript(
        "working",
        ea_ok("ticket=" + str(ticket), "price=" + d(price, digits)),
        "Mt4RiskBot.mq4:498 (send=true)",
    )


def t_modify_position(ticket: int = 80051234) -> Transcript:
    return Transcript("modify_position", ea_ok("ticket=" + str(ticket)), "Mt4RiskBot.mq4:512")


def t_modify_working(ticket: int = 80051299) -> Transcript:
    return Transcript("modify_working", ea_ok("ticket=" + str(ticket)), "Mt4RiskBot.mq4:533")


def t_cancel(ticket: int = 80051299) -> Transcript:
    return Transcript("cancel", ea_ok("ticket=" + str(ticket)), "Mt4RiskBot.mq4:545")


def t_close(ticket: int = 80051234, volume: float = 0.17) -> Transcript:
    return Transcript(
        "close",
        ea_ok("ticket=" + str(ticket), "volume=" + d(volume, 2)),
        "Mt4RiskBot.mq4:567",
    )


def t_close_by(ticket: int = 80051234) -> Transcript:
    return Transcript("close_by", ea_ok("ticket=" + str(ticket)), "Mt4RiskBot.mq4:576")


def t_unsupported() -> Transcript:
    return Transcript("nosuchop", ea_fail(1, "unsupported"), "Mt4RiskBot.mq4:271")


#: One healthy transcript per op, so a test can walk the whole surface.
GOLDEN: dict[str, Transcript] = {
    "ping": t_ping(),
    "account": t_account(),
    "tick": t_tick(),
    "symbol": t_symbol(),
    "select": t_select(),
    "rates": t_rates(),
    "positions": t_positions(),
    "orders": t_orders(),
    "check_market": t_check_market(),
    "market": t_market(),
    "check_working": t_check_working(),
    "working": t_working(),
    "modify_position": t_modify_position(),
    "modify_working": t_modify_working(),
    "cancel": t_cancel(),
    "close": t_close(),
    "close_by": t_close_by(),
}

#: Every `Fail()` an Expert op can return, with the MT4 error code it carries.
#: Keyed by the Expert's own message string so the citation stays checkable.
EA_FAILURES: dict[str, int] = {
    "symbol": 1,  # :298, :310, :330, :339, :428, :476
    "select": 1,  # :332 (SelectReply) and GetLastError at :447
    "trade_disabled": 133,  # :430, :478
    "invalid_volume": 131,  # :433, :481
    "invalid_stops": 130,  # :438, :484
    "sl_modify_failed": 130,  # :453, :495
    "not_found": 4108,  # :507, :519, :540, :556
    "not_position": 1,  # :509, :558
    "not_pending": 1,  # :521, :542
    "unsupported": 1,  # :271
}


@dataclass
class TranscriptExpert:
    """A stand-in for the Expert's `Process()` loop (Mt4RiskBot.mq4:31-61).

    Same mechanics as the real thing: notice `.req`, read it, delete it, answer
    from the transcript, write `.res.tmp` and rename it over `.res`. It parses
    the request with `ea_kv`, the Expert's own reader, so the adapter's encoder
    is exercised against the real consumer.

    It records every request body it saw. It never asserts; tests do that.
    """

    directory: Path
    transcripts: dict[str, Transcript]
    #: Answer with this id instead of the one that was asked, to drive the
    #: adapter's id-match branch (`mt4_live.py:232->236`).
    force_id: int | None = None
    #: Stop answering after this many requests, to drive the timeout path.
    answer_limit: int | None = None
    #: Choose the transcript from the request body instead of the op alone.
    #: MT4 history is per symbol AND timeframe, so a fixture that can only vary
    #: by op cannot express "EURUSD has none and XAUUSD has 200" -- which is the
    #: exact state that was measured on the rig, and a test that cannot express
    #: it would pass on a desk that treated every symbol the same.
    route: Any = None
    seen: list[str] = field(default_factory=list)
    ops: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    _stop: threading.Event = field(default_factory=threading.Event)
    _thread: threading.Thread | None = None
    #: The last request body RECORDED. A delete that does not take leaves the
    #: request on the shared name, and the next poll re-reads it; this is what
    #: stops that becoming a phantom second request (#254, #258).
    _last_body: str = ""

    def __enter__(self) -> TranscriptExpert:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        if exc_type is None and self.errors:
            # A dead stand-in Expert otherwise surfaces as a bridge timeout,
            # which is the wrong diagnosis for the wrong file.
            raise AssertionError("stand-in Expert failed: " + self.errors[0])

    def request(self, op: str) -> str:
        """The raw request body recorded for `op`, newest last."""
        for body, seen_op in zip(reversed(self.seen), reversed(self.ops)):
            if seen_op == op:
                return body
        raise AssertionError(f"no request recorded for op {op!r}; saw {self.ops}")

    def _loop(self) -> None:
        req = self.directory / REQ_NAME
        res = self.directory / RES_NAME
        tmp = self.directory / (RES_NAME + ".tmp")
        while not self._stop.is_set():
            if req.exists():
                try:
                    body = req.read_text(encoding="utf-8")
                except OSError:
                    time.sleep(0.005)
                    continue
                op = ea_kv(body, "op")
                req_id = ea_kv(body, "id")
                # RECORD BEFORE DELETING (#254). This used to sit after the
                # `unlink`, so `seen` meant "requests I both read and deleted"
                # while every caller reads it as "requests the Expert saw". One
                # unlink denial recovered, because the file was still there and
                # the next poll re-read it; a denial that persisted through the
                # budget lost the request entirely and surfaced as a request
                # that never arrived. `ERROR_ACCESS_DENIED` on exactly this
                # operation is what #242 measured on the live Windows box.
                if body == self._last_body:
                    # The same request file, re-read because the delete did not
                    # take. It is already recorded, and recording it twice
                    # would turn a delete failure into a phantom SECOND
                    # request, which is the opposite error from the one the
                    # record-before-delete fix removes. Keyed on the body
                    # rather than the id, so a genuinely new request (which
                    # always carries a new id, and therefore a different body)
                    # is never suppressed.
                    time.sleep(0.005)
                    continue
                self.seen.append(body)
                self.ops.append(op)
                self._last_body = body
                try:
                    req.unlink()
                except OSError:
                    # Already recorded, so a delete failure is now visible AS a
                    # delete failure: the request stays on the shared name and
                    # the guard above stops the re-read double-counting it.
                    pass
                if self.answer_limit is not None and len(self.seen) > self.answer_limit:
                    continue
                routed = self.route(op, body) if callable(self.route) else None
                transcript = (
                    routed if routed is not None else self.transcripts.get(op, t_unsupported())
                )
                answer_id = req_id if self.force_id is None else str(self.force_id)
                try:
                    self._publish(tmp, res, transcript.render(int(answer_id)))
                except OSError as exc:
                    self.errors.append(f"{op}: {type(exc).__name__}: {exc}")
                    return
            time.sleep(0.005)

    def _publish(self, tmp: Path, res: Path, text: str) -> None:
        """Write the reply the way the Expert does, and retry the way NTFS needs.

        The real Expert writes `mt4_risk_bot.res.tmp`, deletes `.res` and renames
        (`Mt4RiskBot.mq4:51-58`). The adapter retries `unlink` and `replace` on
        PermissionError because Windows refuses both while the other side holds a
        handle (`mt4_live.py:172-193`). A stand-in without the same patience is a
        source of flake on the windows-latest leg, not a test of anything.
        """
        deadline = time.monotonic() + 5.0
        while True:
            try:
                tmp.write_text(text, encoding="utf-8", newline="\n")
                os.replace(tmp, res)
                return
            except PermissionError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(0.01)
