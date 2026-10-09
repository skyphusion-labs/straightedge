"""A non-finite quote must refuse, not sail past the guard written for it (#211).

`Engine.market_signal` and `Engine.reverse_signal` both carry

    if tick.bid <= 0 or tick.ask <= 0:
        raise RuntimeError(f"no tick for {symbol}")

and `nan <= 0` is False, so the guard that exists specifically to refuse a
broken quote misses the most broken quote there is. Reachable: both live
adapters build `bid = float(d.get("bid", 0) or 0)` with no finiteness check,
and `float` accepts `nan`, `NaN`, `-nan` and `inf`, so a corrupt mailbox line
reaches `Tick.bid` exactly as a zero does.

THIS IS A FAIL-OPEN ON THE SEND PATH, NOT A REASON DEFECT, and the issue I
filed understated it. Measured on the pre-fix tree, one side non-finite and the
other a real quote:

    /buy   bid=nan ask=ok   -> sent buy EURUSD vol=0.55 ok=True retcode=10009
    /buy   bid=inf ask=ok   -> sent buy EURUSD vol=0.55 ok=True retcode=10009
    /sell  bid=ok  ask=nan  -> sent sell EURUSD vol=0.55 ok=True retcode=10009

A full-size order reaches the venue while the venue is not quoting one side.
The asymmetry is the mechanism: a buy takes its entry from the ASK, so a broken
BID never enters the geometry comparison and nothing downstream objects; a sell
is the mirror. The spread gate cannot save it either, because `tick.spread` is
`ask - bid` and therefore `nan`, and `risk.py`'s
`tick.spread > max_spread_atr_frac * atr` is False for a nan, so the guard that
refuses a blown-out spread is bypassed in exactly the same move.

When BOTH sides are non-finite the geometry comparison does refuse, which is
why that case looks safe and is the one to assert the REASON on rather than the
outcome: a test asserting "nothing was staged" passes there before the fix.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.models import Tick
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand

WED_NOON = datetime(2024, 1, 3, 12, tzinfo=timezone.utc)


def _engine(tmp_path: Path) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    return Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: WED_NOON)


def _quote(engine: Engine, bid: float | None, ask: float | None) -> Tick:
    """Replace the broker tick, keeping whichever side is passed as None real."""
    real = engine.broker.tick("EURUSD")
    t = Tick(
        time=real.time,
        bid=real.bid if bid is None else bid,
        ask=real.ask if ask is None else ask,
    )
    engine.broker.tick = lambda name, _t=t: _t  # type: ignore[method-assign]
    return t


def _try(engine: Engine, cmd: str) -> tuple[str, str]:
    """The staged reply and the confirm reply, confirming only if staged."""
    try:
        staged = engine.handle_command(TgCommand("1", 1, cmd, 1))
    except (RuntimeError, ValueError) as exc:
        return f"raised:{exc}", "(raised)"
    if "confirm " not in staged:
        return staged, "(not staged)"
    return staged, engine.handle_command(TgCommand("1", 1, "/confirm", 2))


# --------------------------------------------------------------------------
# the fail-open: asserted on the OUTCOME, because the outcome is wrong
# --------------------------------------------------------------------------


def test_a_half_non_finite_quote_never_reaches_the_venue(tmp_path: Path) -> None:
    """The three orientations that SENT a full-size order before this fix.

    Asserted on the outcome rather than the reason, because here the outcome
    itself is the defect: these three reached `retcode=10009` with a position on
    the book. The side that is NOT used for the entry can be unreadable and
    nothing objects.
    """
    cases = (
        ("/buy EURUSD", math.nan, None),
        ("/buy EURUSD", math.inf, None),
        ("/sell EURUSD", None, math.nan),
    )
    for cmd, bid, ask in cases:
        engine = _engine(tmp_path / f"{cmd.split()[0][1:]}{bid}{ask}")
        engine.start()
        _quote(engine, bid, ask)
        staged, sent = _try(engine, cmd)
        positions = engine.broker.positions()
        engine.stop()

        assert not positions, (
            f"{cmd} bid={bid} ask={ask}: a full-size order reached the venue on an "
            f"unreadable quote. staged={staged!r} sent={sent!r}"
        )
        assert "confirm " not in staged, f"{cmd}: staged on an unreadable quote: {staged!r}"


def test_a_non_finite_quote_names_the_quote_not_the_geometry(tmp_path: Path) -> None:
    """Both sides non-finite is already refused, so assert the REASON.

    Before the fix this returned `buy needs sl < entry < tp`, which sends the
    operator to re-read a stop/entry/target ordering they got right while the
    venue is not quoting at all. A test asserting nothing was staged passes on
    the pre-fix tree and measures the geometry guard.
    """
    for cmd in ("/buy EURUSD", "/sell EURUSD"):
        engine = _engine(tmp_path / cmd.split()[0][1:])
        engine.start()
        _quote(engine, math.nan, math.nan)
        staged, _sent = _try(engine, cmd)
        engine.stop()

        assert "unreadable tick" in staged, f"{cmd}: {staged!r}"
        assert "needs" not in staged, f"{cmd}: still reported as geometry: {staged!r}"


def test_an_absent_quote_is_distinguished_from_an_unreadable_one(tmp_path: Path) -> None:
    """Two different operator actions, so two different messages.

    A zero quote means the venue sent nothing usable and the desk already says
    `no tick`. A non-finite quote means it sent something that cannot be a
    price, which is a corrupt field rather than a quiet feed, and the values are
    carried so the operator can see which side is broken.
    """
    engine = _engine(tmp_path / "absent")
    engine.start()
    _quote(engine, 0.0, 0.0)
    absent, _ = _try(engine, "/buy EURUSD")
    engine.stop()

    engine2 = _engine(tmp_path / "unreadable")
    engine2.start()
    _quote(engine2, math.nan, 1.1)
    unreadable, _ = _try(engine2, "/buy EURUSD")
    engine2.stop()

    assert "no tick for EURUSD" in absent, absent
    assert "unreadable tick for EURUSD" in unreadable, unreadable
    assert absent != unreadable, "an absent quote and a corrupt one must not read alike"
    assert "nan" in unreadable, "the offending value is the distinguishing detail"


def test_reverse_refuses_a_non_finite_quote(tmp_path: Path) -> None:
    """The second guarded site (`reverse_signal`), which has the same guard.

    Fixing one and not the other would leave the defect reachable through
    `/reverse`, and the two sites are far enough apart in the file to be missed.
    """
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    pos = engine.broker.positions()[0]
    _quote(engine, math.nan, math.nan)
    try:
        reply = engine.handle_command(TgCommand("1", 1, f"/reverse {pos.ticket}", 3))
    except (RuntimeError, ValueError) as exc:
        reply = f"raised:{exc}"
    engine.stop()

    assert "unreadable tick" in reply, reply


# --------------------------------------------------------------------------
# controls: there must be a reachable world in which this allows
# --------------------------------------------------------------------------


def test_an_ordinary_quote_still_stages_and_sends(tmp_path: Path) -> None:
    """A guard that refused every quote would pass everything above."""
    engine = _engine(tmp_path)
    engine.start()
    staged, sent = _try(engine, "/buy EURUSD")
    positions = engine.broker.positions()
    engine.stop()

    assert "confirm buy EURUSD" in staged, staged
    assert "sent buy" in sent, sent
    assert len(positions) == 1


def test_unusable_price_is_the_one_authority() -> None:
    """One function decides what an unusable price is; both sites consult it.

    Behaviour, not existence: every assertion here is about what the function
    RETURNS for a given input, so this cannot pass merely because a symbol was
    added.
    """
    from straightedge.sizing import unusable_price

    assert unusable_price(1.1000) is None
    assert unusable_price(0.0) == "absent"
    assert unusable_price(-1.0) == "absent"
    for bad in (math.nan, math.inf, -math.inf):
        assert unusable_price(bad) == "unreadable", bad
