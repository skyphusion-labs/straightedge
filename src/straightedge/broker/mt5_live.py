"""Adapter over the official MetaTrader5 Python package (Windows) or mt5-mac.

The official package is a Windows-only IPC client against a running terminal.
On macOS, mt5-mac speaks the same function names through Wine inside
MetaTrader 5.app. This adapter accepts either.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from straightedge.constants import (
    FILLING_RETRY_ORDER,
    IDEMPOTENT_TRADE_ACTIONS,
    ORDER_TIME_GTC,
    ORDER_TYPE_BUY,
    ORDER_TYPE_SELL,
    ORDER_TYPE_BUY_LIMIT,
    ORDER_TYPE_BUY_STOP,
    ORDER_TYPE_BUY_STOP_LIMIT,
    ORDER_TYPE_SELL_LIMIT,
    ORDER_TYPE_SELL_STOP,
    RETCODE_OK,
    TRADE_ACTION_CLOSE_BY,
    TRADE_ACTION_DEAL,
    TRADE_ACTION_MODIFY,
    TRADE_ACTION_PENDING,
    TRADE_ACTION_REMOVE,
    TRADE_ACTION_SLTP,
    TRADE_RETCODE_INVALID_FILL,
    choose_filling,
    timeframe_code,
)
from straightedge.models import (
    Account,
    Bar,
    MarketOrder,
    OrderResult,
    PendingOrder,
    Position,
    Side,
    SymbolSpec,
    Tick,
    VenueClock,
    WorkingOrder,
)


#: Named on the measurement so a journal line says WHICH reading produced it.
MT5_CLOCK_SOURCE = "mt5 symbol_info_tick"


def _utc_epoch() -> float:
    """The desk's clock, as a UTC epoch. The one default `now_fn`."""
    return datetime.now(timezone.utc).timestamp()


def load_mt5_module() -> Any:
    try:
        import MetaTrader5 as mt5  # type: ignore

        return mt5
    except ImportError:
        pass
    try:
        import mt5_mac as mt5  # type: ignore

        return mt5
    except ImportError as exc:
        raise RuntimeError(
            "No MT5 Python binding. On Windows: pip install MetaTrader5. "
            "On macOS: brew-installed Python plus `pip install mt5-mac`, and "
            "MetaTrader 5.app from metatrader5.com (not Homebrew; MetaQuotes "
            "does not ship a cask). Paper mode needs neither."
        ) from exc


def _asdict(obj: Any) -> dict:
    if obj is None:
        return {}
    if hasattr(obj, "_asdict"):
        return obj._asdict()
    return dict(getattr(obj, "__dict__", {}) or {})


def _deal_type(side: Side) -> int:
    return ORDER_TYPE_BUY if side is Side.BUY else ORDER_TYPE_SELL


def _order_kind(type_code: int) -> str:
    if type_code in (ORDER_TYPE_BUY_LIMIT, ORDER_TYPE_SELL_LIMIT):
        return "limit"
    if type_code in (ORDER_TYPE_BUY_STOP, ORDER_TYPE_SELL_STOP):
        return "stop"
    return ""


class Mt5Broker:
    #: This venue can answer "no bars" now and serve them a moment later,
    #: because the terminal fetches history from the broker in the background.
    #: `history.preflight` reads this to decide whether a bounded wait could
    #: change the answer. A venue that answers from memory must NOT set it: a
    #: wait there is pure latency, and `run_backtest` seeds one bar on purpose
    #: and streams the rest.
    history_async = True

    def __init__(
        self,
        *,
        login: int = 0,
        password: str = "",
        server: str = "",
        path: str = "",
        timeout_ms: int = 60_000,
        mt5: Any | None = None,
        now_fn: Any | None = None,
    ) -> None:
        self._login = login
        self._password = password
        self._server = server
        self._path = path
        self._timeout = timeout_ms
        self._mt5 = mt5
        #: The DESK's clock, in UTC epoch seconds. `venue_clock` measures
        #: the server's offset by pairing the venue's own stamp with this,
        #: so it is injectable: a clock test that cannot hold one side of a
        #: difference still is not measuring a difference.
        self._now = now_fn if now_fn is not None else _utc_epoch

    def connect(self) -> None:
        mt5 = self._mt5 or load_mt5_module()
        self._mt5 = mt5
        # initialize is not reentrant; drop the old IPC handle first
        if hasattr(mt5, "shutdown"):
            try:
                mt5.shutdown()
            except (RuntimeError, OSError, AttributeError, ValueError):
                pass
        kwargs: dict[str, Any] = {"timeout": self._timeout}
        if self._path:
            # initialize(path, login=..., ...) path is the unnamed first arg
            ok = mt5.initialize(
                self._path,
                login=self._login or None,
                password=self._password or None,
                server=self._server or None,
                timeout=self._timeout,
            )
        else:
            if self._login:
                kwargs["login"] = self._login
            if self._password:
                kwargs["password"] = self._password
            if self._server:
                kwargs["server"] = self._server
            ok = mt5.initialize(**{k: v for k, v in kwargs.items() if v is not None})
        if not ok:
            err = mt5.last_error() if hasattr(mt5, "last_error") else "unknown"
            raise RuntimeError(f"mt5.initialize failed: {err}")
        if self._login and hasattr(mt5, "login"):
            if not mt5.login(self._login, password=self._password, server=self._server):
                err = mt5.last_error() if hasattr(mt5, "last_error") else "unknown"
                raise RuntimeError(f"mt5.login failed: {err}")
        info = mt5.terminal_info()
        if info is not None:
            d = _asdict(info)
            if d.get("trade_allowed") is False:
                raise RuntimeError("terminal trade_allowed is False; enable AutoTrading")

    def disconnect(self) -> None:
        if self._mt5 is not None:
            self._mt5.shutdown()

    def ensure_connected(self) -> None:
        mt5 = self._mt5
        if mt5 is not None:
            try:
                info = mt5.account_info() if hasattr(mt5, "account_info") else None
            except (RuntimeError, OSError, AttributeError, ValueError):
                info = None
            if info is not None and not self._ipc_error():
                return
        self.connect()

    def _last_error(self) -> Any:
        mt5 = self._mt5
        if mt5 is None or not hasattr(mt5, "last_error"):
            return "unknown"
        try:
            return mt5.last_error()
        except (RuntimeError, OSError, AttributeError, ValueError):
            return "unknown"

    def _ipc_error(self) -> bool:
        err = self._last_error()
        if err is None or err == "unknown":
            return False
        code: Any = err
        desc = ""
        if isinstance(err, (tuple, list)):
            if not err:
                return False
            code = err[0]
            if len(err) > 1 and err[1] is not None:
                desc = str(err[1])
        elif isinstance(err, str):
            desc = err
            code = 0
        try:
            code_i = int(code)
        except (TypeError, ValueError):
            code_i = 0
        # MetaTrader5 RES_E_INTERNAL_FAIL* family (-10000..) is IPC death
        if code_i <= -10000:
            return True
        return "ipc" in desc.lower()

    def _with_reconnect(self, call: Any, what: str) -> Any:
        """Call; on a dead link, reconnect and call ONCE more.

        READS ONLY, plus the idempotent trade actions. The retry is sound only
        where repeating the call is free: a read, or an action that states a
        TARGET (`IDEMPOTENT_TRADE_ACTIONS`) and so leaves the same book when it
        arrives twice.

        It is NOT sound for an order that creates or consumes volume. A falsy
        result means the REPLY is missing; it never means the request failed to
        arrive, and no reading of `last_error()` can recover which side of the
        request the link died on. Calling again there is how 0.55 lots becomes
        1.10. Those go through `_send_once`.
        """
        result = call()
        if result is not None and not self._ipc_error():
            return result
        self.connect()
        result = call()
        if result is None or self._ipc_error():
            raise RuntimeError(f"{what} failed: {self._last_error()}")
        return result

    def select_symbol(self, name: str) -> bool:
        return bool(self._mt5.symbol_select(name, True))

    def account(self) -> Account:
        info = self._with_reconnect(lambda: self._mt5.account_info(), "account_info")
        d = _asdict(info)
        return Account(
            login=int(d.get("login", 0)),
            balance=float(d.get("balance", 0)),
            equity=float(d.get("equity", 0)),
            margin=float(d.get("margin", 0)),
            margin_free=float(d.get("margin_free", 0)),
            profit=float(d.get("profit", 0)),
            leverage=int(d.get("leverage", 0)),
            currency=str(d.get("currency", "")),
            trade_allowed=bool(d.get("trade_allowed", False)),
            trade_expert=bool(d.get("trade_expert", False)),
            server=str(d.get("server", "")),
            name=str(d.get("name", "")),
            fifo_close=bool(d.get("fifo_close", False)),
            credit=float(d.get("credit", 0)),
            margin_level=float(d.get("margin_level", 0)),
            trade_mode=int(d.get("trade_mode", 0)),
        )

    def symbol(self, name: str) -> SymbolSpec:
        """Read a symbol spec, recording every field that was NOT measured.

        Every numeric read used `d.get(key, DEFAULT) or DEFAULT` with FX-shaped
        defaults, and `unmeasured` was never set. Two failures in one:

        `or` fires on a legitimate ZERO as well as on absence, so a
        broker-reported zero became a plausible EURUSD number that nothing
        downstream could tell from a real measurement. An XAUUSD spec arriving
        with zeros was sized as though gold had a 100,000 unit contract and a $1
        tick. That is the ratio CLAUDE.md warns about under "The per-symbol
        trap": gold measures `point 0.01` and `contract_size 100` against a
        5-digit pair's `0.00001` and `100000`.

        And with `unmeasured` empty, `unmeasured_for_sizing()` was empty too, so
        the repo's own "unmeasured specs refuse, they never default" rule could
        never fire on MT5. It was implemented, tested and live on MT4 (issue #68,
        broken and re-fixed there) and simply absent here.

        THE MECHANISM, not a reminder to set a flag. Every spec field goes
        through one reader that records it and returns a value no caller can
        mistake for a measurement. `tests/test_mt5_unmeasured_specs.py` enforces
        that structurally: it fails on a raw `d.get(` anywhere in this method,
        and on a raw `d["..."]` for any key outside the three allowlisted below.
        The subscript half is not redundant. With only the `d.get(` check,
        `float(d["volume_min"]) if "volume_min" in d else 0.01` reintroduced #68
        on a sizing-gated field and passed the entire suite.

        `visible`, `trade_mode` and `name` ARE read raw, deliberately, and are
        allowlisted by name in that test. The first two are enums whose failure
        value is not 0.0, so `measure` cannot express them (0 IS the DISABLED
        reading for `trade_mode`); they carry their own recording branches
        below. `name` is the symbol label, not a measurement. A fourth raw key
        has to be added to the allowlist on purpose.

        `positive=` marks the fields where zero is not a possible measurement,
        only a failed one. Fields where zero IS a real reading pass
        `positive=False` and survive: `digits` is 0 on an index quoted in whole
        points, and `stops_level` is 0 on a broker with no minimum stop distance.

        The names recorded are the CANONICAL spec names (`tick_value`, not
        `trade_tick_value`), because `unmeasured_for_sizing()` intersects with
        `SPEC_SIZING_FIELDS`, which is spelled that way. A set full of raw MT5
        keys would look populated and gate nothing.
        """
        info = self._mt5.symbol_info(name)
        if info is None:
            raise RuntimeError(f"symbol_info({name}) failed: {self._mt5.last_error()}")
        d = _asdict(info)
        if "visible" in d and not bool(d["visible"]):
            self.select_symbol(name)
            # `_asdict(None)` is `{}`. Under the old code every `or DEFAULT` then
            # fired at once, so a terminal that answered NOTHING here produced a
            # complete, plausible, entirely invented EURUSD spec. Now an empty
            # dict marks every field unmeasured, which refuses.
            d = _asdict(self._mt5.symbol_info(name))
        unmeasured: set[str] = set()

        def measure(key: str, field: str, *, positive: bool) -> float:
            """The measured value, or 0.0 with `field` recorded as unmeasured."""
            if key not in d:
                unmeasured.add(field)
                return 0.0
            try:
                value = float(d[key])
            except (TypeError, ValueError):
                unmeasured.add(field)
                return 0.0
            if positive and value <= 0:
                unmeasured.add(field)
                return 0.0
            return value

        def text(key: str, field: str) -> str:
            """A string field. Absent is recorded; empty from the broker is not.

            An empty currency code is a real answer from some brokers on
            non-FX instruments, so it is not treated as a failed read.
            """
            if key not in d:
                unmeasured.add(field)
                return ""
            return str(d[key])

        # Evaluated before the constructor call on purpose: `unmeasured` is filled
        # in by these, and relying on argument evaluation order to have happened
        # first would be a trap for the next reader.
        point = measure("point", "point", positive=True)
        digits = measure("digits", "digits", positive=False)
        # No fallback to `point`. A derived value is not a measurement, and
        # `ticks_between` already falls back to `point` on its own if it is ever
        # reached; the sizing gate fires first, which is the honest order.
        tick_size = measure("trade_tick_size", "tick_size", positive=True)
        tick_value = measure("trade_tick_value", "tick_value", positive=True)
        contract_size = measure("trade_contract_size", "contract_size", positive=True)
        volume_min = measure("volume_min", "volume_min", positive=True)
        volume_max = measure("volume_max", "volume_max", positive=True)
        volume_step = measure("volume_step", "volume_step", positive=True)
        stops_level = measure("trade_stops_level", "stops_level", positive=False)
        freeze_level = measure("trade_freeze_level", "freeze_level", positive=False)
        filling = measure("filling_mode", "filling_mode", positive=False)
        spread = measure("spread", "spread", positive=False)

        if "trade_mode" in d:
            trade_mode = int(d["trade_mode"])
        else:
            # 4 is FULL TRADING, so the old default presented a close-only or
            # disabled symbol as fully tradable. 0 is MQL5's DISABLED: the
            # fail-closed direction, and what MT4 already does.
            unmeasured.add("trade_mode")
            trade_mode = 0

        if "visible" in d:
            visible = bool(d["visible"])
        else:
            unmeasured.add("visible")
            visible = True

        return SymbolSpec(
            name=str(d["name"]) if "name" in d else name,
            digits=int(digits),
            point=point,
            trade_tick_size=tick_size,
            trade_tick_value=tick_value,
            trade_contract_size=contract_size,
            volume_min=volume_min,
            volume_max=volume_max,
            volume_step=volume_step,
            trade_stops_level=int(stops_level),
            trade_freeze_level=int(freeze_level),
            filling_mode=int(filling),
            currency_base=text("currency_base", "currency_base"),
            currency_profit=text("currency_profit", "currency_profit"),
            currency_margin=text("currency_margin", "currency_margin"),
            trade_mode=trade_mode,
            visible=visible,
            spread=int(spread),
            unmeasured=frozenset(unmeasured),
        )

    def tick(self, name: str) -> Tick:
        t = self._with_reconnect(
            lambda: self._mt5.symbol_info_tick(name),
            f"symbol_info_tick({name})",
        )
        d = _asdict(t)
        return Tick(
            time=int(d.get("time", 0)),
            bid=float(d.get("bid", 0)),
            ask=float(d.get("ask", 0)),
            last=float(d.get("last", 0)),
            volume=int(d.get("volume", 0) or 0),
        )

    def venue_clock(
        self, name: str, *, max_staleness_sec: float | None
    ) -> VenueClock:
        """Same reading and the same rule as MT4, because it is the same defect.

        MT5 has the same server-time semantics and the same absence of any API
        that states the offset: `copy_rates_from_pos` returns bar times on the
        TERMINAL's trade-server clock, and `symbol_info_tick().time` is that
        same clock at the last tick. So this adapter was affected by
        straightedge#172 identically and is fixed identically, through the one
        seam the engine reads.

        `max_staleness_sec=None` reports the implication and refuses to call it
        a measurement; see `Mt4Broker.venue_clock`. `symbol_info_tick`
        returning None is NOT MEASURED either way, and `_with_reconnect` has
        already had its go at the IPC by then.
        """
        before = self._now()
        t = self._with_reconnect(
            lambda: self._mt5.symbol_info_tick(name),
            f"symbol_info_tick({name})",
        )
        after = self._now()
        d = _asdict(t)
        server = int(d.get("time", 0) or 0)
        if server <= 0:
            return VenueClock.not_measured(
                "server_time",
                source=MT5_CLOCK_SOURCE,
                measured_at=int(after),
                detail=f"symbol_info_tick({name}) carried no server time",
            )
        if max_staleness_sec is None:
            return VenueClock.implied(
                server, (before + after) / 2.0, source=MT5_CLOCK_SOURCE
            )
        return VenueClock.measure(
            server,
            (before + after) / 2.0,
            source=MT5_CLOCK_SOURCE,
            max_staleness_sec=max_staleness_sec,
            round_trip_sec=max(0.0, after - before),
        )

    def rates(self, name: str, timeframe: str | int, count: int) -> list[Bar]:
        raw = self._mt5.copy_rates_from_pos(name, timeframe_code(timeframe), 0, count)
        if raw is None:
            return []
        out: list[Bar] = []
        for row in raw:
            d = _asdict(row) if not isinstance(row, dict) else row
            # numpy void / named tuple: also support index access
            if not d and hasattr(row, "dtype"):
                d = {name: row[name] for name in row.dtype.names}
            out.append(
                Bar(
                    time=int(d.get("time", 0)),
                    open=float(d.get("open", 0)),
                    high=float(d.get("high", 0)),
                    low=float(d.get("low", 0)),
                    close=float(d.get("close", 0)),
                    tick_volume=int(d.get("tick_volume", 0) or 0),
                    spread=int(d.get("spread", 0) or 0),
                    real_volume=int(d.get("real_volume", 0) or 0),
                )
            )
        return out

    def positions(self, magic: int | None = None) -> list[Position]:
        raw = self._with_reconnect(lambda: self._mt5.positions_get(), "positions_get")
        if not raw:
            return []
        out: list[Position] = []
        for row in raw:
            d = _asdict(row)
            mag = int(d.get("magic", 0) or 0)
            if magic is not None and mag != magic:
                continue
            ptype = int(d.get("type", 0))
            out.append(
                Position(
                    ticket=int(d.get("ticket", 0)),
                    symbol=str(d.get("symbol", "")),
                    side=Side.BUY if ptype == 0 else Side.SELL,
                    volume=float(d.get("volume", 0)),
                    price_open=float(d.get("price_open", 0)),
                    sl=float(d.get("sl", 0) or 0),
                    tp=float(d.get("tp", 0) or 0),
                    price_current=float(d.get("price_current", 0)),
                    profit=float(d.get("profit", 0)),
                    swap=float(d.get("swap", 0) or 0),
                    magic=mag,
                    comment=str(d.get("comment", "") or ""),
                    time=int(d.get("time", 0) or 0),
                    identifier=int(d.get("identifier", 0) or d.get("ticket", 0)),
                )
            )
        return out

    def orders(self, magic: int | None = None) -> list[PendingOrder]:
        mt5 = self._mt5
        if mt5 is None or not hasattr(mt5, "orders_get"):
            return []
        raw = self._with_reconnect(lambda: self._mt5.orders_get(), "orders_get")
        if not raw:
            return []
        buy_types = {
            ORDER_TYPE_BUY,
            ORDER_TYPE_BUY_LIMIT,
            ORDER_TYPE_BUY_STOP,
            ORDER_TYPE_BUY_STOP_LIMIT,
        }
        out: list[PendingOrder] = []
        for row in raw:
            d = _asdict(row)
            mag = int(d.get("magic", 0) or 0)
            if magic is not None and mag != magic:
                continue
            ptype = int(d.get("type", 0))
            volume = float(d.get("volume_current", d.get("volume_initial", d.get("volume", 0))) or 0)
            price = float(d.get("price_open", d.get("price_current", d.get("price", 0))) or 0)
            out.append(
                PendingOrder(
                    ticket=int(d.get("ticket", 0)),
                    symbol=str(d.get("symbol", "")),
                    side=Side.BUY if ptype in buy_types else Side.SELL,
                    volume=volume,
                    price=price,
                    sl=float(d.get("sl", 0) or 0),
                    tp=float(d.get("tp", 0) or 0),
                    magic=mag,
                    comment=str(d.get("comment", "") or ""),
                    kind=_order_kind(ptype),
                    time=int(d.get("time_setup") or d.get("time") or 0),
                )
            )
        return out

    def _result(self, raw: Any, request: dict) -> OrderResult:
        if raw is None:
            # No result at all. MQL5 uses retcode 0 for a PASSED order_check,
            # so synthesizing 0 here made an unrun check indistinguishable
            # from a passed one and the engine sent the order (issue #8).
            err = self._mt5.last_error() if self._mt5 else "none"
            return OrderResult.unknown(f"no result: {err}", request)
        d = _asdict(raw)
        return OrderResult(
            retcode=int(d.get("retcode", 0)),
            comment=str(d.get("comment", "") or ""),
            deal=int(d.get("deal", 0) or 0),
            order=int(d.get("order", 0) or 0),
            volume=float(d.get("volume", 0) or 0),
            price=float(d.get("price", 0) or 0),
            bid=float(d.get("bid", 0) or 0),
            ask=float(d.get("ask", 0) or 0),
            request=request,
        )

    def order_check(self, request: dict) -> OrderResult:
        return self._result(self._mt5.order_check(request), request)

    def _send_once(self, request: dict) -> Any:
        """Put a non-idempotent request on the wire EXACTLY once.

        The link is proved up BEFORE the send and never after, and that ordering
        is the entire mechanism. `ensure_connected` is a read, so moving the
        reconnect ahead of the request costs nothing and puts it on the only
        side where a reconnect carries no risk. It narrows the window; it cannot
        close it, and nothing can.

        Once the request has left, a missing reply is an OPEN QUESTION, and this
        adapter's job is to report it as one rather than resolve it by guessing.
        `_result` turns the absent reply into `OrderResult.unknown`, which is
        `measured` False and carries no retcode a caller can retry on.

        A failure to reconnect propagates unchanged: nothing was sent, and the
        engine's send-exception path already keeps the in-flight entry open.
        """
        self.ensure_connected()
        return self._mt5.order_send(request)

    def order_send(self, request: dict) -> OrderResult:
        action = int(request.get("action", 0) or 0)
        if action in IDEMPOTENT_TRADE_ACTIONS:
            raw = self._with_reconnect(
                lambda: self._mt5.order_send(request), "order_send"
            )
        else:
            raw = self._send_once(request)
        result = self._result(raw, request)
        if result.retcode != TRADE_RETCODE_INVALID_FILL:
            return result
        # INVALID_FILL is a CONCLUSIVE rejection: the server refused the request
        # over its filling policy and placed nothing, so re-sending under a
        # different policy is this order's next attempt, not a second order.
        # An unmeasured result cannot reach this loop, because RETCODE_UNKNOWN is
        # not INVALID_FILL and returned above -- which is what keeps the one
        # retry in this method off the ambiguous path.
        tried = {request.get("type_filling")}
        for filling in FILLING_RETRY_ORDER:
            if filling in tried:
                continue
            retry = dict(request)
            retry["type_filling"] = filling
            result = self._result(self._send_once(retry), retry)
            if result.retcode in RETCODE_OK or result.retcode != TRADE_RETCODE_INVALID_FILL:
                return result
            tried.add(filling)
        return result

    def check_market(self, order: MarketOrder) -> OrderResult:
        return self.order_check(self._market_req(order))

    def market(self, order: MarketOrder) -> OrderResult:
        return self.order_send(self._market_req(order))

    def check_working(self, order: WorkingOrder) -> OrderResult:
        return self.order_check(self._working_req(order))

    def working(self, order: WorkingOrder) -> OrderResult:
        return self.order_send(self._working_req(order))

    def modify_position(self, ticket: int, sl: float, tp: float, symbol: str = "") -> OrderResult:
        req: dict[str, object] = {"action": TRADE_ACTION_SLTP, "position": ticket, "sl": sl, "tp": tp}
        if symbol:
            req["symbol"] = symbol
        return self.order_send(req)

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
    ) -> OrderResult:
        req: dict = {"action": TRADE_ACTION_MODIFY, "order": ticket, "type_time": ORDER_TIME_GTC}
        if price is not None:
            req["price"] = price
        if sl is not None:
            req["sl"] = sl
        if tp is not None:
            req["tp"] = tp
        if symbol:
            req["symbol"] = symbol
        if volume:
            req["volume"] = volume
        if kind == "limit":
            req["type"] = ORDER_TYPE_BUY_LIMIT if side == "buy" else ORDER_TYPE_SELL_LIMIT
        elif kind == "stop":
            req["type"] = ORDER_TYPE_BUY_STOP if side == "buy" else ORDER_TYPE_SELL_STOP
        return self.order_send(req)

    def cancel(self, ticket: int) -> OrderResult:
        return self.order_send({"action": TRADE_ACTION_REMOVE, "order": ticket})

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
    ) -> OrderResult:
        spec = self.symbol(symbol)
        close_side = Side.SELL if side == "buy" else Side.BUY
        return self.order_send(
            {
                "action": TRADE_ACTION_DEAL,
                "symbol": symbol,
                "volume": volume,
                "type": _deal_type(close_side),
                "position": ticket,
                "price": price,
                "deviation": deviation,
                "magic": magic,
                "comment": comment[:31],
                "type_time": ORDER_TIME_GTC,
                "type_filling": choose_filling(spec.filling_mode),
            }
        )

    def close_by(self, ticket: int, other: int, symbol: str = "") -> OrderResult:
        req: dict[str, object] = {"action": TRADE_ACTION_CLOSE_BY, "position": ticket, "position_by": other}
        if symbol:
            req["symbol"] = symbol
        return self.order_send(req)

    def _market_req(self, order: MarketOrder) -> dict:
        spec = self.symbol(order.symbol)
        if order.ticket is not None:
            close_side = Side.SELL if order.side is Side.BUY else Side.BUY
            return {
                "action": TRADE_ACTION_DEAL,
                "symbol": order.symbol,
                "volume": order.volume,
                "type": _deal_type(close_side),
                "position": order.ticket,
                "price": self.tick(order.symbol).bid if order.side is Side.BUY else self.tick(order.symbol).ask,
                "sl": order.sl,
                "tp": order.tp,
                "deviation": order.deviation,
                "magic": order.magic,
                "comment": order.comment[:31],
                "type_time": ORDER_TIME_GTC,
                "type_filling": choose_filling(spec.filling_mode),
            }
        tick = self.tick(order.symbol)
        price = tick.ask if order.side is Side.BUY else tick.bid
        return {
            "action": TRADE_ACTION_DEAL,
            "symbol": order.symbol,
            "volume": order.volume,
            "type": _deal_type(order.side),
            "price": price,
            "sl": order.sl,
            "tp": order.tp,
            "deviation": order.deviation,
            "magic": order.magic,
            "comment": order.comment[:31],
            "type_time": ORDER_TIME_GTC,
            "type_filling": choose_filling(spec.filling_mode),
        }

    def _working_req(self, order: WorkingOrder) -> dict:
        spec = self.symbol(order.symbol)
        if order.kind == "limit":
            typ = ORDER_TYPE_BUY_LIMIT if order.side is Side.BUY else ORDER_TYPE_SELL_LIMIT
        else:
            typ = ORDER_TYPE_BUY_STOP if order.side is Side.BUY else ORDER_TYPE_SELL_STOP
        return {
            "action": TRADE_ACTION_PENDING,
            "symbol": order.symbol,
            "volume": order.volume,
            "type": typ,
            "price": order.price,
            "sl": order.sl,
            "tp": order.tp,
            "magic": order.magic,
            "comment": order.comment[:31],
            "type_time": ORDER_TIME_GTC,
            "type_filling": choose_filling(spec.filling_mode),
        }
