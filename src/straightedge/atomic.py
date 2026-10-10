"""One publish-a-temp-file-into-place, for every path that does it.

straightedge#251. `write tmp, chmod, replace` was an unnamed convention
repeated at seven sites in this package, and before #248 exactly ONE of them
retried a refused replace. A convention repeated seven times with no name is a
defect in itself: the next site copies whichever instance its author read.

THE MECHANISM THIS EXISTS FOR. On Windows
`MoveFileEx(..., MOVEFILE_REPLACE_EXISTING)` fails with `ERROR_ACCESS_DENIED`
when the destination is open in a process that did not ask for
`FILE_SHARE_DELETE`, and CPython's ordinary `open()` does not ask. So any
concurrent READER of the destination makes the replace fail for as long as it
holds the handle. On POSIX a rename over an open file succeeds, which is why no
amount of local testing reaches this and why the gate lives on the
`windows-latest` leg.

Observed once on the deployed desk, on the heartbeat (straightedge#242):

    [WinError 5] Access is denied:
    'C:\\bot-state\\journal.heartbeat.tmp' -> 'C:\\bot-state\\journal.heartbeat'

WHICH READER HELD THE HANDLE IS NOT MEASURED, and nothing here claims one. A
scheduled reader is the likeliest candidate on that box, but a scanner is
equally capable of it and the `.tmp` file is also a scan target. The retry is
correct whichever it was, which is the argument for it: it does not depend on
identifying the culprit.

A RETRY ABSORBS A RACE, NEVER A CONDITION. That is the whole reason the except
clause is `PermissionError` alone and never a bare `OSError`: a full disk, a
vanished directory and a revoked ACL are states that waiting cannot fix, and
absorbing them would turn a loud failure into a silent one. It is written here
because a narrow `except` with no stated reason gets widened by the next person
who meets a different `OSError`. `broker/mt4_live.py` ruled the same thing from
the other direction: a retry policy must not depend on error wording.

WHAT THIS DOES NOT COVER, said out loud so nobody reads it as covered: the
`write_text` or `fdopen` that precedes a replace can also meet a
`PermissionError` if a scanner holds the `.tmp` path open for writing. That is
unobserved at every site, so it is not handled; a retry installed on an
unobserved path is a guard nobody can drive red.

`broker/mt4_live.py` KEEPS ITS OWN LOOP ON PURPOSE. The mailbox is the
interface a customer installs against, so it is the most expensive thing here
to change, and it was already correct. Leaving it alone takes the benefit of a
shared helper without touching that interface (straightedge#251).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

#: How long to keep retrying a replace that a concurrent reader refused.
#:
#: Sized by what the CALLER can afford, not by how long a conflict lasts. A
#: reader of these files holds its handle for microseconds, so 0.5s at a 20ms
#: spin is 25 attempts and generous by orders of magnitude. What it must never
#: do is delay the loop it protects: the heartbeat and the risk snapshot are
#: both written at roughly the tick cadence, so this stays far below the
#: smallest plausible `poll_seconds`, and tests pin that relationship rather
#: than leaving it to this comment.
#:
#: A reader that holds a file for longer than this is a DIFFERENT problem, a
#: scanner pinning the path rather than a read racing a write, and that one
#: must surface rather than be absorbed in silence. Callers whose contract is
#: to fail closed on an incomplete write depend on that: see `state.py`, where
#: a write that cannot complete is COULD NOT MEASURE and the money gate halts.
REPLACE_RETRY_SECONDS = 0.5

#: The spin, matching the figure `broker/mt4_live.py` established rather than
#: inventing a second one for the same class of wait.
_SPIN_SECONDS = 0.02


def replace_retrying_on_share_conflict(
    tmp: str | Path, dest: str | Path, *, window_s: float = REPLACE_RETRY_SECONDS
) -> None:
    """`os.replace(tmp, dest)`, retried while a concurrent reader refuses it.

    Raises the `PermissionError` unchanged once `window_s` has elapsed, so a
    caller that must fail closed on an unwritable destination still does.
    Anything that is not a `PermissionError` propagates on the FIRST attempt;
    the module docstring says why, and there are tests at two call sites that
    red if that is ever widened.
    """
    deadline = time.monotonic() + window_s
    while True:
        try:
            os.replace(tmp, dest)
            return
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_SPIN_SECONDS)
