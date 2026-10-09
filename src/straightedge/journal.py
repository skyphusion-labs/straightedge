"""Append-only JSONL audit log. Every risk reject and every fill goes here.

Rotate the live file to <name>.1 (replacing any previous .1) before a
write that would exceed 10 MiB. The new live file is chmod 0600.
tail() and last_event() (confirm restore) read only the live file.
Rotated history is <name>.1.
"""

from __future__ import annotations

import json
import os
import re
import sys
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, TextIO

# `mailbox_token` is spelled out because the match is exact-key, not substring:
# "token" alone does not redact a field called "mailbox_token". Nothing journals
# it today; it is listed so that adding such a field cannot leak one silently.
#
# `login` is the broker account number and it is the one entry here that is NOT
# a credential: the password and the server are separate fields and neither is
# journaled, so a leaked login grants nothing (straightedge#90). It is in the
# set anyway, for two reasons. `SECURITY.md` lists `MT5_LOGIN` under "Secret
# names", so the code and the document have to agree on something; and the
# `start` row is rendered field-by-field into the chat by
# `engine.history_text` (`/history`, and the daily `recap`) and shipped to an
# external advice provider by `engine.advice_history`, which makes an account
# identifier that nobody needs to read a thing in front of a demo audience.
# What the journal loses by this is nothing an audit reads the row for: `mode`,
# `equity`, `server` and `symbols` all survive.
_SECRET_KEYS = frozenset(
    {"token", "password", "api_key", "grok_key", "claude_key", "mailbox_token", "login"}
)
_REDACTED = "[REDACTED]"
# BotFather tokens: <id>:<secret> with 8-12 digit id and 30+ url-safe chars.
_TG_TOKEN_RE = re.compile(r"\d{8,12}:[A-Za-z0-9_-]{30,}")
# Anthropic keys: sk-ant-<...>. xAI keys: xai-<...>. Both providers are BYOK
# straight from the operator's chat (advice turns persist to journal.advice.json
# and are replayed into every following provider call via Advisor._memory), so
# a pasted key with no named field to redact by must be caught by pattern.
_ANTHROPIC_KEY_RE = re.compile(r"sk-ant-[A-Za-z0-9_-]{16,}")
_XAI_KEY_RE = re.compile(r"xai-[A-Za-z0-9_-]{16,}")
_SECRET_PATTERNS = (_TG_TOKEN_RE, _ANTHROPIC_KEY_RE, _XAI_KEY_RE)
_ROTATE_BYTES = 10 * 1024 * 1024


class Journal:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.exists():
            _chmod600(self.path)

    def write(self, event: str, **fields: Any) -> None:
        rec = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "event": event,
            **{k: _jsonable(v) for k, v in fields.items()},
        }
        rec = redact(rec)
        line = json.dumps(rec, default=str) + "\n"
        self._rotate_if_needed(len(line.encode("utf-8")))
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
        _chmod600(self.path)

    def _rotate_if_needed(self, incoming: int) -> None:
        if not self.path.exists():
            return
        size = self.path.stat().st_size
        if size + incoming <= _ROTATE_BYTES:
            return
        dest = self.path.with_name(self.path.name + ".1")
        self.path.replace(dest)
        self.path.touch()
        _chmod600(self.path)

    def tail(self, n: int = 20) -> list[dict[str, Any]]:
        """Last n live records, redacted AGAIN on the way out.

        Redacting on write alone would only protect rows this build wrote. An
        append-only audit log is never rewritten, so every row already on disk
        would keep whatever the build that wrote it did not redact, and
        `tail()` is what feeds the two paths that show a row to somebody:
        `engine.history_text` (`/history` and the daily `recap`, both into the
        chat) and `engine.advice_history` (the `history` field of the advice
        request, so off the box entirely). straightedge#90: the live journal
        held 8 `start` rows with an unredacted `login` when that was found, and
        no write-side fix reaches them.

        `last_event()` deliberately does NOT do this. It restores a pending
        order rather than showing anything to anyone, and a redacted value
        there would be a wrong value in a real-money path.
        """
        if n <= 0 or not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as fh:
            lines = deque(fh, maxlen=n)
        out: list[dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(rec, dict):
                out.append(redact(rec))
        return out

    #: The events that mark a SESSION, and therefore a day the desk was alive.
    SESSION_EVENTS = ("start", "stop")

    def last_session_day_before(self, day: str) -> str:
        """The most recent day the desk was ALIVE, strictly before `day`.

        Durable evidence for `Engine._owed_recap_day`, and the thing no
        in-memory or snapshot value can be: the equity snapshot's `day_key`
        ROLLS, and memory dies with the process (straightedge#129, and the
        review of #198 that measured the window that leaves).

        IT READS THE ROW'S OWN `day` FIELD, not `ts`, and the difference is
        load-bearing. `ts` is stamped by `write()` from the wall clock, while
        every gate in this desk runs on the engine's injected clock; #182 is
        the whole lesson about conflating two clocks. The `start` and `stop`
        rows therefore carry the engine's own `day`, and that is what this
        reads. `ts` is the fallback for rows written before that field
        existed, which keeps an upgraded install from losing one boundary on
        its first boot, and is correct there because the live desk's engine
        clock IS the wall clock.

        Scoped to SESSION_EVENTS on purpose: a `recap` row also carries `day`,
        and counting it as evidence would make the announcement its own
        justification.

        It reads the rotated file too, because a 10MB rotation between two
        boots would otherwise hide yesterday and the day would be silently
        missed. The recap marker is read from the current file only, so after a
        rotation the surviving failure is one DUPLICATE message, never a miss.
        """
        if not day:
            return ""
        wanted = set(self.SESSION_EVENTS)
        best = ""
        for path in (self.path, self.path.with_name(self.path.name + ".1")):
            if not path.exists():
                continue
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(rec, dict) or rec.get("event") not in wanted:
                        continue
                    stamp = str(rec.get("day") or rec.get("ts") or "")[:10]
                    if len(stamp) == 10 and best < stamp < day:
                        best = stamp
        return best

    def last_event(self, *names: str) -> dict[str, Any] | None:
        """Last record whose event is one of names. Full scan; start is rare."""
        if not names or not self.path.exists():
            return None
        wanted = set(names)
        found: dict[str, Any] | None = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(rec, dict) and rec.get("event") in wanted:
                    found = rec
        return found


class InstanceLockError(RuntimeError):
    """Another process already holds this journal's run lock."""


#: The size a single journal row must stay under.
#:
#: Stated here and in `docs/CONTRACT.md` rather than only inside a test, which
#: is what it was until straightedge#226: a bound that lives in one assertion
#: is a number nobody can check a change against, and two separate suites had
#: already hardcoded it. Tests import this, so the documented figure and the
#: asserted figure cannot drift apart.
#:
#: 512 is not arithmetic, it is the observed size of a healthy row plus room:
#: an ordinary `advice_turn` measures 196 bytes and the widest `recap` about
#: 300. #119 is why a ceiling exists at all, where one row stored a rendering
#: of other rows and the payload compounded daily.
RECORD_ROW_BOUND = 512

#: How much of a MODEL-CHOSEN string a row may carry.
#:
#: Long enough that every real instrument name, vendor suffix and all, survives
#: whole (`EURUSD`, `EURUSDm`, `EURUSD.a`, `XAUUSD`, `BTCUSD`), and short enough
#: that no number of such fields can push a row past the bound
#: `docs/CONTRACT.md` states (straightedge#226).
RECORD_STRING_CHARS = 48


def clip_for_record(value: str, limit: int = RECORD_STRING_CHARS) -> str:
    """Bound a model-chosen string for the durable record, and SAY it was cut.

    straightedge#226. `advice_turn` wrote `symbol` verbatim, and `symbol` is
    model-chosen on the advice path, which is the premise of #197: a 5600
    character symbol produced a 5838 byte row against the 512 byte bound this
    repo documents, and the bounded-row test passed anyway because it varied
    fields that are not echoed. #216 bounded the REASON and this is the same
    exposure one field over.

    The marker is not decoration. A silently truncated value reads as the whole
    value, so a reader cannot tell `EURUSD` from a 5600 character string that
    starts with it, and that is the defect class rather than a nicety: the
    length is stated so the row says what it dropped.

    Short values are returned unchanged, so nothing that fits is reshaped.
    """
    if len(value) <= limit:
        return value
    return f"{value[:limit]}[+{len(value) - limit} chars]"


def lock_path_for(journal_path: str | Path) -> Path:
    p = Path(journal_path)
    return p.with_name(p.stem + ".lock")


class InstanceLock:
    """Exclusive lock next to the journal. Released on close or crash.

    Unix: flock. Windows: msvcrt.locking. Same file, same fail.
    """

    def __init__(self, journal_path: str | Path) -> None:
        self.path = lock_path_for(journal_path)
        self._fh: TextIO | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fh = self.path.open("a+", encoding="utf-8")
        _chmod600(self.path)
        try:
            _lock_nb(fh)
        except OSError as exc:
            fh.close()
            raise InstanceLockError(
                f"already running: another process holds {self.path} "
                "(two run --loop cannot share journal/offset)"
            ) from exc
        self._fh = fh

    def release(self) -> None:
        fh = self._fh
        self._fh = None
        if fh is None:
            return
        try:
            _unlock(fh)
        finally:
            fh.close()

    def __enter__(self) -> InstanceLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


def redact_text(s: str) -> str:
    """Replace BotFather tokens and provider API keys in free text
    (stderr, Telegram echoes, and advice turns before they are persisted
    or replayed to the provider on the next call)."""
    for pattern in _SECRET_PATTERNS:
        s = pattern.sub(_REDACTED, s)
    return s


def mask_account_id(value: Any) -> str:
    """A broker login for the OPERATOR'S OWN console, masked to the last four.

    The deliberate call straightedge#90 asked for. `doctor --connect` exists to
    answer one question -- is the terminal attached to the account I think it
    is -- and `[REDACTED]` does not answer it, so printing nothing would break
    the diagnostic to protect an identifier that is not a credential. The last
    four answer it for the one person who already knows the number, and a
    screen share, a screenshot or a `doctor` output pasted into an issue no
    longer carries it. That is why this path masks while the journal and the
    chat redact outright: the console has a reader with a question, and those
    two have no reader who needs the answer.

    The prefix is a fixed `***` rather than one star per hidden digit, because
    the number of stars would publish the login's length for no benefit.
    """
    s = "" if value is None else str(value)
    if len(s) < 6:
        # A real MT4/MT5 login is six digits or more. Below that the last four
        # leave too little hidden to be a mask at all, so show nothing rather
        # than nearly all of it.
        return _REDACTED
    return "***" + s[-4:]


def redact(v: Any) -> Any:
    """Strip secret-named keys and BotFather tokens from logs and chat."""
    if isinstance(v, dict):
        out: dict[str, Any] = {}
        for k, val in v.items():
            if str(k).lower() in _SECRET_KEYS:
                out[k] = _REDACTED
            else:
                out[k] = redact(val)
        return out
    if isinstance(v, (list, tuple)):
        return [redact(x) for x in v]
    if isinstance(v, str):
        return redact_text(v)
    return v


def _chmod600(path: Path) -> None:
    try:
        os.chmod(path, 0o600)
    except OSError:
        return


def _lock_nb(fh: TextIO) -> None:
    """Non-blocking exclusive lock. Raises OSError if held."""
    fd = fh.fileno()
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        if fh.read(1) == "":
            fh.write("0")
            fh.flush()
        fh.seek(0)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(fh: TextIO) -> None:
    fd = fh.fileno()
    if sys.platform == "win32":
        import msvcrt

        fh.seek(0)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        return
    import fcntl

    fcntl.flock(fd, fcntl.LOCK_UN)


def _jsonable(v: Any) -> Any:
    if hasattr(v, "__dataclass_fields__"):
        from dataclasses import asdict

        out = asdict(v)
        for k, val in list(out.items()):
            if hasattr(val, "value"):
                out[k] = val.value
        return out
    if hasattr(v, "value"):
        return v.value
    return v
