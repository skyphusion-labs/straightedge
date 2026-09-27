"""Per-symbol slippage tolerance, and the gate that refuses one the spread eats.

The defect, measured on the live MT4 rig 2026-09-24 (see
tests/live_measurements.py for the numbers): `[risk] deviation_points` was a
single global, default 20, and a POINT is instrument-specific. 20 points is 2
pips on a 5-digit EURUSD and 20 cents on XAUUSD, where the spread alone was 45
points. Every gold send therefore offered the venue less than half of one spread
of tolerance, `OrderSend` rejected intermittently, and nothing in the log named
the cause: what reached the operator was "trades randomly do not go through".

Two changes, and the second is the one that matters:

1. `[risk.symbol_deviation_points]` overrides the global per symbol. A map alone
   only moves the failure: an operator can mis-key it, forget a symbol, or key
   `XAUUSD` on a venue that calls the instrument `XAUUSD.m`, and the send goes
   out on the global default exactly as before.

2. `deviation_below_spread` turns the whole class into a NAMED REFUSAL at our
   own gate, before the order is sent. A deviation below the spread cannot fill:
   the order has to cross the bid/ask gap. So the misconfiguration stops being
   an intermittent venue rejection with no explanation and becomes one refusal
   that names the symbol, the measurement, and the config key to change.

What this file does NOT claim: nothing here ran against the rig. Every number is
a fixture. That the gate refuses the configuration Conrad measured rejecting is
proven; that a gold send with the seeded 150 points then FILLS is not, and COULD
NOT MEASURE from here (the rig is SSH-keyed from the lead's laptop only).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from live_measurements import (
    FX_SPREAD_POINTS,
    GLOBAL_DEVIATION_POINTS,
    GOLD_ATR,
    GOLD_SPREAD_POINTS,
    gold_spec,
    gold_tick,
)
from straightedge.broker.paper import PaperBroker, default_spec
from straightedge.config import (
    DEVIATION_FROM_DEFAULT,
    DEVIATION_FROM_SYMBOL,
    BotConfig,
    load_config,
    parse_symbol_deviation_points,
)
from straightedge.engine import Engine
from straightedge.models import Account, Signal, SignalKind, Tick, WorkingOrder
from straightedge.risk import DEVIATION_BELOW_SPREAD, DEVIATION_HEADROOM_MULTIPLE, RiskManager
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand

WED_NOON = datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)


def _cfg(tmp_path: Path) -> BotConfig:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.session.enabled = False
    return cfg


def _acct(equity: float = 100_000.0) -> Account:
    return Account(
        login=1,
        balance=equity,
        equity=equity,
        margin=0.0,
        margin_free=equity,
        profit=0.0,
        leverage=100,
        currency="USD",
    )


def _gold_signal(*, tick: Tick) -> Signal:
    """A gold BUY whose stop and target come from the MEASURED ATR.

    1.5 ATR stop and 2.5 ATR target, the shipped strategy multiples, so rr
    clears min_rr and the only gate under test is the deviation one.
    """
    entry = round(tick.ask, 2)
    return Signal(
        kind=SignalKind.BUY,
        symbol="XAUUSD",
        entry=entry,
        sl=round(entry - 1.5 * GOLD_ATR, 2),
        tp=round(entry + 2.5 * GOLD_ATR, 2),
        atr=GOLD_ATR,
        reason="test",
    )


def _gate(rm: RiskManager, *, signal: Signal, spec, tick: Tick, equity: float = 100_000.0):
    return rm.evaluate(
        account=_acct(equity),
        signal=signal,
        spec=spec,
        tick=tick,
        positions=[],
        orders=[],
        now=WED_NOON,
        manual=True,
    )


# --- 1. the resolution: symbol first, then the global default ----------------


def test_a_listed_symbol_gets_its_own_value_and_an_absent_one_gets_the_global() -> None:
    """One assertion cannot pass on both, because the two answers DIFFER.

    A test that only checked "the resolved value is an int" or asserted the same
    number on both paths would pass with the map never consulted. So this pins
    that gold resolves to 150 and EURUSD to 20, and that the two are not equal.
    """
    risk = BotConfig().risk
    risk.symbol_deviation_points = {"XAUUSD": 150}

    gold = risk.resolve_deviation("XAUUSD")
    fx = risk.resolve_deviation("EURUSD")

    assert gold.points == 150
    assert gold.source == DEVIATION_FROM_SYMBOL
    assert fx.points == GLOBAL_DEVIATION_POINTS == risk.deviation_points
    assert fx.source == DEVIATION_FROM_DEFAULT
    assert gold.points != fx.points, (
        "the map and the default resolved to the same number, so this test "
        "cannot tell whether the map was read at all"
    )


def test_the_lookup_is_case_insensitive_and_otherwise_exact() -> None:
    """`xauusd` matches. `XAUUSD.m` does NOT, and that is the deliberate part.

    Stripping a broker decoration would mean guessing which decorations name the
    same instrument. A near-miss falls back to the global default instead, and
    `deviation_below_spread` is what makes that fallback loud.
    """
    risk = BotConfig().risk
    risk.symbol_deviation_points = {"xauusd": 150}

    assert risk.resolve_deviation("XAUUSD").points == 150
    assert risk.resolve_deviation("XauUsd").points == 150

    decorated = risk.resolve_deviation("XAUUSD.m")
    assert decorated.points == GLOBAL_DEVIATION_POINTS
    assert decorated.source == DEVIATION_FROM_DEFAULT


def test_the_source_is_reported_so_the_number_can_be_debugged() -> None:
    """20 points read in a journal says nothing about WHERE it came from."""
    risk = BotConfig().risk
    risk.symbol_deviation_points = {"XAUUSD": 150}
    assert risk.resolve_deviation("XAUUSD").source == DEVIATION_FROM_SYMBOL
    assert risk.resolve_deviation("XAUUSD").symbol == "XAUUSD"
    assert risk.resolve_deviation("GBPUSD").source == DEVIATION_FROM_DEFAULT


# --- 2. the gate: red on the measured configuration -------------------------


def test_gold_at_the_fx_default_is_refused_by_name(tmp_path: Path) -> None:
    """THE RED PROOF. The exact configuration measured rejecting at the venue.

    Gold, 45 points of spread, the global 20-point deviation, no per-symbol
    entry. Nothing else about the signal is wrong: rr clears min_rr, the stop
    clears stops_level, and the 45-cent spread is a sixth of what
    max_spread_atr_frac allows against a $17.22 ATR, so `spread_too_wide`
    passes. The only thing wrong is the number the operator configured.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    tick = gold_tick()
    decision = _gate(rm, signal=_gold_signal(tick=tick), spec=gold_spec(), tick=tick)

    assert decision.allowed is False
    assert decision.reason.startswith(DEVIATION_BELOW_SPREAD + ":")


def test_the_refusal_names_the_fix_not_just_the_fault(tmp_path: Path) -> None:
    """The reason reaches the operator verbatim, so it has to be actionable.

    desk.py answers `refused: <reason>`. "Slippage too small" without the config
    key and the number to put in it is a refusal the operator cannot act on, and
    an unactionable refusal is how a gate gets switched off.

    `floor` is what the gate enforced (one spread, 45). `set` is larger on
    purpose: an operator who writes the exact floor is refused again by the
    first tick that widens the spread by one point. See
    DEVIATION_HEADROOM_MULTIPLE.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    tick = gold_tick()
    reason = _gate(rm, signal=_gold_signal(tick=tick), spec=gold_spec(), tick=tick).reason

    assert "XAUUSD" in reason
    assert f"deviation={GLOBAL_DEVIATION_POINTS}" in reason
    assert f"source={DEVIATION_FROM_DEFAULT}" in reason
    assert f"spread={GOLD_SPREAD_POINTS}pt" in reason
    assert f"floor={GOLD_SPREAD_POINTS}pt" in reason
    advised = int(DEVIATION_HEADROOM_MULTIPLE * GOLD_SPREAD_POINTS)
    assert f"set=risk.symbol_deviation_points.XAUUSD>={advised}" in reason, reason


def test_a_mis_keyed_override_is_caught_by_the_gate(tmp_path: Path) -> None:
    """The map can be got wrong; this is what stops that being silent.

    An operator who keys `XAUUSD` on a venue that calls gold `XAUUSD.m` has
    configured nothing, and before this gate the send simply went out on 20
    points and was rejected by the broker with no cause recorded.
    """
    cfg = _cfg(tmp_path)
    cfg.risk.symbol_deviation_points = {"XAUUSD": 150}
    rm = RiskManager(cfg, halt_dir=tmp_path)
    tick = gold_tick()
    signal = _gold_signal(tick=tick)
    decorated = Signal(
        kind=signal.kind,
        symbol="XAUUSD.m",
        entry=signal.entry,
        sl=signal.sl,
        tp=signal.tp,
        atr=signal.atr,
        reason="test",
    )
    decision = _gate(rm, signal=decorated, spec=gold_spec(), tick=tick)

    assert decision.allowed is False
    assert decision.reason.startswith(DEVIATION_BELOW_SPREAD + ":")
    assert "XAUUSD.m" in decision.reason
    assert f"source={DEVIATION_FROM_DEFAULT}" in decision.reason


# --- 3. the gate: green on a correct configuration --------------------------


def test_gold_with_the_seeded_override_passes(tmp_path: Path) -> None:
    """THE GREEN PROOF. Same spec, same tick, the override from the example config.

    150 points is what `config.example.toml` seeds, and it clears the 45-point
    floor with headroom. The decision is `ok` with a volume, so this is the whole
    chain passing and not merely this one gate abstaining.
    """
    cfg = _cfg(tmp_path)
    cfg.risk.symbol_deviation_points = {"XAUUSD": 150}
    rm = RiskManager(cfg, halt_dir=tmp_path)
    tick = gold_tick()
    decision = _gate(rm, signal=_gold_signal(tick=tick), spec=gold_spec(), tick=tick)

    assert decision.allowed is True, decision.reason
    assert decision.reason == "ok"
    assert decision.volume > 0


def test_raising_the_global_default_also_works(tmp_path: Path) -> None:
    """The gate is about the EFFECTIVE number, not about which key supplied it."""
    cfg = _cfg(tmp_path)
    cfg.risk.deviation_points = 150
    rm = RiskManager(cfg, halt_dir=tmp_path)
    tick = gold_tick()
    decision = _gate(rm, signal=_gold_signal(tick=tick), spec=gold_spec(), tick=tick)
    assert decision.reason == "ok"


# --- 4. no regression on normal FX ------------------------------------------


def test_eurusd_at_the_default_is_unaffected(tmp_path: Path) -> None:
    """The measured live FX case: 1 point of spread against 20 of tolerance.

    Twenty times the margin. A gate that touched this would be an outage, not a
    control.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    spec = default_spec("EURUSD")
    half = (FX_SPREAD_POINTS * spec.point) / 2.0
    tick = Tick(time=0, bid=1.10 - half, ask=1.10 + half, last=1.10)
    assert spec.points(tick.spread) == pytest.approx(FX_SPREAD_POINTS)

    signal = Signal(
        kind=SignalKind.BUY, symbol="EURUSD", entry=1.10, sl=1.095, tp=1.1125, atr=0.003
    )
    assert _gate(rm, signal=signal, spec=spec, tick=tick).reason == "ok"


def test_the_paper_brokers_own_fx_spread_is_unaffected(tmp_path: Path) -> None:
    """The repo's own fixture: 10 points of spread against 20 of tolerance.

    2x, which is ordinary retail FX and fills routinely. This case is why the
    floor multiple is 1.0 and not 3.0: at 3.0 the gate refused 88 tests in this
    suite, all of them encoding a configuration that works.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    broker = PaperBroker(balance=100_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    spec = broker.symbol("EURUSD")
    tick = broker.tick("EURUSD")
    assert round(spec.points(tick.spread)) == 10

    mid = round(tick.ask, spec.digits)
    signal = Signal(
        kind=SignalKind.BUY,
        symbol="EURUSD",
        entry=mid,
        sl=round(mid - 0.0045, spec.digits),
        tp=round(mid + 0.0075, spec.digits),
        atr=0.003,
    )
    assert _gate(rm, signal=signal, spec=spec, tick=tick).reason == "ok"


# --- 5. the boundary, pinned deliberately -----------------------------------


def test_exactly_at_the_floor_passes_and_one_point_under_refuses(tmp_path: Path) -> None:
    """`below` the spread, not `at` it, and the boundary is watched.

    Worth pinning for a second reason: the default tick in
    tests/test_refusal_reasons.py carries 20 points against the 20-point
    default, so most of that roster runs at EXACTLY this boundary. If the
    comparison ever loosens to `<=`, it fails here by name instead of turning
    half the suite red for no stated reason.
    """
    cfg = _cfg(tmp_path)
    spread_points = 40
    cfg.risk.deviation_points = spread_points
    rm = RiskManager(cfg, halt_dir=tmp_path)
    tick = gold_tick(spread_points=spread_points)
    spec = gold_spec(spread_points=spread_points)

    assert _gate(rm, signal=_gold_signal(tick=tick), spec=spec, tick=tick).reason == "ok"

    cfg.risk.deviation_points = spread_points - 1
    under = _gate(rm, signal=_gold_signal(tick=tick), spec=spec, tick=tick)
    assert under.reason.startswith(DEVIATION_BELOW_SPREAD + ":")
    assert f"deviation={spread_points - 1}" in under.reason


def test_a_multiple_above_one_refuses_what_one_spread_allows(tmp_path: Path) -> None:
    """The multiple is a real lever, not a decorative field.

    An operator who wants margin sets it, and the gate then refuses a deviation
    that clears one full spread. Proven by the same inputs answering differently
    on either side of the key.
    """
    cfg = _cfg(tmp_path)
    cfg.risk.symbol_deviation_points = {"XAUUSD": 50}
    rm = RiskManager(cfg, halt_dir=tmp_path)
    tick = gold_tick()
    spec = gold_spec()
    assert _gate(rm, signal=_gold_signal(tick=tick), spec=spec, tick=tick).reason == "ok"

    cfg.risk.min_deviation_spread_multiple = 2.0
    strict = _gate(rm, signal=_gold_signal(tick=tick), spec=spec, tick=tick)
    assert strict.reason.startswith(DEVIATION_BELOW_SPREAD + ":")
    assert "floor=90pt" in strict.reason


def test_the_gate_abstains_on_a_crossed_tick(tmp_path: Path) -> None:
    """Spread <= 0 is a broken tick: nothing to compare, so no verdict.

    Recorded rather than left implicit. This gate must not report a PASS it did
    not measure, and it also must not invent a refusal from a tick fault it does
    not own: no gate currently owns a crossed tick, which is a separate finding.
    """
    cfg = _cfg(tmp_path)
    cfg.risk.deviation_points = 1
    rm = RiskManager(cfg, halt_dir=tmp_path)
    flat = Tick(time=0, bid=4000.00, ask=4000.00, last=4000.00)
    decision = _gate(rm, signal=_gold_signal(tick=gold_tick()), spec=gold_spec(), tick=flat)
    assert not decision.reason.startswith(DEVIATION_BELOW_SPREAD)


# --- 6. it refuses; it does not quietly raise the number --------------------


def test_the_operators_number_is_never_overridden(tmp_path: Path) -> None:
    """A gate that fixed the config would leave a number nobody wrote in force."""
    cfg = _cfg(tmp_path)
    rm = RiskManager(cfg, halt_dir=tmp_path)
    tick = gold_tick()
    _gate(rm, signal=_gold_signal(tick=tick), spec=gold_spec(), tick=tick)

    assert cfg.risk.deviation_points == GLOBAL_DEVIATION_POINTS
    assert cfg.risk.symbol_deviation_points == {}
    assert cfg.risk.resolve_deviation("XAUUSD").points == GLOBAL_DEVIATION_POINTS


# --- 7. which number applied is observable on every send -------------------


def test_the_journal_records_the_deviation_and_where_it_came_from(tmp_path: Path) -> None:
    """Observability is the requirement, so it is asserted on the artifact.

    The journal is the structured channel; the number alone cannot tell an
    operator whether their override was consulted or missed.
    """
    cfg = _cfg(tmp_path)
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.symbol_deviation_points = {"EURUSD": 60}
    broker = PaperBroker(balance=100_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: WED_NOON)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("2", 2, "/confirm", 1))

    opened = engine.journal.last_event("open")
    assert opened is not None, "no open record to read the deviation from"
    assert opened["deviation"] == 60
    assert opened["deviation_source"] == DEVIATION_FROM_SYMBOL

    ticket = int(opened["order"])
    engine.handle_command(TgCommand("3", 3, f"/close {ticket}", 1))
    closed = engine.journal.last_event("close")
    assert closed is not None
    assert closed["deviation"] == 60
    assert closed["deviation_source"] == DEVIATION_FROM_SYMBOL
    engine.stop()


# --- 8. the config keys -----------------------------------------------------


def test_the_example_config_seeds_the_measured_metals() -> None:
    """The shipped example must carry gold, and it must resolve.

    A comment recommending a value nobody parsed is not a default.
    """
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.example.toml")
    gold = cfg.risk.resolve_deviation("XAUUSD")
    assert gold.source == DEVIATION_FROM_SYMBOL
    assert gold.points >= DEVIATION_HEADROOM_MULTIPLE * GOLD_SPREAD_POINTS
    assert cfg.risk.resolve_deviation("XAGUSD").source == DEVIATION_FROM_SYMBOL
    assert cfg.risk.resolve_deviation("EURUSD").source == DEVIATION_FROM_DEFAULT


def test_the_handover_config_seeds_them_too() -> None:
    """Gil trades mostly gold, and the handover template is what he is handed."""
    root = Path(__file__).resolve().parents[1]
    cfg = load_config(root / "config.handover.toml")
    assert cfg.risk.resolve_deviation("XAUUSD").source == DEVIATION_FROM_SYMBOL


def test_an_omitted_multiple_gets_the_dataclass_default(tmp_path: Path) -> None:
    """One number, one home.

    The loader's first draft restated `3.0` here while the field said `1.0`, so a
    config that omitted the key got a different answer from one that spelled the
    default out. The fallback reads the dataclass; this is what holds it there.
    """
    path = tmp_path / "c.toml"
    path.write_text('[risk]\nmax_positions = 3\n', encoding="utf-8")
    cfg = load_config(path)
    assert (
        cfg.risk.min_deviation_spread_multiple
        == BotConfig().risk.min_deviation_spread_multiple
    )


def test_a_scalar_where_a_table_belongs_raises(tmp_path: Path) -> None:
    """An override the loader silently dropped is the worst of the outcomes.

    The send would go out on the global default while the config said otherwise
    and nothing reported the disagreement.
    """
    with pytest.raises(ValueError, match="must be a table"):
        parse_symbol_deviation_points(150)


@pytest.mark.parametrize(
    "raw",
    [
        {"XAUUSD": "150"},
        {"XAUUSD": True},
        {"XAUUSD": 150.5},
    ],
)
def test_a_non_integer_point_count_raises(raw) -> None:
    with pytest.raises(ValueError):
        parse_symbol_deviation_points(raw)


def test_the_multiple_cannot_be_configured_below_one_spread() -> None:
    """There is no value that switches this gate off.

    Below 1.0 the gate would permit a deviation that cannot fill, which is the
    defect it exists to name. A loud config error is the right answer; a silently
    disabled control is the one this repo keeps finding.
    """
    cfg = BotConfig()
    cfg.risk.min_deviation_spread_multiple = 0.0
    with pytest.raises(ValueError, match="min_deviation_spread_multiple"):
        cfg.validate()


def test_a_zero_or_negative_override_is_refused() -> None:
    """0 reaches the MT4 Expert as "use your own input Slippage" (Mt4RiskBot.mq4
    only falls back when the passed value is <= 0), so a 0 here would silently
    hand the number to a default nobody configured on this side."""
    cfg = BotConfig()
    cfg.risk.symbol_deviation_points = {"XAUUSD": 0}
    with pytest.raises(ValueError, match="must be > 0"):
        cfg.validate()


def test_two_keys_for_one_symbol_are_refused() -> None:
    """Case-insensitive matching means `xauusd` and `XAUUSD` are one symbol.

    Left alone, the lookup would pick whichever came first in the file and the
    other would be dead config nobody could see was dead.
    """
    cfg = BotConfig()
    cfg.risk.symbol_deviation_points = {"XAUUSD": 150, "xauusd": 40}
    with pytest.raises(ValueError, match="two entries for the same"):
        cfg.validate()


# --- 9. the limit/stop path carries the number the gate judged (issue #92) ---
#
# `risk.evaluate()` has always gated a PENDING signal on `deviation_below_spread`
# exactly as it gates a market one. The number was then never transmitted:
# `WorkingOrder` had no `deviation` field, `_place_pending` never called
# `resolve_deviation`, `_working_payload` sent no `deviation` key, and the
# Expert's pending handler passed its own `input int Slippage = 30` to
# `SendRetry`. So the gate refused, or permitted, a limit order over a tolerance
# that order could not carry, and the config key its refusal advises changed
# nothing on that path.
#
# What the tests below can and cannot establish, stated once: they prove the
# operator's figure now REACHES the venue (the wire assertion lives in
# `tests/test_mt4_wire.py`, which is where wire bytes are asserted in this repo).
# They prove nothing about whether MT4 APPLIES a slippage tolerance to a pending
# order type; the documentation says it is ignored for pending types, that was
# not measured on the live rig, and a unit test structurally cannot measure it.
# Transmitting the operator's number rather than one configured on the other side
# of the bridge does not depend on the answer.

EA_PATH = Path(__file__).resolve().parents[1] / "mt4" / "Experts" / "Mt4RiskBot.mq4"

#: Every Expert handler that reaches a venue call carrying a slippage argument:
#: `OrderSend` for a market order, `OrderSend` for a pending order, and
#: `OrderClose`. This tuple IS the denominator #92 is about. Two of these three
#: resolved the desk's number from the request body; the pending one did not.
EA_SEND_HANDLERS = (
    "string CheckMarket(string id, string body, bool send)",
    "string CheckWorking(string id, string body, bool send)",
    "string ClosePos(string id, string body)",
)


def _ea_function_body(signature: str) -> str:
    """The brace-matched body of one MQL4 function, signature included.

    Matched on braces rather than by line range, because a line range silently
    starts covering the next function the moment anything above it moves.
    """
    src = EA_PATH.read_text(encoding="utf-8")
    start = src.index(signature)
    depth = 0
    for i in range(src.index("{", start), len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces after {signature!r}")


class _RecordingPaper(PaperBroker):
    """A paper broker that keeps the `WorkingOrder` objects it was handed.

    The engine hands the venue an OBJECT; turning that object into wire bytes is
    the adapter's job and is asserted against the byte-exact mailbox in
    `tests/test_mt4_wire.py`. This captures the object so the engine half can be
    asserted without a terminal. Neither half alone is the path.
    """

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.checked: list[WorkingOrder] = []
        self.sent: list[WorkingOrder] = []

    def check_working(self, order: WorkingOrder):  # type: ignore[no-untyped-def]
        self.checked.append(order)
        return super().check_working(order)

    def working(self, order: WorkingOrder):  # type: ignore[no-untyped-def]
        self.sent.append(order)
        return super().working(order)


def test_the_gate_refuses_a_pending_signal_exactly_as_it_refuses_a_market_one(
    tmp_path: Path,
) -> None:
    """Half one of #92's first harm, and on its own it is not the finding.

    That the gate refuses a gold LIMIT at the 20-point default is the behaviour
    that was already there. What made it a defect is that the refused order could
    not have carried the number being judged, which is what the next test pins.
    A gate judging a value it does not transmit cannot be right, and the
    remediation it prints (set `risk.symbol_deviation_points.XAUUSD`) was inert on
    this path.
    """
    cfg = _cfg(tmp_path)
    rm = RiskManager(cfg, halt_dir=tmp_path)
    tick = gold_tick()
    pending = replace(_gold_signal(tick=tick), pending_kind="limit")
    assert pending.pending_kind == "limit", "the signal under test is not a pending one"

    decision = _gate(rm, signal=pending, spec=gold_spec(), tick=tick)

    assert not decision.allowed
    assert decision.reason.startswith(DEVIATION_BELOW_SPREAD)
    assert f"deviation={GLOBAL_DEVIATION_POINTS}" in decision.reason
    assert f"spread={GOLD_SPREAD_POINTS}pt" in decision.reason
    assert "set=risk.symbol_deviation_points.XAUUSD" in decision.reason


def test_a_pending_send_carries_the_resolved_deviation_and_journals_it(
    tmp_path: Path,
) -> None:
    """Half two: the refusal is now a true statement about the path refused.

    One resolver feeds both sides. The gate reads
    `risk.resolve_deviation(symbol)`; `_place_pending` now reads the same call and
    puts the answer on the order, so the number the gate judges and the number the
    send carries cannot differ. 60 is a per-symbol override rather than the global
    default on purpose: a test that asserted the default would pass with the map
    never consulted and with `deviation` left at the dataclass default of 20,
    which is the exact value the pre-#92 code would have produced by accident.

    The `pending` journal record carries `deviation` and `deviation_source` for
    the reason #68 gave for `open` and `close`: a number in a journal cannot tell
    an operator whether their override was consulted, mis-keyed, or never written.
    Before this change that record carried NEITHER field, so a limit order's
    tolerance was unreadable live and after the fact.
    """
    cfg = _cfg(tmp_path)
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.symbol_deviation_points = {"EURUSD": 60}
    broker = _RecordingPaper(balance=100_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: WED_NOON)
    engine.start()
    limit = round(broker.tick("EURUSD").bid - 0.0020, 5)
    engine.handle_command(TgCommand("1", 1, f"/buy EURUSD limit={limit}", 1))
    engine.handle_command(TgCommand("2", 2, "/confirm", 1))

    placed = engine.journal.last_event("pending")
    assert placed is not None, "no pending record; the limit order never went out"
    assert placed["ok"] is True, f"the pending send failed: {placed}"
    assert placed["kind"] == "limit"
    assert placed["deviation"] == 60
    assert placed["deviation_source"] == DEVIATION_FROM_SYMBOL

    assert [o.deviation for o in broker.sent] == [60], (
        "the WorkingOrder handed to the venue does not carry the resolved "
        "deviation, so the gate is judging a number the send does not offer"
    )
    assert [o.deviation for o in broker.checked] == [60], (
        "the pre-trade check ran on a different tolerance from the send"
    )
    assert cfg.risk.resolve_deviation("EURUSD").points == 60
    engine.stop()


def test_the_pending_expert_handler_sends_the_resolved_slippage_not_its_own_input() -> None:
    """The shipped `.mq4`, which is the only artifact a customer installs.

    MQL4 does not execute in this suite and no CI runner has a compiler, so a
    source guard over the shipped file is the only gate available here. It is a
    real gate on that artifact (mutate the file and this goes red) and it is NOT
    evidence about a running terminal.
    """
    body = _ea_function_body("string CheckWorking(string id, string body, bool send)")
    calls = [ln for ln in body.splitlines() if "SendRetry(" in ln]
    assert len(calls) == 1, f"expected one SendRetry call in the pending handler: {calls}"
    assert ", slip," in calls[0], (
        "the pending handler does not pass the resolved slippage to SendRetry"
    )
    assert "Slippage" not in calls[0], (
        "the pending handler still passes its own `input int Slippage` to the send, "
        "so the desk's deviation never reaches OrderSend"
    )


def test_every_expert_send_handler_resolves_the_deviation_from_the_request() -> None:
    """The denominator, measured on the file rather than asserted in prose.

    Three handlers reach a venue call with a slippage argument. Two of them
    resolved the desk's number and one did not, and a suite that only tested the
    two would have been green throughout. The `<= 0` fallback is part of the
    pattern, not decoration: it is what an older desk that sends no `deviation`
    key at all gets, and it must be the only case that reaches the Expert's own
    input.
    """
    resolved = [
        sig
        for sig in EA_SEND_HANDLERS
        if 'KV(body, "deviation")' in _ea_function_body(sig)
        and "if(slip <= 0) slip = Slippage;" in _ea_function_body(sig)
    ]
    missing = [sig for sig in EA_SEND_HANDLERS if sig not in resolved]
    assert not missing, (
        f"{len(resolved)} of {len(EA_SEND_HANDLERS)} Expert send handlers resolve the "
        f"desk's deviation; these do not: {missing}"
    )


def test_a_zero_or_negative_global_deviation_is_refused() -> None:
    """The asymmetry #92 found: the per-symbol map had this floor, the global did not.

    0 does not mean "no tolerance". The Expert reads a deviation of <= 0 as "use
    my own `input int Slippage`" (`Mt4RiskBot.mq4`, all three send handlers), so a
    0 here moves the operator's risk figure to a number configured on the other
    side of the bridge, where nothing on this side can read it. The message names
    the key, because a refusal an operator cannot act on is one they switch off.
    """
    for value in (0, -1):
        cfg = BotConfig()
        cfg.risk.deviation_points = value
        with pytest.raises(ValueError, match=r"risk\.deviation_points must be > 0"):
            cfg.validate()


def test_a_positive_global_deviation_still_validates() -> None:
    """The other half of the guard above, and the half that is easy to skip.

    A floor that refused every value would satisfy the test above and nothing
    else in the suite would notice, because `validate()` raising is what that
    test asserts. This pins that the gate can also say yes.
    """
    cfg = BotConfig()
    cfg.risk.deviation_points = 1
    cfg.validate()
    cfg.risk.deviation_points = GLOBAL_DEVIATION_POINTS
    cfg.validate()
