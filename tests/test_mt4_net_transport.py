"""The MT4 network transport, over real sockets, against the real Expert bytes.

Everything here runs against a REAL listener on 127.0.0.1, a REAL file mailbox on
disk, and the same byte-exact stand-in Expert `tests/test_mt4_wire.py` uses. The
only stubbed seam in the end-to-end tests is the MetaTrader 4 terminal itself,
which no CI runner has. Two tests use a fake `opener` on purpose, and both are
for replies a correct shim cannot produce (a mismatched id, an unbounded body):
stubbing the only thing that can express the failure is the point there.

What this file is for
---------------------
The transport's correctness is almost entirely in its ERROR PARTITION.
`Mt4Broker.startup_connect()` retries `BridgeTimeout` and `OSError` and refuses
to retry anything else, because "nothing is answering yet" is a cold boot that
waiting fixes and "something answered and said no" is a diagnosis that waiting
buries. That partition now has to survive an HTTP hop, and the default behaviour
of the standard library gets it WRONG: `urllib.error.HTTPError` subclasses
`URLError` subclasses `OSError`, so a handler that does not name `HTTPError`
first will retry a 401 in silence for the entire startup budget. That is the
same defect class as the boot bug #74 fixed.

So the pair of tests that matter most are
`test_a_401_is_not_retried_and_does_not_burn_the_startup_budget` and
`test_a_504_IS_retried_until_the_expert_appears`. They are the same harness, the
same budget and the same code path, differing only in what the shim answers, and
they must come out opposite. One alone would not be a gate: a suite where
nothing is ever retried passes the first, and a suite where everything is
retried passes the second.

The auth assertion is the strong form, not the status code
----------------------------------------------------------
A 401 is worth nothing on its own; what matters is that the order never reached
the terminal. Every refusal test therefore asserts the mailbox directory is
EMPTY afterwards, which is the file-system equivalent of the empty Durable
Object namespace in `agent/test/auth-ordering.test.ts`. The denominator is
printed on every one of them.
"""

from __future__ import annotations

import argparse
import io
import socket
import socketserver
import threading
import time
import urllib.error
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, cast

import pytest

from mt4_transcripts import GOLDEN, TranscriptExpert, ea_ok, t_ping
from straightedge.broker import broker_for
from straightedge.broker import mt4_net
from straightedge.broker.mt4_live import (
    PHASE_SHIM_MAILBOX,
    PHASE_SHIM_UNAVAILABLE,
    REQ_NAME,
    BridgeTimeout,
    FileBridge,
    Mt4Broker,
    decode,
    encode,
)
from straightedge.broker.mt4_net import (
    CALL_PATH,
    STATUS_MAILBOX_TIMEOUT,
    STATUS_MAILBOX_UNAVAILABLE,
    MAX_REPLY_BYTES,
    MAX_REQUEST_BYTES,
    MIN_TOKEN_CHARS,
    NET_GRACE_SEC,
    TOKEN_ENV,
    HttpBridge,
    _ShimServer,
    make_shim,
    require_token,
    serve,
)
from straightedge.__main__ import cmd_mt4_shim
from straightedge.config import BotConfig, Mt4Config
from straightedge.models import MarketOrder, Side

#: 43 url-safe characters. A fixed literal rather than a generated one so a
#: failure is reproducible, and comfortably over `MIN_TOKEN_CHARS`.
GOOD_TOKEN = "se73-transport-token-for-tests-only-abcdef1"
BAD_TOKEN = "se73-transport-token-for-tests-only-WRONG01"

#: Short mailbox budget so the timeout tests cost tenths of a second, not
#: seconds. The production default is `mt4.timeout_ms`, 5000.
#: The ceiling for a test where NOTHING CAN ANSWER: no stand-in Expert is
#: running, so the mailbox timeout is certain at any ceiling and this number
#: only decides how long the suite waits for a foregone conclusion. Kept small
#: deliberately, and left as the DEFAULT, so the twenty tests in that shape are
#: unchanged by #258.
MAILBOX_TIMEOUT = 0.3

#: The ceiling for a test where a stand-in Expert MUST answer. Derived rather
#: than guessed (#258).
#:
#: Those tests were failing on `windows-latest` at a measured ~15% of runs,
#: `main` included, because 0.3s is not a transport budget there: it is a bet
#: on a polling thread being SCHEDULED inside 300ms on a loaded two-core runner
#: with a filesystem filter driver. The stand-in polls every 5ms
#: (`mt4_transcripts._loop`), so any stall longer than the ceiling makes the
#: shim answer 504 and the test read a transport fault where the subject was
#: transport TRANSPARENCY.
#:
#: Measured locally, 40 Expert-backed round trips at each ceiling:
#:
#:     ceiling=0.3s   p50=27.9ms  p95=30.4ms  max=30.7ms
#:     ceiling=5.0s   p50=27.8ms  p95=30.1ms  max=30.4ms
#:
#: The green path is IDENTICAL, because the bridge returns as soon as `.res`
#: appears: the budget is a CEILING, not a sleep. Raising a ceiling that is
#: only reached on failure costs nothing when the test passes, and that is what
#: makes this removing the race rather than tuning it.
#:
#: The race is reproducible, by stalling the stand-in's loop past the ceiling
#: the way #254 did for its own instance:
#:
#:     stall     ceiling=0.3s               ceiling=5.0s
#:     0.25s     answered                   answered
#:     0.50s     BridgeTimeout(withdrawn)   answered
#:     2.00s     BridgeTimeout(withdrawn)   answered
#:
#: 5.0s is ~160x the slowest observed round trip. A scheduling stall that long
#: on a CI runner is a broken runner rather than a loaded one, and it would
#: take every other leg down with it instead of one.
ANSWER_BUDGET = 5.0


def _mailbox_names(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir())


def _assert_nothing_reached_the_expert(directory: Path, label: str) -> None:
    """The refusal actually refused: no request was ever written.

    Prints the denominator, because "the directory is empty" is only meaningful
    next to the count it was measured over.
    """
    names = _mailbox_names(directory)
    print(f"mt4-shim refusal {label}: {len(names)} file(s) in the mailbox {names}")
    assert names == [], (
        f"{label}: the shim refused the request but wrote {names} into the "
        "mailbox, so the Expert could still act on it"
    )


@contextmanager
def shim(
    directory: Path,
    *,
    token: str = GOOD_TOKEN,
    timeout_sec: float = MAILBOX_TIMEOUT,
    log: list[str] | None = None,
) -> Iterator[tuple[str, tuple[str, int]]]:
    """A live shim on an OS-assigned loopback port. Yields (url, (host, port))."""
    sink = log if log is not None else []
    server = make_shim(
        files_dir=directory,
        token=token,
        host="127.0.0.1",
        port=0,
        timeout_sec=timeout_sec,
        log=sink.append,
    )
    host, port = server.server_address[0], server.server_address[1]
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True
    )
    thread.start()
    try:
        yield f"http://{host}:{port}{CALL_PATH}", (str(host), int(port))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)


def raw(where: tuple[str, int], request: str) -> tuple[int, str]:
    """One hand-built request, so the shim's own parsing is what answers.

    `urllib` will not emit a request with no `Content-Length`, nor an exotic
    method, and both are cases the auth ordering has to cover. The response is
    read to EOF, which terminates because the shim speaks HTTP/1.0.
    """
    with socket.create_connection(where, timeout=10.0) as sock:
        sock.sendall(request.encode("ascii"))
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    head, _, body = b"".join(chunks).partition(b"\r\n\r\n")
    status = int(head.split()[1])
    return status, body.decode("ascii", "replace")


def request_text(
    method: str,
    path: str,
    *,
    token: str | None,
    body: str = "",
    content_length: str | None = None,
) -> str:
    lines = [f"{method} {path} HTTP/1.0", "Host: 127.0.0.1"]
    if token is not None:
        lines.append(f"Authorization: Bearer {token}")
    if content_length is not None:
        lines.append(f"Content-Length: {content_length}")
    elif body:
        lines.append(f"Content-Length: {len(body)}")
    return "\r\n".join(lines) + "\r\n\r\n" + body


def net_broker(
    url: str,
    *,
    token: str = GOOD_TOKEN,
    timeout_sec: float = MAILBOX_TIMEOUT,
    **kw: Any,
) -> Mt4Broker:
    """A desk pointed at the shim.

    `timeout_sec` is the DESK's mailbox budget and it is named explicitly
    because `**kw` goes to `Mt4Broker`: routing it through there would have put
    the budget on the broker and left the bridge on the default, which fails
    silently rather than loudly. A test whose stand-in Expert must answer
    passes `ANSWER_BUDGET`; everything else keeps the small default.
    """
    bridge = HttpBridge(url, token, timeout_sec=timeout_sec)
    return Mt4Broker(bridge.call, magic=770077, **kw)


def free_port() -> int:
    """A port nothing is listening on, for the connection-refused path."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _FakeResponse:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload

    def read(self, size: int | None = None) -> bytes:
        return self._payload if size is None else self._payload[:size]

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *_: object) -> None:
        return None


def opener_returning(payload: bytes) -> Any:
    def _open(request: object, timeout: float | None = None) -> _FakeResponse:
        del request, timeout
        return _FakeResponse(payload)

    return _open


# ---------------------------------------------------------------------------
# The ICD travels intact. Same Expert bytes as the file transport.
# ---------------------------------------------------------------------------


class TestTheContractSurvivesTheNetwork:
    @pytest.mark.timing
    def test_every_golden_op_answers_over_http(self, tmp_path: Path) -> None:
        """The whole op table, driven through `Mt4Broker` over a real socket.

        This is the denominator that says the transport is transparent: the same
        `GOLDEN` transcripts `tests/test_mt4_wire.py` pins against a local
        mailbox, answered across an HTTP hop, through the same adapter.
        """
        with TranscriptExpert(tmp_path, GOLDEN):
            with shim(tmp_path, timeout_sec=ANSWER_BUDGET) as (url, _):
                br = net_broker(url, timeout_sec=ANSWER_BUDGET)
                br.connect()
                acct = br.account()
                spec = br.symbol("EURUSD")
                tick = br.tick("EURUSD")
                bars = br.rates("EURUSD", "H1", 3)
                positions = br.positions()
                orders = br.orders()
        print(
            f"over HTTP: login={acct.login} symbol={spec.name} bid={tick.bid} "
            f"bars={len(bars)} positions={len(positions)} orders={len(orders)}"
        )
        assert acct.login == 51234567
        assert spec.name == "EURUSD"
        assert tick.bid > 0
        assert bars, "no bars survived the network hop"
        assert positions, "no positions survived the network hop"

    @pytest.mark.timing
    def test_a_market_send_survives_the_network(self, tmp_path: Path) -> None:
        with TranscriptExpert(tmp_path, GOLDEN) as ea:
            with shim(tmp_path, timeout_sec=ANSWER_BUDGET) as (url, _):
                res = net_broker(url, timeout_sec=ANSWER_BUDGET).market(
                    MarketOrder(
                        symbol="EURUSD", side=Side.BUY, volume=0.1, sl=1.09, tp=1.12
                    )
                )
        assert res.ok, res.comment
        assert res.order > 0
        # The Expert saw the desk's own request, unmodified by the shim.
        assert "op=market" in ea.request("market")

    @pytest.mark.timing
    def test_the_shim_forwards_the_desks_request_id_untouched(
        self, tmp_path: Path
    ) -> None:
        """The desk owns the id end to end, so no translation can drift.

        Three pings from one bridge must reach the Expert as 1, 2, 3, exactly as
        they do over the file mailbox (`test_mt4_wire.py`
        `test_request_ids_increment_per_call`).
        """
        with TranscriptExpert(tmp_path, {"ping": t_ping()}) as ea:
            with shim(tmp_path, timeout_sec=ANSWER_BUDGET) as (url, _):
                br = net_broker(url, timeout_sec=ANSWER_BUDGET)
                br.connect()
                br.connect()
                br.connect()
        ids = [line.split("=", 1)[1] for body in ea.seen for line in body.splitlines() if line.startswith("id=")]
        print(f"request ids seen by the Expert over HTTP: {ids}")
        assert ids == ["1", "2", "3"]

    @pytest.mark.timing
    def test_an_expert_refusal_reaches_the_desk_as_a_refusal(
        self, tmp_path: Path
    ) -> None:
        """`ok=0` is a 200 on the wire. It is an answer, not a transport fault.

        Turning it into an HTTP error would make the shim reinterpret the ICD,
        and would make `startup_connect()` see a retryable status for a live
        Expert stating a diagnosis.
        """
        from mt4_transcripts import Transcript, ea_fail

        with TranscriptExpert(tmp_path, {"ping": Transcript("ping", ea_fail(1, "no_terminal"))}):
            with shim(tmp_path, timeout_sec=ANSWER_BUDGET) as (url, _):
                with pytest.raises(RuntimeError, match="no_terminal") as exc:
                    net_broker(url, timeout_sec=ANSWER_BUDGET).connect()
        assert not isinstance(exc.value, BridgeTimeout), (
            "an Expert refusal was reported as a timeout, so startup_connect "
            "would retry a diagnosis for the whole budget"
        )

    @pytest.mark.timing
    def test_the_reply_body_is_the_experts_own_bytes(self, tmp_path: Path) -> None:
        """No re-serialization anywhere in the path.

        `decode` coerces types, so a shim that decoded and re-encoded would not
        be the identity. This asserts the 200 body equals what the Expert wrote.
        """
        from mt4_transcripts import Transcript

        expected = ea_ok("time=1758700000", "note=verbatim/text with spaces").format(id=1)
        with TranscriptExpert(tmp_path, {"ping": Transcript("ping", ea_ok("time=1758700000", "note=verbatim/text with spaces"))}):
            with shim(tmp_path, timeout_sec=ANSWER_BUDGET) as (_url, where):
                status, body = raw(
                    where,
                    request_text(
                        "POST", CALL_PATH, token=GOOD_TOKEN, body=encode("ping", {}, 1)
                    ),
                )
        assert status == 200
        assert body == expected, f"{body!r} != {expected!r}"


# ---------------------------------------------------------------------------
# The error partition. The two tests that must come out opposite.
# ---------------------------------------------------------------------------


class TestTheErrorPartitionSurvivesTheHop:
    @pytest.mark.timing
    def test_a_401_is_not_retried_and_does_not_burn_the_startup_budget(
        self, tmp_path: Path
    ) -> None:
        """A wrong token is a diagnosis. Waiting cannot fix it.

        `HTTPError` is an `OSError`, which `startup_connect()` retries, so this
        goes red the moment `HttpBridge` stops naming `HTTPError` before the
        `URLError` arm. Proven red by deleting that `except` clause: the call
        then spends the whole 8 second budget and raises "never answered".
        """
        log: list[str] = []
        with shim(tmp_path) as (url, _):
            br = net_broker(url, token=BAD_TOKEN, startup_wait_sec=8.0, log=log.append)
            started = time.monotonic()
            with pytest.raises(RuntimeError) as exc:
                br.startup_connect()
            elapsed = time.monotonic() - started
        print(f"401 surfaced after {elapsed:.2f}s of an 8.0s budget; {len(log)} log line(s)")
        assert not isinstance(exc.value, BridgeTimeout), (
            "a 401 was typed as a timeout, so it would be retried for the "
            "entire startup budget in silence"
        )
        assert "401" in str(exc.value), str(exc.value)
        assert elapsed < 4.0, (
            f"a 401 took {elapsed:.2f}s to surface, so it was retried; a wrong "
            "token must be reported at once"
        )
        _assert_nothing_reached_the_expert(tmp_path, "401 during startup")

    @pytest.mark.timing
    def test_a_504_IS_retried_until_the_expert_appears(self, tmp_path: Path) -> None:
        """The positive control for the test above, on the same harness.

        The shim is up and the Expert is not, which is the cold-boot shape. This
        must succeed once the Expert appears; if it did not, the test above
        would pass for the wrong reason (nothing is ever retried) and #74's fix
        would be undone over the network.
        """
        log: list[str] = []
        ea = TranscriptExpert(tmp_path, {"ping": t_ping()})
        with shim(tmp_path) as (url, _):
            br = net_broker(url, startup_wait_sec=10.0, log=log.append)
            appeared = threading.Event()

            def appear() -> None:
                time.sleep(1.0)
                ea.__enter__()
                appeared.set()

            threading.Thread(target=appear, daemon=True).start()
            started = time.monotonic()
            try:
                br.startup_connect()
            finally:
                appeared.wait(timeout=8.0)
                ea.__exit__(None, None, None)
            elapsed = time.monotonic() - started
        attempts = [line for line in log if "no reply yet" in line]
        print(
            f"504 retried {len(attempts)} time(s) over {elapsed:.2f}s, then the "
            "Expert answered"
        )
        assert attempts, f"the 504 was never retried; log was {log}"
        assert any("504" in line for line in attempts), attempts

    @pytest.mark.timing
    def test_a_shim_that_is_not_listening_is_retried(self, tmp_path: Path) -> None:
        """Connection refused is an `OSError`, and at boot that is ordinary.

        The shim may not be up yet, exactly as the terminal may not be, so this
        must take the RETRY path and must still give up bounded.

        What is asserted, and what deliberately is not. The gate is the presence
        of a `retrying in` line: `startup_connect()` emits that only when it has
        decided to loop again, so it separates "retried" from the pre-#74
        behaviour of raising on the first failure. The gate is NOT a COUNT of
        attempts, because the count depends on how long one failed connect takes
        and that is platform-specific: a refused loopback connect returns
        instantly on macOS and Linux, and takes about 2 seconds on Windows
        (`WinError 10061`), which consumed this whole budget in one attempt and
        failed a `>= 2` assertion on both Windows CI legs. Sizing the budget up
        to accommodate that would have bought a latency measurement dressed as a
        behaviour test. That more than one REAL attempt happens is proven by
        `test_a_504_IS_retried_until_the_expert_appears`, where the shim answers
        in milliseconds on every platform.
        """
        del tmp_path
        log: list[str] = []
        url = f"http://127.0.0.1:{free_port()}{CALL_PATH}"
        br = net_broker(url, startup_wait_sec=2.5, log=log.append)
        started = time.monotonic()
        with pytest.raises(RuntimeError, match="never answered") as exc:
            br.startup_connect()
        elapsed = time.monotonic() - started
        attempts = [line for line in log if "no reply yet" in line]
        retrying = [line for line in log if "retrying in" in line]
        print(
            f"connection refused: {len(attempts)} attempt(s), {len(retrying)} of "
            f"them electing to retry, over {elapsed:.2f}s of a 2.5s budget"
        )
        assert retrying, (
            "a refused connection raised without electing to retry, so the shim "
            f"not being up yet is fatal instead of transient: {log}"
        )
        assert not isinstance(exc.value, BridgeTimeout)
        assert elapsed < 20.0, f"the bounded wait ran {elapsed:.2f}s"

    def test_a_mailbox_timeout_is_a_bridge_timeout_in_steady_state(
        self, tmp_path: Path
    ) -> None:
        """One call, no Expert: the type the engine's reconnect path expects."""
        with shim(tmp_path) as (url, _):
            with pytest.raises(BridgeTimeout, match="504"):
                net_broker(url).connect()

    def test_the_shim_times_out_before_the_desk_does(self, tmp_path: Path) -> None:
        """So the desk learns WHICH end gave up.

        A desk that timed out first would abandon a request the Expert may still
        be executing, and a claimed request is never replayed (`docs/MT4.md`),
        so the reply would simply be lost.
        """
        bridge = HttpBridge("http://127.0.0.1:1/x", GOOD_TOKEN, timeout_sec=5.0)
        print(
            f"desk HTTP budget {bridge.timeout:.1f}s against a 5.0s mailbox "
            f"budget (grace {NET_GRACE_SEC:.1f}s)"
        )
        assert bridge.timeout == pytest.approx(5.0 + NET_GRACE_SEC)
        assert bridge.timeout > 5.0
        with shim(tmp_path, timeout_sec=0.2) as (url, _):
            br = net_broker(url)
            with pytest.raises(BridgeTimeout, match="504"):
                br.connect()

    def test_a_reply_with_the_wrong_id_is_not_a_timeout(self) -> None:
        """A confused peer, not a slow one. Not retried."""
        bridge = HttpBridge(
            "http://shim.invalid/x",
            GOOD_TOKEN,
            opener=opener_returning(b"id=99\nok=1\n"),
        )
        with pytest.raises(RuntimeError, match="does not match request") as exc:
            bridge.call("ping", {})
        assert not isinstance(exc.value, BridgeTimeout)

    def test_an_unbounded_reply_is_refused(self) -> None:
        bridge = HttpBridge(
            "http://shim.invalid/x",
            GOOD_TOKEN,
            opener=opener_returning(b"x" * (MAX_REPLY_BYTES + 10)),
        )
        with pytest.raises(RuntimeError, match="exceeded"):
            bridge.call("ping", {})


# ---------------------------------------------------------------------------
# Auth. The status code is the weak assertion; the empty mailbox is the strong one.
# ---------------------------------------------------------------------------


class TestAuthRunsBeforeEverything:
    @pytest.mark.parametrize(
        ("label", "method", "path", "token"),
        [
            ("no header", "POST", CALL_PATH, None),
            ("wrong token", "POST", CALL_PATH, BAD_TOKEN),
            ("wrong token, wrong path", "POST", "/admin", BAD_TOKEN),
            ("wrong token, GET", "GET", CALL_PATH, BAD_TOKEN),
            ("wrong token, exotic method", "PROPFIND", CALL_PATH, BAD_TOKEN),
            ("empty bearer", "POST", CALL_PATH, ""),
        ],
    )
    def test_every_unauthenticated_shape_is_401_and_reaches_no_expert(
        self, tmp_path: Path, label: str, method: str, path: str, token: str | None
    ) -> None:
        """One answer for every refused shape, so nothing is distinguishable.

        The exotic-method row is not decoration: without the `do_*` catch-all in
        `_ShimHandler.__getattr__`, `BaseHTTPRequestHandler` answers an unknown
        method with its OWN 501 before any code here runs, so an unauthenticated
        `PROPFIND` would be distinguishable from an unauthenticated `POST`.
        Proven red by removing that method: this row returns 501.
        """
        body = encode("market", {"symbol": "EURUSD", "volume": 0.1}, 1)
        with TranscriptExpert(tmp_path, GOLDEN):
            with shim(tmp_path) as (_url, where):
                status, reply = raw(
                    where, request_text(method, path, token=token, body=body)
                )
        print(f"unauthenticated {label}: HTTP {status} {reply.strip()!r}")
        assert status == 401, f"{label} answered {status}, not 401"
        assert "error=unauthorized" in reply
        _assert_nothing_reached_the_expert(tmp_path, label)

    @pytest.mark.parametrize(
        ("label", "method", "path"),
        [
            ("wrong path", "POST", "/admin"),
            ("wrong method", "GET", CALL_PATH),
            ("exotic method", "PROPFIND", CALL_PATH),
        ],
    )
    def test_an_authenticated_caller_gets_404_and_reaches_no_expert(
        self, tmp_path: Path, label: str, method: str, path: str
    ) -> None:
        """404 only AFTER auth, so the surface is not mappable by a stranger."""
        with shim(tmp_path) as (_url, where):
            status, reply = raw(
                where, request_text(method, path, token=GOOD_TOKEN, body="id=1\nop=ping\n")
            )
        print(f"authenticated {label}: HTTP {status} {reply.strip()!r}")
        assert status == 404
        assert "error=not_found" in reply
        _assert_nothing_reached_the_expert(tmp_path, label)

    def test_a_401_is_answered_without_reading_the_body(self, tmp_path: Path) -> None:
        """The refusal precedes parsing, so a malformed body cannot change it."""
        with shim(tmp_path) as (_url, where):
            status, _ = raw(
                where,
                request_text(
                    "POST", CALL_PATH, token=BAD_TOKEN, body="{not the mailbox wire"
                ),
            )
        assert status == 401
        _assert_nothing_reached_the_expert(tmp_path, "malformed body, wrong token")

    def test_an_oversized_request_is_refused_on_its_header(self, tmp_path: Path) -> None:
        """Refused on `Content-Length`, before the bytes are read."""
        with shim(tmp_path) as (_url, where):
            status, reply = raw(
                where,
                request_text(
                    "POST",
                    CALL_PATH,
                    token=GOOD_TOKEN,
                    content_length=str(MAX_REQUEST_BYTES + 1),
                ),
            )
        print(f"oversized request: HTTP {status} {reply.strip()!r}")
        assert status == 413
        _assert_nothing_reached_the_expert(tmp_path, "oversized request")

    def test_a_request_with_no_content_length_is_refused(self, tmp_path: Path) -> None:
        with shim(tmp_path) as (_url, where):
            status, reply = raw(
                where, request_text("POST", CALL_PATH, token=GOOD_TOKEN)
            )
        assert status == 411, f"got {status}: {reply!r}"
        _assert_nothing_reached_the_expert(tmp_path, "no content length")

    def test_a_body_with_no_request_id_is_refused(self, tmp_path: Path) -> None:
        """The one field the shim parses. Without it there is no reply to wait for."""
        with shim(tmp_path) as (_url, where):
            status, reply = raw(
                where, request_text("POST", CALL_PATH, token=GOOD_TOKEN, body="op=ping\n")
            )
        print(f"no request id: HTTP {status} {reply.strip()!r}")
        assert status == 400
        assert "error=missing_request_id" in reply
        _assert_nothing_reached_the_expert(tmp_path, "no request id")

    def test_a_refusal_never_logs_the_request_line_or_the_token(
        self, tmp_path: Path
    ) -> None:
        """Both are attacker-controlled on exactly the requests that get logged."""
        log: list[str] = []
        with shim(tmp_path, log=log) as (_url, where):
            raw(
                where,
                request_text(
                    "POST", "/admin?secret=" + BAD_TOKEN, token=BAD_TOKEN, body="id=1\n"
                ),
            )
        joined = "\n".join(log)
        print(f"shim log after a refusal: {log}")
        assert BAD_TOKEN not in joined, "the shim logged the presented token"
        assert "/admin" not in joined, "the shim echoed an attacker-supplied path"
        assert any("401" in line for line in log), log


# ---------------------------------------------------------------------------
# Fail-closed at startup, on both ends.
# ---------------------------------------------------------------------------


class TestItRefusesToExistMisconfigured:
    @pytest.mark.parametrize("token", ["", "   ", "short", "x" * (MIN_TOKEN_CHARS - 1)])
    def test_the_shim_refuses_a_missing_or_weak_token(
        self, tmp_path: Path, token: str
    ) -> None:
        with pytest.raises(RuntimeError, match=TOKEN_ENV):
            make_shim(files_dir=tmp_path, token=token, port=0, log=lambda _m: None)

    @pytest.mark.parametrize("token", ["", "short"])
    def test_the_desk_refuses_a_missing_or_weak_token(self, token: str) -> None:
        with pytest.raises(RuntimeError, match=TOKEN_ENV):
            HttpBridge("http://shim.invalid/x", token)

    def test_the_desk_refuses_an_empty_url(self) -> None:
        with pytest.raises(RuntimeError, match="mailbox_url"):
            HttpBridge("", GOOD_TOKEN)

    def test_the_shim_refuses_an_empty_files_dir(self) -> None:
        with pytest.raises(RuntimeError, match="files_dir"):
            make_shim(files_dir="", token=GOOD_TOKEN, port=0, log=lambda _m: None)

    def test_the_shim_refuses_a_plaintext_routable_bind(self, tmp_path: Path) -> None:
        """`http.server` has no TLS and this endpoint places orders."""
        with pytest.raises(RuntimeError, match="plaintext"):
            make_shim(
                files_dir=tmp_path,
                token=GOOD_TOKEN,
                host="0.0.0.0",  # noqa: S104 - the point of the test
                port=0,
                log=lambda _m: None,
            )

    def test_the_plaintext_flag_is_the_only_way_past_it(self, tmp_path: Path) -> None:
        log: list[str] = []
        server = make_shim(
            files_dir=tmp_path,
            token=GOOD_TOKEN,
            host="0.0.0.0",  # noqa: S104 - the point of the test
            port=0,
            allow_plaintext_exposure=True,
            log=log.append,
        )
        server.server_close()
        assert any("WARNING plaintext" in line for line in log), log

    @pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
    def test_loopback_needs_no_flag(self, tmp_path: Path, host: str) -> None:
        try:
            server = make_shim(
                files_dir=tmp_path, token=GOOD_TOKEN, host=host, port=0, log=lambda _m: None
            )
        except OSError as exc:  # pragma: no cover - IPv6 absent on some runners
            pytest.skip(f"{host} is not bindable here: {exc}")
        server.server_close()

    def test_require_token_accepts_a_long_token(self) -> None:
        assert require_token(GOOD_TOKEN, where="test") == GOOD_TOKEN

    def test_the_shim_is_single_threaded_by_construction(self) -> None:
        """The mailbox is a singleton by contract, so make it structural.

        A threading server could interleave two requests into one mailbox even
        though `docs/MT4.md` forbids two desks against one mailbox. One thread
        means this process cannot be the thing that does it.
        """
        assert not issubclass(_ShimServer, socketserver.ThreadingMixIn)
        # `ForkingMixIn` does not exist on Windows, which has no `fork`, so it is
        # looked up rather than imported. Naming it directly made both Windows CI
        # legs fail with AttributeError.
        forking = getattr(socketserver, "ForkingMixIn", None)
        if forking is not None:
            assert not issubclass(_ShimServer, forking)


# ---------------------------------------------------------------------------
# The raw exchange the shim is built on.
# ---------------------------------------------------------------------------


class TestFileBridgeExchange:
    def test_exchange_uses_the_callers_id_and_returns_raw_text(
        self, tmp_path: Path
    ) -> None:
        """The desk's id, and the Expert's bytes. Neither is rewritten.

        The bridge's own counter is at zero here, so an id of 7 coming back
        proves the caller's id was used rather than a locally generated one.
        """
        with TranscriptExpert(tmp_path, {"ping": t_ping()}):
            bridge = FileBridge(tmp_path, timeout_sec=3.0)
            text = bridge.exchange(encode("ping", {}, 7), 7)
        print(f"raw exchange reply: {text!r}")
        assert text.startswith("id=7\n")
        assert isinstance(text, str)
        assert "ok=1" in text

    def test_exchange_times_out_as_a_bridge_timeout(self, tmp_path: Path) -> None:
        bridge = FileBridge(tmp_path, timeout_sec=0.15)
        with pytest.raises(BridgeTimeout) as caught:
            bridge.exchange(encode("ping", {}, 1), 1)
        # This assertion used to read "the request should be left in place". It
        # was the shim half of the same defect: the shim gives up, answers 504,
        # and the request it wrote is still addressed to an Expert that has not
        # claimed it. `exchange()` goes through the same `_exchange`, so the
        # withdrawal covers the remote desk too.
        assert not (tmp_path / REQ_NAME).exists(), "the abandoned request is still live"
        assert caught.value.withdrawal == "withdrawn"

    @pytest.mark.timing
    def test_exchange_takes_its_budget_from_the_requests_own_ttl(
        self, tmp_path: Path
    ) -> None:
        """The shim must not be the end that gives up first.

        The desk sizes a send budget from the Expert's worst case and states it as
        `ttl_ms`. A shim that waited its own READ budget instead would abandon a
        request the Expert was still executing -- the exact defect the split
        budget removes, reintroduced one hop away.
        """
        bridge = FileBridge(tmp_path, timeout_sec=0.05, send_timeout_sec=1.0)
        started = time.monotonic()
        with pytest.raises(BridgeTimeout):
            bridge.exchange(encode("market", {"symbol": "XAUUSD"}, 1, ttl_ms=400), 1)
        waited = time.monotonic() - started
        assert 0.35 < waited < 0.9, waited

    def test_the_shim_cli_gives_the_mailbox_the_desks_send_ceiling(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The clamp must not become the thing that gives up first.

        `FileBridge.exchange` sizes its wait from the request's `ttl_ms` clamped to
        its own ceiling. If `mt4-shim` builds that bridge with only the READ budget,
        the ceiling is 5000ms, a desk send stating 7060ms is clamped down, and the
        shim abandons a request the Expert is still executing 1.9 seconds before the
        desk would have. That is the exact defect the split budget removes,
        reintroduced one hop away, and it is reachable ONLY on the network
        transport, which is the live topology.

        Found by driving the shipped `cmd_mt4_shim` and measuring what the mailbox
        actually waited; nothing else in this suite passes `send_timeout_sec`, so
        without this guard the gap could not go red.
        """
        built: dict[str, object] = {}
        real = mt4_net.make_shim

        def capture(**kw: object) -> object:
            built.update(kw)
            raise RuntimeError("stop before binding a socket")

        monkeypatch.setattr("straightedge.__main__.make_shim", capture)
        del real
        cfg = BotConfig()
        cfg.mode = "mt4"
        cfg.mt4 = Mt4Config(
            files_dir=str(tmp_path), mailbox_token="t" * 40, timeout_ms=5000
        )
        monkeypatch.setattr("straightedge.__main__.load_config", lambda *a, **k: cfg)
        cmd_mt4_shim(
            argparse.Namespace(
                config=None, host="127.0.0.1", port=8730, i_understand_plaintext=False
            )
        )
        assert built["timeout_sec"] == 5.0
        assert built["send_timeout_sec"] == cfg.mt4.send_timeout_ms / 1000.0, (
            "mt4-shim builds its mailbox with only the read budget, so a send's "
            "ttl_ms is clamped down and the shim gives up before the desk does"
        )

    @pytest.mark.timing
    def test_the_shim_mailbox_waits_the_full_send_ttl(self, tmp_path: Path) -> None:
        """The same thing measured rather than inspected, on the bridge itself."""
        bridge = FileBridge(
            tmp_path, timeout_sec=5.0, send_timeout_sec=6.88
        )
        started = time.monotonic()
        with pytest.raises(BridgeTimeout):
            bridge.exchange(encode("market", {"symbol": "X"}, 1, ttl_ms=1200), 1)
        waited = time.monotonic() - started
        assert 1.1 < waited < 2.0, waited

    @pytest.mark.timing
    def test_a_ttl_beyond_the_shims_ceiling_is_clamped(self, tmp_path: Path) -> None:
        """`ttl_ms` arrives over the network, so it is clamped, not trusted.

        The shim is single threaded by contract, so one request holding it open
        for an attacker-chosen duration is a denial of the mailbox.
        """
        bridge = FileBridge(tmp_path, timeout_sec=0.05, send_timeout_sec=0.2)
        started = time.monotonic()
        with pytest.raises(BridgeTimeout):
            bridge.exchange(encode("market", {"symbol": "X"}, 1, ttl_ms=60_000), 1)
        assert time.monotonic() - started < 1.0


# ---------------------------------------------------------------------------
# Which transport a config selects.
# ---------------------------------------------------------------------------


class TestTransportSelection:
    def _cfg(self, **mt4: Any) -> BotConfig:
        cfg = BotConfig(mode="mt4", mt4=Mt4Config(**mt4))
        return cfg

    def test_a_mailbox_url_selects_the_network_transport(self, tmp_path: Path) -> None:
        cfg = self._cfg(
            files_dir=str(tmp_path),
            mailbox_url="http://mt4-host.invalid/mt4/call",
            mailbox_token=GOOD_TOKEN,
        )
        broker = broker_for(cfg)
        assert isinstance(broker, Mt4Broker)
        assert "HttpBridge" in repr(broker._call.__self__), repr(broker._call)

    def test_no_url_keeps_the_file_mailbox(self, tmp_path: Path) -> None:
        broker = broker_for(self._cfg(files_dir=str(tmp_path)))
        assert isinstance(broker, Mt4Broker)
        assert "FileBridge" in repr(broker._call.__self__), repr(broker._call)

    def test_the_url_wins_over_an_auto_resolved_files_dir(self, tmp_path: Path) -> None:
        """The ordering that matters on Windows.

        `files_dir` resolves to Common Files there even when nobody set it, so a
        url that lost this tie would leave a desk configured for a REMOTE
        terminal quietly reading the LOCAL mailbox, with every indicator green.
        """
        cfg = self._cfg(
            files_dir=str(tmp_path),
            mailbox_url="http://mt4-host.invalid/mt4/call",
            mailbox_token=GOOD_TOKEN,
        )
        assert "HttpBridge" in repr(broker_for(cfg)._call.__self__)

    def test_a_url_with_no_token_refuses_to_build_a_broker(self) -> None:
        cfg = self._cfg(mailbox_url="http://mt4-host.invalid/mt4/call")
        with pytest.raises(RuntimeError, match=TOKEN_ENV):
            broker_for(cfg)

    def test_no_url_and_no_files_dir_names_both_ways_out(self) -> None:
        with pytest.raises(RuntimeError, match="mailbox_url"):
            broker_for(self._cfg())

    def test_doctor_reports_the_network_transport_without_the_token(self) -> None:
        from straightedge.__main__ import mt4_transport_line

        line = mt4_transport_line(
            self._cfg(mailbox_url="http://mt4-host.invalid/mt4/call", mailbox_token=GOOD_TOKEN)
        )
        print(f"doctor: {line}")
        assert "network shim" in line
        assert GOOD_TOKEN not in line, "doctor printed the token value"
        assert f"{TOKEN_ENV}: SET" in line

    def test_doctor_reports_an_unset_token_as_unset(self) -> None:
        from straightedge.__main__ import mt4_transport_line

        line = mt4_transport_line(self._cfg(mailbox_url="http://mt4-host.invalid/mt4/call"))
        assert f"{TOKEN_ENV}: unset" in line

    def test_doctor_reports_the_file_transport(self, tmp_path: Path) -> None:
        from straightedge.__main__ import mt4_transport_line

        line = mt4_transport_line(self._cfg(files_dir=str(tmp_path)))
        print(f"doctor: {line}")
        assert "file mailbox" in line
        assert str(tmp_path) in line

    def test_the_config_reads_the_url_and_token_from_the_environment(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The token has no TOML key at all: it is environment only."""
        from straightedge.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(
            '[account]\nmode = "mt4"\n[mt4]\nmailbox_token = "ignored-in-file"\n',
            encoding="utf-8",
        )
        monkeypatch.setenv("MT4_MAILBOX_URL", "http://from-env.invalid/mt4/call")
        monkeypatch.setenv(TOKEN_ENV, GOOD_TOKEN)
        cfg = load_config(str(path))
        assert cfg.mt4.mailbox_url == "http://from-env.invalid/mt4/call"
        assert cfg.mt4.mailbox_token == GOOD_TOKEN

    def test_a_token_in_the_toml_file_is_never_read(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        from straightedge.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(
            '[account]\nmode = "mt4"\n[mt4]\nmailbox_token = "do-not-read-this-from-a-tracked-file"\n',
            encoding="utf-8",
        )
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        cfg = load_config(str(path))
        assert cfg.mt4.mailbox_token == "", (
            "a secret was read out of a TOML file; docs/CONTRACT.md keeps "
            "secrets in the environment"
        )


# ---------------------------------------------------------------------------
# The arms that only a raised exception can reach.
# ---------------------------------------------------------------------------


def opener_raising(exc: BaseException) -> Any:
    def _open(request: object, timeout: float | None = None) -> Any:
        del request, timeout
        raise exc

    return _open


class TestSlowNetworkStaysRetryable:
    """A slow network must read as "not answering yet", never as a diagnosis.

    Two distinct arms, because the standard library reports a timeout two
    different ways depending on WHEN it happens: a connect timeout arrives
    wrapped in `URLError`, and a read timeout after the headers arrived arrives
    bare as `TimeoutError`. Mapping only one of them would leave half the slow
    network typed as a fatal error, which would undo #74 over the network for
    the half nobody tested.
    """

    def test_a_read_timeout_is_a_bridge_timeout(self) -> None:
        bridge = HttpBridge(
            "http://shim.invalid/x",
            GOOD_TOKEN,
            opener=opener_raising(TimeoutError("read timed out")),
        )
        with pytest.raises(BridgeTimeout, match="timeout"):
            bridge.call("ping", {})

    def test_a_connect_timeout_wrapped_in_urlerror_is_a_bridge_timeout(self) -> None:
        import urllib.error

        bridge = HttpBridge(
            "http://shim.invalid/x",
            GOOD_TOKEN,
            opener=opener_raising(urllib.error.URLError(TimeoutError("connect"))),
        )
        with pytest.raises(BridgeTimeout, match="timeout"):
            bridge.call("ping", {})

    def test_a_non_timeout_urlerror_stays_an_oserror(self) -> None:
        """Connection refused and DNS failure are re-raised unchanged.

        `startup_connect()` already retries `OSError`, so nothing has to be
        translated. Typing it as a `BridgeTimeout` would be a lie about what
        happened for no gain.
        """
        import urllib.error

        bridge = HttpBridge(
            "http://shim.invalid/x",
            GOOD_TOKEN,
            opener=opener_raising(urllib.error.URLError(ConnectionRefusedError(61))),
        )
        with pytest.raises(urllib.error.URLError) as exc:
            bridge.call("ping", {})
        assert isinstance(exc.value, OSError), "startup_connect would not retry this"
        assert not isinstance(exc.value, BridgeTimeout)


class TestTheShimsOwnFailurePaths:
    def test_an_unwritable_mailbox_is_a_503_and_is_retryable(
        self, tmp_path: Path
    ) -> None:
        """MT4 may not have created Common Files yet, so this is a boot shape.

        The directory is made unmakeable portably, by putting a regular FILE
        where its parent has to be, rather than with mode bits that Windows does
        not honour.
        """
        blocker = tmp_path / "not-a-directory"
        blocker.write_text("x", encoding="utf-8")
        mailbox = blocker / "mailbox"
        with shim(mailbox) as (url, where):
            status, reply = raw(
                where,
                request_text("POST", CALL_PATH, token=GOOD_TOKEN, body=encode("ping", {}, 1)),
            )
            print(f"unwritable mailbox: HTTP {status} {reply.strip()!r}")
            assert status == 503
            assert "error=mailbox_unavailable" in reply
            with pytest.raises(BridgeTimeout, match="503") as exc:
                net_broker(url).connect()
        assert isinstance(exc.value, BridgeTimeout), (
            "an unwritable mailbox at boot must be retried, not fatal"
        )

    def test_a_dns_name_is_not_loopback_and_needs_the_flag(self, tmp_path: Path) -> None:
        """A hostname is not an address, so it cannot be judged loopback."""
        with pytest.raises(RuntimeError, match="plaintext"):
            make_shim(
                files_dir=tmp_path,
                token=GOOD_TOKEN,
                host="mt4-host.example.org",
                port=0,
                log=lambda _m: None,
            )

    def test_the_default_log_sink_flushes_to_stdout(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Under Task Scheduler stdout is block-buffered, so this is load-bearing."""
        from straightedge.broker.mt4_net import _log_line

        _log_line("mt4-shim: hello")
        assert "mt4-shim: hello" in capsys.readouterr().out


class _FakeServer:
    """Just enough of `BaseServer` for `serve()`. Records what it was asked."""

    def __init__(self, raise_interrupt: bool) -> None:
        self.raise_interrupt = raise_interrupt
        self.served = 0
        self.closed = 0

    def serve_forever(self, poll_interval: float = 0.5) -> None:
        del poll_interval
        self.served += 1
        if self.raise_interrupt:
            raise KeyboardInterrupt

    def server_close(self) -> None:
        self.closed += 1


class TestServeLoop:
    def test_serve_closes_the_listener_on_a_clean_return(self) -> None:
        server = _FakeServer(raise_interrupt=False)
        assert serve(cast(Any, server)) == 0
        assert (server.served, server.closed) == (1, 1)

    def test_serve_closes_the_listener_on_an_interrupt(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Ctrl-C is an operator stopping the shim, not a crash.

        The listener must still be closed, or a restart cannot rebind.
        """
        server = _FakeServer(raise_interrupt=True)
        assert serve(cast(Any, server)) == 0
        assert server.closed == 1
        assert "interrupt" in capsys.readouterr().err


class TestTheShimCommand:
    def test_a_misconfigured_shim_exits_2_and_says_why(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from straightedge.__main__ import main

        path = tmp_path / "config.toml"
        path.write_text('[account]\nmode = "mt4"\n', encoding="utf-8")
        monkeypatch.delenv(TOKEN_ENV, raising=False)
        monkeypatch.setenv("MT4_FILES_DIR", str(tmp_path))
        rc = main(["--config", str(path), "mt4-shim", "--port", "0"])
        err = capsys.readouterr().err
        print(f"mt4-shim misconfigured: rc={rc} err={err.strip()!r}")
        assert rc == 2
        assert TOKEN_ENV in err

    def test_a_configured_shim_binds_and_serves(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The CLI path, with only `serve` stubbed so the test can return."""
        import straightedge.__main__ as cli

        path = tmp_path / "config.toml"
        path.write_text('[account]\nmode = "mt4"\n', encoding="utf-8")
        monkeypatch.setenv(TOKEN_ENV, GOOD_TOKEN)
        monkeypatch.setenv("MT4_FILES_DIR", str(tmp_path))
        served: list[Any] = []

        def fake_serve(server: Any) -> int:
            served.append(server)
            server.server_close()
            return 0

        monkeypatch.setattr(cli, "serve", fake_serve)
        rc = cli.main(["--config", str(path), "mt4-shim", "--port", "0"])
        assert rc == 0
        assert len(served) == 1, f"servers served: {len(served)}"


class TestTheShimReportsWhatItWithdrew:
    """The shim measures the file leg's withdrawal; the desk must learn it (#135).

    On a mailbox timeout behind the shim, the shim's own `FileBridge._withdraw`
    returns `withdrawn`, `claimed` or `locked`, and that is a MEASUREMENT: the
    `.req` file either came back off the shared name or it did not. Before this,
    `_refuse` put it in the shim's local log only and sent the desk
    `ok=0 error=mailbox_timeout`, so `HttpBridge._from_status` hardcoded the
    pessimistic `claimed`.

    `withdrawal` is read on the SEND path. `withdrawn` means the request is
    provably gone and the order cannot fire; `claimed` is the ambiguous-money
    case that writes `journal.inflight.json` and refuses to transmit that order
    again. So every provably-withdrawn send off-box became an operator
    reconciliation by hand, for an outcome the shim had one process away.
    """

    def test_the_shim_measures_a_withdrawal_the_desk_cannot_see(
        self, tmp_path: Path
    ) -> None:
        """The gap itself, measured on BOTH sides of the wire at once.

        This is the reproduction for #135 and it is written so it can only pass
        when the fact actually crosses: it asserts the shim ACHIEVED `withdrawn`
        from the shim's own log before it asserts what the desk received. If the
        shim ever stopped withdrawing, the first assertion fails and this test
        reports that instead of silently passing on a changed premise.
        """
        log: list[str] = []
        with shim(tmp_path, log=log) as (url, _):
            with pytest.raises(BridgeTimeout) as caught:
                net_broker(url).connect()

        # The shim's side: it took the request back off the shared name. No
        # Expert ever ran, so `unlink` succeeded and `_withdraw` said so.
        shim_lines = [line for line in log if "mailbox_timeout" in line]
        print(f"shim refusal lines: {len(shim_lines)} {shim_lines}")
        assert shim_lines, f"the shim logged no mailbox_timeout refusal: {log}"
        assert any("request=withdrawn" in line for line in shim_lines), (
            "the shim did not withdraw the request, so this test's premise is "
            f"gone and #135 is about a fact that no longer exists: {shim_lines}"
        )

        # The desk's side: the same event, as the desk is entitled to read it.
        print(f"desk withdrawal={caught.value.withdrawal!r}")
        assert caught.value.withdrawal == "withdrawn", (
            "the shim proved the request is gone and the desk still reads it as "
            f"{caught.value.withdrawal!r}, so a provably-withdrawn send is "
            "treated as ambiguous money"
        )
        # The partition must not move: a 504 is still a retryable BridgeTimeout.
        assert "504" in str(caught.value)
        assert caught.value.phase == PHASE_SHIM_MAILBOX

    def test_an_absent_withdrawal_key_falls_back_to_claimed(self) -> None:
        """Absent must never read as `withdrawn` (#135).

        An OLD shim against a NEW desk sends no `withdrawal=` key. The fallback
        is `claimed`, always, so a dropped key fails toward the unsafe reading.
        A bug that loses the key must cost an operator a reconciliation, never a
        clean bill of health on money that may have moved.
        """
        body = b"ok=0\nerror=mailbox_timeout\n"
        exc = urllib.error.HTTPError(
            "http://shim.invalid/x",
            STATUS_MAILBOX_TIMEOUT,
            "Gateway Timeout",
            {},  # type: ignore[arg-type]
            io.BytesIO(body),
        )
        bridge = HttpBridge("http://shim.invalid/x", GOOD_TOKEN, timeout_sec=1.0)
        built = bridge._from_status(exc, "market")
        assert isinstance(built, BridgeTimeout)
        print(f"absent key -> withdrawal={built.withdrawal!r}")
        assert built.withdrawal == "claimed"

    def test_an_unknown_withdrawal_word_falls_back_to_claimed(self) -> None:
        """A word this desk does not know is not a measurement (#135).

        The vocabulary is closed: `withdrawn`, `claimed`, `locked`. A shim that
        sends anything else is a peer this desk cannot interpret, and guessing
        would be inventing a measurement. Same fallback, same direction.
        """
        body = b"ok=0\nerror=mailbox_timeout\nwithdrawal=probably_fine\n"
        exc = urllib.error.HTTPError(
            "http://shim.invalid/x",
            STATUS_MAILBOX_TIMEOUT,
            "Gateway Timeout",
            {},  # type: ignore[arg-type]
            io.BytesIO(body),
        )
        bridge = HttpBridge("http://shim.invalid/x", GOOD_TOKEN, timeout_sec=1.0)
        built = bridge._from_status(exc, "market")
        assert isinstance(built, BridgeTimeout)
        print(f"unknown word -> withdrawal={built.withdrawal!r}")
        assert built.withdrawal == "claimed"

    def test_a_503_reports_no_withdrawal_because_there_was_none(
        self, tmp_path: Path
    ) -> None:
        """#135 must NOT widen to 503, and this pins that it did not.

        On a 503 the mailbox was never usable and nothing was written, so there
        is no withdrawal to report. Reporting one would invent a measurement.
        The desk keeps `claimed` here, and the phase stays the unavailable one.

        THE BODY CARRIES `withdrawal=withdrawn` ON PURPOSE, and the first
        version of this test did not. Without it the case could not fail:
        a 503 body with no key falls back to `claimed` anyway, so "the desk
        ignores the key on a 503" and "the desk reads the key on every status"
        produce the same answer and the control is decoration. Measured: with
        the key absent, deleting the 504 condition in `_from_status` left the
        whole suite green. A correct shim never sends this, because `_refuse`
        is only given a withdrawal on the timeout path; the point is that a
        future or confused one cannot teach this desk a fact that did not
        happen.
        """
        body = b"ok=0\nerror=mailbox_unavailable\nwithdrawal=withdrawn\n"
        exc = urllib.error.HTTPError(
            "http://shim.invalid/x",
            STATUS_MAILBOX_UNAVAILABLE,
            "Service Unavailable",
            {},  # type: ignore[arg-type]
            io.BytesIO(body),
        )
        bridge = HttpBridge("http://shim.invalid/x", GOOD_TOKEN, timeout_sec=1.0)
        built = bridge._from_status(exc, "market")
        assert isinstance(built, BridgeTimeout)
        print(f"503 -> withdrawal={built.withdrawal!r} phase={built.phase!r}")
        assert built.withdrawal == "claimed"
        assert built.phase == PHASE_SHIM_UNAVAILABLE

    def test_the_shim_still_sends_one_key_value_body_and_no_second_format(
        self, tmp_path: Path
    ) -> None:
        """One more line, not JSON (#135).

        `docs/MT4.md` specifies the `key=value` body and `decode` parses it. The
        refusal body must stay in that vocabulary, so an OLD desk against a NEW
        shim text-clips the extra key through `_short` and ignores it rather
        than failing to parse. Measured on the raw bytes, hand-built request, so
        the shim's own writing is what is under test.
        """
        with shim(tmp_path) as (_url, where):
            status, text = raw(
                where,
                request_text("POST", CALL_PATH, token=GOOD_TOKEN, body=encode("tick", {"symbol": "EURUSD"}, 4242)),
            )
        print(f"shim refusal body status={status} body={text!r}")
        assert status == STATUS_MAILBOX_TIMEOUT
        assert not text.lstrip().startswith("{"), "the shim answered JSON"
        parsed = decode(text)
        assert parsed.get("ok") in {0, "0"}
        assert parsed.get("error") == "mailbox_timeout"
        assert parsed.get("withdrawal") == "withdrawn"
