import sys
from types import SimpleNamespace

from straightedge.broker.mt5_live import Mt5Broker, _asdict, load_mt5_module
from straightedge.constants import (
    ORDER_FILLING_IOC,
    ORDER_TYPE_BUY_LIMIT,
    ORDER_TYPE_SELL_LIMIT,
    RETCODE_UNKNOWN,
    TRADE_ACTION_CLOSE_BY,
    TRADE_ACTION_MODIFY,
    TRADE_ACTION_REMOVE,
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_INVALID_FILL,
    TRADE_RETCODE_INVALID_ORDER,
)
from straightedge.models import Side


def _nt(**kw):
    return SimpleNamespace(**kw, _asdict=lambda: dict(kw))


class FakeMt5:
    TRADE_RETCODE_DONE = TRADE_RETCODE_DONE
    TRADE_RETCODE_INVALID_FILL = TRADE_RETCODE_INVALID_FILL

    def __init__(self, *, netting: bool = False) -> None:
        self.netting = netting
        self.inited = False
        self.init_count = 0
        self.disconnected = False
        self.force_ipc = False
        self.dead_after_init = False
        self.init_ok = True
        self.login_ok = True
        self.trade_allowed = True
        self.shutdown_raises = False
        self.last_error_raises = False
        self.fill_always = False
        self.init_calls: list = []
        self.login_calls: list = []
        self.sends: list[dict] = []
        self.fill_fail_once = False
        self.order_rows: list | None = [
            _nt(
                ticket=11,
                symbol="EURUSD",
                type=ORDER_TYPE_BUY_LIMIT,
                volume_current=0.2,
                volume_initial=0.2,
                price_open=1.08,
                sl=1.07,
                tp=1.10,
                magic=20260909,
                comment="lim",
                time_setup=99,
            ),
            _nt(
                ticket=12,
                symbol="GBPUSD",
                type=ORDER_TYPE_SELL_LIMIT,
                volume_current=0.1,
                price_open=1.27,
                sl=1.28,
                tp=1.25,
                magic=1,
                comment="",
                time=50,
            ),
        ]
        self.position_rows: list = [
            _nt(
                ticket=7,
                symbol="EURUSD",
                type=0,
                volume=0.1,
                price_open=1.1,
                sl=1.09,
                tp=1.12,
                price_current=1.11,
                profit=10.0,
                swap=0.0,
                magic=20260909,
                comment="x",
                time=1,
                identifier=7,
            )
        ]

    def initialize(self, *args, **kwargs) -> bool:
        self.init_calls.append((args, kwargs))
        self.init_count += 1
        if not self.init_ok:
            return False
        self.inited = True
        self.disconnected = False
        self.force_ipc = False
        if self.dead_after_init:
            self.disconnected = True
        return True

    def login(self, *args, **kwargs) -> bool:
        self.login_calls.append((args, kwargs))
        return self.login_ok

    def shutdown(self) -> None:
        if self.shutdown_raises:
            raise RuntimeError("shutdown")
        self.inited = False

    def last_error(self):
        if self.last_error_raises:
            raise RuntimeError("last_error")
        if self.force_ipc or self.disconnected or not self.inited:
            return (-10001, "IPC send failed")
        return (1, "ok")

    def _dead(self) -> bool:
        return self.disconnected or not self.inited

    def terminal_info(self):
        return _nt(trade_allowed=self.trade_allowed)

    def account_info(self):
        if self._dead():
            return None
        return _nt(
            login=1,
            trade_mode=0,
            leverage=100,
            balance=10_000.0,
            equity=10_000.0,
            margin=0.0,
            margin_free=10_000.0,
            profit=0.0,
            currency="USD",
            trade_allowed=True,
            trade_expert=True,
            server="Demo",
            name="t",
            fifo_close=False,
            credit=0.0,
            margin_level=0.0,
        )

    def symbol_select(self, name, enable=True) -> bool:
        del name, enable
        return True

    def symbol_info(self, name):
        return _nt(
            name=name,
            digits=5,
            point=0.00001,
            trade_tick_size=0.00001,
            trade_tick_value=1.0,
            trade_contract_size=100000.0,
            volume_min=0.01,
            volume_max=100.0,
            volume_step=0.01,
            trade_stops_level=10,
            trade_freeze_level=0,
            filling_mode=2,
            currency_base="EUR",
            currency_profit="USD",
            currency_margin="USD",
            trade_mode=4,
            visible=True,
            spread=10,
        )

    def symbol_info_tick(self, name):
        del name
        if self._dead():
            return None
        return _nt(time=1, bid=1.1, ask=1.1001, last=1.1, volume=1)

    def copy_rates_from_pos(self, name, tf, start, count):
        del name, tf, start
        return [
            _nt(time=i, open=1.1, high=1.11, low=1.09, close=1.105, tick_volume=10, spread=10, real_volume=0)
            for i in range(count)
        ]

    def orders_get(self):
        if self._dead():
            return None
        return self.order_rows

    def positions_get(self):
        if self._dead():
            return None
        return self.position_rows

    def order_check(self, request):
        return _nt(retcode=0, comment="Done", deal=0, order=0, volume=request.get("volume", 0), price=1.1, bid=1.1, ask=1.1)

    def order_send(self, request):
        if self._dead():
            return None
        self.sends.append(request)
        if self.fill_always or self.fill_fail_once:
            if self.fill_fail_once:
                self.fill_fail_once = False
            return _nt(
                retcode=TRADE_RETCODE_INVALID_FILL,
                comment="invalid fill",
                deal=0,
                order=0,
                volume=0,
                price=0,
                bid=1.1,
                ask=1.1,
            )
        action = int(request.get("action", 0))
        if action == TRADE_ACTION_MODIFY:
            ticket = int(request.get("order") or 0)
            rows = list(self.order_rows or [])
            for i, row in enumerate(rows):
                if int(row.ticket) != ticket:
                    continue
                data = dict(row._asdict())
                if "price" in request:
                    data["price_open"] = request["price"]
                if "sl" in request:
                    data["sl"] = request["sl"]
                if "tp" in request:
                    data["tp"] = request["tp"]
                rows[i] = _nt(**data)
                self.order_rows = rows
                break
            return _nt(
                retcode=TRADE_RETCODE_DONE,
                comment="Done",
                deal=0,
                order=ticket,
                volume=request.get("volume", 0),
                price=request.get("price", 1.1),
                bid=1.1,
                ask=1.1,
            )
        if action == TRADE_ACTION_REMOVE:
            ticket = int(request.get("order") or 0)
            rows = list(self.order_rows or [])
            self.order_rows = [row for row in rows if int(row.ticket) != ticket]
            return _nt(
                retcode=TRADE_RETCODE_DONE,
                comment="Done",
                deal=0,
                order=ticket,
                volume=request.get("volume", 0),
                price=1.1,
                bid=1.1,
                ask=1.1,
            )
        if action == TRADE_ACTION_CLOSE_BY:
            if self.netting:
                return _nt(
                    retcode=TRADE_RETCODE_INVALID_ORDER,
                    comment="hedge only",
                    deal=0,
                    order=0,
                    volume=0,
                    price=0,
                    bid=1.1,
                    ask=1.1,
                )
            gone = {
                int(request.get("position") or 0),
                int(request.get("position_by") or 0),
            }
            self.position_rows = [row for row in self.position_rows if int(row.ticket) not in gone]
            return _nt(
                retcode=TRADE_RETCODE_DONE,
                comment="Done",
                deal=int(request.get("position") or 0),
                order=int(request.get("position") or 0),
                volume=request.get("volume", 0),
                price=1.1,
                bid=1.1,
                ask=1.1,
            )
        return _nt(
            retcode=TRADE_RETCODE_DONE,
            comment="Done",
            deal=1,
            order=2,
            volume=request.get("volume", 0),
            price=1.1,
            bid=1.1,
            ask=1.1,
        )


def test_adapter_maps_account_and_positions() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    acct = broker.account()
    assert acct.login == 1
    assert acct.currency == "USD"
    spec = broker.symbol("EURUSD")
    assert spec.volume_min == 0.01
    tick = broker.tick("EURUSD")
    assert tick.ask > tick.bid
    rates = broker.rates("EURUSD", 16385, 3)
    assert len(rates) == 3
    pos = broker.positions(magic=20260909)
    assert len(pos) == 1
    assert pos[0].side is Side.BUY
    broker.disconnect()
    assert fake.inited is False


def test_adapter_maps_orders() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    ours = broker.orders(magic=20260909)
    assert len(ours) == 1
    order = ours[0]
    assert order.ticket == 11
    assert order.symbol == "EURUSD"
    assert order.side is Side.BUY
    assert order.volume == 0.2
    assert order.price == 1.08
    assert order.sl == 1.07
    assert order.tp == 1.10
    assert order.kind == "limit"
    assert order.time == 99
    all_orders = broker.orders()
    assert len(all_orders) == 2
    assert all_orders[1].side is Side.SELL
    assert all_orders[1].ticket == 12
    fake.order_rows = []
    assert broker.orders() == []
    fake.order_rows = None
    try:
        broker.orders()
        raise AssertionError("expected RuntimeError on None orders_get")
    except RuntimeError as exc:
        assert "orders_get" in str(exc)
    broker.disconnect()


def test_orders_without_orders_get() -> None:
    assert Mt5Broker().orders() == []
    broker = Mt5Broker(mt5=object())
    assert broker.orders() == []


def test_invalid_fill_retries() -> None:
    fake = FakeMt5()
    fake.fill_fail_once = True
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    result = broker.order_send({"action": 1, "type_filling": 0, "volume": 0.01})
    assert result.ok
    assert len(fake.sends) == 2
    assert fake.sends[1]["type_filling"] == ORDER_FILLING_IOC


def test_connect_is_reentrant() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    broker.connect()
    assert fake.init_count == 2
    assert fake.inited is True


def test_ensure_connected_skips_when_healthy() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    broker.ensure_connected()
    assert fake.init_count == 1
    fake.disconnected = True
    broker.ensure_connected()
    assert fake.init_count == 2
    assert fake.disconnected is False


def test_reconnect_after_account_info_none() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    fake.disconnected = True
    acct = broker.account()
    assert acct.login == 1
    assert fake.init_count == 2
    assert fake.inited is True
    tick = broker.tick("EURUSD")
    assert tick.ask > tick.bid
    assert broker.positions()
    assert broker.orders()


def test_reconnect_on_ipc_last_error() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    fake.force_ipc = True
    acct = broker.account()
    assert acct.login == 1
    assert fake.init_count == 2
    assert fake.force_ipc is False


def test_reconnect_once_then_raises() -> None:
    fake = FakeMt5()
    fake.dead_after_init = True
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    assert fake.init_count == 1
    try:
        broker.account()
        raise AssertionError("expected RuntimeError after one reconnect")
    except RuntimeError as exc:
        assert "account_info" in str(exc)
    assert fake.init_count == 2


def test_order_send_reconnects_BEFORE_it_sends_never_after() -> None:
    """A dead link is repaired ahead of the request, not behind it.

    All three assertions below held under the old `_with_reconnect` send too,
    which is why this test passed while the adapter could double-send: this
    fake's `_dead()` answers None WITHOUT recording, so the send it discards
    never reached the terminal and repeating it was free. The dangerous half --
    accepted, then the reply lost -- is unrepresentable here and lives in
    `tests/test_order_send_is_sent_once.py`.

    Kept, and renamed, because the old name ("reconnects on none") described the
    defect as the contract and would invite it straight back.
    """
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    fake.disconnected = True
    result = broker.order_send({"action": 1, "type_filling": 0, "volume": 0.01})
    assert result.ok
    assert fake.init_count == 2, "ensure_connected did not repair the link first"
    assert len(fake.sends) == 1


# Paper mutates in tests/test_paper_pending.py. Live is a pass-through:
# MODIFY / REMOVE / CLOSE_BY return 10009 (TRADE_RETCODE_DONE) and orders()
# still maps whatever orders_get returns after the send.


def test_adapter_modify_pending() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    result = broker.order_send(
        {
            "action": TRADE_ACTION_MODIFY,
            "order": 11,
            "price": 1.079,
            "sl": 1.069,
            "tp": 1.101,
        }
    )
    assert result.retcode == TRADE_RETCODE_DONE
    assert result.ok
    assert fake.sends[-1]["action"] == TRADE_ACTION_MODIFY
    ours = broker.orders(magic=20260909)
    assert len(ours) == 1
    assert ours[0].ticket == 11
    assert ours[0].price == 1.079
    assert ours[0].sl == 1.069
    assert ours[0].tp == 1.101
    all_orders = broker.orders()
    assert len(all_orders) == 2
    assert all_orders[1].ticket == 12
    assert all_orders[1].price == 1.27
    broker.disconnect()


def test_adapter_remove_pending() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    result = broker.order_send({"action": TRADE_ACTION_REMOVE, "order": 11})
    assert result.retcode == TRADE_RETCODE_DONE
    assert result.ok
    remaining = broker.orders()
    assert len(remaining) == 1
    assert remaining[0].ticket == 12
    assert remaining[0].side is Side.SELL
    assert remaining[0].kind == "limit"
    assert broker.orders(magic=20260909) == []
    broker.disconnect()


def test_adapter_close_by() -> None:
    fake = FakeMt5()
    fake.position_rows.append(
        _nt(
            ticket=8,
            symbol="EURUSD",
            type=1,
            volume=0.1,
            price_open=1.11,
            sl=1.12,
            tp=1.09,
            price_current=1.1,
            profit=-10.0,
            swap=0.0,
            magic=20260909,
            comment="y",
            time=2,
            identifier=8,
        )
    )
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    assert len(broker.positions()) == 2
    result = broker.order_send(
        {
            "action": TRADE_ACTION_CLOSE_BY,
            "position": 7,
            "position_by": 8,
        }
    )
    assert result.retcode == TRADE_RETCODE_DONE
    assert result.ok
    assert broker.positions() == []
    mapped = broker.orders()
    assert len(mapped) == 2
    assert mapped[0].ticket == 11
    assert mapped[1].ticket == 12
    broker.disconnect()


def test_adapter_close_by_refused_on_netting() -> None:
    fake = FakeMt5(netting=True)
    fake.position_rows.append(
        _nt(
            ticket=8,
            symbol="EURUSD",
            type=1,
            volume=0.1,
            price_open=1.11,
            sl=1.12,
            tp=1.09,
            price_current=1.1,
            profit=-10.0,
            swap=0.0,
            magic=20260909,
            comment="y",
            time=2,
            identifier=8,
        )
    )
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    assert len(broker.positions()) == 2
    result = broker.order_send(
        {
            "action": TRADE_ACTION_CLOSE_BY,
            "position": 7,
            "position_by": 8,
        }
    )
    assert result.retcode == TRADE_RETCODE_INVALID_ORDER
    assert not result.ok
    assert result.comment == "hedge only"
    assert fake.sends[-1]["action"] == TRADE_ACTION_CLOSE_BY
    remaining = broker.positions()
    assert len(remaining) == 2
    assert {row.ticket for row in remaining} == {7, 8}
    broker.disconnect()


def test_asdict_none_and_plain_object() -> None:
    assert _asdict(None) == {}
    class Plain:
        def __init__(self) -> None:
            self.a = 1
    assert _asdict(Plain()) == {"a": 1}


def test_load_mt5_module_official_then_mac_then_missing() -> None:
    import types

    official = types.ModuleType("MetaTrader5")
    mac = types.ModuleType("mt5_mac")
    saved_official = sys.modules.get("MetaTrader5")
    saved_mac = sys.modules.get("mt5_mac")
    sys.modules["MetaTrader5"] = official
    try:
        assert load_mt5_module() is official
    finally:
        if saved_official is None:
            sys.modules.pop("MetaTrader5", None)
        else:
            sys.modules["MetaTrader5"] = saved_official
    sys.modules["MetaTrader5"] = None  # type: ignore[assignment]
    sys.modules["mt5_mac"] = mac
    try:
        assert load_mt5_module() is mac
        sys.modules["mt5_mac"] = None  # type: ignore[assignment]
        try:
            load_mt5_module()
            raise AssertionError("expected RuntimeError when no MT5 binding")
        except RuntimeError as exc:
            assert "No MT5" in str(exc)
    finally:
        if saved_official is None:
            sys.modules.pop("MetaTrader5", None)
        else:
            sys.modules["MetaTrader5"] = saved_official
        if saved_mac is None:
            sys.modules.pop("mt5_mac", None)
        else:
            sys.modules["mt5_mac"] = saved_mac


def test_connect_with_path_and_login() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake, login=42, password="pw", server="Demo", path="/tmp/term")
    broker.connect()
    assert fake.init_count == 1
    args, kwargs = fake.init_calls[-1]
    assert args[0] == "/tmp/term"
    assert kwargs["login"] == 42
    assert kwargs["password"] == "pw"
    assert kwargs["server"] == "Demo"
    assert fake.login_calls
    broker.disconnect()


def test_connect_passes_login_password_server() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake, login=7, password="pw", server="Demo")
    broker.connect()
    _args, kwargs = fake.init_calls[-1]
    assert kwargs["login"] == 7
    assert kwargs["password"] == "pw"
    assert kwargs["server"] == "Demo"
    assert fake.login_calls[-1][0][0] == 7
    broker.disconnect()


def test_initialize_failure() -> None:
    fake = FakeMt5()
    fake.init_ok = False
    broker = Mt5Broker(mt5=fake)
    try:
        broker.connect()
        raise AssertionError("expected RuntimeError on initialize")
    except RuntimeError as exc:
        assert "initialize" in str(exc)


def test_login_failure() -> None:
    fake = FakeMt5()
    fake.login_ok = False
    broker = Mt5Broker(mt5=fake, login=1, password="pw", server="Demo")
    try:
        broker.connect()
        raise AssertionError("expected RuntimeError on login")
    except RuntimeError as exc:
        assert "login" in str(exc)


def test_trade_allowed_false() -> None:
    fake = FakeMt5()
    fake.trade_allowed = False
    broker = Mt5Broker(mt5=fake)
    try:
        broker.connect()
        raise AssertionError("expected RuntimeError on trade_allowed")
    except RuntimeError as exc:
        assert "trade_allowed" in str(exc)


def test_shutdown_error_is_ignored_on_connect() -> None:
    fake = FakeMt5()
    fake.shutdown_raises = True
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    assert fake.init_count == 1


def test_connect_without_shutdown_attr() -> None:
    class Bare:
        def initialize(self, **kwargs):
            del kwargs
            return True

        def terminal_info(self):
            return _nt(trade_allowed=True)

    broker = Mt5Broker(mt5=Bare())
    broker.connect()


def test_terminal_info_none() -> None:
    fake = FakeMt5()
    fake.terminal_info = lambda: None
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    assert fake.init_count == 1


def test_disconnect_when_never_connected() -> None:
    Mt5Broker().disconnect()


def test_ensure_connected_when_unbound() -> None:
    import straightedge.broker.mt5_live as live

    fake = FakeMt5()
    orig = live.load_mt5_module
    live.load_mt5_module = lambda: fake
    try:
        broker = Mt5Broker()
        broker.ensure_connected()
        assert fake.init_count == 1
        assert fake.inited is True
    finally:
        live.load_mt5_module = orig


def test_ensure_connected_when_account_info_raises() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()

    def boom():
        raise RuntimeError("dead")

    fake.account_info = boom
    broker.ensure_connected()
    assert fake.init_count == 2


def test_last_error_shapes_and_exceptions() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    fake.last_error = lambda: None
    assert broker._ipc_error() is False
    fake.last_error = lambda: "unknown"
    assert broker._ipc_error() is False
    fake.last_error = lambda: ()
    assert broker._ipc_error() is False
    fake.last_error = lambda: "IPC timeout"
    assert broker._ipc_error() is True
    fake.last_error = lambda: "not this"
    assert broker._ipc_error() is False
    fake.last_error = lambda: (-10001,)
    assert broker._ipc_error() is True
    fake.last_error = lambda: [-10001, None]
    assert broker._ipc_error() is True
    fake.last_error = lambda: (object(), "IPC")
    assert broker._ipc_error() is True
    fake.last_error_raises = True
    fake.last_error = FakeMt5.last_error.__get__(fake, FakeMt5)
    assert broker._last_error() == "unknown"
    broker._mt5 = None
    assert broker._last_error() == "unknown"
    assert broker._ipc_error() is False
    broker._mt5 = object()
    assert broker._last_error() == "unknown"


def test_symbol_info_none() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    fake.symbol_info = lambda name: None
    try:
        broker.symbol("EURUSD")
        raise AssertionError("expected RuntimeError on symbol_info")
    except RuntimeError as exc:
        assert "symbol_info" in str(exc)


def test_symbol_not_visible_selects() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    orig = fake.symbol_info
    calls = {"n": 0}

    def info(name):
        calls["n"] += 1
        row = orig(name)
        data = dict(row._asdict())
        data["visible"] = calls["n"] > 1
        return _nt(**data)

    fake.symbol_info = info
    spec = broker.symbol("EURUSD")
    assert spec.visible is True
    assert calls["n"] == 2
    assert fake.inited is True


def test_rates_none() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    fake.copy_rates_from_pos = lambda *a, **k: None
    assert broker.rates("EURUSD", 16385, 3) == []


def test_rates_numpy_dtype_rows() -> None:
    class Row:
        __slots__ = ()
        dtype = SimpleNamespace(
            names=("time", "open", "high", "low", "close", "tick_volume", "spread", "real_volume")
        )

        def __getitem__(self, name):
            return {
                "time": 1,
                "open": 1.1,
                "high": 1.11,
                "low": 1.09,
                "close": 1.105,
                "tick_volume": 10,
                "spread": 10,
                "real_volume": 0,
            }[name]

    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    fake.copy_rates_from_pos = lambda *a, **k: [Row()]
    bars = broker.rates("EURUSD", 16385, 1)
    assert len(bars) == 1
    assert bars[0].close == 1.105


def test_positions_skips_other_magic() -> None:
    fake = FakeMt5()
    fake.position_rows.append(
        _nt(
            ticket=9,
            symbol="GBPUSD",
            type=1,
            volume=0.2,
            price_open=1.2,
            sl=0.0,
            tp=0.0,
            price_current=1.2,
            profit=0.0,
            swap=0.0,
            magic=1,
            comment="",
            time=2,
            identifier=9,
        )
    )
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    pos = broker.positions(magic=20260909)
    assert len(pos) == 1
    assert pos[0].ticket == 7
    broker.disconnect()


def test_order_check_and_none_result() -> None:
    fake = FakeMt5()
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    ok = broker.order_check({"action": 1, "volume": 0.01})
    assert ok.comment == "Done"
    assert ok.measured
    fake.order_check = lambda request: None
    missing = broker.order_check({"action": 1})
    # Must NOT be 0: order_check uses 0 for PASSED (issue #8).
    assert missing.retcode == RETCODE_UNKNOWN
    assert not missing.measured
    assert not missing.ok
    assert "no result" in missing.comment


def test_invalid_fill_exhausts_retries() -> None:
    fake = FakeMt5()
    fake.fill_always = True
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    result = broker.order_send({"action": 1, "type_filling": 0, "volume": 0.01})
    assert result.retcode == TRADE_RETCODE_INVALID_FILL
    assert not result.ok
    assert len(fake.sends) == 3
