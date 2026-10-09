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


def unusable_volume(volume: float) -> str | None:
    """The refusal reason this volume earns, or None if it can be sent.

    ONE REASON WORD, NOT TWO, and that is a decision AGAINST the shape #208
    landed for stops rather than an oversight. `unusable_stop` splits
    `sl_required` from `sl_not_measured` because a stop has two sources: the
    operator omits one, or the VENUE reports a field that cannot be a price,
    and those send the operator to two different places to look. A close volume
    has ONE source. Both paths that reach here carry an operator-typed number
    (`/close TICKET VOL` and `/tp TICKET PX VOL`, each parsed by a bare
    `float()`), because the only COMPUTED volume on the close path is a stored
    scale-out and `normalize_volume` validates that before it is stored. Both
    kinds therefore call for the identical action, retype the number, and a
    second reason word would grow the vocabulary without changing what anyone
    does.

    The VALUE carries the diagnosis, which is the half of #208's argument that
    does transfer: `volume_unusable:nan` and `volume_unusable:-0.5` are
    different mistakes and read as different mistakes, with one name.

    DELIBERATELY NOT `volume_not_measured`. That word is the house term for a
    venue that "sent a value that cannot be a measurement" (`spec_not_measured`,
    `sl_not_measured`). Nothing measured this one; the operator typed it, and
    naming it after a venue failure would send them to the wrong place.

    BOTH TESTS ARE REQUIRED, AND THE ORDER IS NOT. The finiteness test cannot
    be dropped, because `nan <= 0` is False and so a magnitude test alone
    cannot see a `nan` at all: that is precisely how #210 reached the
    partial-close arithmetic and wrote `nan` into the account balance. But this
    is a single `or`, so unlike `unusable_stop`'s early-return ladder both
    operands are evaluated and swapping them changes nothing. The property that
    matters is that the finiteness test EXISTS, not that it comes first, and
    saying "order is load-bearing" here would be a comment claiming a property
    the code does not have.
    """
    if not math.isfinite(volume) or volume <= 0:
        return f"volume_unusable:{volume}"
    return None


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
