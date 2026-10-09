"""One test per refusal reason, each asserting the NAMED reason (#11).

This file is the ROSTER. `allowed is False` cannot tell you a control has
stopped testing anything; `reason == (the name)` can, so every case
here compares the reason string, and the desk cases compare the structured
journal record from #29 rather than the prose the chat gets back.

REASONS is the denominator. It is asserted against the reasons actually
present in risk.py, so adding a reason to the module without adding a case
here fails the suite instead of quietly lowering the count.

That assertion USED to read risk.py as text with a regex that required a
closing quote, and issue #61 recorded what it therefore could not see: a reason
built by concatenation, `reason="spec_not_measured:" + join(...)`. The roster
read green while the module carried a reason no case covered, which is the
denominator quietly ceasing to be one. It is now read from the AST by
tests/refusal_scan.py, which also FAILS on any site it cannot resolve rather
than skipping it, and the count went 22 -> 24 the moment it could see: the
missing two were `spec_not_measured` (the reason #61 named) and
`deviation_below_spread` (added with the gate, in the same shape, deliberately,
because the fix has to be proven against the form that broke it). #61 closed.

One reason cannot be produced by RiskManager at all, and that is a finding,
not a gap to paper over. It is pinned by a test that FAILS if it ever becomes
reachable, so the roster cannot rot into a test against dead code:

- `halted` (risk.py:252) is an `or` fallback for a halted manager with an
  empty reason. Every site that sets the halt flag also sets a reason, so no
  public call can produce it. See test_halted_fallback_is_unreachable.

`size_exceeds_risk` was the second one and is not unreachable any more.
Issue #55 gave the gate a cap the sizer cannot compute, the room left before
the daily-loss and drawdown halts, taken from the persisted snapshot, so the
line fires on real inputs. The pin that held it dead,
test_size_exceeds_risk_is_dominated_by_size_zero, is GONE ON PURPOSE and is
replaced below by a case that names the reason. An assertion flipped quietly
is the thing this repo hunts; this one was flipped deliberately, in the PR
that made the line reachable, and it is named in that PR.

That pin carries its own lesson, recorded here because it is the same mistake
in a different place: it did NOT fail when the guard became reachable. It
re-implemented the guard's old arithmetic from lots_for_risk instead of
calling evaluate, so what it actually measured was the SIZER, which still
behaves exactly as it did. A pin on a dead line has to call the line.

The sweep, the case where the gate refuses a size the sizer allowed, and the
proof that the disagreement comes from state the sizer never sees are in
tests/test_size_guard.py.
"""

from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path

import pytest

from straightedge.broker.paper import PaperBroker, default_spec
from straightedge.config import BotConfig
from straightedge.models import Account, Position, Side, Signal, SignalKind, Tick
from straightedge.risk import RiskManager, day_key
from straightedge.sizing import lots_for_risk
from straightedge.state import snapshot_path_for
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand
from straightedge.engine import Engine

from refusal_scan import scan_decision_reasons, scan_reasons


MAGIC = BotConfig().risk.magic
WED_NOON = datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)
WED_EARLY = datetime(2024, 1, 3, 5, 0, tzinfo=timezone.utc)  # before 07:00 UTC

# Every refusal reason RiskManager can name. Asserted against risk.py itself
# by test_roster_covers_every_reason_in_the_module, so the denominator is
# measured and not carried forward from an issue body.
REASONS = (
    "spec_not_measured",
    # Raised by engine.py, not risk.py, and its behavioural case lives with the
    # gate that raises it (tests/test_working_orders_count_as_exposure.py,
    # TestAnUnmeasuredReadRefuses) rather than being duplicated here: the
    # helpers in this file drive `rm.evaluate`, which is not the surface this
    # refusal comes out of. It is rostered here because the roster is the
    # DENOMINATOR of operator-visible refusals, and that population is no
    # longer confined to one module.
    "orders_unmeasured",
    "deviation_below_spread",
    "state_unreadable",
    "state_unwritable",
    "halt_file",
    "halted",
    "trade_not_allowed",
    "live_not_accepted",
    "daily_loss",
    "max_drawdown",
    "no_signal",
    "outside_session",
    "max_positions",
    "already_in_symbol",
    "currency_exposure",
    "sl_required",
    "rr_below_min",
    "stops_level",
    "spread_too_wide",
    "margin_buffer",
    "size_zero",
    "size_exceeds_risk",
    "max_trades_per_day",
    "exposure_unmeasured",
)

# Not reachable through any public RiskManager call. See the module docstring.
UNREACHABLE = ("halted", "exposure_unmeasured")

# Reasons whose literal is only the HEAD of the runtime string: a payload is
# appended, so a case asserts startswith and the payload separately. Pinned
# against the scanner, so a reason that BECOMES prefix-shaped (or stops being)
# cannot slip past a suite full of equality assertions.
PREFIX_REASONS = ("spec_not_measured", "deviation_below_spread")

# Sites in risk.py that pass a reason through rather than naming one, exactly as
# the scanner unparses them. Pinned, not ignored: a scanner with a silent ignore
# list is the #61 defect again. A NEW forwarding expression fails here and gets
# looked at, which is the point.
FORWARDED_REASON_EXPRESSIONS = (
    "reason",
    "self._halt_reason",
    "self._state_integrity_reason",
    "sticky",
)


def _acct(equity: float = 10_000, **kw) -> Account:
    return Account(
        login=1,
        balance=kw.get("balance", equity),
        equity=equity,
        margin=kw.get("margin", 0.0),
        margin_free=kw.get("margin_free", equity),
        profit=0.0,
        leverage=100,
        currency="USD",
        trade_allowed=kw.get("trade_allowed", True),
        trade_expert=kw.get("trade_expert", True),
        trade_mode=kw.get("trade_mode", 0),
    )


def _sig(kind=SignalKind.BUY, symbol="EURUSD", entry=1.10, sl=1.095, tp=1.1125, atr=0.003) -> Signal:
    return Signal(kind, symbol, entry, sl, tp, atr, reason="test")


def _tick(bid=1.0999, ask=1.1001) -> Tick:
    return Tick(time=0, bid=bid, ask=ask)


def _pos(ticket: int, symbol: str, side=Side.BUY) -> Position:
    return Position(ticket, symbol, side, 0.1, 1.1, 1.09, 1.12, 1.1, 0, magic=MAGIC)


def _cfg(tmp_path: Path, **kw) -> BotConfig:
    """Keep the equity snapshot inside tmp_path, per issue #7."""
    cfg = BotConfig(**kw)
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.session.enabled = False
    return cfg


def _gate(rm: RiskManager, **kw):
    """One evaluate() call with every lever exposed as a keyword."""
    return rm.evaluate(
        account=kw.get("account", _acct()),
        signal=kw.get("signal", _sig()),
        spec=kw.get("spec", default_spec("EURUSD")),
        tick=kw.get("tick", _tick()),
        positions=kw.get("positions", []),
        orders=kw.get("orders", []),
        now=kw.get("now", WED_NOON),
        manual=kw.get("manual", True),
    )


# --- the denominator itself -------------------------------------------------


def test_roster_covers_every_reason_in_the_module() -> None:
    """The roster is measured against risk.py, never against the issue body.

    A reason added to risk.py without a case in this file fails HERE, which is
    the only way a per-reason suite stays a denominator instead of becoming a
    snapshot of the day it was written.
    """
    root = Path(__file__).resolve().parents[1] / "src" / "straightedge"
    src = root / "risk.py"
    scan = scan_reasons(src.read_text(encoding="utf-8"))
    # engine.py builds RiskDecision too, as of the working-orders gate. On main
    # it built ZERO, so reading risk.py alone was a COMPLETE denominator; it
    # stopped being complete because the population moved, not because this
    # scanner changed, and nothing here would have noticed. A refusal the
    # operator sees as `refused: <reason>` must have a case in this file no
    # matter which module constructs it.
    #
    # Narrow scan on purpose: engine.py has a dozen `reason=` kwargs that are
    # journal fields rather than refusals, and every `-> str` method of Engine
    # returns operator prose, so the broad scan_reasons pass would corrupt the
    # roster instead of widening it.
    engine_scan = scan_decision_reasons((root / "engine.py").read_text(encoding="utf-8"))
    assert not engine_scan.unresolved, (
        "the scanner could not read a RiskDecision reason in engine.py, so the "
        "roster is not a denominator: " + repr(list(engine_scan.unresolved))
    )
    assert engine_scan.names, (
        "engine.py names no RiskDecision reason; if that is now true, drop this "
        "scan rather than leaving a check that cannot fail"
    )
    # FIRST, before any comparison: did the scanner manage to read every site?
    # A reason it could not resolve is not a passed check, and a denominator
    # built on a partial read is the #61 defect wearing an AST.
    assert not scan.unresolved, (
        "the reason scanner could not read these sites in risk.py, so the "
        "roster below is not a denominator: " + repr(list(scan.unresolved))
    )
    assert scan.forwarded == FORWARDED_REASON_EXPRESSIONS, (
        "risk.py forwards a reason through an expression this file has not "
        f"pinned: found {scan.forwarded!r}, pinned "
        f"{FORWARDED_REASON_EXPRESSIONS!r}"
    )
    found = set(scan.names) | set(engine_scan.names)
    found.discard("ok")
    assert set(PREFIX_REASONS) == scan.prefixes, (
        "prefix-shaped reasons changed: risk.py has "
        f"{sorted(scan.prefixes)}, this file pins {sorted(PREFIX_REASONS)}"
    )
    missing = found - set(REASONS)
    assert not missing, (
        "risk.py or engine.py names refusal reasons with no case here: "
        + repr(sorted(missing))
    )
    stale = set(REASONS) - found
    assert not stale, (
        "roster names reasons neither risk.py nor engine.py has: " + repr(sorted(stale))
    )
    # No hardcoded total. The count is DERIVED from what risk.py actually
    # names, so adding a reason to the module cannot be satisfied by editing a
    # number here. A literal count is a fact about the day it was written, and
    # this roster exists precisely to stop the denominator drifting.
    assert len(REASONS) == len(set(REASONS)) == len(found), (
        "roster size must equal the reasons risk.py and engine.py name: "
        f"roster={len(REASONS)} modules={len(found)}"
    )
    assert set(UNREACHABLE) <= set(REASONS), "UNREACHABLE names a reason the roster does not"


# --- 1. the halt family: reasons that come out of circuit() -----------------


def test_state_unreadable_names_the_reason(tmp_path: Path) -> None:
    """A corrupt snapshot is COULD NOT MEASURE, so the gate fails closed."""
    cfg = _cfg(tmp_path)
    snapshot_path_for(cfg.journal_path).write_text("{", encoding="utf-8")
    rm = RiskManager(cfg, halt_dir=tmp_path)
    d = _gate(rm)
    assert d.reason == "state_unreadable"
    assert rm.circuit_reason(_acct(), WED_NOON) == "state_unreadable"


def test_state_unwritable_names_the_reason(tmp_path: Path, monkeypatch) -> None:
    """A snapshot that cannot be written loses the budget on restart."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)

    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("straightedge.state.os.replace", boom)
    assert _gate(rm).reason == "state_unwritable"


def test_halt_file_names_the_reason(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rm = RiskManager(cfg, halt_dir=tmp_path)
    Path(cfg.risk.halt_file).write_text("operator\n", encoding="utf-8")
    d = _gate(rm)
    assert d.reason == "halt_file"
    assert d.halt and d.flatten
    assert rm.circuit_reason(_acct(), WED_NOON) == "halt_file"


def test_trade_not_allowed_names_the_reason(tmp_path: Path) -> None:
    """Terminal-level block. Two sites, and BOTH are named here: circuit()

    decides whether a trade may go, circuit_reason() decides whether the LLM
    may stage at all, and the second one had no test at all.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    assert _gate(rm, account=_acct(trade_allowed=False)).reason == "trade_not_allowed"
    assert rm.circuit_reason(_acct(trade_allowed=False), WED_NOON) == "trade_not_allowed"


def test_trade_not_allowed_covers_the_expert_flag_too(tmp_path: Path) -> None:
    """trade_expert=False is the same refusal; the or-branch needs its own case."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    assert _gate(rm, account=_acct(trade_expert=False)).reason == "trade_not_allowed"
    assert rm.circuit_reason(_acct(trade_expert=False), WED_NOON) == "trade_not_allowed"


@pytest.mark.parametrize("mode", ["mt5", "mt4"])
def test_live_not_accepted_names_the_reason(tmp_path: Path, mode: str) -> None:
    """Real-money terminal without the typed acceptance. Both sites."""
    cfg = _cfg(tmp_path, mode=mode, live_accepted=False)
    rm = RiskManager(cfg, halt_dir=tmp_path)
    assert _gate(rm, account=_acct(trade_mode=2)).reason == "live_not_accepted"
    assert rm.circuit_reason(_acct(trade_mode=2), WED_NOON) == "live_not_accepted"


def test_daily_loss_names_the_reason(tmp_path: Path) -> None:
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    rm.observe(_acct(10_000), WED_NOON)
    d = _gate(rm, account=_acct(9_700))  # -3 pct against a 2 pct cap
    assert d.reason == "daily_loss"
    assert d.halt


def test_daily_loss_names_the_reason_on_the_staging_gate(tmp_path: Path) -> None:
    """circuit_reason(), the gate deciding whether the LLM may stage at all."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    rm.observe(_acct(10_000), WED_NOON)
    assert rm.circuit_reason(_acct(9_700), WED_NOON) == "daily_loss"


def test_max_drawdown_names_the_reason(tmp_path: Path) -> None:
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    day2 = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
    rm.observe(_acct(10_000), WED_NOON)
    rm.observe(_acct(9_000), day2)  # new UTC day: daily clock resets, peak does not
    d = _gate(rm, account=_acct(8_900), now=day2)
    assert d.reason == "max_drawdown"
    assert d.halt


def test_max_drawdown_names_the_reason_on_the_staging_gate(tmp_path: Path) -> None:
    """circuit_reason() line 262 had no test; a fresh manager is needed.

    circuit() LATCHES the halt, so asserting both sites on one manager would
    read the latch on the second call and never enter the drawdown branch.
    Two managers is the only way this case can actually reach the line.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    day2 = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
    rm.observe(_acct(10_000), WED_NOON)
    rm.observe(_acct(9_000), day2)
    assert rm.circuit_reason(_acct(8_900), day2) == "max_drawdown"
    assert rm.halt_reason == "", "circuit_reason must not latch a market verdict"


def test_a_latched_halt_is_reported_by_the_staging_gate(tmp_path: Path) -> None:
    """risk.py:252, the latched-halt branch of circuit_reason().

    It returns the latched reason, never the bare string halted; see
    test_halted_fallback_is_unreachable for why that fallback cannot fire.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    rm.observe(_acct(10_000), WED_NOON)
    assert _gate(rm, account=_acct(9_700)).reason == "daily_loss"  # latch it
    assert rm.is_halted
    assert rm.circuit_reason(_acct(10_000), WED_NOON) == "daily_loss"


# --- 2. the evaluate() gates, in the order risk.py applies them -------------


def test_no_signal_names_the_reason(tmp_path: Path) -> None:
    """A FLAT signal, or one with no side, is not an order.

    Asserted directly against RiskManager because neither production caller
    can reach it: the auto leg returns on FLAT before it gets here
    (engine.py:463) and the desk only ever builds BUY or SELL. It is a
    defensive guard on a public method, so the public method is where it is
    tested; see the PR body for why that is a finding and not a fix.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    flat = Signal(SignalKind.FLAT, "EURUSD", 1.10, 1.095, 1.1125, 0.003)
    assert flat.side is None
    assert _gate(rm, signal=flat).reason == "no_signal"


def test_outside_session_names_the_reason(tmp_path: Path) -> None:
    """The session window binds the AUTO leg only; manual=True bypasses it."""
    cfg = _cfg(tmp_path)
    cfg.session.enabled = True
    rm = RiskManager(cfg, halt_dir=tmp_path)
    assert _gate(rm, now=WED_EARLY, manual=False).reason == "outside_session"


def test_outside_session_is_not_applied_to_a_manual_order(tmp_path: Path) -> None:
    """The negative half: the same clock with manual=True must NOT refuse.

    Without this, a gate stuck on always-refuse would pass the case above.
    """
    cfg = _cfg(tmp_path)
    cfg.session.enabled = True
    rm = RiskManager(cfg, halt_dir=tmp_path)
    assert _gate(rm, now=WED_EARLY, manual=True).reason == "ok"


def test_outside_session_covers_the_weekend(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.session.enabled = True
    rm = RiskManager(cfg, halt_dir=tmp_path)
    saturday = datetime(2024, 1, 6, 12, 0, tzinfo=timezone.utc)
    assert _gate(rm, now=saturday, manual=False).reason == "outside_session"


def test_max_positions_names_the_reason(tmp_path: Path) -> None:
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    ours = [_pos(1, "GBPUSD"), _pos(2, "USDCHF"), _pos(3, "AUDCAD")]
    assert len(ours) == BotConfig().risk.max_positions
    assert _gate(rm, positions=ours).reason == "max_positions"


def test_max_positions_counts_only_our_magic(tmp_path: Path) -> None:
    """Another EA at the same broker must not consume our slot count."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    theirs = [
        Position(9, "GBPUSD", Side.BUY, 0.1, 1.2, 1.19, 1.22, 1.2, 0, magic=MAGIC + 1)
        for _ in range(5)
    ]
    assert _gate(rm, positions=theirs).reason == "ok"


def test_max_trades_per_day_names_the_reason(tmp_path: Path) -> None:
    """The daily send cap refuses with its own name, not `size_zero`.

    Added when #58 landed the cap. The roster is the denominator, so a reason
    in the module with no case here fails the suite rather than quietly
    lowering the count -- which is exactly how this test came to be written.
    """
    cfg = _cfg(tmp_path)
    cfg.risk.max_trades_per_day = 1
    rm = RiskManager(cfg, halt_dir=tmp_path)
    # Seed the counter under the key for the timestamp `_gate` actually passes,
    # not wall-clock today. A mismatched key makes the first evaluate roll the
    # day and zero the count -- correct behaviour, and it would have made this
    # assertion fail for a reason that has nothing to do with the cap.
    rm.snapshot.day_key = day_key(WED_NOON)
    rm.snapshot.trades_today = 1
    assert _gate(rm).reason == "max_trades_per_day"


def test_already_in_symbol_names_the_reason(tmp_path: Path) -> None:
    """One position per symbol. Below max_positions, so only this gate fires."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    assert _gate(rm, positions=[_pos(1, "EURUSD")]).reason == "already_in_symbol"


def test_currency_exposure_names_the_reason(tmp_path: Path) -> None:
    """Two long EUR legs already; a third would be three deep on one currency."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    ours = [_pos(1, "EURGBP"), _pos(2, "EURJPY")]
    assert _gate(rm, positions=ours).reason == "currency_exposure"


def test_sl_required_names_the_reason(tmp_path: Path) -> None:
    """No stop is not a trade. The whole desk is built on there being one."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    no_stop = _sig(sl=0.0)
    assert _gate(rm, signal=no_stop).reason == "sl_required"


def test_sl_required_covers_a_zero_risk_distance(tmp_path: Path) -> None:
    """The or-branch: a positive stop sitting exactly on the entry."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    flat_stop = _sig(entry=1.10, sl=1.10, tp=1.11)
    assert flat_stop.risk_distance == 0
    assert _gate(rm, signal=flat_stop).reason == "sl_required"


def test_rr_below_min_names_the_reason(tmp_path: Path) -> None:
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    thin = _sig(entry=1.10, sl=1.095, tp=1.1050)  # rr 1.0 against a 1.5 floor
    assert abs(thin.rr - 1.0) < 1e-9
    assert _gate(rm, signal=thin).reason == "rr_below_min"


def test_stops_level_names_the_reason(tmp_path: Path) -> None:
    """Broker minimum stop distance. rr is kept healthy so only this fires."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    spec = default_spec("EURUSD")
    tight = _sig(entry=1.10, sl=1.09995, tp=1.10015)  # 5e-05 against a 1e-04 floor
    assert tight.risk_distance < spec.min_stop_distance()
    assert tight.rr >= BotConfig().risk.min_rr
    assert _gate(rm, signal=tight, spec=spec).reason == "stops_level"


def test_spread_too_wide_names_the_reason(tmp_path: Path) -> None:
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    wide = _tick(bid=1.0990, ask=1.1010)  # 0.002 against 0.15 * atr = 0.00045
    assert wide.spread > 0.15 * 0.003
    assert _gate(rm, tick=wide).reason == "spread_too_wide"


def test_spread_gate_is_skipped_when_atr_is_unknown(tmp_path: Path) -> None:
    """atr=0 means no yardstick, so the spread gate must not fire on a guess."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    wide = _tick(bid=1.0990, ask=1.1010)
    # The tick is deliberately WIDE, because that is what would trip
    # spread_too_wide if the gate guessed at the missing ATR. It is also 200
    # points against the 20-point default deviation, which cannot fill at all,
    # and deviation_below_spread says so WITHOUT an ATR: the spread against the
    # tolerance is arithmetic, not a guess. So this names the gate that had to
    # abstain, rather than asserting a blanket ok that a second, unrelated gate
    # can invalidate.
    assert _gate(rm, signal=_sig(atr=0.0), tick=wide).reason != "spread_too_wide"
    # Same missing ATR, a spread the default deviation covers twice over (10
    # points, which is what the paper broker's own EURUSD spec quotes): the
    # whole chain reaches ok.
    narrow = _tick(bid=1.09995, ask=1.10005)
    assert _gate(rm, signal=_sig(atr=0.0), tick=narrow).reason == "ok"


def test_spec_not_measured_names_the_reason(tmp_path: Path) -> None:
    """A prefix reason, and the one issue #61 was about.

    It is in the roster now only because the scanner reads the tree. The payload
    lists the fields the venue did not send, so the assertion is startswith plus
    the payload, never equality.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    spec = default_spec("EURUSD")
    blind = replace(spec, point=0.0, unmeasured=frozenset({"point"}))
    decision = _gate(rm, spec=blind)
    assert decision.allowed is False
    assert decision.reason.startswith("spec_not_measured:")
    assert decision.reason.split(":", 1)[1] == "point"


def test_deviation_below_spread_names_the_reason(tmp_path: Path) -> None:
    """The configured slippage cannot cross the spread, so the send would reject.

    Roster case only. The measured gold numbers, the per-symbol resolution, the
    boundary and the no-regression proof on FX are in tests/test_deviation.py.
    """
    cfg = _cfg(tmp_path)
    cfg.risk.deviation_points = 4  # against the 10 points default_spec quotes
    rm = RiskManager(cfg, halt_dir=tmp_path)
    spec = default_spec("EURUSD")
    half = (spec.spread * spec.point) / 2.0
    tick = Tick(time=0, bid=1.10 - half, ask=1.10 + half)
    decision = _gate(rm, spec=spec, tick=tick, signal=_sig(entry=1.10, sl=1.095, tp=1.1125))
    assert decision.allowed is False
    assert decision.reason.startswith("deviation_below_spread:")
    assert "spread=10pt" in decision.reason
    assert "set=risk.symbol_deviation_points.EURUSD>=30" in decision.reason


def test_margin_buffer_names_the_reason(tmp_path: Path) -> None:
    """Free margin below the floor with margin actually in use."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    thin = _acct(10_000, margin=6_000.0, margin_free=4_000.0)  # 0.40 against 0.50
    assert _gate(rm, account=thin).reason == "margin_buffer"


def test_margin_buffer_does_not_fire_on_a_flat_book(tmp_path: Path) -> None:
    """margin == 0 is a flat account, not a squeezed one; the and-branch."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    flat = _acct(10_000, margin=0.0, margin_free=0.0)
    assert _gate(rm, account=flat).reason == "ok"


def test_size_zero_names_the_reason(tmp_path: Path) -> None:
    """The broker minimum lot would risk more than the budget allows.

    500 ticks of stop at 1.00 per tick is 500.00 per lot, so 0.01 lots (the
    broker floor) risks 5.00 against a budget of 100 * 0.005 = 0.50, and
    lots_for_risk refuses to size UP into extra risk. The starting balance is
    matched to the account so the drawdown gate, which runs first, does not
    claim this refusal instead.
    """
    rm = RiskManager(_cfg(tmp_path, initial_balance=100.0), halt_dir=tmp_path)
    d = _gate(rm, account=_acct(100))
    assert d.reason == "size_zero"
    assert d.volume == 0


# --- 3. the last-line size guard, and the one reason still unreachable -----


def test_size_exceeds_risk_names_the_reason(tmp_path: Path) -> None:
    """The last-line size guard, refusing a size the sizer was content with.

    Issue #55. This line used to recompute the cap lots_for_risk had already
    applied, from the inputs lots_for_risk had already been given, with a
    LOOSER tolerance, so nothing could reach it in a state it would refuse. It
    now also measures the volume against the room left before the daily-loss
    and drawdown halts, which comes from the persisted snapshot the sizer is
    never given.

    Here a per-trade risk of 0.5% of equity meets a daily loss budget of 0.1%
    of what the day opened at. The sizer returns a real volume and is content
    with it; one full stop-out on that volume would take the account straight
    through the daily-loss halt, which has not tripped, so the gate refuses.

    The sweep and the independence experiment are in tests/test_size_guard.py.
    """
    cfg = _cfg(tmp_path)
    cfg.risk.daily_loss_pct = 0.001
    rm = RiskManager(cfg, halt_dir=tmp_path)
    acct = _acct(10_000)
    rm.observe(acct, WED_NOON)
    sig = _sig()

    lots = lots_for_risk(
        acct.equity,
        cfg.risk.risk_pct,
        sig.entry,
        sig.sl,
        default_spec("EURUSD"),
        max_risk_multiple=cfg.risk.max_risk_multiple,
    )
    assert lots > 0, "the sizer must ALLOW here, or this case proves nothing"
    assert rm.circuit_reason(acct, WED_NOON) == "", "the circuit must still be clear"

    d = _gate(rm, account=acct, signal=sig)
    assert d.allowed is False
    assert d.reason == "size_exceeds_risk"
    assert d.volume == 0


def test_halted_fallback_is_unreachable(tmp_path: Path) -> None:
    """risk.py:252 reads `self._halt_reason or halted`; the fallback is dead.

    Every site that raises the halt flag sets a reason in the same statement
    block, so there is no public sequence that leaves the flag up and the
    reason empty. Asserted by exercising every public way to halt and
    checking the reason is always non-empty, so the fallback stays dead by
    measurement rather than by reading.
    """
    cfg = _cfg(tmp_path)
    rm = RiskManager(cfg, halt_dir=tmp_path)
    rm.observe(_acct(10_000), WED_NOON)

    rm.write_halt_file("operator")
    assert rm.circuit_reason(_acct(10_000), WED_NOON) == "halt_file"
    assert rm.clear_operator_halt() == ""

    assert _gate(rm, account=_acct(9_700)).reason == "daily_loss"
    assert rm.is_halted and rm.halt_reason != ""
    assert rm.circuit_reason(_acct(9_700), WED_NOON) == "daily_loss"
    assert rm.clear_operator_halt() == "daily_loss"
    assert rm.halt_reason != "", "a halted manager with no reason would reach line 252"


def test_size_exceeds_risk_has_exactly_one_live_site() -> None:
    """The duplicate literal, made attributable instead of ambiguous.

    The string exists twice in the product and the two sites guard different
    things:

      risk.py:347   sizing a NEW market order   -- DEAD, dominated by size_zero
      engine.py     re-pricing an EXISTING working order -- live, /replace

    Before #11 the only test naming the reason matched a substring of a chat
    reply, which passes on the engine site while reading as though it covered
    the risk gate. Deduplicating the literal would not have fixed that; the
    ambiguity was never the spelling, it was that one of the two sites cannot
    run. So the literal stays where it is and attribution is asserted: two
    sites, one of them pinned dead above, and a third appearance fails here.
    """
    src = Path(__file__).resolve().parents[1] / "src" / "straightedge"

    def _sites(text: str) -> int:
        """Occurrences that are SITES, not mentions.

        Full-line comments are excluded (#187). A comment explaining why a
        reason is the WRONG one to report is a mention, not a second place the
        desk can emit it, and counting it forced a choice between spelling the
        reason wrong in prose and tripping this guard. A mention is not a
        relationship.

        Docstrings still count on purpose: a docstring asserting that a reason
        is emitted here IS a claim about this file, and this sprint measured
        four comments that asserted properties their code did not have (see
        `docs/TESTING.md`). A claim is exactly what should force
        re-attribution.
        """
        return sum(
            line.count("size_exceeds_risk")
            for line in text.splitlines()
            if not line.lstrip().startswith("#")
        )

    sites = {
        p.name: _sites(p.read_text(encoding="utf-8"))
        for p in sorted(src.glob("*.py"))
    }
    live = {name: n for name, n in sites.items() if n}
    assert live == {"engine.py": 1, "risk.py": 1}, (
        "size_exceeds_risk moved or gained a site; attribution must be redone: "
        + repr(live)
    )


def test_the_engine_replace_guard_is_the_live_size_exceeds_risk_site(tmp_path: Path) -> None:
    """The one site that CAN refuse with this name, exercised on its own path.

    /replace keeps the order volume the broker already accepted and re-prices
    it, so it never calls lots_for_risk and the dominance above does not apply.
    This is the refusal an operator can actually receive.
    """
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    broker.seed_bars("GBPUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=4))
    broker.seed_bars("AUDUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=5))
    broker.seed_bars("USDCHF", generate_bars(120, drift=0.0004, vol=0.0002, seed=6))
    engine = Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        now_fn=lambda: WED_NOON,
    )
    engine.start()
    tick = broker.tick("EURUSD")
    spec = broker.symbol("EURUSD")
    limit = spec.normalize_price(tick.ask - 0.002)
    sl = spec.normalize_price(limit - 0.005)
    tp = spec.normalize_price(limit + 0.010)
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD limit=" + str(limit) + " sl=" + str(sl) + " tp=" + str(tp), 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    order = broker.orders()[0]
    wider = spec.normalize_price(limit + 0.001)  # widens the stop past the cap
    reply = engine.handle_command(TgCommand("1", 1, "/replace " + str(order.ticket) + " " + str(wider), 3))
    assert reply == "refused: size_exceeds_risk"
    engine.stop()


# --- 4. the same reasons on the surface an operator actually reads ----------
#
# Where a reason is reachable through the desk or the auto leg, the structured
# reject record from #29 is the assertion, not the return value: the record is
# what an operator and an auditor read after the fact. The return-value cases
# above stay because several reasons have no other reachable caller.


def _engine(tmp_path: Path, **kw) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = kw.get("session", False)
    cfg.risk.max_spread_atr_frac = kw.get("max_spread_atr_frac", 10.0)
    cfg.risk.halt_file = str(tmp_path / "HALT")
    if "risk_pct" in kw:
        cfg.risk.risk_pct = kw["risk_pct"]
    if "daily_loss_pct" in kw:
        cfg.risk.daily_loss_pct = kw["daily_loss_pct"]
    if "max_currency_exposure" in kw:
        cfg.risk.max_currency_exposure = kw["max_currency_exposure"]
    if "max_positions" in kw:
        cfg.risk.max_positions = kw["max_positions"]
    broker = PaperBroker(
        balance=kw.get("balance", 10_000),
        leverage=kw.get("leverage", 100),
        trade_allowed=kw.get("trade_allowed", True),
    )
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    broker.seed_bars("GBPUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=4))
    broker.seed_bars("AUDUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=5))
    broker.seed_bars("USDCHF", generate_bars(120, drift=0.0004, vol=0.0002, seed=6))
    return Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        now_fn=lambda: WED_NOON,
    )


def _reject(engine: Engine) -> dict:
    rec = engine.journal.last_event("reject")
    assert rec is not None, "a refusal left no structured record"
    return rec


def test_already_in_symbol_is_journaled_with_the_name(tmp_path: Path) -> None:
    """Open EURUSD, then ask for EURUSD again."""
    engine = _engine(tmp_path)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    assert engine.broker.positions()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 3))
    assert reply == "refused: already_in_symbol"
    rec = _reject(engine)
    assert rec["reason"] == "already_in_symbol"
    assert rec["source"] == "telegram"
    assert rec["stage"] == "stage"
    assert rec["symbol"] == "EURUSD"
    engine.stop()


def test_size_zero_is_journaled_with_the_name(tmp_path: Path) -> None:
    """A risk budget too small to buy even the broker minimum lot."""
    engine = _engine(tmp_path, risk_pct=1e-09)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert reply == "refused: size_zero"
    rec = _reject(engine)
    assert rec["reason"] == "size_zero"
    assert rec["source"] == "telegram"
    engine.stop()


def test_size_exceeds_risk_is_journaled_with_the_name(tmp_path: Path) -> None:
    """The same refusal as an operator receives it: a structured record.

    A daily loss budget of 0.1% against a per-trade risk of 0.5%. The desk
    sizes the order, the last-line guard refuses it, and what lands in the
    journal is the NAMED reason rather than a line of prose in a chat. Before
    issue #55 no input could produce this record at all.
    """
    engine = _engine(tmp_path, daily_loss_pct=0.001)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert reply == "refused: size_exceeds_risk"
    rec = _reject(engine)
    assert rec["reason"] == "size_exceeds_risk"
    assert rec["source"] == "telegram"
    assert rec["stage"] == "stage"
    assert rec["symbol"] == "EURUSD"
    engine.stop()


def test_margin_buffer_is_journaled_with_the_name(tmp_path: Path) -> None:
    """Leverage 10 lets one 0.55-lot leg eat past the free-margin floor."""
    engine = _engine(tmp_path, leverage=10, max_positions=5)
    engine.start()
    engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    engine.handle_command(TgCommand("1", 1, "/confirm", 2))
    acct = engine.broker.account()
    assert acct.margin > 0
    assert acct.margin_free / acct.equity < engine.cfg.risk.min_free_margin_pct
    reply = engine.handle_command(TgCommand("1", 1, "/buy GBPUSD", 3))
    assert reply == "refused: margin_buffer"
    rec = _reject(engine)
    assert rec["reason"] == "margin_buffer"
    assert rec["symbol"] == "GBPUSD"
    engine.stop()


def test_trade_not_allowed_is_journaled_with_the_name(tmp_path: Path) -> None:
    """The terminal itself refuses; the circuit reports it before any sizing."""
    engine = _engine(tmp_path, trade_allowed=False)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert reply == "refused: trade_not_allowed"
    rec = _reject(engine)
    assert rec["reason"] == "trade_not_allowed"
    engine.stop()


def test_max_positions_is_journaled_with_the_name(tmp_path: Path) -> None:
    """Three symbols filled, a fourth asked for.

    The currency cap is lifted so this case can only fail on the slot count;
    three USD-quote legs would otherwise trip currency_exposure first, which is
    its own test below.
    """
    engine = _engine(tmp_path, max_currency_exposure=99)
    engine.start()
    for i, symbol in enumerate(("EURUSD", "GBPUSD", "AUDUSD")):
        engine.handle_command(TgCommand("1", 1, "/buy " + symbol, 2 * i + 1))
        engine.handle_command(TgCommand("1", 1, "/confirm", 2 * i + 2))
    assert len(engine.broker.positions(magic=engine.cfg.risk.magic)) == 3
    reply = engine.handle_command(TgCommand("1", 1, "/buy USDCHF", 9))
    assert reply == "refused: max_positions"
    rec = _reject(engine)
    assert rec["reason"] == "max_positions"
    assert rec["stage"] == "stage"
    engine.stop()


def test_currency_exposure_is_journaled_with_the_name(tmp_path: Path) -> None:
    """Two long USD-quote legs; a third would be three deep on one currency."""
    engine = _engine(tmp_path, max_positions=5)
    engine.start()
    for i, symbol in enumerate(("EURUSD", "GBPUSD")):
        engine.handle_command(TgCommand("1", 1, "/buy " + symbol, 2 * i + 1))
        engine.handle_command(TgCommand("1", 1, "/confirm", 2 * i + 2))
    assert len(engine.broker.positions(magic=engine.cfg.risk.magic)) == 2
    reply = engine.handle_command(TgCommand("1", 1, "/buy AUDUSD", 7))
    assert reply == "refused: currency_exposure"
    rec = _reject(engine)
    assert rec["reason"] == "currency_exposure"
    engine.stop()


def test_spread_too_wide_is_journaled_with_the_name(tmp_path: Path) -> None:
    """The live spread measured against ATR, at the shipped default fraction."""
    engine = _engine(tmp_path, max_spread_atr_frac=0.0001)
    engine.start()
    reply = engine.handle_command(TgCommand("1", 1, "/buy EURUSD", 1))
    assert reply == "refused: spread_too_wide"
    rec = _reject(engine)
    assert rec["reason"] == "spread_too_wide"
    engine.stop()


def test_outside_session_is_journaled_on_the_auto_leg(tmp_path: Path) -> None:
    """The ONLY path that can produce this reason.

    The desk passes manual=True, which bypasses the session window by design,
    so outside_session is unreachable from every operator command. The auto
    leg times itself off the last bar, so the bar clock is the lever.
    """
    engine = _engine(tmp_path, session=True)
    engine.start()
    saturday = datetime(2024, 1, 6, 12, 0, tzinfo=timezone.utc)
    n = 250
    start_ts = int(saturday.timestamp()) - (n - 1) * 3600
    bars = generate_bars(n, drift=0.0006, vol=0.0002, seed=7, start_ts=start_ts)
    assert bars[-1].time == int(saturday.timestamp())
    engine.broker.seed_bars("EURUSD", bars)
    engine.replay_symbol("EURUSD", bars)
    rec = _reject(engine)
    assert rec["reason"] == "outside_session"
    assert rec["source"] == "auto"
    assert rec["stage"] == "signal"
    engine.stop()


def test_the_staging_gate_block_is_journaled_with_the_name(tmp_path: Path) -> None:
    """circuit_reason() is the gate deciding whether the LLM may stage at all.

    #11 named it as missing several branches. On the advice path its verdict
    leaves an advice_circuit_block record, so the reason is assertable rather
    than only visible inside a prose blob.
    """
    engine = _engine(tmp_path)
    engine.start()
    engine.risk.write_halt_file("operator")
    assert engine.advice_circuit_reason() == "halt_file"
    engine.stop()


# --- 5. the lifecycle around a reason, not just its first firing ------------


def test_a_new_utc_day_clears_the_daily_loss_refusal(tmp_path: Path) -> None:
    """daily_loss is sticky until the next UTC day, and only daily_loss is.

    A reason that can be named but never cleared is a different defect from a
    reason that is never named, so the roster asserts both directions.
    """
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    rm.observe(_acct(10_000), WED_NOON)
    assert _gate(rm, account=_acct(9_700)).reason == "daily_loss"
    assert rm.halt_reason == "daily_loss"
    thursday = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
    rm.observe(_acct(9_700), thursday)
    assert rm.halt_reason == "", "a new UTC day must clear the daily budget"
    assert _gate(rm, account=_acct(9_700), now=thursday).reason == "ok"


def test_a_new_utc_day_does_not_clear_max_drawdown(tmp_path: Path) -> None:
    """The negative half: the drawdown halt outlives the day boundary."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    thursday = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
    friday = datetime(2024, 1, 5, 12, 0, tzinfo=timezone.utc)
    rm.observe(_acct(10_000), WED_NOON)
    rm.observe(_acct(9_000), thursday)
    assert _gate(rm, account=_acct(8_900), now=thursday).reason == "max_drawdown"
    rm.observe(_acct(8_900), friday)
    assert rm.halt_reason == "max_drawdown"
    assert _gate(rm, account=_acct(8_900), now=friday).reason == "max_drawdown"


def test_a_wiped_account_is_refused_before_the_margin_ratio(tmp_path: Path) -> None:
    """risk.py:329 has a false branch that cannot be taken. Third finding.

    `if account.equity > 0:` guards a division, so the interesting case is
    equity <= 0. No such account can reach line 329, because the daily-loss
    gate fires first for every one of them: on a fresh day observe() sets
    day_start_equity to the account equity, so daily_loss is 0 and the cap is
    day_start * 0.02, and `0 >= 0` is True at zero equity and True again for
    a negative day_start. The gate therefore fails CLOSED on a wiped account,
    which is the right outcome, and the division guard below it is
    unreachable rather than wrong.

    Pinned, not fixed: this is the correct refusal, just not the reason a
    reader of risk.py:329 would expect, and a coverage report will keep
    showing that branch partial forever.
    """
    for balance in (0.0, -500.0):
        rm = RiskManager(_cfg(tmp_path, initial_balance=balance), halt_dir=tmp_path)
        d = _gate(rm, account=_acct(balance, margin=0.0, margin_free=0.0))
        assert d.reason == "daily_loss", (
            "a wiped account now reaches the margin ratio; risk.py:329 has a"
            " reachable false branch and needs a real test: " + repr((balance, d))
        )
        assert d.halt, "a gate that cannot measure must fail closed"



def test_exposure_unmeasured_is_a_tripwire_not_a_gate(tmp_path: Path) -> None:
    """currency_exposure raises, but evaluate never hands it a non-FX symbol.

    The raise is what stops a future caller reintroducing the silent skip of
    issue #10. evaluate classifies first and passes only confirmed pairs, so
    no broker symbol can reach the except branch. Asserted by measurement:
    currency_exposure still refuses when called directly, while a book full of
    non-FX instruments is ALLOWED and recorded rather than refused. If a
    non-FX position ever tripped this, one open index would block every
    subsequent trade.
    """
    from straightedge.risk import UnclassifiedSymbol, currency_exposure

    with pytest.raises(UnclassifiedSymbol):
        currency_exposure([], extra=("US30", Side.BUY))

    cfg = _cfg(tmp_path)
    cfg.risk.max_positions = 9
    rm = RiskManager(cfg, halt_dir=tmp_path)
    rm.observe(_acct(10_000), WED_NOON)
    book = [
        Position(1, "US30", Side.BUY, 0.1, 1.1, 1.09, 1.12, 1.1, 0, magic=cfg.risk.magic),
        Position(2, "USOIL", Side.BUY, 0.1, 1.1, 1.09, 1.12, 1.1, 0, magic=cfg.risk.magic),
    ]
    d = _gate(rm, positions=book)
    assert d.reason != "exposure_unmeasured"
    assert d.allowed, d.reason
