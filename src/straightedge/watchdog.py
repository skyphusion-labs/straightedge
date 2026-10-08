"""Surface the heartbeat: is the desk ticking, and will it trade.

`journal.heartbeat` has been written on every successful `step_all` since 1.0.0
and `docs/RUNBOOK.md` has called it the watchdog target in two places, but
NOTHING in this repo ever read it. A file nobody reads is not a watchdog, it is
a log line with an official-sounding name; a desk that dies on Tuesday is
discovered on Friday, and every indicator an operator can see reads healthy in
the meantime. This module is the reader.

The three states, and why two of them are not one state
------------------------------------------------------
Live arming is PER PROCESS on purpose. `Desk.restore_from_journal` refuses to
re-arm from a `live_on` record, and fc34 forbids `--i-accept-risk` in a
supervisor, a batch file or a scheduled task, so a restart brings the desk back
ALIVE and DISARMED. That is correct behaviour and this module does not touch it.
It does mean the operator-visible state space is three states and not two:

  1. no completed tick (the process is gone, or its ticks stopped reaching the
     account). `STALE`.
  2. ticking, and something refuses to trade. `ALIVE NOT TRADING`, with the
     gate's own named reason: `live_not_accepted` after a restart, and also
     `halt_file`, `daily_loss`, `max_drawdown`, `trade_not_allowed`,
     `state_unreadable` or `state_unwritable`.
  3. ticking and the circuit is clear. `ALIVE ARMED`.

A watchdog that reports up-or-down calls state 2 healthy, and state 2 is "your
bot silently stopped trading". Distinguishing it is the whole point, so the
reason is never flattened into a boolean: `blocked=` in the heartbeat carries
the string `RiskManager.circuit_reason` itself returned, from the same call the
send gate uses. The desk does not recompute its own arming for this file; a
second copy of a gate is a slower copy that can disagree with the first.

Named blind spot: STALE cannot tell a dead process from a stalled one
---------------------------------------------------------------------
The heartbeat means "a tick reached the account", so a desk whose process is
alive but whose venue link is down writes nothing, exactly like a desk that
exited. Proving which one it is needs the run lock, and taking that lock is
refused here: a watchdog that can hold `journal.lock` for even a moment can
make a restarting desk exit 2 with `already running`, which is a watchdog that
can kill the desk. So the state is called STALE, never DEAD, and the alert
names both readings plus the artifact that separates them after the fact
(`reconnect` records in `journal.jsonl`).

The threshold is derived, never chosen
--------------------------------------
`risk.symbol_deviation_points` (#68) is what an unmeasured constant costs: 20
points is generous slippage on EURUSD and was less than half of gold's spread,
so orders were silently rejected. A hardcoded "alarm after 5 minutes" is the
same defect on the time axis, so every term here comes from the code or the
config that sets it:

  telegram_poll_ceiling_seconds
      `Engine.poll_telegram` is the first thing in every tick and it is the
      tick's own wait: `poll_commands` calls `_post` with a transport timeout of
      `poll_seconds + POLL_TIMEOUT_MARGIN_S`, retried `RETRY_TRIES` times, and
      Telegram's own `retry_after` can make each of the gaps between those
      attempts as long as `RETRY_CAP_S`. All three numbers live in
      `straightedge.telegram` and are imported, not copied.
  venue_prefix_seconds
      `step_all` then calls `ensure_connected()` and `account()`, two commands
      on the venue's per-command budget, which is `timeout_ms` in the config
      section NAMED AFTER the mode, plus `NET_GRACE_SEC` when `mailbox_url`
      points the MT4 transport at a remote shim.

`stale_after_seconds` is `UNBOUNDED_TAIL_ALLOWANCE` times that budget, and the
factor is the one thing here that is not a measurement, so it is stated rather
than buried: the work AFTER `account()` (pending fills, stop checks, the trail,
the per-symbol auto scan) scales with the book and the symbol list and is
bounded by no config value at all, so it cannot be predicted from a config file.
The allowance is therefore checked against reality instead of trusted: the desk
also publishes `tick_gap_max_s`, the largest gap between two heartbeats it has
actually observed in this process, and sets `over_budget=1` when that exceeds
the derived budget. The desk deliberately does NOT widen its own threshold when
that happens. A gate that relaxes itself until it stops firing is a gate that
can no longer go red; it reports the breach and keeps the derived number.

Send-only, and it never fights the desk for updates
---------------------------------------------------
Two processes calling `getUpdates` on one bot token steal each other's
commands, which is why `docs/RUNBOOK.md` forbids two loops. The watcher
therefore never polls: it constructs its Telegram client without an offset path
and calls `send` only. `tests/test_watchdog.py` asserts that a whole watch run
issues zero `getUpdates` requests, because "I did not mean to poll" is not a
control.
"""

from __future__ import annotations

import math
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from straightedge.broker.mt4_net import NET_GRACE_SEC
from straightedge.telegram import POLL_TIMEOUT_MARGIN_S, RETRY_CAP_S, RETRY_TRIES

#: Named so the desk and the watcher cannot disagree about where the file is.
HEARTBEAT_SUFFIX = ".heartbeat"

#: Bytes of process identity. 4 is 32 bits, and the only question asked of a
#: run_id is "is this the same string as last time", over a handful of
#: observations minutes apart. A collision would mean one restart went
#: unreported; the staleness path still covers a desk that is actually down.
RUN_ID_BYTES = 4

#: `step_all` reaches `_write_heartbeat` through `ensure_connected()` then
#: `account()`. Two venue commands, each on the per-command budget.
VENUE_CALLS_BEFORE_HEARTBEAT = 2

#: See the module docstring. This is the allowance for the part of a tick no
#: config value bounds, and `over_budget` is how it stays honest.
UNBOUNDED_TAIL_ALLOWANCE = 2

STATE_ARMED = "ALIVE ARMED"
STATE_NOT_TRADING = "ALIVE NOT TRADING"
STATE_STALE = "STALE"
STATE_UNKNOWN = "UNKNOWN"

#: Printed where a run_id should be. A literal in the message would read as a
#: value the desk published.
_UNKNOWN_RUN = "unknown"

#: Exit codes. Distinct per state on purpose: a scheduled task, a batch file or
#: a human can branch on the state without parsing prose, and collapsing
#: "ticking but disarmed" into either 0 or 1 is the conflation this module
#: exists to remove.
EXIT_ARMED = 0
EXIT_NOT_TRADING = 3
EXIT_STALE = 4
EXIT_UNKNOWN = 5


def new_run_id() -> str:
    """One identity per desk process.

    Not the PID. A PID is recycled by the operating system, so two different
    desks can carry the same one and a restart would read as continuity. Not
    the start timestamp either: a restart inside the clock's own resolution
    would collapse, and the comparison has to be exact to be worth making.
    """
    return secrets.token_hex(RUN_ID_BYTES)


def heartbeat_path_for(journal_path: str | Path) -> Path:
    p = Path(journal_path)
    return p.with_name(p.stem + HEARTBEAT_SUFFIX)


def telegram_poll_ceiling_seconds(cfg: Any) -> float:
    """Longest `poll_telegram` may legitimately take, from its own constants."""
    if not getattr(getattr(cfg, "telegram", None), "enabled", False):
        return 0.0
    poll = max(0, int(getattr(cfg, "poll_seconds", 0) or 0))
    attempts = RETRY_TRIES * (poll + POLL_TIMEOUT_MARGIN_S)
    gaps = (RETRY_TRIES - 1) * RETRY_CAP_S
    return float(attempts + gaps)


def venue_timeout_seconds(cfg: Any) -> float:
    """The per-command budget of the venue THIS config will talk to.

    The section is fetched by the mode's own name rather than matched against
    mode literals, because each venue config section is named after its mode and
    another copy of the venue mode set in `src/` is another place to forget one.
    What that costs is that a renamed config attribute would silently read as
    zero, so `tests/test_watchdog.py` pins both venues' derived budgets against
    the real `BotConfig` instead of trusting the getattr.

    It reads `timeout_ms`, the READ budget, and NOT `mt4.send_timeout_ms`. That is
    a decision, not an oversight: the two commands `step_all` spends before it can
    write a heartbeat are `ensure_connected` and `account`, and both are reads. A
    send lives in the part of a tick no config value bounds, which is what
    `UNBOUNDED_TAIL_ALLOWANCE` already doubles the budget for; the split moved that
    tail by about 1.9s against a 214s MT4 budget, under 1%. Deriving the alarm from
    the send budget instead would widen the staleness threshold an operator was
    promised in `doctor` and in the runbook for a cost the allowance already
    covers. `tests/test_watchdog.py` pins this choice so it cannot be switched
    quietly.
    """
    section = getattr(cfg, str(getattr(cfg, "mode", "") or ""), None)
    per_call = max(0.0, float(getattr(section, "timeout_ms", 0) or 0) / 1000.0)
    if getattr(section, "mailbox_url", ""):
        # `HttpBridge` sets its own HTTP timeout to the command budget plus this
        # grace, so the shim is the end that gives up first.
        per_call += NET_GRACE_SEC
    return per_call


def tick_budget_seconds(cfg: Any) -> int:
    """Bounded part of one tick: the Telegram wait plus the two venue commands."""
    total = telegram_poll_ceiling_seconds(cfg) + (
        VENUE_CALLS_BEFORE_HEARTBEAT * venue_timeout_seconds(cfg)
    )
    return max(1, int(math.ceil(total)))


def stale_after_seconds(cfg: Any) -> int:
    """How old a heartbeat has to be before the desk is not ticking."""
    return max(1, UNBOUNDED_TAIL_ALLOWANCE * tick_budget_seconds(cfg))


def render(
    ts: datetime,
    *,
    blocked: str,
    mode: str,
    stale_after_s: int,
    tick_budget_s: int,
    tick_gap_max_s: float,
    run_id: str,
    started_at: str,
) -> str:
    """The heartbeat file's whole content.

    LINE ONE STAYS THE BARE ISO TIMESTAMP it has been since 1.0.0. Every doc
    and every reader that predates this module keeps working, and the fields
    that follow are `key=value`, one per line, in the same shape as the MT4
    mailbox wire. `docs/CONTRACT.md` carries the format.

    `run_id` is what makes a restart visible, and it is here rather than a PID
    because a PID is recycled and a restart has to be unambiguous. The gap it
    closes: a supervised restart only announced itself through the arming state
    going to `live_not_accepted`, which is a change this file can show ONLY on a
    real-money desk. On the demo account (`trade_mode=0`) no arming is needed,
    so `blocked` was empty before the crash and empty after it, every other
    field read the same, and the restart was invisible in the one configuration
    the end user is being shown. A crash loop was therefore silent exactly where
    it was most likely to be watched.
    """
    over = 1 if tick_gap_max_s > tick_budget_s else 0
    lines = [
        ts.isoformat(),
        f"blocked={blocked}",
        f"mode={mode}",
        f"stale_after_s={int(stale_after_s)}",
        f"tick_budget_s={int(tick_budget_s)}",
        f"tick_gap_max_s={tick_gap_max_s:.1f}",
        f"over_budget={over}",
        f"run_id={run_id}",
        f"started_at={started_at}",
    ]
    return "\n".join(lines) + "\n"


@dataclass(frozen=True)
class Heartbeat:
    """What the file said, with no verdict attached."""

    path: Path
    ts: datetime | None
    #: Never None: `read` returns None entirely when the file cannot be stat'd,
    #: so an optional here would be a branch nothing can ever take.
    mtime: float
    fields: dict[str, str]


def read(path: str | Path) -> Heartbeat | None:
    """Parse the file, or None when there is no file to parse."""
    dest = Path(path)
    try:
        raw = dest.read_text(encoding="utf-8")
        mtime = dest.stat().st_mtime
    except OSError:
        return None
    lines = [line for line in raw.splitlines() if line.strip()]
    ts: datetime | None = None
    if lines:
        try:
            ts = datetime.fromisoformat(lines[0].strip())
        except ValueError:
            ts = None
    if ts is not None and ts.tzinfo is None:
        # The desk's default clock is tz-aware UTC; a naive stamp can only come
        # from an injected clock. Read it as UTC rather than refusing, and never
        # as local time.
        ts = ts.replace(tzinfo=timezone.utc)
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        fields[key.strip()] = value.strip()
    return Heartbeat(path=dest, ts=ts, mtime=mtime, fields=fields)


@dataclass(frozen=True)
class Report:
    """One observation. `text` is what both stdout and the chat get."""

    state: str
    reason: str
    exit_code: int
    text: str
    #: Seconds to wait before the next check. The desk publishes its own
    #: deadline, so the watcher wakes when that deadline expires rather than on
    #: a polling interval somebody picked.
    next_sleep_s: float
    #: The threshold this observation was judged against, so the caller can
    #: re-tell a persisting bad state on the same timescale the desk set.
    stale_after_s: float
    #: The desk process that wrote the heartbeat. Empty when the desk predates
    #: this field, which is NOT the same as unchanged: see `watch`.
    run_id: str = ""
    #: When that process started, verbatim from the file. Reported, never
    #: compared: `run_id` is the identity and this is the human-readable part.
    started_at: str = ""

    @property
    def key(self) -> tuple[str, str]:
        """What has to change before the operator is told again."""
        return (self.state, self.reason)

    @property
    def ok(self) -> bool:
        return self.state == STATE_ARMED


def _age_seconds(ts: datetime, mtime: float, now: datetime) -> float:
    """Age by the desk's own stamp AND by the filesystem, whichever is worse.

    They disagree when the desk's clock is wrong, and a desk with a wrong clock
    is not healthy: `day_key` decides the daily-loss budget from it. Taking the
    larger age means a clock fault shows up as an alarm instead of as silence.
    Clamped at zero: a stamp in the future is not negative age.
    """
    return max(0.0, (now - ts).total_seconds(), now.timestamp() - mtime)


def _int_field(hb: Heartbeat, key: str) -> int | None:
    raw = hb.fields.get(key)
    if raw is None:
        return None
    try:
        return int(float(raw))
    except ValueError:
        return None


def decide(path: str | Path, *, now: datetime, cfg: Any = None) -> Report:
    """Read the heartbeat and name the state. No side effects, no venue calls."""
    dest = Path(path)
    fallback = float(stale_after_seconds(cfg)) if cfg is not None else 60.0
    hb = read(dest)
    if hb is None:
        return Report(
            state=STATE_UNKNOWN,
            reason="no_heartbeat",
            exit_code=EXIT_UNKNOWN,
            next_sleep_s=fallback,
            stale_after_s=fallback,
            text=(
                f"straightedge {STATE_UNKNOWN}: no heartbeat file at {dest}\n"
                "Either the desk has never completed a tick, or this watcher is "
                "pointed at the wrong journal path. Both look identical from "
                "here, so neither is reported as a healthy desk and neither is "
                "reported as a dead one.\n"
                "Check that `run` and `watch` were given the same --config."
            ),
        )
    if hb.ts is None:
        return Report(
            state=STATE_UNKNOWN,
            reason="unparseable",
            exit_code=EXIT_UNKNOWN,
            next_sleep_s=fallback,
            stale_after_s=fallback,
            text=(
                f"straightedge {STATE_UNKNOWN}: the first line of {dest} is not "
                "an ISO timestamp, so the desk's last tick cannot be dated."
            ),
        )
    stale_after = _int_field(hb, "stale_after_s")
    if stale_after is None or stale_after <= 0:
        return Report(
            state=STATE_UNKNOWN,
            reason="no_threshold",
            exit_code=EXIT_UNKNOWN,
            next_sleep_s=fallback,
            stale_after_s=fallback,
            text=(
                f"straightedge {STATE_UNKNOWN}: {dest} carries no usable "
                "stale_after_s, so there is no measured threshold to judge it "
                "against and this watcher will not invent one. The desk that "
                "wrote it predates the watchdog; upgrade the desk and restart "
                "it."
            ),
        )
    if "blocked" not in hb.fields:
        return Report(
            state=STATE_UNKNOWN,
            reason="no_gate_state",
            exit_code=EXIT_UNKNOWN,
            next_sleep_s=fallback,
            stale_after_s=fallback,
            text=(
                f"straightedge {STATE_UNKNOWN}: {dest} carries no blocked= "
                "field, so whether the desk would trade is unmeasured. It is "
                "not reported as armed."
            ),
        )
    age = _age_seconds(hb.ts, hb.mtime, now)
    mode = hb.fields.get("mode", "unknown")
    detail = (
        f"last tick {hb.ts.isoformat()} ({age:.0f}s ago), mode={mode}, "
        f"threshold {stale_after}s"
    )
    notes = []
    if cfg is not None:
        mine = stale_after_seconds(cfg)
        if mine != stale_after:
            notes.append(
                f"NOTE: this watcher derives {mine}s from its own config and "
                f"the desk published {stale_after}s. Two different configs are "
                "in play; the desk's number is the one used here."
            )
    run_id = hb.fields.get("run_id", "")
    started_at = hb.fields.get("started_at", "")
    if not run_id:
        notes.append(
            "NOTE: this desk publishes no run_id, so a restart that does not "
            "change the arming state cannot be seen from the heartbeat. On a "
            "demo account that is EVERY restart. Upgrade the desk; until then "
            "this watcher can report that the desk is ticking and cannot "
            "report that it is the same desk."
        )
    if hb.fields.get("over_budget") == "1":
        notes.append(
            "NOTE: the desk has observed a gap between ticks longer than its "
            f"own budget (tick_gap_max_s={hb.fields.get('tick_gap_max_s')}), so "
            "this threshold is too tight for that book and can produce a false "
            "STALE. It was NOT widened automatically."
        )
    if age > stale_after:
        return Report(
            state=STATE_STALE,
            reason="stale",
            exit_code=EXIT_STALE,
            next_sleep_s=float(stale_after),
            stale_after_s=float(stale_after),
            run_id=run_id,
            started_at=started_at,
            text="\n".join(
                [
                    f"straightedge {STATE_STALE}: no completed tick for "
                    f"{age:.0f}s (threshold {stale_after}s)",
                    detail,
                    "The desk has exited, or its ticks stopped reaching the "
                    "account. This watcher does not take the run lock, so it "
                    "cannot tell those apart; grep `reconnect` in "
                    "journal.jsonl, which separates them after the fact.",
                    "The scheduled task restarts the PROCESS. It comes back "
                    "DISARMED: re-arm with `/live on I-ACCEPT-RISK` in this "
                    "chat once you have checked the terminal.",
                ]
                + notes
            ),
        )
    blocked = hb.fields.get("blocked", "")
    next_sleep = max(1.0, float(stale_after) - age)
    if blocked:
        remedy = (
            "Re-arm from this chat: `/live on I-ACCEPT-RISK`. Arming is per "
            "process and never survives a restart, by design (fc34): do NOT "
            "put --i-accept-risk in the scheduled task."
            if blocked == "live_not_accepted"
            else "Read the reason above against docs/RUNBOOK.md `Halt`; "
            "daily_loss and max_drawdown cannot be cleared from chat."
        )
        return Report(
            state=STATE_NOT_TRADING,
            reason=blocked,
            exit_code=EXIT_NOT_TRADING,
            next_sleep_s=next_sleep,
            stale_after_s=float(stale_after),
            run_id=run_id,
            started_at=started_at,
            text="\n".join(
                [
                    f"straightedge {STATE_NOT_TRADING}: ticking, and it will "
                    f"NOT trade ({blocked})",
                    detail,
                    remedy,
                ]
                + notes
            ),
        )
    return Report(
        state=STATE_ARMED,
        reason="",
        exit_code=EXIT_ARMED,
        next_sleep_s=next_sleep,
        stale_after_s=float(stale_after),
        run_id=run_id,
        started_at=started_at,
        text="\n".join(
            [f"straightedge {STATE_ARMED}: ticking, and the circuit is clear", detail]
            + notes
        ),
    )


def _started_at_utc(text: str) -> datetime | None:
    """A heartbeat `started_at` as an aware datetime, or None if it is not one.

    ONE copy, used by the loop and by the message it composes. Two copies of a
    parse are two places for a naive stamp to be read differently, and the one
    that mattered here is the comparison behind the crash-loop claim: a stamp
    read as local time on one side and UTC on the other would invent or erase
    hours of apparent uptime. Naive reads as UTC and never as local, the same
    rule `read` already applies to line one of the file.
    """
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def restart_text(
    *,
    previous_run_id: str,
    previous_started_at: datetime | None,
    report: Report,
    restarts: int,
    watching_since: datetime,
) -> str:
    """Announce that the desk is a NEW process, and say if it is looping.

    Requirement 3 of straightedge#133 in Conrad's terms: a supervisor that
    quietly papers over repeated crashes converts a loud failure into a slow
    one. So the count is in the message. One restart overnight is information;
    the same line with `restart 46` is a different fact entirely, and an
    operator should not have to scroll a chat to tell them apart.

    The crash-loop claim is deliberately conservative. The elapsed time between
    two processes' `started_at` stamps is the previous run's lifetime PLUS
    however long the box took to restart it, so it is an UPPER BOUND on how long
    that desk lived. Calling a loop only when even the upper bound is under the
    staleness threshold means this line can be believed; the quiet case is a
    restart that is reported without the loop claim, never a loop reported as a
    restart.
    """
    head = (
        f"straightedge RESTARTED: this is a new desk process (restart "
        f"{restarts} seen by this watcher since {watching_since.isoformat()})"
    )
    lines = [head]
    if report.started_at:
        lines.append(f"this run started {report.started_at}")
    this_started = _started_at_utc(report.started_at)
    if previous_started_at is not None and this_started is not None:
        lived = (this_started - previous_started_at).total_seconds()
        # Negative means the clock moved backwards between the two processes,
        # which says nothing about how long the previous one lived, so no claim
        # is made. A desk with a wrong clock is already alarmed by `_age_seconds`
        # taking the worse of the stamp and the filesystem.
        if 0 <= lived < report.stale_after_s:
            lines.append(
                f"the previous run lasted at most {lived:.0f}s, under the "
                f"{report.stale_after_s:.0f}s staleness threshold. That is a "
                "CRASH LOOP, not a restart: the box is starting a desk that "
                "cannot stay up. Read desk stderr and the journal; a desk "
                "whose start() raises never reaches Telegram on its own."
            )
    lines.append(
        f"previous run_id {previous_run_id or _UNKNOWN_RUN}, this run_id "
        f"{report.run_id}"
    )
    lines.append(
        "Arming is per process and never survives a restart (fc34). A "
        "real-money desk is back DISARMED until a human sends "
        "/live on I-ACCEPT-RISK."
    )
    return "\n".join(lines)


def watch(
    path: str | Path,
    cfg: Any,
    *,
    send: Callable[[str], bool] | None = None,
    out: Callable[[str], None] | None = None,
    now_fn: Callable[[], datetime] | None = None,
    sleep_fn: Callable[[float], Any] = time.sleep,
    loop: bool = False,
    ok_every: float = 0.0,
    max_checks: int | None = None,
) -> int:
    """Check once, or until killed. Returns the last observation's exit code.

    The FIRST observation is always announced, healthy or not. That is
    deliberate: an operator installing this has to see the alarm work once, and
    a watcher whose own liveness nobody can check is not a control. After that,
    the chat hears about a CHANGE of state, about a bad state that persists for
    another whole threshold, and about nothing else, because an alarm that
    repeats every few minutes gets muted and a muted alarm is worse than none.

    `ok_every` is the other half of the same problem: this watcher dying is
    silent, and nothing on the box can observe that. Set it and a healthy desk
    is confirmed on that cadence, so SILENCE becomes the signal rather than the
    default. Zero means off, which is the default because a cadence that is not
    the operator's own choice is a number nobody measured.
    """
    emit = out if out is not None else (lambda line: print(line, flush=True))
    clock = now_fn if now_fn is not None else (lambda: datetime.now(timezone.utc))
    last_key: tuple[str, str] | None = None
    last_told: datetime | None = None
    last_ok_beat: datetime | None = None
    #: Process identity of the last heartbeat this watcher saw. Only ever
    #: assigned from a NON-EMPTY run_id: a desk that publishes none must not
    #: look like a desk that restarted, and must not clear the identity of one
    #: that did.
    last_run_id = ""
    last_started_at: datetime | None = None
    restarts = 0
    watching_since: datetime | None = None
    checks = 0
    while True:
        # One clock for the verdict AND for the cadence. `time.monotonic` would
        # be the better choice against an NTP step, but it is not injectable
        # through `now_fn`, and a loop whose timing cannot be driven in a test
        # is a loop whose alert rules were never actually exercised. The cost of
        # being wrong here is one extra or one missing REPEAT of an alert that
        # already fired; the staleness verdict is wall-clock either way.
        now = clock()
        report = decide(path, now=now, cfg=cfg)
        checks += 1
        if watching_since is None:
            watching_since = now
        # A restart needs TWO observed identities. A first sighting is not a
        # restart, and neither is an empty run_id from a desk that predates the
        # field: `decide` already notes that case rather than inferring from it.
        restarted = bool(report.run_id) and bool(last_run_id) and report.run_id != last_run_id
        if restarted:
            restarts += 1
        changed = report.key != last_key
        repeat_due = (
            not report.ok
            and last_told is not None
            and (now - last_told).total_seconds() >= report.stale_after_s
        )
        ok_beat_due = bool(ok_every) and (
            last_ok_beat is None
            or (now - last_ok_beat).total_seconds() >= float(ok_every)
        )
        # A restart ALWAYS tells, whatever the state. That is the whole fix:
        # ARMED -> ARMED across a new process is not a change of state and was
        # silent, which is every restart of a demo desk.
        tell = changed or restarted or repeat_due or (report.ok and ok_beat_due)
        text = report.text
        if restarted:
            text = (
                restart_text(
                    previous_run_id=last_run_id,
                    previous_started_at=last_started_at,
                    report=report,
                    restarts=restarts,
                    watching_since=watching_since,
                )
                + "\n"
                + report.text
            )
        if report.run_id:
            last_run_id = report.run_id
            last_started_at = _started_at_utc(report.started_at)
        if tell:
            emit(text)
            if send is not None and not send(text):
                emit(
                    "watch: the Telegram send FAILED, so this alert reached "
                    "stdout only. Nothing on this host can page you while "
                    "Telegram is unreachable."
                )
            last_told = now
            if report.ok:
                last_ok_beat = now
            last_key = report.key
        if not loop:
            break
        if max_checks is not None and checks >= max_checks:
            break
        sleep_fn(max(1.0, report.next_sleep_s))
    return report.exit_code
