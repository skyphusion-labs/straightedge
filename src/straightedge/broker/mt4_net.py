"""MT4 network transport: the desk off the terminal's Windows host (#73).

The design record is `docs/TRANSPORT.md`. The short version, because a reader
here needs the shape before the code:

`Mt4Broker` takes `call: Callable[[str, dict], dict]`. That seam already
existed, so a network transport is another implementation of it and nothing in
the engine, the risk gates or the Expert changes. This module holds BOTH ends of
that transport, in one file on purpose: they are two halves of one interface and
`docs/MT4.md` has already been the cost of letting two ends of a wire drift.

    Engine -> Mt4Broker -> HttpBridge --HTTPS--> mt4-shim -> FileBridge -> EA

What this deliberately does NOT do:

* It does not touch `mt4/Experts/Mt4RiskBot.mq4`. The Expert issues no
  `WebRequest`, so MQL4's synchronous-WebRequest problem is not solved here, it
  is NOT INCURRED. That is the strongest single argument for this shape.
* It does not invert the direction of control. The desk is still the initiator,
  so halt, daily loss, drawdown, `approve always`, `/auto` and the real-money
  fuse stay in the process that owns them and stay enforceable on the desk's own
  clock rather than on the customer terminal's poll.
* It opens no inbound port on OUR infrastructure. The listener is on the
  customer's host; the desk only ever makes outbound calls.

The body on the wire is the SAME `key=value` mailbox body `docs/MT4.md`
specifies, carried opaquely. There is exactly one wire format in this system and
this module does not add a second one.
"""

from __future__ import annotations

import hmac
import http.server
import ipaddress
import socket
import socketserver
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable, cast

from straightedge.broker.mt4_live import (
    PHASE_CONNECT,
    PHASE_READ,
    PHASE_SHIM_MAILBOX,
    PHASE_SHIM_UNAVAILABLE,
    WITHDRAWAL_WORDS,
    TRANSPORT_NET,
    BridgeTimeout,
    FileBridge,
    decode,
    encode,
)
from straightedge.constants import MAILBOX_SEND_OPS

#: The one path the shim serves. One path, one method: every other request is a
#: 404 AFTER the auth check, so an unauthenticated caller cannot map the surface.
CALL_PATH = "/mt4/call"

#: Bodies are the mailbox's own ASCII `key=value` lines, so the media type says
#: so rather than claiming JSON.
WIRE_CONTENT_TYPE = "text/plain; charset=us-ascii"

#: The shared secret's ONLY home. Never read from a TOML file: `docs/CONTRACT.md`
#: says secrets live in the environment, and this one can place trades.
TOKEN_ENV = "MT4_MAILBOX_TOKEN"

#: A token below this length is refused at STARTUP, on both ends. This endpoint
#: places orders on a real account; there is no value of this setting that turns
#: the gate off, and no flag that creates an unauthenticated mode.
MIN_TOKEN_CHARS = 32

#: Requests are one small `key=value` block. Anything larger is not one of ours.
MAX_REQUEST_BYTES = 64 * 1024

#: Replies can be genuinely large: a 400 bar `rates` reply is roughly 22 KiB.
#: The cap exists so a broken or hostile peer cannot stream without bound.
MAX_REPLY_BYTES = 1024 * 1024

#: Added to the desk's per-command budget to get its HTTP timeout, so the SHIM
#: is normally the end that gives up first and the desk receives a 504 that says
#: which end timed out instead of a bare socket timeout that says nothing. A
#: desk that timed out first would abandon a request the Expert may still be
#: executing, and `docs/MT4.md` never replays a claimed request, so that reply
#: would simply be lost.
NET_GRACE_SEC = 2.0

#: The default listening port. Arbitrary, documented, and loopback-only unless
#: the operator explicitly says otherwise.
DEFAULT_SHIM_PORT = 8730

#: `BridgeTimeout` on the shim's side of the wire. The desk turns it back into a
#: `BridgeTimeout`, which is what `Mt4Broker.startup_connect()` retries.
STATUS_MAILBOX_TIMEOUT = 504

#: The mailbox directory itself could not be written. Also transient at boot,
#: also retried: MT4 may not have created Common Files yet.
STATUS_MAILBOX_UNAVAILABLE = 503

#: Statuses the desk treats as "nothing is answering YET", which waiting can
#: fix. Everything else is a live peer stating a diagnosis, which waiting cannot
#: fix and which must surface at once. Getting this partition wrong in either
#: direction reintroduces a measured defect: retry a 401 for three minutes in
#: silence, or turn a cold boot into a fatal error and undo #74.
RETRYABLE_STATUSES = frozenset({STATUS_MAILBOX_TIMEOUT, STATUS_MAILBOX_UNAVAILABLE})


def require_token(token: str, *, where: str) -> str:
    """Return `token`, or raise. Fail-closed, at startup, on BOTH ends.

    An empty token is not "auth disabled", it is a misconfiguration, and the
    honest time to say so is before anything is listening or trading rather
    than on the first refused order.
    """
    value = (token or "").strip()
    if not value:
        raise RuntimeError(
            f"{where}: {TOKEN_ENV} is unset. The MT4 network transport has no "
            "unauthenticated mode: this endpoint places orders. Set the same "
            f"{TOKEN_ENV} on the desk and on the shim."
        )
    if len(value) < MIN_TOKEN_CHARS:
        raise RuntimeError(
            f"{where}: {TOKEN_ENV} is {len(value)} characters, minimum "
            f"{MIN_TOKEN_CHARS}. Generate one with "
            "`python -c \"import secrets; print(secrets.token_urlsafe(32))\"`."
        )
    return value


def _bearer(token: str) -> str:
    return f"Bearer {token}"


def _authorized(header: str | None, token: str) -> bool:
    """Constant-time comparison of the whole `Authorization` header value.

    Comparing the entire header rather than a parsed-out tail means a request
    with no header, a wrong scheme, extra whitespace or a wrong token all take
    the same path and produce the same answer.
    """
    return hmac.compare_digest((header or ""), _bearer(token))


# ---------------------------------------------------------------------------
# The desk's end.
# ---------------------------------------------------------------------------


class HttpBridge:
    """`Call` over HTTP. Drop-in for `FileBridge.call` in `broker_for`.

    Holds the request-id counter, exactly as `FileBridge` does, because the desk
    owns the id end to end and the shim forwards it untouched.
    """

    def __init__(
        self,
        url: str,
        token: str,
        *,
        timeout_sec: float = 5.0,
        send_timeout_sec: float | None = None,
        opener: Callable[..., Any] | None = None,
    ) -> None:
        if not url:
            raise RuntimeError(
                "mt4.mailbox_url is empty. Set it to the mt4-shim endpoint on "
                "the host that runs MetaTrader 4, or leave it unset and set "
                "mt4.files_dir for a co-located desk."
            )
        self.url = url
        self._token = require_token(token, where="mt4.mailbox_url is set but")
        #: The shim should give up first: see `NET_GRACE_SEC`.
        self.timeout = float(timeout_sec) + NET_GRACE_SEC
        #: Same grace on the send budget, for the same reason and with more at
        #: stake: the end that gives up first is the end that abandons a request
        #: the Expert may still be executing, and on a send that is the
        #: ambiguous-money case rather than a lost tick.
        self.send_timeout = (
            self.timeout
            if send_timeout_sec is None
            else float(send_timeout_sec) + NET_GRACE_SEC
        )
        self._n = 0
        self._lock = threading.Lock()
        self._opener = opener if opener is not None else urllib.request.urlopen

    def budget_for(self, op: str) -> float:
        """The HTTP timeout this op gets. Same partition as `FileBridge`."""
        return self.send_timeout if op in MAILBOX_SEND_OPS else self.timeout

    def call(self, op: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            self._n += 1
            return self._call(op, payload, self._n)

    def _call(self, op: str, payload: dict[str, Any], req_id: int) -> dict[str, Any]:
        budget = self.budget_for(op)
        # The ttl the SHIM reads to size its own mailbox wait, and the Expert
        # reads to fence the request, is the desk's budget MINUS the network
        # grace: the desk allows itself the extra, the far end must not.
        ttl_ms = int(max(0.0, budget - NET_GRACE_SEC) * 1000)
        body = encode(op, payload, req_id, ttl_ms=ttl_ms).encode("ascii", "replace")
        request = urllib.request.Request(
            self.url,
            data=body,
            method="POST",
            headers={
                "Authorization": _bearer(self._token),
                "Content-Type": WIRE_CONTENT_TYPE,
                "Accept": WIRE_CONTENT_TYPE,
            },
        )
        try:
            with self._opener(request, timeout=budget) as resp:
                raw = resp.read(MAX_REPLY_BYTES + 1)
        # HTTPError FIRST, and this ordering is the whole point. HTTPError
        # subclasses URLError subclasses OSError, and `startup_connect()` retries
        # OSError, so a handler that let a 401 fall through to the URLError arm
        # would retry a wrong token in silence for the full startup budget. That
        # is the same defect class as the boot bug #74 fixed. Driven red in
        # `tests/test_mt4_net_transport.py`.
        except urllib.error.HTTPError as exc:
            raise self._from_status(exc, op) from exc
        # A read that timed out after the headers arrived surfaces as
        # TimeoutError, which is an OSError but is NOT a URLError.
        except TimeoutError as exc:
            raise BridgeTimeout(
                f"mt4 net bridge timeout after {budget:.1f}s "
                f"transport={TRANSPORT_NET} phase={PHASE_READ} op={op}: {exc}",
                op=op,
                transport=TRANSPORT_NET,
                # READ, not CONNECT: this arm is reached once the request is on
                # the wire, so the shim may have the body. `connect` below is the
                # one that means nothing answered at all. They were one string
                # until #127 and they are not one fact.
                phase=PHASE_READ,
                # The request crossed the network and this end never had the
                # file, so nothing here can take it back. `claimed` is the
                # honest word for that, and it is the unsafe reading on
                # purpose.
                withdrawal="claimed",
                req_id=req_id,
            ) from exc
        except urllib.error.URLError as exc:
            if isinstance(exc.reason, TimeoutError):
                raise BridgeTimeout(
                    f"mt4 net bridge timeout after {budget:.1f}s "
                    f"transport={TRANSPORT_NET} phase={PHASE_CONNECT} "
                    f"op={op}: {exc.reason}",
                    op=op,
                    withdrawal="claimed",
                    req_id=req_id,
                    transport=TRANSPORT_NET,
                    phase=PHASE_CONNECT,
                ) from exc
            # Connection refused, DNS failure, host down. URLError IS an
            # OSError, and at startup that is ordinary: the shim may not be up
            # yet. Re-raised unchanged so `startup_connect()` retries it.
            raise
        if len(raw) > MAX_REPLY_BYTES:
            raise RuntimeError(
                f"mt4 net bridge: reply exceeded {MAX_REPLY_BYTES} bytes; "
                f"{self.url} is not answering the mailbox protocol"
            )
        data = decode(raw.decode("ascii", "replace"))
        got = int(data.get("id", 0) or 0)
        if got != req_id:
            # The shim forwards the desk's own id, so a mismatch is a confused
            # peer rather than a slow one. Waiting cannot fix it, so it is NOT a
            # BridgeTimeout and is NOT retried at startup.
            raise RuntimeError(
                f"mt4 net bridge: reply id {got} does not match request "
                f"{req_id}; {self.url} is not serving one mailbox per desk"
            )
        return data

    def _from_status(self, exc: urllib.error.HTTPError, op: str = "") -> Exception:
        # ONE read. `exc.read()` drains the stream, so the detail text and the
        # withdrawal word have to come out of the same pass; reading twice would
        # hand the second caller an empty body and a missing key reads as the
        # unsafe `claimed`, which would look like a working fallback instead of
        # a bug.
        body = _read_body(exc)
        detail = _clip(body) or _reason(exc)
        if exc.code in RETRYABLE_STATUSES:
            # The shim answered, and what it said is "the Expert has not replied
            # yet" or "the mailbox is not writable yet". Both are the cold-boot
            # shape, so the type is the one `startup_connect()` retries. They are
            # NOT the same diagnosis, though, and the phase is what keeps them
            # apart in the journal: 504 means the file leg behind the shim timed
            # out, 503 means that leg was never usable.
            phase = (
                PHASE_SHIM_MAILBOX
                if exc.code == STATUS_MAILBOX_TIMEOUT
                else PHASE_SHIM_UNAVAILABLE
            )
            return BridgeTimeout(
                f"mt4 net bridge: {exc.code} from the shim "
                f"transport={TRANSPORT_NET} phase={phase}"
                f"{(' op=' + op) if op else ''} ({detail})",
                op=op,
                transport=TRANSPORT_NET,
                phase=phase,
                # A 504 IS the shim reporting a mailbox timeout, and the
                # shim's own FileBridge has already withdrawn the request on
                # that path. As of #135 it SAYS which outcome it got, so this
                # end reads the measurement instead of assuming the worst.
                #
                # Only on 504. On a 503 the mailbox was never usable and
                # nothing was written, so there is no withdrawal to report and
                # reporting one would invent a measurement.
                withdrawal=(
                    _withdrawal(body)
                    if exc.code == STATUS_MAILBOX_TIMEOUT
                    else "claimed"
                ),
            )
        return RuntimeError(f"mt4 net bridge: HTTP {exc.code} from {self.url} ({detail})")


def _read_body(exc: urllib.error.HTTPError) -> str:
    """The shim's refusal body, once. Never the request body."""
    try:
        return exc.read().decode("ascii", "replace")
    except (OSError, ValueError):  # pragma: no cover - body already consumed
        return ""


def _clip(text: str) -> str:
    """One line, bounded. The shim is trusted to be terse, not assumed to be."""
    return " ".join(text.split())[:200]


def _reason(exc: urllib.error.HTTPError) -> str:
    return exc.reason if isinstance(exc.reason, str) else "no reason given"


def _withdrawal(body: str) -> str:
    """What the far end did with the request, as the far end reports it (#135).

    The shim's `FileBridge` measured this: the `.req` file either came back off
    the shared name or it did not. Before #135 the fact reached the shim's local
    log and stopped there, so every provably-withdrawn send off-box was read as
    ambiguous money and reconciled by hand.

    ABSENT OR UNRECOGNISED FALLS BACK TO `claimed`, ALWAYS, and that direction
    is the whole safety property. An old shim sends no key; a confused one sends
    a word this desk does not know. Neither is a measurement, and `claimed` is
    what "I do not know" has to render as, because `withdrawn` is the only word
    that lets the desk stop worrying about an order it cannot see.
    """
    word = str(decode(body).get("withdrawal", "") or "")
    return word if word in WITHDRAWAL_WORDS else "claimed"


# ---------------------------------------------------------------------------
# The customer host's end.
# ---------------------------------------------------------------------------


class _ShimServer(http.server.HTTPServer):
    """Single-threaded on purpose.

    The mailbox is a singleton by contract ("Do not run two bots against one
    mailbox", `docs/MT4.md`). Serving it from one thread makes that invariant
    STRUCTURAL instead of a rule an operator has to remember: this process
    cannot interleave two requests into one mailbox even if two clients arrive.
    A second caller waits in the listen backlog, bounded by its own timeout.
    """

    #: What `http.server.HTTPServer` already sets, restated because it is a
    #: decision here and not an accident: a shim that could not rebind for a
    #: minute would be down after every restart while the terminal is already
    #: up. The caveat is Windows, where SO_REUSEADDR also lets a SECOND process
    #: bind the same port. That is not a new exposure, it is the same rule the
    #: mailbox already carries: one shim, one terminal, one mailbox.
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        bridge: FileBridge,
        token: str,
        log: Callable[[str], None],
    ) -> None:
        self.bridge = bridge
        self.token = token
        self.log = log
        # Chosen from the host, before `TCPServer.__init__` creates the socket.
        # An IPv6 literal against the AF_INET default fails in getaddrinfo with
        # "nodename nor servname provided", which reads as a DNS problem rather
        # than as an address-family mismatch; measured when `--host ::1` could
        # not bind at all.
        if _is_ipv6(address[0]):
            self.address_family = socket.AF_INET6
        super().__init__(address, _ShimHandler)


class _ShimHandler(http.server.BaseHTTPRequestHandler):
    """One authenticated POST, one mailbox round trip. Nothing else.

    The shim holds no risk logic, no prompts, no model keys and no journal. It is
    a pipe with an auth gate, and it does NOT validate `op`: the Expert already
    answers an unknown op with `ok=0`, and a second op whitelist here would be a
    second place to edit whenever an op is added, failing closed in a way that
    reads like a broken terminal.
    """

    #: HTTP/1.0, so every response closes the connection. With a single-threaded
    #: server, keep-alive would let one idle client hold the only serving thread.
    protocol_version = "HTTP/1.0"
    server_version = "straightedge-mt4-shim"
    sys_version = ""

    def __getattr__(self, name: str) -> Any:
        """Route EVERY HTTP method into `_serve`, including the exotic ones.

        `BaseHTTPRequestHandler` answers a method it has no `do_<M>` for with its
        own 501, which happens BEFORE any code here runs and therefore before the
        auth check. That would make an unauthenticated `PROPFIND` distinguishable
        from an unauthenticated `POST`, which is exactly the surface mapping the
        auth-first ordering exists to deny. Resolving every `do_*` here makes the
        coverage total rather than a list someone has to keep complete.
        """
        if name.startswith("do_"):
            return self._serve
        raise AttributeError(name)

    @property
    def _shim(self) -> _ShimServer:
        return cast(_ShimServer, self.server)

    def _serve(self) -> None:
        started = time.monotonic()
        # AUTH FIRST: before the path, before the method, before a single byte of
        # the body is read. Mirrors `agent/test/auth-ordering.test.ts`, and the
        # property the tests assert is the strong one: after a refused request no
        # `.req` file was ever written, so nothing reached the Expert. A 401 that
        # had already handed the order over would be decoration.
        if not _authorized(self.headers.get("Authorization"), self._shim.token):
            self._refuse(401, "unauthorized")
            return
        if self.command != "POST" or self.path != CALL_PATH:
            # Reported only to an AUTHENTICATED caller, so a scanner learns
            # nothing about which paths exist here.
            self._refuse(404, "not_found")
            return
        raw_len = self.headers.get("Content-Length")
        try:
            length = int(raw_len or "")
        except ValueError:
            self._refuse(411, "length_required")
            return
        if length < 0 or length > MAX_REQUEST_BYTES:
            self._refuse(413, "request_too_large")
            return
        body = self.rfile.read(length).decode("ascii", "replace")
        req_id = int(decode(body).get("id", 0) or 0)
        if req_id <= 0:
            # The shim must know which reply id to wait for, so this is the one
            # field it parses. Everything else travels opaquely.
            self._refuse(400, "missing_request_id")
            return
        try:
            reply = self._shim.bridge.exchange(body, req_id)
        except BridgeTimeout as exc:
            # The withdrawal travels on the WIRE, not only into our own log: the
            # desk is the end that has to decide whether an order can still
            # fire, and this process is the only one that knows (#135).
            self._refuse(
                STATUS_MAILBOX_TIMEOUT,
                "mailbox_timeout",
                detail=str(exc),
                withdrawal=exc.withdrawal,
            )
            return
        except OSError as exc:
            self._refuse(
                STATUS_MAILBOX_UNAVAILABLE, "mailbox_unavailable", detail=str(exc)
            )
            return
        except Exception as exc:  # pragma: no cover - defensive
            # A handler that raises sends nothing, and an empty reply reads to
            # the desk as "the shim is not up yet", which is retried for the
            # whole startup budget. A named 500 is not retried, which is the
            # honest answer for a bug in here.
            self._refuse(500, "shim_error", detail=repr(exc))
            return
        payload = reply.encode("ascii", "replace")
        self.send_response(200)
        self.send_header("Content-Type", WIRE_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)
        self._shim.log(
            f"mt4-shim: id={req_id} 200 in {(time.monotonic() - started) * 1000:.0f}ms"
        )

    def _refuse(
        self, status: int, reason: str, *, detail: str = "", withdrawal: str = ""
    ) -> None:
        """A refusal in the mailbox's own vocabulary, so the desk can read it.

        `ok=0` plus a named `error` is what every Expert refusal looks like, so a
        shim refusal needs no second format to parse.

        `withdrawal` is ONE MORE LINE in that same `key=value` body, never a
        second format (#135). An old desk clips the extra key out of the message
        text and ignores it, and a new desk against an old shim sees no key and
        falls back to `claimed`, so the two halves deploy in either order. That
        matters because the shim runs on the customer's host.

        A word outside `WITHDRAWAL_WORDS` is not written at all, so this end
        cannot teach the desk a vocabulary the desk does not have.
        """
        lines = [f"ok=0\nerror={reason}\n"]
        if withdrawal in WITHDRAWAL_WORDS:
            lines.append(f"withdrawal={withdrawal}\n")
        body = "".join(lines).encode("ascii")
        self.send_response(status)
        self.send_header("Content-Type", WIRE_CONTENT_TYPE)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
        # The peer address, the status and our own reason only. The request line
        # and the headers are attacker-controlled on a refused request, and the
        # body carries order parameters; none of it is echoed into the log.
        peer = self.client_address[0] if self.client_address else "?"
        extra = f" ({detail[:200]})" if detail else ""
        self._shim.log(f"mt4-shim: {peer} {status} {reason}{extra}")

    def log_message(self, fmt: str, *args: Any) -> None:
        """Silence the base class's own logging.

        Its default line includes `self.requestline`, which is attacker-supplied
        text on exactly the requests that get logged. Everything worth recording
        is logged explicitly above.
        """
        return


def _log_line(msg: str) -> None:
    """Flushed per line, for the same reason `mt4_live._log_line` is.

    Under Windows Task Scheduler stdout is a redirected file and Python
    block-buffers it, so an unflushed shim log arrives long after the event it
    describes, or never.
    """
    print(msg, flush=True)


def _is_loopback(host: str) -> bool:
    if host in {"localhost", ""}:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _is_ipv6(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).version == 6
    except ValueError:
        return False


def make_shim(
    *,
    files_dir: str | Path,
    token: str,
    host: str = "127.0.0.1",
    port: int = DEFAULT_SHIM_PORT,
    timeout_sec: float = 5.0,
    send_timeout_sec: float | None = None,
    allow_plaintext_exposure: bool = False,
    log: Callable[[str], None] | None = None,
) -> socketserver.BaseServer:
    """Build the shim listener. Refuses to exist misconfigured.

    Three refusals, all at startup rather than at the first request:

    * no `files_dir`: there is no mailbox to serve.
    * no token, or a short one: see `require_token`.
    * a non-loopback bind without `allow_plaintext_exposure`: `http.server` has
      no TLS and this module does not grow any. The supported exposure is a
      Cloudflare Tunnel, which connects OUTBOUND from this host, terminates TLS
      at the edge, and opens no inbound port at all. The flag exists for an
      operator who has their own terminator in front and says so explicitly,
      which is the same idiom as `--i-accept-risk`.
    """
    sink = log if log is not None else _log_line
    path = str(files_dir or "").strip()
    if not path:
        raise RuntimeError(
            "mt4-shim: mt4.files_dir is empty. The shim runs ON the MetaTrader 4 "
            "host and serves its Common Files mailbox, so it needs that path. "
            "Windows default: %APPDATA%\\MetaQuotes\\Terminal\\Common\\Files"
        )
    checked = require_token(token, where="mt4-shim")
    if not _is_loopback(host) and not allow_plaintext_exposure:
        raise RuntimeError(
            f"mt4-shim: refusing to bind {host} in plaintext. This endpoint "
            "places orders and http.server has no TLS. Expose it with a "
            "Cloudflare Tunnel (outbound only, no inbound port) and keep the "
            "listener on 127.0.0.1, or pass --i-understand-plaintext if you are "
            "terminating TLS in front of it yourself."
        )
    # `FileBridge.exchange` takes the budget from the request's own `ttl_ms`,
    # clamped to this ceiling. The shim must not shorten a send: the end that
    # gives up first is the end that abandons a request the Expert is still
    # executing.
    bridge = FileBridge(
        path, timeout_sec=timeout_sec, send_timeout_sec=send_timeout_sec
    )
    server = _ShimServer((host, port), bridge, checked, sink)
    # `server_address` is typed loosely enough to include a bytes AF_UNIX path;
    # this is an AF_INET listener, so name the two fields we actually bound.
    bound = cast("tuple[str, int]", server.server_address)
    sink(
        f"mt4-shim: serving {CALL_PATH} on {bound[0]}:{bound[1]}, "
        f"mailbox {path}, mailbox timeout {timeout_sec:.1f}s read / "
        f"{bridge.send_timeout:.1f}s send ceiling"
    )
    if not _is_loopback(host):
        sink(
            "mt4-shim: WARNING plaintext listener on a routable address; "
            "TLS must be terminated in front of it"
        )
    return server


def serve(server: socketserver.BaseServer) -> int:
    """Serve until interrupted. Returns a process exit code."""
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("mt4-shim: interrupt", file=sys.stderr)
    finally:
        server.server_close()
    return 0
