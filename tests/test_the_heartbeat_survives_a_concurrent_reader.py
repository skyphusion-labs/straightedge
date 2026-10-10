"""straightedge#242: a concurrent READER of the heartbeat aborted the tick.

Found by a live read of the deployed desk, never by this suite:

    [WinError 5] Access is denied:
    'C:\\bot-state\\journal.heartbeat.tmp' -> 'C:\\bot-state\\journal.heartbeat'

One occurrence in 59 `loop_error` records; the other 58 were a different and
familiar class. On Windows `MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)` fails
with `ERROR_ACCESS_DENIED` when the destination is open in a process that did
not ask for `FILE_SHARE_DELETE`, and CPython's `open()` does not ask.

## One seam

The call site writes `tmp.replace(dest)`-shaped code but the shared helper
in `straightedge.atomic` calls `os.replace`, so **`os.replace` is the one
seam** and these tests patch it there. Patching `Path.replace` would no
longer intercept anything: the refusal would never be injected and the
tests would pass having measured nothing, which is the failure mode this
file exists to avoid (straightedge#251).

## What each test here can and cannot prove

**A monkeypatched `replace` that RAISES is not a held handle.** It proves the
retry loop calls again and gives up when told to, and it proves the call site
is wired to the helper rather than to a bare `replace`. It proves NOTHING about
a share-mode conflict, because the thing it substitutes for cannot fail the way
the real thing does.

So the share-mode case is driven for real, by opening the destination in this
process and writing the heartbeat anyway. That test can only run on Windows,
and it carries its own POSITIVE CONTROL: with the handle held, a raw
`os.replace` must be refused first. If the platform ever stops refusing, the
control fails loudly and the test reports that it could not measure, rather
than passing because there was nothing left to catch.

On POSIX the same scenario is asserted to SUCCEED without any retry, which is
not a second proof of the fix: it is the statement that a local green says
nothing about this defect.
"""

from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

import pytest

from straightedge import watchdog
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.atomic import replace_retrying_on_share_conflict
from straightedge.engine import HEARTBEAT_REPLACE_RETRY_SECONDS, Engine
from straightedge.synthetic import generate_bars


def _engine(tmp_path: Path) -> Engine:
    cfg = BotConfig()
    cfg.journal_path = str(tmp_path / "j.jsonl")
    cfg.session.enabled = False
    cfg.risk.halt_file = str(tmp_path / "HALT")
    broker = PaperBroker(balance=10_000)
    broker.seed_bars("EURUSD", generate_bars(120, drift=0.0004, vol=0.0002, seed=3))
    engine = Engine(cfg, broker, halt_dir=str(tmp_path))
    engine.start()
    return engine


# --- 1. the loop, which is all a raised exception can pin --------------------


def test_a_refused_replace_is_retried_until_it_lands(tmp_path: Path) -> None:
    """The LOOP, not the conflict. A raise is not a held handle."""
    dest = tmp_path / "hb"
    tmp = tmp_path / "hb.tmp"
    tmp.write_text("second", encoding="utf-8")
    dest.write_text("first", encoding="utf-8")

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
    assert dest.read_text(encoding="utf-8") == "second"


def test_the_window_is_bounded_and_then_the_tick_is_allowed_to_fail(
    tmp_path: Path,
) -> None:
    """A reader that never lets go must SURFACE, not hang the desk.

    A retry that waits forever converts a rare dropped tick into a stopped
    desk, which is the worse failure.
    """
    dest = tmp_path / "hb"
    tmp = tmp_path / "hb.tmp"
    tmp.write_text("x", encoding="utf-8")
    dest.write_text("y", encoding="utf-8")

    real = os.replace

    def always(src, dst):  # type: ignore[no-untyped-def]
        raise PermissionError(5, "Access is denied")

    os.replace = always  # type: ignore[assignment]
    started = time.monotonic()
    try:
        with pytest.raises(PermissionError):
            replace_retrying_on_share_conflict(tmp, dest, window_s=0.1)
    finally:
        os.replace = real  # type: ignore[assignment]
    elapsed = time.monotonic() - started
    assert 0.1 <= elapsed < 2.0, elapsed


def test_a_non_permission_error_propagates_on_the_first_attempt(
    tmp_path: Path,
) -> None:
    """NARROWNESS, and it is the control that matters most here.

    A bare `except OSError` would absorb a full disk, a vanished directory and
    a revoked ACL, none of which waiting can fix. If this test ever goes green
    with more than one attempt, the retry has been widened.
    """
    dest = tmp_path / "hb"
    tmp = tmp_path / "hb.tmp"
    tmp.write_text("x", encoding="utf-8")

    attempts: list[int] = []
    real = os.replace

    def boom(src, dst):  # type: ignore[no-untyped-def]
        attempts.append(1)
        raise OSError(28, "No space left on device")

    os.replace = boom  # type: ignore[assignment]
    try:
        with pytest.raises(OSError) as caught:
            replace_retrying_on_share_conflict(tmp, dest)
    finally:
        os.replace = real  # type: ignore[assignment]

    assert not isinstance(caught.value, PermissionError)
    assert len(attempts) == 1, f"a non-PermissionError was retried: {attempts}"


# --- 2. the call site, so the helper cannot sit there unwired ---------------


def test_the_heartbeat_write_uses_the_retry_and_not_a_bare_replace(
    tmp_path: Path,
) -> None:
    """A helper nothing calls is decoration. Drive it through the engine.

    One refusal, then success: if `_write_heartbeat` still called `replace`
    directly this would raise out of it and the published stamp would not
    advance.
    """
    engine = _engine(tmp_path)
    dest = watchdog.heartbeat_path_for(engine.journal.path)
    engine._write_heartbeat()
    first = dest.read_text(encoding="utf-8")

    attempts: list[int] = []
    real = os.replace

    def flaky(src, dst):  # type: ignore[no-untyped-def]
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError(5, "Access is denied")
        return real(src, dst)

    os.replace = flaky  # type: ignore[assignment]
    try:
        engine._write_heartbeat()
    finally:
        os.replace = real  # type: ignore[assignment]
    engine.stop()

    assert len(attempts) >= 2, attempts
    assert dest.read_text(encoding="utf-8") != first, (
        "the heartbeat did not advance, so the refusal was not retried"
    )


def test_the_retry_window_cannot_delay_a_tick() -> None:
    """The constant is sized by what the TICK can afford. Pin the relation.

    Stated in the constant's own comment, so it is asserted here rather than
    left to a reader to check against `poll_seconds`.
    """
    poll = BotConfig().poll_seconds
    assert HEARTBEAT_REPLACE_RETRY_SECONDS < poll / 4, (
        f"a {HEARTBEAT_REPLACE_RETRY_SECONDS}s retry window against a {poll}s "
        "tick can delay the next tick"
    )


# --- 3. the real thing, which only one platform can run --------------------


@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "UNMEASURED on this platform: a POSIX rename over an open file "
        "succeeds, so the share-mode conflict straightedge#242 is about "
        "cannot be reached here. This case runs on the windows-latest leg."
    ),
)
def test_the_heartbeat_lands_once_a_transient_reader_lets_go(
    tmp_path: Path,
) -> None:
    """The defect itself, with a REAL handle, and the handle must let go.

    THE FIRST VERSION OF THIS TEST WAS WRONG AND ONLY THE WINDOWS RUNNER COULD
    SAY SO, which is the whole reason straightedge#242 asked for this leg. It
    held the handle across the entire call, so the retry spun for its full
    window against a reader that never released and then re-raised, exactly as
    `test_the_window_is_bounded_and_then_the_tick_is_allowed_to_fail` requires
    it to. The test asserted the fix would do something the fix must NOT do.

    The defect is a transient READ racing a write, measured at one occurrence
    in two weeks against a 15 second cadence, not a reader that keeps the file.
    A retry absorbs a race; nothing can absorb a permanent hold, and a retry
    that tried would convert a rare dropped tick into a stopped desk. So the
    two cases are split and both are asserted: a hold that outlasts the window
    SURFACES (that test, driven by a raise, which is all a raise can do), and a
    hold that lets go is ABSORBED (this one, driven by a real handle).

    The holder releases only AFTER the positive control has observed a real
    refusal, so this cannot pass by the conflict never having happened, and the
    elapsed time is asserted to prove the write WAITED rather than succeeding
    on its first attempt.
    """
    engine = _engine(tmp_path)
    dest = watchdog.heartbeat_path_for(engine.journal.path)
    engine._write_heartbeat()
    first = dest.read_text(encoding="utf-8")

    holding = threading.Event()
    may_release = threading.Event()
    released = threading.Event()
    hold_s = 0.05

    def hold() -> None:
        with open(dest, "r", encoding="utf-8") as fh:
            fh.read()
            holding.set()
            # Keep the handle until the main thread has PROVEN the platform
            # refuses, then a little longer so the first retry attempt inside
            # `_write_heartbeat` is refused for real.
            may_release.wait(timeout=5.0)
            time.sleep(hold_s)
        released.set()

    reader = threading.Thread(target=hold, name="se242-holder", daemon=True)
    reader.start()
    assert holding.wait(timeout=5.0), "the holding thread never opened the file"

    # POSITIVE CONTROL: with the handle held, this platform must refuse a bare
    # replace onto the destination. If it ever stops refusing, this test can no
    # longer measure anything and says so here rather than passing quietly.
    decoy = tmp_path / "decoy.tmp"
    decoy.write_text("decoy", encoding="utf-8")
    with pytest.raises(PermissionError):
        os.replace(decoy, dest)

    may_release.set()
    started = time.monotonic()
    engine._write_heartbeat()
    elapsed = time.monotonic() - started
    reader.join(timeout=5.0)
    engine.stop()

    assert released.is_set(), "the holding thread did not finish"
    assert dest.read_text(encoding="utf-8") != first, (
        "the heartbeat did not advance after the reader let go"
    )
    assert elapsed >= hold_s / 2, (
        f"the write took {elapsed:.3f}s, which is too fast to have been "
        "refused and retried; the conflict may not have occurred"
    )
    assert elapsed < HEARTBEAT_REPLACE_RETRY_SECONDS * 4, (
        f"the write took {elapsed:.3f}s against a "
        f"{HEARTBEAT_REPLACE_RETRY_SECONDS}s window"
    )


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="this is the POSIX half of the platform statement",
)
def test_on_posix_a_held_handle_does_not_refuse_the_replace(tmp_path: Path) -> None:
    """Why a local green proves nothing about straightedge#242.

    Not a second proof of the fix. This asserts the REASON the Windows case is
    the only one that can red: here the conflict does not exist at all.
    """
    dest = tmp_path / "hb"
    dest.write_text("first", encoding="utf-8")
    tmp = tmp_path / "hb.tmp"
    tmp.write_text("second", encoding="utf-8")
    with open(dest, "r", encoding="utf-8") as holder:
        holder.read()
        os.replace(tmp, dest)
    assert dest.read_text(encoding="utf-8") == "second"
