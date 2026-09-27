"""Official MetaTrader 5 constants, mirrored so paper and live share one vocabulary.

Source: MQL5 Reference, Python Integration + Trading constants.
https://www.mql5.com/en/docs/integration/python_metatrader5
https://www.mql5.com/en/docs/constants/tradingconstants
"""

from __future__ import annotations

# Timeframes (ENUM_TIMEFRAMES)
TIMEFRAME_M1 = 1
TIMEFRAME_M2 = 2
TIMEFRAME_M3 = 3
TIMEFRAME_M4 = 4
TIMEFRAME_M5 = 5
TIMEFRAME_M6 = 6
TIMEFRAME_M10 = 10
TIMEFRAME_M12 = 12
TIMEFRAME_M15 = 15
TIMEFRAME_M20 = 20
TIMEFRAME_M30 = 30
TIMEFRAME_H1 = 16385
TIMEFRAME_H2 = 16386
TIMEFRAME_H3 = 16387
TIMEFRAME_H4 = 16388
TIMEFRAME_H6 = 16390
TIMEFRAME_H8 = 16392
TIMEFRAME_H12 = 16396
TIMEFRAME_D1 = 16408
TIMEFRAME_W1 = 32769
TIMEFRAME_MN1 = 49153

TIMEFRAME_BY_NAME = {
    "M1": TIMEFRAME_M1,
    "M5": TIMEFRAME_M5,
    "M15": TIMEFRAME_M15,
    "M30": TIMEFRAME_M30,
    "H1": TIMEFRAME_H1,
    "H4": TIMEFRAME_H4,
    "D1": TIMEFRAME_D1,
    "W1": TIMEFRAME_W1,
    "MN1": TIMEFRAME_MN1,
}


def timeframe_code(timeframe: str | int) -> int:
    if isinstance(timeframe, int):
        return timeframe
    key = str(timeframe).upper()
    if key not in TIMEFRAME_BY_NAME:
        raise ValueError(f"unknown timeframe {timeframe!r}")
    return TIMEFRAME_BY_NAME[key]

# TRADE_REQUEST_ACTIONS
TRADE_ACTION_DEAL = 1
TRADE_ACTION_PENDING = 5
TRADE_ACTION_SLTP = 6
TRADE_ACTION_MODIFY = 7
TRADE_ACTION_REMOVE = 8
TRADE_ACTION_CLOSE_BY = 10

#: The trade actions whose REPEAT is the same request rather than a second one.
#:
#: Each of these states a TARGET: set this stop, put this order at this price,
#: remove this order. A duplicate arrival leaves the same book, so a lost reply
#: may safely be answered by reconnecting and asking again.
#:
#: Everything absent from this set CREATES or CONSUMES volume (DEAL opens,
#: closes and partially closes; PENDING rests a new order; CLOSE_BY consumes two
#: positions), and for those a repeat is a SECOND money event. A lost reply
#: cannot distinguish "the terminal never saw it" from "the terminal accepted it
#: and the reply died", so those are sent exactly once and an absent reply is
#: reported as `OrderResult.unknown`.
#:
#: This is an ALLOWLIST and never a denylist, which is the whole point: a
#: `TRADE_ACTION_*` added later by someone who has not read this comment lands
#: in the single-attempt lane by default. Fail-closed costs an order; fail-open
#: costs a position. `tests/test_order_send_is_sent_once.py` pins that property.
IDEMPOTENT_TRADE_ACTIONS = frozenset(
    {
        TRADE_ACTION_SLTP,
        TRADE_ACTION_MODIFY,
        TRADE_ACTION_REMOVE,
    }
)

# ORDER_TYPE
ORDER_TYPE_BUY = 0
ORDER_TYPE_SELL = 1
ORDER_TYPE_BUY_LIMIT = 2
ORDER_TYPE_SELL_LIMIT = 3
ORDER_TYPE_BUY_STOP = 4
ORDER_TYPE_SELL_STOP = 5
ORDER_TYPE_BUY_STOP_LIMIT = 6
ORDER_TYPE_SELL_STOP_LIMIT = 7
ORDER_TYPE_CLOSE_BY = 8

# ORDER_TYPE_FILLING
ORDER_FILLING_FOK = 0
ORDER_FILLING_IOC = 1
ORDER_FILLING_RETURN = 2

# SYMBOL_FILLING_MODE bit flags (symbol_info.filling_mode)
SYMBOL_FILLING_FOK = 1
SYMBOL_FILLING_IOC = 2

# ORDER_TYPE_TIME
ORDER_TIME_GTC = 0
ORDER_TIME_DAY = 1
ORDER_TIME_SPECIFIED = 2
ORDER_TIME_SPECIFIED_DAY = 3

# POSITION_TYPE
POSITION_TYPE_BUY = 0
POSITION_TYPE_SELL = 1

# Trade server return codes (ENUM_TRADE_RETURN_CODES)
TRADE_RETCODE_REQUOTE = 10004
TRADE_RETCODE_REJECT = 10006
TRADE_RETCODE_CANCEL = 10007
TRADE_RETCODE_PLACED = 10008
TRADE_RETCODE_DONE = 10009
TRADE_RETCODE_DONE_PARTIAL = 10010
TRADE_RETCODE_ERROR = 10011
TRADE_RETCODE_TIMEOUT = 10012
TRADE_RETCODE_INVALID = 10013
TRADE_RETCODE_INVALID_VOLUME = 10014
TRADE_RETCODE_INVALID_PRICE = 10015
TRADE_RETCODE_INVALID_STOPS = 10016
TRADE_RETCODE_TRADE_DISABLED = 10017
TRADE_RETCODE_MARKET_CLOSED = 10018
TRADE_RETCODE_NO_MONEY = 10019
TRADE_RETCODE_PRICE_CHANGED = 10020
TRADE_RETCODE_PRICE_OFF = 10021
TRADE_RETCODE_INVALID_EXPIRATION = 10022
TRADE_RETCODE_ORDER_CHANGED = 10023
TRADE_RETCODE_TOO_MANY_REQUESTS = 10024
TRADE_RETCODE_NO_CHANGES = 10025
TRADE_RETCODE_SERVER_DISABLES_AT = 10026
TRADE_RETCODE_CLIENT_DISABLES_AT = 10027
TRADE_RETCODE_LOCKED = 10028
TRADE_RETCODE_FROZEN = 10029
TRADE_RETCODE_INVALID_FILL = 10030
TRADE_RETCODE_CONNECTION = 10031
TRADE_RETCODE_ONLY_REAL = 10032
TRADE_RETCODE_LIMIT_ORDERS = 10033
TRADE_RETCODE_LIMIT_VOLUME = 10034
TRADE_RETCODE_INVALID_ORDER = 10035
TRADE_RETCODE_POSITION_CLOSED = 10036
TRADE_RETCODE_INVALID_CLOSE_VOLUME = 10038
TRADE_RETCODE_CLOSE_ORDER_EXIST = 10039
TRADE_RETCODE_LIMIT_POSITIONS = 10040
TRADE_RETCODE_REJECT_CANCEL = 10041
TRADE_RETCODE_LONG_ONLY = 10042
TRADE_RETCODE_SHORT_ONLY = 10043
TRADE_RETCODE_CLOSE_ONLY = 10044
TRADE_RETCODE_FIFO_CLOSE = 10045
TRADE_RETCODE_HEDGE_PROHIBITED = 10046

RETCODE_OK = {TRADE_RETCODE_DONE, TRADE_RETCODE_DONE_PARTIAL, TRADE_RETCODE_PLACED}

# Not an MQL5 code, and deliberately outside the MQL5 space. Synthesized
# locally when a terminal call produced no result at all, so "could not
# measure" can never be read as "measured and passed". MQL5 uses retcode 0
# for a PASSED order_check, which is why a null result must never wear 0.
# Kept out of RETCODE_NAME and RETCODE_OK: that map mirrors the broker's
# vocabulary, and this code never comes from a broker.
RETCODE_UNKNOWN = -1

RETCODE_NAME = {
    10004: "REQUOTE",
    10006: "REJECT",
    10007: "CANCEL",
    10008: "PLACED",
    10009: "DONE",
    10010: "DONE_PARTIAL",
    10011: "ERROR",
    10012: "TIMEOUT",
    10013: "INVALID",
    10014: "INVALID_VOLUME",
    10015: "INVALID_PRICE",
    10016: "INVALID_STOPS",
    10017: "TRADE_DISABLED",
    10018: "MARKET_CLOSED",
    10019: "NO_MONEY",
    10020: "PRICE_CHANGED",
    10021: "PRICE_OFF",
    10022: "INVALID_EXPIRATION",
    10023: "ORDER_CHANGED",
    10024: "TOO_MANY_REQUESTS",
    10025: "NO_CHANGES",
    10026: "SERVER_DISABLES_AT",
    10027: "CLIENT_DISABLES_AT",
    10028: "LOCKED",
    10029: "FROZEN",
    10030: "INVALID_FILL",
    10031: "CONNECTION",
    10032: "ONLY_REAL",
    10033: "LIMIT_ORDERS",
    10034: "LIMIT_VOLUME",
    10035: "INVALID_ORDER",
    10036: "POSITION_CLOSED",
    10038: "INVALID_CLOSE_VOLUME",
    10039: "CLOSE_ORDER_EXIST",
    10040: "LIMIT_POSITIONS",
    10041: "REJECT_CANCEL",
    10042: "LONG_ONLY",
    10043: "SHORT_ONLY",
    10044: "CLOSE_ONLY",
    10045: "FIFO_CLOSE",
    10046: "HEDGE_PROHIBITED",
}


def choose_filling(filling_mode: int) -> int:
    """Pick a filling policy the symbol actually allows.

    SYMBOL_FILLING_MODE is a bitfield. Invalid fill (10030) is the most
    common live-order reject; never hardcode RETURN.
    Prefer FOK (all-or-nothing, risk size is certain), then IOC, then RETURN.
    """
    if filling_mode & SYMBOL_FILLING_FOK:
        return ORDER_FILLING_FOK
    if filling_mode & SYMBOL_FILLING_IOC:
        return ORDER_FILLING_IOC
    return ORDER_FILLING_RETURN


FILLING_RETRY_ORDER = (
    ORDER_FILLING_FOK,
    ORDER_FILLING_IOC,
    ORDER_FILLING_RETURN,
)


# ---------------------------------------------------------------------------
# MT4 mailbox budgets. A READ timing out is cheap; a SEND timing out is the
# ambiguous-money case, so the two get different budgets and the send one is
# DERIVED rather than chosen.
#
# Every term below is labelled MEASURED, COMPUTED or ALLOWANCE, the same
# discipline `tests/live_measurements.py` keeps, and for the same reason: an
# unlabelled number sitting next to measured ones becomes a measurement to the
# next reader. There is exactly ONE allowance here and it is named as such.
# ---------------------------------------------------------------------------

#: MEASURED on the live Vultr desk against the MT4 host, 2026-09-26, by
#: FileSystemWatcher on the mailbox directory (.req renamed in to .res renamed in),
#: over a 24.3 minute window: p50 205ms across 2166 requests. That median is the
#: ONLY trustworthy statistic from that window and `tests/live_measurements.py`
#: says why in as many words: the naive pairing used to compute latency shifts by
#: one after every unanswered request, and three requests went unanswered, so p90
#: and above are unreliable.
#:
#: Which is exactly why this is 1000 and not a tail statistic. A ceiling that a
#: measured tail cannot support has to come from somewhere defensible, so it is
#: about 5x the reliable median, and the conservative direction for a budget whose
#: failure mode is expiring early. Quoting a p90 here would be putting a number I
#: cannot stand behind next to ones I can.
MAILBOX_ROUND_TRIP_CEILING_MS = 1000

#: COMPUTED from `mt4/Experts/Mt4RiskBot.mq4`: the unconditional `Sleep(50)`
#: inside the Expert's three retry ladders, worst case, in ONE `Process()` call.
#: `SendRetry` 8 attempts, `ModifyRetry` 5, `RollbackPosition` 6, every one of
#: them 50ms apart: (8 + 5 + 6) * 50. `tests/test_send_budget.py` recomputes it
#: from the .mq4 source, so changing a loop bound in the Expert without moving
#: this number goes red.
EA_LADDER_SLEEP_MS = 950

#: COMPUTED from the same three ladders: the number of BROKER round trips the
#: Expert can make while the desk waits. 8 `OrderSend` + 5 `OrderModify` + 6
#: `OrderClose`. Local calls (`OrderSelect`, `RefreshRates`) are not counted:
#: they read the terminal's own cache and do not leave the host.
EA_BROKER_CALLS_WORST_CASE = 19

#: COMPUTED from the Expert's inputs `ClaimOpenRetries = 10` and
#: `ClaimOpenRetryMs = 20`: (10 - 1) * 20 per ladder, and there are TWO ladders on
#: the same bound, so 360.
#:
#: The second one is why this number moved while this change was in review. #82
#: retried the CLAIM READ; #83 then gave the REPLY WRITE the same bounded retry,
#: deliberately on the same `tries` knob so the two cannot drift. Both sit between
#: the desk's request and the desk's reply, so both spend the desk's send budget,
#: and a derivation that counted only the first would under-budget a send by 180ms
#: without anything saying so. `tests/test_send_budget.py` counts the retry loops
#: in the .mq4 and goes red if a third appears.
EA_CLAIM_RETRY_MS = 360

#: ALLOWANCE, and the ONLY un-measured term in the derivation. No measurement of
#: `OrderSend` latency against the live OANDA account exists yet: the box is
#: still on the MetaQuotes demo and the gold market is shut, so the first live
#: window is what measures this. 250ms per broker round trip is chosen against
#: the one thing that IS measured about this instrument -- gold's 45 point spread
#: against the shipped 20 point deviation (`tests/live_measurements.py`), which
#: makes a requote the expected case rather than the tail, and a requote is a
#: full round trip. It is deliberately not rounded away into the total; the
#: total is whatever the arithmetic produces.
BROKER_CALL_ALLOWANCE_MS = 250


def derive_send_timeout_ms() -> int:
    """The desk's send budget, as the sum of its named terms.

    Not a chosen number. The desk has to outlast the Expert or it writes off a
    request the Expert is still executing, which is how a duplicate order and a
    late fill both become reachable; so the budget is the transport ceiling plus
    everything the Expert can spend before it can possibly answer.

    Kept as a function rather than a literal so that no copy of the total exists
    anywhere: `config.py` calls it for the default and
    `tests/test_send_budget.py` asserts the shipped default IS this call.
    """
    return (
        MAILBOX_ROUND_TRIP_CEILING_MS
        + EA_LADDER_SLEEP_MS
        + EA_CLAIM_RETRY_MS
        + EA_BROKER_CALLS_WORST_CASE * BROKER_CALL_ALLOWANCE_MS
    )


#: The ops that can CHANGE THE BOOK, and therefore the ops that get the send
#: budget, the send TTL, and the Expert's refusal-when-stale rule.
#:
#: `check_market` and `check_working` are READS on purpose: the Expert returns
#: before `SendRetry` when `send` is false, so they cannot move money and a
#: stale one is harmless. Getting that partition wrong in the other direction
#: would be the expensive mistake, so it is stated as a frozenset here rather
#: than re-derived at each call site.
MAILBOX_SEND_OPS = frozenset(
    {
        "market",
        "working",
        "modify_position",
        "modify_working",
        "cancel",
        "close",
        "close_by",
    }
)
