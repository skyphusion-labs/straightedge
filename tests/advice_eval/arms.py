"""The three context arms, and the assertion that they are actually different.

## Why the arms are not the ones #165 asked for

#165 specifies Arm A as `advice_context()` "exactly as it ships today" and Arm B
as the same context "plus the aggregates the desk already computes". That was
right when the issue was written and is wrong against current `main`:
`Engine.advice_context` already calls `risk_text()`, which states
`daily_loss=.../room=...` and `drawdown=.../room=...`, and `exposure_text()`,
which states `currency_exposure cap=N` with a per-code `net=/room=` row. Three
of the four quantities metric 1 asks about are already aggregated for the model.

Built as written, Arm A and Arm B would be the SAME STRING. The run would
report "Arm B does not lift Arm A" and the cause would be that there was only
one arm, not that context does not matter. That is a result with no reachable
failing state, and `docs/TESTING.md` is this repo's own record of why such a
result is worse than no result: the healthy and the broken case render
identically.

So the ablation is INVERTED, which preserves the causal question (#165's real
subject: does aggregation drive metric 1?) while giving it a state it can fail
in:

* **Arm A**: `advice_context()` exactly as it ships, aggregates included.
* **Arm B**: the same snapshot with the aggregate lines STRIPPED, which is the
  pre-aggregation shape #165 describes as "today". A lift from B to A measures
  what the already-merged aggregation work bought.
* **Arm C**: Arm A plus per-symbol worst-case risk, the one aggregate named in
  #165's metric 1 that the desk still does NOT pre-compute for the model. C is
  the only arm that can justify a change we have not already made.

## How the stripping is done, and why not with a regex over the whole context

Arm B removes two things, by EXACT SUBSTRING taken from the same engine that
built Arm A: the text `exposure_text()` returned, and the `room=` figures inside
the text `risk_text()` returned. Matching the engine's own output means the
removal cannot accidentally take a line that merely looks similar, and it means
a change to either renderer shows up here as a failed build rather than as an
arm that quietly stopped differing.

`assert_arms_differ` is the guard that the whole design rests on. It runs on
every snapshot at build time.
"""

from __future__ import annotations

import re

from straightedge.engine import Engine
from straightedge.sizing import MissingStop, money_per_lot_at_stop

#: Arm labels, in report order.
ARMS = ("A", "B", "C")

#: The ` room=<number>` tokens `risk_text` adds to its loss/drawdown line.
#: Anchored on the space so it cannot eat a `room=` that begins a line, which
#: is the shape `exposure_text` uses and which Arm B removes wholesale instead.
_ROOM_TOKEN = re.compile(r" room=-?[\d.]+")


def _strip_room_figures(risk_text: str) -> str:
    """`risk_text` with its pre-computed room figures removed.

    What is left still carries `daily_loss=<used>/<cap>` and
    `drawdown=<used>/<cap>`, so the model has everything it needs to DERIVE the
    room and nothing that states it. That is precisely the arithmetic-by-eye
    #165 suspects models are bad at, which is the thing being measured.
    """
    return _ROOM_TOKEN.sub("", risk_text)


def _worst_case_block(engine: Engine) -> str:
    """Per-symbol worst-case risk for the open book: the Arm C aggregate.

    Reads `sizing.money_per_lot_at_stop`, the same function `risk.evaluate`
    uses, so Arm C cannot show the model a worst case that disagrees with the
    one the gate would enforce.

    A position with no usable stop is reported `unmeasured`, never as a number.
    `money_per_lot_at_stop` RAISES on a missing stop precisely so a caller
    cannot turn it into a confident zero (the #161 fail-open this repo has
    already paid for), and an aggregate that defaulted here would hand the
    model a fabricated measurement.
    """
    r = engine.cfg.risk
    account = engine.broker.account()
    per_trade = account.equity * r.risk_pct * r.max_risk_multiple
    lines = [f"worst_case_risk per_trade_cap={per_trade:.2f}"]
    rows = engine.broker.positions(magic=r.magic)
    if not rows:
        lines.append("no open positions to measure")
        return "\n".join(lines)
    for position in rows:
        spec = engine.broker.symbol(position.symbol)
        try:
            worst = money_per_lot_at_stop(position.price_open, position.sl, spec) * position.volume
        except MissingStop as exc:
            lines.append(f"#{position.ticket} {position.symbol} worst=unmeasured ({exc.reason})")
            continue
        lines.append(f"#{position.ticket} {position.symbol} worst={worst:.2f}")
    return "\n".join(lines)


def build_arms(engine: Engine) -> dict[str, str]:
    """The three arm texts for this engine, proven distinct before returning."""
    arm_a = engine.advice_context()
    exposure = engine.exposure_text()
    risk = engine.risk_text()

    if exposure not in arm_a:
        raise RuntimeError(
            "exposure_text output is not present verbatim in advice_context, so "
            "Arm B cannot be built by removing it. advice_context composition "
            "has changed and this harness needs updating."
        )
    if risk not in arm_a:
        raise RuntimeError(
            "risk_text output is not present verbatim in advice_context, so Arm B "
            "cannot be built by removing its room figures. advice_context "
            "composition has changed and this harness needs updating."
        )

    arm_b = arm_a.replace(risk, _strip_room_figures(risk))
    # Remove the exposure block and the newline that joined it, so Arm B does
    # not carry a blank line where the aggregate used to be: a structural tell
    # would let a model infer something had been withheld.
    arm_b = arm_b.replace(exposure + "\n", "").replace(exposure, "")

    arm_c = arm_a + "\n" + _worst_case_block(engine)

    arms = {"A": arm_a, "B": arm_b, "C": arm_c}
    assert_arms_differ(arms)
    return arms


def assert_arms_differ(arms: dict[str, str]) -> None:
    """Refuse an arm set that cannot produce a different answer.

    This is the guard the inverted design exists for. If Arm A and Arm B are the
    same string then the headline comparison is between one arm and itself, and
    every "no lift" it reports is an artifact. Rather than let that reach a
    report, the build fails here.
    """
    arm_a, arm_b, arm_c = arms["A"], arms["B"], arms["C"]

    if arm_a == arm_b:
        raise RuntimeError(
            "Arm A and Arm B are identical, so the headline comparison has no "
            "reachable failing state. Either advice_context no longer carries "
            "the aggregates, or the stripping did nothing."
        )
    if arm_a == arm_c:
        raise RuntimeError("Arm C added nothing to Arm A, so the forward-looking arm is vacuous")

    # Arm A must STATE the aggregates and Arm B must not. Checked on both sides,
    # because a one-sided check passes when the renderer stops emitting them.
    if "room=" not in arm_a:
        raise RuntimeError("Arm A carries no room figures, so there is nothing for Arm B to ablate")
    if "currency_exposure" not in arm_a:
        raise RuntimeError("Arm A carries no currency exposure block, so Arm B cannot ablate it")
    if "room=" in arm_b:
        raise RuntimeError(f"Arm B still states a room figure: {arm_b!r}")
    if "currency_exposure" in arm_b:
        raise RuntimeError(f"Arm B still carries the currency exposure block: {arm_b!r}")

    # Arm B must be a SUBSET of Arm A's information, never a rewrite of it. A
    # transformation that changed other content would make the comparison a
    # measurement of two different snapshots rather than of one ablation.
    if len(arm_b) >= len(arm_a):
        raise RuntimeError("Arm B is not shorter than Arm A, so it did not ablate anything")

    # Arm C must be Arm A plus material, so A-vs-C isolates the addition.
    if not arm_c.startswith(arm_a):
        raise RuntimeError("Arm C is not Arm A plus an appended block, so it is not an isolated addition")
    if "worst_case_risk" not in arm_c:
        raise RuntimeError("Arm C is missing the worst-case-risk aggregate it exists to add")
