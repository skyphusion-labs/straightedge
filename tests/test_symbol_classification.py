"""parse_fx must classify or refuse, never silently exempt (issue #10)."""

from __future__ import annotations

import itertools
import string
from datetime import datetime, timezone
from typing import NamedTuple

import pytest

from straightedge.broker.paper import default_spec
from straightedge.config import BotConfig
from straightedge.currencies import CURRENCY_CODES
from straightedge.models import Account, Position, Side, Signal, SignalKind, Tick
from straightedge.risk import (
    RiskManager,
    UnclassifiedSymbol,
    currency_exposure,
    classify_symbol,
    parse_fx,
    SYMBOL_FX,
    SYMBOL_NOT_FX,
)

CODES = ["".join(t) for t in itertools.product(string.ascii_uppercase, repeat=3)]

SUFFIX_CONVENTIONS = (
    "", "m", "M", "c", "C", "pro", "PRO", "mini", "micro", "ecn", "raw",
    "e", "i", "z", "r", "sb", ".a", ".m", ".r", ".pro", "_i", "_SB",
    "-5", "#", "+",
)


def _acct(equity: float = 10_000) -> Account:
    return Account(
        login=1,
        balance=equity,
        equity=equity,
        margin=0.0,
        margin_free=equity,
        profit=0.0,
        leverage=100,
        currency="USD",
        trade_allowed=True,
        trade_expert=True,
        trade_mode=0,
    )


def _sig(symbol: str = "EURUSD") -> Signal:
    return Signal(SignalKind.BUY, symbol, 1.10, 1.095, 1.1125, 0.003, reason="test")


def _pos(ticket: int, symbol: str, side: Side = Side.BUY) -> Position:
    return Position(ticket, symbol, side, 0.1, 1.1, 1.09, 1.12, 1.1, 0, magic=20260909)


def _cfg(tmp_path) -> BotConfig:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    return cfg


def _now() -> datetime:
    return datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)


def test_m_bearing_codes_parse() -> None:
    assert parse_fx("USDMXN") == ("USD", "MXN")
    assert parse_fx("MXNJPY") == ("MXN", "JPY")
    assert parse_fx("EURMXN") == ("EUR", "MXN")
    assert parse_fx("USDMXNm") == ("USD", "MXN")
    assert parse_fx("USDMXN.m") == ("USD", "MXN")
    assert parse_fx("MXNJPYmini") == ("MXN", "JPY")


def test_existing_shapes_still_parse() -> None:
    assert parse_fx("EURUSD") == ("EUR", "USD")
    assert parse_fx("USDJPYm") == ("USD", "JPY")
    assert parse_fx("XAUUSD") == ("XAU", "USD")


# --- the #60 sweep, widened for variable-length codes (issue #77) -------------
#
# #60 swept every three-letter stem in the base and the quote position under
# every suffix convention: 17,576 x 25 x 2 = 878,800 cases, split 9,650
# recognised and 869,150 unrecognised. That denominator ASSUMED the 3-and-3
# split, and its formula (17,576 minus the table size) silently assumed every
# code is three letters. Every one of those cases is still swept below.
#
# What is added is every stem longer than three letters within ONE letter of a
# code: each code longer than three, each code plus one letter, and each proper
# prefix of a code at least four long (MATI, which a suffix letter completes:
# MATI + "c" is MATIC). A stem shares nothing with the table otherwise only if
# no code is a prefix of it and it is a prefix of no code, and then the
# anchored parse cannot use it at all. Offline, EVERY four-letter stem and
# EVERY five-letter stem (a superset of every code plus two letters) is swept,
# 616,917,600 cases; CHANGELOG #77 has the counts. They stay out of CI because a
# 7.66M-case version took a minute a run, and a sweep CI will not tolerate gets
# deleted.
#
# Two oracles, both independent of the resolver's loop:
# - `_reference_split` brute-forces EVERY (base, quote) length, so a bound or
#   ordering error in the resolver disagrees with it;
# - `_three_and_three` is the pre-#77 resolver, verbatim. Wherever it resolved,
#   the new one must give the SAME answer. #77 may only ADD resolutions, and
#   each one it adds must go through a code longer than three letters.

SWEEP_DENOMINATOR_60 = 878_800
#: Cases None under 3-and-3 that now resolve. Pinned, not bounded: a change
#: here is a change in which symbols the currency limit applies to.
SWEEP_NEWLY_RESOLVED = 3_504


def _reference_split(alpha: str) -> tuple[str, str] | None:
    valid = [
        (i + j, -i, alpha[:i], alpha[i:i + j])
        for i in range(1, len(alpha) + 1)
        for j in range(1, len(alpha) - i + 1)
        if alpha[:i] in CURRENCY_CODES and alpha[i:i + j] in CURRENCY_CODES
    ]
    if not valid:
        return None
    best = min(valid)
    return best[2], best[3]


def _three_and_three(alpha: str) -> tuple[str, str] | None:
    if len(alpha) < 6:
        return None
    base, quote = alpha[:3], alpha[3:6]
    if base in CURRENCY_CODES and quote in CURRENCY_CODES:
        return base, quote
    return None


def _longer_stems() -> list[str]:
    stems: set[str] = set()
    for code in CURRENCY_CODES:
        if len(code) > 3:
            stems.add(code)
        stems.update(code + ch for ch in string.ascii_uppercase)
        stems.update(code[:n] for n in range(4, len(code)))
    return sorted(stems)


class Sweep(NamedTuple):
    cases: int
    recognised: int
    bad: list[str]
    newly: list[str]


def sweep(stems: list[str]) -> Sweep:
    """Every stem, both positions, every suffix convention. Importable offline."""
    reference: dict[str, tuple[str, str] | None] = {}
    bad: list[str] = []
    newly: list[str] = []
    cases = recognised = 0
    for stem in stems:
        is_code = stem in CURRENCY_CODES
        reference.clear()
        for suf in SUFFIX_CONVENTIONS:
            for sym, want in ((stem + "USD" + suf, (stem, "USD")), ("EUR" + stem + suf, ("EUR", stem))):
                cases += 1
                got = parse_fx(sym)
                alpha = "".join(ch for ch in sym if ch.isalpha()).upper()
                if alpha not in reference:
                    reference[alpha] = _reference_split(alpha)
                old = _three_and_three(alpha)
                if got != reference[alpha] or classify_symbol(sym) != (SYMBOL_FX if got else SYMBOL_NOT_FX):
                    bad.append(sym)
                elif old is not None and got != old:
                    bad.append(sym)
                elif is_code and got != want:
                    bad.append(sym)
                elif not is_code and got is not None and stem in got:
                    bad.append(sym)
                if is_code:
                    recognised += 1
                if old is None and got is not None:
                    newly.append(sym)
                    if max(len(got[0]), len(got[1])) <= 3:
                        bad.append(sym)
    return Sweep(cases, recognised, bad, newly)


def test_no_code_is_shorter_than_three_letters() -> None:
    """The precondition the whole #77 backward-compatibility claim rests on.

    `resolve_pair` minimises `(i + j, -i)`, so the split consuming the fewest
    letters wins. While every code is at least three letters, six is the
    smallest total any split can reach AND six is reachable only as 3 + 3, so a
    pre-#77 3-and-3 split is always the unique cheapest split and the tie-break
    never runs on one. "Every symbol that resolved under 3-and-3 resolves
    identically" is therefore a THEOREM resting on this bound, not an empirical
    result the sweep happens to observe.

    A code shorter than three letters voids it. Measured, with `XY` admitted to
    the table: `EURXYZ` reads EUR/XY on five letters, and `XYUSD` resolves at
    all where before it was too short to classify.

    The sweep DOES catch that, so this is not a hole in coverage; it is a hole
    in diagnosis. What the sweep catches it with is
    `assert (9650 + 250) == ((199 * 25) * 2)` plus a list of symbol names, which
    states an arithmetic mismatch and says nothing about the cause. Asserted
    against the TABLE, the same way the longer-stem floor above is computed from
    the table rather than from `longer`, so the failure names the precondition.
    """
    too_short = sorted(c for c in CURRENCY_CODES if len(c) < 3)
    assert too_short == [], (
        "a code shorter than three letters voids the #77 theorem that a 3-and-3 "
        f"split is always the unique cheapest split: {too_short}"
    )


def test_sweep_every_stem_in_every_suffix_convention_matches_the_rule() -> None:
    trios = ["".join(t) for t in itertools.product(string.ascii_uppercase, repeat=3)]
    longer = _longer_stems()
    old = sweep(trios)
    new = sweep(longer)
    cases = old.cases + new.cases
    bad = old.bad + new.bad
    newly = old.newly + new.newly
    # Each population is floored against ITSELF. The line this replaces summed
    # the two and compared the sum to one of the floors, which could not fire:
    # the longer-stem half is 257,700 cases on its own, so the #60 half could
    # lose all 257,700 and still clear the sum. Measured on issue #96, with the
    # #60 alphabet cut to 25 letters: 781,250 cases and the assertion passed.
    assert old.cases == SWEEP_DENOMINATOR_60, (
        f"the #60 sweep NARROWED: {old.cases} != {SWEEP_DENOMINATOR_60}"
    )
    # A floor with power, because it is computed from the TABLE and not from
    # `longer`: the longer-stem population must hold every code longer than
    # three letters AND every code plus one letter, and the table being
    # prefix-free (`test_the_shipped_table_is_prefix_free`) is what makes those
    # two families disjoint, so the bound is a sum and not a max. It needs no
    # edit when a code is added, which a pinned count would.
    floor_longer = sum(1 for c in CURRENCY_CODES if len(c) > 3) + len(CURRENCY_CODES) * 26
    assert len(longer) >= floor_longer, (
        f"the longer-stem sweep NARROWED: {len(longer)} stems < {floor_longer}"
    )
    # A tautology, not a floor: both sides are derived from `longer` in the same
    # expression, so it cannot observe `_longer_stems` shrinking. It is kept as
    # a statement of the shape of the sweep. The floor above is what guards the
    # population, together with the recognised-code count and the
    # SWEEP_NEWLY_RESOLVED pin at the end.
    assert new.cases == len(longer) * len(SUFFIX_CONVENTIONS) * 2
    assert old.recognised + new.recognised == len(CURRENCY_CODES) * len(SUFFIX_CONVENTIONS) * 2
    print(
        f"#77 sweep: {cases} cases = {SWEEP_DENOMINATOR_60} from #60 "
        f"({len(old.bad)} failed, {len(old.newly)} newly resolved) + {new.cases} "
        f"longer-stem ({len(new.bad)} failed, {len(new.newly)} newly resolved)"
    )
    assert bad == [], f"{len(bad)} of {cases} failed, first: {bad[:5]}"
    # Of the 878,800 #60 cases, exactly these change, and only from None: the
    # suffix letter completes DOGE, so EUR + DOG + "e" now reads EUR/DOGE and
    # counts toward the limit instead of being recorded as not applicable.
    assert sorted(old.newly) == ["EURDOGe", "EURDOGecn"]
    assert len(newly) == SWEEP_NEWLY_RESOLVED, newly[:20]


def test_too_short_to_classify_returns_none() -> None:
    assert parse_fx("US30") is None
    assert parse_fx("GER40") is None
    assert parse_fx("EUR") is None
    assert parse_fx("") is None


def test_currency_exposure_counts_m_bearing_pairs() -> None:
    positions = [_pos(1, "USDMXN"), _pos(2, "MXNJPY", Side.SELL)]
    exp = currency_exposure(positions)
    assert exp["MXN"] == -2
    assert exp["USD"] == 1
    assert exp["JPY"] == 1


def test_currency_exposure_refuses_an_unclassifiable_symbol() -> None:
    with pytest.raises(UnclassifiedSymbol):
        currency_exposure([_pos(1, "US30")])
    with pytest.raises(UnclassifiedSymbol):
        currency_exposure([], extra=("US30", Side.BUY))


def test_m_bearing_positions_reach_the_currency_limit(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg.risk.max_currency_exposure = 2
    rm = RiskManager(cfg, halt_dir=str(tmp_path))
    positions = [
        _pos(1, "USDMXN", Side.SELL),
        _pos(2, "EURMXN", Side.SELL),
        _pos(3, "GBPMXN", Side.SELL),
        _pos(4, "AUDMXN", Side.SELL),
    ]
    cfg.risk.max_positions = 9
    d = rm.evaluate(
        account=_acct(),
        signal=_sig("CADMXN"),
        spec=default_spec("CADMXN"),
        tick=Tick(time=0, bid=1.0999, ask=1.1001),
        positions=positions,
        orders=[],
        now=_now(),
    )
    assert not d.allowed
    assert d.reason == "currency_exposure"


def test_classify_symbol_separates_fx_from_not_fx() -> None:
    assert classify_symbol("EURUSD") == SYMBOL_FX
    assert classify_symbol("EURUSDm") == SYMBOL_FX
    assert classify_symbol("XAUUSD") == SYMBOL_FX
    assert classify_symbol("USDMXN") == SYMBOL_FX
    assert classify_symbol("US30") == SYMBOL_NOT_FX
    assert classify_symbol("GER40") == SYMBOL_NOT_FX
    assert classify_symbol("USOIL") == SYMBOL_NOT_FX
    assert classify_symbol("EUR") == SYMBOL_NOT_FX


def test_non_fx_symbol_is_allowed_and_the_exclusion_is_named(tmp_path) -> None:
    rm = RiskManager(_cfg(tmp_path), halt_dir=str(tmp_path))
    d = rm.evaluate(
        account=_acct(),
        signal=_sig("US30"),
        spec=default_spec("US30"),
        tick=Tick(time=0, bid=1.0999, ask=1.1001),
        positions=[],
        orders=[],
        now=_now(),
    )
    assert d.allowed, d.reason
    assert d.excluded_from_currency_limit == ("US30",)


def test_non_fx_open_position_is_excluded_not_refused(tmp_path) -> None:
    rm = RiskManager(_cfg(tmp_path), halt_dir=str(tmp_path))
    d = rm.evaluate(
        account=_acct(),
        signal=_sig("EURUSD"),
        spec=default_spec("EURUSD"),
        tick=Tick(time=0, bid=1.0999, ask=1.1001),
        positions=[_pos(1, "US30")],
        orders=[],
        now=_now(),
    )
    assert d.allowed, d.reason
    assert d.excluded_from_currency_limit == ("US30",)


def test_non_fx_position_does_not_hide_a_real_fx_breach(tmp_path) -> None:
    cfg = _cfg(tmp_path)
    cfg.risk.max_positions = 9
    rm = RiskManager(cfg, halt_dir=str(tmp_path))
    positions = [
        _pos(1, "US30"),
        _pos(2, "USDMXN", Side.SELL),
        _pos(3, "EURMXN", Side.SELL),
        _pos(4, "GBPMXN", Side.SELL),
        _pos(5, "AUDMXN", Side.SELL),
    ]
    d = rm.evaluate(
        account=_acct(),
        signal=_sig("CADMXN"),
        spec=default_spec("CADMXN"),
        tick=Tick(time=0, bid=1.0999, ask=1.1001),
        positions=positions,
        orders=[],
        now=_now(),
    )
    assert not d.allowed
    assert d.reason == "currency_exposure"
    assert d.excluded_from_currency_limit == ("US30",)


def test_not_applicable_is_recorded_and_the_trade_proceeds(tmp_path) -> None:
    from straightedge.broker.paper import PaperBroker
    from straightedge.engine import Engine
    from straightedge.synthetic import generate_bars

    cfg = _cfg(tmp_path)
    cfg.symbols = ["US30"]
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker = PaperBroker(balance=10_000)
    bars = generate_bars(400, drift=0.0004, vol=0.0002, seed=3)
    broker.seed_bars("US30", bars)
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=_now)
    engine.replay_symbol("US30", bars)
    rows = engine.journal.tail(50)
    notes = [r for r in rows if r.get("event") == "currency_limit_not_applicable"]
    assert notes, "the exclusion was not recorded, so the limit went quiet"
    assert notes[-1]["excluded"] == ["US30"]
    assert not [r for r in rows if r.get("event") == "reject"]
    assert broker.positions(), "a non-FX instrument must still be tradeable"


def test_sweep_short_stems_are_never_fx() -> None:
    tails = ("", "30", "40", "100", "500", ".cash")
    bad: list[str] = []
    cases = 0
    for n in (1, 2, 3):
        for t in itertools.product(string.ascii_uppercase, repeat=n):
            stem = "".join(t)
            for tail in tails:
                cases += 1
                sym = stem + tail
                if classify_symbol(sym) != SYMBOL_NOT_FX or parse_fx(sym) is not None:
                    bad.append(sym)
    assert cases == (26 + 676 + 17_576) * len(tails)
    assert bad == [], f"{len(bad)} of {cases} wrongly read as a pair, first: {bad[:5]}"


def test_dotted_instrument_is_not_a_pair() -> None:
    for sym in ("US30.cash", "USOIL.cash", "GER40.cash", "UK100.cash"):
        assert classify_symbol(sym) == SYMBOL_NOT_FX, sym
        assert parse_fx(sym) is None, sym


def test_unrecognised_first_six_is_not_a_pair() -> None:
    for sym in ("mEURUSD", "FXEURUSD", "US30.cash", "USOIL.cash", "UK100.cash"):
        assert parse_fx(sym) is None, sym
        assert classify_symbol(sym) == SYMBOL_NOT_FX, sym


def test_separator_inside_the_pair_still_parses() -> None:
    assert parse_fx("EUR.USD") == ("EUR", "USD")
    assert parse_fx("EUR/USD") == ("EUR", "USD")
    assert parse_fx("EUR_USD") == ("EUR", "USD")
    assert classify_symbol("EUR.USD") == SYMBOL_FX


def test_metals_parse_because_iso_assigns_them_codes() -> None:
    assert parse_fx("XAUUSD") == ("XAU", "USD")
    assert parse_fx("XAGUSD") == ("XAG", "USD")
    assert parse_fx("XAUUSDm") == ("XAU", "USD")


def test_sweep_separated_pairs_parse_because_the_table_confirms() -> None:
    seps = (".", "/", "_", "-", " ")
    bad: list[str] = []
    cases = 0
    codes = sorted(CURRENCY_CODES)
    for a in codes:
        for b in codes:
            for sep in seps:
                cases += 1
                if parse_fx(a + sep + b) != (a, b):
                    bad.append(a + sep + b)
    assert cases == len(codes) * len(codes) * len(seps)
    assert bad == [], f"{len(bad)} of {cases} failed to parse, first: {bad[:5]}"
