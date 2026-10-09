"""`VenueClock`: the measured venue offset, and what it refuses (#172).

The behavioural regression lives in `tests/test_bar_clock_is_not_utc.py`; this
file is the instrument's own suite. Three things are measured here, and the
third is the one that matters most:

1. The type cannot represent a half-measurement at all.
2. Each adapter measures the offset from a reading, and reports NOT MEASURED
   rather than UTC when the reading is missing.
3. Every refusal arm is driven to its refusal, and the SAME setup is driven to
   a measurement, so the guard is known to be able to go both ways. A guard
   only ever seen refusing is not known to be a guard.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from straightedge.broker.base import venue_clock_of
from straightedge.broker.mt4_live import MT4_CLOCK_SOURCE, Mt4Broker
from straightedge.broker.mt5_live import MT5_CLOCK_SOURCE, Mt5Broker
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.constants import (
    VENUE_CLOCK_GRID_SEC,
    VENUE_CLOCK_MAX_OFFSET_SEC,
    VENUE_CLOCK_MIN_OFFSET_SEC,
    VENUE_CLOCK_TOLERANCE_SEC,
)
from straightedge.engine import VENUE_CLOCK_UNMEASURED, Engine
from straightedge.__main__ import venue_clock_check
from straightedge.models import VenueClock, VenueClockUnmeasured
from straightedge.synthetic import generate_bars

HOUR = 3600
PLUS_3 = 3 * HOUR
#: An arbitrary but fixed UTC instant to pair samples against.
NOW = datetime(2024, 1, 4, 14, 0, tzinfo=timezone.utc)
NOW_EPOCH = NOW.timestamp()


# --- 1. the type -----------------------------------------------------------


def test_the_two_fields_that_carry_one_fact_cannot_disagree() -> None:
    """`offset_sec is None` and a non-empty `unmeasured` are the same fact."""
    with pytest.raises(ValueError):
        VenueClock(offset_sec=None)
    with pytest.raises(ValueError):
        VenueClock(offset_sec=0, unmeasured=frozenset({"offset_sec"}))


def test_a_measured_clock_converts_server_time_to_the_real_instant() -> None:
    clock = VenueClock(offset_sec=PLUS_3, source="test")
    server_stamp = int((NOW + timedelta(seconds=PLUS_3)).timestamp())
    assert clock.to_utc(server_stamp) == NOW
    assert clock.measured


def test_an_unmeasured_clock_refuses_to_convert_rather_than_assuming_utc() -> None:
    """The backstop behind the engine's own check: no plausible answer."""
    clock = VenueClock.not_measured("server_time", source="test")
    assert not clock.measured
    with pytest.raises(VenueClockUnmeasured):
        clock.to_utc(int(NOW_EPOCH))


# --- 2. the measurement, arm by arm ----------------------------------------


def test_a_fresh_sample_is_the_measurement() -> None:
    clock = VenueClock.measure(int(NOW_EPOCH) + PLUS_3, NOW_EPOCH, source="s")
    assert clock.offset_sec == PLUS_3
    assert clock.measured_at == int(NOW_EPOCH)
    assert clock.source == "s"


def test_a_slightly_stale_sample_still_lands_on_its_grid_point() -> None:
    """The venue stamp is the last TICK, so a few seconds of lag is normal."""
    stale = int(NOW_EPOCH) + PLUS_3 - 40
    assert VenueClock.measure(stale, NOW_EPOCH, source="s").offset_sec == PLUS_3


def test_a_half_hour_venue_is_measured_too() -> None:
    """Whole hours are not the only real offsets, which is why the grid is 15m."""
    offset = 5 * HOUR + 1800
    got = VenueClock.measure(int(NOW_EPOCH) + offset, NOW_EPOCH, source="s")
    assert got.offset_sec == offset


def test_a_sample_off_the_grid_is_not_rounded_to_a_plausible_answer() -> None:
    off = VENUE_CLOCK_TOLERANCE_SEC + 60
    got = VenueClock.measure(int(NOW_EPOCH) + PLUS_3 + off, NOW_EPOCH, source="s")
    assert not got.measured
    assert got.unmeasured == frozenset({"offset_sec"})
    assert "off the" in got.detail


def test_a_frozen_venue_clock_is_refused_by_the_band_and_not_by_the_grid() -> None:
    """The case the grid check structurally CANNOT see.

    A venue clock frozen at Friday's close is tens of hours out by Saturday,
    and tens of hours is a whole number of 15 minute steps, so it lands
    EXACTLY on a grid point and the grid check reports it as a measurement.
    Found by running that input; the civil timezone band is what rejects it.
    """
    frozen = int(NOW_EPOCH) - 2 * 24 * HOUR
    assert (frozen - int(NOW_EPOCH)) % VENUE_CLOCK_GRID_SEC == 0, (
        "this input has to sit ON the grid or it is not testing the band"
    )
    got = VenueClock.measure(frozen, NOW_EPOCH, source="s")
    assert not got.measured
    assert "civil timezone band" in got.detail


def test_the_band_edges_are_measurements_and_one_step_past_them_is_not() -> None:
    """A real UTC+14 venue must not be refused for being unusual."""
    for edge in (VENUE_CLOCK_MIN_OFFSET_SEC, VENUE_CLOCK_MAX_OFFSET_SEC):
        got = VenueClock.measure(int(NOW_EPOCH) + edge, NOW_EPOCH, source="s")
        assert got.offset_sec == edge, f"{edge} is a real timezone"
    for past in (
        VENUE_CLOCK_MIN_OFFSET_SEC - VENUE_CLOCK_GRID_SEC,
        VENUE_CLOCK_MAX_OFFSET_SEC + VENUE_CLOCK_GRID_SEC,
    ):
        assert not VenueClock.measure(int(NOW_EPOCH) + past, NOW_EPOCH, source="s").measured


def test_a_round_trip_too_wide_to_pair_is_not_a_sample() -> None:
    """There the PAIRING failed, not the venue, and the detail says so."""
    got = VenueClock.measure(
        int(NOW_EPOCH) + PLUS_3,
        NOW_EPOCH,
        source="s",
        round_trip_sec=2 * VENUE_CLOCK_TOLERANCE_SEC + 2,
    )
    assert not got.measured
    assert "round trip" in got.detail


def test_a_venue_stamp_of_zero_names_the_wire_field() -> None:
    got = VenueClock.measure(0, NOW_EPOCH, source="s")
    assert got.unmeasured == frozenset({"server_time"})


# --- 3. the venues ---------------------------------------------------------


class _FakeMt4Call:
    """The mailbox, answering `tick` with a server stamp the test chooses."""

    def __init__(self, server_time: int | None) -> None:
        self.server_time = server_time
        self.ops: list[str] = []

    def __call__(self, op: str, payload: dict) -> dict:
        self.ops.append(op)
        if op == "ping":
            return {"ok": True, "ladder_ms": 950, "broker_calls": 19, "fence": 1}
        if op == "tick":
            d: dict = {"ok": True, "bid": 1.1, "ask": 1.1002}
            if self.server_time is not None:
                d["time"] = self.server_time
            return d
        return {"ok": True}


def test_mt4_measures_the_offset_off_the_tick_reply_it_already_sends() -> None:
    """No Expert change: `time=TimeCurrent()` is in the shipped ICD."""
    call = _FakeMt4Call(int(NOW_EPOCH) + PLUS_3)
    broker = Mt4Broker(call, now_fn=lambda: NOW_EPOCH)
    clock = broker.venue_clock("EURUSD")
    assert clock.offset_sec == PLUS_3
    assert clock.source == MT4_CLOCK_SOURCE
    assert call.ops == ["tick"], "the measurement must not need a second op"


def test_mt4_reports_an_expert_that_does_not_stamp_its_reply_as_unmeasured() -> None:
    """An Expert too old to answer is NOT MEASURED, never UTC."""
    broker = Mt4Broker(_FakeMt4Call(None), now_fn=lambda: NOW_EPOCH)
    clock = broker.venue_clock("EURUSD")
    assert clock.unmeasured == frozenset({"server_time"})
    assert "TimeCurrent" in clock.detail


class _FakeMt5Clock:
    def __init__(self, tick_time: int | None) -> None:
        self.tick_time = tick_time

    def symbol_info_tick(self, name: str):
        del name
        fields = {"time": self.tick_time, "bid": 1.1, "ask": 1.1002}
        return SimpleNamespace(**fields, _asdict=lambda: dict(fields))

    def last_error(self):
        return (1, "no error")


def test_mt5_is_affected_identically_and_measures_the_same_way() -> None:
    """MT5 bar and tick times are the trade server's clock, like MT4's."""
    fake = _FakeMt5Clock(int(NOW_EPOCH) + PLUS_3)
    broker = Mt5Broker(mt5=fake, now_fn=lambda: NOW_EPOCH)
    clock = broker.venue_clock("EURUSD")
    assert clock.offset_sec == PLUS_3
    assert clock.source == MT5_CLOCK_SOURCE


def test_mt5_reports_a_tick_without_a_server_time_as_unmeasured() -> None:
    broker = Mt5Broker(mt5=_FakeMt5Clock(0), now_fn=lambda: NOW_EPOCH)
    assert broker.venue_clock("EURUSD").unmeasured == frozenset({"server_time"})


def test_paper_states_the_offset_its_own_bars_are_stamped_with() -> None:
    assert PaperBroker().venue_clock("EURUSD").offset_sec == 0
    assert PaperBroker(utc_offset_sec=PLUS_3).venue_clock("EURUSD").offset_sec == PLUS_3


def test_a_venue_that_cannot_state_a_clock_at_all_is_unmeasured() -> None:
    """Absence is NOT MEASURED. Reading it as UTC is the #172 defect moved."""

    class Older:
        pass

    clock = venue_clock_of(Older(), "EURUSD")
    assert clock.unmeasured == frozenset({"venue_clock"})
    assert clock.source == "Older"


# --- 4. the engine's refusal, and that it can go the other way -------------


class _MutePaper(PaperBroker):
    """Paper, with the one method removed, so the engine meets a mute venue."""

    venue_clock = None  # type: ignore[assignment]


def _engine(tmp_path: Path, broker: PaperBroker) -> tuple[Engine, list]:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.risk.max_spread_atr_frac = 10.0
    bars = generate_bars(250, drift=0.0006, vol=0.0002, seed=7)
    broker.seed_bars("EURUSD", bars)
    engine = Engine(
        cfg,
        broker,
        halt_dir=str(tmp_path),
        now_fn=lambda: datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc),
    )
    return engine, bars


def test_the_auto_leg_refuses_when_the_venue_cannot_state_its_clock(
    tmp_path: Path,
) -> None:
    """COULD NOT MEASURE, with a named reason, and nothing sent."""
    engine, bars = _engine(tmp_path, _MutePaper(balance=10_000))
    engine.start()
    engine.replay_symbol("EURUSD", bars)
    rec = engine.journal.last_event("reject")
    assert rec is not None, "a refusal that leaves no record is not observable"
    assert rec["reason"] == VENUE_CLOCK_UNMEASURED
    assert rec["source"] == "auto"
    assert rec["unmeasured"] == ["venue_clock"]
    assert not engine.broker.positions(magic=engine.cfg.risk.magic)
    engine.stop()


def test_the_same_setup_with_a_measured_clock_does_not_refuse(
    tmp_path: Path,
) -> None:
    """The positive control. A guard only ever seen refusing proves nothing.

    Same engine, same bars, same instant; the ONE difference is a venue that
    can state its clock. The refusal above is therefore attributable to the
    clock and not to the fixture.
    """
    engine, bars = _engine(tmp_path, PaperBroker(balance=10_000))
    engine.start()
    engine.replay_symbol("EURUSD", bars)
    reasons = [
        r.get("reason")
        for r in engine.journal.tail(2000)
        if r.get("event") == "reject"
    ]
    assert VENUE_CLOCK_UNMEASURED not in reasons
    engine.stop()


# --- 5. the operator can SEE which clock the desk is using ------------------


def test_doctor_states_the_measured_offset(capsys: pytest.CaptureFixture) -> None:
    """The defect was invisible: nothing printed the three hours.

    `start_utc = "07:00"` in the config and a UTC+3 bar stamp in the engine
    disagreed by three hours and no instrument anywhere stated either one.
    """
    cfg = BotConfig()
    rc = venue_clock_check(cfg, PaperBroker(utc_offset_sec=PLUS_3))
    out = capsys.readouterr().out
    assert rc == 0
    assert "server UTC+03:00 measured" in out


def test_doctor_states_a_negative_offset_as_negative(
    capsys: pytest.CaptureFixture,
) -> None:
    """A western server is not an error, and must not print as one."""
    venue_clock_check(BotConfig(), PaperBroker(utc_offset_sec=-5 * HOUR))
    assert "server UTC-05:00 measured" in capsys.readouterr().out


def test_doctor_goes_red_when_the_venue_cannot_state_its_clock(
    capsys: pytest.CaptureFixture,
) -> None:
    """NOT MEASURED is a run-affecting condition, so the gate exits non-zero.

    The auto leg refuses every signal in this state, which is exactly the
    condition `doctor` exists to catch BEFORE a run rather than during one.
    """
    rc = venue_clock_check(BotConfig(), _MutePaper())
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT MEASURED" in out
    assert "venue_clock" in out
