# Runbook

The bot is the Python process on this computer.
The desk is Telegram chat commands.
The agent is the Cloudflare Computer worker.
The gateway is Cloudflare AI Gateway `mt5-risk-bot`.
The circuit is halt, daily-loss, and drawdown gates.

WARNING
Paper is the default.
Nothing here guarantees profit.
Advice is not financial advice.

NOTE
Put `--config` before the subcommand.
Example: `python -m straightedge --config config.toml run --mode paper --loop`.

## Paper first

1. Run doctor.
   `python -m straightedge doctor`
2. Stop if doctor is not 0.
3. Run a trend backtest.
   `python -m straightedge backtest --market trend --no-session-filter`
4. Run a range backtest.
   `python -m straightedge backtest --market range --no-session-filter`

`doctor` is the gate.
It pings Telegram if the token is set.
Then it runs paper `/buy` `/confirm` `/close`.
No live terminal is used.
`doctor` is non-zero if the ping fails or the paper round-trip fails.
Do not start a long run until doctor exits 0.
Do not go live until doctor exits 0.

Plain `doctor` does not measure per-symbol history.
It prints `history: NOT MEASURED`.
That needs a live terminal.
Use `doctor --connect` for it.

## Per-symbol history

`doctor --connect` prints one line for each symbol in `[symbols] names`.
Each line gives the bars available and the ATR.

```
history: H1, need 60 bars, 1 of 2 usable
  EURUSD: 0 bars (need 60), ATR unavailable -- the terminal served nothing ...
  XAUUSD: 200 bars (need 60), ATR=17.218800
```

`doctor --connect` is non-zero if any symbol is not usable.
A symbol in `[symbols] names` is a symbol the desk will try to trade.
A symbol you do not trade must not be in that list.
Advice-only names go in `[advice] symbols`.

The desk fetches the history itself.
You do not open any charts.
It asks the terminal ten times, one second apart.
This happens at startup and on `/symbols add`.

A symbol can still fail after that.
Then `run` prints the symbol name and stops.
It does not start.
Check two things.
First, the symbol name must match the broker spelling.
Many brokers use a suffix, for example `EURUSD.m`.
Second, the broker must serve your timeframe for that symbol.

`ERR_HISTORY_WILL_UPDATED` in the message means the download is still running.
Run `doctor --connect` again.
`NO HISTORY DATA` means the terminal has none and is not fetching it.
That is a symbol name or a broker problem.

On the seeded trend generator, equity must finish above start.
On the seeded range generator, the account must not be ruined.
If either check fails on your machine, do not go live.

CSV backtest (unix timestamps in `time`):

```bash
python -m straightedge backtest --csv path/to/ohlc.csv --symbol EURUSD --no-session-filter
```

## Seed paper from a live terminal (no orders)

This needs a running terminal and a binding (`MetaTrader5` or `mt5-mac`).

```bash
python -m straightedge --config config.toml run --mode paper --feed-mt5
```

Orders stay in the in-process broker.

## Docker paper (fleet)

Paper only. No MetaTrader in the image. Do not run this and a laptop
`--loop` on the same bot token. Two loops fight `getUpdates`.

Host: a fleet box that is not dischord. Example: jello.

1. Copy the repo to the host.
2. Copy `.env` (0600) to the repo root on the host. Do not put secrets in the image.
3. Run `docker compose build`.
4. Run `docker compose up -d`.
5. Check `docker compose logs -f desk`.
6. Send `/help` in Telegram.

Data is `./data` (journal, lock, heartbeat). Stop: `docker compose down`.
Host data dir owner must be uid 10001.

Live MT5 is not this image. See `deploy/LIVE.md`.

## Production long-run (macOS)

Telegram is required.
`run` exits 2 without both `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
Only `TELEGRAM_CHAT_ID` is accepted.
Updates from any other chat are ignored.

### Paper loop

No terminal is required.

1. Run doctor.
   `python -m straightedge doctor`
2. Stop if doctor is not 0.
3. Start the paper loop.
   `python -m straightedge --config config.toml run --mode paper --loop`

4. Keep the bot in a terminal, tmux, or the LaunchAgent in `docs/launchd.plist.example`.
5. Stop it with Ctrl-C or `launchctl bootout`.

### MT5 loop

MetaTrader 5.app must already be running and logged in.
`doctor --connect` is the gate (binding, login, `trade_mode`).
It is non-zero if the binding is missing or login fails.
Do not start live on a traceback.
Demo is `trade_mode=0` and does not need `--i-accept-risk`.

WARNING
Real money (`trade_mode=2`) is refused without `--i-accept-risk` at start
or `/live on I-ACCEPT-RISK` in the locked chat.

1. Run doctor with a login check.
   `python -m straightedge --config config.toml doctor --connect`
2. Stop if doctor is not 0.
3. Start the live loop for demo.
   `python -m straightedge --config config.toml run --mode mt5 --loop`
4. For a real account, arm from chat after start. Do not put
   `--i-accept-risk` on a `--loop` command line (fc34): it re-arms real
   money on every crash restart and undoes the per-process live expiry
   1.1.3 added on purpose.
   `/live on I-ACCEPT-RISK`

### MT4 loop

MetaTrader 4 has no official Python package.
Copy `mt4/Experts/Mt4RiskBot.mq4` into `MQL4/Experts`.
Compile it. Attach it to one chart. Enable AutoTrading.
Set `mt4.files_dir` (or `MT4_FILES_DIR`) to Common Files.
On Windows, omit it. Default:

```
%APPDATA%\MetaQuotes\Terminal\Common\Files
```

The Python bot runs on that same Windows host. `fcntl` is not used there.
To run the desk elsewhere instead, run `straightedge mt4-shim` on this host
(loopback plus a Cloudflare Tunnel) and set `mt4.mailbox_url` plus
`MT4_MAILBOX_TOKEN` on the desk. `docs/TRANSPORT.md` is the decision record and
carries the failure table.

`doctor --connect` is the gate (mailbox ping, `account`, `trade_mode`).
It is non-zero if the Expert is missing, the folder is wrong, or the ping times out.
It is also non-zero when the Expert's own numbers say the desk's send budget is
too short (`send fence: TOO SHORT`).

MT4 has two timeouts, not one.
`mt4.timeout_ms` (default 5000) is the READ budget.
It covers ping, account, symbol, tick, select, rates, positions, orders, and both
`check_*` dry runs.
`mt4.send_timeout_ms` (default 7060) is the SEND budget.
It covers `market`, `working`, `modify_position`, `modify_working`, `cancel`,
`close`, and `close_by`.
`doctor` prints both (`budgets: 5000ms read / 7060ms send`).
The send default is derived, not chosen. See `Unresolved sends` below.
Do not start live on a traceback.
Demo is `trade_mode=0` and does not need `--i-accept-risk`.

WARNING
Real money (`trade_mode=2`) is refused without `--i-accept-risk` at start
or `/live on I-ACCEPT-RISK` in the locked chat.

1. Set `account.mode = "mt4"` in config, or `export ACCOUNT_MODE=mt4`.
2. Run doctor with a login check.
   `python -m straightedge --config config.toml doctor --connect`
3. Stop if doctor is not 0.
4. Start the live loop for demo.
   `python -m straightedge --config config.toml run --mode mt4 --loop`
5. For a real account, arm from chat after start. Do not put
   `--i-accept-risk` on a `--loop` command line (fc34): it re-arms real
   money on every crash restart and undoes the per-process live expiry
   1.1.3 added on purpose.
   `/live on I-ACCEPT-RISK`

See `docs/MT4.md` and `mt4/README.md`.

`--loop` polls until Ctrl-C.
`engine.poll_seconds` is the Telegram `getUpdates` timeout.
The example config sets `poll_seconds = 1`.
Each loop tick waits up to that many seconds for a chat update.
Then `step_all` runs fills, SL/TP, trail, and auto.
If the key is omitted, load uses 15.
Do not add a second sleep. The long poll is the wait.
It retries Telegram 429/5xx with backoff.
It resumes `getUpdates` from `journal.tg_offset` (next to `journal_path`).
A restart does not replay or drop commands.
A dropped terminal calls `initialize` again.
One bad tick is journaled (`reconnect` or `loop_error`).
The bot stays up.
`/halt` flattens positions and working orders.
A `HALT` file does the same.
The bot stays halted.
The bot does not exit.
`/resume` works without a restart.
A flatten that does not finish says so.
The reply and the alert both start with `FLATTEN INCOMPLETE`.
They give the number of positions still open.
The bot stays halted either way.

CAUTION
Two `run --loop` on the same journal cannot run together.
The second exits 2 with `already running` on stderr (`journal.lock`).
Stop the first bot, or set a different `engine.journal_path`.
Do not load the LaunchAgent and also run `--loop` in a terminal.

Each successful tick writes `journal.heartbeat` next to the journal.
The tick must reach the account.
Line 1 of the file is an ISO timestamp.
After it come `key=value` lines.
`blocked=` is empty when the desk would trade.
`blocked=` names the gate when it would not.
The file is chmod 0600.
A reconnect that fails does not update it.
Read the file with `watch` below.
Before a write that would exceed 10 MiB, the live journal is renamed to `journal.jsonl.1`.
That replaces any previous `.1`.

## Watchdog

`watch` reads `journal.heartbeat` and tells you the state of the desk.
Run it in a second process.
The desk cannot report its own death.

```bash
python -m straightedge --config config.toml watch
```

One check. It prints the state and exits.
The exit code is the state:

| Exit | State | Meaning |
| --- | --- | --- |
| 0 | `ALIVE ARMED` | The desk is ticking. It will trade. |
| 3 | `ALIVE NOT TRADING` | The desk is ticking. A gate refuses. The gate is named. |
| 4 | `STALE` | No completed tick inside the threshold. |
| 5 | `UNKNOWN` | Nothing was measured. The message says what is missing. |

Exit 3 is the one an up-or-down check cannot see.
The desk is up, and it is not trading.
The usual reason after a restart is `live_not_accepted`.
That means the desk came back DISARMED.
Arming is per process. It never survives a restart.
Send `/live on I-ACCEPT-RISK` in the locked chat to arm it again.
The other reasons are `halt_file`, `daily_loss`, `max_drawdown`,
`trade_not_allowed`, `state_unreadable`, and `state_unwritable`.
Read `Halt` above for those.

Alert mode:

```bash
python -m straightedge --config config.toml watch --loop --ok-every 3600
```

It alerts the locked chat on the first check.
Then it alerts on every change of state.
It never polls Telegram for commands.
It cannot steal the desk's commands.
It never takes `journal.lock`.
It cannot stop the desk from restarting.

`--ok-every 3600` sends one healthy message an hour.
Zero is off. Zero is the default.
Set it. Then silence is a signal too.
Nothing on the computer can see this watcher die.

It also alerts on a RESTART, which is not a change of state.
The heartbeat carries `run_id`, one value per desk process.
When that value changes, the desk is a new process, and the alert says so and
counts how many it has seen.
Without it a restart was visible only through the arming state falling back to
`live_not_accepted`, and that exists only on a real-money desk: on a demo
account every field read the same either side of a crash.
A desk too old to publish `run_id` is reported as such, never as unchanged.

### The threshold is measured, not chosen

`doctor` prints the threshold on every run.

```
watchdog: /path/journal.heartbeat, stale after 428s (tick budget 214s)
```

The threshold comes from your own config.
`engine.poll_seconds` is the Telegram long poll.
Telegram retries that poll up to 4 times.
Telegram can ask for a 60 second wait on each retry.
`[mt4] timeout_ms` (or `[mt5]`) is one venue READ command.
On MT4 that is the read budget of two, and it is the one the watchdog uses.
`[mt4] send_timeout_ms` is the other, and the watchdog does NOT use it.
The tick spends two venue commands before it writes the file.
Both of them are reads (`ensure_connected` and `account`).
The budget is the sum. The threshold is twice the budget.
A send lives in the part of a tick that no config value bounds, and the doubling
is what already covers that tail.
Deriving the alarm from the send budget instead would widen a threshold the
operator was promised, for about 1.9s against a 214s MT4 budget.
A remote MT4 shim (`mt4.mailbox_url`) adds 2 seconds per command.

Do not replace this with a number you like.
A 60 second alarm on a 15 second poll is a false alarm every flood wait.
A false alarm gets muted, and a muted alarm is worse than none.
The Telegram retry ceiling is most of the 428s.
That is why a smaller `poll_seconds` does not make the alarm much faster.

The desk also publishes `tick_gap_max_s`.
That is the longest real gap between two heartbeats.
`over_budget=1` means a real gap passed the budget.
Then the threshold is too tight for this book.
The desk does NOT widen it by itself.
A gate that widens itself until it stops firing is not a gate.
Report `over_budget=1`. Do not ignore it.

`tick_gap_max_s` and `over_budget` count THIS PROCESS only.
A restart sets them back to zero.
That is correct. After a restart this process really is clean.
`tick_gap_ever_s` and `over_budget_ever` count THIS BOX.
A restart does NOT set them back.
Ask `tick_gap_max_s` if the desk is slow NOW.
Ask `tick_gap_ever_s` if this box has EVER been slow.
`over_budget_ever=1` with `over_budget=0` means a breach before the restart.
Report that too.
The book did not get smaller because the desk restarted.
Do not delete `journal.heartbeat`.
That file carries the box history. Deleting it resets `tick_gap_ever_s`.
An older desk writes no `over_budget_ever`.
Then `watch` says it cannot report the box history.
It does NOT read the missing field as clean.
Each breach also writes one `tick_gap_breach` record in the journal.
Use the journal to COUNT breaches.
The heartbeat holds only the worst one.

### What `watch` cannot tell you

`STALE` does not prove the process is gone.
A desk that cannot reach the account writes nothing.
A desk that exited writes nothing.
They look the same from the file.
To tell them apart, grep `reconnect` in `journal.jsonl`.
Proving it needs `journal.lock`, and this command will not touch it.
Holding that lock for one moment can make a restart exit `already running`.

`watch` alerts through Telegram.
If Telegram is down, the alert reaches stdout only.
The line says so.
Nothing on the computer can page you then.

## Unattended (Windows scheduled task)

Use this for a run of days with nobody at the computer.
You need two tasks. One starts the desk. One watches it.

**The definitions live in `deploy/windows/`, not in this paragraph.**
That is the whole lesson of this section. The procedure used to be prose here,
it was typed once, and nothing ever compared the result to it again. Measured
on the live box 2026-10-08: the MT4 terminal was supervised every two minutes,
the desk had a logon trigger only and had run once in twelve days, and the
watcher task did not exist at all. A runbook is not a control.

Read `deploy/windows/README.md` for why each setting is what it is. The three
that are easiest to get wrong and hardest to notice:
`MultipleInstancesPolicy` must be `IgnoreNew` (`StopExisting` makes the task
END the healthy desk every interval), `ExecutionTimeLimit` must be `PT0S`
(`schtasks /create` defaults it to `PT72H`, which kills a healthy desk three
days in), and `LogonType` must be `InteractiveToken` (MT4 is a GUI program).

WARNING
Never put `--i-accept-risk` in either task (fc34).
A scheduled task re-runs its arguments on every restart.
That would arm real money again on every crash, with nobody there.
The desk comes back DISARMED on purpose.
A human arms it from the chat.
`supervision` below fails a task that carries the flag.

### Install

```bat
cd deploy\windows
powershell -ExecutionPolicy Bypass -File .\Install-Supervision.ps1 ^
  -PythonExe "C:\Program Files\Python312\python.exe" ^
  -ConfigPath "C:\bot\config.toml" ^
  -WorkingDirectory "C:\bot"
```

That is a dry run; it changes nothing and says so. Add `-Apply` to register.
It records the previous definitions in `.\tasks-before` FIRST, because a
rollback is only a rollback if the previous state was captured before the
change and not reconstructed after it.

### Audit it, do not assume it

```bat
powershell -ExecutionPolicy Bypass -File .\Export-Tasks.ps1 -OutDir .\tasks
python -m straightedge --config C:\bot\config.toml supervision --tasks .\tasks
```

Read-only. It does not register, start, stop or edit a task, does not touch the
desk or `journal.lock`, and does not send to Telegram, so it is safe to run
mid-session. An audit an operator is afraid to run during trading hours is one
that only ever runs after the outage.

Exit 0 means every declared task is supervision. Non-zero names each failure
and says what it costs. `WARN` lines are hardening and do not change the exit
code.

Pass the DESK's own config. The restart interval has to be at or under the
desk's staleness threshold, `doctor` prints that threshold on every run, and
`supervision` derives the same number from the config it is given; auditing
with a different config measures a different desk. With no Telegram configured
the derived figure is missing its long-poll term, so the audit reports the
interval instead of judging it against a number no running desk can produce.

### Why the repeating trigger is safe while the desk is up

Task Scheduler does not start a second instance of a running task
(`MultipleInstancesPolicy` `IgnoreNew`), so the trigger does nothing while the
desk is alive. When the desk is gone, the next trigger starts it.
`journal.lock` is the second barrier: a second desk exits 2 with
`already running` before it touches MT4 or Telegram.

The first barrier only holds if the process the task launches STAYS ALIVE for
the desk's lifetime. A wrapper script that launches python and returns
immediately makes Task Scheduler mark the task complete, and then only
`journal.lock` is protecting you. The declared definitions launch python
directly for this reason, and because it keeps the command line where the audit
can read it.

MEASURED ON THE LIVE BOX 2026-10-08, so read the paragraph above as a
requirement and not as a description of what is there.
`C:\bot-state\run-desk-hidden.vbs` is one line:

```
CreateObject("Wscript.Shell").Run "cmd /c ""C:\bot-state\run-desk.cmd""", 0, False
```

The wait flag is `False`. It does NOT wait. So on that box wscript returns at
once, Task Scheduler marks the task complete, and `MultipleInstancesPolicy`
protects nothing: `journal.lock` is the only barrier against a second desk.

It is harmless today only because that task's repetition has never fired. The
repetition hangs on a LogonTrigger with `StopAtDurationEnd=true` and no
`Duration`, so the window shuts at logon: `NextRunTime` is empty and
`LastRunTime` is twelve days old.

REPAIR THE TRIGGER WITHOUT REPLACING THE LAUNCHER and you get a fresh python
every five minutes with only the lock in the way. Fix both or neither.

CAUTION
MT4 is a GUI program.
It needs a logged-in Windows session.
`InteractiveToken` runs the task in that session.
A task set to run whether the user is logged on or not cannot see MT4.

### What you will see after a crash

The desk restarts inside the interval.
The heartbeat starts moving again.
You may never get a `STALE` alert. That is correct.
You WILL get a `RESTARTED` line, with a count: the heartbeat carries a `run_id`
per process, so `watch --loop` reports a new process whatever the state. That
matters most on a demo account, where nothing needs arming and every other
field reads the same either side of the crash.
If the restarts are close together you also get `CRASH LOOP`, because a
supervisor that quietly papers over repeated crashes converts a loud failure
into a slow one.
On a real account you ALSO get `ALIVE NOT TRADING (live_not_accepted)`.
That is the desk telling you it came back disarmed.
Send `/live on I-ACCEPT-RISK` to arm it.
Until you do, the desk sizes and refuses. It does not trade.

### Prove it works before you leave it

Do this once, on the demo account.

1. Install both tasks. Confirm `supervision` exits 0 against the live export.
2. Wait for the first `watch` message in the chat.
3. End the desk process in Task Manager.
4. Wait for the restart interval.
5. Read the chat. You get `RESTARTED` and the state.
6. On a real account, send `/live on I-ACCEPT-RISK`.
7. Read the chat. `ALIVE ARMED`.

A watchdog you have never seen fire is not a watchdog.

## Agent advice

The desk can send `/ask` to the agent.
The desk does not need to call xAI or Anthropic directly.
Working memory is the Durable Object SQLite workspace
(`/workspace/notes.md`, `log.md`, `snapshot.md`, `history.json`).
`history.json` is the last journal records from `/ask`.
The agent does not use Cloudflare D1.
Inference is Unified Billing on the gateway.
The bot still does not send trades.

Live agent: `https://mt5-risk-agent.skyphusion.workers.dev/ask`
The gateway: `mt5-risk-bot` on account `fabcb25d9c7eb087110ec474a03e50d2`
Model: `xai/grok-4.6` via REST

```
POST https://api.cloudflare.com/client/v4/accounts/{id}/ai/v1/chat/completions
Authorization: Bearer CF_AIG_TOKEN
cf-aig-gateway-id: mt5-risk-bot
```

WARNING
Do not put `CF_AIG_TOKEN` on `gateway.ai.cloudflare.com` as `Authorization`.
The gateway forwards it to xAI as a provider key.

Agent secrets (never git): `CF_AIG_TOKEN`, `ADVICE_TOKEN`.
Laptop: `agent/.dev.vars` (0600, gitignored).

1. Load the laptop token file.
   `set -a`
2. Source it.
   `source agent/.dev.vars`
3. Stop exporting.
   `set +a`
4. Point the bot at the agent.
   `export AI_PROVIDER=computer`
   `export ADVICE_URL=https://mt5-risk-agent.skyphusion.workers.dev/ask`
5. Run doctor.
   `python -m straightedge doctor`

`/model computer` at runtime.
Session is the Telegram chat id (one workspace per chat).
The agent needs `session` in the body. It must be a JSON string.
Use letters, digits, dot, underscore, and hyphen. The length is 1 to 64.
A missing or bad session gets `400 {"error":"invalid session"}`.
The agent builds no workspace for a session it refuses.
There is no automatic fallback. Send `default` to share one desk on purpose.
`ADVICE_SESSIONS` is optional. Set it to a comma list to serve only those keys.
The bot sends the chat id, so it needs no change.
Redeploy: `cd agent && npx wrangler deploy` (needs `CLOUDFLARE_API_TOKEN`).
The agent depends on `@cloudflare/computer`, which Cloudflare ships as an early preview with unstable APIs.
The Production/Stable classifier in `pyproject.toml` covers the bot, not the agent.

## Demo

1. Open a broker demo account.
2. Enable AutoTrading.
3. Export broker secrets. Never commit these.
   `export MT5_LOGIN=...`
   `export MT5_PASSWORD=...`
   `export MT5_SERVER=...`
4. Set `account.mode = "mt5"` in `config.toml`.
5. Run doctor with a login check.
   `python -m straightedge --config config.toml doctor --connect`
6. Stop if doctor is not 0.
7. Start the live loop.
   `python -m straightedge --config config.toml run --mode mt5 --loop`
8. Confirm `trade_mode=0` in the doctor output.

Leave it running through at least one full session window.
Read `journal.jsonl`.

## Real money

WARNING
The bot refuses `trade_mode=2` without `--i-accept-risk` at start
or `/live on I-ACCEPT-RISK` in the locked chat.

1. Complete the Demo steps.
2. Confirm `doctor --connect` exits 0.
3. Set `risk_pct = 0.002` (0.2%) at first.
4. Start the live loop, then arm from chat.
   `python -m straightedge --config config.toml run --mode mt5 --loop`
   `/live on I-ACCEPT-RISK`
   The phrase is required.
   `/live on` without it is usage.
   `/live off` disarms.

WARNING
Never put `--i-accept-risk` on a `--loop` command line, in a supervisor,
a service wrapper, a batch file, or a scheduled task (fc34). It re-arms
real money on every crash restart, unattended, and undoes the
per-process live expiry 1.1.3 added on purpose
(`Desk.restore_from_journal` already refuses to re-arm from a `live_on`
journal record; a command-line flag baked into a supervised invocation
is the one place arming can still leak back in). `/live on
I-ACCEPT-RISK` from the locked chat is per-process and never survives a
restart -- use it instead.

5. Then `/approve always` if you want sends without `/confirm`.
   On `trade_mode=2`, arm live before `/approve always`.

## Halt

1. Create the halt file, or send `/halt` from the locked chat.
   `touch HALT`
2. Wait for the next loop iteration.
   The bot flattens positions and working orders for the bot's magic (20260909).
   The bot drops the staged confirm.
   The bot stops sending.
   The bot stays up.
3. Read the reply, or the Telegram alert.
   A clean sweep reads `flattened 2/2 positions, cancelled 1/1 orders; halted.`
   A bad sweep reads `FLATTEN INCOMPLETE: 1 still open (#123).`
   The alert also names the reason and the ticket.
   You cannot turn this alert off with `notify_events`.
4. If you see `FLATTEN INCOMPLETE`, open the terminal now.
   Close the named tickets by hand.
   The bot is halted and sends nothing new.
   But the named tickets still carry risk.
   `COULD NOT MEASURE` means the bot could not read the book.
   Then the count is the worst case, not a fact.
   Check every ticket the bot asked to close.
5. To resume an operator halt, remove the file and send `/resume`.
   `/resume` only clears the operator file.
6. Daily-loss halt self-clears at the next UTC midnight.
   Drawdown halt does not.
   Inspect and restart for drawdown.
   Daily-loss and max-drawdown cannot be cleared from Telegram.

The `HALT` path is relative to the bot working directory (LaunchAgent `WorkingDirectory`).
Restart if you are clearing drawdown.

`main()` sets umask 077 so files the bot creates are owner-only.

## Confirm

`/confirm` TTL is `telegram.confirm_seconds` (default 120).
Staging writes `confirm_stage` to the journal.
`start` restores that intent if the last of `confirm_stage` / `confirm_cancel` / `confirm_sent` is still `confirm_stage`.
The TTL must not have expired.
After expiry, `/confirm` replies `nothing to confirm`.
Restage with `/buy` `/sell` `/reverse` or advice.
`start` restores `/approve always` from the last of `approve_always` / `approve_off`.
`start` does NOT restore `live_accepted`. Arming is per process.
A `live_on` record in the journal makes `start` write `live_not_restored`.
`/live` then says arming was not restored. Re-arm with `/live on I-ACCEPT-RISK`.

MT5 positions and working orders stay in the terminal.
Paper positions and paper working orders die with the bot.

## Unresolved sends

A send that got no reply is not a send that did not happen.
The order may be filled, in flight, or never transmitted.
The desk cannot tell those apart, so it says so, and it never re-sends that order
by itself.

Every order the desk sends carries a client order id.
Eight hex characters, minted once, when the order is STAGED.
It rides the `confirm_stage` journal record, so it survives the desk process
dying between two `/confirm`s.
A second `/confirm` on the same staged order carries the SAME id.
The auto leg mints a fresh id per signal, because each bar is a new order and not
a retry of an old one.
A close carries no id: the ticket is already the key, and closing #123 twice
fails the second time.

`journal.inflight.json` sits next to `journal.jsonl` and is chmod 0600.
It holds one entry per send that left with no verdict, keyed by client order id.
The entry is written BEFORE the send.
It is removed only by a verdict from the venue, `ok` or a rejection.
An exception leaves it open.
An open entry is the desk saying it does not know whether that send moved money.
It is not a claim that the order exists.

### What you see in chat

A `/confirm` whose send came back with nothing:

```
send unresolved: no verdict came back for 4f9c1ab2. It may already be on the book. Check /positions and the terminal; this order will NOT be sent again.
```

A second `/confirm` on that same staged order:

```
refused: unresolved send 4f9c1ab2. Nothing was transmitted this time. An earlier attempt for this order left no verdict, so it may already be on the book: check /positions and the terminal, then /cancel and re-stage if nothing moved.
```

`refused:` means nothing went on the wire this time.
`send failed` means the venue answered and rejected it.
Those are different sentences on purpose.

`/reverse` says the same two things about the replacement, after the close has
already happened:

```
closed #123; send unresolved: no verdict came back for 4f9c1ab2. The replacement may be on the book. Check /positions and the terminal; it will NOT be sent again.
closed #123; refused: unresolved send 4f9c1ab2. Nothing was transmitted this time; the replacement may already be on the book. Check /positions and the terminal.
```

### What to do, in order

1. Write down the client order id from the reply. Eight hex characters.
2. Read `journal.inflight.json`. The entry names `symbol`, `side`, `volume`,
   `op` (`market` or `working`), `sl` and `tp` or `price`, `at`, `last_at`, and
   `attempts`.
3. Look for that id in the terminal. Three records carry it, and they are the
   join.
   The order comment on the book is `<your comment> <client id>`, clipped to
   MT4's 31 characters.
   The Expert prints `mt4riskbot send op=market client_id=<id> symbol=<sym>
   lots=<n> ticket=<n> err=<n>` on every market send.
   The journal carries `client_id` on `open`, `pending`, `send_unresolved`,
   `send_refused_unresolved`, and `confirm_unresolved`.
4. A key you FIND is proof. That position or order IS this send.
5. A key you do NOT find proves nothing.
   Brokers append to and overwrite `OrderComment`; this repo already ships a test
   for one rewriting a comment to `rb-1/from #123`.
   Absence is inconclusive. It is never a licence to re-send.
6. Read the account history too, not only the open book.
   An order can have opened and closed while the desk was blind.
7. If something moved, manage it in the terminal.
   A send that never returned never had its stop confirmed either.
   The desk journals a position it can match as `unmanaged_position` with
   `comment=unresolved send <client id>`.
   Set the stop by hand, or with `/sl TICKET PRICE` and `/tp TICKET PRICE`.
8. If nothing moved, `/cancel` to drop the staged confirm, then re-stage with
   `/buy` or `/sell`.
   Do not re-confirm. The re-stage mints a NEW id, so it is not refused.
9. The desk will not re-send the refused order, ever.
   There is no command that clears an entry, on purpose: only a human with the
   terminal in front of them can establish what happened.

You do not need to touch `journal.inflight.json` to trade again.
It only drives the startup report.
To stop that report after you have reconciled, stop the desk, remove that key
from the `open` object in the file, and leave the file at 0600.

### The report repeats on every start

`start` announces every still-open entry, on EVERY start, not once.
It does that BEFORE it connects to the venue.
A desk that cannot reach the terminal is exactly when you most need to know that
an earlier order's outcome was never established.
A standing money question that stops being announced is one that gets forgotten.
The ledger keeps 64 open entries. Past that the OLDEST are dropped.

### The journal records

| Event | Written when | Money state |
| --- | --- | --- |
| `send_unresolved` | a send raised instead of answering, and again for every still-open entry at every start | AMBIGUOUS. `matched` lists tickets whose comment carried the id, `book_read_failed` is set when the book could not be read at all |
| `send_refused_unresolved` | the engine refused a send whose key already had an open entry | NOTHING was transmitted. `attempts` and `first_at` describe the earlier attempt |
| `confirm_unresolved` | the `/confirm` path caught the exception from its own send | AMBIGUOUS. Journal only; the chat got its one line and nothing else |
| `inflight_unreadable` | the ledger file exists and could not be parsed | UNKNOWN. A corrupt ledger reads as EMPTY, so this record is the report you did NOT get |

`send_unresolved` and `confirm_unresolved` carry `request=`, which is what the
bridge did with the request it gave up on.

| `request=` | What it means | What can still fire |
| --- | --- | --- |
| `withdrawn` | the request was still on the shared name and is now gone | Nothing. The Expert can never execute it. |
| `claimed` | the shared name was already gone, so the Expert HAS it | It may be executing right now. A claimed request is never replayed, so its reply is lost. |
| `locked` | it is still there and could not be removed | It can still fire. The Expert's own `ttl_ms` fence is the only thing left. |
| `unknown` | the bridge did not report | Treat it as `claimed`. Not looking is not the same as looking and finding nothing. |

### The two budgets, and where the send budget comes from

`mt4.timeout_ms` is the READ budget, default 5000ms, and it is unchanged from when
it covered sends too.
`mt4.send_timeout_ms` is the SEND budget, default 7060ms, and it is DERIVED.

| Term | ms | Kind |
| --- | --- | --- |
| transport ceiling | 1000 | MEASURED, indirectly: about 5x the p50 of 205ms over 2166 requests. Nothing above the median is quoted, because the pairing that produced it shifts by one after every unanswered request and three went unanswered, so p90 and above are unreliable |
| Expert `Sleep` total | 950 | COMPUTED from `Mt4RiskBot.mq4`: (8 send + 5 modify + 6 rollback attempts) x 50ms |
| claim-open retries | 360 | COMPUTED from `ClaimOpenRetries = 10` and `ClaimOpenRetryMs = 20`: 9 x 20ms, TWICE. The claim read and the reply write are both on that knob |
| broker round trips | 4750 | ALLOWANCE: 19 round trips at 250ms |
| total | 7060 | `constants.derive_send_timeout_ms()`, the only home for this arithmetic |

250ms per broker round trip is an ALLOWANCE, not a measurement, and it is the only
un-measured term.
No `OrderSend` latency against the live OANDA account exists yet.
The first live market-hours window is what measures it.
Gold's measured 45 point spread against the shipped 20 point deviation makes a
requote the expected case rather than the tail, and a requote is a full round
trip, which is what 250ms is chosen against.

`config.validate()` refuses a send budget at or below the read budget.
It also refuses one at or below 2310ms, which is the transport ceiling plus
everything the Expert can spend on `Sleep()` alone (1000 + 950 + 360).
The desk must outlast the Expert, or it writes off requests the Expert is still
executing, which is how a duplicate order becomes reachable.

`doctor --connect` reads the check off the LIVE Expert and prints it:

```
send fence: ok (budget 7060ms vs Expert worst case 2310ms, ladder 950ms, broker calls 19, stale-request refusal yes)
```

`TOO SHORT` makes `doctor --connect` exit 1.
`NOT MEASURED` means the attached Expert does not declare `ladder_ms` and is too
old to be checked.
The numbers come off the attached Expert and not off this repo's copy, because its
retry ladders are `input` parameters and an operator can change them in the
terminal's dialog on a box nobody is watching.
The desk logs a loud WARNING on every ping whose declaration the budget does not
clear.

### Measuring the mailbox round trip

`mt4/tools/measure-mailbox.ps1` is the instrument. It already exists. Do not
write another one.

It watches the mailbox directory from outside both processes.
It never opens, writes or renames a mailbox file.
That is deliberate. A read handle on `mt4_risk_bot.req` can make the Expert
`FileMove` fail with a sharing violation.
That is the same class of failure #82 fixed, caused by the instrument.
So the script reads event names only, never file contents.

Run it IN the session, never detached:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File mt4\tools\measure-mailbox.ps1 `
  -FilesDir "$env:APPDATA\MetaQuotes\Terminal\Common\Files" -Seconds 1500
```

25 minutes is about 2200 requests at the measured rate.
Run it during MARKET HOURS.
Run it again while the box is loaded.
A quiet closed-market window is the easy case, and the reconnects in the journal
are not all from quiet windows.

Report every line it prints, together.
`requests_published` and `unanswered_total` are the denominators.
A p99 from 40 samples with 300 unanswered is not a p99.
The 2026-09 window was 3 unanswered out of 2166, which is 0.139%.

What it still cannot see, and you say so whenever you quote it:
`FileSystemWatcher` drops events on buffer overflow, and the script does not
register the `Error` event. So a missing reply can be a missed notification.
Corroborate every gap against `journal.jsonl`.
A real loss appears there as a `loop_error` or `reconnect` about one adapter
budget later.
Two observers or it did not happen.

Then read the journal for the same window:

```powershell
Select-String -Path journal.jsonl -Pattern '"event":"(reconnect|account_read_failed|loop_error)"' |
  ForEach-Object { $_.Line } | Select-Object -Last 50
```

`cause_phase` separates a quiet mailbox from a slow link.
`cause_op` says which op expired.
If one op is always the one to expire, the budget is the constraint.
If `ping` is, the mailbox is.

### Is 5000ms the right read budget

Unknown. It is deliberately UNCHANGED.

This repo records one measured number for the round trip: p50 205ms over 2166
requests, 2026-09-26, market CLOSED.
`tests/live_measurements.py` is that number's one home.
5000ms is 24x that median.
A budget is sized against the TAIL.

The tail was not measured, and two revisions of the docs said it was.
`docs/MT4.md` claimed "p90 206ms, max 223ms" and `config.py` claimed a "22x
margin" from it.
Both were wrong and both are corrected (#127).
The pairing used in 2026-09 matched each request to the NEXT reply.
One unanswered request shifted every later sample.
Three went unanswered, so p90 and above were not measurements.

The median survived that by LUCK, not by design.
All three losses fell in the last 18% of the window.
About 82% of the samples were never shifted, and the median was among them.
Replayed with one loss moved to 20% in, the old pairing reads 418ms against a
true 206ms.
Do not inherit "the median is trustworthy" as a rule. It was a coincidence.

`measure-mailbox.ps1` now pairs within the request it is open on, and EXCLUDES a
request that got no reply instead of shifting the ones after it.
It reports p50, p90, p99 and max, with `paired` and `unanswered` beside them.
That makes the tail answerable from the same events, with no file reads.

**That fix has NOT been run against a live terminal.** It was written on a Mac
with no Windows host and no MT4. It is an UNVERIFIED instrument until Conrad runs
it on the Vultr box. Do not quote a tail number from this repo until then.

Change `mt4.timeout_ms` only on that output. Say what was measured and on what.

### The Expert MUST be recompiled before any of this is trusted

MQL4 does not compile in CI and does not compile on the developer seat.
The Expert half of the stale-request fence and of the budget declaration is
covered by SOURCE GUARDS ONLY and has not been executed anywhere.
Recompile `mt4/Experts/Mt4RiskBot.mq4` in MetaEditor on the terminal host, then
re-attach it, before you trust either.
New Expert input: `FenceGraceSec = 2`.

Every fix degrades safely against an Expert that was not updated.

| Fix | Against an un-updated Expert |
| --- | --- |
| duplicate-order refusal (the ledger and the client order id) | Works unchanged. It is entirely desk-side. |
| stale-request fence | Only the desk-side withdrawal survives. The old Expert ignores `ttl_ms`, harmlessly, and a request it has already claimed can still fire late. |
| split budgets | The budgets work. The declaration check reports `send fence: NOT MEASURED` instead of a verdict, so `doctor --connect` cannot go red on a short budget. |

With the fence live, a request older than its `ttl_ms` plus `FenceGraceSec` is
refused and nothing is sent:

```
ok=0 retcode=4109 error=request_expired survivor_ticket=0 age_sec=<n>
```

The Expert also prints that refusal, naming the op, the id, the age and the ttl.
The age is measured against the filesystem that HOLDS the request, using an offset
the Expert calibrates at `OnInit` by writing its own probe file, so no clock is
compared across two hosts.
`age_sec=-1` means the age could not be measured at all.
Then a SEND is refused and a READ is allowed.

## Chat lock

Only `TELEGRAM_CHAT_ID` is accepted.
Set it in the environment.
Do not put `chat_id` or the token in `config.toml`.
Updates from any other chat are ignored.
The bot still consumes those updates.
Replies and notifies go only to that chat.

## Sender lock

`TELEGRAM_ALLOW_SENDERS` lists the Telegram sender ids that may command the desk.
Use a comma between ids.
`telegram.allow_senders` in `config.toml` is the same list.
Every command is checked against the list.
Read-only commands are checked too.
An update whose sender cannot be read is refused.
Leave the list empty for a private chat id. That operator needs no config edit.
A group, supergroup, or channel chat id is negative.
On a negative chat id with an empty list, `run` and `doctor` exit non-zero.
To read a sender id, have that person send any message to the bot,
then read `from.id` from the `getUpdates` response.
A refused command is journaled as `command_rejected` and gets no reply.

## macOS

Homebrew has Python, not MetaTrader.
Install the terminal from metatrader5.com.
Log in once by hand.

1. Install the macOS binding.
   `pip install mt5-mac`
2. Run doctor with a login check.
   `python -m straightedge --config config.toml doctor --connect`

If `initialize` fails, launch MetaTrader 5.app yourself.
Wait until it is fully up.
The LaunchAgent starts the bot only.
It does not launch the terminal.

### LaunchAgent

RENAMED 2026-09-24. The label was `org.skyphusion.mt5-risk-bot`. If that agent is
still loaded, bootout `gui/$(id -u)/org.skyphusion.mt5-risk-bot` and remove its
plist BEFORE bootstrapping the new label. Two loaded agents is two loops on one
Telegram token, the hazard `deploy/LIVE.md` warns about.

1. Run doctor. It must exit 0.
   `python -m straightedge doctor`
2. Copy `docs/launchd.plist.example` to
   `~/Library/LaunchAgents/org.skyphusion.straightedge.plist`.
3. Edit `WorkingDirectory`.
4. Edit the venv `python` path.
5. Edit `EnvironmentVariables` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
6. Add `XAI_API_KEY` or `ANTHROPIC_API_KEY` if you use advice.
7. For an MT5 loop, change `--mode paper` to `--mode mt5`. For MT4, `--mode mt4`.
8. For a real account, arm from chat after start with
   `/live on I-ACCEPT-RISK`. Never put `--i-accept-risk` in
   `ProgramArguments` (fc34): `KeepAlive` means launchd restarts the bot
   on a crash, and a flag baked into the persisted argument list re-arms
   real money on every one of those restarts, unattended. `/live on
   I-ACCEPT-RISK` is per-process and does not survive a restart.
9. Put `--config` and the path before `run` in `ProgramArguments`.
10. chmod 600 the installed plist. Never commit it.

The committed example keeps `REPLACE_ME`, `KeepAlive`, and `Umask` 63 (077).
Watchdog: `journal.heartbeat` next to `journal_path` under `WorkingDirectory`.
Read it with `watch`. See `Watchdog` above.
`KeepAlive` restarts the process. The process comes back DISARMED.

11. Create a log directory under `WorkingDirectory`.
    `mkdir -p logs`
    Or point the log keys somewhere writable.
    `*.log` is gitignored.
12. Load the LaunchAgent.

```bash
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/org.skyphusion.straightedge.plist
launchctl print gui/$(id -u)/org.skyphusion.straightedge
```

Stop:

```bash
launchctl bootout gui/$(id -u)/org.skyphusion.straightedge
```

`KeepAlive` restarts a crash.
`Umask` 63 is 077, matching `main()`.
Watchdog liveness is `journal.heartbeat` next to the journal (ISO ts, chmod 0600).
Stale mtime means the loop is not ticking.
Run `watch --loop` beside it. Nothing else reads that file for you.
The lock is released when the bot dies.
The new bot can acquire `journal.lock`.
A leftover `journal.lock` file is not a held lock.
The confirm is restored from the live journal if the TTL has not expired.
Halt does not crash the bot.
Do not bootout to halt.

## Desk setup

`run` will not start without `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

1. Talk to BotFather.
2. Copy the token.
3. In Telegram, send a message to the account for that token.
4. Set `TELEGRAM_CHAT_ID` to that chat.
5. Export a model key.
   `export XAI_API_KEY=...` (Grok)
   and/or `export ANTHROPIC_API_KEY=...` (Claude)
6. Send a test ping.
   `python -m straightedge telegram --message ping`
7. Run doctor. It must exit 0.
   `python -m straightedge doctor`
8. Start the paper, mt5, or mt4 loop above.

Free text is `/ask`.
Context includes `/risk`, positions, working orders, and quotes.
A recommended market, limit, stop, or close-ticket is staged.
`/confirm` is the default send.
`/approve always` sends after risk preview. No `/confirm` each time.
Paper and demo accept `/approve always` at any time.
On `trade_mode=2` without live armed, `/approve always` is refused.
`/approve off` restores staging.
The bot still sizes the order.
The bot can refuse it.
Real-money: `--i-accept-risk` at start, or `/live on I-ACCEPT-RISK` in the locked chat.
The phrase is required.
Then `/approve always` if you want sends without `/confirm`.
`/auto on` is the only way the EMA regime trades on its own.
`/trail on` trails open positions each tick.
It does not turn auto on.
SL/TP hits and pending fills still alert in Telegram when auto is off.

## Handover posture

`/approve always` and `/auto on` are the two paths that reach the broker
with no human keystroke: `/approve always` sends inside the same `handle()`
call as the advice turn, and `/auto on` trades from the EMA signal with no
confirm step at all.
`telegram.allow_approve_always` and `telegram.allow_auto` in `config.toml`
(env: `TELEGRAM_ALLOW_APPROVE_ALWAYS`, `TELEGRAM_ALLOW_AUTO`) gate them.
Both default true, so an operator who never sets these keys sees no change.
Set either to false and the matching command is refused with a named reason
(`approve_always_disabled`, `auto_disabled`), journaled as `reject` with
`source=telegram`, and never answered in chat.
`/approve off` and `/auto off` are never refused; turning a capability off
is always allowed.
A value that is present but not a clean boolean is read as false, never as
the default: a config typo or a bad env var can only remove the capability,
never grant it.
`config.handover.toml` ships with both set false. Copy it to `config.toml`
for a handed-over desk. To re-enable on your own desk, set both to true, or
remove the keys.

The default stays true on purpose: flipping it would silently change every
existing deployment, including one that has never heard of this key. That
leaves a gap for a handover that forgets `config.handover.toml`, so the
posture is observable instead of hidden in a config file. `doctor` and
`run` print it on every invocation (`approve always: allowed|disabled`,
`auto: allowed|disabled`), and every `start` journal record carries
`approve_always_allowed` and `auto_allowed`, so `journal.jsonl` answers
which posture a session actually ran under, after the fact.

Paper is the default (`account.mode = "paper"`).
Real accounts still need `--i-accept-risk` at start, or `/live on I-ACCEPT-RISK` in the locked chat.

Stage a working order, then confirm:

```
/buy EURUSD limit=1.08000 sl=1.07800 tp=1.08300
/confirm
/orders
/cancel TICKET
/quote
/risk
/trail TICKET
/trail on
/sl TICKET PRICE
/tp TICKET PRICE
/tp TICKET PRICE VOL
/replace TICKET PRICE
/reverse TICKET
/closeby TICKET OTHER
/symbols
/symbols add NZDUSD
/symbols remove NZDUSD
/recap
```

`stop=` is the same shape (`/sell EURUSD stop=... sl=... tp=...`).
Do not set both `limit=` and `stop=`.
Bare `/cancel` drops a staged confirm.
`/cancel TICKET` cancels a working order.

`/halt` writes `HALT`.
It drops the confirm.
It cancels working orders.
It flattens positions.
The reply gives the counts, not a fixed sentence.
`/resume` only clears that file.
Daily-loss and max-drawdown cannot be cleared from Telegram.

`/closeby` is hedge-account only.
`/reverse` is two market sends.
The first send closes the ticket.
The second send opens the opposite side.
The circuit can leave you flat after `/confirm` already closed the ticket.
See Live desk limits.

## Live desk limits

WARNING
Nothing here guarantees profit.
Paper P/L is not live P/L.

`/closeby TICKET OTHER` is hedge-account only.
It sends `TRADE_ACTION_CLOSE_BY`.
A netting terminal refuses CLOSE_BY.
You cannot hold two tickets on one symbol.
There is no opposite ticket to close against.
Paper always hedges (a new ticket per deal).
Close-by works in paper even when a live netting account would not.

`/reverse TICKET` is two market sends.
`/reverse` stages.
`/confirm` first closes the ticket.
Then it sends the opposite side.
Staging and the first confirm preview exclude that ticket.
If halt, daily-loss, drawdown, or `risk_pct` refuse then, the ticket stays open.
After the close send succeeds, preview runs again.
Realized P/L can trip the circuit.
It can also leave no room to size the new side.
You are left flat (`closed #TICKET; reverse refused: ...`).
A failed opposite send is the same shape (`closed #TICKET; send failed ...`).

Production live: `doctor --connect` must exit 0 before `run --mode mt5` or `run --mode mt4`.
`trade_mode=2` also needs `--i-accept-risk` at start, or `/live on I-ACCEPT-RISK` in the locked chat.
Demo (`trade_mode=0`) does not.

## Journal

`journal.jsonl` is the source of truth for fills the bot observed.
A pending fill writes `open` with `fill=true`.
A vanished ticket writes `close` with `fill=true`.
The venue holds the live book. It is not the fill log.

A field reading `null` beside an `unrepresentable` list is a value the desk
measured and could not write as a number: `"sl": null` with
`"unrepresentable": ["sl=inf"]` means the stop arrived as infinity, not that
nobody set a stop. The list names the path and which spelling arrived (`inf`,
`-inf`, `nan`), and a path like `symbols[1].atr=nan` locates it inside a
nested row. Do NOT read such a row through a tool that invents a number for
it: before this existed, `jq` printed `1.7976931348623157e+308` for a value
the desk never saw.
JSONL, one event per line: `start`, `open`, `close`, `modify`, `reject`, `halt`, `order_check_fail`, `pending`, `recap`, `reconnect`, `loop_error`, `confirm_stage`, `confirm_cancel`, `confirm_sent`, `approve_always`, `approve_off`, `auto_on`, `auto_off`, `live_on`, `live_off`, `live_not_restored`, `risk_state_error`, `advice_turn`, `advice_circuit_block`, `advice_stage_failed`, `flatten`, `flatten_incomplete`, `close_failed`, `close_partial`, `cancel_failed`, `positions_read_failed`, `orders_read_failed`, `account_read_failed`, `send_unresolved`, `send_refused_unresolved`, `confirm_unresolved`, `inflight_unreadable`, `notify_truncated`, `venue_clock`, `stop`.
`reject` is written by every gate that refuses, on every path, and it is the
record to grep when the bot will not trade.
It carries the NAMED `reason`, plus `source` (`auto`, `telegram`, or `advice`)
and `stage` (`signal`, `stage`, `stage_close`, `confirm`, `reverse`,
`confirm_reverse`, `reverse_after_close`, or `approve`).
`symbol`, `kind`, `rr`, `ticket`, and `command` are present when the refused
request had them.
A refusal is journaled and is never sent back to the chat that triggered it.
The chat gets its one-line reply, and nothing else.
With no journal configured the refusal still prints to stderr, so a control
that fired can never look unexercised.
`advice_turn` carries `degraded` when the schema gate forced the hold: the
reason that reply could not be read, as the gate's own violation names
(`unknown field x`, `symbol is not ASCII`, `tp is neither a number nor null`).
Empty means nothing degraded, and the field is always present, so an empty one
cannot be confused with a desk too old to emit it.
**Read it before reading `action=hold` as a view.** A hold with a non-empty
`degraded` is the desk refusing to act on a reply it could not parse, not the
model standing aside, and those two were indistinguishable in this file before
straightedge#185. The prose reason still goes to the chat as well, so an
operator watching at the time sees both.
`advice_turn` closes one advice turn: `provider`, `session`, `action`,
`symbol`, `sl`, `tp`, `limit`, `stop`, `ticket`, and `staged` (whether the
desk tried to turn the suggestion into an order).
The question and the model reply are never written to `journal.jsonl`.
`journal.advice.json` keeps the last 40 turns. With the agent, the workspace `log.md` keeps every turn, with no cap.
See `docs/DATA.md`.
`advice_circuit_block` is the circuit refusing to let the model stage at all.
`advice_stage_failed` carries `measured=false`: the order could not be built,
so no rule said no. COULD NOT MEASURE is not REFUSED, and it is deliberately
not a `reject`.
`recap` is the daily P&L summary, and it comes in two shapes that must not be
read alike. The normal one carries `equity`, `day_start` and `pnl` for a
boundary the desk was running across. The other carries `unmeasured` naming
`equity` and `pnl`, plus `reason=desk_down_across_the_day_boundary`,
`days_skipped`, and `last_observed_equity` with `last_observed_at`: that is a
day that ended while the desk was off, so its closing equity was never
observed and NO P&L is computed for it. The notify line says
`pnl=NOT MEASURED`. There is no `pnl` field in the row at all, deliberately,
because a zero there could not be told from a flat day.
`day_start` is in the `unmeasured` list too when a previous boot died after
rolling the day: the baseline then exists nowhere and is named rather than
invented.
One such row per restart, whatever the number of missed boundaries, and the
count is in `days_skipped`. If you see one, the desk was down across a UTC
midnight: check the supervision task and `journal.heartbeat`. A second restart
on the same day does not repeat it, because whether a day is owed is read from
the journal: the most recent `day` on a `start` or `stop` row against the last
`recap` row's `day`. That also means it survives a snapshot the desk cannot
read, and that a journal rotation between two boots can at worst repeat the
message once, never swallow it.
`flatten` is one record per sweep.
It carries `requested`, `confirmed_closed`, `closed_elsewhere`, `survivor_count`, and `measured`.
`flatten_incomplete` is the same record, written again, when the sweep left risk open.
Grep `flatten_incomplete` after any halt.
`close_partial` means the broker filled less volume than asked.
That is residual risk, not a close.
`order_check_fail` carries `reason`: `broker_refused` means the venue rejected
the pre-trade check, `not_measured` means the venue returned nothing, so the
check never ran and the order was NOT sent. `not_measured` with `retcode=-1` is
an IPC or bridge fault, not a trading decision; check the terminal link.
Grep `reject` if it never trades.
`outside_session` and `no_regime` are the usual reasons.
`venue_clock` records WHICH CLOCK the desk was on, which no other row says.
`command_error` and `advice_error` are the rows for a command that FAILED, as
opposed to one that was refused. A refusal is `reject` with a named reason; a
failure is one of these two with `measured=false` and the exception class in
`error_type`. The message the chat showed is NOT in the row, on purpose, so
read the chat for the sentence and the journal for the fact. `advice_error`
also carries `turn_spent`: the daily advice budget is counted before the
provider is called, so a failed turn still costs a slot, and this row is the
only place that says which slot went where.
It is journal-only and never pings the chat. One row at `start()`, and one more
whenever the offset CHANGES: a server-side DST roll or a reconnect that lands on
a different server, both of which happen with nobody editing anything. An offset
that has not moved writes nothing, so the file carries boundaries rather than a
per-poll log.
Fields: `offset_sec` when it is measured, or `unmeasured` naming what was not
plus a `detail`; `implied_offset_sec` when the venue could only imply it, which
is what `start()` sees because it has no previous poll to bound the sample with;
`previous_offset_sec` and `previous_unmeasured` on a transition, so one row
states what it moved FROM and a DST roll is legible without diffing two rows;
`sampled`, `venue`, `symbol` and `measured_at`.
Read the row by ONE rule: a key that is there carries a measured value. Nothing
on this row is written as null. If `offset_sec` is missing the desk could not
read the clock, and `unmeasured` says what it could not read; if
`previous_offset_sec` is missing there was no previous offset to state, and
`previous_unmeasured` says what the previous reading was missing.
Why it matters after the fact: before this the offset was measured, used to
convert every bar, and discarded, so `journal.jsonl` could say the desk did not
know what time it was and never that it thought it was UTC+3. Reconcile an order
against the clock that converted its bar by reading the last `venue_clock` row
before it.
A WESTWARD move is recorded LATE, by about its own size, and that is the auto
leg's advance gate rather than this record: bar stamps move back with the
server, and `step_symbol` returns until they climb past their previous high. The
row states what the desk measured when it next ACTED, which is the only instant
it has evidence for.
`venue_clock_unmeasured` means the venue could not state its UTC offset, so
the auto leg cannot know WHEN it is and refuses every signal. The record
carries `unmeasured` and `detail`, and `unmeasured` names which of three
things happened:
- `server_time`: no stamp on the reply. On MT4 that is an Expert too old to
  send `time=TimeCurrent()`; recompile and reattach
  `mt4/Experts/Mt4RiskBot.mq4`.
- `offset_sec`: a stamp arrived and could not be read as an offset. `detail`
  says whether it was off the grid, outside the civil timezone band, or read
  across too wide a round trip.
- `freshness`: the caller could not bound how old the stamp is. On the auto
  leg that means the desk's own poll interval was too wide; see
  `engine.poll_seconds`.

Run `doctor --connect`. It prints what the stamp IMPLIES and says plainly that
freshness is NOT established, because `doctor` has no previous poll to bound
the stamp with, and it exits ZERO on that: a closed market is the normal
weekend state and is not a fault. Compare the implied offset against the
server clock in the terminal. `doctor` exits non-zero only when the clock
cannot be read at all, and, from config alone, when `engine.poll_seconds` is
too slow for any sample to be bounded.

`symbol_not_allowed` names the symbol the MODEL sent, byte for byte, and not an uppercased
version of it. If that string looks odd, read it as odd: a name that is not ASCII is refused on
sight, because uppercasing one can turn it into a real instrument
(`EURU\u017fD` uppercases to `EURUSD`). Nothing is staged from it.
`venue_clock_bar_disagrees` is the other clock refusal and it means the
opposite: the offset WAS measured, and the venue's own forming bar contradicts
it. The record carries `offset_sec`, `bar_time` and `implied_server_now`. A
server cannot be forming a bar that has not opened on its own clock, so this is
a stale tick stamp (the market closed, or the terminal lost its feed while the
series kept its last bar) and the offset is wrong by that staleness. Check the
terminal's own clock and its connection status; the desk resumes by itself once
ticks flow again.

Manual `/buy` and `/sell` still work while any of this holds, because they
time themselves off the bot's clock and never off a bar.
`reconnect` is a dropped venue link, then a fresh connect.
It is written on MT4 and on MT5.
On MT4 it is a mailbox round trip that got no reply.
`ok=true` means the link came back. `ok=false` means it did not.
`error` is the reconnect ATTEMPT failing.
`cause` is what triggered the reconnect.
They are two different faults. Do not read one as the other.
`cause` was added in #127. Before it, a recovered blip wrote `ok=true` alone.
That row said a reconnect happened and never said why.

| Field | Meaning |
| --- | --- |
| `cause` | the triggering error, clipped to 200 characters |
| `cause_type` | its exception class, for example `BridgeTimeout` |
| `cause_op` | the mailbox op in flight: `ping`, `account`, `rates`, `market` |
| `cause_transport` | `file` (the mailbox) or `net` (HTTP to the shim) |
| `cause_phase` | where on that transport it expired. See `docs/MT4.md` |
| `cause_withdrawal` | `withdrawn`, `claimed`, or `locked`. See `Unresolved sends` |
| `cause_req_id` | the request id, to match against the Experts log |
| `unselected` | how many symbols could not be reselected after the connect |

A missing `cause_*` field means the error did not carry it.
It does not mean the value was empty.
`unselected` is absent when every symbol came back.
`unselected` present with `ok=true` is a PARTIAL recovery.
Those symbols cannot trade until they are selected.
The names print to the desk log, not to the row.

`account_read_failed` is the third state.
The reconnect reported `ok=true` and the account still could not be read.
It carries `error`, `error_type`, and the same `op` / `transport` / `phase` /
`withdrawal` fields under an `error_` prefix.
Before #127 this tick returned with nothing written at all.
`loop_error` is a tick that raised.
The bot kept running.
`journal.inflight.json` is the unresolved-send ledger (not JSONL).
It holds the sends that left with no verdict. Read `Unresolved sends` before you
touch it.
`journal.tg_offset` is the Telegram `getUpdates` cursor (not JSONL).
`journal.equity.json` is the risk state: `day_key`, `day_start_equity`, `peak_equity`.
It is written whenever one of those three moves, not only at a halt.
The write is atomic: a temp file, then a rename. A kill mid-write cannot truncate it.
`start` reads it back, so a restart does not hand out a new loss budget on the same UTC day.
A new UTC day still resets `day_start_equity`. The peak is not daily.
To reset the peak, stop the bot and delete the file.
A corrupt or truncated file halts with reason `state_unreadable`. The bot does not change the file.
A file that cannot be written halts with reason `state_unwritable`.
Both mean the bot could not measure. Neither is treated as a clean start.
`journal.lock` is an exclusive lock so two loops cannot share the journal or offset.
Unix: flock. Windows: msvcrt.locking.
`journal.heartbeat` is rewritten each successful `step_all`.
Line 1 is an ISO timestamp. Then `blocked=`, `mode=`, `stale_after_s=`,
`tick_budget_s=`, `tick_gap_max_s=`, `over_budget=`, `tick_gap_ever_s=`,
`over_budget_ever=`, `run_id=`, `started_at=`, and `deployed=`.
This list did not name `run_id=`, `started_at=` or `deployed=`. It does now.
`blocked=` carries the gate reason, from the same call that refuses a send.
`watch` reads it. See `Watchdog`.
Before a write that would exceed 10 MiB, the live file is renamed to `journal.jsonl.1`.
That is one generation.
The previous `.1` is replaced.
`tail` and confirm restore read only the live file.
`journal.jsonl`, `journal.jsonl.1`, `journal.tg_offset`, `journal.equity.json`, `journal.inflight.json`, `journal.lock`, and `journal.heartbeat` are owner-only (chmod 0600).
