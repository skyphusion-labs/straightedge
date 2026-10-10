"""One condition, one spelling, on every channel that can report it (#233).

The point of #233 was not the new string; it was that `sl_required` and
`sl required` were one meaning with two renderings. So this asserts the
PROPERTY rather than the text: every channel that reports "no usable stop"
is asked what it calls it, and the answers must be equal.

Nothing here hardcodes the word. A test pinning `"sl_required"` three times
would pass just as happily on three channels that had drifted back apart, as
long as somebody remembered to update all three literals, which is the thing
that did not happen the first time.
"""

from __future__ import annotations

from datetime import datetime, timezone

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.constants import TRADE_RETCODE_INVALID_STOPS
from straightedge.engine import Engine
from straightedge.models import MarketOrder, PendingOrder, Side
from straightedge.sizing import unusable_stop
from straightedge.synthetic import generate_bars


def _engine(tmp_path) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.strategy.auto = False
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    return Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        now_fn=lambda: datetime(2024, 1, 3, 12, tzinfo=timezone.utc),
    )


def _sizing_word() -> str:
    """What the order and sizing path calls the condition."""
    word = unusable_stop(0.0)
    assert word is not None, "unusable_stop no longer refuses a zero stop"
    return word


def _modify_word(engine: Engine) -> str:
    """What the working-order modify path calls it, driven through the real path."""
    spec = engine.broker.symbol("EURUSD")
    tick = engine.broker.tick("EURUSD")
    order = PendingOrder(
        ticket=1,
        symbol="EURUSD",
        side=Side.BUY,
        volume=0.1,
        price=spec.normalize_price(tick.ask - 0.002),
        sl=spec.normalize_price(tick.ask - 0.007),
        tp=spec.normalize_price(tick.ask + 0.008),
        magic=engine.cfg.risk.magic,
        kind="limit",
    )
    result = engine._modify_pending(order, sl=0.0, tp=order.tp)
    assert result.retcode == TRADE_RETCODE_INVALID_STOPS, result
    return result.comment


def _paper_send_word(engine: Engine) -> str:
    """What the paper adapter's own send guard calls it."""
    # `check_market` rather than `market`: the guard runs on the CHECK leg too,
    # so the word can be read without committing a fill into the book.
    result = engine.broker.check_market(
        MarketOrder(
            symbol="EURUSD",
            side=Side.BUY,
            volume=0.1,
            sl=0.0,
            tp=0.0,
            comment="probe",
            magic=engine.cfg.risk.magic,
        )
    )
    assert result.retcode == TRADE_RETCODE_INVALID_STOPS, result
    return result.comment


def test_every_channel_spells_the_condition_the_same_way(tmp_path) -> None:
    """The property #233 exists to hold, asserted as equality between channels.

    DERIVED FROM EACH CHANNEL, never compared against a literal. Three
    hardcoded copies of the word would pass on three channels that had drifted
    apart again, which is exactly how one condition acquired two spellings in
    the first place.
    """
    engine = _engine(tmp_path)
    spellings = {
        "order and sizing (refused:)": _sizing_word(),
        "working-order modify (sl failed)": _modify_word(engine),
        "paper adapter send guard": _paper_send_word(engine),
    }
    assert len(set(spellings.values())) == 1, (
        "one condition is spelled differently per channel again: "
        + repr(spellings)
    )


def test_the_shared_spelling_is_word_shaped(tmp_path) -> None:
    """One spelling is not enough: it has to be a WORD, not a sentence.

    Converging all three on `sl required` would satisfy the equality above and
    defeat the point, since the whole reason this was worth changing is that a
    near-homonym of a vocabulary word is not findable by an operator grepping
    for the word. So the shared value is also required to be word-shaped, which
    is what makes it enumerable in the contract table.
    """
    import re

    engine = _engine(tmp_path)
    word = _sizing_word()
    assert re.fullmatch(r"[a-z][a-z0-9_]*", word), f"not word-shaped: {word!r}"
    assert " " not in _modify_word(engine), "the modify channel reverted to prose"
    assert " " not in _paper_send_word(engine), "the paper send guard reverted to prose"
