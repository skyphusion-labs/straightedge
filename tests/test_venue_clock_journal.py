"""straightedge#186: the measured venue offset reaches the durable record.

#172 journals the FAILURE and nothing on success. A refusal writes
`venue_clock_unmeasured`, and later `venue_clock_bar_disagrees`, so the record
can say the desk did not know what time it was and can never say it thought it
was UTC+3. The offset was measured, used to convert every bar, and discarded;
the only way to learn it was `doctor --connect` at the moment you asked, which
answers for NOW and says nothing about the instant an order was placed. #37's
evidence package has to answer that from the journal alone.

So the clock is recorded: once at `start()`, and again whenever it CHANGES.

THE CHANGE DETECTOR IS THE ONLY PART WITH REAL DESIGN IN IT, and the two cases
it must catch are the two that happen with nobody editing anything:

* a server-side DST roll, +10800 to +7200;
* a reconnect that lands on a different server, +10800 to 0.

Both are driven here, and so is the case that makes the detector worth having
rather than a per-poll log: an offset that has NOT moved writes nothing.

SCOPE, held to what the issue excluded: no translation layer, `ts` stays our
own clock on every row, no config knob, and no gate change. The last one is
asserted rather than asserted-in-prose: the refusal behaviour is measured with
the recording in place and compared against the reasons the gate names.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import VENUE_CLOCK_UNMEASURED, Engine
from straightedge.models import VenueClock
from straightedge.synthetic import generate_bars

HOUR = 3600
PLUS_3 = 3 * HOUR
#: Southern-hemisphere DST on the server: one hour back, nothing else changes.
PLUS_2 = 2 * HOUR
NOW = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
POLL = 15.0
BAR_OPENED_AGO = 60


class _MovingVenue(PaperBroker):
    """A venue whose offset the test can move, as a server's really does.

    It SAMPLES, like MT4 and MT5, so a caller with no staleness bound gets an
    implication rather than a measurement; that is what `start()` sees.
    """

    def __init__(self, *, offset_sec: int = PLUS_3, **kw: object) -> None:
        super().__init__(utc_offset_sec=offset_sec, **kw)  # type: ignore[arg-type]
        self.our_clock = NOW.timestamp()
        self.fail_with: str = ""

    def venue_clock(self, name: str, *, max_staleness_sec: float | None = None):
        del name
        if self.fail_with:
            raise RuntimeError(self.fail_with)
        stamp = int(self.our_clock + self.utc_offset_sec)
        if max_staleness_sec is None:
            return VenueClock.implied(stamp, self.our_clock, source="moving venue")
        return VenueClock.measure(
            stamp,
            self.our_clock,
            source="moving venue",
            max_staleness_sec=max_staleness_sec,
        )


def _engine(tmp_path: Path, broker: _MovingVenue) -> tuple[Engine, list]:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.risk.max_spread_atr_frac = 10.0
    n = 250
    last_open = int(broker.our_clock + broker.utc_offset_sec) - BAR_OPENED_AGO
    broker.seed_bars(
        "EURUSD",
        generate_bars(
            n, drift=0.0006, vol=0.0002, seed=7, start_ts=last_open - (n - 1) * HOUR
        ),
    )
    clock = [NOW]
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: clock[0])
    return engine, clock


def _advance_one_bar(broker: _MovingVenue, clock: list) -> None:
    """An hour passes for our clock, the venue's, and the series together."""
    broker.our_clock += HOUR
    clock[0] = clock[0] + timedelta(seconds=HOUR)
    series = broker.rates("EURUSD", "H1", 10_000)
    last = series[-1]
    broker.seed_bars(
        "EURUSD",
        list(series)
        + [
            last.__class__(
                time=int(broker.our_clock + broker.utc_offset_sec) - BAR_OPENED_AGO,
                open=last.close,
                high=last.close * 1.001,
                low=last.close * 0.999,
                close=last.close * 1.0005,
                tick_volume=100,
            )
        ],
    )


def _advance_until_the_series_moves(
    engine: Engine, broker: _MovingVenue, clock: list, *, limit: int = 8
) -> int:
    """Advance until `step_symbol` can actually act, and say how long it took.

    A CONSEQUENCE OF THE ADVANCE GATE, not a fixture convenience, and it is
    why the backwards cases below need this and the forward ones do not. When a
    venue's offset moves BACKWARDS its bar stamps move back with it, so
    `step_symbol`'s `if last_t <= prev: return` holds until stamps climb past
    their previous high: a three-hour reconnect westwards is invisible to the
    auto leg for about three hours, whatever this record does. The clock row is
    therefore written when the desk next ACTS, not when the server moved, and
    pretending otherwise by seeding a stamp that could not occur would be
    testing a state the venue cannot produce.
    """
    before = len(_rows(engine))
    for step in range(1, limit + 1):
        _advance_one_bar(broker, clock)
        engine.step_symbol("EURUSD")
        if len(_rows(engine)) > before:
            return step
    raise AssertionError(
        f"the series did not advance within {limit} hours; the gate never opened"
    )


def _rows(engine: Engine) -> list[dict]:
    return [r for r in engine.journal.tail(5000) if r.get("event") == "venue_clock"]


def _offsets(engine: Engine) -> list:
    return [r.get("offset_sec") for r in _rows(engine)]


# --- 1. the opening record -------------------------------------------------


def test_start_records_what_the_venue_could_say_at_the_boundary(
    tmp_path: Path,
) -> None:
    """One row at `start()`, and it is honest about what it is.

    `start()` has no previous poll, so it cannot bound the sample, and a
    SAMPLING venue therefore answers with an implication. The row carries that
    implication and names `freshness` as unmeasured, which is the same thing
    `doctor --connect` prints and the opposite of claiming an offset.
    """
    broker = _MovingVenue(balance=10_000)
    engine, _clock = _engine(tmp_path, broker)
    engine.start()
    rows = _rows(engine)
    assert len(rows) == 1, f"expected one opening row, got {rows}"
    assert rows[0]["unmeasured"] == ["freshness"]
    assert rows[0]["implied_offset_sec"] == PLUS_3
    assert "offset_sec" not in rows[0], "an implication is not an offset"
    assert rows[0]["first_reading"] is True
    assert rows[0]["venue"] == "moving venue"
    engine.stop()


def test_a_declared_venue_records_its_offset_at_start(tmp_path: Path) -> None:
    """Paper declares rather than samples, so the opening row IS a measurement."""
    broker = PaperBroker(balance=10_000, utc_offset_sec=PLUS_3)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, seed=3))
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.session.enabled = False
    engine = Engine(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: NOW)
    engine.start()
    rows = [r for r in engine.journal.tail(5000) if r.get("event") == "venue_clock"]
    assert len(rows) == 1
    assert rows[0]["offset_sec"] == PLUS_3
    assert rows[0]["sampled"] is False
    assert "unmeasured" not in rows[0]
    engine.stop()


def test_a_venue_that_cannot_be_read_at_start_does_not_stop_the_desk(
    tmp_path: Path,
) -> None:
    """An observation must not add a way for the desk to fail to start.

    The MT4 adapter raises when the Expert answers an error, and a tick for a
    symbol still absent from Market Watch is exactly the reading that fails:
    cold symbols are the normal startup state, which is why `warm_history`
    exists at all. So the reading is recorded as unmeasured with the error as
    its detail, and `start()` completes.
    """
    broker = _MovingVenue(balance=10_000)
    engine, _clock = _engine(tmp_path, broker)
    broker.fail_with = "mt4: tick EURUSD failed: no such symbol"
    engine.start()
    rows = _rows(engine)
    assert len(rows) == 1
    assert rows[0]["unmeasured"] == ["server_time"]
    assert "no such symbol" in rows[0]["detail"]
    assert engine.history is not None, "start() did not finish"
    engine.stop()


# --- 2. the change detector, on the two cases that happen by themselves ----


def test_a_server_side_dst_roll_is_recorded_as_a_transition(
    tmp_path: Path,
) -> None:
    """+10800 to +7200, with nobody editing anything. One extra row.

    The row states both numbers, so the transition is legible without diffing
    two rows: a DST roll is only readable as a pair.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")  # first poll pins the bar
    _advance_one_bar(broker, clock)
    engine.step_symbol("EURUSD")
    assert _offsets(engine)[-1] == PLUS_3, "the measured offset was not recorded"

    broker.utc_offset_sec = PLUS_2  # the server rolled its own clock, westwards
    waited = _advance_until_the_series_moves(engine, broker, clock)
    rows = _rows(engine)
    assert rows[-1]["offset_sec"] == PLUS_2, f"the roll was not recorded: {rows}"
    assert rows[-1]["previous_offset_sec"] == PLUS_3, (
        "the row does not say what it moved from: " + repr(rows[-1])
    )
    # Named rather than hidden: a westward roll holds the advance gate shut for
    # about as long as the roll, so the row lands when the desk next acts.
    assert waited == 2, f"the gate opened after {waited} hours, not 2"
    engine.stop()


def test_an_eastward_dst_roll_is_recorded_on_the_very_next_bar(
    tmp_path: Path,
) -> None:
    """The other direction, where the gate does not hold at all.

    +7200 to +10800 pushes bar stamps forward, so the series advances
    immediately and the row lands on the next poll. Both directions are tested
    because only one of them is delayed, and a suite that tested only the
    delayed one would make the delay look intrinsic.
    """
    broker = _MovingVenue(balance=10_000, offset_sec=PLUS_2)
    engine, clock = _engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")
    _advance_one_bar(broker, clock)
    engine.step_symbol("EURUSD")
    assert _offsets(engine)[-1] == PLUS_2

    broker.utc_offset_sec = PLUS_3
    _advance_one_bar(broker, clock)
    engine.step_symbol("EURUSD")
    rows = _rows(engine)
    assert rows[-1]["offset_sec"] == PLUS_3
    assert rows[-1]["previous_offset_sec"] == PLUS_2
    engine.stop()


def test_a_reconnect_to_a_different_server_is_recorded(tmp_path: Path) -> None:
    """+10800 to 0. The other case nobody edits, and a bigger jump."""
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")
    _advance_one_bar(broker, clock)
    engine.step_symbol("EURUSD")

    broker.utc_offset_sec = 0  # a different trade server, stamping UTC
    waited = _advance_until_the_series_moves(engine, broker, clock)
    rows = _rows(engine)
    assert rows[-1]["offset_sec"] == 0
    assert rows[-1]["previous_offset_sec"] == PLUS_3
    # Three hours westwards, so three hours of held gate. The record states
    # what the desk measured when it next acted, which is the only instant it
    # has evidence for.
    assert waited == 4, f"the gate opened after {waited} hours, not 4"
    engine.stop()


def test_an_offset_that_has_not_moved_writes_nothing(tmp_path: Path) -> None:
    """The control that makes the detector worth having.

    Without it this would be a per-poll log, which is the shape #119 exists to
    forbid: a record whose volume scales with ticks rather than with events.
    Five polls at one offset are one row.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")
    for _ in range(5):
        _advance_one_bar(broker, clock)
        engine.step_symbol("EURUSD")
    measured = [r for r in _rows(engine) if r.get("offset_sec") is not None]
    assert len(measured) == 1, (
        f"five polls at one offset wrote {len(measured)} rows: {_offsets(engine)}"
    )
    engine.stop()


def test_losing_and_regaining_the_clock_is_two_transitions(tmp_path: Path) -> None:
    """Measured to unmeasured and back, each recorded once.

    The per-bar refusal already fires every tick while the clock is lost; this
    row is the STATE CHANGE, so the two must not be confused and the state
    change must not repeat while the state holds.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")
    _advance_one_bar(broker, clock)
    engine.step_symbol("EURUSD")
    assert _offsets(engine)[-1] == PLUS_3

    # The venue stops being able to say: two polls in that state.
    broker.fail_with = ""
    broker.utc_offset_sec = PLUS_3
    original = broker.venue_clock

    def unmeasured(name, *, max_staleness_sec=None):
        del name, max_staleness_sec
        return VenueClock.not_measured(
            "server_time", source="moving venue", detail="the venue went quiet"
        )

    broker.venue_clock = unmeasured  # type: ignore[method-assign]
    for _ in range(2):
        _advance_one_bar(broker, clock)
        engine.step_symbol("EURUSD")
    lost = [r for r in _rows(engine) if r.get("unmeasured") == ["server_time"]]
    assert len(lost) == 1, f"the loss was recorded {len(lost)} times: {_rows(engine)}"

    broker.venue_clock = original  # type: ignore[method-assign]
    _advance_one_bar(broker, clock)
    engine.step_symbol("EURUSD")
    assert _rows(engine)[-1]["offset_sec"] == PLUS_3, "the recovery was not recorded"
    assert _rows(engine)[-1]["previous_unmeasured"] == ["server_time"]
    engine.stop()


# --- 3. the exclusions the issue wrote in ---------------------------------


def test_the_recording_changes_no_gate(tmp_path: Path) -> None:
    """Scope: no gate change. Measured, not asserted in prose.

    The clock is recorded from inside `_bar_instant`, which is the seam the
    refusals come out of, so the risk is real rather than theoretical. A lost
    clock must still refuse by name, and a measured one must still not.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")
    _advance_one_bar(broker, clock)
    engine.step_symbol("EURUSD")
    reasons = [
        r.get("reason")
        for r in engine.journal.tail(5000)
        if r.get("event") == "reject"
    ]
    assert VENUE_CLOCK_UNMEASURED not in reasons, "a measured clock refused"

    def unmeasured(name, *, max_staleness_sec=None):
        del name, max_staleness_sec
        return VenueClock.not_measured("server_time", source="moving venue")

    broker.venue_clock = unmeasured  # type: ignore[method-assign]
    _advance_one_bar(broker, clock)
    engine.step_symbol("EURUSD")
    reasons = [
        r.get("reason")
        for r in engine.journal.tail(5000)
        if r.get("event") == "reject"
    ]
    assert VENUE_CLOCK_UNMEASURED in reasons, "a lost clock stopped refusing"
    engine.stop()


def test_the_row_carries_our_own_clock_and_no_translation(tmp_path: Path) -> None:
    """Scope: `ts` stays ours, and nothing here translates a timestamp.

    The row records what the OFFSET was. It does not restate any other row's
    instant in venue time, which is the translation layer the issue excluded:
    the two clocks become reconcilable because the offset is on the record, not
    because the journal starts speaking both.
    """
    broker = _MovingVenue(balance=10_000)
    engine, _clock = _engine(tmp_path, broker)
    engine.start()
    row = _rows(engine)[0]
    assert "ts" in row
    assert row["ts"].endswith("+00:00") or row["ts"].endswith("Z")
    assert "venue_time" not in row
    assert "server_time" not in row, "no row may restate an instant in venue time"
    engine.stop()


def test_the_journal_row_is_bounded_and_flat(tmp_path: Path) -> None:
    """#119's rule: a row carries its own facts, never a rendering of others."""
    import json

    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    engine.start()
    engine.step_symbol("EURUSD")
    _advance_one_bar(broker, clock)
    engine.step_symbol("EURUSD")
    for row in _rows(engine):
        blob = json.dumps(row, sort_keys=True)
        assert len(blob) <= 512, f"a clock row is carrying more than its facts: {blob}"
        for value in row.values():
            assert not isinstance(value, dict), "no nested payloads"
            assert "\n" not in str(value), "no value may be multi-line"
    engine.stop()


def test_the_clock_row_is_journal_only_and_never_pings_the_chat(
    tmp_path: Path,
) -> None:
    """An offset that has not moved is not news, and neither is one that has.

    The loud clock events already exist: a refusal journals
    `venue_clock_unmeasured` and `doctor --connect` prints the reading on
    demand. This row is for the record an operator reads at 03:00.
    """
    from straightedge.engine import _format_event

    assert _format_event("venue_clock", {"offset_sec": PLUS_3}) == ""
    del tmp_path
