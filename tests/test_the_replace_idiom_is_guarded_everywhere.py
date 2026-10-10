"""straightedge#251 step 3: the four remaining unguarded replace sites.

#242 guarded the heartbeat, #257 guarded the state snapshot. The census on this
issue found the idiom at eight sites, and the decision is PER SITE because they
do not share a consequence. Each one's reasoning lives at the call site; this
file is the gate.

WHY THE CENSUS IS A TEST AND NOT A ONE-OFF. The thing that keeps going wrong
with this idiom is the SCANNER, not the loop. A regex for `os\\.replace` is
blind to `tmp.replace(dest)`, which is the form most of these sites use, so the
population was undercounted twice. `replace_scan.py` reads the tree and FAILS
on anything it cannot classify; this file pins its output as exact sets, so a
ninth site cannot appear without a person looking at it.

WHY EVERY GUARD IS PROVEN BY INJECTION. The failure is `PermissionError`, which
is an `OSError`, so a blanket `except OSError` anywhere on the path swallows it
and the site looks correct. Reading the code cannot tell those apart. Every
case below makes a double REFUSE and then asserts what the caller does, and the
assertions are on OBSERVABLES: a file's contents, a journal row, a raised type.

`telegram.py` is the sharpest example of why "no exception" is not an
assertion: `TelegramClient._store_offset` already wraps its write in
`except OSError: return`, so a test that only required the call not to raise
passed before the fix existed. That one reads the file.
"""

from __future__ import annotations

import ast
import json
import os
import sys
import threading
import time
from pathlib import Path

import pytest

from straightedge import atomic
from straightedge.atomic import REPLACE_RETRY_SECONDS
from straightedge.inflight import InflightLedger
from straightedge.journal import Journal
from straightedge.telegram import _write_offset

from replace_scan import ALLOWED_BARE, scan

REPO = Path(__file__).resolve().parents[1]


# --- helpers ---------------------------------------------------------------


class Refuser:
    """An `os.replace` double that refuses the first `n` attempts.

    `n=None` refuses forever, which is the CONDITION case: a holder that
    outlasts the window. Counts attempts so a test can prove the retry
    happened rather than assuming it.
    """

    def __init__(self, n: int | None = 1, errno: int = 5, msg: str = "Access is denied"):
        self.n = n
        self.errno = errno
        self.msg = msg
        self.attempts = 0
        self._real = os.replace

    def __call__(self, src, dst):  # type: ignore[no-untyped-def]
        self.attempts += 1
        if self.n is None or self.attempts <= self.n:
            raise PermissionError(self.errno, self.msg)
        return self._real(src, dst)


class Exploder:
    """Raises a NON-PermissionError OSError, to pin the narrowness."""

    def __init__(self) -> None:
        self.attempts = 0

    def __call__(self, src, dst):  # type: ignore[no-untyped-def]
        self.attempts += 1
        raise OSError(28, "No space left on device")


def _patch(monkeypatch, double) -> None:
    monkeypatch.setattr(os, "replace", double)


# --- 1. the census ---------------------------------------------------------


def test_no_bare_filesystem_replace_survives_in_the_package() -> None:
    """The gate. Measured RED on the parent of this commit with 4 findings."""
    c = scan(REPO)
    assert c.files_scanned >= 25, f"the scanner read {c.files_scanned} files"
    assert c.findings == [], f"unguarded replace sites: {c.findings}"


def test_a_site_the_scanner_cannot_classify_is_a_finding_not_a_skip() -> None:
    """`replace` is five functions wearing one name here.

    A classifier that guesses mis-sorts in BOTH directions: a 1-positional-arg
    heuristic put `dataclasses.replace(cfg, mode="mt5")` in the filesystem
    bucket at four sites while writing this. So anything unclassifiable is
    returned for a person, and this asserts there is nothing in that bucket.
    """
    assert scan(REPO).unresolved == []


def test_the_guarded_sites_are_pinned_exactly() -> None:
    """A site that silently LOSES its guard reds here.

    Without this, `findings == []` could be satisfied by deleting a call
    entirely rather than by guarding it.
    """
    assert sorted(scan(REPO).guarded) == [
        "engine.py:_write_heartbeat",
        "inflight.py:_store",
        "journal.py:_rotate_if_needed",
        "llm.py:save",
        "state.py:save_snapshot",
        "telegram.py:_write_offset",
    ]


def test_the_two_intentional_bare_sites_are_pinned_with_their_reasons() -> None:
    """An allow-list that can grow silently is not an allow-list."""
    assert set(ALLOWED_BARE) == {
        ("atomic.py", "replace_retrying_on_share_conflict"),
        ("broker/mt4_live.py", "_atomic_write"),
    }
    assert sorted(scan(REPO).allowed_bare) == [
        "atomic.py:replace_retrying_on_share_conflict os.replace",
        "broker/mt4_live.py:_atomic_write tmp.replace(target)",
    ]


@pytest.mark.parametrize(
    "body,why",
    [
        ("import os\ndef f(a, b):\n    os.replace(a, b)\n", "os.replace"),
        ("def f(tmp, dest):\n    tmp.replace(dest)\n", "Path.replace"),
        ("import os\ndef f(a, b):\n    os.rename(a, b)\n", "os.rename"),
        ("import shutil\ndef f(a, b):\n    shutil.move(a, b)\n", "shutil.move"),
    ],
)
def test_the_census_REDS_when_a_bare_replace_is_reintroduced(
    tmp_path: Path, body: str, why: str
) -> None:
    """THE POSITIVE CONTROL. A gate nobody has driven red is decorative.

    Run against a synthetic package rather than by mutating the real tree, so
    it cannot leave the repo dirty, and parametrised over all four spellings
    because the original regex could only see one of them.
    """
    pkg = tmp_path / "src" / "straightedge"
    pkg.mkdir(parents=True)
    (pkg / "newsite.py").write_text(body, encoding="utf-8")

    c = scan(tmp_path)
    assert c.findings, f"the census did not see a bare {why}"
    assert any("newsite.py" in f for f in c.findings), c.findings


def test_the_census_does_not_flag_the_lookalikes(tmp_path: Path) -> None:
    """The other direction of the same control: no false positives.

    `str.replace`, `datetime.replace` and `dataclasses.replace` all share the
    name and none of them touches the filesystem.
    """
    pkg = tmp_path / "src" / "straightedge"
    pkg.mkdir(parents=True)
    (pkg / "lookalikes.py").write_text(
        "from dataclasses import replace\n"
        "def f(cfg, s, ts):\n"
        "    a = s.replace('x', 'y')\n"
        "    b = ts.replace(tzinfo=None)\n"
        "    c = replace(cfg, mode='mt5')\n"
        "    return a, b, c\n",
        encoding="utf-8",
    )
    c = scan(tmp_path)
    assert c.findings == [], c.findings
    assert c.unresolved == [], c.unresolved
    assert len(c.non_fs) == 3, c.non_fs


def test_the_census_keys_never_carry_a_backslash() -> None:
    """The bug the windows-latest leg caught, pinned where a POSIX run sees it.

    `str(path.relative_to(pkg))` renders `broker\\mt4_live.py` on Windows, and
    the `ALLOWED_BARE` keys are forward-slash, so the mailbox site fell out of
    the allow-list and into `findings`: 2 failed on windows-latest while every
    ubuntu leg was green, and the gate accused the one site that is bare ON
    PURPOSE.

    A POSIX run cannot reproduce that rendering, so asserting "no backslash in
    the output" here would pass vacuously. This asserts the CHOICE instead, via
    `PureWindowsPath`, which renders the same way on every platform: that the
    two spellings genuinely differ, and that the one the scanner uses is the
    stable one. The first assertion is what keeps this from being decoration.
    """
    from pathlib import PureWindowsPath

    win = PureWindowsPath("broker/mt4_live.py")
    assert "\\" in str(win), (
        "PureWindowsPath stopped rendering a backslash, so this test can no "
        "longer tell the two spellings apart"
    )
    assert win.as_posix() == "broker/mt4_live.py"

    c = scan(REPO)
    emitted = c.findings + c.allowed_bare + c.guarded + c.non_fs + c.unresolved
    assert emitted, "the scanner emitted nothing, so this measured nothing"
    # The PATH PREFIX only. A row's tail is source text and a backslash is
    # legitimate there: `str(v).replace("\\r", " ")` is one of the real
    # `non_fs` rows, and asserting on the whole row flagged it. The key is the
    # part before the first colon, which is what ALLOWED_BARE matches on.
    prefixes = [row.split(":", 1)[0] for row in emitted]
    assert all("\\" not in k for k in prefixes), [k for k in prefixes if "\\" in k]


def test_the_mailbox_spin_matches_the_shared_one() -> None:
    """`mt4_live` keeps its own loop, so the two figures must not drift.

    The comment at that site says a test pins this rather than the comment. A
    copied tool forks at copy time, and the weakest copy guards the
    least-watched path.
    """
    src = (REPO / "src" / "straightedge" / "broker" / "mt4_live.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(src)
    sleeps: list[float] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != "_atomic_write":
            continue
        for call in ast.walk(node):
            if (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "sleep"
                and call.args
                and isinstance(call.args[0], ast.Constant)
            ):
                sleeps.append(float(call.args[0].value))
    assert sleeps, "could not find the mailbox spin; this test stopped measuring"
    assert set(sleeps) == {atomic._SPIN_SECONDS}, (
        f"the mailbox spins at {sleeps} and the shared helper at "
        f"{atomic._SPIN_SECONDS}; a copied tool forked"
    )


# --- 2. journal rotation: the row must outlive the housekeeping -------------


def _journal_at_rotation(tmp_path: Path, monkeypatch) -> Journal:
    """A journal whose NEXT write rotates, reached deterministically.

    Two earlier shapes of this fixture were wrong and both wasted a run, so
    the reasoning is here rather than rediscovered.

    Writing real data up to the shipped 10 MiB threshold took the file past ten
    minutes and measured nothing: what is under test is the branch rotation
    takes, not the size that triggers it.

    Then looping `while size <= threshold` with a small threshold NEVER
    TERMINATES, because the write that crosses the threshold rotates the live
    file away and leaves an empty one behind, so the condition resets on every
    iteration. That is the oscillation, not a slow loop.

    So: seed one row while rotation is still impossible (`_rotate_if_needed`
    returns early when the live file does not exist), then drop the threshold
    so that every later write must rotate. The real comparison, the real
    replace and the real deferral all still run.
    """
    from straightedge import journal as journal_mod

    j = Journal(str(tmp_path / "j.jsonl"))
    j.write("seed", note="x")
    live = Path(j.path)
    assert live.exists(), "no live file, so the rotation branch is unreachable"
    monkeypatch.setattr(journal_mod, "_ROTATE_BYTES", 1)
    return j


def test_a_transient_refusal_still_rotates_and_marks_nothing(
    tmp_path: Path, monkeypatch
) -> None:
    j = _journal_at_rotation(tmp_path, monkeypatch)
    double = Refuser(n=2)
    _patch(monkeypatch, double)
    j.write("after_rotation", v=1)
    monkeypatch.undo()

    assert double.attempts >= 3, f"no retry happened: {double.attempts}"
    assert Path(str(j.path) + ".1").exists(), "the rotation did not land"
    rows = [json.loads(x) for x in Path(j.path).read_text().splitlines() if x.strip()]
    assert rows[-1]["event"] == "after_rotation"
    assert "rotate_deferred" not in rows[-1], rows[-1]


def test_a_refusal_THAT_OUTLASTS_THE_WINDOW_still_records_the_row(
    tmp_path: Path, monkeypatch
) -> None:
    """The decision this site needed: rotation is housekeeping, the row is the product.

    Before this, `_rotate_if_needed` raised out of `write`, `__main__` caught
    and kept looping, and the audit record was gone. On a real-money desk a
    housekeeping failure must not destroy an audit row, so the rotation is
    deferred and the row lands carrying `rotate_deferred` so the deferral is
    visible rather than absorbed.
    """
    j = _journal_at_rotation(tmp_path, monkeypatch)
    before = len(Path(j.path).read_text().splitlines())
    _patch(monkeypatch, Refuser(n=None))
    j.write("breach", detail="must survive")
    monkeypatch.undo()

    rows = [json.loads(x) for x in Path(j.path).read_text().splitlines() if x.strip()]
    assert len(rows) == before + 1, "the row was lost to a failed rotation"
    assert rows[-1]["event"] == "breach"
    assert rows[-1]["detail"] == "must survive"
    assert rows[-1]["rotate_deferred"] == 1, rows[-1]


def test_a_deferred_rotation_never_clobbers_a_callers_own_key(
    tmp_path: Path, monkeypatch
) -> None:
    """The discipline the `nonfinite` merge had to learn at this exact spot."""
    j = _journal_at_rotation(tmp_path, monkeypatch)
    _patch(monkeypatch, Refuser(n=None))
    j.write("breach", rotate_deferred="caller_said_this")
    monkeypatch.undo()

    rows = [json.loads(x) for x in Path(j.path).read_text().splitlines() if x.strip()]
    assert rows[-1]["rotate_deferred"] == "caller_said_this", rows[-1]


def test_a_non_permission_oserror_in_rotation_still_raises(
    tmp_path: Path, monkeypatch
) -> None:
    """A retry absorbs a RACE, never a CONDITION.

    A full disk is not a share conflict, and deferring on it would turn a loud
    failure into a journal that silently never rotates again.
    """
    j = _journal_at_rotation(tmp_path, monkeypatch)
    boom = Exploder()
    _patch(monkeypatch, boom)
    with pytest.raises(OSError) as err:
        j.write("should_raise", v=1)
    monkeypatch.undo()
    assert err.value.errno == 28
    assert boom.attempts == 1, f"a non-PermissionError was retried: {boom.attempts}"


# --- 2b. the deferral reaches a channel something WATCHES ------------------
#
# The row field is a RECORD and reaches nobody: the rotation it describes is
# the thing that is failing, so the log is the worst available channel for
# saying so. `Journal.rotate_deferrals` is the figure that leaves the process,
# published on the heartbeat, which is the file `straightedge-watch` reads.
#
# `breach_rows_lost` is the precedent rather than `over_budget_ever`: both are
# "journal housekeeping gave up", and both are PUBLISHED for a reader rather
# than raised as a watchdog `reason`. The last case here pins that limit so it
# is asserted instead of merely described.


def test_a_deferred_rotation_is_counted_on_the_journal(
    tmp_path: Path, monkeypatch
) -> None:
    j = _journal_at_rotation(tmp_path, monkeypatch)
    assert j.rotate_deferrals == 0, "the seed write must not have deferred"
    _patch(monkeypatch, Refuser(n=None))
    j.write("one", v=1)
    j.write("two", v=2)
    monkeypatch.undo()
    assert j.rotate_deferrals == 2, (
        f"two deferred writes counted as {j.rotate_deferrals}; a COUNT is the "
        "point, because a persistent holder defers every write"
    )


def test_a_transient_refusal_does_not_count_as_a_deferral(
    tmp_path: Path, monkeypatch
) -> None:
    """The count must mean the CONDITION, not the race.

    A retry that lands is the mechanism working. If a race incremented this,
    the figure an operator reads would be noise and the one thing it is for,
    telling a persistent holder from a passing one, would be lost.
    """
    j = _journal_at_rotation(tmp_path, monkeypatch)
    _patch(monkeypatch, Refuser(n=2))
    j.write("retried", v=1)
    monkeypatch.undo()
    assert j.rotate_deferrals == 0, (
        "a refusal the retry absorbed was counted as a deferral"
    )


def test_the_heartbeat_carries_the_deferral_count(tmp_path: Path, monkeypatch) -> None:
    """The observable: the figure is OFF this process, on the watched file.

    Asserted on the heartbeat file rather than on the attribute, because the
    attribute being right while nothing published it is precisely the gap this
    change exists to close.
    """
    from straightedge import watchdog
    from straightedge.config import BotConfig, SessionConfig, TelegramConfig
    from straightedge.broker.paper import PaperBroker
    from straightedge.engine import Engine
    from straightedge import journal as journal_mod

    cfg = BotConfig()
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "journal.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.telegram = TelegramConfig(token="t" * 10, chat_id="42")
    engine = Engine(cfg, PaperBroker(balance=10_000), halt_dir=str(tmp_path))
    engine.start()
    # `start()` does not publish one; the heartbeat is written on a tick. Asked
    # for explicitly so this test does not depend on a tick having run.
    engine._write_heartbeat()

    hb = watchdog.read(watchdog.heartbeat_path_for(cfg.journal_path))
    assert hb is not None, "no heartbeat was written, so nothing here measures"
    fields = hb.fields
    assert fields["rotate_deferrals"] == "0", (
        "published UNCONDITIONALLY: a field that appears only when something "
        "is wrong is a field no reader learns to expect"
    )

    monkeypatch.setattr(journal_mod, "_ROTATE_BYTES", 1)
    _patch(monkeypatch, Refuser(n=None))
    engine.journal.write("forced", v=1)
    monkeypatch.undo()
    engine._write_heartbeat()

    hb = watchdog.read(watchdog.heartbeat_path_for(cfg.journal_path))
    assert hb is not None
    assert hb.fields["rotate_deferrals"] == "1", hb.fields
    engine.stop()


def test_the_deferral_count_is_published_but_NOT_an_alert(tmp_path: Path) -> None:
    """The known limit, ASSERTED rather than described in a comment.

    `rotate_deferrals` is not a watchdog `reason`, so a holder that defers
    rotation forever is visible to a reader of the heartbeat and pages nobody.
    That is deliberate: choosing a threshold and an operator action is a
    watchdog design decision and is filed as straightedge#288.

    Pinned here so the limit cannot change silently in either direction. If
    somebody makes it an alert, this reds and they have to retire #288 and the
    docs along with it, instead of leaving two statements that disagree.
    """
    import inspect

    from straightedge import watchdog

    src = inspect.getsource(watchdog)
    assert "rotate_deferrals" in src, "the field is not in the watchdog at all"
    reasons = [
        line for line in src.splitlines()
        if "reason=" in line and "rotate_deferrals" in line
    ]
    assert reasons == [], (
        f"rotate_deferrals became a watchdog reason: {reasons}. That is a "
        "change to the operator alert contract; see straightedge#288."
    )


def test_a_deferred_rotation_keeps_the_worst_case_row_in_bound(
    tmp_path: Path, monkeypatch
) -> None:
    """`rotate_deferred` is a ROW field, so it lands inside what the bound measures.

    This case exists because the field was asserted by NOTHING. `Journal.write`
    sets `rec["rotate_deferred"] = 1` and re-serialises, so the field is not
    only an atomic-write change: it is a new key on a row that
    `journal.RECORD_ROW_BOUND` answers for. And it appears ONLY when a rotation
    is refused, which no fixture in the bound suite drives, so with #229 and
    this change both landed the bound suite is green with the field ABSENT and
    the larger row is measured by nobody.

    That is the same shape as the reject row measuring 5753 bytes with the
    whole suite green, which is the defect #226 and #229 exist to close, and
    the same shape as `telegram`'s "did not raise" passing before its fix
    existed. A row on a path no fixture reaches cannot have its size asserted.

    Two assertions, and the FIRST is what stops this being decoration: the
    field must actually be present. Without it this test passes on a build
    where the deferral never fires, which is exactly the hole it is here to
    close.

    The worst case is reused rather than rebuilt. `_everything_large` derives
    from `ADVICE_PROPERTIES`, so a schema field added later is driven here too
    without anyone editing this file, and a second local definition of "worst
    case" would fork from it at copy time.

    The deferral is driven by making the helper refuse rather than by the retry
    loop, because what is under test here is the ROW, not the wait. The retry
    itself is covered by the cases above, and a real refused replace is covered
    on the windows-latest leg.
    """
    from straightedge import journal as journal_mod
    from straightedge.journal import RECORD_ROW_BOUND
    from straightedge.telegram import TgCommand
    from test_the_advice_row_is_bounded import (
        _assert_every_row_in_bound,
        _claude_reply,
        _engine,
        _everything_large,
        _turn,
    )

    engine = _engine(tmp_path, _claude_reply(_everything_large()), provider="claude")
    live = Path(engine.journal.path)
    assert live.exists(), (
        "no live journal, so `_rotate_if_needed` returns early and the "
        "deferral branch is unreachable: this would measure nothing"
    )

    def _refuse(tmp, dest, **kw):  # type: ignore[no-untyped-def]
        raise PermissionError(5, "Access is denied")

    monkeypatch.setattr(journal_mod, "_ROTATE_BYTES", 1)
    monkeypatch.setattr(journal_mod, "replace_retrying_on_share_conflict", _refuse)
    engine.handle_command(TgCommand("1", 1, "/ask take a view", 1))
    monkeypatch.undo()

    row = _turn(engine)
    assert row.get("rotate_deferred") == 1, (
        "the deferral did not fire, so the bound was asserted on a row WITHOUT "
        f"the field and this case measured nothing: {sorted(row)}"
    )

    blob = json.dumps(row, sort_keys=True)
    assert len(blob) <= RECORD_ROW_BOUND, (
        f"the worst-case advice row carrying rotate_deferred is {len(blob)} "
        f"bytes against a {RECORD_ROW_BOUND} byte bound. The field costs a "
        "fixed number of bytes on every row it marks, so this is the bound's "
        "headroom being spent rather than a clipping failure: re-derive the "
        "bound rather than nudging it."
    )
    # EVERY row the turn wrote, not only `advice_turn`: with the threshold at 1
    # every write during this turn defers, so every row carries the field.
    _assert_every_row_in_bound(engine)
    engine.stop()


# --- 3. inflight: a refused write must not let the order leave -------------


def test_a_transient_refusal_lets_the_inflight_entry_land(
    tmp_path: Path, monkeypatch
) -> None:
    store = InflightLedger(tmp_path / "j.jsonl")
    store.begin("k1", symbol="EURUSD")
    double = Refuser(n=2)
    _patch(monkeypatch, double)
    store.begin("k2", symbol="GBPUSD")
    monkeypatch.undo()

    assert double.attempts >= 3, double.attempts
    assert store.get("k2") is not None, "the ledger entry was lost"
    assert store.get("k2")["symbol"] == "GBPUSD"


def test_a_hold_that_outlasts_the_window_still_raises_before_the_send(
    tmp_path: Path, monkeypatch
) -> None:
    """Fail-closed is correct here and must stay.

    `begin()` runs BEFORE `broker.market(order)`, so a raise means the order
    never leaves. Absorbing it would send an order with no durable record that
    it was attempted, which is the one thing the ledger exists to prevent.
    """
    store = InflightLedger(tmp_path / "j.jsonl")
    store.begin("k1", symbol="EURUSD")
    _patch(monkeypatch, Refuser(n=None))
    with pytest.raises(PermissionError):
        store.begin("k2", symbol="GBPUSD")
    monkeypatch.undo()


# --- 4. telegram: assert the FILE, because the caller swallows -------------


def test_a_transient_refusal_still_persists_the_offset(
    tmp_path: Path, monkeypatch
) -> None:
    """The assertion is the file's CONTENTS.

    `_store_offset` wraps this in `except OSError: return`, so "did not raise"
    was already true before the fix. Measured: with the bare replace restored,
    this reds on the file contents and would have passed on the exception.
    """
    dest = tmp_path / "offset"
    _write_offset(str(dest), 10)
    double = Refuser(n=2)
    _patch(monkeypatch, double)
    _write_offset(str(dest), 42)
    monkeypatch.undo()

    assert double.attempts >= 3, double.attempts
    assert dest.read_text(encoding="utf-8") == "42", dest.read_text()


def test_the_offset_write_still_gives_up_after_the_window(
    tmp_path: Path, monkeypatch
) -> None:
    """The caller's swallow stays the backstop; the helper must still raise."""
    dest = tmp_path / "offset"
    _write_offset(str(dest), 10)
    _patch(monkeypatch, Refuser(n=None))
    with pytest.raises(PermissionError):
        _write_offset(str(dest), 42)
    monkeypatch.undo()
    assert dest.read_text(encoding="utf-8") == "10", "a failed write changed the file"


# --- 5. llm: a reply the operator already paid for -------------------------


def test_a_transient_refusal_still_persists_the_advice_memory(
    tmp_path: Path, monkeypatch
) -> None:
    from straightedge.config import AdviceConfig
    from straightedge.llm import Advisor

    path = tmp_path / "advice.json"
    adv = Advisor(AdviceConfig(provider="grok", grok_key="x"), persist_path=path)
    adv._memory = [{"role": "user", "content": "first"}]
    adv.save()
    assert path.exists()

    adv._memory = [{"role": "user", "content": "second"}]
    double = Refuser(n=2)
    _patch(monkeypatch, double)
    adv.save()
    monkeypatch.undo()

    assert double.attempts >= 3, double.attempts
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["turns"][-1]["content"] == "second"


# --- 6. the window cannot delay the loops it protects ----------------------


def test_the_retry_window_stays_below_the_tick() -> None:
    from straightedge.config import BotConfig

    poll = BotConfig().poll_seconds
    assert REPLACE_RETRY_SECONDS < poll / 4, (
        f"a {REPLACE_RETRY_SECONDS}s window against a {poll}s tick can delay it"
    )


# --- 7. the real thing, which only one platform can run -------------------


@pytest.mark.timing
@pytest.mark.skipif(
    sys.platform != "win32",
    reason=(
        "UNMEASURED here: a POSIX rename over an open file succeeds, so the "
        "share-mode conflict straightedge#251 is about cannot be reached. This "
        "runs on the windows-latest leg."
    ),
)
def test_the_journal_row_lands_once_a_transient_reader_lets_go(
    tmp_path: Path, monkeypatch
) -> None:
    """Rotation against a REAL held handle, with its own positive control."""
    j = _journal_at_rotation(tmp_path, monkeypatch)
    live = Path(j.path)
    rotated = Path(str(live) + ".1")
    rotated.write_text("previous\n", encoding="utf-8")

    holding = threading.Event()
    may_release = threading.Event()
    hold_s = 0.05

    def hold() -> None:
        with open(rotated, "r", encoding="utf-8") as fh:
            fh.read()
            holding.set()
            may_release.wait(timeout=5.0)
            time.sleep(hold_s)

    reader = threading.Thread(target=hold, name="se251-rot-holder", daemon=True)
    reader.start()
    assert holding.wait(timeout=5.0), "the holding thread never opened the file"

    decoy = tmp_path / "decoy.tmp"
    decoy.write_text("decoy", encoding="utf-8")
    with pytest.raises(PermissionError):
        os.replace(decoy, rotated)

    may_release.set()
    j.write("after_real_conflict", v=1)
    reader.join(timeout=5.0)

    rows = [json.loads(x) for x in live.read_text().splitlines() if x.strip()]
    assert rows[-1]["event"] == "after_real_conflict"


@pytest.mark.skipif(
    sys.platform == "win32", reason="the POSIX half of the platform statement"
)
def test_on_posix_a_held_handle_does_not_refuse_the_replace(tmp_path: Path) -> None:
    """Why a local green proves nothing about straightedge#251."""
    dest = tmp_path / "d"
    dest.write_text("first", encoding="utf-8")
    tmp = tmp_path / "d.tmp"
    tmp.write_text("second", encoding="utf-8")
    with open(dest, "r", encoding="utf-8") as holder:
        holder.read()
        os.replace(tmp, dest)
    assert dest.read_text(encoding="utf-8") == "second"
