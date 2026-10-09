# Venue API

The bot is the Python process on this computer.
A venue is an execution provider.
Paper, MetaTrader 5, and MetaTrader 4 are the venues today.

Engine, desk, and risk never send MetaTrader request dicts.
They send `MarketOrder` and `WorkingOrder`.
Each venue adapter maps those types to its own API.

Paper, MT5, and MT4 implement `straightedge.broker.base.Broker`.
`account.mode` selects the adapter (`paper`, `mt5`, or `mt4`) in `run`.
`broker_for(cfg)` in `straightedge.broker` returns PaperBroker, Mt5Broker, or Mt4Broker.
A new venue is a new adapter plus a factory branch.
Do not teach the engine MT5 constants.

MT4 has no official Python package.
`Mt4Broker` speaks a line mailbox to `mt4/Experts/Mt4RiskBot.mq4`.
See `docs/MT4.md`.

## Types

`MarketOrder` is an immediate buy or sell.
Fields: `symbol`, `side`, `volume`, `sl`, `tp`, `comment`, `magic`, `deviation`, `ticket`.

`WorkingOrder` is a limit or a stop.
Fields: `symbol`, `side`, `kind`, `volume`, `price`, `sl`, `tp`, `comment`, `magic`,
`deviation`, `ticket`.
`kind` is `"limit"` or `"stop"`.

`PendingOrder` is a working order the venue holds.
`kind` is `"limit"` or `"stop"`. Engine never reads MT5 type integers.

`Side` is `buy` or `sell`. MT5 order type integers live in the adapters.

`Bar.time` and `Tick.time` are the BROKER SERVER's wall clock, never UTC.
See "The venue's clock is not UTC" below before comparing one to anything.

`OrderResult.ok` is true when `retcode` is in `RETCODE_OK`.
Only `OrderResult.ok` is a send.
Engine uses `OrderResult.unchanged` and `OrderResult.invalid_stops`.
It does not import MT5 retcode integers.

`RETCODE_OK` includes `DONE_PARTIAL`, so `ok` means the send was accepted, not
that it filled in full. A caller that needs the whole volume gone compares
`OrderResult.volume` against the volume it asked for. `Engine.flatten` does
exactly that, and `FlattenReport` carries the result. Read `pos.volume` before
the close, never after: `Position` is mutable and `PaperBroker` returns live
references, so a partial close rewrites it in place.

`OrderResult.measured` is false when the venue call produced no result at all.
An adapter that gets nothing back MUST return `OrderResult.unknown(...)`, which
carries `RETCODE_UNKNOWN` (`-1`). It must never synthesize a venue code, and in
particular never `0`: MQL5 `order_check` reports a PASSED check as retcode `0`,
so a synthesized `0` made an unrun check indistinguishable from a passed one and
the engine sent the order (issue #8).

COULD NOT MEASURE is not a verdict. When `measured` is false, neither `ok` nor
`retcode` means anything, so a caller gating on a pre-trade check aborts. The
engine journals that as `order_check_fail` with `reason="not_measured"`, kept
distinct from `reason="broker_refused"`, so an operator can tell "the broker
said no" from "we never asked".

## Methods

| Method | Meaning |
| --- | --- |
| `connect` / `disconnect` | Open or close the venue. |
| `account` | Balance, equity, `trade_mode`. |
| `symbol` | Spec for one name. |
| `tick` | Bid and ask. |
| `rates(symbol, timeframe, count)` | Bars. `timeframe` is `"H1"`, not an MT5 integer. |
| `positions` / `orders` | Open positions or working orders. Optional magic. |
| `select_symbol` | Add a name to the book. |
| `check_market` / `market` | Immediate buy or sell. |
| `check_working` / `working` | Limit or stop. |
| `modify_position` | SL/TP on an open position. |
| `modify_working` | Price or SL/TP on a working order. |
| `cancel` | Cancel a working order. |
| `close_position` | Close volume on a ticket. |
| `close_by` | Hedge offset. A venue may refuse. |

MT5 integers and `order_send` dicts stay inside `broker/mt5_live.py` and
`broker/paper.py` as private translation.

## The venue's clock is not UTC, and the offset is MEASURED

`Bar.time` and `Tick.time` are the BROKER SERVER's wall clock, encoded as an
epoch. They are not UTC and the wire says nothing about the difference. MT4
`iTime` / `TimeCurrent` and MT5 `copy_rates_from_pos` / `symbol_info_tick` both
behave this way, so both venues are affected identically.

| Method | Meaning |
| --- | --- |
| `venue_clock(symbol)` | A `VenueClock`: `offset_sec` is `server wall clock - UTC`, so UTC+3 is `+10800`. Convert a venue timestamp with `clock.to_utc(bar.time)`, never by hand. |

`VenueClock` carries the `SymbolSpec` partition, applied to a clock:
`offset_sec` is `None` and `unmeasured` names the field when the offset could
not be measured, and `to_utc` then RAISES rather than returning a plausible
instant. There is no "assume UTC" fallback, because zero is a perfectly
ordinary offset and a defaulted zero cannot be told from a measured one. The
rule is the standing one from straightedge#68: an unmeasured spec refuses, it
never defaults.

The offset is never configured. It is a per-server property that moves with the
SERVER's DST, so a number in `config.toml` is a guess that outlives the first
DST change after somebody wrote it.

**Who converts.** The engine, in `Engine._bar_instant`, which is the ONE place
a venue timestamp becomes a wall-clock instant. Every gate below it is written
in UTC, and there is deliberately no second conversion path that could
disagree. When the clock is unmeasured the auto leg refuses with
`venue_clock_unmeasured` and journals which field was missing; the desk path is
unaffected, because an operator command times itself off the desk clock and
never off a bar.

A venue with no `venue_clock` method at all reads as UNMEASURED, not as UTC
(`venue_clock_of` in `broker/base.py`). The Protocol declares the method, so a
real adapter that omits it is a typecheck failure as well.

`doctor --connect` prints the measured offset and exits non-zero when it is not
measured, because an auto leg that cannot measure when it is refuses every
signal, and that is a condition to catch before a run rather than during one.

## Two optional things a venue may declare about history

Both are read by `straightedge/history.py` and neither is on the `Broker`
Protocol, because only some venues have anything to say and a venue that says
nothing must read as silent rather than as zero.

| Name | Meaning |
| --- | --- |
| `history_async = True` | This venue can answer "no bars" now and serve them a moment later, because the terminal fetches history from the broker in the background. The preflight then waits, bounded. MT4 and MT5 set it. A venue that answers from memory must not: `run_backtest` seeds one bar per symbol on purpose and streams the rest, so a wait there is pure latency. |
| `history_probe(symbol, timeframe, count)` | Returns a `HistoryProbe`: the bars, plus `bars_total`, `selected` and `history_error` when the venue reports them. A venue without this method is read through `rates()` and those three fields stay `None`. |

`None` in any of those three fields means NOT REPORTED. It is never substituted
with 0, for the same reason `survivor_ticket` is not: a zero that stands in for
an unanswered question turns "nobody asked" into "no problem".
The MT5 adapter maps timeframe names with `timeframe_code`.
MT4 integers stay inside `broker/mt4_live.py` and the Expert.

## Config

`account.mode` is `paper`, `mt5`, or `mt4`.
That is a venue name, not a protocol.
A fourth venue adds a name and an adapter.
It does not change Telegram or risk.

## Fills

The venue holds live positions and working orders.
`journal.jsonl` is the source of truth for fills the bot observed.
A pending fill writes `open` with `fill=true`.
A vanished ticket writes `close` with `fill=true`.
Do not treat the terminal deal history as the bot's fill log.
