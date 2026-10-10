"""straightedge#217: the breach record was lost permanently when the write failed.

`_hb_over_warned = True` was set BEFORE `self.journal.write("tick_gap_breach")`,
so a write that raised lost the row for the life of the process: `Journal.write`
has no exception handling, `__main__` catches `Exception` and keeps looping, and
the loop's own `loop_error` write is swallowed by `except Exception: pass`. The
only trace was a stderr line on a box where stderr goes to a file nobody reads,
which is the sentence #190's own comment uses to justify journaling in the first
place.

**A flag set before a durable write loses the record precisely when writing is
what failed.** #190 exists because a high-water mark did not survive a restart;
this is the same class of loss through a different door, inside the function
#190 had just hardened.

## Why the one-line fix is wrong, and what is here instead

Moving the flag after the write retries on every tick for as long as the journal
stays broken, which is an unbounded retry inside the latency path this record
exists to explain. So:

- **Two surfaces, two flags.** The stderr warning is not durable, so its flag is
  still set before the print. The journal row is durable, so its flag is set
  ONLY after `journal.write` returns.
- **Bounded**, at `_HB_BREACH_WRITE_ATTEMPTS` ticks.
- **Captured at detection**, so a retried write records the breach that was
  detected rather than a larger maximum accumulated while the journal was
  unavailable.
- **Giving up is itself recorded**, in `breach_rows_lost` in the HEARTBEAT,
  because a bounded retry that goes quiet is the same defect one level out, and
  the channel designed to carry this is the one that failed. The heartbeat is a
  different file with a live reader.

## What these tests drive

The real arithmetic, never a poked field: nothing here assigns `_hb_gap_max_s`.
The gap is produced by moving the monotonic clock between two `step_all` calls
and letting `_write_heartbeat` measure it, which is #190's seam and the one the
live 608.5 came through.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from straightedge import watchdog
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig, SessionConfig, TelegramConfig
from straightedge.engine import _HB_BREACH_WRITE_ATTEMPTS, Engine
from straightedge.journal import Journal
from straightedge.synthetic import generate_bars

LIVE_GAP_S = 608.5
ROOT = Path(__file__).resolve().parents[1]


def _cfg(tmp_path: Path) -> BotConfig:
    cfg = BotConfig()
    cfg.mode = "mt4"
    cfg.poll_seconds = 1
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "journal.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    # Telegram CONFIGURED for the same reason #190's file documents: the budget
    # derivation includes the Telegram retry ceiling, so an unconfigured desk
    # derives a budget the live box never had and the fixture would measure
    # 608.5 against the wrong number.
    cfg.telegram = TelegramConfig(token="t" * 10, chat_id="42")
    return cfg


def _desk(cfg: BotConfig, tmp_path: Path) -> Engine:
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(80, drift=0.0004, seed=3))
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()
    return engine


def _breach_rows(cfg: BotConfig) -> list[dict]:
    return [
        r
        for r in Journal(cfg.journal_path).tail(500)
        if r.get("event") == "tick_gap_breach"
    ]


def _fields(cfg: BotConfig) -> dict[str, str]:
    hb = watchdog.read(watchdog.heartbeat_path_for(cfg.journal_path))
    assert hb is not None, "no heartbeat was written, so nothing here measures"
    return hb.fields


class _FailingWrite:
    """Make `journal.write` raise for the first `fail_times` breach rows.

    Only `tick_gap_breach` is failed. Failing every row would also break the
    rows the fixture needs to set itself up, and would stop this measuring the
    one write the issue is about.
    """

    def __init__(self, journal: Journal, fail_times: int, exc: BaseException | None = None):
        self.journal = journal
        self.fail_times = fail_times
        self.exc = exc or PermissionError(13, "Permission denied")
        self.attempts = 0
        self._real = journal.write

    def __call__(self, event: str, **fields: object) -> None:
        if event == "tick_gap_breach":
            self.attempts += 1
            if self.attempts <= self.fail_times:
                raise self.exc
        self._real(event, **fields)

    def install(self) -> None:
        self.journal.write = self  # type: ignore[method-assign]


def _tick_with_gap(engine: Engine, clock: list[float], gap_s: float) -> None:
    """One tick, a gap, one more tick. The second is where the gap is seen."""
    engine.step_all()
    clock[0] += gap_s
    engine.step_all()


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    import straightedge.engine as engine_mod

    c = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: c[0])
    return c


# --- 1. the defect ---------------------------------------------------------


def test_a_failed_breach_write_is_retried_on_the_next_tick(
    tmp_path: Path, clock: list[float]
) -> None:
    """THE DEFECT. FAILS IF the flag is set before the write.

    On `main` the first failure consumed the only attempt, `_hb_over_warned`
    stayed True for the process, and the row never existed.
    """
    cfg = _cfg(tmp_path)
    engine = _desk(cfg, tmp_path)
    fail = _FailingWrite(engine.journal, fail_times=1)
    fail.install()

    _tick_with_gap(engine, clock, LIVE_GAP_S)
    assert fail.attempts == 1, "the breach write was not attempted"
    assert not engine._hb_breach_recorded, "the write raised; nothing landed"
    assert _breach_rows(cfg) == [], "a row exists despite the write raising"

    # The NEXT tick must try again.
    engine.step_all()
    engine.stop()

    rows = _breach_rows(cfg)
    assert len(rows) == 1, f"expected exactly one breach row, got {len(rows)}"
    assert engine._hb_breach_recorded
    assert _fields(cfg)["breach_rows_lost"] == "0", (
        "nothing was lost: the retry landed it"
    )


def test_the_row_records_the_breach_that_was_DETECTED(
    tmp_path: Path, clock: list[float]
) -> None:
    """A retried write must not report a gap that grew while it was failing.

    Otherwise the same row reports a different fact depending on when the
    journal came back, and the figure an operator reconstructs is not the
    figure the desk measured.
    """
    cfg = _cfg(tmp_path)
    engine = _desk(cfg, tmp_path)
    fail = _FailingWrite(engine.journal, fail_times=1)
    fail.install()

    _tick_with_gap(engine, clock, LIVE_GAP_S)
    # The journal is still broken, and the box gets MUCH slower meanwhile.
    clock[0] += LIVE_GAP_S * 3
    engine.step_all()
    engine.stop()

    rows = _breach_rows(cfg)
    assert len(rows) == 1, rows
    assert rows[0]["gap_s"] == round(LIVE_GAP_S, 1), (
        f"the row reports {rows[0]['gap_s']}, not the detected {LIVE_GAP_S}"
    )


# --- 2. the bound, and that giving up is recorded --------------------------


def test_a_journal_that_stays_broken_gives_up_and_SAYS_SO(
    tmp_path: Path, clock: list[float]
) -> None:
    """The bound, and the reason it is safe to have one.

    An unbounded retry sits in the latency path this record exists to explain.
    A bounded one that went quiet would be the same defect one level out, so
    the give-up is published where a reader already looks.
    """
    cfg = _cfg(tmp_path)
    engine = _desk(cfg, tmp_path)
    fail = _FailingWrite(engine.journal, fail_times=999)
    fail.install()

    _tick_with_gap(engine, clock, LIVE_GAP_S)
    for _ in range(_HB_BREACH_WRITE_ATTEMPTS + 4):
        engine.step_all()
    engine.stop()

    assert fail.attempts == _HB_BREACH_WRITE_ATTEMPTS, (
        f"the retry is not bounded: {fail.attempts} attempts for a limit of "
        f"{_HB_BREACH_WRITE_ATTEMPTS}"
    )
    assert not engine._hb_breach_recorded
    assert _breach_rows(cfg) == []
    assert _fields(cfg)["breach_rows_lost"] == "1", (
        "the desk gave up and did not say so, which is the defect one level out"
    )


def test_a_healthy_desk_publishes_the_field_as_zero(
    tmp_path: Path, clock: list[float]
) -> None:
    """The control. A field that only appears when something is wrong is a
    field no reader learns to expect, so it is published unconditionally."""
    cfg = _cfg(tmp_path)
    engine = _desk(cfg, tmp_path)
    _tick_with_gap(engine, clock, LIVE_GAP_S)
    engine.stop()

    assert len(_breach_rows(cfg)) == 1
    assert _fields(cfg)["breach_rows_lost"] == "0"


# --- 3. the two surfaces are separate -------------------------------------


def test_the_stderr_warning_still_fires_once_when_the_write_fails(
    tmp_path: Path, clock: list[float], capsys: pytest.CaptureFixture[str]
) -> None:
    """TWO SURFACES, TWO FLAGS, which is the design decision of this issue.

    The warning is about the BREACH and the row is about the RECORD. A reader
    losing the print loses nothing durable; collapsing them into one flag is
    what made a lost row look handled.
    """
    cfg = _cfg(tmp_path)
    engine = _desk(cfg, tmp_path)
    fail = _FailingWrite(engine.journal, fail_times=999)
    fail.install()

    _tick_with_gap(engine, clock, LIVE_GAP_S)
    for _ in range(_HB_BREACH_WRITE_ATTEMPTS + 2):
        engine.step_all()
    engine.stop()

    out = capsys.readouterr().out
    warnings = [ln for ln in out.splitlines() if "exceeds the derived budget" in ln]
    assert len(warnings) == 1, (
        f"the breach warning printed {len(warnings)} times; it is once per "
        "process per breach and a retry must not repeat it"
    )
    lost = [ln for ln in out.splitlines() if "is LOST" in ln]
    assert len(lost) == 1, "giving up printed %d times" % len(lost)


def test_a_non_oserror_is_not_swallowed(tmp_path: Path, clock: list[float]) -> None:
    """NARROWNESS. The failure this survives is the file being unavailable.

    A serialisation defect is not a transient and must not be retried into
    silence, so anything that is not an `OSError` propagates.
    """
    cfg = _cfg(tmp_path)
    engine = _desk(cfg, tmp_path)
    fail = _FailingWrite(engine.journal, fail_times=999, exc=TypeError("not serialisable"))
    fail.install()

    engine.step_all()
    clock[0] += LIVE_GAP_S
    with pytest.raises(TypeError):
        engine.step_all()
    engine.stop()


# --- 4. the field is pinned to the document -------------------------------


def test_the_documented_heartbeat_fields_are_the_rendered_ones() -> None:
    """The field list in `docs/CONTRACT.md` is READ, not trusted.

    Nothing asserted this before, so the documented format and the rendered
    format could drift, and this change adds a field to both. Same shape as
    the bound-versus-document test straightedge#226 landed: a figure stated in
    prose that nothing reads is a magic number.
    """
    rendered = watchdog.render(
        __import__("datetime").datetime(2026, 10, 10, tzinfo=__import__("datetime").timezone.utc),
        blocked="",
        mode="paper",
        stale_after_s=10,
        tick_budget_s=5,
        tick_gap_max_s=0.0,
        tick_gap_ever_s=0.0,
        run_id="r",
        started_at="s",
        deployed="d",
    )
    emitted = [ln.partition("=")[0] for ln in rendered.splitlines()[1:] if "=" in ln]

    text = (ROOT / "docs" / "CONTRACT.md").read_text(encoding="utf-8")

    # ANCHORED TO THE LIST, not to the file. A file-wide search for
    # `` `name=` `` also finds the MT4 mailbox fields and every other
    # key=value format this document describes, so it would be satisfied by a
    # name documented anywhere, which is the #220 trap. Take the one paragraph
    # that enumerates the heartbeat fields and read only that.
    start = text.index("After it, one `key=value` per line:")
    para = text[start : text.index("\n\n", start)]
    documented = re.findall(r"`([a-z_]+)=`", para)

    # BOTH DIRECTIONS. A one-sided check is satisfied by deleting a field from
    # the renderer, which is the asymmetry this repo keeps finding: assert the
    # SETS are equal, so a field added to either side without the other reds.
    # SETS, not sequences. The paragraph legitimately re-mentions a field in a
    # parenthetical, so comparing sequences compares the PROSE and not the
    # contract: the first version of this assertion failed with both
    # differences empty, which is a multiplicity complaint and was the
    # instrument being wrong rather than the subject.
    assert set(documented) == set(emitted), (
        "the heartbeat format has drifted.\n"
        f"  rendered but undocumented: {sorted(set(emitted) - set(documented))!r}\n"
        f"  documented but unrendered: {sorted(set(documented) - set(emitted))!r}\n"
        "docs/CONTRACT.md calls this a contract; nothing asserted it before."
    )
    assert documented, "the field list was not found, so this measured nothing"
