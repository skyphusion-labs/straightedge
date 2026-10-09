"""straightedge#153: the worst gap this BOX has seen must outlive the process.

`tick_gap_max_s` answered one question while the runbook asked it another. It
is the largest gap between heartbeat writes THIS PROCESS has seen, and
`over_budget` is derived from it; but what `over_budget` was installed to test
is whether `UNBOUNDED_TAIL_ALLOWANCE` is adequate for this BOOK, and a restart
resets neither the allowance nor the book. So a restart erased the answer.

Measured on the live desk and quoted here as the fixture, not paraphrased.
Before the 2026-10-08 deploy:

    stale_after_s=428  tick_budget_s=214  tick_gap_max_s=608.5  over_budget=1

After the restart that deploy required:

    stale_after_s=428  tick_budget_s=214  tick_gap_max_s=7.7    over_budget=0

Both are correct for their process. The 608.5 became unrecoverable from any
live surface while #143 was citing it as evidence, and the reset is in the
dangerous direction: a desk restarted after a bad episode publishes its
cleanest possible history.

**These tests drive the real arithmetic, not a poked field.** Nothing here
assigns `_hb_gap_max_s` or `_hb_gap_ever_s`. The gap is produced by moving the
monotonic clock between two `step_all` calls on a real `Engine` and letting
`_write_heartbeat` measure it, which is the same seam the live desk used to
produce 608.5. A test that set the figure directly would prove the renderer
works and say nothing about the measurement.

Each test names the world in which it fails, and each was driven RED by
removing the mechanism it covers; the removals are recorded in the PR.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from straightedge import watchdog
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig, SessionConfig, TelegramConfig
from straightedge.engine import Engine
from straightedge.synthetic import generate_bars
from test_watchdog import MT4_BUDGET

#: The two figures the live box actually published, either side of the deploy.
LIVE_GAP_S = 608.5
LIVE_GAP_AFTER_RESTART_S = 7.7
#: The live box derived 214s, and `MT4_BUDGET` is the suite's pin on the same
#: arithmetic. Asserted rather than assumed: if the derivation moves, this
#: file's premise moves with it and the failure should say so here.
assert MT4_BUDGET == 214, "the live measurement was taken against a 214s budget"
assert LIVE_GAP_S > MT4_BUDGET, "the fixture gap has to breach the budget"
assert LIVE_GAP_AFTER_RESTART_S < MT4_BUDGET, "the post-restart gap must be clean"


def _cfg(tmp_path: Path) -> BotConfig:
    cfg = BotConfig()
    cfg.mode = "mt4"
    cfg.poll_seconds = 1
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "journal.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    # Telegram CONFIGURED, and that is load-bearing rather than incidental:
    # `tick_budget_seconds` is the Telegram retry ceiling plus the two venue
    # commands `step_all` spends before it can write a heartbeat, so an
    # unconfigured desk derives 10s and this file would be measuring its 608.5
    # against a budget the live box never had. With a token and
    # poll_seconds = 1 the derivation is the live 214s, pinned by `MT4_BUDGET`.
    cfg.telegram = TelegramConfig(token="t" * 10, chat_id="42")
    return cfg


def _desk(cfg: BotConfig, tmp_path: Path) -> Engine:
    """One desk PROCESS. A second call is a restart on the same box."""
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(80, drift=0.0004, seed=3))
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()
    return engine


def _fields(cfg: BotConfig) -> dict[str, str]:
    hb = watchdog.read(watchdog.heartbeat_path_for(cfg.journal_path))
    assert hb is not None, "no heartbeat was written, so nothing here is measuring"
    return hb.fields


def _run_with_gap(cfg: BotConfig, tmp_path: Path, clock: list[float], gap_s: float) -> str:
    """One process that ticks, waits `gap_s`, ticks again. Returns its run_id.

    The first `step_all` pins the baseline (`_hb_last_mono` is None until a
    heartbeat has been written), so the gap is only observable on the second.
    """
    engine = _desk(cfg, tmp_path)
    engine.step_all()
    clock[0] += gap_s
    engine.step_all()
    run_id = engine._hb_run_id
    engine.stop()
    return run_id


def test_a_restart_does_not_lose_the_worst_gap_this_box_has_seen(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The defect, reproduced end to end with the live figures.

    FAILS IF: the box figure is not carried across the restart, which is what
    `main` did. Then `tick_gap_ever_s` reads 7.7 and `over_budget_ever` reads
    0 after the restart, and the only trace that this box ever went 608 seconds
    between completed ticks is gone from every live surface.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])

    first_run = _run_with_gap(cfg, tmp_path, clock, LIVE_GAP_S)
    before = _fields(cfg)
    assert before["tick_gap_max_s"] == "608.5"
    assert before["over_budget"] == "1"
    assert before["tick_gap_ever_s"] == "608.5", (
        "the first process must establish the box history from its own "
        "observations; there was no earlier heartbeat to restore from"
    )
    assert before["over_budget_ever"] == "1"

    # A NEW process on the SAME box. A restart gets a fresh monotonic clock,
    # which is exactly why the per-process figure resets.
    clock[0] = 0.0
    second_run = _run_with_gap(cfg, tmp_path, clock, LIVE_GAP_AFTER_RESTART_S)
    after = _fields(cfg)
    assert second_run != first_run, (
        "the two desks share a run_id, so this test is not measuring a restart"
    )

    # The per-process half is UNCHANGED and still answers its own question.
    assert after["tick_gap_max_s"] == "7.7"
    assert after["over_budget"] == "0", (
        "this process really is inside its budget and must be able to say so; "
        "a figure that can never go green is one an operator learns to ignore"
    )

    # The box half survived, and this is the whole issue.
    assert after["tick_gap_ever_s"] == "608.5", (
        "the restart erased the worst gap this box has ever observed, so the "
        "figure #143 cites as evidence is unrecoverable from any live surface"
    )
    assert after["over_budget_ever"] == "1", (
        "a freshly restarted desk is reporting a clean history it did not earn"
    )


def test_the_box_figure_is_never_lower_than_what_this_process_saw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A restored figure must not be able to UNDERSTATE the live one.

    The restore reads the prior heartbeat and the desk keeps measuring, so the
    published figure is a maximum over both. The order matters: a worse gap
    AFTER a restart has to raise the box figure, not be hidden by it.

    FAILS IF: the restore assigns instead of taking a maximum, so a process
    that sees something worse than the restored figure publishes the smaller
    number.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])

    _run_with_gap(cfg, tmp_path, clock, float(MT4_BUDGET) + 10.0)
    assert _fields(cfg)["tick_gap_ever_s"] == f"{MT4_BUDGET + 10.0:.1f}"

    clock[0] = 0.0
    _run_with_gap(cfg, tmp_path, clock, LIVE_GAP_S)
    after = _fields(cfg)
    assert after["tick_gap_max_s"] == "608.5"
    assert after["tick_gap_ever_s"] == "608.5", (
        "the second process observed the worse gap and the box figure did not "
        "move, so the restored value is overwriting the measurement"
    )


def test_an_older_desks_heartbeat_does_not_become_a_clean_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Upgrading from a build with no box figure starts the chain HERE.

    The previous process's `tick_gap_max_s` is deliberately NOT adopted as the
    box figure. It is the answer to the other question, and reading it as the
    box history would reintroduce the conflation this issue exists to remove:
    a one-process reading published as an all-time one.

    FAILS IF: the restore falls back to `tick_gap_max_s` when the box field is
    absent. Then this desk claims a 608.5 box history it never observed and
    cannot vouch for, and the claim is indistinguishable from a measured one.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    dest = watchdog.heartbeat_path_for(cfg.journal_path)
    # A 1.6.0 heartbeat, byte-for-byte in the shipped format of that build.
    dest.write_text(
        "\n".join(
            [
                datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc).isoformat(),
                "blocked=",
                "mode=mt4",
                "stale_after_s=428",
                "tick_budget_s=214",
                "tick_gap_max_s=608.5",
                "over_budget=1",
                "run_id=deadbeef",
                "started_at=2026-10-08T06:00:00+00:00",
                "deployed=unmeasured",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    _run_with_gap(cfg, tmp_path, clock, 2.0)

    after = _fields(cfg)
    assert after["tick_gap_ever_s"] == "2.0", (
        "the desk adopted the previous PROCESS's figure as the BOX history"
    )
    assert after["over_budget_ever"] == "0"


def test_the_watcher_says_the_box_breached_when_this_process_has_not(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A durable figure published to nobody is not an improvement.

    FAILS IF: `decide` reports only the per-process figure. Then the operator
    reading `watch` after a restart sees a clean desk, which is the reassuring
    half of a two-part answer and the reason this issue was filed.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    _run_with_gap(cfg, tmp_path, clock, LIVE_GAP_S)
    clock[0] = 0.0
    _run_with_gap(cfg, tmp_path, clock, LIVE_GAP_AFTER_RESTART_S)

    report = watchdog.decide(
        watchdog.heartbeat_path_for(cfg.journal_path),
        now=datetime.now(timezone.utc),
        cfg=cfg,
    )
    assert "this BOX has breached it before" in report.text
    assert "608.5" in report.text
    # The per-process NOTE must NOT be claimed, because this process is clean.
    assert "the desk has observed a gap between ticks longer" not in report.text


def test_a_heartbeat_with_no_box_field_is_not_read_as_a_clean_history(
    tmp_path: Path,
) -> None:
    """The absent-check-reads-as-passed case, on the watcher side.

    An older desk publishes no `over_budget_ever`. That is unknown, not clean,
    and `decide` has to say which.

    FAILS IF: the branch treats a missing field as `0` and stays silent. The
    operator then reads a watcher that is quietly unable to answer the question
    it appears to be answering.
    """
    dest = tmp_path / "journal.heartbeat"
    dest.write_text(
        "\n".join(
            [
                datetime.now(timezone.utc).isoformat(),
                "blocked=",
                "mode=mt4",
                "stale_after_s=428",
                "tick_budget_s=214",
                "tick_gap_max_s=7.7",
                "over_budget=0",
                "run_id=deadbeef",
                "started_at=2026-10-08T06:00:00+00:00",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    report = watchdog.decide(dest, now=datetime.now(timezone.utc))
    assert "publishes no over_budget_ever" in report.text
    assert "NOT read as a clean history" in report.text


def test_the_breach_record_outlives_the_heartbeat_that_reported_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The journal answers HOW OFTEN, which neither figure can.

    The heartbeat carries one maximum. Two breaches a week apart and one
    breach look identical in it, and the stdout warning that used to be the
    only other trace goes to a file nobody reads on the deployed box.

    FAILS IF: the breach is only printed. Then the question "how often does
    this box breach" has no answer on the box at all, and the heartbeat's
    maximum is the entire record.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])

    first_run = _run_with_gap(cfg, tmp_path, clock, LIVE_GAP_S)
    clock[0] = 0.0
    # A second process that also breaches: two occurrences, one maximum.
    second_run = _run_with_gap(cfg, tmp_path, clock, float(MT4_BUDGET) + 1.0)

    from straightedge.journal import Journal

    rows = [r for r in Journal(cfg.journal_path).tail(500) if r.get("event") == "tick_gap_breach"]
    assert len(rows) == 2, (
        "one record per process per breach; the heartbeat cannot distinguish "
        f"two breaches from one and the journal has to, got {rows}"
    )
    assert rows[0]["gap_s"] == 608.5
    assert rows[0]["budget_s"] == MT4_BUDGET
    assert rows[0]["symbols"] == 1
    assert rows[0]["run_id"] == first_run
    assert rows[1]["run_id"] == second_run
    assert "positions" not in rows[0], (
        "a position count means a venue round trip inside the heartbeat "
        "writer, which would add latency to the path this record explains"
    )
