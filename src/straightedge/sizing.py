"""Risk-based position sizing. Lot size is a function of equity, stop distance,
and the symbol's tick value. Never round UP into extra risk.
"""

from __future__ import annotations

import math

from straightedge.models import SymbolSpec


def ticks_between(a: float, b: float, spec: SymbolSpec) -> float:
    tick = spec.trade_tick_size or spec.point
    if tick <= 0:
        return 0.0
    return abs(a - b) / tick


class MissingStop(ValueError):
    """Raised when a worst-case loss is asked for without a stop to measure to.

    A ValueError so the desk's existing command handling reports it instead of
    dying: `poll_telegram` already catches ValueError and replies with the
    message.
    """


def money_per_lot_at_stop(entry: float, sl: float, spec: SymbolSpec) -> float:
    """Money lost per lot if price travels from `entry` to `sl`.

    REFUSES on a missing stop instead of computing one (#187). `sl = 0` is the
    venue encoding for "no stop", and `ticks_between(price, 0, spec)` is
    `price / tick_size`, which is enormous: measured, a 0.1 lot EURUSD order
    resting with no stop produced a worst case of 11,506.70 against a 50.00
    per-trade cap. Arithmetic then proceeded on that as though it were a
    measurement.

    `Position.risk_distance` already treats `sl <= 0` as not-measurable, so the
    two functions disagreed about what a missing stop MEANS, and a third opinion
    at each call site is how that family spreads: the next caller inherits
    whichever one it happens to reach. The disagreement is resolved here, where
    the number is produced.

    It RAISES rather than returning 0.0, and that asymmetry with `risk_distance`
    is deliberate. A 0.0 here would make every caller's `worst` zero and pass
    every cap trivially, which is precisely the #161 fail-open this repo has
    already paid for once. The two agree that a missing stop is not a
    measurement; only one of them has a sentinel that fails closed.

    Every current caller is audited to never reach this: `_stop_guard` returns
    early on `sl <= 0`, and `risk.evaluate` refuses `sl_required` before both
    its own call and `lots_for_risk`. `replace_pending` is the one site that can
    see a venue-supplied `sl = 0`, and it names the refusal. A future caller
    that reaches this gets a loud failure rather than a confident number, which
    is the correct direction for an unaudited path on a real-money desk.
    """
    if sl <= 0:
        raise MissingStop(
            f"no stop to measure to: sl={sl!r}. A missing stop is not a distance, "
            "and a worst case cannot be computed without one."
        )
    ticks = ticks_between(entry, sl, spec)
    return ticks * spec.trade_tick_value


def normalize_volume(raw: float, spec: SymbolSpec) -> float:
    """Floor to volume_step, clamp to [min, max]. Returns 0 if below min."""
    if raw <= 0 or spec.volume_step <= 0:
        return 0.0
    stepped = math.floor(raw / spec.volume_step + 1e-12) * spec.volume_step
    digits = max(0, round(-math.log10(spec.volume_step))) if spec.volume_step < 1 else 0
    stepped = round(stepped, digits)
    if stepped < spec.volume_min - 1e-12:
        return 0.0
    if stepped > spec.volume_max:
        stepped = spec.volume_max
    return stepped


def lots_for_risk(
    equity: float,
    risk_pct: float,
    entry: float,
    sl: float,
    spec: SymbolSpec,
    *,
    max_risk_multiple: float = 1.0,
) -> float:
    """Lots such that a full stop-out loses about equity * risk_pct.

    If the broker minimum lot would risk more than equity * risk_pct *
    max_risk_multiple, return 0 (skip the trade). Never size up to min lot.
    """
    if equity <= 0 or risk_pct <= 0:
        return 0.0
    per_lot = money_per_lot_at_stop(entry, sl, spec)
    if per_lot <= 0:
        return 0.0
    budget = equity * risk_pct
    raw = budget / per_lot
    lots = normalize_volume(raw, spec)
    if lots <= 0:
        return 0.0
    actual_risk = per_lot * lots
    if actual_risk > budget * max_risk_multiple + 1e-9:
        return 0.0
    return lots
