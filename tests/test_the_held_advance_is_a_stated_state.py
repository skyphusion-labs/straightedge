"""straightedge#212: the held advance gate says so, once per symbol per move.

`step_symbol` returns on `if last_t <= prev` and that gate is #172's
dump-trade guard. It is correct, it is load-bearing, and nothing here changes
it. What it did not do was SAY anything: a westward venue clock move carries
bar stamps back with it, the gate then holds for about the size of the move,
and an hour of that was byte-for-byte indistinguishable in the record from an
hour of quiet market. No entry, no refusal, no halt, nothing.

THE CARDINALITY IS THE DESIGN, not the string. At the default
`engine.poll_seconds` of 15 the gate returns on an unchanged stamp nearly
every poll, so about 240 an hour for one symbol and about 960 across the four
in `[symbols]`; `config.example.toml` ships 1 second, which is fifteen times
that. A row per suppressed poll would bury the signal it exists to produce.
So the row is written ONCE per symbol per backwards move, and this file
asserts that against a world where the fixture polls many times while held,
plus a per-poll mutant of the same code that must go RED on the same
assertion. An assertion whose failing world has not been observed is
decoration.

WHAT THE ROW MEASURES IS THE STAMPS, NOT THE CLOCK, and the reason is
asserted here rather than argued in prose: `test_the_recorded_clock_move_is_
useless_while_the_gate_holds` drives a westward move and shows the
`venue_clock` transition row arriving only when the gate OPENS, because the
clock is read in `_bar_instant`, which is reached through `_act`, which this
gate returns before. A row conditioned on that record could not fire while
the hold was in progress.

THE FIXTURE MOVES OUR CLOCK, THE VENUE'S CLOCK AND THE BAR SERIES TOGETHER.
The #209 author found out the hard way that a fixture that moves one and not
the others is testing a world the venue cannot produce. A westward move here
is the server's offset dropping AND the terminal re-stamping its history onto
the new clock, at one instant, which is what a reconnect onto a server on a
different offset looks like.

LIMIT, stated because a test file that overstates its subject is worse than
none. A venue that STALLS the top of its series across the roll (the last bar
keeps its old stamp, so `last_t == prev`) is not covered and cannot be: that
state is identical to a quiet market in everything this branch can see, and
the only thing that could tell them apart is the venue clock, which is not
read on this path. See the `_note_held_advance` docstring.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import Engine, _format_event
from straightedge.models import VenueClock
from straightedge.synthetic import generate_bars

HOUR = 3600
PLUS_3 = 3 * HOUR
NOW = datetime(2024, 1, 4, 12, 0, tzinfo=timezone.utc)
POLL = 15.0
#: How long before the poll the last bar opened, so the series is never
#: stamped in the venue's own future.
BAR_OPENED_AGO = 60
SYMBOLS = ("EURUSD", "GBPUSD")
EVENT = "advance_held"


class _MovingVenue(PaperBroker):
    """A venue whose offset the test can move, as a server's really does.

    It SAMPLES, like MT4 and MT5, so a caller with no staleness bound gets an
    implication rather than a measurement. Same shape as the venue in
    `tests/test_venue_clock_journal.py`, deliberately, because that is the
    fixture the #209 measurement behind this issue was taken on.
    """

    def __init__(self, *, offset_sec: int = PLUS_3, **kw: object) -> None:
        super().__init__(utc_offset_sec=offset_sec, **kw)  # type: ignore[arg-type]
        self.our_clock = NOW.timestamp()

    def venue_clock(self, name: str, *, max_staleness_sec: float | None = None):
        del name
        stamp = int(self.our_clock + self.utc_offset_sec)
        if max_staleness_sec is None:
            return VenueClock.implied(stamp, self.our_clock, source="moving venue")
        return VenueClock.measure(
            stamp,
            self.our_clock,
            source="moving venue",
            max_staleness_sec=max_staleness_sec,
        )


def _seed(broker: _MovingVenue, symbol: str, *, seed: int) -> None:
    n = 250
    last_open = int(broker.our_clock + broker.utc_offset_sec) - BAR_OPENED_AGO
    broker.seed_bars(
        symbol,
        generate_bars(
            n, drift=0.0006, vol=0.0002, seed=seed, start_ts=last_open - (n - 1) * HOUR
        ),
    )


def _engine(
    tmp_path: Path, broker: _MovingVenue, *, cls: type[Engine] = Engine
) -> tuple[Engine, list]:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.risk.max_spread_atr_frac = 10.0
    cfg.symbols = list(SYMBOLS)
    # The acting path is not the subject here, and an entry would put orders in
    # the way of the row count.
    cfg.strategy.auto = False
    for i, name in enumerate(SYMBOLS):
        _seed(broker, name, seed=7 + i)
    clock = [NOW]
    engine = cls(cfg, broker, halt_dir=str(tmp_path), now_fn=lambda: clock[0])
    return engine, clock


def _poll(engine: Engine, broker: _MovingVenue, clock: list, *, times: int = 1) -> None:
    """Poll every symbol, `times` times, with no bar forming.

    Our clock and the venue's advance together by the poll interval, which is
    what passing time looks like to both of them. The series does not move,
    because a bar forming is what `_advance_one_bar` is for.
    """
    for _ in range(times):
        broker.our_clock += POLL
        clock[0] = clock[0] + timedelta(seconds=POLL)
        for name in SYMBOLS:
            engine.step_symbol(name)


def _advance_one_bar(engine: Engine, broker: _MovingVenue, clock: list) -> None:
    """An hour passes for our clock, the venue's, and both series together.

    The new bar sits on the timeframe GRID, one hour after the last one, which
    is what a venue does and what keeps a deficit an exact number of hours
    however many polls have gone by. It is asserted to be no later than the
    venue could have stamped it, because a bar the venue could not have
    produced is the fixture mistake the #209 author hit first.
    """
    broker.our_clock += HOUR
    clock[0] = clock[0] + timedelta(seconds=HOUR)
    for name in SYMBOLS:
        series = broker.rates(name, "H1", 10_000)
        last = series[-1]
        stamp = last.time + HOUR
        assert stamp <= int(broker.our_clock + broker.utc_offset_sec), (
            "the fixture stamped a bar in the venue own future"
        )
        broker.seed_bars(
            name,
            list(series)
            + [replace(last, time=stamp, open=last.close, close=last.close * 1.0005)],
        )
    for name in SYMBOLS:
        engine.step_symbol(name)


def _move_the_server_clock(broker: _MovingVenue, *, by: int) -> None:
    """The server's offset moves, and its history is re-stamped with it.

    ONE INSTANT, no time passing: this is the reconnect-onto-a-different-server
    shape, and the one the #209 fixture produces. Our clock does not move,
    because nothing about our host changed.
    """
    broker.utc_offset_sec += by
    for name in SYMBOLS:
        series = broker.rates(name, "H1", 10_000)
        broker.seed_bars(name, [replace(b, time=b.time + by) for b in series])


def _rows(engine: Engine, event: str = EVENT) -> list[dict]:
    return [r for r in engine.journal.tail(5000) if r.get("event") == event]


def _held(engine: Engine, symbol: str) -> list[dict]:
    return [r for r in _rows(engine) if r.get("symbol") == symbol]


def _pin(engine: Engine, broker: _MovingVenue, clock: list) -> None:
    """Get both symbols past the first-poll pin and acting normally."""
    engine.start()
    for name in SYMBOLS:
        engine.step_symbol(name)
    _advance_one_bar(engine, broker, clock)
    assert not _rows(engine), "a normally advancing desk wrote a held row"


# --- 1. the westward move, which is the direction that is delayed ----------


def test_a_westward_move_states_the_hold_once_per_symbol(tmp_path: Path) -> None:
    """One row per symbol, carrying how far back the stamps went.

    Twelve polls while held, two symbols, and the count that must come back is
    two. `behind_sec` is the move, which is also about how much longer the
    gate holds, so the reader of one row can tell both.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    _pin(engine, broker, clock)
    highs = {name: engine.last_bar_time[name] for name in SYMBOLS}

    _move_the_server_clock(broker, by=-HOUR)
    _poll(engine, broker, clock, times=12)

    for name in SYMBOLS:
        rows = _held(engine, name)
        assert len(rows) == 1, f"{name} wrote {len(rows)} rows for one move: {rows}"
        row = rows[0]
        assert row["behind_sec"] == HOUR, row
        assert row["previous_bar_time"] == highs[name], row
        assert row["previous_bar_time"] - row["bar_time"] == HOUR, row
        # The gate is STILL SHUT, which is the whole point of the row.
        assert engine.last_bar_time[name] == highs[name], (
            "the gate let the stamp through: " + repr(engine.last_bar_time[name])
        )
    engine.stop()


def test_the_hold_ends_and_states_nothing_more(tmp_path: Path) -> None:
    """The climb back is silent, and the gate opens on its own.

    Two bars, which is the same measurement `test_venue_clock_journal.py` makes
    for a one-hour westward roll. Nothing is written while the stamps climb:
    inside one held window the deficit only shrinks, and a shrinking deficit is
    not news.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    _pin(engine, broker, clock)
    highs = {name: engine.last_bar_time[name] for name in SYMBOLS}

    _move_the_server_clock(broker, by=-HOUR)
    _poll(engine, broker, clock)
    assert len(_rows(engine)) == len(SYMBOLS)

    opened_after = 0
    for step in (1, 2, 3):
        _advance_one_bar(engine, broker, clock)
        if all(engine.last_bar_time[n] > highs[n] for n in SYMBOLS):
            opened_after = step
            break
    assert opened_after == 2, f"the gate opened after {opened_after} bars, not 2"
    assert len(_rows(engine)) == len(SYMBOLS), (
        "the climb back wrote more rows: " + repr(_rows(engine))
    )

    # And a SECOND move, after the first one is over, is a new move with a new
    # row. The bound is per move, not one per process.
    _move_the_server_clock(broker, by=-HOUR)
    _poll(engine, broker, clock, times=4)
    assert len(_rows(engine)) == 2 * len(SYMBOLS), _rows(engine)
    engine.stop()


def test_a_deeper_move_while_held_earns_its_own_row(tmp_path: Path) -> None:
    """A second move DURING the hold changes how long it will last.

    So it gets a row, and the number on it is the new total. A deficit that is
    equal or smaller does not, which is the same rule stated from the other
    side: that is what keeps this off the per-poll path when stamps climb.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    _pin(engine, broker, clock)

    _move_the_server_clock(broker, by=-2 * HOUR)
    _poll(engine, broker, clock, times=3)
    assert [r["behind_sec"] for r in _held(engine, SYMBOLS[0])] == [2 * HOUR]

    # One bar forms, so the stamps climb an hour: the deficit SHRINKS to one
    # hour and nothing is written.
    _advance_one_bar(engine, broker, clock)
    _poll(engine, broker, clock, times=3)
    assert [r["behind_sec"] for r in _held(engine, SYMBOLS[0])] == [2 * HOUR], (
        "a shrinking deficit wrote a row: " + repr(_held(engine, SYMBOLS[0]))
    )

    # Now the server moves back AGAIN, deeper than anything stated.
    _move_the_server_clock(broker, by=-2 * HOUR)
    _poll(engine, broker, clock, times=3)
    assert [r["behind_sec"] for r in _held(engine, SYMBOLS[0])] == [2 * HOUR, 3 * HOUR]
    engine.stop()


# --- 2. the other direction, and the quiet market -------------------------


def test_an_eastward_move_states_nothing(tmp_path: Path) -> None:
    """The direction that is not delayed, which is why both are tested.

    An eastward move pushes stamps forward, so the gate opens on the next poll
    and there is no hold to state. A suite that tested only the delayed
    direction would make the delay look intrinsic to a clock change.
    """
    broker = _MovingVenue(balance=10_000, offset_sec=2 * HOUR)
    engine, clock = _engine(tmp_path, broker)
    _pin(engine, broker, clock)
    highs = {name: engine.last_bar_time[name] for name in SYMBOLS}

    _move_the_server_clock(broker, by=+HOUR)
    _poll(engine, broker, clock, times=12)

    assert _rows(engine) == [], "an eastward move stated a hold: " + repr(_rows(engine))
    for name in SYMBOLS:
        assert engine.last_bar_time[name] == highs[name] + HOUR, (
            "the gate did not open on an eastward move for " + name
        )
    engine.stop()


def test_a_quiet_market_states_nothing(tmp_path: Path) -> None:
    """The control that separates this row from a per-poll log.

    No clock move at all, twenty polls per symbol, and the stamp unchanged on
    every one. This is the state an hour of westward hold used to be
    indistinguishable from, and it must stay silent or the row means nothing.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    _pin(engine, broker, clock)

    _poll(engine, broker, clock, times=20)

    assert _rows(engine) == [], "a quiet market wrote rows: " + repr(_rows(engine))
    engine.stop()


# --- 3. the assertion above can go red ------------------------------------


class _PerPollEngine(Engine):
    """The mutant: the same row, written on every suppressed poll.

    This is the obvious implementation and the wrong one, and it is here
    because `test_a_westward_move_states_the_hold_once_per_symbol` is worth
    nothing unless a world exists in which it fails. The fixture polls twelve
    times while held, so this must produce twelve rows per symbol and the
    once-per-symbol assertion must go red on exactly the drive that passes
    against the shipped code.
    """

    def _note_held_advance(self, symbol: str, last_t: int, prev: int) -> None:
        if last_t >= prev:
            return
        self._emit(
            EVENT,
            symbol=symbol,
            behind_sec=int(prev - last_t),
            bar_time=last_t,
            previous_bar_time=prev,
        )


def test_the_once_per_move_assertion_goes_red_on_a_per_poll_emitter(
    tmp_path: Path,
) -> None:
    """The control of the control, driven exactly like the real test."""
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker, cls=_PerPollEngine)
    _pin(engine, broker, clock)

    _move_the_server_clock(broker, by=-HOUR)
    _poll(engine, broker, clock, times=12)

    for name in SYMBOLS:
        rows = _held(engine, name)
        assert len(rows) == 12, (
            f"the mutant wrote {len(rows)} rows for {name}, so this control is "
            "not driving the suppressed branch twelve times and the "
            "once-per-move assertion elsewhere in this file is decorative"
        )
    engine.stop()


# --- 4. the design call, asserted rather than argued ----------------------


def test_the_recorded_clock_move_arrives_only_when_the_gate_opens(
    tmp_path: Path,
) -> None:
    """Why the row measures the STAMPS and not `_record_venue_clock`.

    The venue clock is read in `_bar_instant`, which is reached through
    `_act`, which the advance gate returns before. So while a westward hold is
    in progress the desk has recorded NO transition, and a row conditioned on
    one could not fire until the hold was over. The transition lands on the
    poll the gate opens on, which this drives and asserts.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    _pin(engine, broker, clock)

    def transitions() -> list[dict]:
        return [r for r in _rows(engine, "venue_clock") if "previous_offset_sec" in r]

    assert transitions() == []
    _move_the_server_clock(broker, by=-HOUR)
    _poll(engine, broker, clock, times=12)
    assert transitions() == [], (
        "the clock move was recorded while the gate held, which would change "
        "the design of _note_held_advance: " + repr(transitions())
    )
    assert len(_rows(engine)) == len(SYMBOLS), "but the hold WAS stated"

    _advance_one_bar(engine, broker, clock)
    _advance_one_bar(engine, broker, clock)
    moved = transitions()
    assert moved, "the gate opened and the clock move was never recorded"
    assert moved[0]["previous_offset_sec"] - moved[0]["offset_sec"] == HOUR, moved[0]
    engine.stop()


# --- 5. no behaviour change ------------------------------------------------


def test_the_gate_still_returns_before_the_acting_path(tmp_path: Path) -> None:
    """The row is an observation, and the gate it observes is unchanged.

    `_act` is the only way anything reaches the venue from this leg, so it is
    replaced with a tripwire for the duration of the hold. Twelve polls per
    symbol, rows written, and the acting path not entered once.
    """
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    _pin(engine, broker, clock)
    before = dict(engine.last_bar_time)
    opened = len(broker.positions())

    def _tripwire(*a: object, **kw: object) -> None:
        raise AssertionError("the advance gate let a suppressed poll through to _act")

    engine._act = _tripwire  # type: ignore[method-assign]
    _move_the_server_clock(broker, by=-HOUR)
    _poll(engine, broker, clock, times=12)

    assert len(_rows(engine)) == len(SYMBOLS)
    assert dict(engine.last_bar_time) == before, "the gate advanced its own high"
    assert len(broker.positions()) == opened, "a suppressed poll reached the venue"
    engine.stop()


def test_the_row_is_journal_only_and_never_pings_the_chat(tmp_path: Path) -> None:
    """A desk behaving as designed does not message the operator about it.

    The hold is a state to read when you are already wondering why nothing has
    opened, which is what `docs/RUNBOOK.md` answers.
    """
    del tmp_path
    assert _format_event(EVENT, {"symbol": "EURUSD", "behind_sec": HOUR}) == ""


@pytest.mark.parametrize("field", ["symbol", "behind_sec", "bar_time", "previous_bar_time"])
def test_the_row_declares_its_own_fields(tmp_path: Path, field: str) -> None:
    """Every field a reader of `docs/RUNBOOK.md` is told to expect is present."""
    broker = _MovingVenue(balance=10_000)
    engine, clock = _engine(tmp_path, broker)
    _pin(engine, broker, clock)
    _move_the_server_clock(broker, by=-HOUR)
    _poll(engine, broker, clock)
    rows = _rows(engine)
    assert rows, "nothing was written, so no field can be checked"
    assert field in rows[0], rows[0]
    engine.stop()
