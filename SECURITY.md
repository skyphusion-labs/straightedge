# Security

Report vulnerabilities to conrad@skyphusion.org.

WARNING
Do not open a public issue for a live trading defect that could move money.

The bot is the Python process on this computer.
The desk is Telegram chat commands.
The agent is the Cloudflare Computer worker.
The gateway is Cloudflare AI Gateway `mt5-risk-bot`.

## Production secrets

Secrets live in the environment.
Do not put secrets in `config.toml`.
`config.toml` is gitignored.

Secret names:

- `MT5_LOGIN`
- `MT5_PASSWORD`
- `MT5_SERVER`
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `XAI_API_KEY`
- `ANTHROPIC_API_KEY`
- `ADVICE_URL`
- `ADVICE_TOKEN`

The agent uses `CF_AIG_TOKEN` and `ADVICE_TOKEN`.
The agent bills through the gateway with Unified Billing.
Do not put a provider key on the agent.
Env vars override toml if both are set.

Journal writes replace keys named `token`, `password`, `api_key`, `grok_key`, `claude_key`, `mailbox_token`, and `login` with `[REDACTED]`.
`Journal.tail()` redacts again when it reads, so a row written by an older build is redacted too.
Journal writes also strip BotFather token patterns from string fields.
Loop stderr and Telegram `send` strip the same BotFather pattern.

### The broker login

`MT5_LOGIN` is an account IDENTIFIER, not a credential.
It grants nothing on its own: `MT5_PASSWORD` and `MT5_SERVER` are separate and the password is never journaled.
It is on the secret-names list anyway, because four paths used to put it in front of a reader (straightedge#90): the journal, `doctor --connect` on stdout, `/history` and the daily `recap` in the chat, and the `history` field of the advice request, which leaves the box.
The journal and the chat now redact it outright; nothing an audit reads a `start` row for is lost, since `mode`, `equity`, `server` and `symbols` all remain.
`doctor --connect` MASKS it instead, to `***` plus the last four digits, and shows nothing at all below six digits.
That difference is deliberate: `doctor --connect` exists to tell the operator which account the terminal is attached to, and a full `[REDACTED]` would not answer that question, while the last four answer it for someone who already knows the number and survive a screen share, a screenshot, or `doctor` output pasted into an issue.

Only `TELEGRAM_CHAT_ID` is accepted.
Updates from any other chat are ignored.
The bot still consumes those updates.
Replies go only to that chat.

`TELEGRAM_ALLOW_SENDERS` is a comma-separated list of Telegram sender ids.
`telegram.allow_senders` in `config.toml` is the same list.
Every command is checked against that list before it runs.
Read-only commands are checked too.
An update whose sender cannot be read is refused.
An empty list keeps a private chat id working with no config edit.
A group, supergroup, or channel chat id is negative.
The bot refuses to start on a negative chat id with an empty list.
A refused command is journaled as `command_rejected` with the sender id.
A refused command gets no reply.

`journal.jsonl` is chmod 0600 on open and after each write.
`journal.jsonl.1` stays chmod 0600 after rotate.
`journal.tg_offset` is chmod 0600 on each persist.
`journal.lock` is chmod 0600 when `run` takes the exclusive lock (flock / msvcrt).
`journal.heartbeat` is chmod 0600 after each write.
`HALT` is chmod 0600 when the bot writes it.

WARNING
Two things arm a real-money account, both per process, neither restored by
a restart: starting the bot with `--i-accept-risk`, or `/live on
I-ACCEPT-RISK` in the locked Telegram chat at any time while it runs. A
restart never re-arms from an earlier `/live on` in the journal; `start`
writes `live_not_restored` instead, and `/live` says so until the phrase
is re-typed. `/live off` disarms immediately. The risk engine still sizes
and can refuse a sized order even while armed.
