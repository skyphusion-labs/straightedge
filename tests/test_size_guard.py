"""The last-line size guard: it must be able to fire, and it must be able to
disagree with the sizer (issue #55).

Two layers reading the same inputs are one layer with a longer runtime. This
guard was exactly that: it recomputed the cap lots_for_risk had already
applied, from the same entry, stop, spec and equity, with a LOOSER tolerance,
so no input could reach it in a state it would refuse. A 497,664-case sweep
reached it 114,840 times and tripped it zero times.

It now measures the sized volume against loss_room: what the account may
still lose before the daily-loss or max-drawdown halt trips. Those budgets
are derived from the persisted EquitySnapshot (day_start_equity,
peak_equity), which lots_for_risk is never given. That is the whole of the
independence, and the tests are ordered by how much they prove:

1. it can fire at all, and does not fire on everything (the sweep, same
   shape as the one that proved the old guard dead),
2. it refuses a size the sizer allowed,
3. the disagreement comes from state the sizer cannot see: hold every input
   lots_for_risk receives EXACTLY constant, move only the persisted
   snapshot, and the verdict flips,
4. a future loosening of the sizer is still caught, which is the reason the
   dead guard was repaired instead of deleted,
5. the refusal stays distinguishable from size_zero.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from straightedge.broker.paper import default_spec
from straightedge.config import BotConfig
from straightedge.models import Account, Signal, SignalKind, Tick
from straightedge.risk import RiskManager
from straightedge.sizing import lots_for_risk, money_per_lot_at_stop

WED_NOON = datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)
SPEC = default_spec("EURUSD")


def _acct(equity: float = 10_000.0) -> Account:
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


def _sig(entry: float = 1.10, stop: float = 0.005) -> Signal:
    return Signal(SignalKind.BUY, "EURUSD", entry, entry - stop, entry + 2 * stop, 0.003, reason="test")


def _cfg(tmp_path: Path, **kw) -> BotConfig:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.session.enabled = False
    for k, v in kw.items():
        setattr(cfg.risk, k, v)
    return cfg


def _gate(rm: RiskManager, account: Account, signal: Signal | None = None, spec=SPEC):
    return rm.evaluate(
        account=account,
        signal=signal if signal is not None else _sig(),
        spec=spec,
        tick=Tick(time=0, bid=1.0999, ask=1.1001),
        positions=[],
        orders=[],
        now=WED_NOON,
        manual=True,
    )


def _fresh(cfg: BotConfig, tmp_path: Path, day_start: float, peak: float) -> RiskManager:
    """A manager whose PERSISTED snapshot says: today opened at day_start, the
    peak is peak. Public API only. The state file is removed so nothing is
    restored from an earlier case, the seed balance is zeroed so it cannot
    dominate the peak, and observe is then fed the two equities in order:
    the first call lands on a new day_key and sets day_start, the second
    raises the peak without moving day_start.
    """
    cfg.initial_balance = 0.0
    state = tmp_path / "equity.json"
    if state.exists():
        state.unlink()
    rm = RiskManager(cfg, halt_dir=tmp_path, state_path=state)
    rm.observe(_acct(day_start), WED_NOON)
    if peak > day_start:
        rm.observe(_acct(peak), WED_NOON)
    assert rm.snapshot.day_start_equity == day_start
    return rm


# The sweep grid. It is deliberately a QUARTER of the grid used to measure the
# guard (497,664 cases, in the PR and the changelog for #55), because every
# case here rewrites the persisted snapshot through the public API and Windows
# pays for that: at 11,520 cases the windows-latest CI leg went from 65s to
# 4m37s. A sweep nobody will tolerate in CI gets deleted, so the shipped one
# is sized to be kept and the big number is recorded where it was measured.
EQUITIES = (1_000.0, 10_000.0, 1_000_000.0)
DAYFAC = (1.0, 1.005, 1.02, 1.05, 1.15)
PEAKFAC = (1.0, 1.10)
RISKPCT = (0.0005, 0.005, 0.01, 0.03)
DAILYPCT = (0.005, 0.02, 0.05)
DDPCT = (0.05, 0.20)
MRM = (1.0, 2.0)
STOPS = (0.0005, 0.02)
SWEEP_CASES = 2_880


def test_the_guard_can_fire_and_does_not_fire_on_everything(tmp_path: Path) -> None:
    """1. The same class of sweep that proved the old guard dead.

    A guard that trips 0 times in half a million cases is decoration. So is
    one that trips every time: it would be refusing the whole product, and no
    input could show the ALLOW path. Both counts are asserted, and both are
    reported in every failure message, so the denominator is never hidden.

    Reaching the line means getting past the size_zero branch immediately
    above it, which is exactly what the old pin measured.
    """
    reached = tripped = allowed = cases = 0
    for rp in RISKPCT:
        for dl in DAILYPCT:
            for dd in DDPCT:
                for mrm in MRM:
                    cfg = _cfg(
                        tmp_path,
                        risk_pct=rp,
                        daily_loss_pct=dl,
                        max_drawdown_pct=dd,
                        max_risk_multiple=mrm,
                    )
                    for eq in EQUITIES:
                        for df in DAYFAC:
                            for pf in PEAKFAC:
                                for stop in STOPS:
                                    cases += 1
                                    rm = _fresh(cfg, tmp_path, eq * df, eq * max(pf, df))
                                    d = _gate(rm, _acct(eq), _sig(stop=stop))
                                    if d.reason in ("ok", "size_exceeds_risk"):
                                        reached += 1
                                    if d.reason == "size_exceeds_risk":
                                        tripped += 1
                                    if d.reason == "ok":
                                        allowed += 1
    counted = "cases=%d reached=%d tripped=%d allowed=%d" % (cases, reached, tripped, allowed)
    assert cases == SWEEP_CASES, counted
    assert reached > 500, "the sweep hardly reached the guard: " + counted
    assert tripped > 0, "the guard tripped ZERO times, which is where #55 began: " + counted
    assert allowed > 0, "the guard refused everything, which is not a guard either: " + counted


def test_it_refuses_a_size_the_sizer_allowed(tmp_path: Path) -> None:
    """2. Sizer: allow. Gate: refuse. The load-bearing case.

    The day opened at 10,000 with a 2% daily loss budget, so 200 was the
    budget for the day and 160 of it is gone. lots_for_risk still sizes 0.5%
    of LIVE equity, because it is never told about the day, and it is content:
    the sized risk is inside its own per-trade cap. One full stop-out on that
    volume would end the day past the daily-loss halt, so the gate refuses
    something the first layer had no way to object to.
    """
    cfg = _cfg(tmp_path)
    rm = _fresh(cfg, tmp_path, 10_000.0, 10_000.0)
    acct = _acct(9_840.0)
    sig = _sig()

    lots = lots_for_risk(
        acct.equity, cfg.risk.risk_pct, sig.entry, sig.sl, SPEC,
        max_risk_multiple=cfg.risk.max_risk_multiple,
    )
    assert lots > 0, "the sizer must ALLOW here, or this case proves nothing"
    worst = money_per_lot_at_stop(sig.entry, sig.sl, SPEC) * lots
    per_trade = acct.equity * cfg.risk.risk_pct * cfg.risk.max_risk_multiple
    assert worst <= per_trade + 1e-09, "the per-trade cap must NOT be what refuses"

    room = rm.loss_room(acct)
    assert 0 < room < worst, (room, worst)
    assert rm.circuit_reason(acct, WED_NOON) == "", "the circuit must still be clear"

    d = _gate(rm, acct, sig)
    assert d.allowed is False
    assert d.reason == "size_exceeds_risk"
    assert d.volume == 0


def test_only_the_persisted_snapshot_moves_the_verdict(tmp_path: Path) -> None:
    """3. Independence, stated as an experiment rather than as an argument.

    Every input lots_for_risk receives is held EXACTLY constant across the two
    halves: the same equity, risk_pct, entry, stop, spec and max_risk_multiple.
    The sizer therefore returns the same lots in both. The only difference is
    what the persisted snapshot says the day opened at, which the sizer is
    never given, and the gate flips from ok to a refusal.

    A layer that cannot produce this result is not a second layer. That is
    what was wrong with the guard before issue #55, and asserting it here is
    what stops the guard being rewritten back into a copy of the sizer.
    """
    acct = _acct(10_000.0)
    sig = _sig()
    cfg_a = _cfg(tmp_path)
    cfg_b = _cfg(tmp_path)
    assert cfg_a.risk == cfg_b.risk

    lots = lots_for_risk(
        acct.equity, cfg_a.risk.risk_pct, sig.entry, sig.sl, SPEC,
        max_risk_multiple=cfg_a.risk.max_risk_multiple,
    )
    assert lots > 0

    flat = _fresh(cfg_a, tmp_path, 10_000.0, 10_000.0)   # nothing lost today
    down = _fresh(cfg_b, tmp_path, 10_160.0, 10_160.0)   # 160 of the day spent

    assert flat.circuit_reason(acct, WED_NOON) == ""
    assert down.circuit_reason(acct, WED_NOON) == ""
    assert flat.loss_room(acct) > down.loss_room(acct)

    a = _gate(flat, acct, sig)
    b = _gate(down, acct, sig)
    assert a.reason == "ok"
    assert a.volume == lots, "the sizer verdict must be identical in both halves"
    assert b.reason == "size_exceeds_risk"
    assert b.volume == 0


def test_a_loosened_sizer_is_still_caught(tmp_path: Path, monkeypatch) -> None:
    """4. The backstop half, watched firing.

    The per-trade cap cannot fire against the current sizer, by construction:
    lots_for_risk enforces the same inequality with the same tolerance before
    it returns. That is not a reason to delete it, it is a reason to prove
    something can make it fire, because the regression it guards against is a
    future change to the sizer.

    So the sizer is loosened here on purpose, by 1.5x, on a clean account with
    the whole daily budget intact. The live half of the guard would allow this
    volume; the per-trade half refuses it. Without this test the term would be
    exactly the decoration issue #55 was about.
    """
    def loosened(equity, risk_pct, entry, sl, spec, *, max_risk_multiple=1.0):
        honest = lots_for_risk(
            equity, risk_pct, entry, sl, spec, max_risk_multiple=max_risk_multiple,
        )
        return round(honest * 1.5, 2)

    cfg = _cfg(tmp_path)
    rm = _fresh(cfg, tmp_path, 10_000.0, 10_000.0)
    acct = _acct(10_000.0)
    sig = _sig()
    honest = lots_for_risk(
        acct.equity, cfg.risk.risk_pct, sig.entry, sig.sl, SPEC,
        max_risk_multiple=cfg.risk.max_risk_multiple,
    )
    assert _gate(rm, acct, sig).reason == "ok", "the honest sizer must pass first"

    monkeypatch.setattr("straightedge.risk.lots_for_risk", loosened)
    oversized = round(honest * 1.5, 2)
    worst = money_per_lot_at_stop(sig.entry, sig.sl, SPEC) * oversized
    per_trade = acct.equity * cfg.risk.risk_pct * cfg.risk.max_risk_multiple
    assert worst > per_trade, "the loosening must breach the per-trade cap"
    assert worst < rm.loss_room(acct), "and must NOT breach the halt room, or the halves are not separable"

    d = _gate(rm, acct, sig)
    assert d.allowed is False
    assert d.reason == "size_exceeds_risk"


def test_the_two_size_refusals_stay_different_words(tmp_path: Path) -> None:
    """5. size_zero and size_exceeds_risk must not collapse into one word.

    One manager, one account, one config; only the stop distance differs. A
    stop so wide that the broker minimum lot would risk more than the budget
    leaves the sizer with nothing to return, and that is size_zero. A normal
    stop on a day already 160 down is a real volume the account cannot afford
    to lose, and that is size_exceeds_risk. An operator reading the journal
    gets two different sentences for two different situations.
    """
    cfg = _cfg(tmp_path)
    rm = _fresh(cfg, tmp_path, 10_000.0, 10_000.0)
    acct = _acct(9_840.0)

    wide = _sig(stop=0.5)
    assert lots_for_risk(
        acct.equity, cfg.risk.risk_pct, wide.entry, wide.sl, SPEC,
        max_risk_multiple=cfg.risk.max_risk_multiple,
    ) == 0
    assert _gate(rm, acct, wide).reason == "size_zero"

    normal = _sig()
    assert _gate(rm, acct, normal).reason == "size_exceeds_risk"
