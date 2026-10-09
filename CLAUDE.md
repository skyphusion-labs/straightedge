# straightedge

Risk-first MT4/MT5 trading desk, operated from Telegram, with an AI advice layer and a risk
engine that sizes and can refuse orders. Public MIT repo (`skyphusion-labs/straightedge`), the
revenue lane: real money, an end user demoing it live. Paper mode is the default; live trading
needs explicit gates (below).

## Names (the repo's own vocabulary, see README)

The bot = the Python process (`src/straightedge/`). The desk = Telegram chat commands. The
agent = the Cloudflare Worker AI advisor (`agent/`, `AI_PROVIDER=computer`, routed through the
AI Gateway named in README's Names table; `tests/test_venue_vocabulary.py` pins that literal
gateway id, so this file points at README rather than restating it). `grok` and `claude` are
the other two `AI_PROVIDER` values. `grok` is BYOK straight to `api.x.ai`. **`claude` routes
by URL, not by a mode flag:** point `advice.claude_url` at a Cloudflare AI Gateway
(`gateway.ai.cloudflare.com/...`) and it authenticates with `cf-aig-authorization` and sends
no Anthropic key at all, because Unified Billing supplies the provider credential; leave it at
`api.anthropic.com` and it stays BYOK with `x-api-key`. One credential field, `claude_key`,
carries whichever token the URL implies. The URL decides so there is no second setting that
can disagree with it, and a self-hoster with their own Anthropic key and no Cloudflare account
keeps the original behaviour by changing nothing.

The circuit = halt, daily-loss, and drawdown gates.

## Broker layer

`src/straightedge/broker/` is one interface, three implementations: `mt4_live.py`,
`mt5_live.py`, `paper.py`. `docs/VENUE.md` is the provider-agnostic execution API
(`MarketOrder`, `WorkingOrder`) all three implement; `docs/CONTRACT.md` is the behaviour the
test suite enforces.

**MT5** needs a live terminal; README's Live MT5 section has the Windows/macOS package
choice (`tests/test_venue_vocabulary.py` pins those exact package names, so this file points
at README rather than restating them).

**The MT4 EXPERT is a file mailbox, not a network call, and that is still true.**
`mt4/Experts/Mt4RiskBot.mq4` talks only through `FileOpen` / `FileWriteString` /
`FileDelete` on the two mailbox basenames `docs/MT4.md` names as the ICD (that doc, and
`tests/test_venue_vocabulary.py`, pin the exact filenames; this file points at the ICD
rather than restating them). Zero `WebRequest`, zero sockets, and **nothing is going to
add any**: the Expert is the only artifact a customer installs, so its interface is the
most expensive thing here to change.

**The BOT no longer has to be on that Windows box.** `straightedge#73` is ruled (Conrad,
2026-09-25, "before the first paying customer") and shipped; `docs/TRANSPORT.md` is the
decision record and it carries the failure table. `mt4.mailbox_url` selects between two
transports behind the one `Mt4Broker` `call` seam: unset is the file mailbox and the bot
must be co-resident, set means the bot speaks HTTPS to `straightedge mt4-shim` running
beside the terminal. The desk is still the INITIATOR either way, which is what keeps
halt, daily-loss, drawdown and the live gates in the bot's process and on the bot's
clock. **Do not re-propose the EA calling `WebRequest` for its own decisions**: it is
synchronous, so it would block the chart thread on our latency, and it would move those
gates behind the customer's polling. Making the EA a dumb TRANSPORT client is the
deferred destination, and `docs/TRANSPORT.md` lists what it needs first. **The old
blocker ("no CI runner has an MQL4 compiler") is FALSE as of 2026-09-26**:
`.github/workflows/mt4-compile.yml` installs MetaEditor on a GitHub-hosted
`windows-latest` runner and compiles the Expert, so a broken `.mq4` now reds a PR
(straightedge#86). That closes the BUILD half only. A clean compile says nothing about
whether a `WebRequest` loop is correct, and compiling is not loading, so the transport
decision in `docs/TRANSPORT.md` is not re-opened by it.

`mt4_live.py` already carries retry logic for NTFS refusing to unlink a file the terminal
still holds open; that is a filesystem race, not a bug to silently work around further,
and sharing Common Files over SMB is specifically NOT the way to move the bot off the box.

## The per-symbol trap

`SymbolSpec` (`src/straightedge/models.py`) carries `point`, `trade_tick_size`,
`trade_tick_value`, and `trade_contract_size` per symbol, and sizing (`sizing.py`) is driven
entirely from those measured fields, never a hardcoded constant. That matters because
instruments differ by orders of magnitude on exactly these fields: gold (XAUUSD) measured at
`point 0.01`, `contract_size 100` (oz/lot) vs. a typical 5-digit FX pair at `point 0.00001`,
`contract_size 100000`. A constant that is instrument-blind silently mis-sizes by that ratio.
**straightedge#68** measured this for gold and found `deviation_points` (max tolerated
slippage) was one such instrument-blind constant: 20 points is generous slippage on EURUSD and
was $0.20 against gold's ~$0.45 spread, causing silent order rejects. Fix in progress; the
per-symbol override lives at `[risk.symbol_deviation_points]` in `config.example.toml`
(currently `XAUUSD`/`XAGUSD` only). Adding an index CFD (SP500, Nasdaq) needs its own entry
here and in any other instrument-scaled constant; do not assume FX defaults generalize.

**Unmeasured specs refuse, they never default.** If a broker has not streamed a symbol's specs
(e.g. it is not in MT4 Market Watch yet), the adapter records the field as `unmeasured` and
sizing returns 0 lots rather than falling back to a plausible-looking default. Never "fix" a
zero-lot trade by supplying a default in this path; that is the exact defect straightedge#68
found and re-broke (see `broker/mt4_live.py:294` for the history).

## Safety posture

- `risk_pct` is an operator-set percentage of equity per trade (`config.toml [risk]`,
  `risk_pct = 0.005` in the example). Daily-loss and max-drawdown percentages flatten and halt.
- Approval gate: `/confirm` is the default send for every staged order; `/approve always`
  skips it after a risk preview. Both require the desk to have staged the order first; nothing
  reaches the venue unstaged.
- `auto` (EMA regime entries) and `mode = "mt4"/"mt5"` (live) are separate gates, both off by
  default (`account.mode = "paper"`, `desk.auto = false`). A real account additionally needs
  `--i-accept-risk` at start or `/live on I-ACCEPT-RISK` (the exact phrase) in the locked chat.

## Running it

`pip install -e ".[dev]"`, then `pytest` and `python -m straightedge doctor`; do not start a
live loop until `doctor` exits 0. CI (`.github/workflows/ci.yml`) matrixes `ubuntu-latest` /
`windows-latest` x Python 3.12/3.13, all GitHub-hosted. `docs/RUNBOOK.md` covers paper vs.
live, the launchd unit, HALT, confirm-on-restart, and journal rotation; `SECURITY.md` covers
secret handling and file permissions (journal/lock/heartbeat/HALT are chmod 0600, process
umask 077).
