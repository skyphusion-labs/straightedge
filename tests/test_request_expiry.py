"""A request the desk has given up on must not be executable later.

`FileBridge._exchange` used to raise `BridgeTimeout` with no cleanup, leaving
the `.req` file on the shared name, addressed to an Expert that had not claimed
it yet. The Expert polls every 100ms and executes whatever it finds, so a trade
the operator was told had FAILED could fire minutes afterwards, unattended. The
old behaviour was pinned on purpose in
`tests/test_mt4_wire.py::test_the_request_stays_in_the_mailbox_after_a_timeout`
with the reasoning "a desk that keeps going is fine". That reasoning holds for a
read op and does not hold for `op=market`.

Two layers, and only the second one is a guarantee:

* the desk WITHDRAWS the request when it gives up. Best effort: it can fail on
  a held handle, and it cannot run at all if the desk process died between
  writing the request and noticing the timeout. That process-exit case is the
  unbounded one, and it was observed on the live box (silent restart at
  2026-09-26T01:56:49Z with no traceback).
* every request carries a TTL, and the Expert refuses one that is older. This
  is the layer that survives the desk not being there any more.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from mt4_transcripts import ea_kv

from straightedge.broker.mt4_live import REQ_NAME, BridgeTimeout, FileBridge, encode


class CapturingBridge(FileBridge):
    """Records what `call()` put on the wire, with no mailbox and no thread.

    NOT TIMING-DEPENDENT, and that is the point of it (#258). The `ttl_ms` a
    request carries is decided entirely by `budget_for(op)` and `encode()`,
    both of which run BEFORE `_exchange` is reached, so the wire contract can
    be read at that seam synchronously.

    What this replaces: the same assertion made by starting a stand-in Expert
    in another thread, letting both calls time out, and then reading what the
    Expert had managed to observe. That inferred the contract from the
    SCHEDULER -- the double polls every 5ms against a 200ms read budget, and
    the desk withdraws its request when the budget expires, so a stall longer
    than one budget lost the observation entirely. Measured on
    `windows-latest, 3.12` and root-caused in #254; reproduced on macOS by
    stalling the double's loop:

        stall= 0.0s   seen={'tick': '200', 'market': '600'}
        stall=0.25s   seen={'market': '600'}        <- the CI failure

    The subject was never the scheduler. It is which budget each op is given,
    and that is now observed where it is decided.
    """

    def __init__(self, *args: object, **kw: object) -> None:
        super().__init__(*args, **kw)  # type: ignore[arg-type]
        self.bodies: list[str] = []

    def _exchange(
        self, body: str, req_id: int, *, timeout_sec: float | None = None, op: str = ""
    ) -> str:
        # `call()` has already chosen the budget and encoded the body, which is
        # the whole subject. Raising the real type keeps the caller's contract.
        self.bodies.append(body)
        raise BridgeTimeout(
            "captured by the test before any mailbox io",
            op=op,
            withdrawal="withdrawn",
            req_id=req_id,
        )


def _budgets(tmp_path: Path) -> FileBridge:
    return FileBridge(tmp_path, timeout_sec=0.2)


class TestTheDeskWithdrawsWhatItAbandons:
    def test_a_timed_out_send_is_removed_from_the_mailbox(self, tmp_path: Path) -> None:
        bridge = _budgets(tmp_path)
        with pytest.raises(RuntimeError, match="timeout"):
            bridge.call("market", {"symbol": "XAUUSD", "side": "buy", "volume": 0.01})
        left = tmp_path / REQ_NAME
        assert not left.exists(), (
            "the abandoned request is still on the shared name; the Expert's "
            f"next 100ms poll will execute it. body={left.read_text(encoding='utf-8')!r}"
        )

    def test_a_timed_out_read_is_removed_too(self, tmp_path: Path) -> None:
        """A stale read is harmless, but leaving it is still litter that the
        next call has to clean up, and the Expert still burns a claim on it."""
        bridge = _budgets(tmp_path)
        with pytest.raises(RuntimeError, match="timeout"):
            bridge.call("tick", {"symbol": "XAUUSD"})
        assert not (tmp_path / REQ_NAME).exists()

    def test_the_timeout_says_whether_the_withdrawal_succeeded(self, tmp_path: Path) -> None:
        """Best effort must never be reported as a guarantee.

        The exception has to carry the fact, because the desk decides what to
        tell the operator from it: a request that was withdrawn cannot fire
        later, and a request that could not be withdrawn can.
        """
        bridge = _budgets(tmp_path)
        with pytest.raises(RuntimeError) as exc:
            bridge.call("market", {"symbol": "XAUUSD", "side": "buy", "volume": 0.01})
        assert getattr(exc.value, "withdrawn", None) is True
        assert getattr(exc.value, "op", None) == "market"


class TestEveryRequestCarriesATtl:
    def test_the_encoded_request_states_its_ttl(self) -> None:
        """A duration, not a deadline, and it goes after `op`.

        The desk and the terminal can be on different hosts (`mt4.mailbox_url`),
        so a wall-clock deadline would have to survive clock skew between them,
        and skew in the wrong direction makes a stale request look FRESH. A
        duration has no clock domain.
        """
        body = encode("market", {"symbol": "XAUUSD"}, 7, ttl_ms=7060)
        lines = body.strip().split("\n")
        assert lines[0] == "id=7"
        assert lines[1] == "op=market"
        assert lines[-1] == "ttl_ms=7060"
        assert ea_kv(body, "ttl_ms") == "7060"

    def test_an_expert_too_old_for_the_fence_still_gets_a_valid_request(self) -> None:
        """No `ttl_ms` at all rather than a sentinel. An Expert that does not
        know the field must read the request exactly as it always did, which is
        why the desk's own withdrawal above is not optional."""
        assert "ttl_ms" not in encode("ping", {}, 1)

    def test_a_send_carries_the_send_budget_and_a_read_carries_the_read_one(
        self, tmp_path: Path
    ) -> None:
        """The fence the Expert enforces is the budget the desk actually waited.

        Two numbers on the wire, because one of them being wrong is how the
        Expert would refuse a request the desk was still waiting on, or execute
        one the desk had already written off.
        """
        bridge = CapturingBridge(tmp_path, timeout_sec=0.2, send_timeout_sec=0.6)
        for op, payload in (
            ("tick", {"symbol": "XAUUSD"}),
            ("market", {"symbol": "XAUUSD", "side": "buy", "volume": 0.01}),
        ):
            with pytest.raises(BridgeTimeout):
                bridge.call(op, payload)
        seen = {ea_kv(b, "op"): ea_kv(b, "ttl_ms") for b in bridge.bodies}
        assert seen == {"tick": "200", "market": "600"}, seen
