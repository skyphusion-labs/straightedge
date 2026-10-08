"""The error that triggered a reconnect has to reach the journal (issue #127).

Why this file exists, measured and not supposed. `Engine.step_all` caught the
exception that triggers every venue reconnect WITHOUT BINDING IT:

    except (RuntimeError, OSError, ValueError):
        if not self._reconnect_broker():
            return

`_reconnect_broker` then wrote `reconnect ok=True` with no cause field, so a
recovered blip left a record that a reconnect happened and no record of what
broke. Conrad's live journal from the Vultr Windows box over 2026-09-26 to
2026-10-07 is mostly lone `ok=True` lines for exactly that reason:

    2026-09-30T03:24:01 reconnect ok=True
    2026-10-01T11:46:55 reconnect ok=True
    2026-10-07T11:19:47 loop_error error=mt4 bridge timeout
    2026-10-07T17:53:59 reconnect ok=True

Each `ok=True` there means something threw and the reason is gone, so "is the
bridge flaky and expected on that box, or is it degrading" cannot be put to
twelve days of records.

**The trap this file is written around.** Asserting that a `reconnect` event
EXISTS passes against the defect, because the event already existed. Every
assertion below is on the CAUSE: that it is present, that it carries the
transport, the phase and the op, and that it came off the exception rather than
out of a constant. `test_the_cause_is_read_off_the_exception_not_hardcoded` is
the control for that last part: it drives a cause with no bridge attributes at
all and requires the bridge fields to be ABSENT. A hardcoded `cause_phase`
passes the first test and fails that one.

**Boundedness is a gate here, not a note.** straightedge#119 is open because a
journal row stored a rendering of other rows and compounded daily. A row that
records a cause is the obvious next instance of that shape, so the clip is
asserted directly.
"""

from __future__ import annotations

import email.message
import io
import urllib.error
from pathlib import Path
from typing import Any

import pytest

from straightedge.broker.mt4_live import (
    BRIDGE_TIMEOUT_PHASES,
    PHASE_CONNECT,
    PHASE_MAILBOX,
    PHASE_READ,
    PHASE_SHIM_MAILBOX,
    PHASE_SHIM_UNAVAILABLE,
    TRANSPORT_FILE,
    TRANSPORT_NET,
    BridgeTimeout,
    FileBridge,
)
from straightedge.broker.mt4_net import HttpBridge
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig, SessionConfig
from straightedge.engine import Engine
from straightedge.synthetic import generate_bars

GOOD_TOKEN = "t" * 40


def _cfg(tmp_path: Path, *, symbols: list[str] | None = None) -> BotConfig:
    cfg = BotConfig()
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = symbols or ["EURUSD"]
    cfg.initial_balance = 10_000.0
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    return cfg


class DroppingBroker:
    """A paper broker that can drop the link the way the MT4 bridge does.

    `raise_on_ensure` is where the real fault lands in production:
    `Mt4Broker.ensure_connected()` pings the mailbox on every single step, so a
    quiet mailbox raises there with `op=ping` long before `account()` is reached.
    """

    def __init__(
        self,
        inner: PaperBroker,
        *,
        raise_on_ensure: BaseException | None = None,
        raise_on_account: BaseException | None = None,
        fail_connect: BaseException | None = None,
        fail_select: tuple[str, ...] = (),
    ) -> None:
        self._inner = inner
        self._ensure_exc = raise_on_ensure
        #: Raised from `account()` only AFTER the reconnect has run, so
        #: `Engine.start()` still gets a readable account and the test is about
        #: `step_all`. A broker that refused the account from the first call
        #: would fail in `start()` and the test would be red for a reason that
        #: has nothing to do with the defect.
        self._account_exc = raise_on_account
        self._connect_exc = fail_connect
        self._fail_select = fail_select
        self.connects = 0
        #: Set once the reconnect has run, so the account failure can be made to
        #: happen BEFORE it and not after, or the other way round.
        self.reconnected = False
        self.account_calls = 0

    def connect(self) -> None:
        self.connects += 1
        # Every injected fault below is gated on this NOT being the first
        # connect. `Engine.start()` connects, selects every symbol and reads the
        # account before `step_all` is ever called, so a broker that refused any
        # of those from the first call would make these tests red in `start()`,
        # for a reason that has nothing to do with the defect under test.
        if self._connect_exc is not None and self.connects > 1:
            raise self._connect_exc
        self._inner.connect()
        self.reconnected = self.connects > 1

    def disconnect(self) -> None:
        self._inner.disconnect()

    def ensure_connected(self) -> None:
        if self._ensure_exc is not None and not self.reconnected:
            raise self._ensure_exc

    def account(self) -> Any:
        self.account_calls += 1
        if self._account_exc is not None and self.reconnected:
            raise self._account_exc
        return self._inner.account()

    def select_symbol(self, name: str) -> None:
        if name in self._fail_select and self.reconnected:
            raise RuntimeError(f"mt4: select {name} refused by the Expert")
        self._inner.select_symbol(name)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def _engine(cfg: BotConfig, broker: Any, tmp_path: Path) -> Engine:
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()
    return engine


def _rows(engine: Engine, event: str) -> list[dict[str, Any]]:
    return [r for r in engine.journal.tail(40) if r.get("event") == event]


def _bridge_timeout(**kw: Any) -> BridgeTimeout:
    defaults: dict[str, Any] = {
        "op": "ping",
        "withdrawal": "withdrawn",
        "req_id": 41,
        "transport": TRANSPORT_FILE,
        "phase": PHASE_MAILBOX,
    }
    defaults.update(kw)
    return BridgeTimeout(
        "mt4 bridge timeout after 5.0s transport=file phase=mailbox "
        "op=ping request=withdrawn",
        **defaults,
    )


def _seeded(symbols: list[str]) -> PaperBroker:
    inner = PaperBroker(balance=10_000)
    for i, name in enumerate(symbols):
        inner.seed_bars(name, generate_bars(80, drift=0.0004, seed=3 + i))
    return inner


class TestTheCauseReachesTheJournal:
    def test_a_recovered_blip_records_what_broke(self, tmp_path: Path) -> None:
        """The defect, stated as an assertion.

        Before the fix this test fails on `cause`, with the `reconnect` row
        itself present and `ok=True`: the event existed, the reason did not.
        """
        cfg = _cfg(tmp_path)
        broker = DroppingBroker(_seeded(cfg.symbols), raise_on_ensure=_bridge_timeout())
        engine = _engine(cfg, broker, tmp_path)
        engine.step_all()

        rows = _rows(engine, "reconnect")
        assert rows, "no reconnect was journaled at all"
        row = rows[-1]
        print(f"reconnect row: {row}")
        assert row.get("ok") is True
        assert "mt4 bridge timeout" in str(row.get("cause") or ""), (
            "the reconnect recovered and the journal still does not say what "
            f"triggered it: {row}"
        )
        assert row.get("cause_type") == "BridgeTimeout"
        engine.stop()

    def test_the_transport_the_phase_and_the_op_are_named(self, tmp_path: Path) -> None:
        """Three facts that shared one string, now three fields.

        A timeout on the file mailbox, a timeout waiting on the HTTP shim and a
        host that is gone all produced `mt4 bridge timeout`. Which op was in
        flight mattered too: a timed-out `ping` is a quiet mailbox and a
        timed-out `market` is money in an unknown state.
        """
        cfg = _cfg(tmp_path)
        broker = DroppingBroker(
            _seeded(cfg.symbols),
            raise_on_ensure=_bridge_timeout(
                op="market", transport=TRANSPORT_NET, phase=PHASE_SHIM_MAILBOX
            ),
        )
        engine = _engine(cfg, broker, tmp_path)
        engine.step_all()

        row = _rows(engine, "reconnect")[-1]
        print(f"reconnect row: {row}")
        assert row.get("cause_transport") == TRANSPORT_NET
        assert row.get("cause_phase") == PHASE_SHIM_MAILBOX
        assert row.get("cause_op") == "market"
        assert row.get("cause_withdrawal") == "withdrawn"
        assert row.get("cause_req_id") == 41
        engine.stop()

    def test_the_cause_is_read_off_the_exception_not_hardcoded(
        self, tmp_path: Path
    ) -> None:
        """The control for the two tests above.

        Their green has to be produced by the exception. A `cause_transport`
        written as a literal, or defaulted to "file" because that is the common
        case, passes both of them and fails here: a plain `RuntimeError` carries
        no bridge attributes, so those keys must be ABSENT rather than blank or
        guessed. Absent and empty are different answers; one says the transport
        did not report, the other says it reported nothing.
        """
        cfg = _cfg(tmp_path)
        broker = DroppingBroker(
            _seeded(cfg.symbols),
            raise_on_ensure=RuntimeError("mt5.initialize failed: (1, 'no ipc')"),
        )
        engine = _engine(cfg, broker, tmp_path)
        engine.step_all()

        row = _rows(engine, "reconnect")[-1]
        print(f"reconnect row: {row}")
        assert row.get("cause_type") == "RuntimeError"
        assert "no ipc" in str(row.get("cause") or "")
        for key in (
            "cause_op",
            "cause_transport",
            "cause_phase",
            "cause_withdrawal",
            "cause_req_id",
        ):
            assert key not in row, (
                f"{key} was written for an exception that does not carry it, so "
                "the bridge fields are not being read off the exception"
            )
        engine.stop()

    def test_a_failed_reconnect_keeps_its_own_error_and_adds_the_cause(
        self, tmp_path: Path
    ) -> None:
        """`error` and `cause` are two different faults and must not merge.

        `error` is this reconnect attempt failing. `cause` is what made it
        necessary. Overloading one field would make an `ok=True` row that
        carried an `error` unreadable, and the pair is what distinguishes "the
        bridge blipped and the reconnect also failed" from "the bridge blipped".
        """
        cfg = _cfg(tmp_path)
        broker = DroppingBroker(
            _seeded(cfg.symbols),
            raise_on_ensure=_bridge_timeout(),
            fail_connect=RuntimeError("mt4 ping failed: no Expert on the chart"),
        )
        engine = _engine(cfg, broker, tmp_path)
        engine.step_all()

        row = _rows(engine, "reconnect")[-1]
        print(f"reconnect row: {row}")
        assert row.get("ok") is False
        assert "no Expert on the chart" in str(row.get("error") or "")
        assert "mt4 bridge timeout" in str(row.get("cause") or "")
        assert row.get("error") != row.get("cause")
        engine.stop()

    def test_the_cause_is_clipped_so_a_row_cannot_grow(self, tmp_path: Path) -> None:
        """straightedge#119's shape, refused here before it can appear.

        #119 is open because a journal row stored a rendering of other rows and
        compounded daily. Everything this change writes is a bounded scalar, and
        the only free-text field is clipped. A bridge that one day reports a
        5000-character error must not put 5000 characters on a row that is
        written on every blip.
        """
        cfg = _cfg(tmp_path)
        broker = DroppingBroker(
            _seeded(cfg.symbols),
            raise_on_ensure=BridgeTimeout("x" * 5000, op="y" * 500),
        )
        engine = _engine(cfg, broker, tmp_path)
        engine.step_all()

        row = _rows(engine, "reconnect")[-1]
        assert len(str(row["cause"])) == 200, len(str(row["cause"]))
        assert len(str(row["cause_op"])) == 40, len(str(row["cause_op"]))
        longest = max(len(str(v)) for v in row.values())
        print(f"longest field on the row: {longest} chars")
        assert longest <= 200, f"an unbounded field reached the row: {row}"
        engine.stop()


class TestAReconnectThatRecoveredLessThanItClaims:
    def test_a_symbol_that_cannot_be_reselected_is_counted(
        self, tmp_path: Path
    ) -> None:
        """`ok=True` with the symbols missing used to read as a clean recovery.

        `_reconnect_broker` swallowed `select_symbol` failures with a bare
        `continue` and wrote `ok=True` anyway, so a link that came back without
        its instruments rendered identically to one that came back whole. Those
        symbols cannot trade until they are selected.
        """
        symbols = ["EURUSD", "XAUUSD", "GBPUSD"]
        cfg = _cfg(tmp_path, symbols=symbols)
        broker = DroppingBroker(
            _seeded(symbols),
            raise_on_ensure=_bridge_timeout(),
            fail_select=("XAUUSD", "GBPUSD"),
        )
        engine = _engine(cfg, broker, tmp_path)
        engine.step_all()

        row = _rows(engine, "reconnect")[-1]
        print(f"reconnect row: {row}")
        assert row.get("ok") is True
        assert row.get("unselected") == 2, (
            "two of three symbols could not be reselected and the row does not "
            f"say so: {row}"
        )
        engine.stop()

    def test_a_clean_reconnect_carries_no_unselected_field(
        self, tmp_path: Path
    ) -> None:
        """The positive control for the count: it has to be able to be absent.

        A field written unconditionally as 0 would make the test above pass and
        mean nothing, because every row would carry it and an operator grepping
        `unselected` would get every reconnect the desk ever made.
        """
        cfg = _cfg(tmp_path, symbols=["EURUSD", "XAUUSD"])
        broker = DroppingBroker(
            _seeded(["EURUSD", "XAUUSD"]), raise_on_ensure=_bridge_timeout()
        )
        engine = _engine(cfg, broker, tmp_path)
        engine.step_all()

        row = _rows(engine, "reconnect")[-1]
        print(f"reconnect row: {row}")
        assert row.get("ok") is True
        assert "unselected" not in row, row
        engine.stop()

    def test_an_account_still_unreadable_after_the_reconnect_is_journaled(
        self, tmp_path: Path
    ) -> None:
        """The third state, which used to leave no record at all.

        `step_all` reconnected, called `account()` again, and on a second
        failure returned with NOTHING written, while the `reconnect ok=True`
        above it claimed recovery. A tick that was abandoned has to be in the
        record, or the journal says the desk is fine on a desk that is not
        ticking.
        """
        cfg = _cfg(tmp_path)
        broker = DroppingBroker(
            _seeded(cfg.symbols),
            raise_on_ensure=_bridge_timeout(),
            raise_on_account=_bridge_timeout(op="account"),
        )
        engine = _engine(cfg, broker, tmp_path)
        engine.step_all()

        rows = _rows(engine, "account_read_failed")
        assert rows, (
            "the reconnect reported ok=True, the account was still unreadable, "
            "and the tick left no record: "
            f"{[r.get('event') for r in engine.journal.tail(10)]}"
        )
        row = rows[-1]
        print(f"account_read_failed row: {row}")
        assert row.get("error_type") == "BridgeTimeout"
        assert row.get("error_op") == "account"
        assert row.get("error_transport") == TRANSPORT_FILE
        engine.stop()


def _http_error(code: int, body: bytes) -> urllib.error.HTTPError:
    """A real `HTTPError`, readable once, exactly as `_short` expects it."""
    return urllib.error.HTTPError(
        "http://shim.invalid/mt4/call",
        code,
        "shim said no",
        email.message.Message(),
        io.BytesIO(body),
    )


def _opener_raising(exc: BaseException) -> Any:
    def _open(request: object, timeout: float | None = None) -> Any:
        del request, timeout
        raise exc

    return _open


def _net_bridge(exc: BaseException) -> HttpBridge:
    return HttpBridge(
        "http://shim.invalid/mt4/call", GOOD_TOKEN, opener=_opener_raising(exc)
    )


class TestEveryTimeoutNamesWhereItHappened:
    """Five conditions that shared two strings, driven one at a time.

    This is the partition, not a sample of it: the set of `(transport, phase)`
    pairs produced below is compared against `BRIDGE_TIMEOUT_PHASES`, so a sixth
    phase added without a case here goes RED, and two cases that collapse onto
    one pair go red as well. A test that merely checked each raise had SOME phase
    would pass with every phase set to the same value, which is the defect.
    """

    def test_the_file_mailbox_timeout_names_the_file_leg(self, tmp_path: Path) -> None:
        bridge = FileBridge(tmp_path, timeout_sec=0.05)
        with pytest.raises(BridgeTimeout) as got:
            bridge.call("ping", {})
        exc = got.value
        print(f"file leg: {exc}")
        assert (exc.transport, exc.phase) == (TRANSPORT_FILE, PHASE_MAILBOX)
        assert exc.op == "ping"
        assert f"transport={TRANSPORT_FILE}" in str(exc)
        assert f"phase={PHASE_MAILBOX}" in str(exc)

    def test_a_read_timeout_and_a_connect_timeout_are_not_one_fact(self) -> None:
        """Both are `BridgeTimeout` and both must stay retryable, but a shim
        that accepted the request and went quiet is a different diagnosis from
        a host that never answered: the first may be executing the request."""
        with pytest.raises(BridgeTimeout) as got_read:
            _net_bridge(TimeoutError("read timed out")).call("account", {})
        with pytest.raises(BridgeTimeout) as got_connect:
            _net_bridge(urllib.error.URLError(TimeoutError("connect"))).call(
                "account", {}
            )
        print(f"read: {got_read.value}\nconnect: {got_connect.value}")
        assert (got_read.value.transport, got_read.value.phase) == (
            TRANSPORT_NET,
            PHASE_READ,
        )
        assert (got_connect.value.transport, got_connect.value.phase) == (
            TRANSPORT_NET,
            PHASE_CONNECT,
        )
        assert got_read.value.phase != got_connect.value.phase

    def test_a_504_and_a_503_from_the_shim_are_not_one_fact(self) -> None:
        """The shim's two retryable refusals. `504` is the Expert not replying
        across a mailbox the shim CAN use; `503` is a mailbox it cannot use at
        all. Both wait, and an operator fixes them in different places."""
        with pytest.raises(BridgeTimeout) as got_504:
            _net_bridge(_http_error(504, b"ok=0\nerror=mailbox_timeout\n")).call(
                "positions", {}
            )
        with pytest.raises(BridgeTimeout) as got_503:
            _net_bridge(_http_error(503, b"ok=0\nerror=mailbox_unavailable\n")).call(
                "positions", {}
            )
        print(f"504: {got_504.value}\n503: {got_503.value}")
        assert (got_504.value.transport, got_504.value.phase) == (
            TRANSPORT_NET,
            PHASE_SHIM_MAILBOX,
        )
        assert (got_503.value.transport, got_503.value.phase) == (
            TRANSPORT_NET,
            PHASE_SHIM_UNAVAILABLE,
        )
        assert "op=positions" in str(got_504.value)

    def test_the_partition_is_total_and_every_pair_is_distinct(
        self, tmp_path: Path
    ) -> None:
        raised: list[BridgeTimeout] = []
        for make in (
            lambda: FileBridge(tmp_path, timeout_sec=0.05).call("ping", {}),
            lambda: _net_bridge(TimeoutError("read")).call("ping", {}),
            lambda: _net_bridge(urllib.error.URLError(TimeoutError("c"))).call(
                "ping", {}
            ),
            lambda: _net_bridge(_http_error(504, b"ok=0\n")).call("ping", {}),
            lambda: _net_bridge(_http_error(503, b"ok=0\n")).call("ping", {}),
        ):
            with pytest.raises(BridgeTimeout) as got:
                make()
            raised.append(got.value)
        pairs = [(e.transport, e.phase) for e in raised]
        print(f"pairs: {pairs}")
        assert len(set(pairs)) == len(pairs), f"two conditions collapsed: {pairs}"
        assert {p for _, p in pairs} == set(BRIDGE_TIMEOUT_PHASES), (
            "the phases this suite can actually produce are not the phases the "
            f"module declares: produced {sorted({p for _, p in pairs})}, "
            f"declared {sorted(BRIDGE_TIMEOUT_PHASES)}"
        )
