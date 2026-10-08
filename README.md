# straightedge

The bot is the Python process on this computer.
The desk is Telegram chat commands.
The agent is the Cloudflare Computer worker.
The gateway is Cloudflare AI Gateway `mt5-risk-bot`.
Only `AI_PROVIDER=computer` (the agent) routes through the gateway. The
default `AI_PROVIDER=grok` and `AI_PROVIDER=claude` are BYOK straight to
`api.x.ai` and `api.anthropic.com`: no gateway billing, caching, rate
limit, or observability on either.

You send desk commands from one Telegram chat.
The bot sizes every order.
The bot can refuse an order.
Default: advice is staged.
`/confirm` is the default send.
`/approve always` sends after risk preview.

WARNING
Nothing here guarantees profit.
Paper is the default.
Auto EMA trading is off until `/auto on`.

## Names

| Word | Meaning |
| --- | --- |
| the bot | the Python process on this computer |
| the desk | Telegram chat commands |
| the agent | the Cloudflare Computer worker |
| the gateway | Cloudflare AI Gateway `mt5-risk-bot`. Used only by `AI_PROVIDER=computer`; `grok` and `claude` are direct BYOK, not gatewayed |
| the circuit | halt, daily-loss, and drawdown gates |

## Install and paper run

NOTE
Put `--config` before the subcommand.
Example: `python -m straightedge --config config.toml run`.

1. Create a venv.
   `python3 -m venv .venv`
2. Activate the venv.
   `source .venv/bin/activate`
3. Install the bot.
   `pip install -e ".[dev]"`
4. Copy the example config.
   `cp config.example.toml config.toml`
   Handing this desk to someone else instead? Copy `config.handover.toml`;
   it disables `/approve always` and `/auto on`. See docs/RUNBOOK.md.
5. Set the Telegram token.
   `export TELEGRAM_BOT_TOKEN=...`
6. Set the locked chat id.
   `export TELEGRAM_CHAT_ID=...`
   For a shared chat, also set the sender allow-list.
   `export TELEGRAM_ALLOW_SENDERS=...`
7. Set a Grok key if you use default advice.
   `export XAI_API_KEY=...`
8. Run tests.
   `pytest`
9. Run doctor.
   `python -m straightedge doctor`
10. Start the paper loop only if doctor exits 0.
    `python -m straightedge --config config.toml run --mode paper --loop`

Do not start a long run until doctor exits 0.

For Claude advice:

```bash
export ANTHROPIC_API_KEY=...
export AI_PROVIDER=claude
```

For the agent:

```bash
set -a
source agent/.dev.vars
set +a
export AI_PROVIDER=computer
export ADVICE_URL=https://mt5-risk-agent.skyphusion.workers.dev/ask
```

See `agent/README.md` and `docs/RUNBOOK.md`.

## Loop facts

`--loop` retries Telegram HTTP 429 and 5xx.
`--loop` calls `initialize` again after a dropped MT5 IPC.
`engine.poll_seconds` is the Telegram `getUpdates` timeout.
The example config sets `poll_seconds = 1`.
If the key is omitted, load uses 15.
The Telegram offset is `journal.tg_offset` next to the journal.
The risk state is `journal.equity.json` next to the journal.
It keeps the daily loss budget and the equity peak across a restart.
One failed tick is journaled as `reconnect` or `loop_error`.
The bot stays up.
Two `run --loop` processes cannot share one journal.
The second process prints `already running` and exits 2.
Each successful tick writes `journal.heartbeat`.
`python -m straightedge watch` reads it and says whether the desk is ticking AND
armed. Three states, not two: a restart brings the desk back disarmed on
purpose, and `ALIVE NOT TRADING` is how you hear about it.
The live journal rotates to `journal.jsonl.1` at 10 MiB.
`journal.jsonl` is the source of truth for fills the bot observed.

See `docs/RUNBOOK.md`.

macOS LaunchAgent: copy `docs/launchd.plist.example`.
See `docs/RUNBOOK.md` for load steps.

## Live MT5

Live and demo need a running terminal.
The official `MetaTrader5` package is Windows-only.
On macOS, install MetaTrader 5.app from metatrader5.com.
Then run `pip install mt5-mac`.

1. Set broker secrets.
   `export MT5_LOGIN=...`
   `export MT5_PASSWORD=...`
   `export MT5_SERVER=YourBroker-Demo`
2. Run doctor with a login check.
   `python -m straightedge --config config.toml doctor --connect`
3. Stop if doctor is not 0.
4. Start the live loop.
   `python -m straightedge --config config.toml run --mode mt5 --loop`

## Live MT4

MetaTrader 4 has no official Python package.
Attach `mt4/Experts/Mt4RiskBot.mq4` to one chart.
Set `mt4.files_dir` to Common Files (`MT4_FILES_DIR`).
On Windows, omit it. Default is `%APPDATA%\\MetaQuotes\\Terminal\\Common\\Files`.
By default the bot process runs on that Windows host (`journal.lock` uses
`msvcrt`). To run it somewhere else, start `straightedge mt4-shim` on the MT4
host and set `mt4.mailbox_url` on the desk; the Expert does not change.
See `docs/TRANSPORT.md`.

1. Set `account.mode = "mt4"`.
2. Run doctor with a mailbox check.
   `python -m straightedge --config config.toml doctor --connect`
3. Stop if doctor is not 0.
4. Start the live loop.
   `python -m straightedge --config config.toml run --mode mt4 --loop`

See `docs/MT4.md` and `mt4/README.md`.

WARNING
A real account (`trade_mode=2`) also needs `--i-accept-risk` at start,
or `/live on I-ACCEPT-RISK` in the locked chat.

The bot accepts only `TELEGRAM_CHAT_ID`.
The journal, stderr, and chat echoes redact BotFather tokens as `[REDACTED]`.
`/confirm` is restored from the journal if the 120s TTL has not expired.
Halt is `touch HALT` or `/halt`.
The bot stays up after halt.
Journal, offset, lock, heartbeat, and HALT files are chmod 0600.
The bot sets umask 077.
Secrets stay in the environment.
See `SECURITY.md`.

## Desk

`/buy EURUSD` stages a sized market order.
The bot uses an ATR stop if you omit `sl=`.
`/buy EURUSD limit=1.08000 sl=... tp=...` stages a working order.
`stop=` is the same shape.
Do not set both `limit=` and `stop=`.
`/confirm` sends the staged order.
`/approve always` sends after risk preview. No `/confirm` each time.
`/approve off` restores staging.
`/live on I-ACCEPT-RISK` arms real-money sends from the locked chat.
The phrase is required.
`/orders` lists working orders.
`/cancel TICKET` cancels a working order.
Bare `/cancel` drops a staged confirm.

`/quote` with no symbol lists the book.
`/symbols list|add|remove` edits the book at runtime.
`/risk` shows daily-loss and drawdown room.
`/sl` and `/tp` TICKET work on positions and working orders.
`/replace TICKET PRICE` moves a working order entry.
`/reverse TICKET` stages a close plus the opposite market.
`/confirm` then sends two market orders.
The first send closes the ticket.
The second send opens the opposite side.
The circuit can refuse the second send.
Then you are left flat.
`/closeby TICKET OTHER` offsets two opposite hedges on the same symbol.
It uses `TRADE_ACTION_CLOSE_BY`.
Hedge accounts only.
A netting terminal refuses it.
Paper always hedges.
Paper P/L is not live P/L.
`/tp TICKET PRICE VOL` scales out VOL at PRICE.
The rest stays.
`/trail TICKET` moves SL with the ATR trail.
It never loosens.
`/trail on` does that every tick for open positions.
It does not turn on EMA entries.
Default is off.

SL/TP hits and pending fills still alert when `/auto` is off.
A UTC day roll sends a recap: one line, equity vs `day_start`, once per day.
`/recap` dumps that plus the last journal rows.
A recap is not a trade.

Free text is advice.
The model can stage a market, `limit=`, `stop=`, or close-ticket order.
`/confirm` is the default send.
`/approve always` sends after risk preview.
If the next order would trip the circuit, advice is hold or close only.
Send `/help` for the rest.

## Docs

`docs/CONTRACT.md` is the behaviour that tests enforce.
`docs/VENUE.md` is the provider-agnostic execution API (`MarketOrder`, `WorkingOrder`).
`docs/THIRD-PARTY.md` is every third-party component and the licence terms it carries.
`docs/MT4.md` is the MT4 file-mailbox ICD.
`docs/RUNBOOK.md` is paper, live, `/live on I-ACCEPT-RISK`, `/approve always`, `poll_seconds=1`, HALT, confirm-on-restart, lock, heartbeat, `watch`, the unattended Windows scheduled task, journal rotate, and launchd.
`docs/launchd.plist.example` is a user LaunchAgent.
It uses paper `--loop`, `KeepAlive`, and `Umask` 63.
Tokens stay `REPLACE_ME` in the example.

## License

MIT. Third-party terms, including the MetaQuotes EULA that governs the terminal
and MetaEditor, are in `docs/THIRD-PARTY.md`.
