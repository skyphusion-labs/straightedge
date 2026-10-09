# MetaTrader 5 API (what the bot uses)

The bot is the Python process on this computer.

Pulled from the official MQL5 Python Integration reference
(https://www.mql5.com/en/docs/integration/python_metatrader5)
and the trade-server return-code table.
The bot mirrors these constants in `straightedge/constants.py`.
The paper broker and the live adapter share one vocabulary.

## Binding

`pip install MetaTrader5` (PyPI `metatrader5`, current train 5.0.x).
The package is an IPC client against a running Windows terminal.
It is not a REST API.
It is not available as a native macOS wheel.

macOS options:

1. Install `mt5-mac` (PyPI 0.3.0).
   It talks JSON to Python-for-Windows inside the Wine runtime in MetaTrader 5.app.
   Same function names.
2. Use a Windows VPS running the official package.
   That VPS can expose REST (`mt5api`, `mt5-bridge`).
   The bot does not speak those HTTP APIs.
3. Use paper or backtest.
   No terminal is required.

`initialize()` locates or launches `terminal64.exe`.
Optional named args: `login`, `password`, `server`, `timeout` (default 60000 ms), `portable`.
`shutdown()` drops the connection.
`login(login, password, server)` switches account after initialize.

AlgoTrading must be enabled in the terminal.
Otherwise you get `TRADE_RETCODE_CLIENT_DISABLES_AT` = 10027.
`terminal_info().trade_allowed` is the check.

## Function map

Python functions that wrap MQL5:

| Python | MQL5 |
| --- | --- |
| `account_info` | AccountInfoInteger / Double / String |
| `terminal_info` | TerminalInfo* |
| `symbols_get` / `symbol_info` / `symbol_info_tick` | SymbolInfo* |
| `symbol_select` | SymbolSelect |
| `copy_rates_from` / `_from_pos` / `_range` | CopyRates |
| `copy_ticks_from` / `_range` | CopyTicks |
| `order_calc_margin` / `order_calc_profit` | OrderCalcMargin / Profit |
| `order_check` | OrderCheck |
| `order_send` | OrderSend |
| `orders_get` / `positions_get` | OrdersTotal+Get / PositionsTotal+Get |
| `history_orders_get` / `history_deals_get` | History* |
| `market_book_add` / `_get` / `_release` | MarketBook* |

Python-only: `initialize`, `login`, `shutdown`, `version`, `last_error`.

## account_info fields used here

`login`, `trade_mode` (0 demo, 1 contest, 2 real), `leverage`, `trade_allowed`,
`trade_expert`, `fifo_close`, `balance`, `equity`, `margin`, `margin_free`,
`profit`, `margin_level`, `server`, `currency`, `company`.

## Rates / ticks

`copy_rates_from_pos(symbol, timeframe, start_pos, count)` returns bars
`(time, open, high, low, close, tick_volume, spread, real_volume)`.
Timeframes: M1=1, M5=5, M15=15, M30=30, H1=16385, H4=16388, D1=16408.

`symbol_info_tick` returns `bid`, `ask`, `last`, `time`, `volume`.

**Both `time` fields are the TRADE SERVER's wall clock, not UTC, and the
package has no call that states the difference.** That was measured as a
live defect on the MT4 path (straightedge#172) and MT5 behaves identically
here, so the adapter is fixed identically: `Mt5Broker.venue_clock` measures
the offset by pairing `symbol_info_tick().time` with the desk's own UTC
clock, read either side of the call, and the engine converts bar time with
it before any gate sees an instant. An unmeasurable offset REFUSES; see
`docs/VENUE.md`. Nothing here is verified against a live MT5 terminal.

## Trade request (`order_send` / `order_check`)

Dict mapped onto `MqlTradeRequest`:

| Field | Role |
| --- | --- |
| `action` | TRADE_ACTION_DEAL=1, PENDING=5, SLTP=6, MODIFY=7, REMOVE=8, CLOSE_BY=10 |
| `magic` | EA id. The bot uses 20260909. Positions are filtered by it. |
| `symbol` | Instrument |
| `volume` | Lots. Must snap to `volume_min` / `volume_step` / `volume_max`. |
| `type` | ORDER_TYPE_BUY=0, SELL=1, plus pending types 2-7 |
| `price` | Required for instant/request execution. Optional for market execution. |
| `sl` / `tp` | Absolute prices. The bot requires SL. |
| `deviation` | Max slippage in points. Per symbol from `[risk.symbol_deviation_points]`, else `[risk] deviation_points`. Journaled with `deviation_source`. |
| `type_filling` | FOK=0, IOC=1, RETURN=2. Must match `SYMBOL_FILLING_MODE` bits |
| `type_time` | GTC=0, DAY=1, SPECIFIED=2 |
| `comment` | Keep short. Terminals truncate around 31 chars. |
| `position` | Ticket when closing or changing SL/TP |
| `position_by` | Opposite ticket for `TRADE_ACTION_CLOSE_BY` |

`order_check` validates funds and request shape.
Its success retcode is **0**, not 10009.
`order_send` success is 10009 (`TRADE_RETCODE_DONE`) or 10010 (partial) or 10008 (placed, pending).

Close a position: `TRADE_ACTION_DEAL` with the opposite `type` and `position=<ticket>`.
Netting vs hedging: on netting, an opposite deal reduces the single position.
On hedging you must pass the ticket.

Change position SL/TP: `TRADE_ACTION_SLTP` with `position`, `sl`, `tp`.
Change a pending order: `TRADE_ACTION_MODIFY` with `order`, `price`, `sl`, `tp`.
Close two opposite hedges: `TRADE_ACTION_CLOSE_BY` with `position` and `position_by`.
Hedge accounts only.
A netting terminal refuses CLOSE_BY.
A netting account has one net position per symbol.
There is no opposite ticket.
The paper broker always hedges (a new ticket per deal).
Close-by works in paper.
That is not a claim that paper P/L equals live.

## Filling mode (10030)

`SYMBOL_FILLING_MODE` is a bitfield: FOK=1, IOC=2.
If neither bit is set, RETURN is the market/exchange default.
CAUTION
Hardcoding RETURN is the usual cause of `TRADE_RETCODE_INVALID_FILL` (10030).

The bot picks FOK if allowed, else IOC, else RETURN.
It retries the other two on 10030.

FOK = all-or-nothing (size stays equal to the risk calc).
IOC = fill what you can (size can shrink; the bot still sends the computed lot).

## Stops (10016)

`SYMBOL_TRADE_STOPS_LEVEL` is the minimum SL/TP distance in points from the close price.
`SYMBOL_TRADE_FREEZE_LEVEL` blocks modify when price is that close to SL/TP.

**Only `stops_level` is enforced before send** (`risk.py`, the `stops_level`
refusal). `freeze_level` is MEASURED and recorded on the spec
(`trade_freeze_level`, populated by the MT5, MT4 and paper adapters) and nothing
reads it, so this desk sends the modify and the broker rejects it server-side;
the desk reports a failed modify with the broker's retcode. The claim that both
are enforced before send was false, and the half-truth was the worse part: a
reader who checked `stops_level`, found it, and inferred the rest would believe a
guard that is not there (issue #89).

That is a bounded cost, not an open exposure: the authoritative check lives at
the broker either way, and the outcome is the same refusal one round trip later.
If the pre-send freeze check is ever implemented, the pin in
`tests/test_mt5_unmeasured_specs.py` goes red and sends you back here.

## Lot math

```
ticks = abs(entry - sl) / trade_tick_size
money_per_lot = ticks * trade_tick_value
lots = floor((equity * risk_pct / money_per_lot) / volume_step) * volume_step
```

If the broker minimum lot would risk more than `risk_pct`, the trade is skipped.
Never round up.

For USD-quoted FX, `trade_tick_value` is about `contract_size * tick_size` in account currency.
For USDJPY it scales with price (`contract_size * tick_size / price` when the profit currency is not USD).
The live adapter reads `trade_tick_value` from the terminal so it stays correct.
The paper broker uses a static approximation.

### Unmeasured specs refuse, they never default

Every numeric field in `Mt5Broker.symbol` goes through one reader that records the
field in `SymbolSpec.unmeasured` and returns `0.0` when the terminal did not answer
it, or answered a zero where zero is not a possible measurement. Sizing then
returns 0 lots and `risk.evaluate` refuses with `spec_not_measured:<fields>`.

The names recorded are the CANONICAL spec names (`tick_value`, not
`trade_tick_value`), because `unmeasured_for_sizing()` intersects them with
`SPEC_SIZING_FIELDS`. A set of raw MT5 keys would look populated and gate nothing.

This used to be MT4-only. The MT5 adapter filled in FX-shaped defaults instead
(`trade_tick_value or 1.0`, `trade_contract_size or 100_000`, `point` 0.00001) and
never set `unmeasured`, so the rule could not fire here at all. Two consequences,
both live:

* `or` fires on a legitimate ZERO as well as on absence, so an XAUUSD spec
  arriving with zeros was sized as though gold had a 100,000 unit contract and a
  $1 tick. See "The per-symbol trap" in `CLAUDE.md` for the ratio.
* `trade_mode` defaulted to **4, full trading**, so a close-only or disabled
  symbol read as fully tradable. It now defaults to `0` (DISABLED) and is recorded
  as unmeasured, matching MT4.

Zero survives as a measurement where zero is a real reading: `digits` is 0 on an
index quoted in whole points, and `trade_stops_level` is 0 on a broker with no
minimum stop distance. Those fields are read with `positive=False`.

`trade_tick_size` is NOT defaulted to `point` any more. A derived value is not a
measurement. `ticks_between` still falls back to `point` on its own, but the
sizing gate fires first, which is the honest order.

**Not done here, and worth a decision:** MT5 also exposes
`trade_tick_value_loss` and `trade_tick_value_profit`. For RISK sizing the loss
leg is the correct one, and preferring it would both be more accurate and reduce
refusals when `trade_tick_value` is absent. That changes which number drives
sizing, so it is a separate change rather than part of making unmeasured honest.

## Return codes the bot cares about

| Code | Constant | Meaning |
| --- | --- | --- |
| 10004 | REQUOTE | Price moved. Retry with a fresh tick. |
| 10009 | DONE | Filled |
| 10010 | DONE_PARTIAL | Partial fill |
| 10014 | INVALID_VOLUME | Lot not on the step grid |
| 10016 | INVALID_STOPS | SL/TP inside stops_level |
| 10017 | TRADE_DISABLED | Symbol or account |
| 10018 | MARKET_CLOSED | Session |
| 10019 | NO_MONEY | Margin |
| 10024 | TOO_MANY_REQUESTS | Slow down |
| 10026 / 10027 | SERVER/CLIENT_DISABLES_AT | AutoTrading off |
| 10030 | INVALID_FILL | Wrong type_filling |
| 10031 | CONNECTION | Terminal offline |
| 10040 | LIMIT_POSITIONS | Server cap |
| 10045 | FIFO_CLOSE | US FIFO accounts |

## Market publication checks (still valid for any EA)

From "The checks a trading robot must pass before publication in the Market":

- Never `OrderSend` when margin is insufficient. Check first.
- Never place SL/TP inside `SYMBOL_TRADE_STOPS_LEVEL`.
- Never modify inside `SYMBOL_TRADE_FREEZE_LEVEL`.
- Handle hedging vs netting.
- Log retcodes.

The bot does those on both paper and live paths **except the
`SYMBOL_TRADE_FREEZE_LEVEL` one**, which it does not check before sending a
modify; see Stops (10016) above. This list is quoted from MQL5's publication
requirements, so it states what an EA is asked to do, not what this desk has
implemented, and the two were allowed to read as the same thing (issue #89).

## What the Python package is not

It is not a hosted REST service.
It cannot run inside Cloudflare Workers.
It does not backtest.
MetaTrader's Strategy Tester is MQL5-only.
The paper broker here is a separate, conservative simulator.
Same-bar SL and TP: SL wins.
