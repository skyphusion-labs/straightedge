"""Issue #30: the MT4 symbol reader fabricates values it never measured.

`float(d.get(key, DEFAULT) or DEFAULT)` fires on a legitimate ZERO, not only on
absence, so a broker-reported zero silently becomes a EURUSD-shaped default. The
resulting spec is indistinguishable from a measured one, and the last-line guard
cannot catch it because `risk.py` recomputes `money_per_lot_at_stop` from the
same corrupt spec: a wrong number is compared against a wrong number and passes.

Two zero producers, both real: `MarketInfo` answers 0 for a symbol that is not in
Market Watch, and the Expert truncated `tick_value` to four decimals so any real
value below 0.00005 arrived as zero.

MQL4 has exactly ONE tick-value identifier, `MODE_TICKVALUE`. There is no
loss-leg variant, so the MT5 remedy of preferring `trade_tick_value_loss` does
not transfer. On MT4 the only honest fix is to REFUSE.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from mt4_transcripts import EA_SYMBOL_KEYS, t_symbol
from test_mt4_wire import broker, reads_of, wired
from test_risk import _acct, _cfg, _now, _sig, _tick

from straightedge.broker.mt4_live import Mt4Broker
from straightedge.broker.paper import default_spec
from straightedge.risk import RiskManager
from straightedge.sizing import lots_for_risk

ADAPTER_PATH = Path(__file__).resolve().parents[1] / "src" / "straightedge" / "broker" / "mt4_live.py"

# The four the Expert cannot answer. MQL4's MarketInfo has no trade-mode
# identifier and no per-symbol currency identifiers, so these are not an
# oversight in the Expert; they are a limit of the platform.
NEVER_ON_THE_WIRE = ("trade_mode", "currency_base", "currency_profit", "currency_margin")


def _symbol_reader_source() -> str:
    src = ADAPTER_PATH.read_text(encoding="utf-8")
    start = src.index("    def symbol(self, name: str)")
    return src[start : src.index("    def tick(self, name: str)", start)]


# --------------------------------------------------------------------------
# The denominator, as an assertion rather than a sentence in a PR
# --------------------------------------------------------------------------


def test_the_denominator_is_pinned() -> None:
    """15 fields read, 11 on the wire, 4 unanswerable.

    Pinned so that adding a read without a producer, or dropping a producer,
    goes red here instead of quietly widening the unmeasured set. Uses the same
    `reads_of` the wire suite uses, so the two denominators cannot drift apart
    or disagree about what counts as a read.
    """
    reads = reads_of(Mt4Broker.symbol)
    assert len(reads) == 15, sorted(reads)
    assert len(EA_SYMBOL_KEYS) == 11, EA_SYMBOL_KEYS
    assert reads - set(EA_SYMBOL_KEYS) == set(NEVER_ON_THE_WIRE)


def test_the_or_idiom_is_gone_from_the_symbol_reader() -> None:
    """Asserted on the AST, not on the text.

    A line-matching version of this guard matched the prose in the reader's own
    docstring, which explains the idiom it removed. A guard that a comment can
    turn red is a guard that a comment can also turn green, so it reads the
    syntax tree and counts `or` expressions instead.
    """
    tree = ast.parse(ADAPTER_PATH.read_text(encoding="utf-8"))
    reader = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "symbol"
    )
    ors = [
        node
        for node in ast.walk(reader)
        if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or)
    ]
    assert ors == [], (
        f"{len(ors)} `or` expression(s) remain in the symbol reader. "
        "`d.get(key, DEFAULT) or DEFAULT` cannot tell a measured zero from an "
        "absent key, so each one can fabricate a value: "
        + ", ".join(f"line {node.lineno}" for node in ors)
    )


# --------------------------------------------------------------------------
# tick_value: the headline field
# --------------------------------------------------------------------------


def test_a_zero_tick_value_is_not_a_measurement(tmp_path: Path) -> None:
    """This inverts a pin #50 deliberately left in place.

    #50 asserted that a zero tick value becomes 1.0 and is then
    indistinguishable from a genuine 1.0. That was the defect being recorded,
    not endorsed. Here it becomes a refusal.
    """
    with wired(tmp_path, {"symbol": t_symbol(tick_value="0.0000")}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert "tick_value" in spec.unmeasured
    assert spec.trade_tick_value == 0.0, (
        "a value that was not measured must not carry a usable-looking number"
    )


def test_a_measured_tick_value_is_kept(tmp_path: Path) -> None:
    """Positive control. If this went unmeasured too, the check above would be
    measuring the instrument rather than the defect."""
    with wired(tmp_path, {"symbol": t_symbol(tick_value="0.6700")}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert spec.trade_tick_value == 0.67
    assert "tick_value" not in spec.unmeasured


def test_a_tick_value_above_one_is_kept(tmp_path: Path) -> None:
    """The oversizing direction has to survive untouched; refusing it would be
    a different defect with the same symptom."""
    with wired(tmp_path, {"symbol": t_symbol(tick_value="2.5000")}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert spec.trade_tick_value == 2.5
    assert spec.unmeasured == frozenset() or "tick_value" not in spec.unmeasured


def test_the_fabricated_one_point_zero_oversizes_by_the_ratio() -> None:
    """Why 1.0 is not a conservative default.

    The error is the ratio true/1.0. Below 1.0 it undersizes, which is safe.
    Above 1.0 it oversizes by exactly that ratio, so a 2.5 tick value spends
    2.5x the intended budget. This is arithmetic on the shipped sizer, not a
    claim about the adapter.
    """
    def spec_with(tick_value: float):
        base = default_spec("EURUSD")
        return base.__class__(**{**base.__dict__, "trade_tick_value": tick_value})

    equity, risk_pct, entry, sl = 10_000.0, 0.01, 1.10000, 1.09800
    budget = equity * risk_pct

    # Sized honestly against a 2.5 tick value.
    honest = lots_for_risk(equity, risk_pct, entry, sl, spec_with(2.5))
    # Sized against the fabricated 1.0 while the truth is still 2.5.
    fabricated = lots_for_risk(equity, risk_pct, entry, sl, spec_with(1.0))
    assert honest > 0 and fabricated > 0

    # What the fabricated size actually loses, priced at the true tick value.
    from straightedge.sizing import money_per_lot_at_stop

    real_loss = money_per_lot_at_stop(entry, sl, spec_with(2.5)) * fabricated
    assert real_loss > budget * 2.0, (real_loss, budget)
    assert fabricated > honest * 2.0

    # The other direction is safe, which is why the fix REFUSES rather than
    # substituting a "better" number: 1.0 is not conservative, it is just wrong
    # in a direction that depends on the instrument.
    jpy_loss = money_per_lot_at_stop(entry, sl, spec_with(0.67)) * fabricated
    assert jpy_loss < budget


# --------------------------------------------------------------------------
# The refusal has to be NAMED, not size_zero
# --------------------------------------------------------------------------


def _decide(tmp_path: Path, spec):
    rm = RiskManager(_cfg(tmp_path))
    rm.observe(_acct(10_000), _now())
    return rm.evaluate(
        account=_acct(10_000),
        signal=_sig(),
        spec=spec,
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )


def _unmeasured_spec(*names: str):
    base = default_spec("EURUSD")
    fields = dict(base.__dict__)
    fields["unmeasured"] = frozenset(names)
    for name in names:
        if name == "tick_value":
            fields["trade_tick_value"] = 0.0
        if name == "volume_step":
            fields["volume_step"] = 0.0
    return base.__class__(**fields)


def test_an_unmeasured_tick_value_refuses_by_name(tmp_path: Path) -> None:
    got = _decide(tmp_path, _unmeasured_spec("tick_value"))
    assert not got.allowed
    assert got.reason.startswith("spec_not_measured"), (
        f"refused as {got.reason!r}; `size_zero` says the budget was too small, "
        "which is a different fact from never having measured the tick value"
    )
    assert "tick_value" in got.reason


def test_an_unmeasured_volume_step_refuses_by_name_not_size_zero(tmp_path: Path) -> None:
    """The issue's second, quieter field. `normalize_volume` returns 0 when the
    step is not positive, so every order was refused `size_zero` with no
    explanation of why."""
    got = _decide(tmp_path, _unmeasured_spec("volume_step"))
    assert not got.allowed
    assert got.reason.startswith("spec_not_measured")
    assert "volume_step" in got.reason


def test_a_fully_measured_spec_is_not_refused_for_measurement(tmp_path: Path) -> None:
    """Positive control on the gate itself. A gate that refuses everything is
    not a gate."""
    got = _decide(tmp_path, default_spec("EURUSD"))
    assert not got.reason.startswith("spec_not_measured")


# --------------------------------------------------------------------------
# The four fields the Expert cannot answer
# --------------------------------------------------------------------------


def test_the_four_unanswerable_fields_are_marked_not_fabricated(tmp_path: Path) -> None:
    with wired(tmp_path, {"symbol": t_symbol()}):
        spec = broker(tmp_path).symbol("EURUSD")
    for name in NEVER_ON_THE_WIRE:
        assert name in spec.unmeasured, f"{name} is never on the wire but is not marked"


def test_trade_mode_does_not_present_as_fully_tradable(tmp_path: Path) -> None:
    """A close-only symbol must not read as fully tradable.

    LATENT, not live: nothing in `src/` reads `SymbolSpec.trade_mode` today,
    only `Account.trade_mode` is consumed. This is closed so that it cannot
    become live later, and the PR body does not claim an exploitable path.
    """
    with wired(tmp_path, {"symbol": t_symbol()}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert spec.trade_mode != 4, (
        "the Expert never sends trade_mode, so 4 (full trading) is invented; a "
        "close-only symbol would present as fully tradable"
    )


# --------------------------------------------------------------------------
# Precision: a zero that the Expert's own formatting created
# --------------------------------------------------------------------------


def test_a_thousandth_lot_step_survives_the_wire(tmp_path: Path) -> None:
    """The Expert serialised volume fields at 2 decimals, so a 0.001 step
    arrived as `0.00` and every order was refused with no reason given."""
    with wired(tmp_path, {"symbol": t_symbol(volume_step=0.001, volume_min=0.001)}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert spec.volume_step == 0.001
    assert "volume_step" not in spec.unmeasured


def test_a_small_tick_value_is_no_longer_truncated_to_zero(tmp_path: Path) -> None:
    """Four decimals turned any real value below 0.00005 into `0.0000`.

    This only closes the TRUNCATION producer. A symbol that is not in Market
    Watch still answers a true zero, and that one is still a refusal, which is
    what the test above covers.
    """
    with wired(tmp_path, {"symbol": t_symbol(tick_value="0.00004000")}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert spec.trade_tick_value == 0.00004
    assert "tick_value" not in spec.unmeasured


def test_digits_zero_is_a_legitimate_measurement(tmp_path: Path) -> None:
    """Instruments quoted in whole points report 0 digits. `or 5` turned that
    into a 5-decimal FX shape."""
    with wired(tmp_path, {"symbol": t_symbol(digits=0, point=1.0, tick_size=1.0)}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert spec.digits == 0
    assert "digits" not in spec.unmeasured


# --------------------------------------------------------------------------
# The branches a current Expert cannot reach, and one it can
# --------------------------------------------------------------------------


def test_a_non_numeric_value_is_unmeasured_not_an_exception(tmp_path: Path) -> None:
    """A garbled field must not take the adapter down.

    `float("n/a")` would raise out of `symbol()`, which is the same failure mode
    as the unsanitised row in #31: an unnamed crash where a named refusal
    belongs.
    """
    with wired(tmp_path, {"symbol": t_symbol(tick_value="n/a")}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert "tick_value" in spec.unmeasured
    assert spec.trade_tick_value == 0.0


def test_a_future_expert_that_does_send_the_four_is_believed(tmp_path: Path) -> None:
    """The unmeasured marking is not hardcoded.

    MQL4 cannot supply these today, but the adapter must not assert that
    forever: if a field arrives it is a measurement. Without this the four would
    be permanently unmeasured by fiat, which is its own kind of fabrication.
    """
    from mt4_transcripts import Transcript, ea_ok

    richer = Transcript(
        "symbol",
        ea_ok(
            "digits=5",
            "point=0.00001",
            "volume_min=0.01000000",
            "volume_max=500.00000000",
            "volume_step=0.01000000",
            "tick_value=0.67000000",
            "tick_size=0.00001",
            "contract_size=100000",
            "stops_level=10",
            "freeze_level=0",
            "spread=12",
            "trade_mode=3",
            "currency_base=EUR",
            "currency_profit=USD",
            "currency_margin=EUR",
        ),
    )
    with wired(tmp_path, {"symbol": richer}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert spec.unmeasured == frozenset()
    assert spec.trade_mode == 3
    assert spec.currency_margin == "EUR"
    assert spec.trade_tick_value == 0.67


def test_a_short_symbol_name_has_no_currencies_to_derive(tmp_path: Path) -> None:
    """An index name has nothing to slice, so the convention yields empty and
    the field is still marked unmeasured rather than looking like a reading."""
    with wired(tmp_path, {"symbol": t_symbol()}):
        spec = broker(tmp_path).symbol("US30")
    assert spec.currency_base == ""
    assert "currency_base" in spec.unmeasured


def test_an_absent_numeric_field_is_unmeasured(tmp_path: Path) -> None:
    """A reply missing a field entirely. Distinct from a zero, same verdict."""
    from mt4_transcripts import Transcript, ea_ok

    thin = Transcript("symbol", ea_ok("digits=5", "point=0.00001"))
    with wired(tmp_path, {"symbol": thin}):
        spec = broker(tmp_path).symbol("EURUSD")
    assert "tick_value" in spec.unmeasured
    assert "volume_step" in spec.unmeasured
    assert spec.digits == 5
    assert "digits" not in spec.unmeasured


def test_the_expert_emits_enough_decimals_to_carry_a_thousandth(tmp_path: Path) -> None:
    """A guard on the Expert, because the transcripts only MODEL it.

    Found while mutation-testing: reverting the Expert's formatting alone left
    every test above green, because they are driven by the transcript helper
    rather than by the `.mq4`. Transcript and Expert can drift, so the Expert's
    own source is asserted here.

    Two decimals turned a 0.001 lot step into `0.00` and a tick value below
    0.00005 into `0.0000`. Both then read as a failed measurement, which after
    this change is a refusal rather than a fabricated default: correct, but the
    order never needed refusing in the first place.
    """
    ea = (
        Path(__file__).resolve().parents[1] / "mt4" / "Experts" / "Mt4RiskBot.mq4"
    ).read_text(encoding="utf-8")
    start = ea.index("string SymbolReply(")
    body = ea[start : ea.index("\n}", start)]
    thin = []
    for field in ("volume_min", "volume_max", "volume_step", "tick_value"):
        for line in body.splitlines():
            if f'"{field}=" +' not in line:
                continue
            match = re.search(r",\s*(\d+)\)", line)
            if match is None or int(match.group(1)) < 8:
                thin.append(line.strip())
    assert thin == [], (
        "the Expert truncates these to too few decimals, which manufactures a "
        "zero the adapter then has to refuse:\n" + "\n".join(thin)
    )
