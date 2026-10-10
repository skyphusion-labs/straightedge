"""straightedge#242: a concurrent READER of the heartbeat aborted the tick.

Found by a live read of the deployed desk, never by this suite:

    [WinError 5] Access is denied:
    'C:\\bot-state\\journal.heartbeat.tmp' -> 'C:\\bot-state\\journal.heartbeat'

One occurrence in 59 `loop_error` records; the other 58 were a different and
familiar class. On Windows `MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)` fails
with `ERROR_ACCESS_DENIED` when the destination is open in a process that did
not ask for `FILE_SHARE_DELETE`, and CPython's `open()` does not ask.

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
import time
from pathlib import Path

import pytest

from straightedge import watchdog
from straightedge.broker.paper import PaperBroker
from straightedge.config import BotConfig
from straightedge.engine import (
    HEARTBEAT_REPLACE_RETRY_SECONDS,
    Engine,
    _replace_retrying_on_share_conflict,
)
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
    real = Path.replace

    def flaky(self: Path, target):  # type: ignore[no-untyped-def]
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(5, "Access is denied")
        return real(self, target)

    Path.replace = flaky  # type: ignore[method-assign]
    try:
        _replace_retrying_on_share_conflict(tmp, dest)
    finally:
        Path.replace = real  # type: ignore[method-assign]

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

    real = Path.replace

    def always(self: Path, target):  # type: ignore[no-untyped-def]
        raise PermissionError(5, "Access is denied")

    Path.replace = always  # type: ignore[method-assign]
    started = time.monotonic()
    try:
        with pytest.raises(PermissionError):
            _replace_retrying_on_share_conflict(tmp, dest, window_s=0.1)
    finally:
        Path.replace = real  # type: ignore[method-assign]
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
    real = Path.replace

    def boom(self: Path, target):  # type: ignore[no-untyped-def]
        attempts.append(1)
        raise OSError(28, "No space left on device")

    Path.replace = boom  # type: ignore[method-assign]
    try:
        with pytest.raises(OSError) as caught:
            _replace_retrying_on_share_conflict(tmp, dest)
    finally:
        Path.replace = real  # type: ignore[method-assign]

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
    real = Path.replace

    def flaky(self: Path, target):  # type: ignore[no-untyped-def]
        attempts.append(1)
        if len(attempts) == 1:
            raise PermissionError(5, "Access is denied")
        return real(self, target)

    Path.replace = flaky  # type: ignore[method-assign]
    try:
        engine._write_heartbeat()
    finally:
        Path.replace = real  # type: ignore[method-assign]
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
def test_the_heartbeat_lands_while_a_reader_holds_the_destination(
    tmp_path: Path,
) -> None:
    """The defect itself, with a held handle rather than a raised exception.

    Carries its own positive control: the raw `os.replace` must be REFUSED
    while the handle is held. Without that, a green here could mean the
    platform no longer refuses and the test caught nothing.
    """
    engine = _engine(tmp_path)
    dest = watchdog.heartbeat_path_for(engine.journal.path)
    engine._write_heartbeat()
    first = dest.read_text(encoding="utf-8")

    with open(dest, "r", encoding="utf-8") as holder:
        holder.read()

        # POSITIVE CONTROL: this platform must still refuse a bare replace
        # onto a destination held open without FILE_SHARE_DELETE.
        decoy = tmp_path / "decoy.tmp"
        decoy.write_text("decoy", encoding="utf-8")
        with pytest.raises(PermissionError):
            os.replace(decoy, dest)

        # AND the heartbeat still publishes, which is the fix.
        time.sleep(0.01)
        engine._write_heartbeat()

    engine.stop()
    assert dest.read_text(encoding="utf-8") != first, (
        "the heartbeat did not advance while a reader held the destination"
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
