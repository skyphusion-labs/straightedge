"""What is running, published by the thing that put it there.

Why this exists. The live box ran twelve days at 25 commits behind `main` and
nobody could tell. `__version__` is printed by `doctor` and by nothing else;
`Engine.status_text` carried halt state, mode, server, equity, balance, peak,
positions and risk, and no version at all. So from the locked chat there was no
reading that distinguished a current desk from a stale one, and the only signal
that eventually surfaced the staleness was a person logging into the box.

Why the DEPLOY writes it rather than the desk deriving it. The obvious
alternative is for the desk to shell out to `git` or read `.git/HEAD`, and both
are worse:

  - `git` on PATH becomes a runtime dependency of a real-money desk, for a
    diagnostic. A desk that fails to start because `git describe` moved is a new
    failure mode bought with nothing.
  - `.git/HEAD` couples the desk to a git layout. The box happens to be a
    checkout today; `docs/DEPLOY.md` does not promise it always will be, and a
    deploy that unpacked an archive would leave the desk confidently reporting
    a commit from whatever tree it was last in.

The deploy is the only actor that knows what it deployed, so it is the actor
that records it. The desk reads a file.

UNSTAMPED IS A STATE, NOT A GAP. A desk with no stamp publishes
`deployed=unstamped` and says so in `/status`. It never guesses, never falls
back to `__version__`, and is never silent, because silence here is what twelve
days of staleness looked like. It deliberately does NOT raise a watchdog note:
staleness of the stamp has no bearing on whether the desk is ticking, and an
alarm that fires on every observation of a developer's machine is an alarm that
gets muted. The field stating its own absence is the whole mechanism.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

#: Sidecar suffix, same idiom as the heartbeat, the lock and the inflight
#: ledger: derived from the journal path so one `journal_path` setting locates
#: every artifact of a desk.
DEPLOY_STAMP_SUFFIX = ".deployed.json"

#: What `describe` returns when there is no stamp, or one that cannot be read.
#: A single word on purpose: it goes into a `key=value` heartbeat line and into
#: a chat message, and both are read by people scanning rather than parsing.
UNSTAMPED = "unstamped"


def stamp_path_for(journal_path: str | Path) -> Path:
    p = Path(journal_path)
    return p.with_name(p.stem + DEPLOY_STAMP_SUFFIX)


@dataclass(frozen=True)
class DeployStamp:
    """What the deploy recorded. Every field is a string it wrote verbatim."""

    #: What the operator ASKED for: a tag, or a branch name, or an OID.
    ref: str
    #: The full 40 character OID the ref resolved to. This is the precise
    #: answer; `ref` is the one a human can say out loud. They are both kept
    #: because a tag spanning eighteen changelog sections cannot name a tree.
    commit: str
    #: When the deploy ran, ISO 8601, as the deploy saw the clock.
    at: str

    def one_line(self) -> str:
        """`<ref> <full oid>`, or just the oid when no ref was recorded.

        The FULL oid, not an abbreviation. This string's only job is to answer
        "exactly what is running", and an abbreviated commit is a different
        kind of answer: it is almost always unambiguous, which is not the same
        as being the identifier a person can paste into `git show`.
        """
        if self.ref and self.ref != self.commit:
            return f"{self.ref} {self.commit}"
        return self.commit


def read(journal_path: str | Path) -> DeployStamp | None:
    """The stamp, or None when there is not a readable one.

    A malformed stamp returns None rather than raising, and that is the same
    call `InflightLedger._load` makes for the same reason: this is a
    diagnostic, and a desk that refuses to start over an unparseable
    diagnostic trades a rare ambiguity for a certain outage. The cost is that
    "no stamp" and "corrupt stamp" both read as `unstamped`, which is
    acceptable because the remedy is identical: run a real deploy.
    """
    dest = stamp_path_for(journal_path)
    try:
        raw = json.loads(dest.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    commit = str(raw.get("commit", "")).strip()
    if not commit:
        # A stamp with no commit is not a stamp. Refusing it here is what stops
        # `deployed=` from carrying a ref that names no tree, which would be
        # worse than `unstamped`: it would look like an answer.
        return None
    return DeployStamp(
        ref=str(raw.get("ref", "")).strip(),
        commit=commit,
        at=str(raw.get("at", "")).strip(),
    )


def describe(journal_path: str | Path) -> str:
    """One line naming what is running, for the heartbeat and for `/status`."""
    stamp = read(journal_path)
    return UNSTAMPED if stamp is None else stamp.one_line()
