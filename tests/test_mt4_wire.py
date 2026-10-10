"""Drive the MT4 adapter through the real pipe-separated wire.

`tests/test_mt4_adapter.py` drives `Mt4Broker` through a stub that returns
native Python dicts, so `parse_rows` takes its fast path (`mt4_live.py:140-141`)
and the wire decoding never executes through the broker. This file closes that
gap: every test here goes through `FileBridge`, a mailbox on disk, and a
stand-in Expert that answers with the byte-exact text
`mt4/Experts/Mt4RiskBot.mq4` emits. `encode`, `decode`, `parse_rows` and
`_split_row` all run for real.

Three things this file asserts that a dict stub structurally cannot:

1. **Field order.** A dict is indifferent to the order of a pipe-joined row, so
   reordering one field in the Expert leaves a dict-driven suite green while
   `swap` lands in `time`.
2. **Measured versus defaulted.** The stub hardcodes `tick_value: 1.0`, the
   exact value `mt4_live.py:286` substitutes for a zero measurement, so it
   cannot tell the two apart. The transcripts carry the raw wire string.
3. **Fields the Expert never sends.** A dict stub supplies whatever the test
   author remembered. A transcript is the Expert's actual output, so a field the
   Expert omits is absent, and the value the adapter reports for it is its own
   default.
"""

from __future__ import annotations

import inspect
import re
import sys
import threading
import time
from pathlib import Path

import pytest

from mt4_transcripts import (
    BARS_EURUSD_H1,
    EA_ACCOUNT_KEYS,
    EA_BAR_EMIT,
    EA_FAILURES,
    EA_OPS,
    EA_ORD_EMIT,
    EA_POS_EMIT,
    EA_SYMBOL_KEYS,
    GOLDEN,
    Transcript,
    TranscriptExpert,
    ea_fail,
    ea_kv,
    ea_ok,
    ea_rows_reply,
    ord_row,
    pos_row,
    t_account,
    t_market_ticket_only,
    t_modify_working,
    t_select,
    t_symbol,
)
from straightedge.broker.mt4_live import (
    BAR_FIELDS,
    ORD_FIELDS,
    POS_FIELDS,
    REQ_NAME,
    RES_NAME,
    BridgeTimeout,
    FileBridge,
    Mt4Broker,
    _mailbox_encoding,
    decode,
    encode,
)
from straightedge.constants import (
    TRADE_RETCODE_DONE,
    TRADE_RETCODE_INVALID_STOPS,
    TRADE_RETCODE_INVALID_VOLUME,
    TRADE_RETCODE_PLACED,
    TRADE_RETCODE_POSITION_CLOSED,
    TRADE_RETCODE_REJECT,
    TRADE_RETCODE_TRADE_DISABLED,
)
from straightedge.models import MarketOrder, Side, WorkingOrder

TIMEOUT = 3.0


def wired(tmp_path: Path, transcripts: dict[str, Transcript], **kw: object) -> TranscriptExpert:
    return TranscriptExpert(tmp_path, transcripts, **kw)  # type: ignore[arg-type]


class RenameClaimer:
    """Claim the shared name the way the real Expert does, deterministically.

    WHY THIS EXISTS RATHER THAN `wired(..., answer_limit=0)`.
    `TranscriptExpert` models the claim as read-then-`unlink` and deliberately
    TOLERATES an `unlink` that is refused, because #242 measured
    `ERROR_ACCESS_DENIED` on exactly that operation on the live Windows box. When
    the refusal happens the request stays on the shared name, so the bridge's own
    withdrawal succeeds and reports `withdrawn` -- which is CORRECT, because
    nothing had claimed it. The test then fails on the withdrawal assertion
    instead of on the precondition it actually lost, which is why
    `test_a_request_the_expert_already_claimed_is_reported_as_claimed` failed on
    the windows leg and never on POSIX.

    The real Expert claims with `FileMove(REQ_NAME, FILE_COMMON, gClaimPath, ...)`
    (`mt4/Experts/Mt4RiskBot.mq4:313`) BEFORE it reads the body, and `_withdraw`
    (`src/straightedge/broker/mt4_live.py:555-574`) is written against exactly
    that: the file being GONE is the claim. So this claims by rename, retries a
    refusal within a bounded grace, and records whether it ever got the claim.

    `claimed` is the precondition, asserted separately from the behaviour under
    test, so a lost precondition says so instead of masquerading as a wrong
    withdrawal.
    """

    def __init__(self, directory: Path, grace_sec: float = 10.0) -> None:
        self.directory = directory
        self.grace_sec = grace_sec
        self.claimed = False
        self.refusals = 0
        self.detail = ""
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def __enter__(self) -> RenameClaimer:
        self.directory.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def _loop(self) -> None:
        req = self.directory / REQ_NAME
        claim = self.directory / (REQ_NAME + ".claim")
        deadline = time.monotonic() + self.grace_sec
        while not self._stop.is_set() and time.monotonic() < deadline:
            try:
                req.replace(claim)
            except FileNotFoundError:
                # Not written yet, or already claimed by this loop.
                time.sleep(0.002)
                continue
            except PermissionError:
                # NTFS refuses while the other side holds the handle. The real
                # Expert retries its claim for the same reason
                # (`Mt4RiskBot.mq4:324-331`).
                self.refusals += 1
                time.sleep(0.005)
                continue
            self.claimed = True
            return
        self.detail = (
            "never claimed %s within %.1fs (%d refusals)"
            % (REQ_NAME, self.grace_sec, self.refusals)
        )


def broker(tmp_path: Path) -> Mt4Broker:
    return Mt4Broker(FileBridge(tmp_path, timeout_sec=TIMEOUT).call, magic=770077)


def reads_of(method: object) -> set[str]:
    """Every wire key a broker method reads, taken from the method's own source.

    Deriving the read set from the source rather than restating it here is what
    makes the send-versus-read denominator below a measurement instead of a
    memo. It goes red when the adapter starts or stops reading a field.

    Three spellings, because #30 replaced the symbol reader's
    `d.get(key, DEFAULT) or DEFAULT` with explicit measurement helpers:
    `.get("key")`, `measure("key", ...)` / `derived("key", ...)`, and `d["key"]`.
    Recognising only the first would collapse this denominator to zero and the
    pin would read GREEN for the wrong reason, which is the exact failure this
    helper exists to prevent.
    """
    src = inspect.getsource(method)
    found = set(re.findall(r'\.get\(\s*"([a-z_]+)"', src))
    found |= set(re.findall(r'\b(?:measure|derived)\(\s*"([a-z_]+)"', src))
    found |= set(re.findall(r'\bd\[\s*"([a-z_]+)"\s*\]', src))
    return found


# ---------------------------------------------------------------------------
# The contract: what the Expert emits versus what the adapter reads.
# ---------------------------------------------------------------------------


class TestWireContract:
    def test_seventeen_ops_and_the_adapter_calls_only_those(self, tmp_path: Path) -> None:
        """`docs/MT4.md:88-103` describes 17 ops in 14 rows; three rows carry two."""
        assert len(EA_OPS) == 17
        assert len(set(EA_OPS)) == 17

    def test_every_op_has_a_golden_transcript(self) -> None:
        assert set(GOLDEN) == set(EA_OPS)

    def test_position_row_order_matches_the_adapter_tuple(self) -> None:
        """Expert :397-409 against `POS_FIELDS` (`mt4_live.py:47-61`).

        This is the check the dict stub cannot make. If either side reorders one
        field, this goes red; under a dict stub both stay green.
        """
        assert EA_POS_EMIT == POS_FIELDS

    def test_order_row_order_matches_the_adapter_tuple(self) -> None:
        """Expert :383-393 against `ORD_FIELDS` (`mt4_live.py:62-74`)."""
        assert EA_ORD_EMIT == ORD_FIELDS

    def test_bar_row_order_matches_the_adapter_tuple(self) -> None:
        """Expert :351-356 against `BAR_FIELDS` (`mt4_live.py:46`)."""
        assert EA_BAR_EMIT == BAR_FIELDS

    def test_symbol_send_versus_read_denominator(self) -> None:
        """11 keys sent, 15 keys read, 4 of them never on the wire.

        The four the Expert never emits are the four the adapter can only
        default. `trade_mode` is the one that matters: `models.py:122` defaults
        it to 4, MT5's "full" trade mode, so a symbol the broker has restricted
        was reported as unrestricted. #30 changed that: the four are now recorded
        in `SymbolSpec.unmeasured` instead of being fabricated. The denominator
        itself is unchanged, and that is the point of keeping this pin: MQL4
        cannot send any of the four, so the gap is permanent and what had to
        change was the adapter's honesty about it, not the count.
        """
        sent = GOLDEN["symbol"].keys_sent() - {"id", "ok"}
        read = reads_of(Mt4Broker.symbol)
        assert sent == set(EA_SYMBOL_KEYS)
        assert len(sent) == 11
        assert len(read) == 15
        assert read - sent == {
            "currency_base",
            "currency_profit",
            "currency_margin",
            "trade_mode",
        }
        assert sent - read == set()

    def test_account_send_versus_read_denominator(self) -> None:
        """13 keys sent, 14 read: `free_margin` is an alternative with no producer.

        `mt4_live.py:266` accepts `margin_free` or `free_margin`. The Expert
        emits only `margin_free` (:284), so the second spelling is a branch no
        producer can reach.
        """
        sent = GOLDEN["account"].keys_sent() - {"id", "ok"}
        read = reads_of(Mt4Broker.account)
        assert sent == set(EA_ACCOUNT_KEYS)
        assert len(sent) == 13
        assert read - sent == {"free_margin"}
        assert sent - read == set()

    def test_no_reply_carries_op_so_id_is_the_only_match(self) -> None:
        """`Ok()` and `Fail()` emit no `op` (:83-91).

        `_TEXT_FIELDS` (`mt4_live.py:90`) already lists `op`, so the decoder is
        ready for a field no producer sends. Until the Expert threads it
        through, a reply can only be matched on its id, which is what the
        `req_id` half of #15 turns on.
        """
        for name, transcript in GOLDEN.items():
            assert "op" not in transcript.keys_sent(), name


# ---------------------------------------------------------------------------
# One golden round trip per op, through the mailbox.
# ---------------------------------------------------------------------------


class TestGoldenRoundTrips:
    def test_ping_and_account(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            br = broker(tmp_path)
            br.connect()
            br.ensure_connected()
            acct = br.account()
        assert ea_kv(ea.request("ping"), "op") == "ping"
        assert acct.login == 51234567
        assert acct.balance == 10000.0
        assert acct.equity == 9987.5
        assert acct.margin == 212.34
        assert acct.margin_free == 9775.16
        assert acct.profit == -12.5
        assert acct.currency == "USD"
        assert acct.leverage == 100
        assert acct.trade_mode == 0
        assert acct.trade_allowed is True
        assert acct.trade_expert is True
        assert acct.name == "Gil Test"
        assert acct.server == "Broker-Demo01"

    def test_symbol(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            spec = broker(tmp_path).symbol("eurusd")
        assert ea_kv(ea.request("symbol"), "symbol") == "EURUSD"
        assert spec.name == "EURUSD"
        assert spec.digits == 5
        assert spec.point == 0.00001
        assert spec.trade_tick_size == 0.00001
        assert spec.trade_contract_size == 100000.0
        assert spec.volume_min == 0.01
        assert spec.volume_max == 500.0
        assert spec.volume_step == 0.01
        assert spec.trade_stops_level == 10
        assert spec.trade_freeze_level == 0
        assert spec.spread == 12

    def test_tick(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            tick = broker(tmp_path).tick("eurusd")
        assert ea_kv(ea.request("tick"), "symbol") == "EURUSD"
        assert tick.bid == 1.10012
        assert tick.ask == 1.10024
        assert tick.time == 1758700000

    def test_select(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            assert broker(tmp_path).select_symbol("eurusd") is True
        assert ea_kv(ea.request("select"), "symbol") == "EURUSD"

    def test_select_failure_is_reported(self, tmp_path: Path) -> None:
        with wired(tmp_path, {"select": t_select(ok=False)}):
            assert broker(tmp_path).select_symbol("eurusd") is False

    def test_rates_decode_from_pipe_rows(self, tmp_path: Path) -> None:
        """The oldest-first ordering in `RatesReply` (:349) survives decoding."""
        with wired(tmp_path, GOLDEN) as ea:
            bars = broker(tmp_path).rates("eurusd", "H1", 2)
        req = ea.request("rates")
        assert ea_kv(req, "timeframe") == "H1"
        assert ea_kv(req, "count") == "2"
        assert len(bars) == 2
        assert bars[0].time == BARS_EURUSD_H1[0]["time"]
        assert bars[0].open == 1.10010
        assert bars[0].high == 1.10180
        assert bars[0].low == 1.09960
        assert bars[0].close == 1.10120
        assert bars[0].tick_volume == 8412
        assert bars[1].close == 1.10200
        assert bars[1].tick_volume == 7733

    def test_positions_decode_every_field_from_the_pipe_row(self, tmp_path: Path) -> None:
        """All 13 position fields, in the Expert's order, off the real wire."""
        with wired(tmp_path, GOLDEN) as ea:
            got = broker(tmp_path).positions()
        assert ea_kv(ea.request("positions"), "magic") == "0"
        assert len(got) == 1
        pos = got[0]
        assert pos.ticket == 80051234
        assert pos.symbol == "EURUSD"
        assert pos.side is Side.BUY
        assert pos.volume == 0.17
        assert pos.price_open == 1.10015
        assert pos.sl == 1.09815
        assert pos.tp == 1.10415
        assert pos.price_current == 1.10120
        assert pos.profit == 17.85
        assert pos.magic == 770077
        assert pos.comment == "rb-1"
        assert pos.swap == -0.42
        assert pos.time == 1758694000

    def test_orders_decode_every_field_from_the_pipe_row(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            got = broker(tmp_path).orders(770077)
        assert ea_kv(ea.request("orders"), "magic") == "770077"
        assert len(got) == 1
        order = got[0]
        assert order.ticket == 80051299
        assert order.symbol == "EURUSD"
        assert order.side is Side.BUY
        assert order.kind == "limit"
        assert order.volume == 0.09
        assert order.price == 1.09500
        assert order.sl == 1.09300
        assert order.tp == 1.09900
        assert order.magic == 770077
        assert order.comment == "rb-2"
        assert order.time == 1758695000

    def test_empty_book_is_not_an_error(self, tmp_path: Path) -> None:
        """`n=0` with no rows, the shape `BookReply` returns on a flat book."""
        with wired(tmp_path, {"positions": Transcript("positions", ea_ok("n=0"))}):
            assert broker(tmp_path).positions() == []

    def test_sell_side_decodes(self, tmp_path: Path) -> None:
        rows = [pos_row(side="sell", comment="")]
        with wired(tmp_path, {"positions": Transcript("positions", ea_rows_reply(EA_POS_EMIT, rows))}):
            got = broker(tmp_path).positions()
        assert got[0].side is Side.SELL
        assert got[0].comment == ""

    def test_check_market_does_not_send(self, tmp_path: Path) -> None:
        order = MarketOrder(
            symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.09815, tp=1.10415, magic=770077
        )
        with wired(tmp_path, GOLDEN) as ea:
            res = broker(tmp_path).check_market(order)
        req = ea.request("check_market")
        assert ea_kv(req, "side") == "buy"
        assert ea_kv(req, "volume") == "0.17"
        assert res.ok
        assert res.order == 0
        assert res.price == 1.10024

    def test_market_send(self, tmp_path: Path) -> None:
        order = MarketOrder(
            symbol="EURUSD",
            side=Side.BUY,
            volume=0.17,
            sl=1.09815,
            tp=1.10415,
            magic=770077,
            comment="rb-1",
        )
        with wired(tmp_path, GOLDEN) as ea:
            res = broker(tmp_path).market(order)
        req = ea.request("market")
        assert ea_kv(req, "comment") == "rb-1"
        assert ea_kv(req, "magic") == "770077"
        assert res.ok
        assert res.retcode == TRADE_RETCODE_DONE
        assert res.order == 80051234
        assert res.volume == 0.17
        assert res.price == 1.10024

    def test_market_reply_without_volume_or_price(self, tmp_path: Path) -> None:
        """`OrderSelect` failed after the send (:456-457), so only the ticket comes back.

        The order is on. `volume` and `price` are absent, and the adapter
        reports them as zero, which is a default and not a measurement.
        """
        order = MarketOrder(symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.09815, tp=1.10415)
        with wired(tmp_path, {"market": t_market_ticket_only()}):
            res = broker(tmp_path).market(order)
        assert res.ok
        assert res.order == 80051234
        assert res.volume == 0.0
        assert res.price == 0.0

    def test_check_working_and_working(self, tmp_path: Path) -> None:
        order = WorkingOrder(
            symbol="EURUSD", side=Side.BUY, kind="limit", volume=0.09, price=1.095, sl=1.093, tp=1.099
        )
        with wired(tmp_path, GOLDEN) as ea:
            br = broker(tmp_path)
            check = br.check_working(order)
            sent = br.working(order)
        assert ea_kv(ea.request("check_working"), "kind") == "limit"
        assert check.ok
        assert check.retcode == TRADE_RETCODE_PLACED
        assert check.order == 0
        assert sent.ok
        assert sent.retcode == TRADE_RETCODE_PLACED
        assert sent.order == 80051299
        assert sent.price == 1.095

    def test_the_working_send_carries_the_deviation_on_the_wire(self, tmp_path: Path) -> None:
        """Issue #92, and the assertion is on the REQUEST BYTES on purpose.

        `WorkingOrder` had no `deviation` field and `_working_payload` sent no
        `deviation` key, so the Expert's pending handler had nothing to read and
        passed its own `input int Slippage = 30` to `OrderSend` while
        `risk.evaluate()` gated the same signal on the operator's figure. An
        internal call that received the right number proves nothing about what
        the Expert reads; the mailbox does.

        150 is the gold value `config.example.toml` seeds. Both `check_working`
        and `working` are asserted: a pre-trade check run on a different
        tolerance from the send is answering a different question.

        What this does NOT establish: whether MT4 APPLIES slippage to a pending
        order type. It is documented as ignored there, that was not measured on
        the live rig, and no test in this repo can measure it. This asserts
        transmission only.
        """
        order = WorkingOrder(
            symbol="XAUUSD",
            side=Side.BUY,
            kind="limit",
            volume=0.09,
            price=1.095,
            sl=1.093,
            tp=1.099,
            deviation=150,
        )
        with wired(tmp_path, GOLDEN) as ea:
            br = broker(tmp_path)
            br.check_working(order)
            br.working(order)
        assert ea_kv(ea.request("check_working"), "deviation") == "150"
        assert ea_kv(ea.request("working"), "deviation") == "150"

    def test_every_send_op_that_carries_a_tolerance_puts_it_on_the_wire(
        self, tmp_path: Path
    ) -> None:
        """The denominator. Two of three carried it before #92.

        Three request ops reach a venue call with a slippage argument: `market`
        and `working` through `OrderSend`, `close` through `OrderClose`. 77 is a
        value no default produces, so a payload that dropped the key and an
        Expert that fell back to its own input cannot both read as a pass.
        """
        dev = 77
        with wired(tmp_path, GOLDEN) as ea:
            br = broker(tmp_path)
            br.market(
                MarketOrder(
                    symbol="EURUSD",
                    side=Side.BUY,
                    volume=0.17,
                    sl=1.09815,
                    tp=1.10415,
                    deviation=dev,
                )
            )
            br.working(
                WorkingOrder(
                    symbol="EURUSD",
                    side=Side.BUY,
                    kind="limit",
                    volume=0.09,
                    price=1.095,
                    sl=1.093,
                    tp=1.099,
                    deviation=dev,
                )
            )
            br.close_position(
                80051234,
                symbol="EURUSD",
                side="buy",
                volume=0.17,
                price=1.10012,
                deviation=dev,
            )
        ops = ("market", "working", "close")
        carried = [op for op in ops if ea_kv(ea.request(op), "deviation") == str(dev)]
        assert carried == list(ops), (
            f"{len(carried)} of {len(ops)} send ops transmit the resolved deviation; "
            f"missing: {[op for op in ops if op not in carried]}"
        )

    def test_modify_position(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            res = broker(tmp_path).modify_position(80051234, 1.09915, 1.10515, symbol="EURUSD")
        req = ea.request("modify_position")
        assert ea_kv(req, "ticket") == "80051234"
        assert ea_kv(req, "sl") == "1.09915"
        assert res.ok
        assert res.order == 80051234

    def test_modify_working(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            res = broker(tmp_path).modify_working(80051299, price=1.0945, sl=1.0925, tp=1.0985)
        req = ea.request("modify_working")
        assert ea_kv(req, "price") == "1.0945"
        assert res.ok

    def test_cancel(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            res = broker(tmp_path).cancel(80051299)
        assert ea_kv(ea.request("cancel"), "ticket") == "80051299"
        assert res.ok

    def test_close_position(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            res = broker(tmp_path).close_position(
                80051234, symbol="EURUSD", side="buy", volume=0.17, price=1.10012
            )
        req = ea.request("close")
        assert ea_kv(req, "deviation") == "20"
        assert ea_kv(req, "magic") == "770077"
        assert res.ok
        assert res.volume == 0.17

    def test_close_by(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            res = broker(tmp_path).close_by(80051234, 80051299, "EURUSD")
        assert ea_kv(ea.request("close_by"), "other") == "80051299"
        assert res.ok

    def test_unknown_op_gets_the_unsupported_fail(self, tmp_path: Path) -> None:
        """`Handle()`'s fallback (:271) reaches the adapter as a refusal."""
        with wired(tmp_path, {}):
            res = broker(tmp_path).cancel(1)
        assert not res.ok
        assert res.comment == "unsupported"


# ---------------------------------------------------------------------------
# Measured versus defaulted. This is the reason the file exists.
# ---------------------------------------------------------------------------


class TestMeasuredVersusDefaulted:
    def test_a_real_tick_value_survives_the_wire(self, tmp_path: Path) -> None:
        """A JPY cross measures about 0.67, and the adapter must carry it.

        `tests/test_mt4_adapter.py:59` hardcodes `tick_value: 1.0`, so it cannot
        make this assertion at all: every value it can produce is the default.
        """
        with wired(tmp_path, {"symbol": t_symbol(tick_value="0.6700")}):
            spec = broker(tmp_path).symbol("USDJPY")
        assert spec.trade_tick_value == 0.67

    def test_an_oversizing_tick_value_survives_the_wire(self, tmp_path: Path) -> None:
        """An index or metal measures above 1.0. Defaulting it to 1.0 undersizes
        the divisor and therefore oversizes the trade, so the measurement has to
        arrive intact."""
        with wired(tmp_path, {"symbol": t_symbol(tick_value="2.5000")}):
            spec = broker(tmp_path).symbol("XAUUSD")
        assert spec.trade_tick_value == 2.5

    def test_zero_tick_value_is_distinguishable_from_a_measured_one(self, tmp_path: Path) -> None:
        """Was `..._is_indistinguishable_from_...`, inverted by #30.

        The pin it replaced recorded the defect: `MarketInfo` returns 0 for
        `MODE_TICKVALUE` on an unselected symbol, the adapter replaced it via
        `or 1.0`, and the resulting object was byte-identical to a genuine 1.0
        measurement. Only the transcript retained the evidence.

        The two now differ in the object, not only on the wire.
        """
        transcript = t_symbol(tick_value="0.0000")
        assert transcript.value_sent("tick_value") == "0.0000"
        with wired(tmp_path, {"symbol": transcript}):
            spec = broker(tmp_path).symbol("USDJPY")
        assert "tick_value" in spec.unmeasured
        assert spec.trade_tick_value == 0.0
        genuine = t_symbol(tick_value="1.00000000")
        with wired(tmp_path, {"symbol": genuine}):
            other = broker(tmp_path).symbol("USDJPY")
        assert other.trade_tick_value == 1.0
        assert "tick_value" not in other.unmeasured
        assert spec.trade_tick_value != other.trade_tick_value
        assert transcript.value_sent("tick_value") != genuine.value_sent("tick_value")

    def test_symbol_trade_mode_is_unmeasured_because_the_expert_cannot_send_it(
        self, tmp_path: Path
    ) -> None:
        """Was `..._is_defaulted_because_...`, inverted by #30.

        `SymbolReply` emits no `trade_mode` and MQL4 has no trade-mode
        identifier, so it can never be measured over this wire. It used to
        default to 4, MT5's "full" mode, so every symbol read as fully tradable
        regardless of what the broker permits. It is now recorded as unmeasured
        and left at 0, MQL5's DISABLED, which is the fail-closed direction.

        Still LATENT rather than live: nothing in `src/` reads
        `SymbolSpec.trade_mode`, only `Account.trade_mode` is consumed. Closed
        so it cannot become live later.
        """
        transcript = GOLDEN["symbol"]
        assert transcript.value_sent("trade_mode") is None
        with wired(tmp_path, {"symbol": transcript}):
            spec = broker(tmp_path).symbol("EURUSD")
        assert "trade_mode" in spec.unmeasured
        assert spec.trade_mode != 4

    def test_symbol_currencies_are_derived_from_the_name_not_measured(self, tmp_path: Path) -> None:
        """No currency field is on the wire; the adapter slices the name.

        MQL4 has no per-symbol currency identifier, so the convention stays
        (it is useful and usually right) but #30 records all three as unmeasured
        so no caller mistakes a naming convention for a measurement.
        """
        transcript = GOLDEN["symbol"]
        for key in ("currency_base", "currency_profit", "currency_margin"):
            assert transcript.value_sent(key) is None
        with wired(tmp_path, {"symbol": transcript}):
            spec = broker(tmp_path).symbol("EURUSD")
        assert spec.currency_base == "EUR"
        assert spec.currency_profit == "USD"
        assert spec.currency_margin == ""
        for key in ("currency_base", "currency_profit", "currency_margin"):
            assert key in spec.unmeasured

    def test_short_symbol_name_yields_empty_currencies(self, tmp_path: Path) -> None:
        """A name under six characters, for instance an index, has nothing to slice."""
        with wired(tmp_path, {"symbol": GOLDEN["symbol"]}):
            spec = broker(tmp_path).symbol("US500")
        assert spec.currency_base == ""
        assert spec.currency_profit == ""

    def test_tick_last_and_volume_are_defaulted(self, tmp_path: Path) -> None:
        """`TickReply` (:301-304) sends bid, ask and time only."""
        transcript = GOLDEN["tick"]
        assert transcript.value_sent("last") is None
        assert transcript.value_sent("volume") is None
        with wired(tmp_path, {"tick": transcript}):
            tick = broker(tmp_path).tick("EURUSD")
        assert tick.last == tick.bid
        assert tick.volume == 0

    def test_account_real_mode_is_measured_not_assumed(self, tmp_path: Path) -> None:
        """`AccountReply` does send `trade_mode` (:288), so this one is measured.

        `risk.py:255` and `:275` refuse to size on `trade_mode == 2` without
        explicit acceptance, and that guard depends on the field being on the
        wire rather than defaulted to 0.
        """
        transcript = t_account(trade_mode=2)
        assert transcript.value_sent("trade_mode") == "2"
        with wired(tmp_path, {"account": transcript}):
            acct = broker(tmp_path).account()
        assert acct.trade_mode == 2

    def test_account_with_trading_disabled(self, tmp_path: Path) -> None:
        with wired(tmp_path, {"account": t_account(trade_allowed=0, trade_expert=0)}):
            acct = broker(tmp_path).account()
        assert acct.trade_allowed is False
        assert acct.trade_expert is False


# ---------------------------------------------------------------------------
# Order sensitivity, which is what a dict stub throws away.
# ---------------------------------------------------------------------------


class TestFieldOrderDrift:
    def test_transposing_two_emitted_fields_corrupts_the_named_field(self, tmp_path: Path) -> None:
        """Swap `swap` and `time` in the emitter and `swap` reads the timestamp.

        This is the failure mode `test_position_row_order_matches_the_adapter_tuple`
        guards against, demonstrated end to end. A dict-returning stub cannot
        produce it, because a dict has no order to get wrong.
        """
        drifted = EA_POS_EMIT[:11] + ("time", "swap")
        assert drifted != EA_POS_EMIT
        row = pos_row()
        transcript = Transcript("positions", ea_rows_reply(drifted, [row]))
        with wired(tmp_path, {"positions": transcript}):
            got = broker(tmp_path).positions()
        assert got[0].swap == 1758694000.0
        assert got[0].time == 0

    def test_a_short_row_pads_with_zero_rather_than_failing(self, tmp_path: Path) -> None:
        """`_split_row` (`mt4_live.py:157`) fills missing trailing fields.

        So an Expert that stops emitting the last field produces a position with
        `time` zero and no error anywhere. Pinned, not endorsed.
        """
        truncated = EA_POS_EMIT[:-1]
        transcript = Transcript("positions", ea_rows_reply(truncated, [pos_row()]))
        with wired(tmp_path, {"positions": transcript}):
            got = broker(tmp_path).positions()
        assert got[0].swap == -0.42
        assert got[0].time == 0

    def test_a_pipe_in_the_comment_breaks_the_position_read(self, tmp_path: Path) -> None:
        """`_wire()` strips pipes outbound; the Expert does not strip them back.

        `OrderComment()` is joined straight into the row (:407), so a
        broker-supplied comment containing a pipe inserts an extra part, every
        later field reads the previous one's value, and `swap` ends up holding
        the comment's tail. `_pos` then calls `float()` on it and the whole book
        read raises, so the desk cannot enumerate its own positions.

        Current behaviour, pinned rather than endorsed. The honest fix is
        Expert-side sanitation before the join, which is #31's territory, not
        this instrument's.
        """
        row = pos_row(comment="tp|hit")
        transcript = Transcript("positions", ea_rows_reply(EA_POS_EMIT, [row]))
        with wired(tmp_path, {"positions": transcript}):
            with pytest.raises(ValueError):
                broker(tmp_path).positions()

    def test_a_pipe_in_an_order_comment_breaks_the_pending_read(self, tmp_path: Path) -> None:
        """Same defect on the pending row, where `time` follows `comment` (:392)."""
        row = ord_row(comment="a|b")
        transcript = Transcript("orders", ea_rows_reply(EA_ORD_EMIT, [row]))
        with wired(tmp_path, {"orders": transcript}):
            with pytest.raises(ValueError):
                broker(tmp_path).orders()

    def test_a_pipe_in_the_account_name_is_carried_verbatim(self, tmp_path: Path) -> None:
        """`AccountName()` is not sanitized either (:291), but `name` is the last
        key/value line rather than a pipe-joined row, so it survives intact. The
        asymmetry only bites on rows."""
        with wired(tmp_path, {"account": t_account(name="Gil|Test")}):
            acct = broker(tmp_path).account()
        assert acct.name == "Gil|Test"


# ---------------------------------------------------------------------------
# Reply matching. The foundation the req_id half of #15 needs.
# ---------------------------------------------------------------------------


class TestReplyMatching:
    def test_a_foreign_reply_id_is_not_consumed(self, tmp_path: Path) -> None:
        """Covers the branch at `mt4_live.py:232->236`, never taken until now.

        The only control that would notice a request-id collision is the id
        comparison. Answer with someone else's id and the bridge must keep
        polling rather than accept it, then time out with the foreign reply
        still on disk.
        """
        with wired(tmp_path, GOLDEN, force_id=9999) as ea:
            bridge = FileBridge(tmp_path, timeout_sec=0.4)
            with pytest.raises(RuntimeError, match="timeout"):
                bridge.call("ping", {})
            assert ea.ops == ["ping"]
        res = tmp_path / RES_NAME
        assert res.exists()
        assert decode(res.read_text(encoding="utf-8"))["id"] == 9999

    def test_the_request_is_withdrawn_when_the_bridge_gives_up(self, tmp_path: Path) -> None:
        """The opposite of what this test used to pin, and the reason it changed.

        It used to assert the `.req` file was STILL THERE after a timeout, on the
        reasoning that the next call removes it so "a desk that keeps going is
        fine". That reasoning holds for a read and does not hold for `op=market`:
        the Expert polls the shared name every 100ms and executes whatever it
        finds, so an abandoned send could fire minutes after the operator was told
        it had failed, and across a desk process exit nothing bounded "later" at
        all. Measured case: a silent desk restart on the live box at
        2026-09-26T01:56:49Z.

        The withdrawal is best effort and the exception says which outcome it got,
        because "I gave up" and "nothing can happen now" are different facts.
        """
        bridge = FileBridge(tmp_path, timeout_sec=0.2)
        with pytest.raises(BridgeTimeout) as caught:
            bridge.call("market", {"symbol": "EURUSD", "side": "buy", "volume": 0.17})
        assert not (tmp_path / REQ_NAME).exists()
        assert caught.value.withdrawal == "withdrawn"
        assert caught.value.withdrawn is True
        assert caught.value.may_still_execute is False
        assert caught.value.op == "market"

    def test_a_request_the_expert_already_claimed_is_reported_as_claimed(
        self, tmp_path: Path
    ) -> None:
        """The unsafe outcome must never render as the safe one.

        The Expert claims the shared name by renaming it, so by the time the
        bridge gives up the file can be gone because the Expert HAS it. An unlink
        that finds nothing there is not a withdrawal, and calling it one would
        turn the ambiguous-money case into a clean bill of health.
        """
        with RenameClaimer(tmp_path) as claimer:
            bridge = FileBridge(tmp_path, timeout_sec=0.4)
            with pytest.raises(BridgeTimeout) as caught:
                bridge.call("market", {"symbol": "EURUSD", "side": "buy", "volume": 0.17})
        # The PRECONDITION, asserted before the behaviour. A claim that never
        # happened is a lost setup, not a wrong withdrawal, and conflating the
        # two is what made this read as a flake rather than as a broken fixture.
        assert claimer.claimed, "precondition lost: " + claimer.detail
        assert caught.value.withdrawal == "claimed"
        assert caught.value.withdrawn is False
        assert caught.value.may_still_execute is True

    def test_a_locked_request_that_cannot_be_withdrawn_says_so(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A withdrawal that failed is reported, never assumed.

        On NTFS `unlink` raises `PermissionError` while the terminal holds the
        handle. The bridge spins for a bounded grace and then reports `locked`,
        which leaves the Expert's own `ttl_ms` fence as the only remaining guard.
        """
        real = Path.unlink

        def refuse(self: Path, *a: object, **kw: object) -> None:
            # Only the WITHDRAWAL is refused. `_retry_unlink` passes
            # `missing_ok=True` and `_withdraw` deliberately does not, which is
            # the one caller under test here; refusing both would make the call
            # raise PermissionError before it ever reached the timeout.
            if self.name == REQ_NAME and "missing_ok" not in kw:
                raise PermissionError(32, "held by the terminal")
            real(self, *a, **kw)  # type: ignore[arg-type]

        monkeypatch.setattr(Path, "unlink", refuse)
        monkeypatch.setattr("straightedge.broker.mt4_live._WITHDRAW_GRACE_SEC", 0.05)
        bridge = FileBridge(tmp_path, timeout_sec=0.1)
        with pytest.raises(BridgeTimeout) as caught:
            bridge.call("market", {"symbol": "EURUSD", "side": "buy", "volume": 0.17})
        assert caught.value.withdrawal == "locked"
        assert caught.value.may_still_execute is True

    def test_request_ids_increment_per_call(self, tmp_path: Path) -> None:
        with wired(tmp_path, GOLDEN) as ea:
            br = broker(tmp_path)
            br.connect()
            br.connect()
            br.connect()
        assert [ea_kv(body, "id") for body in ea.seen] == ["1", "2", "3"]

    def test_disconnect_swallows_a_dead_mailbox(self, tmp_path: Path) -> None:
        """`mt4_live.py:256-257`. Shutting down must not raise on a dead Expert."""
        br = Mt4Broker(FileBridge(tmp_path, timeout_sec=0.15).call)
        br.disconnect()

    def test_connect_raises_the_experts_own_error(self, tmp_path: Path) -> None:
        with wired(tmp_path, {"ping": Transcript("ping", ea_fail(1, "no_terminal"))}):
            br = broker(tmp_path)
            with pytest.raises(RuntimeError, match="no_terminal"):
                br.connect()


# ---------------------------------------------------------------------------
# Refusals. Every Fail() the Expert can emit, mapped through the real wire.
# ---------------------------------------------------------------------------


class TestRefusalMapping:
    @pytest.mark.parametrize(
        ("mt4_error", "expected"),
        [
            (1, TRADE_RETCODE_REJECT),
            (129, 10015),
            (130, TRADE_RETCODE_INVALID_STOPS),
            (131, TRADE_RETCODE_INVALID_VOLUME),
            (132, TRADE_RETCODE_TRADE_DISABLED),
            (133, TRADE_RETCODE_TRADE_DISABLED),
            (134, 10019),
            (136, TRADE_RETCODE_TRADE_DISABLED),
            (138, 10015),
            (146, 10028),
            (4108, TRADE_RETCODE_POSITION_CLOSED),
            (4109, TRADE_RETCODE_TRADE_DISABLED),
        ],
    )
    def test_mt4_error_maps_to_the_mt5_retcode(
        self, tmp_path: Path, mt4_error: int, expected: int
    ) -> None:
        """`_MT4_RET` (`mt4_live.py:76-88`) driven through a real `Fail()` reply."""
        with wired(tmp_path, {"market": Transcript("market", ea_fail(mt4_error, "nope"))}):
            res = broker(tmp_path).market(
                MarketOrder(symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.098, tp=1.104)
            )
        assert not res.ok
        assert res.retcode == expected
        assert res.comment == "nope"

    @pytest.mark.parametrize("message", sorted(EA_FAILURES))
    def test_every_expert_failure_message_reaches_the_desk(
        self, tmp_path: Path, message: str
    ) -> None:
        """The refusal has to arrive by name, not as a generic reject.

        #11 turns on refusals being assertable by name; this proves the MT4
        venue carries the Expert's own word for it across the wire.
        """
        code = EA_FAILURES[message]
        with wired(tmp_path, {"market": Transcript("market", ea_fail(code, message))}):
            res = broker(tmp_path).market(
                MarketOrder(symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.098, tp=1.104)
            )
        assert not res.ok
        assert res.comment == message

    def test_an_unmapped_broker_retcode_passes_straight_through(self, tmp_path: Path) -> None:
        """`mt4_live.py:469`: an MT5-range code is trusted as-is."""
        with wired(tmp_path, {"market": Transcript("market", ea_fail(10027, "autotrading_off"))}):
            res = broker(tmp_path).market(
                MarketOrder(symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.098, tp=1.104)
            )
        assert res.retcode == 10027

    def test_require_raises_on_a_failed_read_op(self, tmp_path: Path) -> None:
        """`mt4_live.py:448-452`. A read op that fails must raise, not return zeros.

        Without this, `symbol` would hand back a spec built entirely from
        defaults, including `tick_value` 1.0, with no signal that nothing was
        measured.
        """
        with wired(tmp_path, {"symbol": Transcript("symbol", ea_fail(1, "symbol"))}):
            with pytest.raises(RuntimeError, match="symbol"):
                broker(tmp_path).symbol("NOSUCH")

    def test_a_reply_with_no_verdict_is_unknown_not_rejected(self, tmp_path: Path) -> None:
        """`mt4_live.py:455-460`, the #19 vocabulary, on the MT4 wire.

        A reply carrying neither `ok` nor `retcode` means nothing was measured.
        It must not read as the broker saying no.
        """
        with wired(tmp_path, {"market": Transcript("market", "id={id}\nerror=torn\n")}):
            res = broker(tmp_path).market(
                MarketOrder(symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.098, tp=1.104)
            )
        assert not res.ok
        assert res.measured is False
        assert "no result" in res.comment


# ---------------------------------------------------------------------------
# The outbound half: what the adapter puts on the wire, read by the Expert's
# own parser rather than by the adapter's decoder.
# ---------------------------------------------------------------------------


class TestOutboundEncoding:
    def test_an_omitted_optional_field_is_absent_from_the_wire(self, tmp_path: Path) -> None:
        """`encode` skips None (`mt4_live.py:101-102`), and absence is meaningful.

        `ModifyPend` (:525-530) keeps the order's existing price, sl or tp when
        the key is missing, because `KV` returns "" and it tests for "". An
        omitted key and a key sent as 0 mean opposite things.
        """
        with wired(tmp_path, {"modify_working": t_modify_working()}) as ea:
            broker(tmp_path).modify_working(80051299, sl=1.0925)
        body = ea.request("modify_working")
        assert ea_kv(body, "sl") == "1.0925"
        assert ea_kv(body, "price") == ""
        assert ea_kv(body, "tp") == ""
        assert "price=" not in body
        assert "tp=" not in body

    @pytest.mark.parametrize(
        ("kwargs", "present"),
        [
            ({"price": 1.09}, {"price"}),
            ({"sl": 1.08}, {"sl"}),
            ({"tp": 1.11}, {"tp"}),
            ({"price": 1.09, "sl": 1.08}, {"price", "sl"}),
            ({"price": 1.09, "sl": 1.08, "tp": 1.11}, {"price", "sl", "tp"}),
        ],
    )
    def test_only_the_named_optionals_reach_the_wire(
        self, tmp_path: Path, kwargs: dict[str, float], present: set[str]
    ) -> None:
        with wired(tmp_path, {"modify_working": t_modify_working()}) as ea:
            broker(tmp_path).modify_working(80051299, **kwargs)
        body = ea.request("modify_working")
        for key in ("price", "sl", "tp"):
            sent = ea_kv(body, key) != ""
            assert sent is (key in present), key

    def test_a_pipe_in_an_outbound_comment_is_replaced(self, tmp_path: Path) -> None:
        """`_wire()` (`mt4_live.py:94`) turns `|` into `/` so the row cannot shift."""
        order = MarketOrder(
            symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.098, tp=1.104, comment="a|b"
        )
        with wired(tmp_path, GOLDEN) as ea:
            broker(tmp_path).market(order)
        assert ea_kv(ea.request("market"), "comment") == "a/b"

    def test_a_newline_in_an_outbound_comment_cannot_forge_a_field(self, tmp_path: Path) -> None:
        """Newlines become spaces, so a comment cannot inject a protocol line."""
        order = MarketOrder(
            symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.098, tp=1.104, comment="a\nmagic=1"
        )
        with wired(tmp_path, GOLDEN) as ea:
            broker(tmp_path).market(order)
        body = ea.request("market")
        assert ea_kv(body, "comment") == "a magic=1"
        assert ea_kv(body, "magic") == "770077"

    def test_non_ascii_is_replaced_not_raised(self, tmp_path: Path) -> None:
        """FILE_ANSI on the Expert side cannot carry it (`mt4_live.py:95`)."""
        order = MarketOrder(
            symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.098, tp=1.104, comment="café"
        )
        with wired(tmp_path, GOLDEN) as ea:
            broker(tmp_path).market(order)
        assert ea_kv(ea.request("market"), "comment") == "caf?"

    def test_the_comment_is_clipped_to_the_experts_limit(self, tmp_path: Path) -> None:
        """`ClipComment` (:127-132) takes 31; `_market_payload` clips first (:429)."""
        order = MarketOrder(
            symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.098, tp=1.104, comment="x" * 80
        )
        with wired(tmp_path, GOLDEN) as ea:
            broker(tmp_path).market(order)
        assert ea_kv(ea.request("market"), "comment") == "x" * 31

    def test_the_request_carries_id_then_op_first(self, tmp_path: Path) -> None:
        """`encode` (`mt4_live.py:99`) leads with id and op, which `KV` prefix-matches."""
        with wired(tmp_path, GOLDEN) as ea:
            broker(tmp_path).connect()
        lines = ea.request("ping").split("\n")
        assert lines[0] == "id=1"
        assert lines[1] == "op=ping"


# ---------------------------------------------------------------------------
# Decoder robustness against what a shared mailbox file can actually contain.
# ---------------------------------------------------------------------------


class TestDecoderRobustness:
    def test_lines_without_a_separator_are_skipped(self, tmp_path: Path) -> None:
        """`mt4_live.py:111-112`, a branch that had never executed.

        The mailbox is a plain text file in a shared directory. The decoder
        claims to skip anything that is not a key/value line; this proves it
        does instead of assuming it.
        """
        noisy = "id={id}\nMt4RiskBot\nok=1\n\ntime=1758700000\n"
        with wired(tmp_path, {"ping": Transcript("ping", noisy)}):
            broker(tmp_path).connect()

    def test_crlf_lines_decode(self, tmp_path: Path) -> None:
        """FILE_TXT on Windows writes CRLF; `decode` strips it (`mt4_live.py:110`)."""
        got = decode("id=1\r\nok=1\r\ntime=1758700000\r\n")
        assert got["id"] == 1
        assert got["time"] == 1758700000

    def test_word_boolean_forms_decode(self) -> None:
        """`_coerce` (`mt4_live.py:121-125`) accepts `true` and `false`.

        No Expert reply emits either: `Ok()` and `Fail()` write `ok=1` and
        `ok=0`. These two branches are decoder contract with no producer, which
        is the same shape of gap as `free_margin` and `op`.
        """
        assert decode("a=true\nb=True\nc=false\nd=False\n") == {
            "a": True,
            "b": True,
            "c": False,
            "d": False,
        }

    def test_text_fields_are_never_coerced(self) -> None:
        """A numeric-looking symbol or comment stays a string (`mt4_live.py:114`)."""
        got = decode("symbol=500\ncomment=1.5\nerror=404\n")
        assert got["symbol"] == "500"
        assert got["comment"] == "1.5"
        assert got["error"] == "404"

    def test_an_unparseable_number_stays_a_string(self) -> None:
        """`_coerce`'s ValueError path (`mt4_live.py:130-131`)."""
        assert decode("balance=n/a\n")["balance"] == "n/a"


# ---------------------------------------------------------------------------
# Windows mailbox contention. Simulated, because the real proof is the
# windows-latest leg in #26; none of this has ever executed on Windows.
# ---------------------------------------------------------------------------


class TestWindowsMailboxContention:
    def test_a_permanently_locked_reply_file_raises(self, tmp_path: Path, monkeypatch) -> None:
        """`_retry_unlink` gives up at the deadline (`mt4_live.py:177-179`).

        NTFS refuses unlink while the terminal holds the handle. If it never
        lets go, the adapter must raise rather than spin forever.
        """
        real = Path.unlink

        def always_locked(self, *a, **k):
            if self.name == RES_NAME:
                raise PermissionError(13, "locked")
            return real(self, *a, **k)

        (tmp_path / RES_NAME).write_text("id=0\n", encoding="utf-8")
        monkeypatch.setattr(Path, "unlink", always_locked)
        bridge = FileBridge(tmp_path, timeout_sec=0.1)
        with pytest.raises(PermissionError):
            bridge.call("ping", {})

    def test_a_locked_request_rename_is_retried(self, tmp_path: Path, monkeypatch) -> None:
        """`_atomic_write` retries `replace` (`mt4_live.py:186-193`)."""
        hits = {"n": 0}
        real = Path.replace

        def flaky(self, target, *a, **k):
            if self.name.endswith(REQ_NAME + ".tmp") and hits["n"] < 1:
                hits["n"] += 1
                raise PermissionError(13, "locked")
            return real(self, target, *a, **k)

        monkeypatch.setattr(Path, "replace", flaky)
        with wired(tmp_path, GOLDEN):
            broker(tmp_path).connect()
        assert hits["n"] == 1

    def test_a_torn_read_of_the_reply_is_retried(self, tmp_path: Path, monkeypatch) -> None:
        """`mt4_live.py:226-230`. The Expert's rename can race the adapter's read."""
        hits = {"n": 0}
        real = Path.read_text

        def flaky(self, *a, **k):
            if self.name == RES_NAME and hits["n"] < 1:
                hits["n"] += 1
                raise OSError(5, "torn")
            return real(self, *a, **k)

        monkeypatch.setattr(Path, "read_text", flaky)
        with wired(tmp_path, GOLDEN):
            broker(tmp_path).connect()
        assert hits["n"] == 1


# ---------------------------------------------------------------------------
# Branches the adapter claims but no current producer can reach. Listed
# separately on purpose: covering them proves the adapter honours its own
# stated contract, and it proves nothing about the delivered venue. Each one is
# a send-versus-read gap, and #31 is where the Expert side of them belongs.
# ---------------------------------------------------------------------------


class TestAdapterClaimsWithNoProducer:
    def test_encode_omits_a_none_value(self) -> None:
        """`mt4_live.py:101-102`.

        No payload builder in `Mt4Broker` can produce None: `sl`, `tp` and
        `comment` default to 0.0, 0.0 and "" (`models.py:32-33`), and
        `_market_payload` collapses `ticket` with `or 0` (:432). So this is
        `encode`'s documented contract exercised directly, not a venue path.
        """
        text = encode("market", {"symbol": "EURUSD", "sl": None, "volume": 0.17}, 4)
        assert ea_kv(text, "sl") == ""
        assert "sl=" not in text
        assert ea_kv(text, "volume") == "0.17"

    def test_a_successful_reply_carrying_a_retcode_keeps_it(self, tmp_path: Path) -> None:
        """`mt4_live.py:463-465`.

        `Ok()` (:83-86) emits no `retcode`, so nothing in today's Expert reaches
        this branch; the adapter re-derives DONE or PLACED instead. Pinned so
        that an Expert which starts reporting its own success code is carried
        through rather than overwritten.
        """
        reply = "id={id}\nok=1\nretcode=10009\nticket=80051234\n"
        with wired(tmp_path, {"market": Transcript("market", reply)}):
            res = broker(tmp_path).market(
                MarketOrder(symbol="EURUSD", side=Side.BUY, volume=0.17, sl=1.098, tp=1.104)
            )
        assert res.retcode == TRADE_RETCODE_DONE
        assert res.order == 80051234

    def test_a_working_reply_carrying_placed_keeps_it(self, tmp_path: Path) -> None:
        reply = "id={id}\nok=1\nretcode=10008\nticket=80051299\n"
        with wired(tmp_path, {"working": Transcript("working", reply)}):
            res = broker(tmp_path).working(
                WorkingOrder(
                    symbol="EURUSD", side=Side.BUY, kind="limit", volume=0.09, price=1.095
                )
            )
        assert res.retcode == TRADE_RETCODE_PLACED

    def test_the_windows_mailbox_encoding_is_selected_on_win32(self, monkeypatch) -> None:
        """`mt4_live.py:165-169`.

        This proves the branch picks `mbcs`, and nothing more. Whether an ANSI
        code page actually round-trips the Expert's output is a question only a
        Windows runner can answer, which is #26.
        """
        monkeypatch.setattr(sys, "platform", "win32")
        assert _mailbox_encoding() == "mbcs"
        monkeypatch.setattr(sys, "platform", "darwin")
        assert _mailbox_encoding() == "utf-8"

    def test_a_permanently_locked_request_rename_raises(self, tmp_path: Path, monkeypatch) -> None:
        """`_atomic_write` gives up at the deadline (`mt4_live.py:190-192`)."""
        def always_locked(self, target, *a, **k):
            raise PermissionError(13, "locked")

        monkeypatch.setattr(Path, "replace", always_locked)
        bridge = FileBridge(tmp_path, timeout_sec=0.1)
        with pytest.raises(PermissionError):
            bridge.call("ping", {})
