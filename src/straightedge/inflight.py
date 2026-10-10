"""The durable record that a send was ATTEMPTED, written BEFORE it is attempted.

Why this file exists. There was no trace anywhere that an order had been put on
the wire until the reply came back: `Engine._open` journals `open` only after
`broker.market()` returns. So a send whose bridge call timed out left the system
in a state indistinguishable from never having sent at all -- the staged order
still staged, the journal silent -- and the operator's natural next move was to
confirm it again. `BridgeTimeout` subclasses `RuntimeError` and `Desk.handle`
catches `RuntimeError`, so nothing even interrupted the flow. Two identical
market orders, measured at 0.55 lots each in the red drive for this change.

A TIMEOUT IS NOT EVIDENCE THE ORDER DID NOT REACH THE BROKER. That is the whole
trap, and it dictates the shape of everything here: the ledger entry is written
first and is NOT removed when the attempt fails, only when an outcome is known.
An entry that is still open is the desk saying "I do not know whether this moved
money", and nothing may read that as "it did not".

The mirror of it matters just as much: AN EMPTY BOOK IS NOT EVIDENCE EITHER. The
desk times out while the Expert is still inside `SendRetry`, so the position the
send is about to create is not on the book when the desk looks. A guard that
cleared itself on "no position found" would be a guard that fails open in exactly
the case it exists for.

Durability is the point, not a nicety. The observed failure on the live box was a
SILENT PROCESS RESTART (2026-09-26T01:56:49Z, no traceback in `desk.err`, picked
back up by the `straightedge-desk` scheduled task), and the staged order
survives a restart by design via the journal's `confirm_stage` record.

That parenthesis read "the 5 minute scheduled task" until 2026-10-08, and the
correction is worth keeping because the part it got wrong is the part that
mattered. The task does declare a five-minute repetition and it has fired ZERO
times: the repetition hangs on a LogonTrigger with `StopAtDurationEnd=true` and
no `Duration`, so its window shuts at logon, and the box shows an empty
`NextRunTime`, a `LastRunTime` twelve days old, and 8 `start` records in the
whole journal where a five-minute cadence would be thousands. What restarted the
desk at 01:56 was that task firing on LOGON, which is the one event that is not
available when a desk dies unattended. The intent recorded here was real and the
mechanism behind it was not, which is what `straightedge#133` is about. An in-memory set would be
empty in precisely the process that most needs to know. So each write is
`flush` + `fsync` + atomic `replace`.

Why not a venue-side dedupe key instead. See `docs/MT4.md`: MT4 gives a send two
carriers that come back on the book, `magic` and `comment`. `magic` is per desk
and is what ownership filtering reads, so it cannot also be per order. `comment`
CAN carry an id, and it does -- but brokers append to and overwrite
`OrderComment` (the repo already ships a test for a broker rewriting one to
`rb-1/from #123`), so the key can vanish from the book. A dedupe that looks the
key up on the book and sends again when it is absent has its failure in the
DANGEROUS direction: a rewritten comment reads as "not sent". This ledger is in
the desk's own storage, nothing on the venue can rewrite it, and its failure
direction is refusing a send. The comment key is kept as corroboration for
reconciliation, never as the guarantee.
"""

from __future__ import annotations

import json
import os
import secrets
import time
from pathlib import Path

from straightedge.atomic import replace_retrying_on_share_conflict
from typing import Any

#: Sidecar suffix, same idiom as the heartbeat and the instance lock: derived
#: from the journal path so one `journal_path` setting locates every artifact of
#: a desk and two desks cannot share one by accident.
INFLIGHT_SUFFIX = ".inflight.json"

#: Open entries kept. Far above any plausible count -- an open entry means an
#: ambiguous money event, which should be rare -- and present only so a pathology
#: cannot grow the file without bound. The OLDEST are dropped, because a recent
#: unresolved send is the one an operator can still act on.
MAX_OPEN_ENTRIES = 64

#: 8 hex characters, 32 bits. Short because it has to fit in MT4's 31 character
#: `OrderComment` alongside the operator's own comment, and that is enough:
#: a collision would make the desk REFUSE a send it had already refused-or-sent,
#: never send one twice, so the failure direction of the birthday problem here is
#: the safe one.
KEY_BYTES = 4


def new_key() -> str:
    """A fresh client order id. ASCII hex, so `_wire()` passes it unchanged."""
    return secrets.token_hex(KEY_BYTES)


def inflight_path_for(journal_path: str | Path) -> Path:
    p = Path(journal_path)
    return p.with_name(p.stem + INFLIGHT_SUFFIX)


class InflightLedger:
    """Open send attempts, by client order id. Durable across a process exit.

    Deliberately NOT a general key-value store: the only mutations are `begin`
    (an attempt is about to leave) and `resolve` (its outcome is known). There is
    no "clear because it is old" and no "clear because the book looks empty",
    because both of those are the unsafe reading of an absence.
    """

    def __init__(self, journal_path: str | Path) -> None:
        self.path = inflight_path_for(journal_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    # -- reads ------------------------------------------------------------
    def _load(self) -> dict[str, dict[str, Any]]:
        """Open entries, or an empty map. A corrupt file reads as EMPTY on
        purpose and says so through `readable()`: a ledger that raised here would
        take the desk down at startup over a file that only ever adds refusals,
        which trades a rare ambiguity for a certain outage."""
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        if not isinstance(raw, dict):
            return {}
        open_entries = raw.get("open")
        if not isinstance(open_entries, dict):
            return {}
        return {str(k): v for k, v in open_entries.items() if isinstance(v, dict)}

    def readable(self) -> bool:
        """False when the file exists and could not be parsed. Reported, never
        silently treated as "no open sends"."""
        if not self.path.exists():
            return True
        try:
            json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return True

    def get(self, key: str) -> dict[str, Any] | None:
        return self._load().get(key)

    def open_entries(self) -> dict[str, dict[str, Any]]:
        return self._load()

    # -- writes -----------------------------------------------------------
    def _store(self, entries: dict[str, dict[str, Any]]) -> None:
        if len(entries) > MAX_OPEN_ENTRIES:
            ordered = sorted(
                entries.items(), key=lambda kv: float(kv[1].get("at", 0) or 0)
            )
            entries = dict(ordered[-MAX_OPEN_ENTRIES:])
        tmp = self.path.with_name(self.path.name + ".tmp")
        payload = json.dumps({"version": 1, "open": entries}, sort_keys=True)
        with tmp.open("w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            # The failure this exists for is a process that DIES. A buffered
            # write that never reached the platter is an entry the next process
            # cannot see, which is the in-memory set this file replaces.
            os.fsync(fh.fileno())
        # NOT a bare `os.replace`: a concurrent reader of this file makes that
        # fail on Windows (straightedge#251, and the helper carries the
        # mechanism). The consequence here is NOT the halt `state.py` takes:
        # `_store` is reached from `Engine._open` and `_place_pending` through
        # `begin()`, which runs BEFORE `broker.market(order)`, and neither call
        # site wraps it. So a refused replace raises out of the send path with
        # the order NEVER SENT, which is the safe direction and is why this is
        # a retry rather than a rewrite. `PermissionError` is an `OSError` and
        # so escapes `Desk.handle`'s `(ValueError, RuntimeError)`; the loop
        # catches it one level up in `Engine.poll_telegram` and the desk
        # survives. Read in the code rather than inferred: no `_halt_reason` is
        # set on this path, and the only two halts are `state_unreadable` and
        # `state_unwritable` in `risk.py`.
        replace_retrying_on_share_conflict(tmp, self.path)
        try:
            self.path.chmod(0o600)
        except OSError:  # pragma: no cover - platform dependent
            pass

    def begin(self, key: str, **meta: Any) -> None:
        """Record an attempt that has NOT yet left. Called before the send."""
        entries = self._load()
        existing = entries.get(key)
        attempts = int((existing or {}).get("attempts", 0)) + 1
        entries[key] = {
            **(existing or {}),
            **{k: v for k, v in meta.items() if v is not None},
            "at": float((existing or {}).get("at", time.time())),
            "last_at": time.time(),
            "attempts": attempts,
        }
        self._store(entries)

    def resolve(self, key: str, outcome: str) -> None:
        """Remove an entry whose outcome is KNOWN.

        `outcome` is accepted and discarded here on purpose: the durable record
        of what happened belongs in the journal, which is append-only and is what
        an operator reads. This file is state, not history, and a resolved entry
        must leave no residue that a later read could mistake for an open one.
        """
        del outcome
        entries = self._load()
        if entries.pop(key, None) is None:
            return
        self._store(entries)


#: MT4 truncates `OrderComment` at 31 characters, so the operator's own comment
#: is clipped to leave room for the key rather than the other way round: the key
#: is what makes a position on the book attributable to one send, and a clipped
#: key attributes nothing.
COMMENT_LIMIT = 31


def stamped_comment(comment: str, key: str) -> str:
    """`<operator comment> <client order id>`, clipped to fit the venue.

    This is CORROBORATION, not the dedupe mechanism. Brokers append to and
    overwrite `OrderComment` -- this repo already ships a test for one rewriting a
    comment to `rb-1/from #123` -- so a key that is missing from the book proves
    nothing. It is here because when the key IS present, the match is definitive,
    and a definitive positive is worth a great deal during a reconcile at 22:00
    UTC on a Sunday.
    """
    if not key:
        return comment[:COMMENT_LIMIT]
    room = COMMENT_LIMIT - len(key) - 1
    if room <= 0:  # pragma: no cover - KEY_BYTES makes this unreachable
        return key[:COMMENT_LIMIT]
    head = comment[:room].rstrip()
    return f"{head} {key}".strip() if head else key
