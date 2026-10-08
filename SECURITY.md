# Security

Report vulnerabilities to conrad@skyphusion.org.

WARNING
Do not open a public issue for a live trading defect that could move money.

The bot is the Python process on this computer.
The desk is Telegram chat commands.
The agent is the Cloudflare Computer worker.
The gateway is Cloudflare AI Gateway `mt5-risk-bot`.

## Production secrets

Secrets BELONG in the environment. Put them there.

`config.toml` is accepted as a fallback for nine of them, and the environment
wins whenever both are set. This section used to say "Do not put secrets in
`config.toml`", which read as a control the loader does not have; it was a
recommendation, and `straightedge#139` made the document say what the code
does rather than the reverse. `config.toml` is gitignored and the operator
keeps it 0600.

Secret names, each with the TOML key the loader will read when the variable is
unset. `src/straightedge/config.py` declares this table as `FILE_SOURCED_SETTINGS`
and `tests/test_config_secret_provenance.py` asserts this document and that
tuple name the same nine, in both directions, so the two cannot drift again.

| Environment variable | TOML key read as a fallback |
| --- | --- |
| `MT5_LOGIN` | `mt5.login` |
| `MT5_PASSWORD` | `mt5.password` |
| `MT5_SERVER` | `mt5.server` |
| `TELEGRAM_BOT_TOKEN` | `telegram.token` |
| `TELEGRAM_CHAT_ID` | `telegram.chat_id` |
| `XAI_API_KEY` | `advice.grok_key` |
| `ANTHROPIC_API_KEY` | `advice.claude_key` |
| `ADVICE_URL` | `advice.computer_url` |
| `ADVICE_TOKEN` | `advice.computer_token` |

`MT4_MAILBOX_TOKEN` is the one secret with NO TOML key at all. A
`mt4.mailbox_token` written in the file is read by nothing. It is REPORTED,
not refused: the only state where ignoring it could leave an order endpoint
unauthenticated is `mt4.mailbox_url` set with no `MT4_MAILBOX_TOKEN`, and
`mt4_net.require_token` already refuses that at startup on both ends.

### Which source did THIS config use

`doctor` and `run` both print it, by key name and never by value:

```
secrets: all from the environment
secrets: 2 from config.toml (telegram.token, advice.grok_key); the environment overrides any of them. Values are never printed
secrets IGNORED in config.toml: mt4.mailbox_token. The loader reads these from nowhere; put the value in the environment variable instead, and remove it from the file
```

The journal's `start` record carries the same two lists as
`settings_from_file` and `settings_read_from_nowhere`, so the posture is readable
live and after the fact. Same trade as the handover posture in
`docs/CONTRACT.md`: a permissive default that every existing deployment
already depends on is left alone, and the gap it leaves is closed by
observability rather than by a stricter default.

The agent uses `CF_AIG_TOKEN` and `ADVICE_TOKEN`.
The agent bills through the gateway with Unified Billing.
Do not put a provider key on the agent.

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
`journal.advice.json` is chmod 0600 on each write (written to a temp file, then replaced).
What each file holds, where it goes, and for how long: `docs/DATA.md`.

WARNING
Two things arm a real-money account, both per process, neither restored by
a restart: starting the bot with `--i-accept-risk`, or `/live on
I-ACCEPT-RISK` in the locked Telegram chat at any time while it runs. A
restart never re-arms from an earlier `/live on` in the journal; `start`
writes `live_not_restored` instead, and `/live` says so until the phrase
is re-typed. `/live off` disarms immediately. The risk engine still sizes
and can refuse a sized order even while armed.
