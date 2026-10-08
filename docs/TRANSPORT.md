# MT4 transport: how the desk reaches the terminal

Decision record for issue #73. Conrad ruled 2026-09-25: the EA transport moves off
same-box co-location **before the first paying customer**.

This file is the design. `docs/MT4.md` remains the ICD for what travels; this file
is about what carries it. The two are deliberately separate, because the whole
point of the decision below is that the ICD does not change.

## What was coupled, and why it had to move

`mt4/Experts/Mt4RiskBot.mq4` reaches the desk only through files in MT4's
`FILE_COMMON` directory: 2 `FileOpen`, 1 `FileWriteString`, 1 `FileReadString`,
3 `FileDelete`, 1 `FileMove` for the claim, and **zero** `WebRequest` or socket
calls. So the desk had to see that directory, which in practice means the same
Windows host. Measured consequence, 2026-09-25: the host rebooted, MT4 came back
healthy, and the desk was dead because it lost a startup race with the terminal.
#74 bounded that race. It did not remove the reason the race exists.

Sharing the folder over SMB is not the way out. `mt4_live.py` carries explicit
`PermissionError` retries because NTFS refuses to unlink a file while the terminal
holds the handle; that is a local-filesystem race, and putting a network hop
inside it makes a fragile thing worse.

## The two constraints, taken as inputs and not as footnotes

**(1) `WebRequest` in MQL4 is synchronous.** It blocks the calling EA thread for
the whole call. The EA's only scheduler is `OnTimer` (this Expert arms
`EventSetMillisecondTimer(100)`, falling back to `EventSetTimer(1)`) and `OnTick`,
both of which run on that one thread. A blocking call inside the EA therefore
stalls chart processing for its full duration, and on a dead endpoint for the full
timeout. Note the EA already blocks its own thread today: `SendRetry`,
`ModifyRetry`, `RollbackPosition` and `RollbackPending` each `Sleep(50)` up to 5
times, so a trade op can hold the thread for ~250 ms plus broker round trips.
Anything added is additive on top of that, not a first offence.

**(2) The direction of control inverts if, and only if, the EA becomes the
client.** Today the desk is the initiator: it writes a request, the EA picks it
up. If the EA calls out, then somebody of ours must already be listening, and the
questions of who binds a port, who authenticates whom, who retries, and what a
down desk looks like all get different answers. That inversion is a *choice*, and
the important finding in this document is that it is separable from moving the
transport off-box.

## Three shapes, not two

The issue framed this as file mailbox versus `WebRequest`. There are three.

| | **A. EA as decision client** | **B. Local relay shim (CHOSEN)** | **C. EA as transport client** |
| --- | --- | --- | --- |
| who initiates TCP | EA | desk | EA |
| who decides when to act | the customer's tick loop | the desk | the desk |
| what carries the ICD | a new request/response protocol | the existing `key=value` body over HTTP | the existing `key=value` body over HTTP |
| EA changes | rewrite | **none** | `Process()` replaced, `Handle()` and all 20 reply builders untouched |
| customer recompiles the `.ex4` | yes | **no** | yes |
| MT4 allowed-URL whitelist needed | yes | **no** | yes |
| blocks the EA thread on our latency | yes, the decision is in the call | **no** | only on a queue read |
| inbound port on our infrastructure | yes | **no** | no, if the rendezvous is a Worker |
| verifiable in this repo's CI | compiles only | **yes, both OS legs** | compiles only, behaviour still unverifiable |

### A is rejected

Under A the EA asks what to trade and the answer contains our risk engine, our
model call and our journal write. That puts our p99 inside a synchronous call on
the customer's chart thread, which is constraint (1) at its worst. It also moves
the halt, approve, auto and live gates behind the customer's polling: a gate that
can only be enforced while their terminal is calling is not a gate. And it
discards the ICD, which is the most expensive artifact here to change, because the
EA is the only thing a customer installs. Rejected, and not for re-proposal.

### C is right, later, and cannot be verified from here now

C is the honest version of "move the EA onto the network": make the EA a **dumb
transport client**, not a decision client. The desk still decides and still
initiates; it enqueues a request into a rendezvous the EA polls, the EA drains it,
runs the same `Handle(body)` dispatcher it runs today, and posts the same reply
back. The direction of the TCP connection inverts; the direction of control does
not. That bounds constraint (1) properly, because the EA's blocking call contains
a queue read and a bounded server hold, never our decision latency.

It is still the more expensive half, for reasons that are measured and not
aesthetic:

- The new code sits in the **one artifact a customer installs**. As of 2026-09-26
  CI does compile it (`.github/workflows/mt4-compile.yml`, straightedge#86), so the
  half of this bullet that said "a green CI run says nothing about whether the Expert
  builds" is retired: it now says exactly that. The half that matters here survives
  unchanged, and it is the expensive half: **a compile is not a behaviour test.**
  Nothing in CI exercises a `WebRequest` loop, a rendezvous, a retry path or a
  reconnect, and there is no MT4 terminal in CI to run an Expert in. Real money is
  behind this repo, so shipping an unverified rewrite of `Process()` is still not
  available, and the compile gate does not change that.
- It needs a rendezvous service, tenancy, revocation and a Worker deploy before
  the first byte moves.
- It adds a **manual, GUI-only, per-terminal** allowed-URL whitelist entry that
  cannot be set programmatically and whose absence fails as MQL4 error 4060 in a
  way that reads like a bug.

So C is the destination and B is the step that gets there while staying
verifiable. What B pre-pays is real but it is bounded, and overclaiming it would
be dishonest: B settles the **desk-side seam** (a `Call` over a network with a
correct error partition), the **auth and revocation model**, the **config shape**,
and the **operational model** of a desk that is not on the customer's box. It does
not pre-pay C's wire, because the HTTP roles swap: in B the desk is the client, in
C the EA is.

## B, the chosen transport

```
  our side                       the customer's Windows host
  ------------------------------ -------------------------------------------
  Engine -> Mt4Broker            (unchanged)
              |
              | Call = (op, payload) -> dict            (unchanged seam)
              v
          HttpBridge  --- HTTPS --->  mt4-shim  --->  FILE_COMMON mailbox
                                       ^                   |
                                       |                   v
                                    (same process)     Mt4RiskBot.mq4
                                                       (BYTE-FOR-BYTE UNCHANGED)
```

`Mt4Broker.__init__` already takes `call: Callable[[str, dict], dict]`. That is
the seam, it already existed, and a network transport is another implementation of
it. `FileBridge` keeps serving a co-located desk; `HttpBridge` serves a remote one.
`broker_for()` picks one.

The shim is `python -m straightedge mt4-shim` from this same package: it owns a
`FileBridge` against `FILE_COMMON`, and it turns one authenticated `POST
/mt4/call` into one mailbox round trip. It is a dumb pipe with an auth gate. It
holds no risk logic, no prompts, no model keys and no journal.

### Why the body is the existing `key=value` wire and not JSON

Because then there is exactly one wire format in the system. `encode()` and
`decode()` are reused byte-for-byte, the golden transcripts in
`tests/mt4_transcripts.py` still describe the bytes that cross the network, and
`docs/MT4.md` stays the single ICD. A JSON envelope would have added a second
serialization surface whose only job is to carry the first one.

```
POST /mt4/call HTTP/1.1
Authorization: Bearer <MT4_MAILBOX_TOKEN>
Content-Type: text/plain; charset=us-ascii

id=7
op=market
symbol=EURUSD
...
```

The 200 body is the `.res` body, unmodified, `id` included. `HttpBridge` accepts
it only when `id` matches the request, exactly as `FileBridge` does.

## What B does about constraint (1)

Nothing, because it does not apply. The EA is not modified, makes no `WebRequest`,
and its thread is never blocked by anything on the network. The synchronous-call
problem is not solved here; it is **not incurred**. That is the single strongest
argument for this shape, and it is the reason to take the less exciting option.

## What B does about constraint (2)

Nothing inverts. The desk remains the initiator, so every gate stays in the
process that owns it and stays enforceable on the desk's own clock:

- `HALT` / `/halt` still flattens on the desk's decision, not on the terminal's
  poll.
- daily-loss, drawdown, currency exposure, `risk_pct` sizing, `approve always`,
  `/auto`, `/live` and the real-money fuse are untouched, in `risk.py` and
  `engine.py`, where they already are.
- `journal.jsonl` stays the fill SSOT on our side.

The inbound network surface is **on the customer's host, not ours**: the shim
binds a listener, `HttpBridge` binds nothing. Our desk makes outbound calls only.
That is the direct answer to "an unauthenticated inbound port that can place
trades is not shippable": we do not open one, and the one that exists is
authenticated, is bound to loopback by default, and is exposed to us through an
outbound-only tunnel.

## Auth model

**A bearer token, required, with no off switch.**

- `MT4_MAILBOX_TOKEN` in the environment on **both** ends. It is never read from a
  config file, matching `docs/CONTRACT.md` ("Secrets belong in the environment").
  It is the ONE secret with no TOML key at all, so unlike the other nine there
  is no file fallback to fall back to (straightedge#139).
- The shim **refuses to start** with no token, or with a token shorter than 32
  characters. There is no unauthenticated mode and no flag that creates one: this
  endpoint places trades.
- Comparison is `hmac.compare_digest`.
- **Auth runs first, before the path, before the method, before the body is
  read**, mirroring `agent/test/auth-ordering.test.ts`. Every unauthenticated
  request gets `401` whatever it asked for, so a scanner learns nothing about which
  paths exist, and `tests/test_mt4_net_transport.py` asserts the stronger
  property: after a refused request, **no `.req` file was ever written to the
  mailbox**. A 401 that had already handed the request to the EA would be
  decoration.

**Transport security.** `http.server` has no TLS and this shim does not grow any.
It binds `127.0.0.1` by default and **refuses a non-loopback bind** unless
`--i-understand-plaintext` is passed, which is the same idiom as
`--i-accept-risk`. The supported exposure is a Cloudflare Tunnel on the customer's
host: `cloudflared` connects outbound to Cloudflare, TLS terminates at the edge,
and no inbound port is opened on the host at all. That also supplies the
revocation story, and it costs nothing at rest.

**Revocation, three independent levers, any one of which is sufficient:** rotate
`MT4_MAILBOX_TOKEN` and restart the shim; revoke the Cloudflare Access service
token; delete the tunnel. Per-customer keys per the estate's per-function key rule,
so revoking one customer touches no other.

## Failure semantics

The partition that matters is the one `Mt4Broker.startup_connect()` depends on.
`BridgeTimeout` means "nothing is answering yet", which waiting can fix and which
is retried; anything else means "something answered and is stating a diagnosis",
which waiting cannot fix and which is reported at once. **That partition has to
survive the HTTP hop, in both directions**, or one of two defects appears: a cold
boot becomes a fatal error and #74's fix is undone, or a wrong token is retried in
silence for three minutes, which is the same defect class as the boot bug.

It survives by being explicit, because the default would be wrong:
`urllib.error.HTTPError` subclasses `URLError` subclasses `OSError`, and
`startup_connect()` retries `OSError`. A naive handler would therefore retry a
401. `HttpBridge` catches `HTTPError` first, and `tests/test_mt4_net_transport.py`
drives that case red on purpose.

| What happened | Shim answers | `HttpBridge` raises | `transport`/`phase` | Retried at startup | Steady state |
| --- | --- | --- | --- | --- | --- |
| EA answered | `200` + `.res` body | nothing | n/a | n/a | the reply |
| EA did not answer in time | `504` | `BridgeTimeout` | `net`/`shim-mailbox` | **yes** | `_reconnect_broker()` |
| mailbox unwritable (`OSError`) | `503` | `BridgeTimeout` | `net`/`shim-unavailable` | **yes** | `_reconnect_broker()` |
| no answer at all inside the budget | nothing | `BridgeTimeout` | `net`/`connect` | **yes** | `_reconnect_broker()` |
| answer began and did not finish | partial | `BridgeTimeout` | `net`/`read` | **yes** | `_reconnect_broker()` |
| shim not started / host down | no answer (`URLError`) | `OSError` | n/a, not a timeout | **yes** | `_reconnect_broker()` |
| bad or missing token | `401` | `RuntimeError` | n/a | **no** | surfaces at once |
| wrong path or method | `401`, then `404` | `RuntimeError` | n/a | **no** | surfaces at once |
| body over 64 KiB | `413` | `RuntimeError` | n/a | **no** | surfaces at once |
| reply `id` does not match | `200` | `RuntimeError` | n/a | **no** | surfaces at once |
| EA replied `ok=0` | `200` | nothing; `ok=0` reaches the adapter | n/a | **no**, by design | the refusal |

**The `transport`/`phase` column is the #127 addition, and it is what makes this
table readable from a journal rather than only from this file.** Five of these
rows used to reach `journal.jsonl` as the one string `mt4 bridge timeout`, so on
the live desk a quiet terminal, a dead shim and a slow link were one fact with
one response. `BridgeTimeout` now carries both as attributes,
`Engine._reconnect_broker` writes them onto the `reconnect` row as
`cause_transport` and `cause_phase`, and `docs/MT4.md` ("A timeout says WHICH
transport and WHERE on it") is where an operator reads what to do about each.

**One thing this still cannot tell you, and it is named rather than papered
over.** On a `504` the shim's own `FileBridge` HAS withdrawn the request and
knows the answer (`withdrawn`, `claimed` or `locked`), but that answer goes only
to the shim's local log: the refusal body carries `ok=0` and a reason, and no
`withdrawal`. `HttpBridge._from_status` therefore reports `claimed`, which is the
unsafe reading, on purpose, because not knowing must never render as a clean bill
of health. Putting the real answer on the wire is a change to the shim's refusal
body and is tracked separately; it is not folded into #127.

**The shim times out before the desk does, on purpose.** `HttpBridge` allows the
op's own budget plus `NET_GRACE_SEC` (2 seconds), so the desk normally receives a
`504` that says which end gave up rather than a bare socket timeout that says
nothing. A client timeout that fired first would abandon a request the EA may
still be executing, and the orphan rule in `docs/MT4.md` (a claimed request is
never replayed) means the reply would simply be lost. Which budget an op gets, and
how the shim learns it from the request itself, is the next section: the
grace is the DESK's slack and is never the far end's.

**On the EA side, nothing changed.** Claim-by-rename, the terminal-wide singleton
lock, the refusal of a second attach, the two-step entry with `survivor_ticket`,
and the deliberate non-replay of an orphaned claim all still hold, because the EA
is the same file.

**Ordering is unchanged and the desk is still the one that waits.** `startup_connect()`
now also covers "the shim is not up yet", which is the same shape as "the terminal
is not up yet" and needs no new budget.

## Budgets, and the `ttl_ms` that crosses the wire

MT4 has two per-op budgets, and the split is a money decision rather than a tuning
one. A READ that times out is retried by the next step. A SEND that times out is
AMBIGUOUS: the order may be filled, in flight, or never sent, and the desk cannot
tell. So `mt4.timeout_ms` (default 5000) covers the ops that cannot move the book
and `mt4.send_timeout_ms` (default 7060) covers the ops that can.
`constants.MAILBOX_SEND_OPS` is the single place that partition is written down:
`market`, `working`, `modify_position`, `modify_working`, `cancel`, `close`,
`close_by`. `check_market` and `check_working` are READS, because the Expert
returns before `SendRetry` when `send` is false, so a stale one cannot move money.

The send budget is DERIVED and not chosen: `constants.derive_send_timeout_ms()`
sums a 1000 ms measured transport ceiling, the Expert's 950 ms worst-case `Sleep`
total, 360 ms of claim-open retries across two ladders (the claim read and the
reply write are both on the same `tries` knob), and 19 broker round trips at a
250 ms allowance. The desk has to outlast the Expert or it writes off a request the Expert
is still executing. That 250 ms is the one un-measured term; `docs/RUNBOOK.md`
("Unresolved sends") carries the table, the validation floor, and the measurement
it is still waiting for.

### The fence is a DURATION, never a deadline

`encode()` puts `ttl_ms` last on every request body. `HttpBridge._call` picks the
op's budget with `budget_for()` and states `ttl_ms` as that budget MINUS
`NET_GRACE_SEC`: the desk allows itself the extra 2 seconds and the far end must
not have them, or the far end becomes the one that outlasts the desk.

A duration rather than a deadline, because of this document's own premise. The desk
and the terminal are on different hosts, so a wall-clock deadline would be judged
against a clock that never agreed with the one that set it. The failure is not
symmetrical. When the terminal's clock runs BEHIND, every stale request looks
FRESH, and that is the direction that fires a trade nobody is waiting for any
more. A duration is measured entirely on the side that reads it: the Expert ages
the request file against the filesystem that HOLDS it, using an offset it measures
at `OnInit` by writing its own probe file, so `TimeLocal()` versus
`FILE_MODIFY_DATE` is measured rather than guessed (`CalibrateFileTime` and
`RequestAgeSec` in `mt4/Experts/Mt4RiskBot.mq4`).

A request the Expert refuses as stale comes back as
`ok=0 retcode=4109 error=request_expired survivor_ticket=0 age_sec=<n>`, which is
the "EA replied `ok=0`" row of the failure table above: a live peer stating a diagnosis,
reported at once and never retried.

### The shim sizes its mailbox wait FROM the request, clamped

`FileBridge.exchange` is what the shim serves a remote desk with, and it takes its
budget from the body's own `ttl_ms` rather than from its own configuration, clamped
to `max(self.timeout, self.send_timeout)`. A body that declares no ttl falls back
to the read budget.

Both halves earn their place. The desk decided how long its send is allowed to
take, and a shim that gave up at its own read budget would abandon a request the
Expert was still executing: that is the exact defect the split budget removes, put
back one hop away. The clamp is there because `ttl_ms` arrives over the network and
the shim is single-threaded by contract (one shim, one mailbox, one terminal), so
an unbounded value from the wire would hold the mailbox against everybody else.

The shim states both numbers at startup, so which end will give up first is
readable without inspecting a config:

```
mt4-shim: serving /mt4/call on 127.0.0.1:8730, mailbox <FILE_COMMON>, mailbox timeout <read>s read / <send>s send ceiling
```

On the shipped defaults that line reads `5.0s read / 6.9s send ceiling`.

**This is where the split first got it wrong, so the guard is named.** The first
version of `cmd_mt4_shim` passed only `mt4.timeout_ms` to `make_shim`. The clamp
then shortened a 7060 ms send ttl to 5000 ms and the shim gave up about 1.9 seconds
BEFORE the desk's send budget expired: the exact failure the split exists to
remove, reintroduced one hop away, and reachable only on the network transport,
which is the live topology. Measured rather than reasoned about, by driving the
shipped CLI and timing what the mailbox waited: 5.01 s against a 6.88 s desk
budget. `cmd_mt4_shim` now passes both, and
`tests/test_mt4_net_transport.py::TestFileBridgeExchange::test_the_shim_cli_gives_the_mailbox_the_desks_send_ceiling`
inspects the arguments the CLI actually builds the bridge with, because nothing
else in the suite passes `send_timeout_sec` and without it the gap could not go red.

## Measured numbers

Measured on this tree at `821bacf`, driving a real `Engine` over a counting
`Call`, 4 configured symbols:

| | mailbox round trips |
| --- | --- |
| `Engine.start()` | 47 (1 ping, 4 select, 1 account, 40 rates, 1 positions) |
| one `Engine.step_all()` | 8 (1 ping, 1 account, 2 positions, 4 rates) |

The 40 `rates` are the history preflight retrying a cold series up to 10 times per
symbol; a warm terminal costs 4. So the steady-state cost of moving the transport
off-box is **8 network round trips per step**, and the file leg inside the shim is
unchanged at up to 100 ms of EA timer latency per op. At a 50 ms RTT that is about
0.8 s per step against about 0.4 s today, against an `H1` strategy. Every op in
that count is a READ, so the budget for all of them is `mt4.timeout_ms` and it does
not need to change; the ops that got their own budget do not appear in a
steady-state step at all.

Measured against a REAL `straightedge mt4-shim` subprocess over loopback, with the
stand-in Expert from `tests/mt4_transcripts.py` polling the mailbox every 20 ms:
**32.4 ms per op** over 20 consecutive ops, and the shim's own log put 22 to 33 ms
of that inside the mailbox round trip. So the file leg dominates and the HTTP
framing is not the cost; in production the EA's 100 ms timer replaces the 20 ms
poll and the network RTT is added on top. The same run saw request ids reach the
Expert as 1 through 25 through the real process, so the desk's id survives the hop
untranslated, and a wrong token against the live process was refused `401` with the
mailbox directory still holding **0 files**.

## What this does NOT cover

- **It does not remove Python from the customer's host.** It removes the risk
  engine, the model access, the prompts, the journal and the secrets; what stays
  is a dumb pipe. The issue's target of "MT4 and an EA, nothing else" needs C.
- **It does not remove auto-logon, the Windows host, or MT4.** Those are required
  by MT4 itself, which is a GUI terminal that must be running in a logged-in
  session. No transport choice changes that, and the issue's comparison table
  credited `WebRequest` with removing them. It does not. The honest install-step
  delta for C is: minus Python, minus the scheduled task, **plus** a manual
  allowed-URL whitelist entry.
- **It does not keep the desk able to flatten during a network partition.** Today
  a co-located desk can only be cut off by MT4 dying; off-box, the network is a new
  way to be cut off, and that is a cost of #73 itself rather than of this option.
  What holds in that window is the **broker-side stop**: the EA attaches SL and TP
  on every entry (`docs/MT4.md`, "Two-step entry"), so a partition leaves positions
  protected by the broker's own server-side stops, not naked. That is why this is
  shippable. It is also why the two-step entry's `survivor_ticket` path matters
  more than it did, not less.
- **It does not make the shim highly available.** One shim, one terminal, one
  mailbox, per the existing singleton rule. If the shim dies, the desk reports it
  and reconnects when it returns; restart-on-failure is the scheduled task's job,
  exactly as it already is for the desk.
- **It does not add TLS to the shim.** That is deliberate; see the auth model.
- **It is not tested against a live MT4 terminal from CI, and cannot be.** Both
  ends of the *network* leg are exercised over real sockets on both CI OS legs,
  and once by hand against a real `mt4-shim` SUBPROCESS rather than an in-process
  listener, so the CLI, the config load, the bind refusals and the auth gate were
  each crossed as separate processes at least once.
  The *file* leg behind the shim is the same `FileBridge` that
  `tests/test_mt4_wire.py` already pins against a real directory. The terminal
  itself still needs the manual attach checklist in `docs/MT4.md`.

## What C needs when it is scheduled

Written down now so the decision does not have to be re-derived:

1. A rendezvous with **claim-once** semantics. A request handed out is never
   handed out twice, which is the network equivalent of claim-by-rename; a tick
   loop that retries a request that already placed an order is how you get a
   double position.
2. A **bounded server hold** on the EA's poll, so the blocked-thread window is a
   number we chose, not a timeout we inherited.
3. The `Handle(body)` dispatcher and all 20 reply builders in `Mt4RiskBot.mq4`
   reused unchanged. Only `Process()` is replaced. That is the measurement that
   makes C tractable: roughly 70 lines change and roughly 660 do not.
4. The allowed-URL whitelist as a **named install step with its own diagnosis**,
   because MQL4 error 4060 otherwise reads as a bug in our EA.
5. A decision from Conrad on **where the rendezvous and the desk run**, which is
   spend, and therefore not ours.
