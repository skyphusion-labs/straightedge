"""Restart must not discard the protective state, nor restore the permissive one.

Issue #7. Three properties, each of which was false at 281b4fa:

1. a daily_loss trip survives a restart inside the same UTC day;
2. the equity peak that feeds max_drawdown survives a restart;
3. a journal that contains live_on does NOT arm real money in a new process.

Every assertion here reads the REASON, not the status. `allowed is False` cannot
tell you the test stopped testing anything; `reason == "daily_loss"` can.

The gate must still be able to go GREEN, so the refusals are paired with the
positive controls that would catch a gate wedged shut: a genuine new UTC day,
and a clean first start with no snapshot on disk.
"""

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from straightedge.broker.paper import PaperBroker, default_spec
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.journal import Journal
from straightedge.models import Account, EquitySnapshot, Signal, SignalKind, Tick
from straightedge.risk import RiskManager
from straightedge.state import (
    SNAPSHOT_VERSION,
    StateUnreadable,
    StateUnwritable,
    load_snapshot,
    save_snapshot,
    snapshot_path_for,
)
from straightedge.synthetic import generate_bars
from straightedge.telegram import TgCommand
from wincompat import assert_owner_mode, assert_same_path

DAY1 = datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)  # Wednesday noon UTC
DAY2 = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)


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
        trade_allowed=True,
        trade_expert=True,
        trade_mode=kw.get("trade_mode", 0),
    )


def _sig() -> Signal:
    return Signal(SignalKind.BUY, "EURUSD", 1.10, 1.095, 1.1125, 0.003, reason="test")


def _cfg(tmp_path: Path) -> BotConfig:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    return cfg


def _gate(rm: RiskManager, equity: float, now: datetime = DAY1):
    return rm.evaluate(
        account=_acct(equity),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=Tick(time=0, bid=1.0999, ask=1.1001),
        positions=[],
        orders=[],
        now=now,
    )


def _engine(tmp_path: Path) -> Engine:
    cfg = _cfg(tmp_path)
    cfg.risk.max_spread_atr_frac = 10.0
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    return Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: DAY1)


def _snap(**kw) -> EquitySnapshot:
    base = dict(
        time=1_704_283_200,
        balance=9_700.0,
        equity=9_700.0,
        peak_equity=10_000.0,
        day_start_equity=10_000.0,
        day_key="2024-01-03",
    )
    base.update(kw)
    return EquitySnapshot(**base)  # type: ignore[arg-type]


# --- the three defects ------------------------------------------------------


def test_daily_loss_halt_survives_restart_same_utc_day(tmp_path: Path) -> None:
    """A fresh process on the same UTC day must not hand out a new loss budget."""
    first = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    first.observe(_acct(10_000), DAY1)
    tripped = _gate(first, 9_700)  # -3% against a 2% daily cap
    assert tripped.reason == "daily_loss"
    assert tripped.halt is True

    second = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    again = _gate(second, 9_700)
    assert again.reason == "daily_loss"
    assert again.allowed is False


def test_equity_peak_survives_restart(tmp_path: Path) -> None:
    """The drawdown gate reads the peak. A restart must not zero it."""
    first = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    first.observe(_acct(10_000), DAY1)
    first.observe(_acct(20_000), DAY1)  # a winning run
    assert first.snapshot.peak_equity == 20_000

    second = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    # New UTC day at 17_500: 0% on the day, 12.5% off the old peak, 10% cap.
    # Observing and gating at the same equity keeps daily_loss out of the way,
    # so the reason proves the drawdown gate read the restored peak.
    second.observe(_acct(17_500), DAY2)
    assert second.snapshot.peak_equity == 20_000
    assert second.snapshot.day_start_equity == 17_500  # a genuine new day resets
    d = _gate(second, 17_500, now=DAY2)
    assert d.reason == "max_drawdown"


def test_journal_live_on_does_not_arm_a_new_process(tmp_path: Path) -> None:
    """A journal replay must not be able to arm real money."""
    journal = Journal(tmp_path / "j.jsonl")
    journal.write("live_on")

    engine = _engine(tmp_path)
    assert engine.cfg.live_accepted is False
    engine.start()
    assert engine.cfg.live_accepted is False
    reply = engine.handle_command(TgCommand("1", 1, "/live", 1))
    assert "live=off" in reply
    engine.stop()


def test_declined_live_restore_is_journaled(tmp_path: Path) -> None:
    """The decline is never silent: it is an audit record and an operator line."""
    journal = Journal(tmp_path / "j.jsonl")
    journal.write("live_on")

    engine = _engine(tmp_path)
    engine.start()
    events = [r.get("event") for r in engine.journal.tail(50)]
    assert "live_not_restored" in events
    rec = engine.journal.last_event("live_not_restored")
    assert rec is not None and rec.get("scope") == "process"
    assert engine.desk.live_expired is True
    engine.stop()


def test_operator_can_rearm_in_this_process(tmp_path: Path) -> None:
    """Per-process expiry is not a lockout. The operator re-arms by typing it."""
    journal = Journal(tmp_path / "j.jsonl")
    journal.write("live_on")
    engine = _engine(tmp_path)
    engine.start()
    assert engine.cfg.live_accepted is False
    engine.handle_command(TgCommand("1", 1, "/live on I-ACCEPT-RISK", 1))
    assert engine.cfg.live_accepted is True
    assert engine.desk.live_expired is False
    assert "live=on" in engine.handle_command(TgCommand("1", 1, "/live", 2))
    engine.stop()


# --- the gate must still be able to go green --------------------------------


def test_new_utc_day_resets_the_loss_budget(tmp_path: Path) -> None:
    """Keyed on day_key: the same day holds, a genuine new day resets."""
    first = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    first.observe(_acct(10_000), DAY1)
    assert _gate(first, 9_700).reason == "daily_loss"

    second = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    fresh = _gate(second, 9_700, now=DAY2)
    assert fresh.reason == "ok"
    assert fresh.allowed is True
    assert second.snapshot.day_start_equity == 9_700
    assert second.snapshot.peak_equity == 10_000  # the peak is not daily


def test_clean_first_start_has_no_snapshot_and_is_allowed(tmp_path: Path) -> None:
    """An absent file is a clean start, not a refusal. The negative control."""
    path = snapshot_path_for(tmp_path / "j.jsonl")
    assert not path.exists()
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    assert rm.state_error == ""
    assert rm.halt_reason == ""
    d = _gate(rm, 10_000)
    assert d.reason == "ok"


def test_restored_state_is_recomputed_not_a_cached_verdict(tmp_path: Path) -> None:
    """Persisting the INPUT, not the verdict. Recovered equity trades again."""
    first = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    first.observe(_acct(10_000), DAY1)
    assert _gate(first, 9_700).reason == "daily_loss"

    second = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    assert second.halt_reason == ""  # no halt was persisted
    assert second.snapshot.day_start_equity == 10_000  # the input was
    recovered = _gate(second, 9_950, now=DAY1)  # 0.5% down, same day
    assert recovered.reason == "ok"


# --- COULD NOT MEASURE is not a clean state --------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "",
        "   \n",
        '{"version": 1, "day_key": "2024-01-03", "peak_equity": 10000, "day_st',
        "[]",
        '"a string"',
        '{"day_key": "2024-01-03"}',
        # Was version 2, which this build now WRITES (#13 added the daily
        # counters). An unknown version still has to be refused, so the case
        # moved to one no build has ever written rather than being deleted.
        '{"version": 99, "time": 0, "balance": 1, "equity": 1, "peak_equity": 1,'
        ' "day_start_equity": 1, "day_key": "2024-01-03"}',
        '{"version": 1, "time": 0, "balance": 1, "equity": 1, "peak_equity": NaN,'
        ' "day_start_equity": 1, "day_key": "2024-01-03"}',
        '{"version": 1, "time": 0, "balance": 1, "equity": 1, "peak_equity": "x",'
        ' "day_start_equity": 1, "day_key": "2024-01-03"}',
        '{"version": 1, "time": 0, "balance": 1, "equity": 1, "peak_equity": -1,'
        ' "day_start_equity": 1, "day_key": "2024-01-03"}',
        '{"version": 1, "time": 0, "balance": 1, "equity": 1, "peak_equity": 1,'
        ' "day_start_equity": 1, "day_key": 20240103}',
    ],
)
def test_bad_snapshot_is_unreadable_not_empty(tmp_path: Path, body: str) -> None:
    path = snapshot_path_for(tmp_path / "j.jsonl")
    path.write_text(body, encoding="utf-8")
    with pytest.raises(StateUnreadable):
        load_snapshot(path)


def test_corrupt_snapshot_fails_closed(tmp_path: Path) -> None:
    """A money gate cannot trade on a state it could not read. Closed, loudly.

    Fails CLOSED because the alternative is trading with an unknown loss budget
    and an unknown peak, which is the defect in #7 wearing a different hat. A
    corrupt file is also LEFT ALONE: it is the operator's evidence, and clearing
    it is a deliberate act that resets the peak.
    """
    path = snapshot_path_for(tmp_path / "j.jsonl")
    body = '{"version": 1, "day_key": "2024-01-03", "peak_eq'  # SIGKILL mid-write
    path.write_text(body, encoding="utf-8")

    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    assert rm.halt_reason == "state_unreadable"
    assert "not valid json" in rm.state_error
    d = _gate(rm, 10_000)
    assert d.reason == "state_unreadable"
    assert d.allowed is False
    assert d.halt is True
    assert path.read_text(encoding="utf-8") == body  # evidence preserved


def test_state_unreadable_is_distinguishable_from_clean(tmp_path: Path) -> None:
    """The whole point: could-not-measure must not look like a fresh start."""
    clean = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    assert (clean.halt_reason, clean.state_error) == ("", "")

    other = tmp_path / "corrupt"
    other.mkdir()
    snapshot_path_for(other / "j.jsonl").write_text("{", encoding="utf-8")
    cfg = _cfg(tmp_path)
    cfg.journal_path = str(other / "j.jsonl")
    broken = RiskManager(cfg, halt_dir=tmp_path)
    assert broken.halt_reason == "state_unreadable"
    assert broken.state_error != ""


def test_unwritable_snapshot_halts(tmp_path: Path, monkeypatch) -> None:
    """A write that fails means the next restart loses the budget. Closed."""
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)

    def boom(*_a, **_k):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr("straightedge.state.os.replace", boom)
    d = _gate(rm, 10_000)
    assert d.reason == "state_unwritable"
    assert d.allowed is False
    assert "No space left on device" in rm.state_error


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_unwritable_directory_halts(tmp_path: Path) -> None:
    """The same thing again without a stub, on a real read-only directory."""
    if os.geteuid() == 0:
        pytest.skip("root ignores the mode bits this test relies on")
    ro = tmp_path / "ro"
    ro.mkdir()
    cfg = _cfg(tmp_path)
    cfg.journal_path = str(ro / "j.jsonl")
    ro.chmod(0o500)
    try:
        rm = RiskManager(cfg, halt_dir=tmp_path)
        assert rm.halt_reason == ""  # absent reads clean
        d = _gate(rm, 10_000)
        assert d.reason == "state_unwritable"
    finally:
        ro.chmod(0o700)


# --- the write itself ------------------------------------------------------


def test_snapshot_written_on_a_peak_move_not_only_on_a_halt(tmp_path: Path) -> None:
    """A file written only at the halt has already lost the peak."""
    path = snapshot_path_for(tmp_path / "j.jsonl")
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    rm.observe(_acct(10_000), DAY1)
    assert path.exists()
    rm.observe(_acct(12_500), DAY1)  # a new high, no halt anywhere near
    assert rm.halt_reason == ""
    on_disk = json.loads(path.read_text(encoding="utf-8"))
    assert on_disk["peak_equity"] == 12_500
    assert on_disk["day_key"] == "2024-01-03"
    assert on_disk["version"] == SNAPSHOT_VERSION
    assert "written_at" in on_disk


def test_snapshot_is_0600(tmp_path: Path) -> None:
    path = save_snapshot(snapshot_path_for(tmp_path / "j.jsonl"), _snap())
    assert_owner_mode(path)


def test_snapshot_round_trips(tmp_path: Path) -> None:
    path = snapshot_path_for(tmp_path / "j.jsonl")
    save_snapshot(path, _snap(peak_equity=20_000.0))
    got = load_snapshot(path)
    assert got is not None
    assert got.peak_equity == 20_000.0
    assert got.day_start_equity == 10_000.0
    assert got.day_key == "2024-01-03"


def test_failed_write_leaves_the_previous_snapshot_intact(
    tmp_path: Path, monkeypatch
) -> None:
    """os.replace is the atomic step. If it fails, nothing partial is reachable."""
    path = snapshot_path_for(tmp_path / "j.jsonl")
    save_snapshot(path, _snap(peak_equity=20_000.0))
    before = path.read_text(encoding="utf-8")

    def boom(*_a, **_k):
        raise OSError(5, "I/O error")

    monkeypatch.setattr("straightedge.state.os.replace", boom)
    with pytest.raises(StateUnwritable):
        save_snapshot(path, _snap(peak_equity=99_000.0))
    assert path.read_text(encoding="utf-8") == before
    assert not path.with_name(path.name + ".tmp").exists()
    restored = load_snapshot(path)
    assert restored is not None and restored.peak_equity == 20_000.0


def test_nan_is_refused_on_the_way_out_too(tmp_path: Path) -> None:
    """allow_nan=False. A NaN peak would make the drawdown test silently false."""
    path = snapshot_path_for(tmp_path / "j.jsonl")
    with pytest.raises(StateUnwritable):
        save_snapshot(path, _snap(peak_equity=float("nan")))
    assert not path.exists()


# --- production wiring, not a stub ----------------------------------------


def test_engine_announces_an_unreadable_snapshot_at_start(tmp_path: Path) -> None:
    """COULD NOT MEASURE is said at start, not at the first refusal."""
    snapshot_path_for(tmp_path / "j.jsonl").write_text("{trunc", encoding="utf-8")
    engine = _engine(tmp_path)
    engine.start()
    rec = engine.journal.last_event("risk_state_error")
    assert rec is not None
    assert rec.get("reason") == "state_unreadable"
    assert "not valid json" in str(rec.get("error"))
    assert "state_unreadable" in engine.risk_text()
    engine.stop()


def test_engine_puts_the_snapshot_beside_its_journal(tmp_path: Path) -> None:
    """The one un-stubbable seam: the shipped Engine, on a real halt."""
    engine = _engine(tmp_path)
    assert_same_path(engine.risk.state_path, tmp_path / "j.equity.json")
    engine.start()
    engine.broker._balance = 9_700.0  # -3% against the 2% daily cap
    reason = engine.risk.circuit_reason(engine.broker.account(), DAY1)
    assert reason == "daily_loss"
    assert engine.risk.state_path.exists()
    engine.stop()

    second = _engine(tmp_path)
    second.start()
    second.broker._balance = 9_700.0
    assert second.risk.circuit_reason(second.broker.account(), DAY1) == "daily_loss"
    second.stop()
