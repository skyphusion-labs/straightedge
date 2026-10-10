"""straightedge#218: the two paths #190 left unpinned.

#190 (`425b623d1393678c8fdaabe926a1842a4a47f452`, closing #153) split one field
answering two questions into two fields, and a reviewer raised two
non-blocking findings against the code it touched. Both survived a reviewer
mutation, which is the definition of an unpinned path:

1. **The `except ValueError` branch in `Engine._restore_gap_ever` has no test.**
   Hardcoding its behaviour left the suite green.
2. **The watcher's box-breach note is unpinned in the BOTH-over case.** Removing
   the `over_budget != "1"` clause left the suite green, and that clause is the
   only thing stopping a note which SAYS "THIS PROCESS is inside its budget"
   from being printed about a process that is not.

## The numbers in #218's body are corrected here, and that is why they were measured again

#218 records, from the reviewer's reading, that a damaged `tick_gap_ever_s` of
`garbage`, `nan`, `-5` or empty "publishes `0.0`". **Measured on a real engine
it publishes the live process's own observation, not `0.0`:** `2.0` for a
process that saw a 2.0s gap, `3.0` for the next one. `0.0` is only what a
process that observed nothing at all would publish, so the figure in the issue
is an artefact of its fixture rather than the behaviour.

The behaviour CLASS is what the issue says it is, and it is the right one: a
value the desk cannot read is discarded and the chain restarts from this
process's own measurement, never from a number nobody measured. Pinning the
class with the wrong number would have been a test that passes for a reason
that is not the reason.

## `inf` is pinned as measured, and it contradicts a claim in the code

Measured, a damaged `tick_gap_ever_s=inf` does three things, and the second and
third are more than #218 claims:

* it carries through as `inf`, and STICKS: a second and third process fold it
  with `max` and it never comes down, so only deleting `journal.heartbeat`
  clears it;
* it sets `over_budget_ever=1`;
* it makes `watchdog.decide` tell the operator **this BOX has breached it
  before (tick_gap_ever_s=inf)**, which is a breach claim no process observed.

`_restore_gap_ever`'s own docstring said the figure "is never lower than what
this process has itself observed, so it is always a true lower bound on the
box's history and never an invented one". `inf` IS an invented one. The
docstring is corrected in this change, because a false claim in the place a
reader meets the field is worse than no claim.

**Whether the restore should REFUSE a non-finite value is a behaviour change on
a live operator surface, so it is not taken here**: #218 says two tests and no
behaviour change, and that is right. It is `#328`, which carries the three
shapes it could take. The properties above are pinned as they are, so whichever
way that decision goes it has to be a deliberate change that reds this file and
not a silent one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from straightedge import watchdog
from test_tick_gap_survives_restart import (
    LIVE_GAP_AFTER_RESTART_S,
    LIVE_GAP_S,
    _cfg,
    _fields,
    _run_with_gap,
)
from test_watchdog import MT4_BUDGET

#: The gap each probe process observes. Both are well under the budget, so
#: nothing these processes see can set `over_budget` by itself and every
#: breach claim below has to have come from the damaged field.
FIRST_GAP_S = 2.0
SECOND_GAP_S = 3.0

assert FIRST_GAP_S < MT4_BUDGET
assert SECOND_GAP_S < MT4_BUDGET

#: The per-process note, and the BOX note, by their opening words. Matched on
#: the claim rather than the whole sentence, because the whole sentence
#: interpolates figures.
PROCESS_NOTE = "the desk has observed a gap between ticks longer"
BOX_NOTE = "this BOX has breached it before"
#: The note straightedge#328 added, and the one the absence branch must NOT
#: emit for a damaged figure: reading damage as an upgrade gap would tell the
#: operator to start the chain here, which is the clean-history reading.
DAMAGED_NOTE = "this box's history is UNREADABLE"
NO_FIELD_NOTE = "this desk publishes no over_budget_ever"


def _damaged_prior(raw: str) -> str:
    """A 1.8.0-shaped heartbeat whose box figure is `raw`.

    Crafted rather than produced by an engine, and that is not a stub: this is
    a DAMAGED file, and no engine writes one. Every OTHER field is exactly what
    a healthy 1.8.0 desk publishes, so the damaged field is the only variable.
    """
    return (
        "\n".join(
            [
                datetime(2026, 10, 8, 12, 0, tzinfo=timezone.utc).isoformat(),
                "blocked=",
                "mode=mt4",
                "stale_after_s=428",
                f"tick_budget_s={MT4_BUDGET}",
                "tick_gap_max_s=7.7",
                "over_budget=0",
                f"tick_gap_ever_s={raw}",
                "over_budget_ever=0",
                "run_id=deadbeef",
                "started_at=2026-10-08T06:00:00+00:00",
                "deployed=unmeasured",
            ]
        )
        + "\n"
    )


def _report(cfg) -> watchdog.Report:
    return watchdog.decide(
        watchdog.heartbeat_path_for(cfg.journal_path),
        now=datetime.now(timezone.utc),
        cfg=cfg,
    )


# ----------------------------------------------------------------- item 1


@pytest.mark.parametrize("raw", ["garbage", "nan", "-5", "", "1.2.3", "None"])
def test_a_damaged_box_figure_is_discarded_and_the_chain_restarts_here(
    raw: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The unpinned `except ValueError` branch, plus the values that reach `max`.

    `garbage`, `1.2.3` and `None` take the `except ValueError` branch. `nan` and
    `-5` parse and are then discarded by `max`, and the empty string never
    reaches `float` at all. Four different mechanisms, ONE published outcome,
    which is why they belong in one table: the desk publishes what IT measured.

    FAILS IF: the branch adopts the damaged value, or raises out of
    `_write_heartbeat`, or falls back to the previous process's
    `tick_gap_max_s` of 7.7, which is the other question's number.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    dest = watchdog.heartbeat_path_for(cfg.journal_path)
    dest.write_text(_damaged_prior(raw), encoding="utf-8")
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])

    _run_with_gap(cfg, tmp_path, clock, FIRST_GAP_S)

    after = _fields(cfg)
    assert after["tick_gap_ever_s"] == str(FIRST_GAP_S), (
        f"a damaged box figure of {raw!r} published "
        f"{after['tick_gap_ever_s']!r} rather than this process's own "
        f"{FIRST_GAP_S}"
    )
    assert after["over_budget_ever"] == "0", (
        "a damaged field produced a breach claim this process did not observe"
    )
    assert BOX_NOTE not in _report(cfg).text, (
        "the watcher told the operator the box breached, on the strength of a "
        "field it could not read"
    )


def test_a_damaged_box_figure_does_not_stop_the_heartbeat_being_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The branch exists so a damaged file cannot take the heartbeat with it.

    This is the half the parametrised test above cannot state, because it reads
    the heartbeat and would therefore fail for this reason as well without
    saying so. Here the heartbeat's `run_id` is what proves THIS process wrote
    it, so the reason is named rather than inferred from a file existing.

    FAILS IF: `float(raw)` is allowed to raise out of `_write_heartbeat`. The
    desk would then stop publishing a heartbeat at all, and
    `straightedge-watch` would read that as a dead desk and restart it, in a
    loop, on one unreadable line in a diagnostic field.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    dest = watchdog.heartbeat_path_for(cfg.journal_path)
    dest.write_text(_damaged_prior("garbage"), encoding="utf-8")
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])

    run_id = _run_with_gap(cfg, tmp_path, clock, FIRST_GAP_S)

    assert run_id, "the process never got a run_id, so it never wrote a heartbeat"
    assert _fields(cfg)["run_id"] == run_id, (
        "the heartbeat on disk is not this process's; the damaged field stopped "
        "the write"
    )


def test_a_non_finite_box_figure_is_published_as_damaged_and_claims_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """straightedge#328, and this test used to pin the OPPOSITE on purpose.

    It pinned `inf` carrying through, publishing as `inf`, setting
    `over_budget_ever=1` and making the watcher tell the operator the BOX had
    breached. That was recorded rather than endorsed, and straightedge#328
    decided against it: a breach claim no process observed, on the surface an
    operator reads to judge whether `UNBOUNDED_TAIL_ALLOWANCE` is too tight.

    What is pinned now, and the shape chosen was TWO of the three on the issue
    because they close different directions:

    1. the non-finite figure is refused AT THE WRITE, so nothing non-finite is
       ever published whatever its source, and it is published as `damaged`
       rather than dropped;
    2. `over_budget_ever` is `damaged` too, because a figure that is not a
       measurement cannot answer whether the box breached, and publishing `1`
       or `0` from it would invent the answer in one direction or the other;
    3. the watcher says the history is UNREADABLE, and says neither of the two
       things it could otherwise say;
    4. the PER-PROCESS pair is untouched, which was already true and is the
       property that would make a regression here worse than the defect;
    5. it still STICKS across a restart, because the box history really is
       unrecoverable, and deleting the heartbeat is still the one way out.

    THE SHAPE THAT WAS REFUSED, recorded so the next reader does not re-propose
    it: refusing the non-finite at the RESTORE instead. That is a smaller change
    and it makes `_restore_gap_ever`'s original claim true, but it converts a
    damaged file into a CLEAN box history, and reading absence as clean is the
    one thing straightedge#190 refuses everywhere else.

    FAILS IF: any of the five changes in either direction.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    dest = watchdog.heartbeat_path_for(cfg.journal_path)
    dest.write_text(_damaged_prior("inf"), encoding="utf-8")
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])

    _run_with_gap(cfg, tmp_path, clock, FIRST_GAP_S)
    first = dict(_fields(cfg))
    assert first["tick_gap_ever_s"] == watchdog.DAMAGED, (
        "a non-finite box figure must be published as "
        f"{watchdog.DAMAGED!r}, not as a number: {first['tick_gap_ever_s']!r}"
    )
    assert first["over_budget_ever"] == watchdog.DAMAGED, (
        "the breach flag derived from a non-measurement must not claim an "
        f"answer in either direction: {first['over_budget_ever']!r}"
    )

    text = _report(cfg).text
    assert DAMAGED_NOTE in text, "the operator was not told the history is unreadable"
    assert BOX_NOTE not in text, (
        "the watcher claimed this BOX breached, off a figure no process observed"
    )
    assert NO_FIELD_NOTE not in text, (
        "the watcher read the damage as an upgrade gap, which tells the operator "
        "to start the chain here and is the clean-history reading #190 refuses"
    )

    # A SECOND process on the SAME box, with a fresh monotonic clock. The damage
    # must STICK: the box history really is unrecoverable, so a restart must not
    # quietly turn it into a clean one.
    clock[0] = 0.0
    _run_with_gap(cfg, tmp_path, clock, SECOND_GAP_S)
    second = dict(_fields(cfg))
    assert second["tick_gap_ever_s"] == watchdog.DAMAGED, (
        "the damage did not survive the restart, so a restart launders a "
        f"damaged box history into a clean one: {second['tick_gap_ever_s']!r}"
    )
    assert second["tick_gap_max_s"] == str(SECOND_GAP_S), (
        "the PER-PROCESS figure was contaminated too, which would be a "
        "different and worse defect than the one being fixed"
    )
    assert second["over_budget"] == "0", (
        "the per-process breach flag was contaminated by the damaged box figure"
    )

    # Deleting the file is the documented way out, and that is asserted rather
    # than described, because "sticks until the file is deleted" is a claim
    # about BOTH halves.
    dest.unlink()
    clock[0] = 0.0
    _run_with_gap(cfg, tmp_path, clock, SECOND_GAP_S)
    third = dict(_fields(cfg))
    assert third["tick_gap_ever_s"] == str(SECOND_GAP_S), (
        "deleting the heartbeat did not clear the damaged box figure, so there "
        f"is no way out at all: {third['tick_gap_ever_s']!r}"
    )
    assert third["over_budget_ever"] == "0"


# ----------------------------------------------------------------- item 2


def test_the_box_note_is_not_claimed_when_this_process_has_breached_too(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The both-over case, which is where the wording carries the most and is checked the least.

    The box note opens "THIS PROCESS is inside its budget, but this BOX has
    breached it before". When both figures are over budget that first clause is
    FALSE, and the operator is being told the process is clean at the moment it
    is not. The `over_budget != "1"` clause is the only thing preventing it.

    Driven from a real engine rather than a crafted file: one process observes
    the live 608.5, so `over_budget` and `over_budget_ever` are both set by
    MEASUREMENT, which is the state a real desk is in mid-episode.

    FAILS IF: the `over_budget != "1"` clause is removed. A reviewer mutation
    doing exactly that left #190's suite green.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    _run_with_gap(cfg, tmp_path, clock, LIVE_GAP_S)

    fields = _fields(cfg)
    assert fields["over_budget"] == "1", "the fixture did not breach the budget"
    assert fields["over_budget_ever"] == "1", "the box figure did not record it"

    text = _report(cfg).text
    assert PROCESS_NOTE in text, (
        "the per-process note is missing, so this test is not measuring the "
        "both-over case at all"
    )
    assert BOX_NOTE not in text, (
        "the watcher told the operator THIS PROCESS is inside its budget while "
        "over_budget=1. The note is false in exactly the case where both "
        "figures are over.\n\n" + text
    )


def test_the_box_note_IS_claimed_once_this_process_is_clean_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The control pair, and without it the test above is satisfied by deleting the note.

    Same box, one restart later, the per-process figure clean. Now the box note
    is the only thing that can tell the operator the threshold is still too
    tight for this book, so it MUST appear.
    """
    import straightedge.engine as engine_mod

    cfg = _cfg(tmp_path)
    clock = [0.0]
    monkeypatch.setattr(engine_mod.time, "monotonic", lambda: clock[0])
    _run_with_gap(cfg, tmp_path, clock, LIVE_GAP_S)
    clock[0] = 0.0
    _run_with_gap(cfg, tmp_path, clock, LIVE_GAP_AFTER_RESTART_S)

    fields = _fields(cfg)
    assert fields["over_budget"] == "0", "this process should be clean"
    assert fields["over_budget_ever"] == "1", "the box history should be dirty"

    text = _report(cfg).text
    assert BOX_NOTE in text, (
        "the box note went missing in the one case it exists for: a restarted "
        "desk publishing its cleanest possible history.\n\n" + text
    )
    assert PROCESS_NOTE not in text, (
        "the per-process note was claimed about a clean process"
    )
