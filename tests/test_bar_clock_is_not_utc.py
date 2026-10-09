"""The regression test for straightedge#172, written to go RED on `main`.

MT4 and MT5 stamp a bar with the BROKER SERVER's wall clock. `engine._act`
read that integer and built a datetime with `tz=timezone.utc`, which RELABELS
the instant instead of converting it, so on a UTC+3 server every gate
downstream of the auto leg ran three hours off: the session window, the
`day_key` the daily-loss budget is measured against, and `weekday()`.

The suite could not see any of this. Every other test seeds bars whose
timestamps ARE UTC, so the fixture agreed with the defect and 1192 tests were
green over it. The lever these tests add is a venue whose own clock is NOT
UTC, which is the one input the old fixtures never supplied.

Nothing here imports anything that exists only after the fix. The venue's
offset is handed over by ASSIGNING `utc_offset_sec` on the paper broker, which
is inert on `main` and load-bearing after the fix, so this exact file runs on
both sides of the change and its red is the defect rather than a missing
import. The API's own tests are in `tests/test_venue_clock.py`.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine
from straightedge.models import Account
from straightedge.synthetic import generate_bars

HOUR = 3600
#: MetaQuotes-Demo, the server the live desk is on, measured at UTC+3 in October.
PLUS_3 = 3 * HOUR
#: A Sydney-hosted server in southern summer. Big enough that relabelling moves
#: the WEEKDAY, not just the hour.
PLUS_10 = 10 * HOUR

#: Thursday, inside the shipped 07:00-17:00 UTC window, and before the Friday
#: cutoff can be an excuse for the refusal.
THU_1400 = datetime(2024, 1, 4, 14, 0, tzinfo=timezone.utc)
#: Thursday evening. The UTC day has NOT rolled; on a UTC+3 server the bar
#: clock already reads Friday.
THU_2230 = datetime(2024, 1, 4, 22, 30, tzinfo=timezone.utc)
THU_1200 = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
#: Sunday evening, the illiquid open. On a UTC+10 server the bar clock reads
#: Monday 08:00, which is inside the window on a weekday.
SUN_2200 = datetime(2024, 1, 7, 22, 0, tzinfo=timezone.utc)


class _OffsetPaper(PaperBroker):
    """Paper, plus the two things a clock test has to be able to move.

    `equity_override` makes the daily-loss halt REAL rather than poked into
    the snapshot: the gate computes it from the account the way it does in
    production.
    """

    def __init__(self, **kw: object) -> None:
        super().__init__(**kw)  # type: ignore[arg-type]
        self.equity_override: float | None = None

    def account(self) -> Account:
        acct = super().account()
        if self.equity_override is None:
            return acct
        return replace(acct, equity=self.equity_override)


def _bars_stamped(server_last: datetime, n: int = 250) -> list:
    """Bars whose `time` is the SERVER's wall clock, as the venues deliver them.

    `generate_bars` takes an epoch, and the venue hands us the server's wall
    clock encoded as if it were UTC. That is exactly what the defect reads, so
    the fixture builds it the same way.
    """
    start = int(server_last.timestamp()) - (n - 1) * HOUR
    bars = generate_bars(n, drift=0.0006, vol=0.0002, seed=7, start_ts=start)
    assert bars[-1].time == int(server_last.timestamp())
    return bars


def _engine(
    tmp_path: Path, *, now: datetime, offset_sec: int, balance: float = 10_000.0
) -> tuple[Engine, list[datetime], _OffsetPaper]:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = True
    cfg.risk.halt_file = str(tmp_path / "HALT")
    # The spread gate is not what this file measures.
    cfg.risk.max_spread_atr_frac = 10.0
    broker = _OffsetPaper(balance=balance)
    # Assigned, not passed: see the module docstring. A venue that stamps its
    # bars in UTC+N declares exactly that, and the desk measures it.
    broker.utc_offset_sec = offset_sec
    clock = [now]
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: clock[0])
    return engine, clock, broker


def _rejects(engine: Engine) -> list[dict]:
    return [r for r in engine.journal.tail(2000) if r.get("event") == "reject"]


def _reasons(engine: Engine) -> list[str]:
    return [str(r.get("reason", "")) for r in _rejects(engine)]


def test_the_session_window_is_measured_against_the_real_utc_instant(
    tmp_path: Path,
) -> None:
    """14:00 UTC on a UTC+3 server is inside 07:00-17:00. It was refused.

    This is the measured finding from the live box: three auto signals refused
    `outside_session` at 14:00, 15:00 and 16:00 UTC with the window configured
    07:00-17:00. On `main` the last bar reads 17:00 server, `in_session`
    computes minutes=1020, and `start <= minutes < end` is False on the
    boundary exactly.
    """
    engine, _clock, broker = _engine(tmp_path, now=THU_1400, offset_sec=PLUS_3)
    bars = _bars_stamped(THU_1400 + timedelta(seconds=PLUS_3))
    broker.seed_bars("EURUSD", bars)
    engine.start()
    engine.replay_symbol("EURUSD", bars)
    assert "outside_session" not in _reasons(engine), (
        "the auto leg refused an in-window instant, so it is timing itself off "
        "the broker clock and not off UTC: " + repr(_reasons(engine))
    )
    engine.stop()


def test_the_daily_budget_does_not_roll_on_the_broker_day(tmp_path: Path) -> None:
    """One budget, one boundary. The auto leg rolled on the BROKER day.

    22:30 UTC Thursday is 01:30 Friday on a UTC+3 server. `observe()` rolls
    `day_key` and RESETS `day_start_equity` to the current, depressed equity,
    which hands back a loss budget that the UTC day the operator configured
    has not finished.
    """
    engine, clock, broker = _engine(tmp_path, now=THU_1200, offset_sec=PLUS_3)
    bars = _bars_stamped(THU_2230 + timedelta(seconds=PLUS_3))
    broker.seed_bars("EURUSD", bars)
    engine.start()
    assert engine.risk.snapshot.day_key == "2024-01-04"
    assert engine.risk.snapshot.day_start_equity == 10_000.0

    # 3% down on a 2% budget: the halt is earned, not injected.
    broker.equity_override = 9_700.0
    clock[0] = THU_2230
    engine.replay_symbol("EURUSD", bars)

    assert engine.risk.snapshot.day_key == "2024-01-04", (
        "the auto leg rolled the day on the broker clock, so the daily-loss "
        "budget has a different boundary than the desk and the recap"
    )
    assert engine.risk.snapshot.day_start_equity == 10_000.0, (
        "day_start_equity was re-baselined to the depressed equity, which IS "
        "the fresh budget"
    )
    assert engine.risk.halt_reason == "daily_loss"
    engine.stop()


def test_a_daily_loss_halt_is_not_released_by_the_broker_midnight(
    tmp_path: Path,
) -> None:
    """The sharp one. A halt whose job is to stop trading for the rest of the
    day released up to the broker offset early, because `observe()` clears it
    on the day-key change and the broker day crosses midnight first.
    """
    engine, clock, broker = _engine(tmp_path, now=THU_1200, offset_sec=PLUS_3)
    bars = _bars_stamped(THU_2230 + timedelta(seconds=PLUS_3))
    broker.seed_bars("EURUSD", bars)
    engine.start()

    broker.equity_override = 9_700.0
    trip = engine.risk.circuit(broker.account(), THU_1200)
    assert trip.halt and engine.risk.halt_reason == "daily_loss", (
        "the test did not manage to trip the halt it is about to measure"
    )

    # Same UTC day, three hours before UTC midnight. The broker day HAS rolled.
    clock[0] = THU_2230
    engine.replay_symbol("EURUSD", bars)
    assert engine.risk.halt_reason == "daily_loss", (
        "the daily-loss halt was cleared while it is still 2024-01-04 in UTC, "
        "which is the unit the operator's config is written in"
    )
    engine.stop()


def test_the_weekend_block_is_measured_against_the_real_utc_instant(
    tmp_path: Path,
) -> None:
    """`weekday()` off a relabelled stamp is a DAY-sized error, not an hour.

    22:00 UTC Sunday on a UTC+10 server stamps the bar Monday 08:00, which is
    a weekday inside the window, so the Sat/Sun block does not fire during the
    Sunday open.
    """
    engine, _clock, broker = _engine(tmp_path, now=SUN_2200, offset_sec=PLUS_10)
    bars = _bars_stamped(SUN_2200 + timedelta(seconds=PLUS_10))
    broker.seed_bars("EURUSD", bars)
    engine.start()
    engine.replay_symbol("EURUSD", bars)
    assert "outside_session" in _reasons(engine), (
        "the auto leg traded the Sunday open: the weekend block read the "
        "broker's Monday, not UTC's Sunday"
    )
    assert not engine.broker.positions(magic=engine.cfg.risk.magic)
    engine.stop()
