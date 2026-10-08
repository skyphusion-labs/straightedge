# Data: what the desk stores, what it sends, and for how long

The bot is the Python process on this computer.
The desk is Telegram chat commands.
The agent is the Cloudflare Computer worker.
The gateway is Cloudflare AI Gateway `mt5-risk-bot`.

This page is read from the code, with the file and line that does each thing.
Where a line says "no cap" or "never deleted", that is the measured state of
the code, not a policy. The code is the contract; this page says what it does.

## Files the bot writes

All of them sit beside `journal.jsonl` (`engine.journal_path`, default
`journal.jsonl` in the config directory). All are chmod 0600 (`SECURITY.md`).

| File | What is in it | How long it stays |
| --- | --- | --- |
| `journal.jsonl` | One JSON record per event: fills, refusals, risk state, `start` (mode, account `login`, equity, server, symbols, posture), and `advice_turn` (`provider`, `session`, `action`, `symbol`, `sl`, `tp`, `limit`, `stop`, `ticket`, `staged`). **The question and the reply are not in it** (`desk.py`, `_ask`, the `advice_turn` write). | Rotates to `journal.jsonl.1` at 10 MiB (`journal.py`, `_ROTATE_BYTES`); one old generation is kept. Nothing deletes either file. |
| `journal.advice.json` | `{"turns": [{"role": "user" or "assistant", "content": ...}]}`. The `user` content is the question you typed. The `assistant` content is the reply prose (the text before the JSON tail, or the raw reply when there is no tail). Secrets are redacted on write and again on load (`llm.py`, `_remember`, `load`, `redact_text`). The desk snapshot is not stored here. | The last 40 turns (`KEEP_TURNS = 40`, `llm.py`), that is the last 20 questions and the last 20 replies. Rewritten after every advice turn, atomically (`save`). **No age limit and no delete path:** the most recent 40 turns stay until you delete the file. Delete it with the bot stopped; the next turn starts with no memory. |
| `journal.tg_offset`, `journal.lock`, `journal.heartbeat`, `HALT` | Telegram update offset, the exclusive-run lock, the last tick time, the halt marker. No chat content. | See `docs/RUNBOOK.md`. |

`journal.advice.json` is written for every provider, including the agent
(`_remember` runs after every `ask`). What is sent back out of it differs by
provider, below.

## What an advice turn sends, by provider

Every `/ask` and every free-text message is an advice turn (`desk.py`,
`handle`). Each one sends the question plus the desk context
(`engine.advice_context`): status (`mode`, broker `server`, equity, balance,
peak equity, position count, risk percent), risk room, open positions, working
orders, the symbol list, the auto and trail flags, and a quote per symbol. The
account login is not in the context; it is in the journal `start` record (see
below, and straightedge#90).

| `AI_PROVIDER` | Where it goes | What goes with the question |
| --- | --- | --- |
| `grok` (default) | `api.x.ai`, with your `XAI_API_KEY`. No gateway. | The desk context, **and the last 40 turns from `journal.advice.json` as prior messages** (`llm.py`, `_grok`). xAI's terms govern what xAI keeps. |
| `claude` | `api.anthropic.com`, with your `ANTHROPIC_API_KEY`. No gateway. | Same shape (`_claude`). Anthropic's terms govern what Anthropic keeps. |
| `computer` | `ADVICE_URL`, with `ADVICE_TOKEN` as a bearer token. The shipped URL is `https://mt5-risk-agent.skyphusion.workers.dev/ask`, a Worker on the maintainer's Cloudflare account (`agent/wrangler.jsonc`, `CF_ACCOUNT_ID`). | The desk context, the Telegram chat id as `session`, the model id, and **the last 40 records of `journal.jsonl`** as `history` (`engine.advice_history` is `journal.tail(40)`; `llm.py`, `_computer`). The stored advice turns are not sent on this path. The `start` record is among those 40 after a restart, and it carries the account `login`. |

**`compose.yaml` chooses the agent.** The Docker paper desk sets
`AI_PROVIDER: computer` and `ADVICE_URL` to the maintainer Worker above
(`compose.yaml`). So, as shipped, a compose deployment sends every advice turn,
the desk context and the journal tail to infrastructure the maintainer
operates, not to a provider you chose with your own key. It only works with an
`ADVICE_TOKEN` the maintainer issued, and you can point `ADVICE_URL` at your
own deployment of `agent/`, but the default is the maintainer's. Nothing else
in the docs said so before this page.

## What the agent keeps

One Durable Object per `session` (the Telegram chat id). Its storage is SQLite
on the Cloudflare account that runs the Worker. Files, from
`agent/src/desk-agent.ts`:

| Path | What is in it | How long it stays |
| --- | --- | --- |
| `/workspace/snapshot.md` | The desk context from the latest turn. | Overwritten on every turn. |
| `/workspace/history.json` | The journal tail from the latest turn. | Overwritten on every turn. |
| `/workspace/log.md` | Every turn, appended: `## <UTC timestamp> user` and the question, then `## <timestamp> assistant` and the full reply. | **Capped at the 40 most recent entries, within 32 KiB and 800 lines** (`agent/src/log-retention.ts`, straightedge#131). An entry is one role's message, so 40 is the same unit and the same number as the desk's own `llm.KEEP_TURNS`; the byte and line caps are the read tool's own limits, so the whole log fits in one read. Older entries are DELETED on the next turn and the drop is named on the file's first line with cumulative totals. There is still no delete route and no age limit: the Worker answers `GET /health` and `POST /ask` only (`agent/src/index.ts`), so a session's most recent 40 entries stay until the Worker's operator deletes that Durable Object. |
| `/workspace/notes.md` | Whatever the model writes as durable notes. | **Not capped by us**, and unlike `log.md` nothing appends to it: the model rewrites it whole through the write tool. In practice it settles around the read window, because the model can only see 32 KiB of it to carry forward, and the edit tool refuses a file over that cap outright. No delete route, same as above. |

So, with the agent, **your most recent 40 messages are kept on the Worker
operator's account, and everything older is deleted on the next turn.** That is
a cap, not an expiry: a session that stops being used keeps its last 40 entries
indefinitely, because there is no age limit and nothing runs when nobody asks.
`notes.md` is whatever the model chose to keep, and that stays too.

If that operator is the maintainer (the compose default), deleting what remains
means asking the maintainer. If you run `agent/` yourself, you delete the
Durable Object storage yourself. straightedge#131 decided the cap; a delete
route and an age limit are still not implemented.

**Inference from the agent.** The agent calls the gateway REST API with
`cf-aig-collect-log-payload: false` (`desk-agent.ts`), which asks the gateway
not to store the prompt and reply. Gateway logs of metadata (model, tokens,
timing) depend on the gateway's own settings, which live in the Cloudflare
dashboard and not in this repo. The model is xAI's, billed through Cloudflare;
xAI's terms govern what xAI keeps. `agent/wrangler.jsonc` enables Workers
observability; the agent code writes nothing from the question or the reply to
`console`, so those logs hold request metadata only.

## Telegram

Everything the desk says is a Telegram message in your chat, including the
advice replies. Telegram keeps it under Telegram's terms. This repo cannot
change that.

## How to check this page

- `grep -n KEEP_TURNS src/straightedge/llm.py` and read `_remember`, `save`,
  `_grok`, `_claude`, `_computer`.
- `grep -n "advice_history\|advice_context" src/straightedge/engine.py`.
- `grep -n "log.md\|snapshot.md\|history.json" agent/src/desk-agent.ts` and
  `grep -n pathname agent/src/index.ts` for the routes.
- `grep -n "AI_PROVIDER\|ADVICE_URL" compose.yaml`.

Read 2026-10-08 at `main` `3c34378ce02417f58026a581e4a3cad7cc0606f8`. A later
change to any of those files reopens this page.
