"""Durable risk state beside the journal. The gate's INPUT, never its verdict.

Restart must not hand out a new loss budget inside the same UTC day, and must
not zero the equity peak that the drawdown gate reads. This module persists the
`EquitySnapshot` that both gates recompute from.

Four rules, each load-bearing:

- The snapshot is the INPUT to the recomputation, not a cached verdict. Every
  gate still recomputes from it on every call. Nothing here makes a halt sticky.
- Three fields are load-bearing on restore: `day_key`, `day_start_equity` and
  `peak_equity`. `time`, `balance` and `equity` are written for forensics only;
  the broker re-supplies them through `RiskManager.observe()` before any gate
  reads them, so on disk they are AS OF `written_at`, not current.
- The write is atomic: temp file in the same directory, fsync, `os.replace`.
  This process can be SIGKILLed mid-write, so a partial file must never be
  reachable; the reader sees the previous snapshot or the new one.
- A read or write that fails is COULD NOT MEASURE, and is NOT a clean state. An
  absent file returns None (a genuine first start). Anything else raises, and
  the money gate fails CLOSED on it. Non-finite numbers are rejected on purpose:
  a NaN `peak_equity` would make the drawdown comparison silently false forever.
"""

from __future__ import annotations

import json
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from straightedge.models import EquitySnapshot

SNAPSHOT_VERSION = 2

#: Versions this build can still restore. A reader that rejected the version it
#: wrote yesterday would fail CLOSED on every existing install, which is a
#: self-inflicted outage rather than a safety property. Version 1 predates the
#: daily counters, so they restore as 0: an install upgrading mid-day gets its
#: full allowance once, which is the safe direction to be wrong in.
READABLE_VERSIONS = frozenset({1, 2})

_WRITE_FLAGS = os.O_WRONLY
_WRITE_FLAGS |= os.O_CREAT
_WRITE_FLAGS |= os.O_TRUNC


class StateUnreadable(RuntimeError):
    """The snapshot exists but could not be trusted. Not a clean state."""


class StateUnwritable(RuntimeError):
    """The snapshot could not be written. The next restart would lose it."""


def snapshot_path_for(journal_path: str | Path) -> Path:
    """Sidecar beside the journal: journal.jsonl -> journal.equity.json."""
    p = Path(journal_path)
    return p.with_name(p.stem + ".equity.json")


def load_snapshot(path: str | Path) -> EquitySnapshot | None:
    """Restore the snapshot. None means absent: a clean first start.

    Raises StateUnreadable for a corrupt, truncated, mistyped, non-finite or
    unknown-version file. The caller must not treat that as a clean start.
    """
    p = Path(path)
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise StateUnreadable(f"cannot read {p}: {exc}") from exc
    if not raw.strip():
        raise StateUnreadable(f"{p} is empty")
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise StateUnreadable(f"{p} is not valid json: {exc}") from exc
    if not isinstance(data, dict):
        raise StateUnreadable(f"{p} is not a json object")
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise StateUnreadable(f"{p} has no integer version")
    if version not in READABLE_VERSIONS:
        raise StateUnreadable(
            f"{p} version {version}, this build reads {sorted(READABLE_VERSIONS)}"
        )
    day = data.get("day_key")
    if not isinstance(day, str):
        raise StateUnreadable(f"{p} day_key is not a string")
    return EquitySnapshot(
        time=_req_int(p, data, "time"),
        balance=_req_float(p, data, "balance"),
        equity=_req_float(p, data, "equity"),
        peak_equity=_req_float(p, data, "peak_equity", non_negative=True),
        day_start_equity=_req_float(p, data, "day_start_equity", non_negative=True),
        day_key=day,
        trades_today=_count(p, data, "trades_today"),
        advice_turns_today=_count(p, data, "advice_turns_today"),
    )


def _count(p: Path, data: dict[str, Any], key: str) -> int:
    """A daily counter. Absent means 0, which is how a version 1 file reads.

    A present value still has to be a real non-negative int: a NaN or a
    negative would make the cap comparison silently false forever, the same
    failure `peak_equity` is already guarded against.
    """
    if key not in data:
        return 0
    v = data.get(key)
    if isinstance(v, bool) or not isinstance(v, int):
        raise StateUnreadable(f"{p} {key} is not an integer")
    if v < 0:
        raise StateUnreadable(f"{p} {key} is negative")
    return v


#: How long `save_snapshot` keeps retrying a `replace` that a concurrent READER
#: refused, before it gives up and lets the write fail (straightedge#251).
#:
#: Same figure and same reasoning as `engine.HEARTBEAT_REPLACE_RETRY_SECONDS`,
#: because it is the same situation: this file is written on essentially every
#: tick (`RiskManager.observe` persists whenever the snapshot MOVES, and a
#: rising `peak_equity` moves it), so the window must stay far below the tick
#: interval or a retry would delay the thing it protects. A reader holds this
#: file for microseconds; 0.5s at a 20ms spin is 25 attempts.
#:
#: THE DUPLICATED NUMBER IS DELIBERATE AND TEMPORARY. Five sites in this
#: package publish write-tmp-then-replace and straightedge#251 unifies them
#: behind one named helper in a SECOND commit, so that the behaviour change
#: here and the refactor there can be reviewed separately. Until then this
#: constant exists rather than importing from `engine`, because `state` is
#: below `engine` and must not depend on it.
STATE_REPLACE_RETRY_SECONDS = 0.5

#: The spin, matching the figure `broker/mt4_live.py` established.
_STATE_REPLACE_SPIN_SECONDS = 0.02


def _replace_retrying_on_share_conflict(
    tmp: Path, dest: Path, *, window_s: float = STATE_REPLACE_RETRY_SECONDS
) -> None:
    """`os.replace(tmp, dest)`, retried while a concurrent reader refuses it.

    straightedge#251, found while fixing straightedge#242 and reached by the
    same mechanism: on Windows `MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)`
    fails with `ERROR_ACCESS_DENIED` when the destination is open in a process
    that did not ask for `FILE_SHARE_DELETE`, and CPython's `open()` does not
    ask. On POSIX a rename over an open file succeeds, so no local run reaches
    this.

    WHY IT MATTERS MORE HERE THAN IT DID FOR THE HEARTBEAT. A refused write
    raises `StateUnwritable`, which `RiskManager._persist_state` turns into
    `_halted = True` with `_halt_reason = "state_unwritable"`, and a halted
    desk returns from `step_all` BEFORE `_resolve_pending`, `_check_stops` and
    `_manage_open`. So a reader holding this file for one read stops the desk
    from managing its own positions. It is bounded to an availability defect
    and not a money defect by one fact, which is worth stating here because
    the next reader will ask: an opening order carries `sl` and `tp` to the
    VENUE, so protective stops survive a halted desk. Trailing and scale-outs
    do not.

    A HOLD THAT OUTLASTS THE WINDOW STILL HALTS, and that is correct, not a
    gap. The module's contract is that a write it cannot complete is COULD NOT
    MEASURE and the money gate fails closed on it. This retry absorbs a RACE,
    never a CONDITION: a full disk, a vanished directory and a revoked ACL are
    states that waiting cannot fix, which is why the clause is
    `PermissionError` only and never a bare `OSError`, and why there is a test
    that drives a non-`PermissionError` through here and requires it to
    propagate on the FIRST attempt.
    """
    deadline = time.monotonic() + window_s
    while True:
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_STATE_REPLACE_SPIN_SECONDS)


def save_snapshot(path: str | Path, snap: EquitySnapshot) -> Path:
    """Atomically replace the snapshot. Raises StateUnwritable on any OSError."""
    p = Path(path)
    tmp = p.with_name(p.name + ".tmp")
    payload = {
        "version": SNAPSHOT_VERSION,
        "written_at": datetime.now(timezone.utc).isoformat(),
        "time": int(snap.time),
        "balance": float(snap.balance),
        "equity": float(snap.equity),
        "peak_equity": float(snap.peak_equity),
        "day_start_equity": float(snap.day_start_equity),
        "day_key": str(snap.day_key),
        "trades_today": int(snap.trades_today),
        "advice_turns_today": int(snap.advice_turns_today),
    }
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(tmp, _WRITE_FLAGS, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, allow_nan=False)
                fh.write("\n")
                fh.flush()
                os.fsync(fh.fileno())
            # NOT a bare `os.replace`: a concurrent reader of this file makes
            # that fail on Windows, and a failed write here HALTS the desk
            # (straightedge#251). The helper says how narrowly it retries.
            _replace_retrying_on_share_conflict(tmp, p)
        except BaseException:
            # Covers a failed serialise AND a failed replace. Leaving the temp
            # file behind would leak a 0600 file and litter the journal dir.
            _unlink_quiet(tmp)
            raise
        os.chmod(p, 0o600)
    except (OSError, ValueError) as exc:
        raise StateUnwritable(f"cannot write {p}: {exc}") from exc
    return p


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        return


def _number(p: Path, data: dict[str, Any], key: str) -> float:
    v = data.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise StateUnreadable(f"{p} {key} is not a number")
    if not math.isfinite(float(v)):
        raise StateUnreadable(f"{p} {key} is not finite")
    return float(v)


def _req_float(
    p: Path, data: dict[str, Any], key: str, *, non_negative: bool = False
) -> float:
    v = _number(p, data, key)
    if non_negative and v < 0:
        raise StateUnreadable(f"{p} {key} is negative")
    return v


def _req_int(p: Path, data: dict[str, Any], key: str) -> int:
    return int(_number(p, data, key))
