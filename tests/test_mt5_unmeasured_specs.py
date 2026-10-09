"""An MT5 spec field that was not measured must not read as a measurement.

`Mt5Broker.symbol` filled in FX-shaped defaults for anything the terminal did not
send, and never set `unmeasured`:

    trade_tick_value=float(d.get("trade_tick_value") or 1.0),
    trade_contract_size=float(d.get("trade_contract_size") or 100_000),
    point=float(d.get("point", 0.00001)),
    trade_mode=int(d.get("trade_mode", 4)),

Two things go wrong at once.

`or` fires on a legitimate ZERO as well as on absence, so a broker-reported zero
became a EURUSD-shaped number that nothing downstream could tell from a real
measurement. An XAUUSD spec arriving with zeros was sized as if gold had a
100,000 unit contract and a $1 tick: the exact ratio `CLAUDE.md` warns about
under "The per-symbol trap", where gold measures `point 0.01` and
`contract_size 100` against a 5-digit pair's `0.00001` and `100000`.

And because `unmeasured` stayed empty, `spec.unmeasured_for_sizing()` was empty
too, so the repo's own "unmeasured specs refuse, they never default" rule could
NEVER fire on MT5. The rule was implemented, tested and live on MT4 (issue #68,
re-broken and re-fixed, `broker/mt4_live.py`) and simply absent here.

`trade_mode` is the quiet one: defaulting to 4 means FULL TRADING, so a
close-only or disabled symbol presented as fully tradable. MT4 already defaults
it to 0 (MQL5's DISABLED) for exactly this reason.

## The mechanism, not a reminder to set a flag

Every numeric field goes through one `measure()` reader that records the field as
unmeasured and returns 0.0. There is no path that reads a spec field and forgets,
because there is no other reader: the last test in this file is a source guard
that fails if one is added. `positive=` marks the fields where zero is not a
possible measurement, only a failed one, so a real zero (`digits` on a
whole-point instrument, a zero `stops_level`) still survives as a measurement.

The names recorded are the CANONICAL spec names (`tick_value`, not
`trade_tick_value`), because `SPEC_SIZING_FIELDS` is spelled that way and
`unmeasured_for_sizing()` intersects against it. A set full of raw MT5 keys would
look populated and gate nothing.

Nothing here is verified against a live MT5 terminal.
"""

from __future__ import annotations

import re
from pathlib import Path

from test_mt5_adapter import FakeMt5, _nt

from straightedge.broker.mt5_live import Mt5Broker
from straightedge.models import SPEC_SIZING_FIELDS
from straightedge.sizing import lots_for_risk, money_per_lot_at_stop

MT5_SRC = Path(__file__).resolve().parents[1] / "src" / "straightedge" / "broker" / "mt5_live.py"

FULL = dict(
    name="EURUSD",
    digits=5,
    point=0.00001,
    trade_tick_size=0.00001,
    trade_tick_value=1.0,
    trade_contract_size=100000.0,
    volume_min=0.01,
    volume_max=100.0,
    volume_step=0.01,
    trade_stops_level=10,
    trade_freeze_level=0,
    filling_mode=2,
    currency_base="EUR",
    currency_profit="USD",
    currency_margin="USD",
    trade_mode=4,
    visible=True,
    spread=10,
)


class SpecMt5(FakeMt5):
    """A terminal that answers whatever spec the test hands it."""

    def __init__(self, fields: dict | None, **kw) -> None:
        super().__init__(**kw)
        self.fields = fields

    def symbol_info(self, name):
        del name
        if self.fields is None:
            return None
        return _nt(**self.fields)


def _spec(fields: dict | None):
    fake = SpecMt5(fields)
    broker = Mt5Broker(mt5=fake)
    broker.connect()
    return broker.symbol("XAUUSD")


class TestAZeroIsNotAMeasurement:
    def test_a_zero_tick_value_is_recorded_unmeasured(self) -> None:
        spec = _spec({**FULL, "name": "XAUUSD", "trade_tick_value": 0.0})

        assert "tick_value" in spec.unmeasured, spec.unmeasured
        assert spec.trade_tick_value == 0.0, (
            "a broker-reported zero became a EURUSD-shaped 1.0 tick value"
        )

    def test_a_zero_contract_size_is_recorded_unmeasured(self) -> None:
        spec = _spec({**FULL, "name": "XAUUSD", "trade_contract_size": 0.0})

        assert "contract_size" in spec.unmeasured, spec.unmeasured
        assert spec.trade_contract_size != 100_000, (
            "gold was given a 100,000 unit contract size, the #68 defect exactly"
        )

    def test_a_zero_point_is_recorded_unmeasured(self) -> None:
        spec = _spec({**FULL, "name": "XAUUSD", "point": 0.0})

        assert "point" in spec.unmeasured, spec.unmeasured
        assert spec.point == 0.0

    def test_the_gold_shaped_case_refuses_instead_of_sizing(self) -> None:
        """The whole point: the refusal rule must be able to fire on MT5.

        An XAUUSD spec arriving with zeros used to be sized as EURUSD. Now the
        sizing-critical fields are named as unmeasured and sizing returns 0 lots.
        """
        spec = _spec(
            {
                **FULL,
                "name": "XAUUSD",
                "trade_tick_value": 0.0,
                "trade_contract_size": 0.0,
            }
        )

        assert spec.unmeasured_for_sizing(), (
            "no sizing-critical field was named, so the repo's own "
            "unmeasured-specs-refuse rule still cannot fire on MT5"
        )
        assert lots_for_risk(10_000, 0.005, 4000.0, 3980.0, spec) == 0.0

    def test_the_recorded_names_are_the_ones_the_gate_reads(self) -> None:
        """A set of raw MT5 keys would look populated and gate nothing.

        `unmeasured_for_sizing()` intersects with `SPEC_SIZING_FIELDS`, so
        `trade_tick_value` in that set is invisible to the gate while still
        reading as "something was unmeasured".
        """
        spec = _spec(
            {
                **FULL,
                "trade_tick_value": 0.0,
                "trade_tick_size": 0.0,
                "volume_step": 0.0,
            }
        )

        assert {"tick_value", "tick_size", "volume_step"} <= spec.unmeasured
        assert "trade_tick_value" not in spec.unmeasured, (
            "the raw MT5 key was recorded instead of the canonical spec name"
        )
        assert spec.unmeasured_for_sizing() <= SPEC_SIZING_FIELDS


class TestAbsenceIsNotAMeasurementEither:
    def test_a_missing_field_is_recorded_unmeasured(self) -> None:
        fields = {k: v for k, v in FULL.items() if k != "trade_tick_value"}
        spec = _spec(fields)

        assert "tick_value" in spec.unmeasured

    def test_trade_mode_defaults_to_disabled_not_to_full_trading(self) -> None:
        """4 is FULL TRADING. A symbol we could not ask about is not that.

        MT4 already closes this the same way and for the same reason.
        """
        fields = {k: v for k, v in FULL.items() if k != "trade_mode"}
        spec = _spec(fields)

        assert spec.trade_mode == 0, (
            "an unmeasured trade_mode presented as 4, full trading; a close-only "
            "or disabled symbol would read as fully tradable"
        )
        assert "trade_mode" in spec.unmeasured

    def test_a_second_symbol_info_returning_none_does_not_yield_a_full_spec(
        self,
    ) -> None:
        """The invisible-symbol retry path, which had no guard at all.

        `symbol()` re-reads after `select_symbol`, and `_asdict(None)` is `{}`.
        Every `or DEFAULT` then fired at once, so a terminal that answered
        NOTHING produced a complete, plausible, entirely invented EURUSD spec.
        """

        class VanishingMt5(SpecMt5):
            def __init__(self) -> None:
                super().__init__({**FULL, "name": "XAUUSD", "visible": False})
                self.calls = 0

            def symbol_info(self, name):
                self.calls += 1
                if self.calls == 1:
                    return _nt(**self.fields)
                return None

        fake = VanishingMt5()
        broker = Mt5Broker(mt5=fake)
        broker.connect()
        spec = broker.symbol("XAUUSD")

        assert fake.calls == 2, "the retry path was not exercised"
        assert spec.unmeasured_for_sizing(), (
            "a terminal that answered nothing produced a fully measured spec"
        )
        assert lots_for_risk(10_000, 0.005, 4000.0, 3980.0, spec) == 0.0


class TestARealMeasurementSurvives:
    def test_a_complete_spec_has_nothing_unmeasured(self) -> None:
        """Positive control. Refusing everything would pass every test above."""
        spec = _spec(dict(FULL))

        assert spec.unmeasured == frozenset(), spec.unmeasured
        assert spec.trade_tick_value == 1.0
        assert spec.trade_contract_size == 100_000
        assert spec.point == 0.00001
        assert spec.trade_mode == 4
        assert lots_for_risk(10_000, 0.005, 1.1000, 1.0950, spec) > 0

    def test_a_legitimate_zero_is_kept_as_a_measurement(self) -> None:
        """Zero IS a real reading for some fields, and must not be discarded.

        An index quoted in whole points has `digits = 0`; a broker with no
        minimum stop distance reports `stops_level = 0`; a zero spread is a real,
        if lucky, measurement. Marking those unmeasured would refuse to trade
        instruments that are perfectly well described.
        """
        spec = _spec(
            {
                **FULL,
                "name": "US30",
                "digits": 0,
                "trade_stops_level": 0,
                "trade_freeze_level": 0,
                "spread": 0,
            }
        )

        assert spec.digits == 0
        assert spec.trade_stops_level == 0
        assert not {"digits", "stops_level", "freeze_level", "spread"} & spec.unmeasured
        assert spec.unmeasured_for_sizing() == frozenset()
        assert lots_for_risk(10_000, 0.005, 1.1000, 1.0950, spec) > 0

    def test_a_gold_shaped_spec_that_IS_measured_sizes_from_its_own_numbers(
        self,
    ) -> None:
        """The other half of the per-symbol trap: measured gold must size as gold."""
        spec = _spec(
            {
                **FULL,
                "name": "XAUUSD",
                "digits": 2,
                "point": 0.01,
                "trade_tick_size": 0.01,
                "trade_tick_value": 1.0,
                "trade_contract_size": 100.0,
            }
        )

        assert spec.unmeasured == frozenset()
        assert spec.trade_contract_size == 100.0
        assert spec.point == 0.01
        # And it SIZES. Asserting the spec fields alone left FX as the only
        # instrument this file proves can size, and FX is precisely where the
        # defect was invisible: the fabricated spec errs only when the real
        # tick_value/tick_size ratio differs from 1e5, which is exactly where a
        # 5-digit pair sits. Gold is the case that moved, so gold is the case
        # that has to size here. $40 of real risk against a $50 budget.
        lots = lots_for_risk(10_000.0, 0.005, 4000.0, 3980.0, spec)
        assert lots == 0.02, lots
        assert money_per_lot_at_stop(4000.0, 3980.0, spec) * lots == 40.0


def _symbol_code() -> str:
    """`Mt5Broker.symbol`, executable lines only, with the docstring removed.

    The first version of this guard scanned the whole method and went red on the
    DOCSTRING, which quotes the old defaulting expressions deliberately so the
    next reader knows what was wrong. A guard that cannot tell prose from an
    executable read is committing the mistake it exists to catch, so it strips the
    docstring and reads code.
    """
    src = MT5_SRC.read_text(encoding="utf-8")
    start = src.index("    def symbol(self, name: str) -> SymbolSpec:")
    end = src.index("    def tick(self, name: str) -> Tick:", start)
    body = src[start:end]
    first = body.index('"""')
    second = body.index('"""', first + 3)
    code = body[:first] + body[second + 3 :]
    assert "SymbolSpec(" in code, "the docstring strip ate the constructor"
    return code


class TestThereIsNoOtherReader:
    def test_every_spec_field_goes_through_measure(self) -> None:
        """The mechanism, guarded. A new `d.get(` here is a new way to forget.

        This is what makes "unmeasured" unrepresentable rather than something to
        remember: the reader cannot be bypassed, because a bypass fails here.
        """
        code = _symbol_code()

        leaked = re.findall(r"d\.get\(", code)
        assert leaked == [], (
            f"{len(leaked)} raw d.get( read(s) remain in Mt5Broker.symbol; every "
            "spec field must go through measure() or it can silently default"
        )
        assert "or 1.0" not in code
        assert "or 100_000" not in code

        # Subscripts too, NOT only `d.get(`. The `d.get(`-only form of this
        # guard was decorative for the route that matters: replacing
        # `measure("volume_min", ...)` with `float(d["volume_min"]) if
        # "volume_min" in d else 0.01` reintroduces issue #68 on a field that
        # IS in SPEC_SIZING_FIELDS, and it passed every test in this file AND
        # the entire 983-test suite. A guard that cannot observe the bypass it
        # names is not a guard.
        #
        # Three literal keys are read raw on purpose and are allowlisted BY
        # NAME, so a fourth has to be added here deliberately rather than
        # arriving unnoticed. None of the three is a sizing input:
        # `visible` and `trade_mode` carry their own recording branches (a
        # measure() returning 0.0 cannot express DISABLED-vs-unmeasured for an
        # enum), and `name` is the symbol label, not a measurement.
        ALLOWED_RAW_KEYS = {"visible", "trade_mode", "name"}
        raw_keys = set(re.findall(r'd\[\s*"([^"]+)"\s*\]', code))
        unexpected = raw_keys - ALLOWED_RAW_KEYS
        assert unexpected == set(), (
            f"raw d[...] read(s) for {sorted(unexpected)} in Mt5Broker.symbol; a "
            "spec field read by subscript bypasses measure() and can silently "
            "default, which is exactly how #68 was re-broken. Route it through "
            "measure()/text(), or allowlist it here with a reason."
        )

        # `d[key]` with the VARIABLE key is the two readers themselves. Pinning
        # the count stops a third variable-key reader being added beside them,
        # which the literal-key allowlist above cannot see.
        assert len(re.findall(r"d\[key\]", code)) == 2, (
            "expected exactly two d[key] reads, one in measure() and one in "
            "text(); a third is a new reader that records nothing"
        )

    def test_the_fx_shaped_defaults_are_gone(self) -> None:
        code = _symbol_code()

        assert "0.00001" not in code, (
            "a 5-digit FX point is still hardcoded as a fallback in symbol()"
        )
        assert "trade_mode\", 4" not in code.replace("'", '"')

    def test_the_guard_can_see_a_bypass(self) -> None:
        """A positive control on the instrument itself.

        If the docstring strip ever removed the whole method, both guards above
        would pass on an empty string. This shows the slice still contains the
        reads it is judging.
        """
        code = _symbol_code()

        assert "measure(" in code
        assert "unmeasured" in code
        assert re.search(r"d\.get\(", "x = d.get('k')") is not None
        # And the subscript pattern, on a sample that MUST match, so the
        # allowlist assertion above cannot pass by never matching anything.
        assert re.findall(r'd\[\s*"([^"]+)"\s*\]', 'x = d["volume_min"]') == [
            "volume_min"
        ]
        assert len(re.findall(r"d\[key\]", "v = d[key]")) == 1


class TestFreezeLevelIsMeasuredAndNotEnforced:
    """`trade_freeze_level` is populated everywhere and read nowhere (#89).

    `docs/MT5-API.md` said `SYMBOL_TRADE_STOPS_LEVEL` and
    `SYMBOL_TRADE_FREEZE_LEVEL` were "both enforced before send". Only the first
    is. The half-truth was the worse part: a reader who checked `stops_level`,
    found the refusal in `risk.py`, and inferred the rest would believe a guard
    that does not exist, on a repo that moves real money.

    The doc now states what the code does. This pins the state the doc
    describes, so the two cannot silently re-diverge in either direction:

    * if a READER of `trade_freeze_level` appears, the enforcement claim became
      true and the doc must be restored to say so;
    * if a POPULATION site disappears, the field stopped being measured and the
      doc's "measured and recorded" is what became false.

    Either way this test sends the next person to `docs/MT5-API.md`, which is
    the whole point of pinning a documented property rather than trusting prose.
    """

    #: Every site that WRITES the field, plus its declaration. MEASURED off the
    #: tree, not remembered; the first draft of this constant said mt5_live.py
    #: had one site and this test red on its own author, which is the behaviour
    #: wanted from it.
    #:
    #:   models.py          the dataclass field declaration
    #:   broker/mt5_live.py 2: the MT5 SYMBOL property name handed to measure(),
    #:                      and the assignment onto the spec
    #:   broker/mt4_live.py the assignment onto the spec
    #:   broker/paper.py    2: one per spec constructor
    #:
    #: Every one is a WRITE or a declaration. None is a read by a decision path,
    #: which is what the next test asserts separately.
    POPULATION = {
        "models.py": 1,
        "broker/mt5_live.py": 2,
        "broker/mt4_live.py": 1,
        "broker/paper.py": 2,
    }

    @staticmethod
    def _src() -> Path:
        return Path(__file__).resolve().parents[1] / "src" / "straightedge"

    def _sites(self) -> dict[str, int]:
        src = self._src()
        found: dict[str, int] = {}
        for path in sorted(src.rglob("*.py")):
            n = path.read_text(encoding="utf-8").count("trade_freeze_level")
            if n:
                found[path.relative_to(src).as_posix()] = n
        return found

    def test_the_population_sites_are_these_and_nothing_else(self) -> None:
        """The denominator, printed, so a zero below means something."""
        sites = self._sites()
        print(f"trade_freeze_level sites: {sum(sites.values())} in {len(sites)} file(s) {sites}")
        assert sites == self.POPULATION, (
            "trade_freeze_level gained or lost a site. If something now READS "
            "it, the pre-send freeze check exists and docs/MT5-API.md must say "
            f"so again (issue #89). Found: {sites!r}"
        )

    def test_no_decision_module_reads_the_freeze_level(self) -> None:
        """The claim the doc used to make, asserted as the absence it is.

        `risk.py`, `engine.py` and `desk.py` are where a pre-send guard would
        have to live, because they are the modules that decide whether to send.
        A hit in any of them means the guard arrived.
        """
        src = self._src()
        deciders = ("risk.py", "engine.py", "desk.py")
        hits = {
            name: (src / name).read_text(encoding="utf-8").count("trade_freeze_level")
            for name in deciders
        }
        print(f"freeze_level reads in the decision modules: {hits}")
        assert sum(hits.values()) == 0, (
            "a decision module now reads trade_freeze_level, so the pre-send "
            f"freeze check exists; update docs/MT5-API.md (issue #89): {hits!r}"
        )

    def test_the_scanner_can_find_a_reader(self) -> None:
        """Positive control. An absence is evidence only if a presence shows.

        Both assertions above are "we found nothing". That reads identically to
        a scanner pointed at the wrong tree or spelling the field wrong, which
        is the failure this repo keeps finding. So: the same needle, counted
        over a file that definitely contains it, and over a sample that must
        match.
        """
        models = (self._src() / "models.py").read_text(encoding="utf-8")
        assert models.count("trade_freeze_level") == 1, (
            "the scanner cannot find the field in its own declaration, so the "
            "zero counts above measured the instrument"
        )
        assert "trade_freeze_level".count("freeze") == 1
        # And the enforced half IS findable, which is what makes "only
        # stops_level is enforced" a measurement rather than an assumption.
        risk = (self._src() / "risk.py").read_text(encoding="utf-8")
        print(f"stops_level references in risk.py: {risk.count('stops_level')}")
        assert risk.count("stops_level") >= 1, (
            "stops_level is not in risk.py either, so this scanner proves nothing"
        )
