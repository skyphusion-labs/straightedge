from datetime import datetime, timezone
from pathlib import Path

from straightedge.broker.paper import default_spec
from straightedge.config import BotConfig
from straightedge.models import Account, Position, Side, Signal, SignalKind, Tick
from straightedge.risk import RiskManager, currency_exposure, in_session, parse_fx
from straightedge.state import snapshot_path_for


def _acct(equity: float = 10_000, **kw) -> Account:
    return Account(
        login=1,
        balance=kw.get("balance", equity),
        equity=equity,
        margin=kw.get("margin", 0.0),
        margin_free=kw.get("margin_free", equity),
        profit=kw.get("profit", 0.0),
        leverage=100,
        currency="USD",
        trade_allowed=kw.get("trade_allowed", True),
        trade_expert=True,
        trade_mode=kw.get("trade_mode", 0),
    )


def _sig(kind=SignalKind.BUY, symbol="EURUSD", entry=1.10, sl=1.095, tp=1.1125, atr=0.003) -> Signal:
    return Signal(kind, symbol, entry, sl, tp, atr, reason="test")


def _tick(bid=1.0999, ask=1.1001) -> Tick:
    return Tick(time=0, bid=bid, ask=ask)


def _now() -> datetime:
    return datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)  # Wednesday noon UTC


def _cfg(tmp_path: Path, **kw) -> BotConfig:
    """Keep the equity snapshot inside tmp_path.

    RiskManager persists its snapshot beside cfg.journal_path (issue #7). With
    the default relative path every test in this file would share one
    ./journal.equity.json and read each other's peak.
    """
    cfg = BotConfig(**kw)
    cfg.journal_path = str(tmp_path / "j.jsonl")
    return cfg


def test_parse_fx() -> None:
    assert parse_fx("EURUSD") == ("EUR", "USD")
    assert parse_fx("USDJPYm") == ("USD", "JPY")


def test_currency_exposure_blocks_third_usd() -> None:
    positions = [
        Position(1, "EURUSD", Side.BUY, 0.1, 1.1, 1.09, 1.12, 1.1, 0, magic=1),
        Position(2, "GBPUSD", Side.BUY, 0.1, 1.2, 1.19, 1.22, 1.2, 0, magic=1),
    ]
    exp = currency_exposure(positions, extra=("AUDUSD", Side.BUY))
    assert exp["USD"] == -3


def test_session_london_ny() -> None:
    from straightedge.config import SessionConfig

    s = SessionConfig()
    assert in_session(datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc), s)
    assert not in_session(datetime(2024, 1, 3, 5, 0, tzinfo=timezone.utc), s)
    assert not in_session(datetime(2024, 1, 5, 17, 0, tzinfo=timezone.utc), s)  # Friday 17:00


def test_halt_file(tmp_path: Path) -> None:
    (tmp_path / "HALT").write_text("stop\n")
    rm = RiskManager(_cfg(tmp_path), halt_dir=tmp_path)
    d = rm.evaluate(
        account=_acct(),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    assert not d.allowed and d.halt and d.flatten


def test_daily_loss_halt(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rm = RiskManager(cfg)
    rm.observe(_acct(10_000), _now())
    d = rm.evaluate(
        account=_acct(9_700),  # -3% vs 2% daily cap
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    assert d.halt and d.reason == "daily_loss"


def test_drawdown_halt(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    rm = RiskManager(cfg)
    day1 = _now()
    day2 = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
    rm.observe(_acct(10_000), day1)
    rm.observe(_acct(9_000), day2)  # new UTC day: daily clock resets, peak does not
    d = rm.evaluate(
        account=_acct(8_900),  # 11% off peak, 1.1% on the day
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=day2,
    )
    assert d.halt and d.reason == "max_drawdown"


def test_live_blocked_without_flag(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, mode="mt5", live_accepted=False)
    rm = RiskManager(cfg)
    d = rm.evaluate(
        account=_acct(trade_mode=2),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    assert not d.allowed and d.reason == "live_not_accepted"


def test_live_blocked_without_flag_mt4(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path, mode="mt4", live_accepted=False)
    rm = RiskManager(cfg)
    d = rm.evaluate(
        account=_acct(trade_mode=2),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    assert not d.allowed and d.reason == "live_not_accepted"


def test_operator_halt_clears_file_not_drawdown(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.risk.halt_file = str(tmp_path / "HALT")
    rm = RiskManager(cfg, halt_dir=tmp_path)
    day1 = _now()
    day2 = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
    rm.observe(_acct(10_000), day1)
    rm.observe(_acct(9_000), day2)
    d = rm.evaluate(
        account=_acct(8_900),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=day2,
    )
    assert d.reason == "max_drawdown"
    leftover = rm.clear_operator_halt()
    assert leftover == "max_drawdown"
    rm.write_halt_file("telegram")
    assert rm.halt_path().exists()
    leftover = rm.clear_operator_halt()
    assert leftover == "max_drawdown"
    assert not rm.halt_path().exists()


# --- /resume message correctness (#15 item 4) -------------------------------
#
# test_operator_halt_clears_file_not_drawdown (above) calls clear_operator_halt
# right after the halt trips, with no circuit()/evaluate() call in between --
# so it never exercises the actual defect. In a running bot the poll loop
# calls circuit() on every tick, and circuit()'s halt_file check unconditionally
# overwrites _halt_reason to "halt_file" whenever the operator HALT file
# exists, clobbering whatever reason (daily_loss, max_drawdown, or a
# state-integrity halt) was already there. These tests insert that tick.
#
# For daily_loss and max_drawdown the underlying gate still refuses on the
# NEXT evaluate() (every gate recomputes from the snapshot), so the issue's
# own framing -- "message defect, not a safety hole" -- holds for those two.
# It does NOT hold for state_unreadable / state_unwritable: neither is
# recomputed by circuit() on each call (the file is read once, at start), so
# the clobbering bug let clear_operator_halt() silently drop a COULD NOT
# MEASURE halt with nothing to re-derive it from. That is wider than the
# filed issue and is fixed here too.


def test_resume_after_a_poll_tick_still_reports_daily_loss(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.risk.halt_file = str(tmp_path / "HALT")
    rm = RiskManager(cfg, halt_dir=tmp_path)
    rm.observe(_acct(10_000), _now())
    d = rm.evaluate(
        account=_acct(9_700),  # -3% vs 2% daily cap
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    assert d.reason == "daily_loss"
    rm.write_halt_file("telegram")
    # The poll tick a running bot would make between /halt and /resume.
    rm.evaluate(
        account=_acct(9_700),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    leftover = rm.clear_operator_halt()
    assert leftover == "daily_loss", (
        "/resume would have replied 'trading may resume' during a live daily_loss halt"
    )


def test_resume_after_a_poll_tick_still_reports_max_drawdown(tmp_path: Path) -> None:
    cfg = _cfg(tmp_path)
    cfg.risk.halt_file = str(tmp_path / "HALT")
    rm = RiskManager(cfg, halt_dir=tmp_path)
    day1 = _now()
    day2 = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
    rm.observe(_acct(10_000), day1)
    rm.observe(_acct(9_000), day2)
    kwargs = dict(
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
    )
    d = rm.evaluate(account=_acct(8_900), now=day2, **kwargs)
    assert d.reason == "max_drawdown"
    rm.write_halt_file("telegram")
    rm.evaluate(account=_acct(8_900), now=day2, **kwargs)  # the poll tick
    leftover = rm.clear_operator_halt()
    assert leftover == "max_drawdown"


def test_resume_does_not_clear_a_state_unreadable_halt(tmp_path: Path) -> None:
    """The sharper finding: this reason is not equity-recomputed, so a
    silently cleared halt here is not a stale message, it is disabled
    daily-loss/drawdown protection with no indication anything changed."""
    path = snapshot_path_for(tmp_path / "j.jsonl")
    path.write_text('{"version": 1, "day_key": "2024-01-03", "peak_eq', encoding="utf-8")
    cfg = _cfg(tmp_path)
    cfg.risk.halt_file = str(tmp_path / "HALT")
    rm = RiskManager(cfg, halt_dir=tmp_path)
    assert rm.halt_reason == "state_unreadable"
    rm.write_halt_file("telegram")
    rm.evaluate(  # the poll tick
        account=_acct(10_000),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    leftover = rm.clear_operator_halt()
    assert leftover == "state_unreadable", (
        "/resume silently cleared a COULD NOT MEASURE halt"
    )
    assert rm.halt_reason == "state_unreadable"
    d = rm.evaluate(
        account=_acct(10_000),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    assert d.reason == "state_unreadable" and not d.allowed


def test_resume_does_not_reopen_persist_state_after_state_unreadable(
    tmp_path: Path,
) -> None:
    """_persist_state's own reentry guard reads the same clobberable slot;
    prove the corrupt file (the operator's evidence) survives a halt/resume
    cycle too, not just the in-memory reason."""
    path = snapshot_path_for(tmp_path / "j.jsonl")
    body = '{"version": 1, "day_key": "2024-01-03", "peak_eq'
    path.write_text(body, encoding="utf-8")
    cfg = _cfg(tmp_path)
    cfg.risk.halt_file = str(tmp_path / "HALT")
    rm = RiskManager(cfg, halt_dir=tmp_path)
    rm.write_halt_file("telegram")
    rm.evaluate(
        account=_acct(10_000),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    rm.clear_operator_halt()
    # A snapshot-moving observe() would try to persist here on the old code
    # path, once _halt_reason no longer read "state_unreadable".
    rm.observe(_acct(10_500), _now())
    assert path.read_text(encoding="utf-8") == body, "the evidence file was overwritten"


def test_persist_state_guard_survives_a_second_tick_before_any_resume(
    tmp_path: Path,
) -> None:
    """Sharper than the /resume case above: two poll ticks with the operator
    HALT file present, and no /resume at all. circuit() clobbers
    _halt_reason to "halt_file" on the FIRST tick; if _persist_state's guard
    read that same slot, the SECOND tick's snapshot-moving observe() would
    overwrite the evidence file before anyone ever touched /resume."""
    path = snapshot_path_for(tmp_path / "j.jsonl")
    body = '{"version": 1, "day_key": "2024-01-03", "peak_eq'
    path.write_text(body, encoding="utf-8")
    cfg = _cfg(tmp_path)
    cfg.risk.halt_file = str(tmp_path / "HALT")
    rm = RiskManager(cfg, halt_dir=tmp_path)
    rm.write_halt_file("telegram")
    kwargs = dict(
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    rm.evaluate(account=_acct(10_000), **kwargs)  # tick 1: clobbers _halt_reason
    assert rm._halt_reason == "halt_file"  # confirms the clobber actually happened
    rm.evaluate(account=_acct(10_500), **kwargs)  # tick 2: equity moved, would persist
    assert path.read_text(encoding="utf-8") == body, "the evidence file was overwritten"


def test_rr_and_size_ok(tmp_path: Path) -> None:
    rm = RiskManager(_cfg(tmp_path))
    d = rm.evaluate(
        account=_acct(),
        signal=_sig(),
        spec=default_spec("EURUSD"),
        tick=_tick(),
        positions=[],
        orders=[],
        now=_now(),
    )
    assert d.allowed
    assert d.volume > 0
