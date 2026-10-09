"""`VenueClock`: the measured venue offset, and what it refuses (#172).

The behavioural regression lives in `tests/test_bar_clock_is_not_utc.py`; this
file is the instrument's own suite.

The straightedge#182 review is why half of it exists. The first version of this
change carried a chosen 180s tolerance and a civil-timezone band, and the
review swept staleness through the REAL adapter with `TimeCurrent()` frozen at
Friday's close on a genuinely UTC+3 server:

    2h after close  -> "server UTC+01:00 measured"   exit 0
    5h              -> "server UTC-02:00 measured"   exit 0
    11h             -> "server UTC-08:00 measured"   exit 0
    15h+            -> NOT MEASURED                  exit 1

The band is not a freshness check: it only bites once offset-minus-staleness
leaves the civil range. Everything from about twelve minutes to fifteen hours
was accepted with an offset wrong by up to fifteen hours and the word
`measured` printed beside it, to a human deciding whether to start a live loop,
every weekend. That sweep is reproduced here as `TestTheFrozenFridaySweep` and
it is the test this file exists for.

The fix is that the staleness bound is now an ARGUMENT with no default, so a
caller that cannot measure one cannot obtain a measured clock, and `doctor`
(which has no previous poll) is structurally unable to print a measurement.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from straightedge.__main__ import venue_clock_cadence_check, venue_clock_check
from straightedge.broker.base import venue_clock_of
from straightedge.broker.mt4_live import MT4_CLOCK_SOURCE, Mt4Broker
from straightedge.broker.mt5_live import MT5_CLOCK_SOURCE, Mt5Broker
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.constants import (
    VENUE_CLOCK_GRID_SEC,
    VENUE_CLOCK_MAX_OFFSET_SEC,
    VENUE_CLOCK_MIN_OFFSET_SEC,
)
from straightedge.engine import VENUE_CLOCK_UNMEASURED, Engine
from straightedge.models import VenueClock, VenueClockUnmeasured
from straightedge.synthetic import generate_bars

HOUR = 3600
PLUS_3 = 3 * HOUR
#: A poll cadence the desk actually runs at, used as the measured bound.
POLL = 15.0
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


def test_a_measurement_and_an_implication_cannot_coexist() -> None:
    """An implication is what there is INSTEAD of a measurement."""
    with pytest.raises(ValueError):
        VenueClock(offset_sec=PLUS_3, implied_offset_sec=PLUS_3)


def test_a_measured_clock_converts_server_time_to_the_real_instant() -> None:
    clock = VenueClock.declared(PLUS_3, source="test")
    server_stamp = int((NOW + timedelta(seconds=PLUS_3)).timestamp())
    assert clock.to_utc(server_stamp) == NOW
    assert clock.measured


def test_an_unmeasured_clock_refuses_to_convert_rather_than_assuming_utc() -> None:
    """The backstop behind the engine's own check: no plausible answer."""
    clock = VenueClock.not_measured("server_time", source="test")
    assert not clock.measured
    with pytest.raises(VenueClockUnmeasured):
        clock.to_utc(int(NOW_EPOCH))


def test_an_implied_clock_refuses_to_convert_too() -> None:
    """Printable is not actionable. This is the whole point of the field."""
    clock = VenueClock.implied(int(NOW_EPOCH) + PLUS_3, NOW_EPOCH, source="t")
    assert clock.implied_offset_sec == PLUS_3
    assert not clock.measured
    assert clock.offset_sec is None
    with pytest.raises(VenueClockUnmeasured):
        clock.to_utc(int(NOW_EPOCH) + PLUS_3)


# --- 2. the bound is structural, not documentary ---------------------------


def test_measure_cannot_be_called_without_a_staleness_bound() -> None:
    """The review's finding, pinned as a type error.

    A bound with a default is a bound that gets inherited: it was argued
    correctly for one of three callers and then written down unconditionally
    in three documents. A caller that cannot measure one must not be able to
    reach this method at all.
    """
    with pytest.raises(TypeError):
        VenueClock.measure(int(NOW_EPOCH) + PLUS_3, NOW_EPOCH, source="s")  # type: ignore[call-arg]


def test_a_bound_too_wide_for_the_grid_measures_nothing() -> None:
    """Twice the uncertainty must stay under one grid step, or several fit."""
    got = VenueClock.measure(
        int(NOW_EPOCH) + PLUS_3,
        NOW_EPOCH,
        source="s",
        max_staleness_sec=VENUE_CLOCK_GRID_SEC / 2,
    )
    assert not got.measured
    assert "too wide" in got.detail


def test_the_round_trip_counts_toward_the_same_uncertainty() -> None:
    """A sample read across a wide round trip is not a tight sample."""
    got = VenueClock.measure(
        int(NOW_EPOCH) + PLUS_3,
        NOW_EPOCH,
        source="s",
        max_staleness_sec=POLL,
        round_trip_sec=VENUE_CLOCK_GRID_SEC,
    )
    assert not got.measured
    assert "too wide" in got.detail


# --- 3. the measurement, arm by arm ----------------------------------------


def test_a_bounded_sample_is_the_measurement() -> None:
    clock = VenueClock.measure(
        int(NOW_EPOCH) + PLUS_3, NOW_EPOCH, source="s", max_staleness_sec=POLL
    )
    assert clock.offset_sec == PLUS_3
    assert clock.measured_at == int(NOW_EPOCH)
    assert clock.source == "s"


def test_a_sample_stale_within_its_bound_still_lands_on_its_grid_point() -> None:
    """The venue stamp is the last TICK, so lag inside the bound is normal."""
    stale = int(NOW_EPOCH) + PLUS_3 - 12
    got = VenueClock.measure(stale, NOW_EPOCH, source="s", max_staleness_sec=POLL)
    assert got.offset_sec == PLUS_3


def test_a_sample_staler_than_its_bound_is_refused() -> None:
    """The residual check, doing the one job it can do."""
    stale = int(NOW_EPOCH) + PLUS_3 - 10 * POLL
    got = VenueClock.measure(stale, NOW_EPOCH, source="s", max_staleness_sec=POLL)
    assert not got.measured
    assert "off the" in got.detail


def test_a_half_hour_venue_is_measured_too() -> None:
    """Whole hours are not the only real offsets, which is why the grid is 15m."""
    offset = 5 * HOUR + 1800
    got = VenueClock.measure(
        int(NOW_EPOCH) + offset, NOW_EPOCH, source="s", max_staleness_sec=POLL
    )
    assert got.offset_sec == offset


def test_the_band_edges_are_measurements_and_one_step_past_them_is_not() -> None:
    """A real UTC+14 venue must not be refused for being unusual."""
    for edge in (VENUE_CLOCK_MIN_OFFSET_SEC, VENUE_CLOCK_MAX_OFFSET_SEC):
        got = VenueClock.measure(
            int(NOW_EPOCH) + edge, NOW_EPOCH, source="s", max_staleness_sec=POLL
        )
        assert got.offset_sec == edge, f"{edge} is a real timezone"
    for past in (
        VENUE_CLOCK_MIN_OFFSET_SEC - VENUE_CLOCK_GRID_SEC,
        VENUE_CLOCK_MAX_OFFSET_SEC + VENUE_CLOCK_GRID_SEC,
    ):
        got = VenueClock.measure(
            int(NOW_EPOCH) + past, NOW_EPOCH, source="s", max_staleness_sec=POLL
        )
        assert not got.measured
        assert "civil timezone band" in got.detail


def test_a_venue_stamp_of_zero_names_the_wire_field() -> None:
    for got in (
        VenueClock.measure(0, NOW_EPOCH, source="s", max_staleness_sec=POLL),
        VenueClock.implied(0, NOW_EPOCH, source="s"),
    ):
        assert got.unmeasured == frozenset({"server_time"})


# --- 4. the venues ---------------------------------------------------------


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
    clock = broker.venue_clock("EURUSD", max_staleness_sec=POLL)
    assert clock.offset_sec == PLUS_3
    assert clock.source == MT4_CLOCK_SOURCE
    assert call.ops == ["tick"], "the measurement must not need a second op"


def test_mt4_with_no_bound_reports_an_implication_and_not_a_measurement() -> None:
    call = _FakeMt4Call(int(NOW_EPOCH) + PLUS_3)
    broker = Mt4Broker(call, now_fn=lambda: NOW_EPOCH)
    clock = broker.venue_clock("EURUSD", max_staleness_sec=None)
    assert not clock.measured
    assert clock.unmeasured == frozenset({"freshness"})
    assert clock.implied_offset_sec == PLUS_3


def test_mt4_reports_an_expert_that_does_not_stamp_its_reply_as_unmeasured() -> None:
    """An Expert too old to answer is NOT MEASURED, never UTC."""
    broker = Mt4Broker(_FakeMt4Call(None), now_fn=lambda: NOW_EPOCH)
    clock = broker.venue_clock("EURUSD", max_staleness_sec=POLL)
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
    clock = broker.venue_clock("EURUSD", max_staleness_sec=POLL)
    assert clock.offset_sec == PLUS_3
    assert clock.source == MT5_CLOCK_SOURCE


def test_mt5_with_no_bound_reports_an_implication_too() -> None:
    fake = _FakeMt5Clock(int(NOW_EPOCH) + PLUS_3)
    broker = Mt5Broker(mt5=fake, now_fn=lambda: NOW_EPOCH)
    clock = broker.venue_clock("EURUSD", max_staleness_sec=None)
    assert clock.unmeasured == frozenset({"freshness"})
    assert clock.implied_offset_sec == PLUS_3


def test_mt5_reports_a_tick_without_a_server_time_as_unmeasured() -> None:
    broker = Mt5Broker(mt5=_FakeMt5Clock(0), now_fn=lambda: NOW_EPOCH)
    got = broker.venue_clock("EURUSD", max_staleness_sec=POLL)
    assert got.unmeasured == frozenset({"server_time"})


def test_paper_declares_the_offset_its_own_bars_are_stamped_with() -> None:
    """Declared, not sampled: no server, so no staleness and no bound needed."""
    for bound in (None, POLL):
        assert (
            PaperBroker(utc_offset_sec=PLUS_3).venue_clock(
                "EURUSD", max_staleness_sec=bound
            ).offset_sec
            == PLUS_3
        )
    assert PaperBroker().venue_clock("EURUSD", max_staleness_sec=None).offset_sec == 0


def test_a_venue_that_cannot_state_a_clock_at_all_is_unmeasured() -> None:
    """Absence is NOT MEASURED. Reading it as UTC is the #172 defect moved."""

    class Older:
        pass

    clock = venue_clock_of(Older(), "EURUSD", max_staleness_sec=POLL)
    assert clock.unmeasured == frozenset({"venue_clock"})
    assert clock.source == "Older"


# --- 5. the frozen-Friday sweep: the review's own finding -------------------


class TestTheFrozenFridaySweep:
    """`doctor` must never print a measured offset it cannot vouch for.

    Driven through the REAL `Mt4Broker.venue_clock` and the REAL
    `venue_clock_check`, with the venue genuinely UTC+3 and `TimeCurrent()`
    frozen at Friday's close, exactly as the review swept it.
    """

    #: Staleness, and what the pre-fix gate printed at it. The first three are
    #: the ones that mattered: a confident wrong offset, with exit 0.
    SWEEP = (
        (12 * 60, "UTC+02:45"),
        (2 * HOUR, "UTC+01:00"),
        (5 * HOUR, "UTC-02:00"),
        (11 * HOUR, "UTC-08:00"),
        (15 * HOUR, "outside the band"),
        (26 * HOUR, "outside the band"),
    )

    def _doctor_line(self, capsys, stale_sec: float) -> tuple[int, str]:
        server_now = int(NOW_EPOCH) + PLUS_3
        call = _FakeMt4Call(int(server_now - stale_sec))
        broker = Mt4Broker(call, now_fn=lambda: NOW_EPOCH)
        rc = venue_clock_check(BotConfig(), broker)
        return rc, capsys.readouterr().out

    @pytest.mark.parametrize("stale_sec,was", SWEEP)
    def test_no_staleness_is_reported_as_a_measured_offset(
        self, capsys: pytest.CaptureFixture, stale_sec: float, was: str
    ) -> None:
        """Asserts the PROPERTY and the branch, never the absence of a word.

        This test could not go red (straightedge#193). It read
        `"measured" not in out`, and making `implied()` return a MEASURED
        clock sends doctor down the `if clock.measured:` branch, which prints
        "declared by the venue". The word never appeared, so the test stayed
        green at all six stalenesses while doctor confidently asserted a wrong
        offset. It pinned the WORD, not the CLAIM, and a rewording of either
        print line could have made it decorative again.

        FAILS IF: a venue that SAMPLES a server hands a measured clock to a
        caller that supplied no staleness bound, or doctor reports a sampled
        clock as one the venue declared. Both are assertions about state, so
        neither can be satisfied by changing prose.
        """
        del was
        server_now = int(NOW_EPOCH) + PLUS_3
        broker = Mt4Broker(
            _FakeMt4Call(int(server_now - stale_sec)), now_fn=lambda: NOW_EPOCH
        )
        # The property, read off the same seam doctor reads: no bound, so a
        # sampling venue cannot produce a measurement whatever anything prints.
        clock = venue_clock_of(broker, "EURUSD", max_staleness_sec=None)
        assert not clock.measured, (
            "a venue that samples a server returned a MEASURED clock to a "
            "caller that supplied no staleness bound"
        )
        assert clock.offset_sec is None, "an unmeasured clock must carry no offset"
        assert clock.sampled, "this venue read a server, so the clock is sampled"
        # The branch taken, which is what an operator actually sees.
        rc, out = self._doctor_line(capsys, stale_sec)
        assert rc == 0
        assert "freshness NOT established" in out
        assert "declared by the venue" not in out, (
            "doctor reported a SAMPLED clock as one the venue declared: "
            + out.strip()
        )

    @pytest.mark.parametrize("stale_sec,was", SWEEP)
    def test_freshness_is_named_as_not_established(
        self, capsys: pytest.CaptureFixture, stale_sec: float, was: str
    ) -> None:
        del was
        _rc, out = self._doctor_line(capsys, stale_sec)
        assert "freshness NOT established" in out

    @pytest.mark.parametrize("stale_sec,was", SWEEP)
    def test_a_closed_market_does_not_red_the_gate(
        self, capsys: pytest.CaptureFixture, stale_sec: float, was: str
    ) -> None:
        """Exit 0 on purpose: an operator who sees a red doctor every weekend
        learns to ignore doctor. The ENGINE refuses; this gate reports."""
        del was
        rc, _out = self._doctor_line(capsys, stale_sec)
        assert rc == 0

    def _measured(self, stale_sec: float) -> VenueClock:
        server_now = int(NOW_EPOCH) + PLUS_3
        call = _FakeMt4Call(int(server_now - stale_sec))
        broker = Mt4Broker(call, now_fn=lambda: NOW_EPOCH)
        return broker.venue_clock("EURUSD", max_staleness_sec=POLL)

    def test_the_bound_refuses_a_staleness_that_is_not_a_grid_multiple(self) -> None:
        """What the measured bound CAN do, on the engine's path."""
        got = self._measured(12 * 60)
        assert not got.measured
        assert "off the" in got.detail

    def test_the_band_refuses_an_absurd_one(self) -> None:
        got = self._measured(26 * HOUR)
        assert not got.measured
        assert "civil timezone band" in got.detail

    @pytest.mark.parametrize("stale_sec", (2 * HOUR, 5 * HOUR, 11 * HOUR))
    def test_a_grid_multiple_of_staleness_is_undetectable_and_that_is_pinned(
        self, stale_sec: float
    ) -> None:
        """PINNED: `measure()` ALONE cannot see this, and the engine can.

        A staleness that is an exact multiple of 900s lands exactly on a grid
        point, so the residual is zero and the sample looks perfect; the civil
        band does not see it either, because 11h is 44 whole grid steps. This
        test asserts that limit of the TYPE rather than implying it, which is
        the form this repo uses for a known limitation (see
        `test_refusal_reasons`).

        It is not open end to end. `Engine._bar_instant` cross-checks the
        measured offset against the venue's OWN forming bar and refuses with
        `venue_clock_bar_disagrees`, because a server cannot be forming a bar
        its own clock says has not opened yet. Section 8 drives exactly these
        stalenesses through the engine and watches them refuse. The bound
        being MEASURED is the other half: after a bar advance, a stamp hours
        old cannot occur at all, which is why
        `test_the_advance_gate_that_justifies_the_bound_is_still_there`
        exists.

        If the TYPE ever gains its own discriminator, this test reds. That is
        correct: come back and assert the better behaviour here.
        """
        got = self._measured(stale_sec)
        assert got.measured, (
            "a freshness discriminator now exists; update this pin to assert it"
        )
        assert got.offset_sec == PLUS_3 - int(stale_sec), (
            "the accepted offset is the stamp's implication, which is what "
            "makes this a hole rather than a surprise"
        )

    def test_a_fresh_sample_still_reads_as_the_measurement(self) -> None:
        """The positive control. A refusal nothing can escape is not a gate."""
        server_now = int(NOW_EPOCH) + PLUS_3
        call = _FakeMt4Call(server_now)
        broker = Mt4Broker(call, now_fn=lambda: NOW_EPOCH)
        got = broker.venue_clock("EURUSD", max_staleness_sec=POLL)
        assert got.offset_sec == PLUS_3


# --- 6. doctor's other two states, and the cadence it cannot bound ---------


def test_doctor_states_a_declared_offset_as_declared(
    capsys: pytest.CaptureFixture,
) -> None:
    rc = venue_clock_check(BotConfig(), PaperBroker(utc_offset_sec=PLUS_3))
    out = capsys.readouterr().out
    assert rc == 0
    assert "server UTC+03:00 declared by the venue" in out


def test_doctor_states_a_negative_declared_offset_as_negative(
    capsys: pytest.CaptureFixture,
) -> None:
    """A western server is not an error and must not print as one."""
    venue_clock_check(BotConfig(), PaperBroker(utc_offset_sec=-5 * HOUR))
    assert "server UTC-05:00 declared" in capsys.readouterr().out


def test_doctor_goes_red_when_the_clock_cannot_be_read_at_all(
    capsys: pytest.CaptureFixture,
) -> None:
    """The distinction the exit code carries: unreadable is not unvouchable.

    Removing it is what this test exists to catch: if an unreadable clock
    stops exiting non-zero, or an unvouchable one starts, this reds.
    """
    rc = venue_clock_check(BotConfig(), _MutePaper())
    out = capsys.readouterr().out
    assert rc == 1
    assert "NOT MEASURED" in out
    assert "venue_clock" in out


def test_a_poll_cadence_too_slow_to_bound_the_clock_is_red_from_config_alone(
    capsys: pytest.CaptureFixture,
) -> None:
    """Knowable with no terminal, so it cannot become a weekend false red."""
    cfg = BotConfig()
    cfg.poll_seconds = VENUE_CLOCK_GRID_SEC // 2
    rc = venue_clock_cadence_check(cfg)
    out = capsys.readouterr().out
    assert rc == 1
    assert "too slow to bound the venue clock" in out


def test_the_shipped_poll_cadence_is_not_red(capsys: pytest.CaptureFixture) -> None:
    """The positive control for the check above, at the shipped default."""
    rc = venue_clock_cadence_check(BotConfig())
    assert rc == 0
    assert capsys.readouterr().out == ""


# --- 7. the engine's refusal, and the gate that makes its bound true -------


class _MutePaper(PaperBroker):
    """Paper, with the one method removed, so the engine meets a mute venue."""

    venue_clock = None  # type: ignore[assignment]


class _SamplingPaper(PaperBroker):
    """Paper that SAMPLES, so a caller with no bound gets no measurement.

    The paper venue declares its offset, which is correct for paper and means
    the backtest is unaffected by any of this. A venue that samples a server
    behaves differently on a caller that cannot bound staleness, and that
    difference is what this double exists to exercise.
    """

    def venue_clock(self, name: str, *, max_staleness_sec: float | None = None):
        del name
        if max_staleness_sec is None:
            return VenueClock.implied(
                int(NOW_EPOCH) + self.utc_offset_sec, NOW_EPOCH, source="sampling paper"
            )
        return VenueClock.measure(
            int(NOW_EPOCH) + self.utc_offset_sec,
            NOW_EPOCH,
            source="sampling paper",
            max_staleness_sec=max_staleness_sec,
        )


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


def _reject_reasons(engine: Engine) -> list[str]:
    return [
        str(r.get("reason", ""))
        for r in engine.journal.tail(2000)
        if r.get("event") == "reject"
    ]


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


def test_replay_cannot_measure_a_sampling_venue_and_says_so(tmp_path: Path) -> None:
    """`replay_symbol` owns no advance gate, so it has no bound to give.

    The review named this caller: it would otherwise inherit the engine's
    bound without owning the thing that justifies it.
    """
    engine, bars = _engine(tmp_path, _SamplingPaper(balance=10_000, utc_offset_sec=PLUS_3))
    engine.start()
    engine.replay_symbol("EURUSD", bars)
    rec = engine.journal.last_event("reject")
    assert rec is not None
    assert rec["reason"] == VENUE_CLOCK_UNMEASURED
    assert rec["unmeasured"] == ["freshness"]
    engine.stop()


def test_step_symbol_measures_the_bound_and_a_sampling_venue_is_measured(
    tmp_path: Path,
) -> None:
    """The positive control for the two refusals above.

    Same sampling venue, reached through `step_symbol`, which DOES own the
    advance gate: the bound is measured from its own previous poll and the
    clock is a measurement, so no clock refusal appears.
    """
    engine, bars = _engine(tmp_path, _SamplingPaper(balance=10_000, utc_offset_sec=PLUS_3))
    engine.start()
    engine.step_symbol("EURUSD")  # first poll pins the bar and returns
    engine.broker.seed_bars("EURUSD", bars + [
        bars[-1].__class__(
            time=bars[-1].time + HOUR,
            open=bars[-1].close,
            high=bars[-1].close * 1.001,
            low=bars[-1].close * 0.999,
            close=bars[-1].close * 1.0005,
            tick_volume=100,
        )
    ])
    engine.step_symbol("EURUSD")
    assert VENUE_CLOCK_UNMEASURED not in _reject_reasons(engine)
    engine.stop()


def test_the_advance_gate_that_justifies_the_bound_is_still_there(
    tmp_path: Path,
) -> None:
    """Pin the PRECONDITION, not just the arithmetic.

    The engine's bound is only true because `step_symbol` returns unless the
    last bar time advanced since the previous poll: that is what proves a tick
    arrived inside the measured interval. Nothing else in the suite fails if
    that gate is removed, which is how an inherited assumption survives. This
    asserts the gate by observation: a second poll with no new bar must not
    reach the venue clock at all.
    """
    engine, bars = _engine(tmp_path, _SamplingPaper(balance=10_000))
    engine.start()
    asked: list[float | None] = []
    inner = engine.broker.venue_clock

    def watched(name, *, max_staleness_sec=None):
        asked.append(max_staleness_sec)
        return inner(name, max_staleness_sec=max_staleness_sec)

    engine.broker.venue_clock = watched  # type: ignore[method-assign]
    engine.step_symbol("EURUSD")
    engine.step_symbol("EURUSD")
    engine.step_symbol("EURUSD")
    assert asked == [], (
        "the venue clock was sampled without a bar advance, so the measured "
        "bound no longer has the gate that justifies it"
    )
    del bars
    engine.stop()


# --- 8. the venue's own bars, against the venue's own clock ----------------


class _StaleSamplingPaper(PaperBroker):
    """A venue whose tick stamp is `stale_sec` behind its own clock.

    The shape of a market close: `TimeCurrent()` freezes while our clock keeps
    running. `our_clock` is OUR side of the pairing and the test moves it, so
    the venue's clock and the bars stay consistent with each other and only
    the TICK STAMP is stale, which is the case the engine has to survive.
    """

    def __init__(self, *, stale_sec: float, offset_sec: int, **kw: object) -> None:
        super().__init__(utc_offset_sec=offset_sec, **kw)  # type: ignore[arg-type]
        self.stale_sec = float(stale_sec)
        self.our_clock = NOW_EPOCH

    def venue_clock(self, name: str, *, max_staleness_sec: float | None = None):
        del name
        stamp = int(self.our_clock + self.utc_offset_sec - self.stale_sec)
        if max_staleness_sec is None:
            return VenueClock.implied(stamp, self.our_clock, source="stale sampling")
        return VenueClock.measure(
            stamp,
            self.our_clock,
            source="stale sampling",
            max_staleness_sec=max_staleness_sec,
        )


#: How long ago the forming bar opened, in the fixtures below. A forming bar
#: opened in the PAST by definition, which is the premise the whole check
#: rests on, so a fixture that puts it in the future is testing nothing.
BAR_OPENED_AGO = 60


def _advancing_engine(
    tmp_path: Path, broker: _StaleSamplingPaper, *, bar_opened_ago: int = BAR_OPENED_AGO
) -> tuple[Engine, list[datetime]]:
    """An engine whose clock, the venue's clock and the bars move together.

    `bar_opened_ago` is a parameter because the bar-disagreement check's blind
    spot is a function of it: the comparison is against the bar's OPEN, so the
    check sees a staleness only once that staleness exceeds the bar's age.
    Every existing caller keeps the default.
    """
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.risk.max_spread_atr_frac = 10.0
    n = 250
    last_open = int(broker.our_clock + broker.utc_offset_sec) - bar_opened_ago
    bars = generate_bars(
        n, drift=0.0006, vol=0.0002, seed=7, start_ts=last_open - (n - 1) * HOUR
    )
    broker.seed_bars("EURUSD", bars)
    clock = [datetime.fromtimestamp(broker.our_clock, tz=timezone.utc)]
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: clock[0])
    return engine, clock


def _advance_one_bar(engine: Engine, broker: _StaleSamplingPaper, clock: list) -> None:
    """One hour passes for everyone: our clock, the venue's, and the series."""
    broker.our_clock += HOUR
    clock[0] = clock[0] + timedelta(seconds=HOUR)
    series = broker.rates("EURUSD", "H1", 10_000)
    last = series[-1]
    broker.seed_bars(
        "EURUSD",
        list(series)
        + [
            last.__class__(
                time=last.time + HOUR,
                open=last.close,
                high=last.close * 1.001,
                low=last.close * 0.999,
                close=last.close * 1.0005,
                tick_volume=100,
            )
        ],
    )


@pytest.mark.parametrize("stale_sec", (2 * HOUR, 5 * HOUR, 11 * HOUR, 15 * HOUR))
def test_a_grid_multiple_stale_stamp_is_caught_by_the_venues_own_bars(
    tmp_path: Path, stale_sec: float
) -> None:
    """The hole `VenueClock.measure` cannot close, closed by the engine.

    Each of these stalenesses is a whole number of grid steps, so the sample
    lands exactly on a grid point, the residual is zero and the civil band is
    inside range: the measurement looks perfect and is wrong by hours. The
    venue's own forming bar is what contradicts it, because a server cannot be
    forming a bar that its own clock says has not opened yet.
    """
    broker = _StaleSamplingPaper(stale_sec=stale_sec, offset_sec=PLUS_3, balance=10_000)
    engine, clock = _advancing_engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")  # first poll pins the bar and returns
    _advance_one_bar(engine, broker, clock)

    # It has to survive measure() for this to be the case at all.
    measured = broker.venue_clock("EURUSD", max_staleness_sec=POLL)
    assert measured.measured, "this staleness has to pass measure() to be the case"
    assert measured.offset_sec == PLUS_3 - int(stale_sec)

    engine.step_symbol("EURUSD")
    assert "venue_clock_bar_disagrees" in _reject_reasons(engine), (
        "a stamp hours behind the venue's own bar was accepted as the clock"
    )
    assert not engine.broker.positions(magic=engine.cfg.risk.magic)
    engine.stop()


def test_a_fresh_sampling_venue_passes_the_bar_check(tmp_path: Path) -> None:
    """The positive control. The check must have a reachable green.

    This one earned its keep: the first version of the fixture above seeded a
    bar an hour AHEAD of the venue's own clock, so the refusal it observed was
    the fixture's, not the defect's, and only this control showed it.
    """
    broker = _StaleSamplingPaper(stale_sec=0, offset_sec=PLUS_3, balance=10_000)
    engine, clock = _advancing_engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")
    _advance_one_bar(engine, broker, clock)
    engine.step_symbol("EURUSD")
    reasons = _reject_reasons(engine)
    assert "venue_clock_bar_disagrees" not in reasons
    assert VENUE_CLOCK_UNMEASURED not in reasons
    engine.stop()


@pytest.mark.parametrize("bar_opened_ago,refuses", ((898, True), (899, False)))
def test_the_bar_check_is_blind_in_the_last_moments_before_a_bar_closes(
    tmp_path: Path, bar_opened_ago: int, refuses: bool
) -> None:
    """Pin the RESIDUAL, so the constant's docstring has a test under it.

    `VENUE_CLOCK_BAR_DISAGREES` used to claim it made a stale stamp
    "unreachable rather than merely unlikely". It narrows rather than closes
    (straightedge#193): the comparison is against the forming bar's OPEN, so it
    sees a staleness only once that staleness exceeds the bar's AGE. With a
    900s stale stamp the arithmetic is `refuse iff bar_opened_ago < 900 - slack`,
    which is 899, so these two parameters are the two sides of the blind spot
    and nothing between them exists to test.

    The bound is VIOLATED here, which is the only world this check is for: a
    900s staleness is a whole grid multiple, so it lands exactly on a grid
    point, `measure()` reports it as a clean measurement, and the venue's own
    bar is the only remaining contradiction.

    FAILS IF: the comparison moves to the bar's CLOSE or gains a staleness
    allowance, which would change where the blind spot sits without anything
    else noticing.
    """
    broker = _StaleSamplingPaper(
        stale_sec=VENUE_CLOCK_GRID_SEC, offset_sec=PLUS_3, balance=10_000
    )
    engine, clock = _advancing_engine(tmp_path, broker, bar_opened_ago=bar_opened_ago)
    engine.start()
    engine.step_symbol("EURUSD")
    _advance_one_bar(engine, broker, clock)

    # State the error this staleness produces, so the accepted case documents
    # the harm rather than merely the absence of a refusal.
    measured = broker.venue_clock("EURUSD", max_staleness_sec=1.0)
    assert measured.measured, "a grid-multiple staleness has to survive measure()"
    assert measured.offset_sec == PLUS_3 - VENUE_CLOCK_GRID_SEC, (
        "the snap absorbs a whole grid step, so the offset is wrong by one"
    )

    engine.step_symbol("EURUSD")
    got = "venue_clock_bar_disagrees" in _reject_reasons(engine)
    assert got is refuses, (
        f"bar opened {bar_opened_ago}s ago with a {VENUE_CLOCK_GRID_SEC}s stale "
        f"stamp: expected refusal={refuses}, got {got}. This is the documented "
        "blind spot and its boundary; if it moved, the constant's docstring is "
        "now wrong again."
    )
    engine.stop()


def test_a_stamp_past_the_civil_band_implies_no_offset_at_all(tmp_path: Path) -> None:
    """Past the band there is no timezone to imply, so none is offered.

    `implied()` snapped and reported whatever came out, so a 48h-frozen clock
    on a UTC+3 server was presented as implying `UTC-45:00`. That is not a
    timezone, and printing it lost the one reading that separated "stale" from
    "absurd" (straightedge#193).

    The band applies to the VALUE. `unmeasured` stays `{"freshness"}` on
    purpose, which the next test pins from the doctor side.

    FAILS IF: the band is applied by refusing differently instead, or not at
    all.
    """
    del tmp_path
    frozen = int(NOW_EPOCH) + PLUS_3 - 48 * HOUR
    got = VenueClock.implied(frozen, NOW_EPOCH, source="s")
    assert not got.measured
    assert got.implied_offset_sec is None, (
        "an offset outside the civil band is not an offset and must not be "
        "offered as one"
    )
    assert "outside the civil" in got.detail
    assert got.unmeasured == frozenset({"freshness"}), (
        "the measurement STATE must not change with the band: doctor keys its "
        "exit code on this set"
    )
    inside = VenueClock.implied(int(NOW_EPOCH) + PLUS_3, NOW_EPOCH, source="s")
    assert inside.implied_offset_sec == PLUS_3, "a real offset still implies itself"


def test_doctor_says_no_offset_past_the_band_and_still_exits_zero(
    capsys: pytest.CaptureFixture,
) -> None:
    """The seam the band must NOT be applied through.

    Applying it by returning `not_measured("offset_sec", ...)` would break the
    `unmeasured == {"freshness"}` equality in `venue_clock_check`, send doctor
    down the NOT MEASURED branch and exit NON-ZERO past about 15h of
    staleness. That is a red `doctor` every weekend, which straightedge#182
    decided against. A correction applied through the wrong seam re-creates
    the thing it was correcting.

    FAILS IF: exit goes non-zero on a closed market, or the line states an
    offset it cannot stand behind.
    """
    server_now = int(NOW_EPOCH) + PLUS_3
    broker = Mt4Broker(
        _FakeMt4Call(int(server_now - 48 * HOUR)), now_fn=lambda: NOW_EPOCH
    )
    rc = venue_clock_check(BotConfig(), broker)
    out = capsys.readouterr().out
    assert rc == 0, "a closed market is not a run-affecting fault"
    assert "implies NO offset" in out
    assert "freshness NOT established" in out
    assert "UTC-" not in out, "no offset may be printed where none is implied"


def test_a_sampled_clock_cannot_carry_a_zero_timestamp() -> None:
    """The invariant that replaces an accidental guard.

    `Engine._bar_instant` reads `measured_at` as an OPERAND
    (`measured_at + offset_sec`). Before straightedge#193 the same field was
    the GATE, so a sampled clock with a zero timestamp was excluded by
    accident rather than by rule. `sampled` took over the gate, so the rule
    has to be stated or the arithmetic could be fed a sentinel.

    FAILS IF: the assertion is dropped, which would make the sentinel
    reachable again with nothing saying so.
    """
    with pytest.raises(ValueError, match="sampled but carries no measured_at"):
        VenueClock(offset_sec=0, measured_at=0, source="s", sampled=True)
    # And the two legitimate shapes still build.
    VenueClock(offset_sec=0, measured_at=int(NOW_EPOCH), source="s", sampled=True)
    VenueClock.declared(PLUS_3, source="paper")


def test_a_declared_clock_is_not_sampled() -> None:
    """What keeps the bar check away from a venue that never read a clock.

    FAILS IF: `declared()` starts reporting itself as sampled, which would put
    the paper venue's own bars through a cross-check against a clock that was
    never sampled from anything.
    """
    got = VenueClock.declared(PLUS_3, source="paper")
    assert got.measured
    assert got.sampled is False
    assert VenueClock.measure(
        int(NOW_EPOCH) + PLUS_3, NOW_EPOCH, source="s", max_staleness_sec=POLL
    ).sampled is True


def test_a_stamp_one_poll_stale_still_passes_the_bar_check(tmp_path: Path) -> None:
    """The boundary that matters: ordinary lag must not refuse.

    A real tick stamp is seconds behind the server's now, and the forming bar
    opened before both. If this refused, the check would be a tripwire on
    every normal poll rather than a guard against a frozen clock.
    """
    broker = _StaleSamplingPaper(
        stale_sec=BAR_OPENED_AGO - 1, offset_sec=PLUS_3, balance=10_000
    )
    engine, clock = _advancing_engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")
    _advance_one_bar(engine, broker, clock)
    engine.step_symbol("EURUSD")
    assert "venue_clock_bar_disagrees" not in _reject_reasons(engine)
    engine.stop()


# --- 9. the error bar the type does not carry ------------------------------


class _DriftedVenue(PaperBroker):
    """A terminal whose OWN clock is wrong by `drift`, like everything it serves.

    This is the input strummer's addendum found: the drift cancels out of the
    measured difference, the grid snap takes it out of the offset, and the bar
    stamp still carries it, so the instant every gate sees moves by exactly
    `drift`. It is the only error source left in the measured path.
    """

    def __init__(self, *, drift: float, offset_sec: int = PLUS_3, **kw: object) -> None:
        super().__init__(utc_offset_sec=offset_sec, **kw)  # type: ignore[arg-type]
        self.drift = float(drift)
        self.our_clock = SESSION_CLOSE.timestamp()

    def venue_clock(self, name: str, *, max_staleness_sec: float | None = None):
        del name
        stamp = int(self.our_clock + self.utc_offset_sec + self.drift)
        if max_staleness_sec is None:
            return VenueClock.implied(stamp, self.our_clock, source="drifted")
        return VenueClock.measure(
            stamp,
            self.our_clock,
            source="drifted",
            max_staleness_sec=max_staleness_sec,
        )


#: The bar's TRUE UTC instant, placed exactly on the configured session close.
SESSION_CLOSE = datetime(2024, 1, 4, 17, 0, tzinfo=timezone.utc)
#: The widest bound the grid rule can accept, and the first one it cannot.
WIDEST_LEGAL_BOUND = VENUE_CLOCK_GRID_SEC / 2 - 1
NARROWEST_ILLEGAL_BOUND = VENUE_CLOCK_GRID_SEC / 2


def _session_engine(tmp_path: Path, broker: PaperBroker) -> tuple[Engine, list]:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = True  # the shipped 07:00-17:00 UTC window
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.risk.max_spread_atr_frac = 10.0
    n = 250
    last = int(SESSION_CLOSE.timestamp()) + broker.utc_offset_sec + int(
        getattr(broker, "drift", 0)
    )
    bars = generate_bars(
        n, drift=0.0006, vol=0.0002, seed=7, start_ts=last - (n - 1) * HOUR
    )
    broker.seed_bars("EURUSD", bars)
    engine = Engine(
        cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: SESSION_CLOSE
    )
    return engine, bars


def test_no_drift_puts_the_boundary_exactly_where_the_config_says(
    tmp_path: Path,
) -> None:
    """The reference row. A bar ON the close is outside the window."""
    broker = _DriftedVenue(drift=0, balance=10_000)
    engine, bars = _session_engine(tmp_path, broker)
    engine.start()
    engine._act("EURUSD", bars, max_staleness_sec=POLL)
    assert "outside_session" in _reject_reasons(engine)
    engine.stop()


@pytest.mark.parametrize("drift", (-90, -179, -449))
def test_a_drift_wider_than_the_bound_refuses_instead_of_sliding(
    tmp_path: Path, drift: float
) -> None:
    """On the desk's own path the softness is the MEASURED poll gap, seconds.

    Every one of these drifts was absorbed silently under the chosen 180s
    tolerance this change started with, moving a 17:00:00 instant back inside
    the window and leaving the desk armed up to three minutes past its
    configured close. Against a measured bound they refuse by name.
    """
    broker = _DriftedVenue(drift=drift, balance=10_000)
    engine, bars = _session_engine(tmp_path, broker)
    engine.start()
    engine._act("EURUSD", bars, max_staleness_sec=POLL)
    reasons = _reject_reasons(engine)
    assert VENUE_CLOCK_UNMEASURED in reasons, (
        f"a {drift}s drift was absorbed against a {POLL}s bound"
    )
    assert not engine.broker.positions(magic=engine.cfg.risk.magic)
    engine.stop()


def test_the_worst_legal_bound_absorbs_its_own_width_and_that_is_disclosed(
    tmp_path: Path,
) -> None:
    """The error bar, pinned at its worst legal value.

    A caller declaring the widest bound the grid rule accepts absorbs a drift
    of that width: the instant slides and the session gate does not fire. That
    is the softness `docs/CONTRACT.md` and `VenueClock` disclose, and this is
    the test that keeps the disclosure true. One second wider and nothing is
    measured at all, so the softness cannot grow past half a grid step.
    """
    broker = _DriftedVenue(drift=-WIDEST_LEGAL_BOUND, balance=10_000)
    engine, bars = _session_engine(tmp_path, broker)
    engine.start()
    clock = broker.venue_clock("EURUSD", max_staleness_sec=WIDEST_LEGAL_BOUND)
    assert clock.measured and clock.offset_sec == PLUS_3
    seen = clock.to_utc(bars[-1].time)
    assert seen == SESSION_CLOSE - timedelta(seconds=WIDEST_LEGAL_BOUND), (
        "the absorbed error is exactly the terminal's drift"
    )
    engine._act("EURUSD", bars, max_staleness_sec=WIDEST_LEGAL_BOUND)
    assert "outside_session" not in _reject_reasons(engine), (
        "this row is the disclosure: inside the bound, the window is soft"
    )
    engine.stop()


def test_one_second_past_the_grid_rule_measures_nothing_at_all(
    tmp_path: Path,
) -> None:
    """The cap on that softness, so it cannot be widened by a caller."""
    broker = _DriftedVenue(drift=0, balance=10_000)
    engine, bars = _session_engine(tmp_path, broker)
    engine.start()
    got = broker.venue_clock("EURUSD", max_staleness_sec=NARROWEST_ILLEGAL_BOUND)
    assert not got.measured
    assert "too wide" in got.detail
    engine._act("EURUSD", bars, max_staleness_sec=NARROWEST_ILLEGAL_BOUND)
    assert VENUE_CLOCK_UNMEASURED in _reject_reasons(engine)
    engine.stop()
