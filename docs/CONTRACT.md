# Contract

`docs/TESTING.md` is the companion to this file: this one says what the suite enforces, that one says what a green suite cannot see. Read it before writing a fixture.

Code that disagrees with this file is wrong.

The bot is the Python process on this computer.
The desk is Telegram chat commands.
The agent is the Cloudflare Computer worker.
The gateway is Cloudflare AI Gateway `mt5-risk-bot`.
The circuit is halt, daily-loss, and drawdown gates.
The risk engine is the sizer in the bot (`RiskManager.evaluate`).

You trade and ask for advice from one chat.
Paper, MetaTrader 5, or MetaTrader 4 is the execution venue.
The risk engine is the only thing that can size or refuse an order.
The venue API is `Broker` (`MarketOrder`, `WorkingOrder`). Engine does not send MT5 request dicts.
Auto EMA trading is off until `/auto on`.

## Allowed claims

| Claim | Fact |
| --- | --- |
| Desk required | Telegram is required for `run`. |
| Desk trades | Slash commands place, close, and modify trades. |
| Free text | Free text goes to the configured model. |
| Advice send | Default: advice is staged. `/confirm` sends. `/approve always`: after risk preview, send. Risk can still refuse. |
| Confirm | A staged suggestion waits for `/confirm` (default 120s). `/approve always` skips that wait after a successful risk preview. |
| Approve | `/approve always` sends after risk preview. No `/confirm`. Default off. `/approve off` restores staging. Circuit and `risk_pct` still refuse. Halt still refuses. Paper and demo accept `/approve always` at any time. On `trade_mode=2` without live armed, `/approve always` is refused until `/live on I-ACCEPT-RISK`. Last of `approve_always` / `approve_off` in `journal.jsonl` restores on start. |
| Live from chat | Real-money sends need `live_accepted`. Set it with `--i-accept-risk` at start, or `/live on I-ACCEPT-RISK` in the locked chat. The phrase is required. `/live on` without it is usage. `/live off` clears it. Arming is PER PROCESS and is never restored from the journal: a restart always starts disarmed, and a `live_on` record writes `live_not_restored` instead. Same fuse as `--i-accept-risk`. Risk still sizes and can refuse. |
| Size | Every new order is sized so a full stop-out loses at most `risk_pct` of equity (default 0.5%). |
| Min lot | If the broker minimum lot would exceed that, the trade is skipped. |
| Halt room | A sized order is also measured against what the account may still lose before the daily-loss or drawdown halt. A volume whose full stop-out would carry the account through either halt is refused as `size_exceeds_risk`, before the halt trips. Those budgets come from `journal.equity.json`, which the sizer never reads, so this gate can refuse a size the sizer was content with. A per-trade risk above `daily_loss_pct` refuses every entry. |
| Slippage | `deviation_points` (default 20) is the maximum tolerated slippage on a send, in POINTS, and a point is instrument-specific: 20 points is 2 pips on a 5-digit EURUSD and 20 cents on XAUUSD. `[risk.symbol_deviation_points]` overrides it per symbol, keyed by the name the BROKER uses, case-insensitive and otherwise exact (a venue that calls gold `XAUUSD.m` must be keyed `XAUUSD.m`; a near-miss falls back to the global default). Resolution is symbol first, then the default, and every `open`, `pending` and `close` record in `journal.jsonl` carries `deviation` and `deviation_source` (`symbol` or `default`), so which number applied is readable live and after the fact. Closes resolve per symbol too and are never gated on it. A limit or stop send resolves and transmits it the same way a market send does (issue #92, where it did neither); whether MT4 APPLIES a slippage tolerance to a pending order type is documented as ignored and has NOT been measured on the live rig, so the desk sends the operator's figure rather than assuming either answer. |
| Slippage refused | A new entry whose effective deviation is below `min_deviation_spread_multiple` (default 1.0) times the LIVE spread is refused as `deviation_below_spread` before the order is sent. EVERY new entry, limit and stop included: making the gate skip pending signals was considered and rejected in #92, because it removes a loud wrong answer (a refusal over a tolerance the send did not carry) in favour of a silent one (a send carrying a number nobody on this side configured). A market order crosses the bid/ask gap, so a tolerance smaller than the spread can only be rejected by the venue, intermittently, with nothing in the log naming the cause: measured on the live MT4 rig 2026-09-24, XAUUSD quoted a 45-point spread against the 20-point global default. The refusal names the symbol, the effective deviation, its source, the measured spread, the floor it enforced, and the config key and value to set (headroom included, so fixing it to the exact floor does not fail on the next tick). It never raises the deviation itself: silently overriding an operator's risk number would leave a figure in force that is neither configured nor readable. The spread is read at evaluate time, so this gate is as transient as the market; `spread_too_wide` is applied FIRST, so a temporary blowout is named as a wide spread and only a spread the instrument carries normally is named as a misconfiguration. A spread of zero or less is a broken tick and the gate abstains. `min_deviation_spread_multiple` cannot be set below 1.0: there is no value that switches the gate off. |
| Daily loss | Daily loss of `daily_loss_pct` (default 2%) of start-of-UTC-day equity flattens positions for the bot's magic and halts until the next UTC day. A restart does not clear it. `day_start_equity` is restored from `journal.equity.json` when the UTC day is the same. |
| Drawdown | Drawdown of `max_drawdown_pct` (default 10%) from peak equity flattens and stays halted. `peak_equity` is restored from `journal.equity.json`, so a restart does not clear it. To reset the peak, stop the bot and delete that file. |
| Halt | `HALT` or `/halt` flattens immediately (positions and working orders). |
| Risk state | `journal.equity.json` holds `day_key`, `day_start_equity`, and `peak_equity` next to the journal. It is the INPUT the gates recompute from, never a stored verdict. The write is atomic (temp file, then rename). |
| State unreadable | A snapshot that is corrupt, truncated, or from a newer version halts with reason `state_unreadable`. The file is not changed. This is COULD NOT MEASURE, not a clean start. Inspect it, then delete it to start clean; that also resets the peak. |
| State unwritable | A snapshot that cannot be written halts with reason `state_unwritable`. The next restart would lose the loss budget, so the bot refuses to trade. |
| Flatten proof | A flatten counts what it closed. It reports `requested`, `confirmed_closed`, `closed_elsewhere`, and the survivor tickets, to `journal.jsonl` as `flatten`. A close is confirmed only when the filled volume covers the whole position, so a partial fill (`DONE_PARTIAL`) is residual risk, never a close. An unreadable book is COULD NOT MEASURE, which counts as an incomplete sweep, not a clean one. |
| Flatten failure | A sweep that leaves risk open writes `flatten_incomplete` and alerts `FLATTEN INCOMPLETE: n still open` with the tickets. `notify_events` cannot silence that alert. Survivors are never marked already-seen, so they alert again. The halt still holds: a failed flatten stops new entries. |
| Real money | Real-money accounts (`trade_mode = 2`) refuse orders unless `--i-accept-risk` was passed at start or `/live on I-ACCEPT-RISK` was sent in the locked chat. |
| Fills SSOT | `journal.jsonl` is the source of truth for fills the bot observed. Pending fills write `open` with `fill=true`. Vanished tickets write `close` with `fill=true`. The venue holds the live book. It is not the fill log. |
| Paper default | Paper is the default mode. |
| Alerts | SL/TP hits and pending-order fills emit Telegram alerts even when `/auto` is off. |
| Notify | Default notify events include `open`, `close`, `pending`, and `recap`. `flatten_incomplete` is always sent, whatever `notify_events` says. |
| Notify bound | One logical message becomes at most `MAX_SEND_CHUNKS` (3) Telegram sends of `CHUNK_CHARS` (3900) each. Past that the message is clipped, the clip is named inline to the operator, and a `notify_truncated` row records the dropped character count. A truncation is never silent. |
| History bound | `history_text` renders at most `HISTORY_ROW_CHARS` (1000) characters of any ONE journal row and says `[truncated: N more chars in the journal]` when it clips. The journal FILE keeps the whole row; this bounds only what the chat renders, so one oversized field cannot crowd the other rows out of the message. |
| Recap | A UTC day roll sends a recap notify: equity vs `day_start` and the day's P&L, ONE line, at most once per UTC day. That is not a trade. It carries NO `journal.tail`: a journal row must never store a rendering of other rows, because the renderer re-expands it and the payload compounds daily (straightedge#119). The day's rows are a pull, `/recap`, not a push. |
| Secrets | Secrets belong in the environment: `MT5_LOGIN`, `MT5_PASSWORD`, `MT5_SERVER`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `XAI_API_KEY`, `ANTHROPIC_API_KEY`, `AI_PROVIDER`, `ADVICE_URL`, `ADVICE_TOKEN`, `MT4_MAILBOX_TOKEN`. All but `MT4_MAILBOX_TOKEN` are ALSO accepted from `config.toml` when the variable is unset, and the environment wins when both are set (`config.FILE_SOURCED_SETTINGS`; SECURITY.md has the key table). That is a supported fallback, not a control: `doctor` and `run` print which keys this config took from the file, and the `start` record carries `settings_from_file` plus `settings_read_from_nowhere`, by key name and never by value (straightedge#139). |
| MT4 transport | The MT4 venue has two transports and one ICD (`docs/MT4.md`, decision in `docs/TRANSPORT.md`). Unset `mt4.mailbox_url`: the desk reads MT4's Common Files mailbox itself and must run on the MetaTrader 4 host. Set: the desk speaks HTTPS to `straightedge mt4-shim` on that host and can run anywhere, while the Expert is byte-for-byte the same file and issues no `WebRequest`. `mailbox_url` is checked BEFORE `files_dir`, because `files_dir` auto-resolves on Windows and a url that lost the tie would leave a remote-configured desk reading a local mailbox. The desk is still the initiator either way, so every risk gate stays in the desk's process and on the desk's clock. |
| MT4 transport auth | `MT4_MAILBOX_TOKEN` is required on BOTH ends, is environment-only (there is no TOML key), and must be at least 32 characters. There is no unauthenticated mode and no flag that creates one. The shim checks it BEFORE the path, the method and the body, answers every unauthenticated request with `401` whatever it asked for, and writes nothing to the mailbox when it refuses. `http.server` has no TLS, so the shim binds `127.0.0.1` and refuses a routable address without `--i-understand-plaintext`; the supported exposure is a Cloudflare Tunnel, which opens no inbound port. |
| MT4 transport failure | The partition `Mt4Broker.startup_connect()` depends on survives the network. The shim answers `504` when the Expert did not reply and `503` when the mailbox is unwritable, and the desk turns both back into `BridgeTimeout`, which is retried at startup. Every `BridgeTimeout` NAMES its `transport` (`file` or `net`) and its `phase` (`mailbox`, `connect`, `read`, `shim-mailbox`, `shim-unavailable`), as attributes and in its message, so the five conditions are distinguishable in `journal.jsonl` and not only in prose. On a `504` the refusal body also carries the far end's own `withdrawal` (`withdrawn` / `claimed` / `locked`), so an off-box desk learns whether a timed-out request is provably gone instead of assuming the ambiguous-money case; absent or unrecognised reads as `claimed`, and the key is read on `504` only, because a `503` means nothing was written (#135). Everything else (`401`, `404`, `411`, `413`, `400`, `500`, a mismatched reply id) is a live peer stating a diagnosis, is raised as `RuntimeError`, and is NEVER retried. A read gets `mt4.timeout_ms` and a send gets `mt4.send_timeout_ms`; the desk allows itself 2 seconds more than it tells the far end, so the far end gives up first and the desk learns which. A network partition means the desk cannot flatten; what protects a position in that window is the broker-side stop the Expert attaches on every entry, not the desk. |
| MT4 send timeout | A send that produced no reply is UNRESOLVED, never "failed": the order may be filled, in flight, or never sent. The desk withdraws the request if it still can, states which of `withdrawn` / `claimed` / `locked` it achieved, reads the book once, reports what it matched, and refuses to transmit that order again. It does NOT conclude from an empty book that nothing happened. |
| Agent billing | `AI_PROVIDER=computer` posts to the agent. The agent bills through the gateway (`CF_AIG_TOKEN`), not a provider key. |
| Redact | Journal writes, `loop_error` stderr, and Telegram `send` redact BotFather tokens. Named secret keys in the journal become `[REDACTED]`, on write AND again on `tail()` read, so a row from an older build is redacted too. `login` is one of those keys and `doctor --connect` masks it to the last four instead; see SECURITY.md for why the two differ. |
| File mode | `journal.jsonl`, `journal.jsonl.1`, `journal.tg_offset`, `journal.equity.json`, `journal.lock`, `journal.heartbeat`, and `HALT` are chmod 0600 on Unix. The bot sets umask 077. Windows has no POSIX mode bits; the lock is still exclusive. |
| Sender lock | `TELEGRAM_ALLOW_SENDERS` (or `telegram.allow_senders`) lists the sender ids that may command the desk. Every command is checked, read-only included. A sender that cannot be read is refused. A negative (shared) chat id with an empty list refuses to start. A refusal is journaled as `command_rejected` and is not answered. |
| Currency limit | `max_currency_exposure` (default 2) caps the net count of COMMITMENTS touching any one currency: open positions AND resting working orders, because an order at the broker becomes a position without anyone being asked again. A buy of EURUSD counts +EUR and -USD. The symbol being staged counts toward the check, not just the open book. |
| Symbol classification | Non-alphabetic characters are dropped, and the pair is a BASE code at the start of what remains followed immediately by a QUOTE code, BOTH recognised (ISO 4217, plus the metal codes ISO assigns so `XAUUSD` parses, plus crypto codes so `BTCUSD` parses). A code may be any length the table carries, so `DOGEUSD` is DOGE/USD and `MATICUSD` is MATIC/USD (issue #77; before it the six characters were split 3 and 3 and no longer ticker could resolve). Anything after the quote is a vendor suffix and is ignored: every suffix convention resolves (`EURUSDm`, `EURUSD.a`, `EURUSD_i`, `EURUSDmicro`), and so does a separator inside the pair (`EUR.USD`). A prefix decoration still hides the pair (`mEURUSD`, `FXEURUSD` stay not applicable), because the base is anchored at the first letter. **Ambiguity rule**, when more than one split has both halves recognised: (1) the split that consumes the FEWEST letters wins, so a suffix letter is never absorbed into a code when a shorter reading exists, and every symbol that resolved under 3-and-3 resolves identically; (2) on an equal count, the LONGER base wins. `USDTRY` is USD/TRY, and stays USD/TRY even if `USDT` is ever added, because USDT/RY does not leave a recognised quote. The shipped table is prefix-free (pinned by a test), so today no real symbol reaches the rule. The table CONFIRMS a pair; it never refuses a trade. |
| Limit not applicable | One rule, two outcomes. If either half is not a recognised code, or six alphabetic characters do not exist, the currency limit DOES NOT APPLY: the trade is ALLOWED and the exclusion is recorded in `journal.jsonl` as `currency_limit_not_applicable` with the excluded symbols. This covers an instrument that cannot be a pair (`US30`, `USOIL`, `GER40.cash`), decoration that hides the pair (`FXEURUSD`, `mEURUSD`), and a pair whose code is missing from the table. There is no third state: cannot-tell and is-not-FX get the same treatment, because the honest answer to both is do not pretend to measure, do not block, make it visible. Not applicable is never silent; silence was the original defect. |
| Table completeness | A currency code missing from the table degrades that symbol to allowed-and-recorded, never to refused. Completeness is desirable, not a safety property. The table is ISO 4217, the four metal codes, and the crypto majors (`ADA BCH BNB BTC DOT EOS ETC ETH LTC SOL TRX XBT XLM XMR XRP XTZ ZEC`). The crypto set also carries `AVAX DOGE LINK MATIC SHIB` since issue #77. The selection rule for adding one is written in `currencies.py`: alphabetic and three characters or longer, not a prefix of another code and no code a prefix of it, quoted by a venue as the base of a spot pair, and no collision with ISO 4217. Every code added widens the false-positive surface, so the code set is a decision, not a dump. |
| Crypto exposure | Ruled on issue #66. A crypto code shares the ONE bucket per code that every other code uses: a buy of `BTCUSD` counts +BTC and -USD, so its USD leg is counted identically to the USD leg of `EURUSD` and of `XAUUSD`, and its BTC leg caps `BTCUSD` against `BTCJPY`. Crypto volatility is not FX volatility, and that does not change the answer, because `max_currency_exposure` counts TICKETS and never money: it exists so the book cannot hold several positions that are secretly the same bet, and long BTC, long gold and long EUR are all short USD. Per-unit risk is equalised by per-trade sizing and by the daily-loss and drawdown gates, which do read money. A SEPARATE crypto bucket was rejected: it would let a fourth short-USD ticket in without the FX count seeing it, which is the same silent non-application, just narrower. `BTCUSDT` resolves as BTC/USD, folding a USD-pegged stablecoin leg into USD, which is the intended reading for a correlation count. |
| Exposure unmeasured | `currency_exposure` RAISES for a symbol that is not an FX pair, so the silent skip that was issue #10 cannot be reintroduced by a future caller. `RiskManager.evaluate` classifies first and never passes it one, so the `exposure_unmeasured` refusal is a TRIPWIRE against caller/classifier divergence and cannot fire from any broker symbol. A non-FX position contributes nothing to currency exposure, which is correct rather than an underestimate. |
| Sizer | `RiskManager.evaluate` is the only sizer. It is not optional. |
| Advice snapshot exposure | The snapshot every advice turn carries states net currency exposure per code, the configured `risk.max_currency_exposure` cap, and the ROOM left against it (`cap - abs(net)`, which is what the gate honours since it refuses at `abs(net) > cap`). It is computed by `risk.currency_exposure` over the same committed set `RiskManager.evaluate` nets, our open positions plus our working orders, and never by a second aggregation written for the report: two exposure figures that can disagree is the version-skew defect recorded in #142, and a 3-and-3 symbol split agrees with the table on EURUSD while getting DOGEUSD wrong (#98). Reporting only; no gate reads it and nothing about what the engine permits changes. A symbol the limit does not apply to is listed as `excluded_from_currency_limit` rather than silently absent (the issue #10 decision), and an order book that cannot be read marks the line `INCOMPLETE` rather than printing a total that understates the book by its resting legs. `risk_text` states `room` on `daily_loss` and `drawdown` for the same reason: the system prompt tells the model it has those, so the desk states them instead of leaving the subtraction to a model. |
| Advice whitelist | A symbol the MODEL picked must be in `advice.symbols`, or in `symbols.names` when that is empty. A miss is `reject` with reason `symbol_not_allowed` and stage `advice_symbol`. It gates OPENS only: an advice `close` is never refused for the symbol, because a control that can stop you reducing exposure is not a risk control. A human `/buy` or `/sell` is NOT gated by it; the operator chose that instrument themselves. |
| Daily caps | Two budgets per UTC day, both `0` to disable, both durable beside the journal so a restart cannot hand out a fresh allowance. `risk.max_trades_per_day` counts OPENING sends across auto, telegram and advice, refusing with reason `max_trades_per_day`; closes never count and are never capped. `advice.max_turns_per_day` counts advice turns and refuses with `max_advice_turns_per_day` BEFORE the provider is called, because it bounds a bill and a turn costs money whether or not it ends in an order. |
| Refusal precedence | A rule saying no outranks COULD NOT MEASURE. The whitelist runs before the order is built, so an unlisted symbol reports `symbol_not_allowed` rather than `advice_stage_failed`. |
| Handover posture | `telegram.allow_approve_always` and `telegram.allow_auto` (env: `TELEGRAM_ALLOW_APPROVE_ALWAYS`, `TELEGRAM_ALLOW_AUTO`) gate `/approve always` and `/auto on`. Both default true: unset, behaviour is unchanged. Set false, the command is refused with a named reason (`approve_always_disabled`, `auto_disabled`), journaled as `reject` (`source=telegram`), and never answered in chat. A value that is present but not a clean boolean is read as false, never as the default: a bad env var or a config typo can only remove the capability, never grant it. `/approve off` and `/auto off` are never refused. `config.handover.toml` sets both false. The default stays true on purpose (flipping it would silently change every existing deployment); the gap that leaves is closed by observability, not by a stricter default: `doctor` and `run` print the posture, and every `start` journal record carries `approve_always_allowed` / `auto_allowed`, so a session's posture is readable both live and after the fact. |
| Refusal event naming | `command_rejected` (PR #40) answers WHO: a sender that is not on the allow-list. `reject` (PR #49, and the handover posture above) answers WHAT: a specific command that this deployment's policy or risk state does not permit, regardless of who sent it. Do not merge the two event names or re-litigate this split per issue; a refusal is either an identity question or a policy question, never both at once. |
| Refusal record | Every gate that refuses writes `reject` to `journal.jsonl` with the NAMED `reason`, plus `source` (`auto`, `telegram`, `advice`) and `stage` (which gate, on which leg). The auto, desk, and advice paths share that one event name. A refusal is journaled and is never broadcast to the chat that triggered it. With no journal configured it still prints to stderr. |
| Not-advice line | Every reply out of the advice path opens with `telegram.NOT_ADVICE` as its FIRST line, including replies that refuse to stage. First and not last: `send()` truncates from the END past `MAX_SEND_CHUNKS` x `CHUNK_CHARS`, and a model's reply has no length bound, so a footer would go missing on the longest replies. An operator-typed `/buy` or `/sell` does NOT carry it; nothing advised that trade. |
| Advice record | An advice turn writes `advice_turn`: `provider`, `session`, `action`, `symbol`, `sl`, `tp`, `limit`, `stop`, `ticket`, `staged`. The question and the reply are never written to `journal.jsonl`; `journal.advice.json` keeps the last 40 turns and the agent workspace `log.md` keeps the 40 most recent entries within the read tool caps (`docs/DATA.md`). A suggestion the circuit refuses to stage writes `advice_circuit_block` with the circuit `reason`. |
| Unmeasured is not refused | An advice action that could not be turned into an order at all writes `advice_stage_failed` with `measured=false`, never `reject`. COULD NOT MEASURE stays distinct from REFUSED. |
| Auto arming | `/auto on` and `/auto off` write `auto_on` and `auto_off`, the audit trail `/live` and `/approve` already had. |

## Forbidden claims

| Claim | Fact |
| --- | --- |
| Returns | Do not claim consistent positive returns. |
| Blind follow | Do not claim that Grok or Claude is a signal you must follow blindly. |
| Paper equals live | Paper P/L is not live P/L. |
| Paper fill | Paper fills at bid/ask. |
| Same bar | Same-bar SL and TP: SL wins. |
| Paper pending | Paper pending limit/stop fills on tick (bid/ask vs price) or bar (high/low vs price). |

## Modes

| Mode | Orders | Data |
| --- | --- | --- |
| `paper` | in-process PaperBroker | synthetic, `--feed-mt5`, or empty |
| `mt5` | terminal `order_send` | live terminal |
| `mt4` | Expert mailbox (`docs/MT4.md`) | live terminal + `Mt4RiskBot.mq4` |

## Telegram commands

| Command | Effect |
| --- | --- |
| `/quote [SYMBOL]` | Show one symbol. Omit SYMBOL to show all configured symbols. |
| `/risk` | Show daily-loss and drawdown room vs caps. |
| `/buy` `/sell` SYMBOL `[sl=] [tp=] [limit=PRICE] [stop=PRICE]` | Stage a market order, or a working limit/stop. Do not set both limit and stop. |
| `/confirm` | Market: reprice to the live tick, preview, send. Limit/stop: preview at the staged price, send. |
| `/approve always\|off` | always: after risk preview, send. No `/confirm`. Default off. |
| `/live on I-ACCEPT-RISK\|off` | Arm or disarm real-money sends from chat. Phrase required. Same fuse as `--i-accept-risk`. |
| `/cancel` | Drop the staged confirm. |
| `/cancel TICKET` | Cancel a working order. |
| `/replace TICKET PRICE` | Move a working order entry. Uses `TRADE_ACTION_MODIFY`. The circuit still refuses, and the replacement is measured against the per-trade cap AND the halt room, the same pair a new order is measured against (#104). **A strictly risk-REDUCING replacement is always allowed (#164):** when the proposed worst-case loss is at or below what is already resting, the cap is not consulted, which mirrors `_stop_guard`'s early return on a tightening and its documented asymmetry. A cap exists to bound risk, so refusing the one action that unconditionally lowers it inverted the cap's purpose: measured on #164, a $50.00 resting order, a $40.00 proposed replacement and $13.80 of room was refused, leaving `/cancel` as the only move and MORE risk resting than allowing the reduction would. Only the direction changes the answer; the threshold for an INCREASE is unchanged, which is what keeps #104 closed. **An unmeasured spec refuses before any of this arithmetic runs (#161),** with reason `spec_not_measured` naming the fields: `ticks_between` returns 0.0 on a spec the venue never streamed, so worst-case loss computed to 0.0 and every replacement passed the size guard and was sent. That refusal is also what makes the reduction comparison meaningful, since on an unmeasured spec both sides of it are 0.0. |
| `/orders` | List working orders. |
| `/close TICKET\|SYMBOL\|all [VOL]` | Flatten or partial close. **A VOL that is not a finite number above zero is REFUSED as `volume_unusable:<value>` (#210),** which covers `nan`, `inf`, `-inf`, zero and negatives under one reason word with the value carrying the diagnosis. One word rather than the two `/sl` and `/replace` use for stops, because a close volume has a single source: the operator types it, so every kind calls for the same action. The finiteness half is not cosmetic and is the reason this row exists: `nan <= 0` is False, so the magnitude guard alone passed `nan` to the partial-close arithmetic, which ran `balance += pnl * (nan / volume)` and wrote `nan` to both the account balance and the position volume while answering `closed`. That is a SUCCESS reported for a close that did not happen, and because every circuit gate reads equity, a `nan` equity compares False against the daily-loss and drawdown bounds from then on, so the bad input disables the gates meant to catch the next one. The paper adapter refuses a non-finite close volume on its own as well, with `TRADE_RETCODE_INVALID_VOLUME`; that layer is unreachable while the desk guard stands, and is kept because the corruption it prevents is irreversible for the rest of the session. |
| `/closeby TICKET OTHER` | Hedge-account only. `TRADE_ACTION_CLOSE_BY` offsets two opposite tickets. Same symbol, opposite sides. Remainder 0 or at least `volume_min`. Not a new send. Netting terminals refuse CLOSE_BY. Paper always hedges. |
| `/reverse TICKET [sl=] [tp=]` | Two market sends: close the ticket, then the opposite side. `/confirm` is the send. Stage and preview exclude that ticket. Mirrors SL/TP distances if omitted. The circuit and `risk_pct` still refuse. After flatten they can leave you flat. |
| `/sl` TICKET PRICE | Modify a position or a working order. Success only if the broker applied it. On a PROTECTED open position: tightening always applies, including when the circuit has tripped. A value at or below zero, and any non-finite value (`nan`, `inf`), are REFUSED as `stop_removal_refused`, because `/sl` sets a stop to a price and `0` is the venue encoding for "no stop" while `nan` compares False against every bound and so is no stop either. Widening is capped at `risk_pct * max_risk_multiple` of equity AND the remaining loss room, else `stop_exceeds_risk`; a stop AT or BEYOND breakeven is not a widening and is not capped, because there is no loss left to bound. A widening on a symbol whose sizing fields are unmeasured is refused as `spec_not_measured:<fields>`, since the cap cannot be computed without them; a TIGHTENING on such a symbol still applies, so risk can always be reduced. On an UNPROTECTED position (no stop) any finite price is allowed, including a no-op zero, because nothing can be un-protected twice and this is also the path `/tp` takes. Adding a stop to a position that has none is always allowed. Refusals are journaled as `modify_refused`. |
| `/tp` TICKET PRICE `[VOL]` | Full TP, or scale-out VOL at PRICE (partial close when hit). The circuit still refuses. **A VOL that is not a finite number above zero is REFUSED as `volume_unusable:<value>`, the same reason word `/close` gives (#210).** Before that refusal existed this path did not merely misreport: `normalize_volume` calls `math.floor`, which raises ValueError on `nan` but **OverflowError on `inf`**, and OverflowError derives from ArithmeticError rather than ValueError, so it was not in the `(ValueError, RuntimeError, OSError)` tuple the command handler and the poll loop catch. `/tp TICKET PX inf` therefore left the handler by an uncaught exception, and `nan` surfaced the interpreter's `cannot convert float NaN to integer` where a refusal belongs. The check sits immediately before the arithmetic, not at the top of the command, so no already-reachable refusal changes precedence. |
| `/be TICKET` | Move SL to entry. Never loosen. |
| `/trail on\|off\|TICKET` | on: `manage()` existing positions every tick. No EMA entries. Default off. TICKET: one-shot. Never loosen. |
| `/history` | Last journal events. |
| `/recap` | Equity vs UTC `day_start` plus `journal.tail`. The UTC day roll sends the first half only (see Recap above). |
| `/symbols list\|add\|remove [SYMBOL]` | Configured book (runtime). Bare `/symbols` lists. Cannot drop the last name, or a name with positions/orders. |
| `/ask ...` or free text | Grok, Claude, or the agent. Local: last 40 turns in `journal.advice.json`. `AI_PROVIDER=computer`: Durable Object SQLite workspace (`/workspace/notes.md`, `log.md`, `snapshot.md`, `history.json` from `journal.tail`) plus Computer tools. `log.md` keeps the 40 most recent entries within the read tool's own byte and line caps, the same window as `journal.advice.json`, and names any drop on its first line (straightedge#131, `docs/DATA.md`). Session is the Telegram chat id. JSON can stage. Default send is `/confirm`. `/approve always` sends after risk preview. Not Cloudflare D1. |
| `/model grok\|claude\|computer` | Switch provider. |
| `/auto on\|off` | Optional EMA regime. Fill alerts do not wait for this. Journaled as `auto_on` / `auto_off`. |
| `/status` `/positions` `/halt` `/resume` | Account. `/halt` flattens, drops the confirm, and cancels working orders. |

## Confirm and send

`/confirm` for a market order reprices and re-runs `preview`.
A limit or stop keeps the staged price.
Halt, daily-loss, and drawdown still refuse.
The reply includes `ok` and `retcode`.
Only `OrderResult.ok` starts with `sent `.
A second `/buy` while a confirm is live is refused until `/cancel`.
Advice never overwrites a live confirm.
Close and SL/TP success replies come from `OrderResult.ok`.
They do not come from "the ticket existed".
`/sl` `/tp` on a working order uses `TRADE_ACTION_MODIFY` (paper supported).
Side geometry is kept (`buy: sl < price < tp`).
`/tp TICKET PRICE VOL` is a scale-out.
When PRICE is hit, only VOL closes.
Remainder keeps its SL.
Halt, daily-loss, and drawdown still refuse.
VOL must snap to lot step.
Remainder must be 0 or at least `volume_min`.

### One staged order, at most one transmission

A staged order carries a client order id, minted once at stage time, kept on the
`confirm_stage` journal record and therefore unchanged across a `/confirm` that
timed out and across a desk restart in between.

A record of the attempt is written to `<journal stem>.inflight.json` BEFORE the
send and is cleared only by a VERDICT: success, or a venue rejection. A send whose
key already has an open record is REFUSED and nothing is transmitted.

A timeout is not evidence the order did not reach the broker, and an empty book is
not evidence either: the desk's budget can expire while the Expert is still inside
its retry ladder, so the position the send is about to create is not visible yet.
The desk therefore refuses rather than reconciling itself to a conclusion. The
reply says nothing was transmitted and names the key; `/cancel` and re-stage is the
operator's move, after checking the terminal.

A CLOSE is not guarded this way and does not need to be: the ticket is already the
key at the venue, so closing #N twice fails the second time. An OPEN has no
equivalent, because nothing on the MT4 side can tell two identical `OrderSend`
calls apart.

Journal events: `send_unresolved` (a send answered nothing; carries the key, what
the mailbox did with the request, and any position whose comment matched),
`send_refused_unresolved` (a second send for the same key was refused),
`confirm_unresolved` (the chat path's record of the same), `inflight_unreadable`
(the ledger file exists and could not be parsed, which must never read as "no open
sends"). `Engine.start()` re-announces every open record on EVERY start.

## Advice

Advice JSON fields: `action`, `symbol`, `sl`, `tp`, `limit`, `stop`, `ticket`, `summary`.
`limit` and `stop` are XOR.
A close action with `ticket` stages that close.

**The `claude` provider constrains the reply to a schema (straightedge#180).** The request sends
`output_config.format` as a JSON schema whose `action` is an enum over the four actions above, with
`additionalProperties: false` and every field required. The reply is therefore ONE object of NINE
fields: the eight listed above plus `text`, which is the prose the operator reads. `text` exists
because a schema-constrained reply has no room for prose outside the object, and `Advice.text` is
what the desk renders.

The schema is a second gate, never a replacement. `parse_advice` still produces every `Advice`, and
the two read one pinned action vocabulary so they cannot disagree about what an action is. A reply
that is not a schema object, which is what a proxy dropping `output_config` would produce, parses
exactly as before: that is the degrade, and it is also what `grok` and `computer` use permanently,
since neither can constrain output.

A reply that VIOLATES the schema is treated as evidence the constraint did not apply, because it is
applied by a server the desk does not run: the action is forced to `hold` and the reason is STATED
in the prose. Not silently coerced, which is the distinction the Refusal record and Unmeasured is
not refused rows require of every other gate.

**A brace in `symbol` is a violation, and the symbol is never repaired.** `_JSON_TAIL` cannot match
an object with a brace inside a string value, so such a reply used to fall back to `hold` with no
symbol. `summary` is a label nobody trades on, so braces there are stripped to keep the object
parseable. `symbol` NAMES THE INSTRUMENT: stripping braces there manufactures a different, tradeable
symbol, and since the desk gates a model-chosen symbol on `advice.symbols`, `EUR{USD}` fails that
gate loudly while `EURUSD` passes it. Repairing it would turn a named `symbol_not_allowed` refusal
into a staged order on an instrument the model never named, so a braced symbol forces the hold and
is reported as `null`.

**A model-chosen symbol is checked BEFORE it is transformed, and a non-ASCII one is refused.** Same
rule as the brace, one character further: `"EURU\u017fD".upper()` is `"EURUSD"`, because Unicode
uppercasing maps U+017F LATIN SMALL LETTER LONG S onto ASCII `S`, so uppercasing a model-chosen name
can MANUFACTURE a tradeable instrument the model never named. The ligatures `ff`, `fi`, `st`, the
dotless `i` and `ss` (which expands to `SS`) do the same. So the name must already be ASCII:
`advice_allows` refuses anything else, `parse_advice` leaves it exactly as sent rather than
uppercasing it, the structured path reports `symbol ... is not ASCII` as a violation and holds, and
the refusal NAMES the string the model sent rather than the one it uppercases to. `eurusd` still
works, because an ASCII case fold is the same instrument. The operator's own whitelist is held to
the same rule: a non-ASCII entry matches nothing rather than widening the list to a symbol nobody
typed (straightedge#197).
Default send is `/confirm`. `/approve always` sends after risk preview.
`/approve always` is available in paper and demo without a live fuse.
On `trade_mode=2`, arm live first (`--i-accept-risk` or `/live on I-ACCEPT-RISK`).
Context always includes `/risk`, positions, working orders, and quotes.
If the next order would trip the circuit, context says hold/close only.
Buy/sell is not staged.

Buy limit must be below ask.
Sell limit must be above bid.
Buy stop must be above ask.
Sell stop must be below bid.
`limit=` and `stop=` together are refused.
`/replace TICKET PRICE` keeps that geometry and existing SL/TP.
It does not send a new order.

## Reverse

`/reverse TICKET` stages a close plus the opposite market.
`/confirm` is two market sends, not one.
First it closes that ticket.
Then it sends an opposite market deal.
Default SL/TP mirror the open trade's distances around the live bid/ask.
Risk sizes the new side independently.
Staging and the first confirm preview exclude that ticket so `already_in_symbol` does not block.
Halt, daily-loss, drawdown, and `risk_pct` still refuse.
The ticket stays open if they refuse before the close send.
After the close send succeeds, preview runs again on the live book.
Realized P/L can trip the circuit.
It can also leave no room for `risk_pct`.
The reply is `closed #TICKET; reverse refused: ...`.
There is no opposite position then.
A failed opposite send is `closed #TICKET; send failed ...`.
Reverse is for open positions, not working orders.

## Close-by

`/closeby TICKET OTHER` is a flatten, not a new order.
Both tickets must use the bot's magic (20260909).
They must be the same symbol and opposite sides.
Overlap volume closes.
The larger side keeps the remainder.
Same ticket, same side, or a leftover below `volume_min` is refused.
Live CLOSE_BY is hedge-account only.
A netting terminal refuses it (one net position per symbol, no opposite ticket).
Paper always hedges: each deal is its own ticket.
Close-by works in paper even when a live netting account would not.
Paper P/L is not live P/L.

## Loop, fills, trail, session

Each loop tick resolves pending fills and SL/TP even when `/auto` is off.
Pending fills notify as `FILL/OPEN`.
SL/TP hits notify as `CLOSE` with `reason=sl` or `reason=tp`.
`/trail on` runs `manage()` on open positions every `step_all` tick.
It does not enable EMA entries.
`/auto on` still owns entries.
Trail default is off.

Manual `/buy` `/sell` skip the session window.
Auto does not.

### One clock: the UTC day, and the venue offset is measured

Every boundary in this file is the UTC day and the UTC wall clock: the session
window, `skip_friday_after_utc`, the Saturday/Sunday block, `day_key`, the
daily-loss budget, the two daily caps and the recap. There is no broker day
anywhere in the contract.

That had to be DECIDED rather than assumed, because before straightedge#172 it
was accidentally both. MT4 and MT5 stamp bars with the broker server's own wall
clock, the auto leg built its instant from the last bar's stamp and labelled it
UTC without converting it, and the desk and recap paths used the bot's clock.
One daily-loss budget had two different day boundaries, and which one applied
depended on whether a human or the regime fired the trade. On the live UTC+3
server the auto leg ran three hours early, which moved a configured 07:00-17:00
window to 04:00-14:00 and let a `daily_loss` halt release three hours before
the UTC day it was measured in had ended.

The UTC day is the boundary, for three reasons. The operator's config is
written in UTC (`start_utc`, `end_utc`, `skip_friday_after_utc`), so a budget
on any other boundary is a budget the operator cannot see. The desk, the recap
and the persisted `day_key` already roll on it. And a broker day is a
per-server, DST-varying property, so keying the money budget to it would key it
to something that moves without anyone editing anything.

**The window is exact only to within the venue clock's measurement
uncertainty.** A terminal whose own clock is drifted stamps its bars and its
ticks identically, so the drift cancels from the measured difference and the
grid snap then takes it out of the offset while the bar stamp keeps it: the
instant every gate sees moves by that drift. The residual check caps it at the
sample's uncertainty, which `2u < VENUE_CLOCK_GRID_SEC` (`constants.py`) holds
strictly under half a grid step, so a configured 17:00 close is soft by at most
that, in the direction of staying armed slightly longer. On the desk's own
path the uncertainty is the MEASURED gap since its previous poll, seconds in a
live loop, and anything beyond it refuses rather than sliding: measured through
a real engine, a 90s drift against a 15s bound refuses, while the same 90s
against a declared 180s bound moved a 17:00:00 instant to 16:58:30 and the
session gate did not fire. The number has one home, `VENUE_CLOCK_GRID_SEC`;
nothing here restates it.

A bar's timestamp is therefore CONVERTED to UTC at the one seam where it enters
the engine, using an offset measured off the venue (`docs/VENUE.md`, "The
venue's clock is not UTC"). When that offset cannot be measured the auto leg
REFUSES with `venue_clock_unmeasured` rather than assuming UTC. Manual commands
are unaffected: they time themselves off the bot's clock and never off a bar.

## Loop survival

`run --loop` retries Telegram HTTP 429 and 5xx with backoff.
It resumes `getUpdates` at the same offset.
That offset is written next to the journal (`journal.tg_offset`).
The write happens after each update is handled or skipped.
A restart does not replay or drop commands.
A dropped MT5 IPC calls `initialize` again.
One failed poll, send, or broker tick is journaled (`reconnect` or `loop_error`).
The bot stays up.
A `reconnect` row NAMES what triggered it (`cause`, `cause_type`, and
`cause_op` / `cause_transport` / `cause_phase` / `cause_withdrawal` when the
venue reports them). `error` on that row is the reconnect ATTEMPT failing and is
a different fault from `cause`.
A reconnect that came back without all its symbols carries `unselected`.
A reconnect that reported `ok=true` whose account is still unreadable is
journaled as `account_read_failed`, not as a silent skipped tick.
Two `run --loop` processes cannot share a journal.
`run` takes an exclusive lock on `journal.lock` (same stem as `journal_path`).
Unix: `flock`. Windows: `msvcrt.locking`. Same fail (`already running`, exit 2).
A second `run --loop` prints `already running` to stderr and exits non-zero.
The lock is released on exit or crash.
Each `step_all` that reaches `account` writes `journal.heartbeat`.
Line 1 is an ISO timestamp, and that has not changed since 1.0.0.
After it, one `key=value` per line: `blocked=`, `mode=`, `stale_after_s=`,
`tick_budget_s=`, `tick_gap_max_s=`, `over_budget=`, `tick_gap_ever_s=`,
`over_budget_ever=`, `run_id=`, `started_at=`, `deployed=`.
(`deployed=` was shipped by 1.6.0 and this list did not name it; corrected
here rather than left for a reader to find in the renderer.)
`run_id=` is one value per desk PROCESS, assigned at construction and never
reassigned, and `started_at=` is when that process started.
A reader compares `run_id` across observations to see a RESTART. It is not the
PID, which the operating system recycles, and not the timestamp, which two
restarts inside one clock tick would collapse.
A reader that finds no `run_id` says so and reports that a restart cannot be
observed from the file; it does NOT read the absence as continuity.
`blocked=` is the reason `RiskManager.circuit_reason` gave for the same account
at the same instant, and it is EMPTY when the desk would trade. It is not a
second copy of the gate; it is the gate's own answer, so a heartbeat cannot
claim the desk is armed when a send would be refused.
`stale_after_s` is derived from `engine.poll_seconds`, the Telegram retry
ceiling and the venue `timeout_ms` of the mode in use. A reader that finds it
missing or unreadable reports UNKNOWN and refuses to invent a threshold.
`over_budget=1` means an observed gap between ticks exceeded the derived budget.
The desk reports that and does NOT widen its own threshold.
TWO PAIRS, TWO QUESTIONS, and a reader must not substitute one for the other.
`tick_gap_max_s` and `over_budget` are THIS PROCESS: the largest gap this desk
process has observed. A restart resets them, and that is correct, because a
desk whose book has shrunk has to be able to report a clean budget again.
`tick_gap_ever_s` and `over_budget_ever` are THIS BOX: the largest gap carried
forward across restarts. A restart does NOT reset them, because what a breach
tests is whether the derived allowance is adequate for this BOOK, and a restart
changes neither the allowance nor the book.
`over_budget_ever=1` with `over_budget=0` means this box breached before the
last restart. It is not a stale reading; it is the one a restart used to erase.
`tick_gap_ever_s` is a maximum over the heartbeats that SURVIVED, and it is
never lower than what the live process has itself observed. Deleting
`journal.heartbeat` resets it, and that is the only way to lose the box history.
A desk too old to publish `over_budget_ever` leaves the box history UNKNOWN. A
reader says so and does NOT read the missing field as a clean history, the same
rule this file already states for a missing `stale_after_s`.
Every breach also appends a `tick_gap_breach` journal record carrying `gap_s`,
`budget_s`, `symbols` and `run_id`, once per process per breach. The heartbeat
holds the worst gap; the journal is what can answer how OFTEN. The journal is
not the store for the maximum: rotation is one generation and `tail` reads only
the live file, so a maximum recovered by scanning it would be a lower bound.
The file is chmod 0600, atomic replace.
A failed reconnect does not.
`straightedge watch` reads the file. It exits 0 for `ALIVE ARMED`, 3 for
`ALIVE NOT TRADING` (with the gate named), 4 for `STALE`, and 5 for `UNKNOWN`.
It never calls `getUpdates` and never takes `journal.lock`.
`watch --loop` announces a RESTART whenever `run_id` changes, whatever the
state, and carries a count so a crash loop is not one line. A restart is not a
change of STATE: on a demo account nothing needs arming, so without `run_id`
every field reads the same either side of a crash.
It names a crash loop only when the time between two processes' `started_at`
stamps, which is an upper bound on the previous run's life, is under
`stale_after_s`.
Before a journal write that would exceed 10 MiB, the live file is renamed to `<name>.1`.
That replaces any previous `.1`.
The new live file is chmod 0600.
`tail` and `last_event` (confirm restore) read only the live file.

## Confirm restore

A staged `/confirm` is journaled (`confirm_stage`).
`start` restores it if the last of `confirm_stage` / `confirm_cancel` / `confirm_sent` is still `confirm_stage`.
The TTL must not have expired.
`start` restores `/approve always` if the last of `approve_always` / `approve_off` is `approve_always`.
`start` does NOT restore `live_accepted`. Arming is per process. A `live_on` record makes `start` write `live_not_restored`, and `/live` says so. Re-arm with `/live on I-ACCEPT-RISK`.
`start` restores `day_key`, `day_start_equity`, and `peak_equity` from `journal.equity.json`.

## Doctor

`doctor` pings Telegram when the token is set.
It always runs an in-process paper `/buy` `/confirm` `/close`.
No live terminal is required.
`--connect` is the optional venue login check.
MT5: binding, login, `trade_mode`. Non-zero if the binding is missing or login fails.
MT4: mailbox ping plus `account` (`account.mode=mt4`, Expert attached).
`mt4.files_dir` / `MT4_FILES_DIR` is Common Files. On Windows, empty means
`%APPDATA%\\MetaQuotes\\Terminal\\Common\\Files`.
A traceback is not a pass.
Production live is `doctor --connect` then `run --mode mt5` or `run --mode mt4`.
`trade_mode=2` still needs `--i-accept-risk` at start, or `/live on I-ACCEPT-RISK` in the locked chat.

NOTE
Put `--config` before the subcommand.

## Gate

`pytest` with `--cov-fail-under=80`.
Required check names are `ci`, `coverage`, `CodeQL`. `ci` is an aggregator:
it needs the python matrix (`ci-matrix`, ubuntu-latest and windows-latest x
3.12/3.13) plus `agent-typecheck` and `agent-test`, and fails if any of them
did not succeed -- a matrix or agent job failing has teeth at the merge gate
without the org ruleset listing every leg by name.
`tests/` must stay green.
`agent/` must stay green: `npm run typecheck` and `npm test` in `agent/`.
`agent/` tests run in workerd via `@cloudflare/vitest-pool-workers`, not node.
The AI Gateway is the only hop the `agent/` suite replaces; it does so at the
outbound-request boundary, so nothing in `agent/src/` is stubbed.
