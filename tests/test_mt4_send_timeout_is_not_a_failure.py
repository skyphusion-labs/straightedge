"""MQL4 error 128 is ERR_TRADE_TIMEOUT, and a timeout is not a rejection.

`SendRetry` retried `OrderSend` 50ms after errors 146, 128 and 141. Two of those
three are refusals taken BEFORE the request leaves:

    146  ERR_TRADE_CONTEXT_BUSY    another trade is in progress, nothing was sent
    141  ERR_TOO_MANY_REQUESTS     throttled, nothing was sent

128 is the other kind. The request reached the server and the REPLY did not come
back, so the outcome is UNKNOWN: a timed-out order that actually filled was sent
again, and the desk only ever learned the second ticket. MQL4's own guidance is
to confirm the order did not go through before re-sending.

So the ladder stops at 128 and the BOOK decides, which is the discipline
`RollbackPosition` in this same Expert already follows: `OrderClose`'s return
value is a claim, and the book is the artifact.

**A miss on the book is not evidence.** The desk stamps a client order id into
the comment and MT4 returns the comment on the book, so a HIT is definitive; but
brokers append to and overwrite `OrderComment`, and a fill may not be in the
local pool yet. A guard that re-sent on "not found" would have its failure in the
dangerous direction, which is the reasoning already written down in
`src/straightedge/inflight.py`. So a miss is reported as UNKNOWN, never as
failed, and the desk's in-flight ledger is where that question lives.

**The Expert deliberately does not WAIT for the book to settle.** Waiting spends
the desk's send budget, which is derived from the Expert's declared ladder
(`tests/test_send_budget.py`), and an unresolved send already has a durable home
one layer up. Keeping the timing envelope byte-identical is what makes this a
pure safety change: the scan reads the terminal's own order pool, so it adds no
broker round trip and no `Sleep`.

MQL4 does not execute in this suite, so the Expert half of this file is SOURCE
GUARDS over the shipped artifact, the same kind of evidence as
`tests/test_mt4_claim_open_retry.py` and `tests/test_send_budget.py`: they go red
if the .mq4 is mutated, and they are NOT a behavioural test of a running
terminal. The behavioural proof is a market-hours window on the real box and it
is called out in the PR rather than implied by a green suite here.
"""

from __future__ import annotations

import re
from pathlib import Path

from straightedge.constants import MT4_SEND_TIMEOUT_UNKNOWN

EA_PATH = Path(__file__).resolve().parents[1] / "mt4" / "Experts" / "Mt4RiskBot.mq4"


def _ea() -> str:
    return EA_PATH.read_text(encoding="utf-8")


def _func(name: str) -> str:
    """The source of one Expert function, from its signature to its closing brace."""
    src = _ea()
    m = re.search(r"^\w[\w \*&]*\b%s\s*\(" % re.escape(name), src, re.M)
    assert m is not None, f"the Expert no longer declares `{name}`"
    start = src.index("{", m.end())
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[m.start() : i + 1]
    raise AssertionError(f"unbalanced braces in {name}")


def _block_after(body: str, marker: str) -> str:
    """The balanced `{...}` that follows `marker`.

    Slicing to the end of the enclosing function instead would swallow the
    ladder's own trailing `Sleep`, and the guard would read as if the timeout
    branch slept when it does not. A guard that cannot tell those apart is the
    kind this repo keeps finding.
    """
    assert marker in body, f"{marker!r} is gone from this function"
    start = body.index("{", body.index(marker))
    depth = 0
    for i in range(start, len(body)):
        if body[i] == "{":
            depth += 1
        elif body[i] == "}":
            depth -= 1
            if depth == 0:
                return body[start : i + 1]
    raise AssertionError(f"unbalanced braces after {marker}")


class TestTheLadderStopsAtATimeout:
    def test_the_old_retry_set_is_gone(self) -> None:
        """The defect, named exactly as it was written."""
        assert "err != 146 && err != 128 && err != 141" not in _ea(), (
            "SendRetry still retries OrderSend on error 128; a timed-out order "
            "that filled is sent a second time"
        )

    def test_128_is_handled_as_its_own_outcome(self) -> None:
        body = _func("SendRetry")
        assert "if(err == 128)" in body, (
            "SendRetry no longer distinguishes ERR_TRADE_TIMEOUT from the "
            "refusals that never reached the server"
        )

    def test_the_conclusive_refusals_keep_their_retry(self) -> None:
        """The positive control: 146 and 141 must STILL be retried.

        Removing every retry would satisfy the test above and break the thing the
        ladder exists for, on a broker that answers 146 under normal load.
        """
        body = _func("SendRetry")
        assert "err != 146 && err != 141" in body, body

    def test_the_timeout_branch_never_sends_again(self) -> None:
        """Between the 128 test and its return, nothing may reach OrderSend."""
        body = _func("SendRetry")
        assert body.count("OrderSend(") == 1, (
            "SendRetry has more than one OrderSend site, so the single-send "
            "reading of this function no longer holds"
        )
        branch = _block_after(body, "if(err == 128)")
        assert "OrderSend(" not in branch, branch
        assert "Sleep(" not in branch, (
            "the timeout branch waits, which spends the desk's send budget "
            "without declaring it in SE_LADDER_SLEEP_MS"
        )

    def test_the_timeout_branch_asks_the_book_then_gives_an_unknown(self) -> None:
        body = _func("SendRetry")
        branch = _block_after(body, "if(err == 128)")
        assert "FindByClientId(" in branch, branch
        assert "SE_SEND_UNKNOWN" in branch, branch

    def test_the_unknown_sentinel_is_distinct_from_a_rejection(self) -> None:
        m = re.search(r"^#define\s+SE_SEND_UNKNOWN\s+(-?\d+)\s*$", _ea(), re.M)
        assert m is not None, "the Expert no longer declares SE_SEND_UNKNOWN"
        assert int(m.group(1)) not in (-1, 0), (
            "the unknown sentinel collides with a clean rejection (-1) or a "
            "ticket, so the callers cannot tell them apart"
        )


class TestTheBookLookupIsCorroborationOnly:
    def test_it_matches_on_the_desks_client_order_id(self) -> None:
        body = _func("FindByClientId")
        assert "OrderMagicNumber() != magic" in body, body
        assert "StringFind(OrderComment(), clientId)" in body, body
        assert "MODE_TRADES" in body, (
            "the scan does not cover the pending pool, so a timed-out working "
            "order can never be found"
        )

    def test_an_empty_client_id_finds_nothing(self) -> None:
        """No key, no definitive match. It must not fall back to a loose match."""
        body = _func("FindByClientId")
        assert re.search(r'clientId\s*==\s*""', body), body

    def test_the_client_id_reaches_the_send(self) -> None:
        src = _ea()
        assert src.count('SendRetry(sym, typ, vol, price, slip, ClipComment(KV(body, "comment")), magic, KV(body, "client_id"), sendErr)') == 2, (
            "both send handlers must pass the desk's client_id into SendRetry, "
            "or the book lookup has nothing definitive to match on"
        )


class TestTheReplySaysUnknownAndNotZero:
    def test_the_unresolved_reply_omits_survivor_ticket(self) -> None:
        """Absent means COULD NOT MEASURE; zero would assert a clean failure.

        `docs/MT4.md` already defines the absent field that way, so this state
        needs no new ICD field: `survivor_ticket=0` would tell the desk the book
        was checked and nothing survived, which is the one thing we do not know.
        """
        body = _func("FailUnresolved")
        assert "survivor_ticket" not in body, body
        assert "Fail(" in body, body

    def test_both_send_handlers_report_the_unknown(self) -> None:
        src = _ea()
        assert src.count("SE_SEND_UNKNOWN)") >= 2, (
            "a send handler still funnels the unknown into FailTrade, which "
            "reports survivor_ticket=0 and resolves the desk ledger"
        )
        assert src.count(f'FailUnresolved(id, 128, "{MT4_SEND_TIMEOUT_UNKNOWN}")') == 2

    def test_the_error_token_is_the_one_the_adapter_reads(self) -> None:
        """One spelling, two languages. The .mq4 cannot import the constant."""
        assert MT4_SEND_TIMEOUT_UNKNOWN in _ea(), (
            f"the Expert does not emit {MT4_SEND_TIMEOUT_UNKNOWN!r}, so the "
            "adapter's unmeasured mapping can never fire"
        )
# --- the adapter half: real bytes through the real mailbox --------------------
#
# These are NOT source guards. They drive encode -> file -> decode -> OrderResult
# through `FileBridge` on a real directory, so the wire contract is exercised as
# shipped. `_Mailbox` is reused from the sibling suite rather than copied, the
# same way `test_idempotency_guard_can_go_red` reuses its own fixtures: a second
# stand-in Expert would be a second thing to keep true.
from pathlib import Path  # noqa: E402

from test_mt4_unmanaged import _Mailbox, _market, _working  # noqa: E402

from straightedge.broker.mt4_live import FileBridge, Mt4Broker  # noqa: E402

TIMEOUT_REPLY = (
    "id={id}\nok=0\nretcode=128\nerror=" + MT4_SEND_TIMEOUT_UNKNOWN + "\n"
)


def _broker(tmp_path: Path) -> Mt4Broker:
    return Mt4Broker(FileBridge(tmp_path, timeout_sec=5.0).call, magic=7)


class TestTheAdapterReadsItAsUnmeasured:
    def test_a_timed_out_market_send_is_not_measured(self, tmp_path: Path) -> None:
        """retcode 128 must NOT become a rejection.

        Without the token mapping, `_MT4_RET` has no entry for 128 and the
        fallback makes it `TRADE_RETCODE_REJECT`, which is MEASURED. The engine
        would then resolve the in-flight entry and the desk would tell the
        operator the venue refused an order that may be filling.
        """
        with _Mailbox(tmp_path, TIMEOUT_REPLY):
            res = _broker(tmp_path).market(_market())

        assert not res.measured, (
            f"a timed-out send came back measured with retcode {res.retcode}"
        )
        assert not res.ok

    def test_a_timed_out_send_leaves_survivorship_unknown(self, tmp_path: Path) -> None:
        """Absent is None, and None is not zero.

        Zero would say the book was checked and nothing survived. That is the one
        thing this reply does not know.
        """
        with _Mailbox(tmp_path, TIMEOUT_REPLY):
            res = _broker(tmp_path).market(_market())

        assert res.survivor_ticket is None, res.survivor_ticket

    def test_a_timed_out_working_send_is_not_measured(self, tmp_path: Path) -> None:
        with _Mailbox(tmp_path, TIMEOUT_REPLY):
            res = _broker(tmp_path).working(_working())

        assert not res.measured
        assert res.survivor_ticket is None

    def test_a_timed_out_send_is_unmeasured_because_it_WAS_transmitted(
        self, tmp_path: Path
    ) -> None:
        """Unmeasured because the reply was LOST, not because nothing was sent.

        `OrderResult.transmitted` arrived with #100, which is now on `main`
        (e46191b), so the assertion this file used to defer can be made. It is
        the difference that decides whether the desk may re-send: a request that
        never left is safe to retry, one that was transmitted is not.
        """
        with _Mailbox(tmp_path, TIMEOUT_REPLY):
            res = _broker(tmp_path).market(_market())

        assert res.transmitted, "a timed-out send DID reach the wire"
        assert not res.measured

    def test_the_reported_reason_is_not_overwritten_as_unreported(
        self, tmp_path: Path
    ) -> None:
        """The Expert reported a reason, so nothing may claim it did not.

        `_result` appends "reason not reported by the Expert" when an unmeasured
        failure carries NO reason, which is the `raw == 0` case. A send timeout
        reports its reason precisely. Without the emptiness gate this comment
        reads `send_timeout_outcome_unknown (reason not reported by the
        Expert)`, which contradicts itself in the journal and in the desk reply
        the operator reads during the incident.
        """
        with _Mailbox(tmp_path, TIMEOUT_REPLY):
            res = _broker(tmp_path).market(_market())

        assert res.comment == MT4_SEND_TIMEOUT_UNKNOWN, res.comment
        assert "not reported" not in res.comment

    def test_the_operator_is_not_told_to_reattach_the_expert(self) -> None:
        """The remedy on a timeout is to LOOK, never to detach the Expert.

        `survivor_unknown` covers two states: an Expert too old to answer, and a
        current one that deliberately could not settle the book. The old text
        said "Update Mt4RiskBot.mq4" for both, so the documented remedy on a
        timeout was to reattach the Expert while an unstopped position may be
        live. An absent reason still gets the update instruction, which is what
        the second assertion pins.
        """
        from straightedge.engine import _format_event

        timed_out = _format_event(
            "survivor_unknown",
            {"symbol": "XAUUSD", "retcode": -1, "comment": MT4_SEND_TIMEOUT_UNKNOWN},
        )
        assert "Mt4RiskBot.mq4" not in timed_out, timed_out
        assert "TIMED OUT" in timed_out
        assert "may already be on the book" in timed_out
        assert "NOT be sent again" in timed_out

        no_reason = _format_event(
            "survivor_unknown",
            {"symbol": "XAUUSD", "retcode": 10006, "comment": ""},
        )
        assert "Mt4RiskBot.mq4" in no_reason, no_reason

    def test_an_ordinary_rejection_is_still_measured(self, tmp_path: Path) -> None:
        """The positive control. Not every failure may become unmeasured.

        Without this, mapping the whole `ok=0` space to unknown would satisfy the
        tests above and destroy every real rejection the desk relies on.
        """
        reply = "id={id}\nok=0\nretcode=130\nerror=invalid_stops\nsurvivor_ticket=0\n"
        with _Mailbox(tmp_path, reply):
            res = _broker(tmp_path).market(_market())

        assert res.measured, "a genuine venue rejection stopped being a verdict"
        assert not res.ok
        assert res.survivor_ticket == 0

    def test_an_older_expert_that_already_resent_is_unchanged(
        self, tmp_path: Path
    ) -> None:
        """The compatibility boundary, stated as a test.

        An Expert older than this change reports a timeout as a plain `OrderSend`
        failure, AFTER having already re-sent. The adapter cannot undo that and
        must not pretend to: keyed on the TOKEN, such a reply keeps its existing
        meaning, and the customer's fix is to install the new Expert. This is
        exactly why the mapping is not keyed on the bare code 128.
        """
        reply = "id={id}\nok=0\nretcode=128\nerror=OrderSend\nsurvivor_ticket=0\n"
        with _Mailbox(tmp_path, reply):
            res = _broker(tmp_path).market(_market())

        assert res.measured
        assert res.survivor_ticket == 0
