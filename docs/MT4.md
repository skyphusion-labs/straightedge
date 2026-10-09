# MT4 mailbox ICD

MetaTrader 4 has no official Python package.
The bot does not load a DLL into the terminal.
The owned interface is a file mailbox in Common Files.

The Expert is `mt4/Experts/Mt4RiskBot.mq4`.
Python is `straightedge.broker.mt4_live`.
Engine still sends `MarketOrder` and `WorkingOrder` only.

## Two transports, one ICD

Everything below describes what travels. **Who carries it is now a choice**, and
`docs/TRANSPORT.md` is the decision record (#73).

| `mt4.mailbox_url` | Transport | The desk runs |
| --- | --- | --- |
| unset | the file mailbox described below | on the MetaTrader 4 host |
| set | HTTPS to `straightedge mt4-shim` on the MT4 host | anywhere |

The Expert is **byte-for-byte the same file either way**: it issues no
`WebRequest` and knows nothing about the network. The shim runs beside the
terminal, owns this mailbox, and turns one authenticated `POST /mt4/call` into
one round trip through it. The body on the wire is the same `key=value` block
this document specifies, carried opaquely, so every line below still applies.

## Files

Directory: `mt4.files_dir` or `MT4_FILES_DIR`.
That path is Terminal Common Files, not the data folder for one install.

Windows default if the key is empty:

```
%APPDATA%\MetaQuotes\Terminal\Common\Files
```

`%APPDATA%` in a configured path expands. `journal.lock` uses `msvcrt.locking`
there. With no `mt4.mailbox_url`, the bot and the terminal must run on the same
Windows host; with one, only the shim does.

| File | Writer | Reader |
| --- | --- | --- |
| `mt4_risk_bot.req` | Python | Expert |
| `mt4_risk_bot.res` | Expert | Python |

Python writes `.req.tmp` and replaces it onto `.req`.
The Expert CLAIMS `.req` by renaming it to `mt4_risk_bot.req.claim.<chart id>`
before it reads a byte, and reads the body from the claimed path.
The Expert writes `.res` in one open/write/flush/close.
Python accepts `.res` only when `id` matches the request.

The mailbox is global under Common Files.
Do not run two bots against one mailbox.

### One Expert, enforced

"Attach to one chart" used to be a rule the operator had to remember, on the
order-execution path. It is now what the software enforces, in two layers.

**The claim.** `Process()` never reads the shared name. It takes a terminal-wide
mutex (`GlobalVariableSetOnCondition`, the one primitive MQL4 documents as
atomic, and documents for exactly this: "a mutex at interaction of several
Expert Advisors working simultaneously within one client terminal"), then renames
`mt4_risk_bot.req` to `mt4_risk_bot.req.claim.<chart id>` and reads the body from
there. A second Expert reaching the same point finds the source gone, logs
`claim lost`, and executes nothing. Before this, the Expert read the whole body
from the shared name and deleted it AFTERWARDS, so two instances both got the
full request and both called `OrderSend`: one requested trade, two positions,
double the sized risk, and only one of the two in the journal, which corrupts
every drawdown and exposure figure derived from it.

**The singleton.** `OnInit()` takes a second terminal-wide lock and returns
`INIT_FAILED` when another instance holds it, so a duplicate attach fails loudly
instead of running:

```
mt4riskbot REFUSING TO START: another straightedge Expert is already running in
this terminal and owns the mt4_risk_bot mailbox (holder last seen 0s ago).
Attach this Expert to exactly ONE chart. Two instances would both send the same
order, so this one is stopping.
```

So if you do attach a second chart: the second Expert does not start, the line
above appears in the Experts log, the first Expert keeps trading, and no order is
duplicated. The holder refreshes its lock on every timer tick and every market
tick and releases it in `OnDeinit`, so a crashed instance frees it after
`SingletonStaleSeconds` (default 15) and a legitimate restart is never blocked.
Both locks are temporary globals, which MT4 deletes at terminal shutdown, so a
terminal crash cannot leave one behind on disk.

**Scope, stated plainly.** MQL4's atomic guarantee is per TERMINAL. Two separate
MT4 terminals on one host share Common Files and would contend for the same
mailbox; there the rename is the only barrier, and MQL4 does not document
`FileMove` as atomic. Run one terminal against one mailbox.

**Orphans.** A crash between the rename and the reply leaves
`mt4_risk_bot.req.claim.<chart id>` behind. That file is not the mailbox, so it
blocks nothing and no other instance waits on it; the same chart overwrites it on
its next claim (`FILE_REWRITE`). The orphaned request is deliberately NOT
replayed. The adapter times out and reports no result, which is the safe answer;
replaying a claimed order after a restart is how you get back the duplicate this
change removed.

## Line format

ASCII. LF. One `key=value` per line.
No nested JSON.
Lists are `n=` plus `row0=`, `row1=`, ...
Row fields are `|` separated.
Comments are clipped to 31 characters (MT4 `OrderSend` limit).

### Both ends sanitise, and neither trusts the other

`|`, CR and LF are the only structure this protocol has, so no value may
contain one. `|` becomes `/`; CR and LF each become a space.

BOTH ends apply that rule to everything they write, because both ends handle
text the other did not produce:

- Python, outbound, in `_wire()`: order comments come from config and could
  carry anything.
- The Expert, inbound, in `Wire()`: `OrderComment()`, `OrderSymbol()`,
  `AccountName()`, `AccountServer()` and `AccountCurrency()` are whatever the
  BROKER put there. Brokers do append annotations to comments (`[sl]`,
  `from #123`), so a pipe arriving from the terminal is ordinary, not exotic.

Sanitising on only one side is not a half-measure, it is the whole defect. A
position row is 13 pipe-separated fields with the comment at index 10, so one
extra pipe moves `swap` onto the comment tail and `time` onto `swap`, and
`positions()` raises out of `float()`. The desk then cannot enumerate its own
book at all.

One asymmetry, on purpose: `_wire()` also forces ASCII (non-ASCII becomes `?`),
and `Wire()` does not, because MQL4 has no cheap equivalent. Non-ASCII broker
text therefore reaches the adapter as the terminal's code page renders it. It
cannot break framing, which is what the rule protects.

Request:

```
id=1
op=market
symbol=EURUSD
side=buy
volume=0.1
sl=1.09000
tp=1.12000
comment=straightedge 3f1c8a02
magic=20260909
deviation=20
client_id=3f1c8a02
ttl_ms=7060
```

`id` is first and `op` is second, always. `ttl_ms` is last. Every other field is
optional and absent rather than empty when it does not apply.

`client_id` is the desk's idempotency key for this order, minted once when the
order was STAGED and unchanged across a retry or a process restart. It is also
prefixed into `comment`, which is how it reaches the BOOK: MT4 returns
`OrderComment()` on a position, so a key visible there attributes that position to
one send. That attribution is corroboration and NOT the dedupe mechanism -- brokers
append to and overwrite `OrderComment`, so a key MISSING from the book proves
nothing. The guarantee is the desk-side ledger (see below).

`deviation` is the maximum tolerated slippage in POINTS, resolved by the desk per
symbol (`docs/CONTRACT.md`), and it is sent on `market`, `working` and `close`.
The Expert falls back to its own `input int Slippage` only when the value is
absent or <= 0, which is what an older desk produces; a 0 configured on the desk
side is refused by `cfg.validate()` rather than handed over silently. It reached
`working` in issue #92 and not before: until then that handler passed its own
input to `OrderSend` while the desk was gating the same signal on the operator's
figure. **MT4 documents the `OrderSend` slippage parameter as IGNORED for pending
order types. That has not been measured on this rig**, so the field is
transmitted rather than either assumed to matter or assumed not to; if it is
ignored, what the change buys on this op is a journal record that is true.

`ttl_ms` is how long this request stays executable, counted from when it was
written. It is a DURATION and not a deadline, and that is load-bearing: with
`mt4.mailbox_url` the desk and the terminal are on different hosts, so a
wall-clock deadline would have to survive clock skew between them, and skew in the
wrong direction makes a stale request look FRESH. A duration has no clock domain;
the Expert measures the age on the filesystem that holds the file, against its own
clock, after calibrating how that clock relates to a file timestamp.

An Expert that predates `ttl_ms` ignores it and behaves exactly as before. That is
why the desk ALSO withdraws an abandoned request itself; neither layer is trusted
alone.

Success reply:

```
id=1
ok=1
ticket=123456
volume=0.10
price=1.10020
```

Failure reply:

```
id=1
ok=0
retcode=130
error=invalid_stops
```

`ok` is `1` or `0`.
`retcode` on failure is the MT4 `GetLastError` for the call that failed, or a
code the Expert chose for a check it performed itself (`130` for its own stops
check, `131` for volume, `133` for trade-not-allowed, `4108` for a ticket it
could not find).
Python maps those onto `TRADE_RETCODE_*` so `OrderResult.ok` stays venue-neutral.

`retcode=0` on a failure means the Expert did not report a reason. It is NOT a
broker rejection, and the adapter does not present it as one: it reports
`RETCODE_UNKNOWN`, `OrderResult.measured` is false, and the comment says the
reason was not reported. An Expert older than 1.3.1 produces this on every
failed send and modify, because `GetLastError()` clears the error register when
it is read and the retry helpers read it first. Recompile and reattach
`Mt4RiskBot.mq4` if you see it.

## Ops

| op | Meaning |
| --- | --- |
| `ping` | Expert is attached and the mailbox is live. |
| `account` | Balance, equity, `trade_mode` (`0` demo / `2` real), `trade_allowed`. |
| `symbol` | Spec for one name. |
| `tick` | Bid and ask. |
| `rates` | Bars, oldest first. `timeframe` is `H1`, not an MT5 integer. Also selects the symbol, asks the terminal to fetch the series if it is short, and reports `bars_total`, `selected` and `history_error`. See History below. |
| `select` | `SymbolSelect`. |
| `positions` / `orders` | Open positions or working orders. `magic=0` means all. |
| `check_market` / `market` | Immediate buy or sell. Check does not send. |
| `check_working` / `working` | Limit or stop. Check does not send. |
| `modify_position` | SL/TP on an open position. |
| `modify_working` | Price or SL/TP on a working order. |
| `cancel` | `OrderDelete`. |
| `close` | `OrderClose` (partial volume allowed). |
| `close_by` | `OrderCloseBy`. Hedge accounts only. |

MT4 has no `OrderCheck`. `check_*` is the Expert validating volume and stops.

`ping` also declares the Expert to the desk:

```
id=1
ok=1
time=1790389600
ladder_ms=950
broker_calls=19
fence=1
```

`ladder_ms` is the worst-case total of the Expert's own `Sleep` inside one
`Process()` call, `broker_calls` is how many broker round trips it can make in
that call, and `fence` says whether it can measure a request's age. The desk
checks its send budget against `ladder_ms` on every connect and warns loudly when
it does not clear; `doctor --connect` prints the verdict and exits non-zero on
`TOO SHORT`. These are declared rather than documented because the ladder bounds
reach the Expert as `input` parameters, so the attached Expert can differ from the
one in this repo with nothing saying so. A number in a runbook cannot go red.

Absent means an Expert too old to answer, which is reported as `NOT MEASURED` and
never as a pass.

### Ops that can move money get a different budget

| Class | Ops | Budget |
| --- | --- | --- |
| Read | `ping`, `account`, `symbol`, `tick`, `select`, `rates`, `positions`, `orders`, `check_market`, `check_working` | `mt4.timeout_ms`, default 5000 |
| Send | `market`, `working`, `modify_position`, `modify_working`, `cancel`, `close`, `close_by` | `mt4.send_timeout_ms`, default 7060 |

`check_market` and `check_working` are READS: the Expert returns before
`SendRetry` when `send` is false, so they cannot change the book. The partition
lives in `constants.MAILBOX_SEND_OPS` and in the Expert's `IsSendOp()`, and
`tests/test_mt4_stale_request_fence.py` asserts the two lists are identical,
because that is the only place the two languages are ever compared.

### request_expired

```
id=1
ok=0
retcode=4109
error=request_expired
survivor_ticket=0
age_sec=412
```

The Expert found the request older than its own `ttl_ms` and refused it without
reaching `OrderSend`. `survivor_ticket=0` here is a real measurement rather than a
default: the refusal happens before anything touches the book, so the Expert
genuinely knows nothing survived. `age_sec` is `-1` when the age could not be
measured at all, and an unmeasurable age refuses a SEND and allows a READ.

## History is per symbol AND timeframe

MT4 keeps a separate price series for every symbol/timeframe pair, and it builds
one only when something asks for it. A terminal with H4 charts open and a desk
configured for H1 therefore has H1 history for nothing. Measured on the rig:

```
EURUSD  bars=0    ATR=nan
USDJPY  bars=0    ATR=nan
XAUUSD  bars=200  ATR=17.2188
```

XAUUSD was the only symbol with an H1 chart. Nothing failed: `step_symbol` asked
for bars, got none, and returned, so two configured symbols were untradeable and
nothing said so.

**The operator opens no charts.** The desk asks for every configured symbol's
series at startup, which is what makes the terminal request it from the server,
and waits up to ten attempts one second apart. The ask is the fix.

`RatesReply` is what makes that possible, and it does three things the earlier
version did not:

- It calls `SymbolSelect` itself. A symbol absent from Market Watch has no series
  to serve, and `rates` previously depended on `Engine.start()` having selected
  it first, which is call order rather than a guarantee.
- When it holds fewer bars than were asked for, it touches the series (`iClose`
  on bar 0) before deciding there is nothing to send. Under the old code
  `iBars() == 0` set the row count to zero, so the loop never ran and no
  price-series function was reached at all; `iBars` alone is not the documented
  download trigger.
- It reports the state of the series on every reply, healthy or not:

| field | Meaning |
| --- | --- |
| `bars_total` | Bars the terminal holds for this symbol/timeframe, which can exceed `n`. |
| `selected` | `1` if `SymbolSelect` succeeded. `0` means the symbol is not in Market Watch. |
| `history_error` | `GetLastError()` after the touch. `4066` `ERR_HISTORY_WILL_UPDATED` means the download is in flight; `4073` `ERR_NO_HISTORY_DATA` means the terminal has none and is not fetching; `0` means it reported no error. |

Without `history_error`, `ok=1 n=0` meant three different things at once
(downloading now, not served under this name, genuinely empty) and the desk could
only read the reassuring one. It now tells "wait" apart from "the symbol name is
wrong", which is the difference between a nine-second startup pause and a run
that must not begin.

There is no `Sleep` in the handler and there must not be. `Process()` is a
single-threaded mailbox behind `gBusy` and the adapter's bridge times out at
5 seconds, so a wait in there would stall every other op. The wait lives in
`straightedge/history.py`.

An Expert older than this emits `n=` and the rows only. The adapter reports the
three fields as `None` in that case, never as zeros, so "the Expert cannot
answer" stays distinct from "the Expert answered zero". Recompile and reattach
`Mt4RiskBot.mq4` to get the fields.

## Two-step entry, and what happens when step two fails

A market send is two calls, not one. `OrderSend` opens the position with SL and
TP at 0, then `OrderModify` attaches the stop. That is for ECN brokers that
reject stops on the first send. Between the two calls the position is open and
has no stop. The window is one broker round trip plus up to 5 modify retries at
50 ms each.

MT5 does not have this window. It attaches the stop in the same call as the
entry, so nothing in this section applies to the MT5 adapter.

If the modify fails, the Expert rolls the position back. The rollback is
verified against the book, not against the return value of `OrderClose`: the
Expert re-reads the ticket and only reports success when `OrderCloseTime()`
shows it is gone. Failing to SELECT the ticket is not treated as a reason to
give up on it either; that path rolls back too.

Three outcomes, and the reply says which one happened:

| Outcome | Reply | Meaning |
| --- | --- | --- |
| stop attached | `ok=1` | The position is open and protected. |
| rollback verified | `ok=0`, `survivor_ticket=0` | Nothing is open. A clean failure. |
| rollback NOT verified | `ok=0`, `survivor_ticket=<ticket>`, `error=sl_modify_failed_position_live` | The position is STILL OPEN and has NO STOP. |

The third row is the one that matters. The send failed and something is live.
The adapter puts the ticket on `OrderResult.survivor_ticket`, and the engine
writes a `unmanaged_position` journal event and tells the operator in words.
Deal with it in the terminal: the bot does not size, manage, or stop out a
position it did not record.

`check_working` / `working` behave the same way, with `OrderDelete` instead of
`OrderClose` and `sl_modify_failed_order_live` instead.

### survivor_ticket absent is not survivor_ticket zero

Every failure reply from `market`, `working`, `check_market` and `check_working`
carries `survivor_ticket`. An Expert older than version 1.2.0 does not send the
field at all. The adapter reports that as `survivor_ticket = None`, which is
COULD NOT MEASURE, and the engine writes `survivor_unknown`. It is NOT reported
as zero, because "the Expert did not answer" and "the Expert checked and
nothing survived" are different facts and only one of them is safe.

If you see `survivor_unknown` with NO reason reported, recompile and reattach
`Mt4RiskBot.mq4`. If it carries `send_timeout_outcome_unknown`, the Expert is
already current and deliberately could not settle the book: the order may be on
the book, so check the terminal before re-staging and do NOT reattach the Expert
while an unstopped position may be live.

### A send that TIMED OUT is not a send that failed

`SendRetry` USED TO retry `OrderSend` on three MQL4 errors, and only two of them
are safe to repeat. It now retries on those two only:

| Error | Meaning | Repeating it |
| --- | --- | --- |
| 146 `ERR_TRADE_CONTEXT_BUSY` | another trade is in progress; nothing was sent | safe, and retried |
| 141 `ERR_TOO_MANY_REQUESTS` | throttled; nothing was sent | safe, and retried |
| 128 `ERR_TRADE_TIMEOUT` | the request REACHED the server and the reply did not come back | **opens a second position** |

128 does not mean the order failed. It means the outcome is unknown. The Expert
used to re-send 50ms later, so a timed-out order that actually filled was sent
again and the desk only ever learned the second ticket.

The ladder now STOPS at 128 and the BOOK decides, which is the discipline
`RollbackPosition` already follows in the same Expert: `OrderClose`'s return
value is a claim, and the book is the artifact. `FindByClientId` scans
`MODE_TRADES` (open AND pending) for an order carrying this desk's `magic` and
this send's `client_id`.

* **Found.** The send DID land. The Expert returns that ticket and the reply is
  an ordinary `ok=1`, because the order exists and nothing is ambiguous.
* **Not found.** This proves nothing, and the Expert does not treat it as a
  failure. Brokers append to and overwrite `OrderComment`, so the key can vanish
  from the book, and a fill may simply not be in the local pool yet. A lookup
  that re-sent on "not found" would have its failure in the DANGEROUS direction.

The not-found reply is:

```
ok=0
retcode=128
error=send_timeout_outcome_unknown
```

**`survivor_ticket` is deliberately ABSENT, and this is the one trade reply that
omits it.** Per the section above, an absent field is COULD NOT MEASURE and the
adapter reports `survivor_ticket = None`. `survivor_ticket=0` would assert that
the book was checked and nothing survived, which is the single thing this reply
does not know. So this outcome needs no new ICD field: the existing
absent-is-unmeasured rule already carries it.

The adapter maps that token to `RETCODE_UNKNOWN`, so `OrderResult.measured` is
False. That matters because `_MT4_RET` has no entry for 128 and the fallback
would otherwise make it `TRADE_RETCODE_REJECT`: a rejection is a VERDICT, and a
verdict closes the desk's in-flight entry and tells the operator the venue
refused an order that may be filling. The entry instead stays open and the send
is reported as unresolved, which is what `docs/RUNBOOK.md` has an operator
reconcile.

**The mapping is keyed on the TOKEN, never on the bare code 128.** An Expert
older than this contract reports a timeout as a plain `error=OrderSend` failure,
and by then it has already re-sent. The adapter cannot undo that and does not
pretend to; such a reply keeps its existing meaning, and the fix is to install
the current Expert.

**The Expert does not WAIT for the book to settle.** Waiting would spend the
desk's send budget, which is derived from the Expert's declared
`SE_LADDER_SLEEP_MS` and `SE_BROKER_CALLS` (see "Why a send gets its own budget"
and `tests/test_send_budget.py`). The book scan reads the terminal's own order
pool, so it costs no broker round trip and no `Sleep`, and the declared ladder is
unchanged. An unresolved send already has a durable home one layer up in
`<journal stem>.inflight.json`; resolving it is a human's job with the terminal
in front of them, not a job to attempt inside a timing budget.

### Startup reconciliation

`OnInit` scans the book and prints one line per position that is open with no
stop, then a summary count. Set the `ReconcileMagic` input to limit the scan to
one magic number; `0`, the default, reports every unstopped position.

The Expert REPORTS these and does not adopt them. A position that predates the
current session is not sized, not managed, and not stopped out by this bot. A
human closes or protects it.

## What a symbol spec can and cannot say on MT4

The adapter reads 15 fields. `SymbolReply` can supply 11. The other 4 are not
an oversight in the Expert: MQL4's `MarketInfo` has no identifier for them.

A field that was not measured is recorded in `SymbolSpec.unmeasured` and left at
a value that cannot be mistaken for usable. It is never replaced with a
plausible default, because a plausible default is indistinguishable from a
measurement and nothing downstream can then tell them apart.

| Field | On the wire | Notes |
| --- | --- | --- |
| `digits` | yes | `0` is a real reading for an instrument quoted in whole points. |
| `point` | yes | Zero is not a reading; `MarketInfo` answers 0 for a symbol not in Market Watch. |
| `tick_size` | yes | Same. |
| `tick_value` | yes | Same. See the note below; MT4 has ONE tick value. |
| `contract_size` | yes | Zero is not a reading. |
| `volume_min` / `volume_max` / `volume_step` | yes | 8 decimals, so a 0.001 lot step survives. |
| `stops_level` / `freeze_level` / `spread` | yes | `0` is a real reading and stays one. |
| `trade_mode` | NO | MQL4 has no trade-mode identifier. Recorded unmeasured, left at `0` (disabled), the fail-closed direction. |
| `currency_base` / `currency_profit` | NO | Derived by slicing the symbol name. A naming convention, not a measurement, and recorded as such. |
| `currency_margin` | NO | No MQL4 source at all. |

### MT4 has one tick value, by design

MQL4 exposes exactly one tick-value identifier, `MODE_TICKVALUE`. There is no
loss-leg variant. The MT5 remedy of preferring `trade_tick_value_loss` when the
primary field is unusable **does not transfer**, so do not re-propose it. On MT4
the only honest response to an unusable tick value is to REFUSE.

`1.0` was the old default and it is not conservative. The error is the ratio
`true / 1.0`:

- below 1.0 (a JPY cross, about 0.67): undersized, which is safe;
- above 1.0 (indices, metals, most CFDs; say 2.5): oversized by that ratio, so
  a 100 unit budget becomes a 250 unit loss.

The shipped example config lists `EURUSD`, `GBPUSD`, `USDJPY`, `AUDUSD`, so one
of the four defaults already has a non-unit tick value. This is not an exotic
edge case.

The last-line guard could not catch it: `risk.py` recomputes
`money_per_lot_at_stop` from the same spec, so a wrong number was compared
against a wrong number and passed. A guard that shares its subject's input is
not a guard.

### What refusal looks like

The risk gate refuses with `spec_not_measured:<field>,<field>` before anything
reads the spec, rather than letting sizing return zero and reporting
`size_zero`. Those are different facts: `size_zero` says the budget was too
small, and this says nothing was measured. If you see it, the symbol is
probably not in Market Watch; add it there and restart.

## Startup waits for the Expert. Steady state does not.

MT4 is a GUI application. A desk started from a boot-triggered Windows
scheduled task therefore races the terminal's own launch: MT4 has to start, load,
log in to the broker and reach the Expert's first timer tick before anything can
answer on the mailbox.

Measured on a live host after its first reboot since setup: MT4 came back
healthy, the Expert was attached, the mailbox path was right, and the desk was
dead. `Engine.start()` sent ONE `ping` on the 5 second per-command timeout, the
ping timed out, the process exited, and nothing retried. Every visible
indicator read healthy and the bot was gone. An operator with no shell on that
host has no way to notice it or fix it.

`Engine.start()` now calls `Mt4Broker.startup_connect()`, which retries the
ping with a growing gap until the Expert answers or the budget runs out.

| | Budget | Set by | Used by |
| --- | --- | --- | --- |
| Startup | `mt4.startup_wait_sec`, default 180s | `startup_connect()` | `Engine.start()`, once |
| Steady state, reads | `mt4.timeout_ms`, default 5000 | `FileBridge.timeout` | every op that cannot change the book |
| Steady state, sends | `mt4.send_timeout_ms`, default 7060 | `FileBridge.send_timeout` | `market`, `working`, `modify_*`, `cancel`, `close`, `close_by` |

**The two numbers are deliberately not one number.** `Engine.step_all()` calls
`ensure_connected()` on every step and `Engine._reconnect_broker()` calls
`connect()` on the trading path, and both of those keep the short budget. A
cold-boot-sized budget leaking into either of them would turn a transient blip
into a multi-minute stall while the desk holds live positions, which is worse
than the startup bug it would be fixing. `doctor --connect` also keeps the
short budget, because interactive diagnosis should fail fast.

**It is bounded, and the bound is stated.** Wall clock worst case is the budget
plus one `timeout_ms`: a new ping is only started while time remains, but a
ping already in flight is allowed to finish. An unbounded wait would convert a
wrong `files_dir`, a detached Expert or an absent MT4 into a process that hangs
forever looking busy, which is not an improvement on a crash.

**It says what it is doing.** Every attempt prints to stdout, flushed per line,
so a redirected desk log shows the wait as it happens rather than as one burst
afterwards:

```
mt4: waiting up to 180s for the Expert to answer on the mailbox
mt4: no reply yet, attempt 1 at 5.0s of 180s (mt4 bridge timeout after 5.0s
transport=file phase=mailbox op=ping request=withdrawn); retrying in 1.0s
mt4: no reply yet, attempt 2 at 11.0s of 180s (mt4 bridge timeout after 5.0s
transport=file phase=mailbox op=ping request=withdrawn); retrying in 2.0s
mt4: Expert answered on attempt 6 after 48.3s
```

### Why a send gets its own budget, and where 7060 comes from

A read that times out is retried by the next step. A send that times out is
AMBIGUOUS: the order may be filled, in flight, or never sent, and the desk cannot
tell. The two failures do not cost the same, so they do not share a number.

Measured steady-state round trip on the live rig (2026-09-26, a FileSystemWatcher
on the mailbox directory over 01:57:15Z to 02:21:48Z, `.req` renamed in to `.res`
renamed in): **p50 205ms over 2166 requests, and nothing above that median.**
`tests/live_measurements.py` is that measurement's one home and it is cited here,
never restated.

**An earlier revision of this paragraph claimed "p90 206ms, max 223ms, and zero
round trips over 1000ms", and `config.py` claimed a "22x margin" from it. Both
were wrong and both are corrected (#127):** the pairing that produced those
numbers shifts by one after every unanswered request and three of the 2166 went
unanswered, so the tail was never measured. It is stated here as well as nine
lines below because a number is restated by being convenient, and the restatement
is what a later reader trusts. What the read budget's 5000ms is actually known to
be is **24x the median**, with the tail unknown; see "Is 5000ms the right read
budget" below for what would settle it.

For a send it is not a margin at all, because the Expert can spend most of it
before it is able to reply. The send budget is therefore DERIVED, term by term,
and `constants.derive_send_timeout_ms()` is its only home:

| Term | ms | Kind |
| --- | --- | --- |
| Transport ceiling | 1000 | MEASURED, indirectly: about 5x the p50 of 205ms, which is the ONLY trustworthy statistic from that window (see below) |
| Expert ladder `Sleep` total | 950 | COMPUTED: (8 `OrderSend` + 5 `OrderModify` + 6 rollback) x 50ms |
| Claim-open retries | 2 x 180 = 360 | COMPUTED: (`ClaimOpenRetries` 10 - 1) x `ClaimOpenRetryMs` 20, for the claim read AND the reply write |
| Broker round trips | 19 x 250 = 4750 | ALLOWANCE, the only un-measured term |
| **Total** | **7060** | |

**Why the ceiling is not a tail statistic.** `tests/live_measurements.py` records
the p50 and deliberately records nothing above it: the naive pairing used to
compute latency shifts by one after every unanswered request, and three of the 2166
requests in that window went unanswered, so p90 and above are unreliable. A
ceiling has to come from somewhere defensible, so it is a multiple of the median,
in the conservative direction for a budget whose failure mode is expiring early.

### Is 5000ms the right read budget

**Unknown, and deliberately UNCHANGED (#127).** Whether 5s is right for a
file-mailbox round trip on a loaded Windows VPS is a measurement, and the only
statistic the rig has ever yielded is the median. 5000ms is 24x that median; a
budget is sized against the TAIL, and the tail is the part that was not measured.
Moving it on the strength of the median would be an opinion wearing a number.

Two things make it answerable rather than permanently open:

- **the journal can now distinguish the cases.** A `reconnect` row carries
  `cause_phase` and `cause_op`, so a run of `file`/`mailbox` timeouts on `ping`
  (a quiet mailbox) is separable from `net`/`read` (a slow link) and from a
  single op that is always the one to expire (a budget that is genuinely short).
  If the budget were the binding constraint, the timeouts would cluster on the
  most expensive op, and they do not have to be guessed at any more.
- **the pairing flaw is fixed in the instrument that found it.**
  `mt4/tools/measure-mailbox.ps1` used to pair each request with the next reply,
  so one unanswered request shifted every later sample. It now holds the open
  request and EXCLUDES one that got no reply. That is sound because the mailbox
  is a strict singleton by contract: one `.req` at a time, and the desk blocks on
  its reply, so a `.res` landing between two `.req` renames belongs to the first
  of them and to nothing else.

  **Pairing by the request `id` would have been the obvious fix and it is the
  wrong one.** Reading `mt4_risk_bot.req` means holding a read handle on the
  shared name, and on Windows that can make the Expert's claiming `FileMove`
  fail with a sharing violation: the instrument would manufacture the
  `ERR_CANNOT_OPEN_FILE` class of failure that #82 exists to fix. The script is
  read-only over event NAMES for that reason, and it says so in its own header.

  **The median was never trustworthy as a rule, only as an accident.** All three
  losses in the 2026-09 window fell in its last 18%, so about 82% of samples were
  never shifted. Replayed with the desk's real cadence and one loss moved to 20%
  in, the old pairing reads 418ms against a true 206ms.

Until that run exists, this repo records 205ms p50 and says the tail is unknown.
`docs/RUNBOOK.md`, "Measuring the mailbox round trip", is how to run it.

**There are TWO claim-retry ladders, not one, and that term moved late.** #82
retried the claim READ; #83 then gave the reply WRITE the same bounded retry, on
the same `tries` knob so the two cannot drift. Both sit between the desk's request
and the desk's reply, so both spend the send budget.
`tests/test_send_budget.py` counts the loops in the .mq4 and goes red if a third
appears.

The allowance is named as an allowance on purpose. No measurement of `OrderSend`
latency against the live OANDA account exists yet: the box is still on the
MetaQuotes demo and the gold market is shut, so the first market-hours window is
what measures it. 250ms is chosen against the one thing that IS measured about
this instrument, gold's 45 point spread against the shipped 20 point deviation,
which makes a requote the expected case rather than the tail, and a requote is a
full round trip.

`cfg.validate()` refuses a send budget at or below the read budget, and one inside
the Expert's `Sleep` total. The watchdog still derives its alarm from
`timeout_ms`, the READ budget, because the two commands `step_all` spends before
it can write a heartbeat are both reads; that reasoning is in
`watchdog.venue_timeout_seconds`.

### A request the desk gave up on must not fire later

`FileBridge` used to raise with no cleanup, leaving the `.req` file on the shared
name addressed to an Expert that had not claimed it. The Expert polls every 100ms
and executes whatever it finds, so a trade the operator was told had FAILED could
fire minutes afterwards; across a desk process exit nothing bounded "later" at all,
and a silent restart with no traceback was observed on the live box at
2026-09-26T01:56:49Z.

Two layers, and only the second is a guarantee:

1. the desk WITHDRAWS the request before raising. `BridgeTimeout` then states which
   outcome it got, because "I gave up" and "nothing can happen now" are different
   facts:

   | `withdrawal` | Meaning |
   | --- | --- |
   | `withdrawn` | the request was still on the shared name and is now gone. It cannot fire. |
   | `claimed` | the shared name was already gone, so the Expert HAS it and may be executing it now. This is the ambiguous-money case. |
   | `locked` | still there and could not be removed. It can still fire, and `ttl_ms` is the only guard left. |

   **This survives the shim hop.** With `mt4.mailbox_url` set, the withdrawal is
   measured on the shim's host and travels back in the `504` refusal body as a
   `withdrawal=` line, so an off-box desk reads the same three words a
   co-located one does (#135). A body with no `withdrawal=` line, or one
   carrying a word outside the three, reads as `claimed`: that is an old shim or
   a confused one, and neither is a measurement. The key is read on a `504`
   only, because a `503` means nothing was ever written.

2. the Expert refuses a request older than its `ttl_ms`. This is the layer that
   survives the desk not being there any more, and it is the only one that does.

The Expert does not guess how to read a file timestamp. MQL4 does not state
whether `FILE_MODIFY_DATE` comes back in local time or UTC, and the two wrong
answers fail in opposite directions: one makes every request look ancient and
stops the desk working, the other makes an old request look fresh and silently
removes the fence. So `OnInit` writes its own probe file, reads the stamp back,
and keeps the offset. A calibration that fails reports it, and then an
unmeasurable age refuses a send.

### A timeout says WHICH transport and WHERE on it

Giving up used to report one string. `mt4 bridge timeout` was produced by the
file mailbox, by a desk whose HTTP call to the shim never answered, and by a shim
that reached the mailbox and got nothing back; and the `503` and `504` arms were
one message apart from the status code. Those are different faults with different
operator actions, and on the live desk twelve days of journal could not separate
them (#127).

`BridgeTimeout` therefore carries `transport` and `phase` as ATTRIBUTES, and
`Engine._reconnect_broker` writes them into `journal.jsonl`. The attributes are
primary and the message text is secondary on purpose: an operator reads the
message, a script reads the row, and nothing should have to partition these facts
by parsing English.

| `transport` | `phase` | What happened | Where an operator looks |
| --- | --- | --- | --- |
| `file` | `mailbox` | no `.res` with this request's id appeared inside the budget | the terminal: is MT4 up, is the Expert attached to exactly one chart, is `files_dir` the Common Files folder, and is the Experts log showing a claim it could not read |
| `net` | `connect` | the HTTP call got no answer at all | the shim process and the path to it: `mt4-shim` not running, the tunnel down, the host gone |
| `net` | `read` | the response began and did not finish | the shim is alive and slow, or the link is losing the reply. The request may be executing |
| `net` | `shim-mailbox` | the shim answered `504`: it reached the mailbox and the Expert did not reply | the terminal, exactly as for `file`/`mailbox`. The shim is fine |
| `net` | `shim-unavailable` | the shim answered `503`: it could not use the mailbox directory | `files_dir` on the MT4 host, and whether MT4 has created Common Files yet |

`straightedge.broker.mt4_live` declares the vocabulary (`TRANSPORT_FILE`,
`TRANSPORT_NET`, `PHASE_MAILBOX`, `PHASE_CONNECT`, `PHASE_READ`,
`PHASE_SHIM_MAILBOX`, `PHASE_SHIM_UNAVAILABLE`, and `BRIDGE_TIMEOUT_PHASES`
listing all five). This table cites those names; it does not restate them.
`tests/test_reconnect_cause_is_recorded.py` drives all five, asserts every
`(transport, phase)` pair is distinct, and compares the set it can produce
against `BRIDGE_TIMEOUT_PHASES`, so a sixth phase added without a case goes red.

`file`/`mailbox` is the only pair the file transport can report, because the
bridge's other give-up points (an unlinkable `.res`, an unwritable `.req`) raise
`OSError` rather than `BridgeTimeout`. The field is written anyway, so a reader
never has to know which transports have one phase and which have four.

**What reaches the journal.** `reconnect` rows carry the TRIGGER under a `cause`
prefix: `cause`, `cause_type`, and `cause_op`, `cause_transport`, `cause_phase`,
`cause_withdrawal`, `cause_req_id` when the exception carries them. `error` on
the same row still means the reconnect ATTEMPT itself failed, which is a
different fault, and the two are never merged. `docs/RUNBOOK.md` has the
operator's reading of those rows.

### A staged order is transmitted at most once

`BridgeTimeout` subclasses `RuntimeError` and `Desk.handle` catches
`RuntimeError`, so a `/confirm` whose bridge call timed out returned the bare
string `mt4 bridge timeout` to the operator with the order still staged. A second
`/confirm` sent it again. Measured before the fix: two identical 0.55 lot market
orders, the second reporting success.

A TIMEOUT IS NOT EVIDENCE THE ORDER DID NOT REACH THE BROKER, and AN EMPTY BOOK IS
NOT EVIDENCE EITHER: the desk's budget expires while the Expert may still be
inside `SendRetry`, so the position the send is about to create is not on the book
when the desk looks.

So every staged order carries a client order id, a record of the attempt is written
to `<journal stem>.inflight.json` BEFORE the send and cleared only by a VERDICT
(success or a venue rejection), and a send whose key already has an open record is
REFUSED. The ledger is durable (flush, fsync, atomic replace) because the observed
failure was a process that died; the key rides the `confirm_stage` journal record,
so it survives the restart too.

Refusing costs an order. Sending twice costs money and cannot be undone. See
`docs/RUNBOOK.md` for how an operator reconciles one.

Giving up names the elapsed time and what to check:

```
mt4 bridge never answered: 21 ping(s) over 180.4s, budget 180s, last error:
mt4 bridge timeout after 5.0s transport=file phase=mailbox op=ping
request=withdrawn. Check that MetaTrader 4 is running, that
mt4/Experts/Mt4RiskBot.mq4 is attached to exactly one chart with AutoTrading
enabled, and that mt4.files_dir is the Terminal Common Files folder.
```

**A reply of `ok=0` is NOT retried.** That is a live Expert stating a
diagnosis, and waiting cannot change it. The partition is a type, not a
message: `FileBridge` raises `BridgeTimeout` (a `RuntimeError` subclass) when
nothing answered, and only that and `OSError` are retried.

**What this does not cover.** An in-process wait fixes the cold-boot race. It
does not cover MT4 taking longer than the budget, or MT4 dying later, because a
process that has exited cannot retry anything. The scheduled task should also
restart the desk on failure; the wait reduces how often that is needed, it does
not replace it.

**That scheduled task is now written down, and so is the half a restart cannot
fix.** `docs/RUNBOOK.md`, "Unattended (Windows scheduled task)", creates the
task and creates a second one beside it for `straightedge watch`, which reads
`journal.heartbeat` and is the first thing in this repo that ever did. The
reason the two tasks are not one is the reason this paragraph exists: a restart
brings the PROCESS back, and it brings it back DISARMED, because live arming is
per process and fc34 forbids putting `--i-accept-risk` anywhere a supervisor can
re-run it. So the failure that costs an unattended week is not the crash, which
heals; it is the desk sitting there ticking and refusing to trade with nobody
told. `watch` reports that as its own state, `ALIVE NOT TRADING
(live_not_accepted)`, with its own exit code, and a human re-arms from the chat.

## Timeframes

The wire uses names: `M1`, `M5`, `M15`, `M30`, `H1`, `H4`, `D1`, `W1`, `MN1`.
The Expert maps those onto `PERIOD_*`.
Do not send MT5 timeframe integers (`16385` is not `PERIOD_H1`).

## Wire contract

`tests/test_mt4_wire.py` holds a golden transcript for every op, and
`tests/mt4_transcripts.py` derives each one from the Expert's own emitters with
line citations. Those two files are the executable copy of this document. Change
the Expert's reply format and they go red.

Two checks in there are worth knowing about before editing either side:

- The pipe-join order of the position, order and bar rows is asserted against
  the adapter's field tuples. Reorder one field on either side and the suite
  fails by name.
- A field the Expert does not send is recorded as absent, not as zero, so the
  tests can tell a measured value from a defaulted one.

`broker/mt4_live.py` carries a per-file coverage floor declared in
`pyproject.toml` under `[tool.straightedge.coverage_floors]`.

## Attach

1. Install the `.ex4` that CI built for the commit you are deploying, from the
   `Mt4RiskBot-ex4-<sha>` artifact on that commit's `mt4-compile` run
   (`.github/workflows/mt4-compile.yml`, straightedge#86). CI DOES compile now: it
   asserts the artifact is fresh and non-empty and that the log reads
   `0 errors, 0 warnings`, and it fails on a source that cannot compile. If you
   compile by hand instead, the Toolbox Errors tab must read
   `0 error(s), 0 warning(s)` and that line goes in the handover checklist, but then
   the binary on the box is not tied to a commit.
   **Compiling is not loading:** replacing the `.ex4` on disk does not reload it, so
   record which binary is live from MT4's own inputs line after a terminal restart.
2. Attach it to one chart. A second attach refuses to initialise and prints
   `REFUSING TO START` in the Experts log; see "One Expert, enforced".
3. Enable AutoTrading. Allow live trading on the Expert.
4. Set `account.mode = "mt4"` and `mt4.files_dir`.
5. `doctor --connect` must print `venue=mt4` and exit 0.

For a desk that is NOT on this host, steps 1 to 3 are unchanged, and step 4
becomes: set `MT4_MAILBOX_TOKEN` on both machines, run
`straightedge mt4-shim` here, and set `mt4.mailbox_url` on the desk. See
`docs/TRANSPORT.md`.

Real money (`trade_mode=2`) still needs `--i-accept-risk` or `/live on I-ACCEPT-RISK`.
Paper P/L is not live P/L.
Nothing here guarantees profit.
