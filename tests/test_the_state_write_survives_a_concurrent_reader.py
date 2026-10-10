"""straightedge#251: a refused state write HALTS the desk, and it stops managing.

Found while fixing #242 and reached by the same mechanism. On Windows
`MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)` fails with `ERROR_ACCESS_DENIED`
while the destination is open in a process that did not ask for
`FILE_SHARE_DELETE`, and CPython's `open()` does not ask. On POSIX a rename
over an open file succeeds, so no local run reaches the conflict.

## Why this one is not the heartbeat

`save_snapshot` raising `StateUnwritable` makes `RiskManager._persist_state`
set `_halted = True` with `_halt_reason = "state_unwritable"`, and a halted
desk returns from `step_all` BEFORE `_resolve_pending`, `_check_stops` and
`_manage_open`. **So the observable is not "no exception", it is "the desk is
not halted".** That is what these tests assert, because an exception absorbed
while the halt flag stayed set would be a fix in name only.

`observe()` persists whenever the snapshot MOVES, and a rising `peak_equity`
moves it, so this file is written at roughly the tick cadence rather than
rarely. `record_trade()` persists immediately at the send.

## What stays broken ON PURPOSE

A hold that outlasts the retry window still halts. The module's contract is
that a write it cannot complete is COULD NOT MEASURE and the money gate fails
closed on it, so the retry absorbs a RACE and never a CONDITION: a full disk, a
vanished directory and a revoked ACL are states waiting cannot fix. There is a
test below that requires exactly that, and it is a sibling of the fix, not a
gap in it.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from straightedge.config import BotConfig
from straightedge.models import Account, EquitySnapshot
from straightedge.risk import RiskManager
from straightedge.atomic import replace_retrying_on_share_conflict
from straightedge.state import (
    STATE_REPLACE_RETRY_SECONDS,
    StateUnwritable,
    save_snapshot,
)


def _acct(equity: float) -> Account:
    """A real `Account`, not a stub: every field the dataclass requires.

    Built here rather than inline so the three call sites below cannot drift
    apart, and so a field added to `Account` breaks one place.
    """
    return Account(
        login=1,
        balance=10_000.0,
        equity=equity,
        margin=0.0,
        margin_free=equity,
        profit=equity - 10_000.0,
        leverage=100,
        currency="USD",
    )


def _snap(equity: float = 10_000.0) -> EquitySnapshot:
    return EquitySnapshot(
        time=1,
        balance=equity,
        equity=equity,
        peak_equity=equity,
        day_start_equity=equity,
        day_key="2026-10-10",
        trades_today=0,
        advice_turns_today=0,
    )


def _rm(tmp_path: Path) -> RiskManager:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    return RiskManager(cfg, halt_dir=str(tmp_path), state_path=tmp_path / "state.json")


# --- 1. the loop, which is all a raised exception can pin -------------------


def test_a_refused_replace_is_retried_until_it_lands(tmp_path: Path) -> None:
    """The LOOP, not the conflict. A raise is not a held handle."""
    dest = tmp_path / "s.json"
    tmp = tmp_path / "s.json.tmp"
    dest.write_text("old", encoding="utf-8")
    tmp.write_text("new", encoding="utf-8")

    attempts: list[int] = []
    real = os.replace

    def flaky(src, dst):  # type: ignore[no-untyped-def]
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    os.replace = flaky  # type: ignore[assignment]
    try:
        replace_retrying_on_share_conflict(tmp, dest)
    finally:
        os.replace = real  # type: ignore[assignment]

    assert len(attempts) == 3, attempts
    assert dest.read_text(encoding="utf-8") == "new"


def test_a_non_permission_error_still_raises_state_unwritable_at_once(
    tmp_path: Path,
) -> None:
    """NARROWNESS, and it must keep the module's own error type.

    A retry absorbs a RACE, never a CONDITION. A bare `except OSError` here
    would absorb a full disk, a vanished directory and a revoked ACL, and the
    money gate would stop failing closed on states that waiting cannot fix.
    """
    attempts: list[int] = []
    real = os.replace

    def boom(src, dst):  # type: ignore[no-untyped-def]
        attempts.append(1)
        raise OSError(28, "No space left on device")

    os.replace = boom  # type: ignore[assignment]
    try:
        with pytest.raises(StateUnwritable):
            save_snapshot(tmp_path / "s.json", _snap())
    finally:
        os.replace = real  # type: ignore[assignment]

    assert len(attempts) == 1, f"a non-PermissionError was retried: {attempts}"


def test_a_hold_that_outlasts_the_window_STILL_HALTS(tmp_path: Path) -> None:
    """The behaviour that must NOT be fixed, asserted as a sibling of the fix.

    `state.py` fails closed on a write it cannot complete, and a retry that
    waited forever would convert a rare race into a desk that never halts on a
    genuinely unwritable file.
    """
    rm = _rm(tmp_path)
    # PERSIST ONCE FIRST, and this line is load-bearing rather than setup.
    # Without it the destination does not exist, so a mutation that SWALLOWS
    # the refusal at the deadline still halts: `save_snapshot` continues to
    # `os.chmod(p, ...)` on a missing file and raises `FileNotFoundError`
    # instead. The test then passes for the wrong reason and cannot tell
    # "the retry gave up and raised" from "the retry swallowed and a later
    # line failed". Measured: with the destination absent, replacing the
    # `raise` at the deadline with a `return` left this test GREEN.
    rm.observe(_acct(10_000.0), _now())
    assert Path(rm.state_path).exists(), "nothing was persisted, so chmod, not the replace, would be the thing that fails"

    real = os.replace

    def always(src, dst):  # type: ignore[no-untyped-def]
        raise PermissionError(5, "Access is denied")

    os.replace = always  # type: ignore[assignment]
    try:
        rm.observe(_acct(10_500.0), _now())
    finally:
        os.replace = real  # type: ignore[assignment]

    assert rm.is_halted, "an unwritable state file must still halt the desk"
    assert rm.halt_reason == "state_unwritable", rm.halt_reason


def _now():
    from datetime import datetime, timezone

    return datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)


# --- 2. the observable: the desk is NOT halted -----------------------------


def test_a_retried_write_leaves_the_desk_UNHALTED(tmp_path: Path) -> None:
    """The fix stated as what an operator sees, not as an absent exception.

    An exception absorbed while `_halted` stayed set would be a fix in name
    only, so the assertion is the halt flag and the halt reason.
    """
    rm = _rm(tmp_path)
    real = os.replace
    attempts: list[int] = []

    def flaky(src, dst):  # type: ignore[no-untyped-def]
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    os.replace = flaky  # type: ignore[assignment]
    try:
        rm.observe(_acct(10_500.0), _now())
    finally:
        os.replace = real  # type: ignore[assignment]

    assert len(attempts) >= 2, attempts
    assert not rm.is_halted, f"the desk halted anyway: {rm.halt_reason}"
    assert rm.halt_reason == ""
    assert rm.state_error == ""


def test_the_retry_window_cannot_delay_a_tick() -> None:
    """Sized by what the TICK can afford, since this writes at tick cadence."""
    poll = BotConfig().poll_seconds
    assert STATE_REPLACE_RETRY_SECONDS < poll / 4, (
        f"a {STATE_REPLACE_RETRY_SECONDS}s window against a {poll}s tick can "
        "delay the next tick"
    )


# --- 3. the real thing, which only one platform can run --------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "UNMEASURED on this platform: a POSIX rename over an open file "
        "succeeds, so the share-mode conflict straightedge#251 is about "
        "cannot be reached here. This case runs on the windows-latest leg."
    ),
)
def test_the_desk_is_not_halted_once_a_transient_reader_lets_go(
    tmp_path: Path,
) -> None:
    """The defect itself, with a REAL handle that lets go.

    Driven as the OBSERVABLE rather than as a file operation: after a reader
    holds the state file and releases it, the desk must not be halted.

    Carries its own positive control, a raw `os.replace` that must be REFUSED
    while the handle is held, so this cannot pass by the conflict never having
    happened. straightedge#242's first version of this shape held the handle
    across the whole call and therefore asserted the fix would do something it
    must not; the holder here releases on a signal.
    """
    rm = _rm(tmp_path)
    acct = _acct(10_000.0)
    rm.observe(acct, _now())
    dest = Path(rm.state_path)
    assert dest.exists(), "nothing was persisted, so there is no file to hold"

    holding = threading.Event()
    may_release = threading.Event()
    released = threading.Event()
    hold_s = 0.05

    def hold() -> None:
        with open(dest, "r", encoding="utf-8") as fh:
            fh.read()
            holding.set()
            may_release.wait(timeout=5.0)
            time.sleep(hold_s)
        released.set()

    reader = threading.Thread(target=hold, name="se251-holder", daemon=True)
    reader.start()
    assert holding.wait(timeout=5.0), "the holding thread never opened the file"

    decoy = tmp_path / "decoy.tmp"
    decoy.write_text("decoy", encoding="utf-8")
    with pytest.raises(PermissionError):
        os.replace(decoy, dest)

    may_release.set()
    started = time.monotonic()
    rm.observe(_acct(10_500.0), _now())
    elapsed = time.monotonic() - started
    reader.join(timeout=5.0)

    assert released.is_set(), "the holding thread did not finish"
    assert not rm.is_halted, f"the desk halted anyway: {rm.halt_reason}"
    assert rm.halt_reason == ""
    assert elapsed >= hold_s / 2, (
        f"the write took {elapsed:.3f}s, too fast to have been refused and "
        "retried; the conflict may not have occurred"
    )
    assert elapsed < STATE_REPLACE_RETRY_SECONDS * 4, (
        f"the write took {elapsed:.3f}s against a "
        f"{STATE_REPLACE_RETRY_SECONDS}s window"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="this is the POSIX half of the platform statement",
)
def test_on_posix_a_held_handle_does_not_refuse_the_replace(tmp_path: Path) -> None:
    """Why a local green proves nothing about straightedge#251."""
    dest = tmp_path / "s.json"
    dest.write_text("first", encoding="utf-8")
    tmp = tmp_path / "s.json.tmp"
    tmp.write_text("second", encoding="utf-8")
    with open(dest, "r", encoding="utf-8") as holder:
        holder.read()
        os.replace(tmp, dest)
    assert dest.read_text(encoding="utf-8") == "second"
