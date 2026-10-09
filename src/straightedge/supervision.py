"""Audit the Windows scheduled tasks that are supposed to keep the desk alive.

Why this file exists, and why it is not a shell script
------------------------------------------------------
`docs/RUNBOOK.md` has documented the correct two-task arrangement since the
watchdog landed: one repeating task starts the desk, one repeating task runs
`watch --loop`. The procedure was prose, an operator typed it once, and nothing
compared the result to the instruction ever again. Measured on the live box
2026-10-08: the MT4 terminal is supervised every two minutes, the DESK has a
logon trigger only and had run exactly once in twelve days, and no watcher task
exists at all. Twelve days of an unsupervised real-money desk, and every
indicator a human could see read normal, because there was no indicator.

A runbook is not a control. The control is a command that reads the live task
definitions and goes red, and that is this module. It is Python rather than
PowerShell for one reason: the comparison has to be exercised by the test suite
on every push, and `tests/test_supervision.py` drives it against a fixture that
reproduces the measured 2026-10-08 drift byte for byte. A PowerShell audit would
run only on the box, which is the same mistake as the runbook one layer down.

The split of labour is therefore: `deploy/windows/Export-Tasks.ps1` dumps what
Task Scheduler holds (read-only, three lines, no logic), and this module decides
whether that is supervision. Nothing here touches the box, starts a process, or
takes `journal.lock`.

Why the interval ceiling is not a number in this file
-----------------------------------------------------
`straightedge.watchdog` already derives how long the desk may go without a tick
from the config that desk runs with, and `#68` is the standing reminder of what
an instrument-blind constant costs. So the restart interval is checked against
`watchdog.stale_after_seconds(cfg)` computed from the SAME config file the desk
is launched with, on the box, at audit time. A ceiling baked in here would be a
measurement of the author's config rather than the operator's.

Severity, and why an unverifiable invariant is a failure
--------------------------------------------------------
`FAIL` means supervision is defeated or a safety invariant cannot be confirmed.
`WARN` means hardening that does not by itself stop a restart. Only `FAIL` sets
a non-zero exit, so the gate has one meaning.

`action_not_auditable` is a `FAIL` and that is the deliberate part. The live
desk task launches `wscript.exe` against a wrapper script, so the desk's real
arguments are not in the task definition: this audit cannot confirm that
`--i-accept-risk` is absent (fc34 forbids it in a scheduled task, because a task
re-runs its arguments on every restart and would re-arm real money with nobody
there), and it cannot confirm the task starts the desk at all. An invariant that
cannot be observed has not been satisfied, and reporting it as satisfied is the
exact failure this module was written about. The remedy is to point the task at
the interpreter directly so the arguments are in the definition where an auditor
can read them.

The declaration is not the behaviour
------------------------------------
Everything above reads a task DEFINITION. #151 measured what that misses, and
it is not a corner case: the live desk's definition carried
`Repetition PT5M` on its `LogonTrigger`, every other field was correct, the
interval was inside the derived ceiling, and this audit returned ZERO FINDINGS
AND EXIT 0 while that desk had not restarted in twelve days. A human reading
the XML saw `PT5M` and concluded the desk was supervised; so did the audit.

So there are now two instruments, and they fail independently:

* STATIC, from the declaration alone. `CLOCK_TRIGGERS`: a repetition hung only
  on an event trigger repeats inside that event, and a logon is the one event
  that is not available when the desk dies unattended. This is the shape that
  actually happened, it needs no export, and it therefore also protects the
  templates this repo ships.
* LIVE, from a `<task>.info.json` sidecar that `Export-Tasks.ps1` writes from
  `Get-ScheduledTaskInfo`. `NextRunTime` is the only reading that separates a
  configured supervisor from a running one, and it is the one that catches the
  shapes a declaration cannot settle: a `StartBoundary` in the future, an
  expired `EndBoundary`, an elapsed `Duration`, a task the live system has
  disabled behind an XML that says enabled.

No single live field is safe, which is why `_check_liveness` judges a TUPLE.
`LastTaskResult = 0x800710E0` is the HEALTHY steady state of the desk's task:
the trigger fires every five minutes, finds the desk alive, and `IgnoreNew`
refuses the duplicate. Flagging non-zero would report the live desk broken
every five minutes forever; ignoring the field would miss a task erroring every
cycle. See `REFUSED_DUPLICATE_LAUNCH`.

A dump of a real task with no sidecar is `liveness_unmeasured`, a FAIL, by the
same rule `task_unreadable` follows. A shipped TEMPLATE is exempt, because it
is registered with nothing and has no `NextRunTime` to have: the artifact says
which it is via `TEMPLATE_PLACEHOLDER`, rather than an operator having to
remember a flag, because a forgotten flag would turn a live audit into a
declaration audit silently and silent is this module's whole subject.

Times in the sidecar are explicit UTC with the offset resolved ON THE BOX,
where the box's offset is known. A bare local timestamp relabelled as UTC by
the reader is straightedge#172's defect with a different clock in it, so
`_parse_utc` REFUSES a naive timestamp rather than assuming one, and the
time-based readings abstain when it does.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.etree import ElementTree

from straightedge import watchdog

FAIL = "FAIL"
WARN = "WARN"

#: `MultipleInstancesPolicy` has to be this. `StopExisting` would make the
#: repeating trigger KILL the live desk on every interval, turning the thing
#: that is supposed to keep it alive into the thing that ends it; `Parallel`
#: would leave `journal.lock` as the only barrier against two desks.
REQUIRED_INSTANCES_POLICY = "IgnoreNew"

#: `ExecutionTimeLimit` has to be indefinite. `schtasks /create` defaults to
#: PT72H, which ends a healthy desk three days in, during a session, for no
#: reason an operator would ever connect to a task setting.
INDEFINITE_LIMITS = frozenset({"PT0S", "PT0M", "PT0H"})

#: `LastTaskResult` when Task Scheduler refused to start a second instance
#: because one was already running and `MultipleInstancesPolicy` is `IgnoreNew`.
#: 0x800710E0 is HRESULT_FROM_WIN32(ERROR_SERVICE_ALREADY_RUNNING).
#:
#: THIS IS THE HEALTHY STEADY STATE FOR A SUPERVISION TASK, and it is why
#: `LastTaskResult` is unusable on its own. The desk's task fires every five
#: minutes and its whole job is "start the desk if it is not running", so on a
#: healthy box it finds the desk alive and is refused, every time, forever. An
#: audit that flagged non-zero here would report the live desk broken every
#: five minutes while it worked perfectly. A ZERO would mean it had just
#: launched a fresh instance, which is the exception and not the rule.
#:
#: The sibling `mt4-terminal-supervisor` reports 0 for the opposite reason: its
#: action is a short probe that exits cleanly, so it is never the
#: already-running case. Same family of task, opposite healthy value. Measured
#: on the live box 2026-10-09 17:46 UTC, both desk tasks reporting this code
#: while `NextRunTime` was five minutes out and `Missed=0`.
#:
#: So nothing here judges one field. The reading is the TUPLE: live state, last
#: result, next run time, instances policy.
REFUSED_DUPLICATE_LAUNCH = 0x800710E0

#: Missed repetition windows tolerated before the audit says so. One is a
#: reboot or a busy moment. A climbing count is the scheduler failing to start
#: something it still intends to start, which no declaration can show.
MISSED_RUNS_WARN_AT = 1

#: `LastRunTime` older than this many declared intervals is reported. Three
#: windows is late enough that one slow start or a reboot does not trip it, and
#: early enough to catch "it ran once a fortnight ago" independently of the
#: declaration. That is exactly the state the live desk was in for twelve days
#: while its XML read perfectly.
STALE_LAST_RUN_INTERVALS = 3.0

#: fc34. A scheduled task re-runs its arguments on every restart, so this flag
#: in a task definition arms real money again on every crash with nobody there.
FORBIDDEN_ARGS = ("--i-accept-risk",)

#: The module an auditable task definition has to name somewhere in its action.
#: This is a property of the DEFINITION, not of which executable is launched:
#: `cmd.exe /c "...python -m straightedge run --loop >> desk.log 2>&1"` is
#: perfectly auditable because every argument is right there in the XML, while
#: `wscript.exe C:\bot-state\run-desk-hidden.vbs` is not, because the desk's
#: real arguments are inside a file this audit never sees. Checking the
#: interpreter's FILENAME instead would have failed the first shape and passed
#: a wrapper called `python-wrapper.exe`.
MODULE_NAME = "straightedge"


@dataclass(frozen=True)
class TaskSpec:
    """What one task has to be for the desk to count as supervised."""

    name: str
    #: The subcommand its arguments must name, e.g. `run` or `watch`.
    subcommand: str
    #: MT4 is a GUI program and only a task running in the logged-in session can
    #: see it, so the desk task's principal is load-bearing and the watcher's is
    #: not. Declared per task rather than assumed for both.
    require_interactive: bool
    #: Why this task exists, quoted into the report so a red line explains
    #: itself to someone who has never read this file.
    purpose: str


DESK_TASK = TaskSpec(
    name="straightedge-desk",
    subcommand="run",
    require_interactive=True,
    purpose=(
        "restarts the desk without a human. The desk owns halt, daily-loss, "
        "drawdown and every refusal gate, so a dead desk is not a paused desk"
    ),
)

WATCH_TASK = TaskSpec(
    name="straightedge-watch",
    subcommand="watch",
    require_interactive=False,
    purpose=(
        "reads the heartbeat and tells the chat when the desk is not ticking. "
        "A desk cannot report its own death"
    ),
)

DECLARED_TASKS: tuple[TaskSpec, ...] = (DESK_TASK, WATCH_TASK)


@dataclass(frozen=True)
class Finding:
    task: str
    severity: str
    code: str
    detail: str

    def line(self) -> str:
        return f"{self.severity} {self.task}: {self.code} -- {self.detail}"


#: Trigger types that recur on a CLOCK, so a repetition hung on one is a cadence
#: that survives having nobody logged in.
#:
#: The rule this encodes is measured, not inferred from the schema. Three
#: readings agree: the broken desk carried its `PT5M` on a `LogonTrigger` alone
#: and fired zero times in twelve days; the working sibling
#: `mt4-terminal-supervisor` carried its repetition on a `TimeTrigger` and fired
#: every two minutes throughout; and the remedy applied on 2026-10-09 was to add
#: a `TimeTrigger` beside the existing `LogonTrigger`, after which `NextRunTime`
#: populated five minutes out.
#:
#: Deliberately NOT a claim about `StopAtDurationEnd` with an empty `Duration`.
#: #151's body asserted that as the mechanism and that was an inference about
#: XML semantics which nothing here measured; the documented schema says an
#: absent `Duration` repeats indefinitely. What was measured is the trigger
#: TYPE, so the trigger type is what this judges.
CLOCK_TRIGGERS = frozenset({"TimeTrigger", "CalendarTrigger"})

#: A task definition carrying this is a TEMPLATE shipped in this repo, not a
#: dump of a live task. `deploy/windows/*.xml` hold it where an installer
#: substitutes real values, and `test_the_declaration_still_carries_its_
#: placeholders` pins that it stays there.
#:
#: It is the discriminator for whether the liveness half APPLIES. A template has
#: no `NextRunTime` because it is not registered with anything, which is not a
#: defect; a dump with real paths and no liveness sidecar is a measurement that
#: was not taken, which is. Keying on this rather than on a flag the operator
#: must remember is deliberate: a forgotten flag would turn a live audit into a
#: declaration audit silently, and silent is the whole subject of this module.
TEMPLATE_PLACEHOLDER = "REPLACE_ME"


@dataclass(frozen=True)
class Repetition:
    """One enabled trigger's repetition, with the trigger TYPE kept.

    The type is the load-bearing part and dropping it is what hid the live
    defect for twelve days. `PT5M` on a `TimeTrigger` is a cadence; the same
    `PT5M` on a `LogonTrigger` is a repetition inside an event that has already
    happened. Both read identically if all you keep is the interval, which is
    all this module used to keep.
    """

    #: Local XML tag, e.g. `TimeTrigger` or `LogonTrigger`.
    trigger: str
    interval_s: float
    #: Raw ISO text, `""` when the element is absent. Reported as measured, with
    #: no reading imposed on it: see `CLOCK_TRIGGERS` on why the mechanism this
    #: module judges is the trigger type and not this field.
    duration: str = ""
    stop_at_duration_end: bool = False


@dataclass
class TaskInfo:
    """What Task Scheduler says the task will actually DO next.

    `schtasks /query /xml` does not carry any of this; `Get-ScheduledTaskInfo`
    does, and `deploy/windows/Export-Tasks.ps1` writes it to a
    `<task>.info.json` sidecar beside the XML it already dumps.

    This whole type exists because of #151: the audit read the declaration and
    never asked what would happen next. Measured on the merged audit, against
    the exact shape the live box carried, a `LogonTrigger` holding
    `Repetition PT5M`: ZERO findings, exit 0, while that desk had not restarted
    in twelve days.
    """

    name: str
    #: A sidecar was found. False means UNMEASURED, which is a failure and never
    #: a pass, by the same rule `task_unreadable` already follows.
    present: bool = False
    #: A sidecar existed and would not parse. Distinct from absent for the same
    #: reason `TaskView.unreadable` is.
    unreadable: bool = False
    parse_error: str = ""
    #: `Ready`, `Running`, `Disabled`, as Task Scheduler reports it LIVE. A
    #: different fact from `Settings/Enabled` in the XML, and a task whose
    #: declaration says enabled while its live state says disabled is precisely
    #: the declaration-versus-reality gap this module is about.
    state: str = ""
    #: Carried from `Get-ScheduledTask` so the benign-refusal reading can be
    #: made without re-reading the XML half.
    multiple_instances: str = ""
    last_task_result: int | None = None
    number_of_missed_runs: int | None = None
    #: UTC, ISO 8601, as written by the exporter. Empty means Task Scheduler has
    #: no next run for this task, which is the whole point of the sidecar.
    next_run_time_utc: str = ""
    last_run_time_utc: str = ""
    #: When the exporter took the measurement, UTC. Staleness is computed
    #: against THIS and never against the audit host's clock: the audit can run
    #: on another machine days later, and comparing a box's timestamp to this
    #: host's clock is #172's mistake with a different clock in it.
    measured_utc: str = ""


@dataclass
class TaskView:
    """The handful of fields that decide whether a task is supervision.

    Deliberately not a faithful model of the Task Scheduler schema. Everything
    absent from here was read and judged irrelevant, which is a statement this
    audit can be held to; a general-purpose parser would make the same silence
    mean "not implemented yet".
    """

    name: str
    present: bool
    #: True when a dump EXISTED and would not parse. Distinct from `present`
    #: being False, which means no such task: one is "the box does not have
    #: supervision" and the other is "this audit could not measure", and
    #: collapsing them would let a corrupt export read as a definite answer.
    unreadable: bool = False
    enabled: bool | None = None
    command: str = ""
    arguments: str = ""
    logon_type: str = ""
    instances_policy: str = ""
    execution_time_limit: str = ""
    #: One entry per enabled trigger that repeats, carrying the trigger TYPE
    #: alongside the window. `repeat_intervals_s` is DERIVED from this rather
    #: than stored beside it: two copies of the same list is how a declaration
    #: and a reality drift apart, which is the defect this module is about.
    repetitions: list[Repetition] = field(default_factory=list)
    has_logon_trigger: bool = False
    #: This dump is a TEMPLATE from this repo, not a live task. See
    #: `TEMPLATE_PLACEHOLDER`: it decides whether the liveness half applies at
    #: all, because a template has no NextRunTime and that is not a defect.
    is_template: bool = False
    #: What Task Scheduler says it will do next, from the sidecar. `None` means
    #: no sidecar was read: UNMEASURED, never healthy.
    info: TaskInfo | None = None
    battery_flags: tuple[bool, bool] = (False, False)
    #: Why the dump would not parse, quoted into the report. A reader needs the
    #: parser's own words to tell a truncated file from a wrong one.
    parse_error: str = ""

    @property
    def repeat_intervals_s(self) -> list[float]:
        """Seconds per repeating enabled trigger. Derived, never stored.

        Kept as a property so the trigger TYPE cannot be dropped on the way in
        while this list still reads full, which is what made a `LogonTrigger`
        carrying `PT5M` indistinguishable from a working cadence.
        """
        return [rep.interval_s for rep in self.repetitions]


_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<hours>\d+(?:\.\d+)?)H)?"
    r"(?:(?P<minutes>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<seconds>\d+(?:\.\d+)?)S)?)?$"
)


def parse_duration_seconds(text: str) -> float | None:
    """ISO 8601 duration to seconds, or None when it is not one.

    None is distinct from 0.0 on purpose: `PT0S` means "no limit" to Task
    Scheduler, and an unparseable value means this audit does not know. Those
    two must never collapse, because one is the configuration we want and the
    other is a measurement we failed to take.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    match = _DURATION_RE.match(raw)
    if match is None:
        return None
    parts = match.groupdict()
    if not any(parts.values()):
        return None
    return (
        float(parts["days"] or 0) * 86400.0
        + float(parts["hours"] or 0) * 3600.0
        + float(parts["minutes"] or 0) * 60.0
        + float(parts["seconds"] or 0)
    )


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _find(node: Any, *names: str) -> Any:
    """First descendant whose LOCAL name matches, namespace ignored.

    Task Scheduler XML declares a default namespace and `schtasks /query /xml`
    emits it, so every path lookup would otherwise need the URI spelled out. One
    helper that matches on the local name keeps a namespace version bump from
    silently turning every check into a no-op, which is the shape of defect this
    whole module exists to catch.
    """
    for child in node.iter():
        if _local(child.tag) in names and child is not node:
            return child
    return None


def _text(node: Any, *names: str) -> str:
    found = _find(node, *names)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


def _bool_text(node: Any, *names: str) -> bool | None:
    raw = _text(node, *names).lower()
    if raw in ("true", "1"):
        return True
    if raw in ("false", "0"):
        return False
    return None


def parse_task_xml(name: str, xml_text: str) -> TaskView:
    """One `schtasks /query /tn <name> /xml` dump into a `TaskView`.

    A dump that will not parse is reported as absent-and-unreadable rather than
    raising: an audit that dies on one malformed task tells you nothing about
    the others, and the operator needs the whole picture in one run.
    """
    try:
        root = ElementTree.fromstring(xml_text.lstrip("﻿"))
    except ElementTree.ParseError as exc:
        return TaskView(name=name, present=False, unreadable=True, parse_error=str(exc))
    view = TaskView(
        name=name, present=True, is_template=TEMPLATE_PLACEHOLDER in xml_text
    )
    settings = _find(root, "Settings")
    if settings is not None:
        view.enabled = _bool_text(settings, "Enabled")
        view.instances_policy = _text(settings, "MultipleInstancesPolicy")
        view.execution_time_limit = _text(settings, "ExecutionTimeLimit")
        view.battery_flags = (
            bool(_bool_text(settings, "DisallowStartIfOnBatteries")),
            bool(_bool_text(settings, "StopIfGoingOnBatteries")),
        )
    exec_node = _find(root, "Exec")
    if exec_node is not None:
        view.command = _text(exec_node, "Command")
        view.arguments = _text(exec_node, "Arguments")
    principal = _find(root, "Principal")
    if principal is not None:
        view.logon_type = _text(principal, "LogonType")
    triggers = _find(root, "Triggers")
    if triggers is not None:
        for trigger in list(triggers):
            kind = _local(trigger.tag)
            trigger_on = _bool_text(trigger, "Enabled")
            # An absent <Enabled> means enabled, which is the opposite of how a
            # missing field usually reads. Defaulting it to False here would
            # make a working trigger invisible to the audit.
            if trigger_on is False:
                continue
            if kind == "LogonTrigger":
                view.has_logon_trigger = True
            repetition = _find(trigger, "Repetition")
            if repetition is None:
                continue
            interval = parse_duration_seconds(_text(repetition, "Interval"))
            if interval is not None and interval > 0:
                view.repetitions.append(
                    Repetition(
                        trigger=kind,
                        interval_s=interval,
                        duration=_text(repetition, "Duration"),
                        stop_at_duration_end=bool(
                            _bool_text(repetition, "StopAtDurationEnd")
                        ),
                    )
                )
    return view


def _opt_int(raw: Any) -> int | None:
    """An integer, or None when the exporter wrote null or nonsense.

    None and 0 must not collapse: `LastTaskResult` 0 means the last launch
    succeeded, and None means this audit does not know what it was.
    """
    if raw is None or isinstance(raw, bool):
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _opt_text(raw: Any) -> str:
    return "" if raw is None else str(raw).strip()


def parse_task_info(name: str, json_text: str) -> TaskInfo:
    """One `<task>.info.json` sidecar into a `TaskInfo`.

    A sidecar that will not parse is `unreadable`, never absent and never
    healthy, for the same reason `parse_task_xml` reports a bad dump that way:
    an audit that cannot read its own instrument has not taken a measurement.
    """
    try:
        raw = json.loads(json_text)
    except json.JSONDecodeError as exc:
        return TaskInfo(name=name, present=False, unreadable=True, parse_error=str(exc))
    if not isinstance(raw, dict):
        return TaskInfo(
            name=name,
            present=False,
            unreadable=True,
            parse_error=f"expected a JSON object, got {type(raw).__name__}",
        )
    return TaskInfo(
        name=name,
        present=True,
        state=_opt_text(raw.get("state")),
        multiple_instances=_opt_text(raw.get("multiple_instances")),
        last_task_result=_opt_int(raw.get("last_task_result")),
        number_of_missed_runs=_opt_int(raw.get("number_of_missed_runs")),
        next_run_time_utc=_opt_text(raw.get("next_run_time_utc")),
        last_run_time_utc=_opt_text(raw.get("last_run_time_utc")),
        measured_utc=_opt_text(raw.get("measured_utc")),
    )


def _parse_utc(text: str) -> datetime | None:
    """An ISO 8601 instant the exporter wrote, as an aware UTC datetime.

    Returns None rather than guessing. A value with no offset is REFUSED rather
    than assumed to be UTC: relabelling a local timestamp as UTC is #172's
    defect, and the exporter is the thing that knows the box's offset, so it
    writes the offset in. An audit that filled it in here would be inventing a
    measurement.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.endswith(("z", "Z")):
        raw = raw[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _check_trigger(
    spec: TaskSpec,
    view: TaskView,
    max_interval_s: float,
    *,
    measurable: bool,
) -> list[Finding]:
    out: list[Finding] = []
    if not view.repeat_intervals_s:
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="no_repeating_trigger",
                detail=(
                    "no enabled trigger repeats, so nothing ever starts this "
                    "task a second time. A logon trigger fires when a human "
                    "logs in, which is the one event that is not available "
                    "when the desk dies unattended. This task "
                    f"{spec.purpose}."
                ),
            )
        )
        return out
    if not any(rep.trigger in CLOCK_TRIGGERS for rep in view.repetitions):
        # The #151 defect, caught in the DECLARATION. This is the half that
        # needs no sidecar, so it protects the shipped template too, and it is
        # why the audit no longer depends on a liveness export to see the one
        # shape that actually happened.
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="repetition_not_on_a_clock",
                detail=(
                    f"declares a repetition ({_describe_repetitions(view)}) and "
                    "not one of those triggers recurs on a clock "
                    f"({', '.join(sorted(CLOCK_TRIGGERS))}). A repetition hung "
                    "only on an event trigger repeats INSIDE that event, and a "
                    "logon is the one event that is not available when the desk "
                    "dies unattended, so a human reads PT5M and the task never "
                    "fires. Measured: this exact shape ran zero times in twelve "
                    "days while every other field was correct, the working "
                    "sibling mt4-terminal-supervisor carries its repetition on "
                    "a TimeTrigger, and adding a TimeTrigger beside the "
                    "LogonTrigger is what populated NextRunTime. This task "
                    f"{spec.purpose}"
                ),
            )
        )
        return out
    slowest = max(view.repeat_intervals_s)
    if not measurable:
        # The same caveat `watchdog_line` carries in `doctor`: with Telegram
        # unset the long-poll term is missing, so the derived threshold
        # collapses toward its floor and describes a desk that CANNOT run
        # (`run` refuses to start without Telegram). Comparing a real 300s
        # interval against a 2s figure would fail every correct task, and a
        # gate that reds on a healthy box is a gate that gets ignored. Report
        # that the comparison was not made; never make it badly.
        out.append(
            Finding(
                task=view.name,
                severity=WARN,
                code="interval_unjudged",
                detail=(
                    f"repeats every {slowest:.0f}s, and this invocation has no "
                    "Telegram configured, so the derived staleness threshold "
                    f"({max_interval_s:.0f}s) is missing its long-poll term "
                    "and is not the figure a running desk gets. The interval "
                    "was NOT judged. Run this with the desk's own config on "
                    "the box to judge it"
                ),
            )
        )
        return out
    if slowest > max_interval_s:
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="interval_slower_than_staleness",
                detail=(
                    f"repeats every {slowest:.0f}s, and the desk's own derived "
                    f"staleness threshold is {max_interval_s:.0f}s. A restart "
                    "slower than the threshold means the gap is alarmed before "
                    "it is closed, so the operator is paged for an outage the "
                    "box was going to fix. Derive the interval from the "
                    "threshold, do not pick it"
                ),
            )
        )
    return out


def _check_action(spec: TaskSpec, view: TaskView) -> list[Finding]:
    out: list[Finding] = []
    command = view.command.strip().strip('"')
    if not command:
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="no_action",
                detail="the task definition carries no Exec action to run",
            )
        )
        return out
    args = view.arguments
    if MODULE_NAME not in f"{command} {args}":
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="action_not_auditable",
                detail=(
                    f"launches {Path(command).name!r} and its arguments never "
                    f"name the {MODULE_NAME} module, so the desk's real "
                    "command line is inside a wrapper this audit cannot read. "
                    "Two invariants then cannot be checked AT ALL: that the "
                    "task starts the desk, and that "
                    f"{FORBIDDEN_ARGS[0]} is absent from it (fc34). An "
                    "invariant that cannot be read has not been met, which is "
                    "why this is a failure and not a warning. Put the command "
                    "line in the task definition; a cmd.exe wrapper that "
                    "carries the arguments and redirects output is fine"
                ),
            )
        )
        return out
    for bad in FORBIDDEN_ARGS:
        if bad in args:
            out.append(
                Finding(
                    task=view.name,
                    severity=FAIL,
                    code="task_arms_real_money",
                    detail=(
                        f"carries {bad} in a scheduled task (fc34). A task "
                        "re-runs its arguments on every restart, so this arms "
                        "a real-money desk again after every crash with nobody "
                        "watching. Arming is per process by design; a human "
                        "sends /live on I-ACCEPT-RISK from the chat"
                    ),
                )
            )
    if not re.search(rf"(^|\s){re.escape(spec.subcommand)}(\s|$)", args):
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="wrong_subcommand",
                detail=(
                    f"does not invoke the {spec.subcommand!r} subcommand, so it "
                    f"is not the task that {spec.purpose}"
                ),
            )
        )
    if spec.subcommand == "watch" and "--loop" not in args:
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="watcher_does_not_loop",
                detail=(
                    "runs `watch` without --loop, so it checks once and exits. "
                    "A one-shot check on a timer cannot distinguish a state "
                    "change from a repeat and will re-announce on every fire"
                ),
            )
        )
    if spec.subcommand == "run" and "--loop" not in args:
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="desk_does_not_loop",
                detail=(
                    "runs `run` without --loop, so the desk takes one tick and "
                    "exits. The repeating trigger would then be the tick rate"
                ),
            )
        )
    return out


def _check_settings(spec: TaskSpec, view: TaskView) -> list[Finding]:
    out: list[Finding] = []
    if view.enabled is False:
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="task_disabled",
                detail=(
                    "the task is disabled. Its triggers are correct and none of "
                    "them will ever fire, which is the state a trigger-only "
                    "check reports as healthy"
                ),
            )
        )
    if view.instances_policy and view.instances_policy != REQUIRED_INSTANCES_POLICY:
        severity = FAIL
        extra = ""
        if view.instances_policy == "StopExisting":
            extra = (
                ". StopExisting is the dangerous one: the repeating trigger "
                "would END the healthy desk on every interval"
            )
        out.append(
            Finding(
                task=view.name,
                severity=severity,
                code="instances_policy",
                detail=(
                    f"MultipleInstancesPolicy is {view.instances_policy!r}, "
                    f"not {REQUIRED_INSTANCES_POLICY!r}{extra}"
                ),
            )
        )
    limit = view.execution_time_limit.strip()
    if limit and limit.upper() not in INDEFINITE_LIMITS:
        seconds = parse_duration_seconds(limit)
        measured = f"{seconds:.0f}s" if seconds is not None else f"{limit!r}"
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="execution_time_limit",
                detail=(
                    f"ExecutionTimeLimit is {measured}, so Task Scheduler ends "
                    "this process itself once that elapses. `schtasks /create` "
                    "defaults to PT72H, which kills a healthy desk three days "
                    "in, mid-session. Set PT0S"
                ),
            )
        )
    if spec.require_interactive and view.logon_type and view.logon_type != "InteractiveToken":
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="not_interactive",
                detail=(
                    f"LogonType is {view.logon_type!r}. MT4 is a GUI program "
                    "and only a task in the logged-in session can see it, so a "
                    "task set to run whether the user is logged on or not "
                    "restarts a desk that cannot reach the terminal"
                ),
            )
        )
    if any(view.battery_flags):
        out.append(
            Finding(
                task=view.name,
                severity=WARN,
                code="battery_stops_the_task",
                detail=(
                    "DisallowStartIfOnBatteries or StopIfGoingOnBatteries is "
                    "set, which are the schtasks defaults. Harmless on a VPS "
                    "with no battery; on a laptop it stops the desk on unplug"
                ),
            )
        )
    if not view.has_logon_trigger:
        out.append(
            Finding(
                task=view.name,
                severity=WARN,
                code="no_logon_trigger",
                detail=(
                    "no logon trigger, so after a reboot this task waits for "
                    "its next repeat instead of starting with the session"
                ),
            )
        )
    return out


def _describe_repetitions(view: TaskView) -> str:
    """Every repetition as measured, with no reading imposed on it."""
    parts = []
    for rep in view.repetitions:
        window = rep.duration.strip() or "(no Duration)"
        parts.append(
            f"{rep.trigger} every {rep.interval_s:.0f}s, Duration {window}, "
            f"StopAtDurationEnd {str(rep.stop_at_duration_end).lower()}"
        )
    return "; ".join(parts) or "(none)"


def _check_liveness(spec: TaskSpec, view: TaskView) -> list[Finding]:
    """What Task Scheduler intends to DO, as opposed to what the XML declares.

    `repetition_not_on_a_clock` already catches the one shape that happened,
    from the declaration alone. This half is for the shapes a declaration
    CANNOT settle: a `StartBoundary` in the future, an expired `EndBoundary`, an
    elapsed `Duration`, a task the live system has disabled behind an XML that
    says enabled, a launch that errors every cycle. `NextRunTime` is empty in
    the first three and the XML is perfect in all five.

    Skipped entirely for a template, which has no NextRunTime because it is
    registered with nothing. That is not an unmeasured invariant; it is a
    different question, and `TEMPLATE_PLACEHOLDER` is how the artifact says
    which question it is answering.
    """
    if view.is_template:
        return []
    out: list[Finding] = []
    info = view.info
    if info is not None and info.unreadable:
        return [
            Finding(
                task=view.name,
                severity=FAIL,
                code="liveness_unreadable",
                detail=(
                    "a liveness sidecar for this task was written and will not "
                    f"parse ({info.parse_error}). The declaration was read; "
                    "what Task Scheduler will DO next was NOT measured, so it "
                    "is reported as a failure rather than as either answer. "
                    "Re-dump with deploy/windows/Export-Tasks.ps1"
                ),
            )
        ]
    if info is None or not info.present:
        return [
            Finding(
                task=view.name,
                severity=FAIL,
                code="liveness_unmeasured",
                detail=(
                    "this is a dump of a real task and there is no "
                    f"{view.name}.info.json beside it, so the audit read the "
                    "DECLARATION and never asked Task Scheduler what the task "
                    "will do next. An unmeasured invariant has not been met "
                    "(#151). Re-dump with a current "
                    "deploy/windows/Export-Tasks.ps1, which writes the sidecar "
                    "beside each XML. If you meant to audit the shipped "
                    f"templates, they carry {TEMPLATE_PLACEHOLDER} and this "
                    "check stands aside for them"
                ),
            )
        ]
    disabled_live = info.state.strip().lower() == "disabled"
    if disabled_live:
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="live_state_disabled",
                detail=(
                    "Task Scheduler reports State=Disabled. That is a different "
                    "fact from Settings/Enabled in the XML, and when the two "
                    "disagree the live state is the one that decides whether "
                    f"anything fires. This task {spec.purpose}"
                ),
            )
        )
    # Conditional on a repetition being DECLARED, which settles #151's open
    # question: a task whose only trigger fires once at logon declares no
    # repetition and is never judged here, and a task with no repetition at all
    # is already `no_repeating_trigger`. Skipped when the task is disabled by
    # either reading, because then the empty NextRunTime is explained and
    # naming one cause twice makes a report harder to act on.
    if (
        view.repetitions
        and not info.next_run_time_utc
        and not disabled_live
        and view.enabled is not False
    ):
        slowest = max(view.repeat_intervals_s)
        out.append(
            Finding(
                task=view.name,
                severity=FAIL,
                code="repetition_never_fires",
                detail=(
                    f"declares a repetition ({_describe_repetitions(view)}) and "
                    "Task Scheduler reports NO NextRunTime, so it does not "
                    "intend to start this task again. The repetition is real "
                    "and it is not a cadence. A declaration cannot settle this: "
                    f"{slowest:.0f}s is declared, the trigger window is what "
                    "decides, and only the scheduler knows whether it is open. "
                    f"This task {spec.purpose}"
                ),
            )
        )
    measured_at = _parse_utc(info.measured_utc)
    next_run = _parse_utc(info.next_run_time_utc)
    if measured_at is not None and next_run is not None and next_run < measured_at:
        out.append(
            Finding(
                task=view.name,
                severity=WARN,
                code="next_run_in_the_past",
                detail=(
                    f"NextRunTime {info.next_run_time_utc} is BEFORE the moment "
                    f"the export measured it ({info.measured_utc}), so the "
                    "scheduler is not advancing the window. Populated but stale "
                    "is a third state, distinct from empty and from healthy"
                ),
            )
        )
    result = info.last_task_result
    if result is not None and result != 0:
        if result == REFUSED_DUPLICATE_LAUNCH:
            # The healthy steady state for a supervision task, so this is a
            # finding ONLY in the combination that contradicts it: a refused
            # duplicate launch means an instance WAS running, and if nothing is
            # running now then what it declined to replace has since died.
            if info.state.strip().lower() != "running":
                out.append(
                    Finding(
                        task=view.name,
                        severity=WARN,
                        code="refused_launch_while_nothing_runs",
                        detail=(
                            "LastTaskResult is 0x800710E0, the benign refusal "
                            "of a duplicate launch, which means an instance was "
                            f"already running. But State is {info.state!r}, not "
                            "'Running'. On a healthy supervision task those two "
                            "go together; apart, they say the instance it "
                            "declined to replace has since exited. Can also be "
                            "a momentary race between the refusal and the export"
                        ),
                    )
                )
        else:
            out.append(
                Finding(
                    task=view.name,
                    severity=FAIL,
                    code="last_run_failed",
                    detail=(
                        f"LastTaskResult is {result} "
                        f"(0x{result & 0xFFFFFFFF:08X}) and that is NOT the "
                        "benign already-running refusal 0x800710E0, so the last "
                        "launch of this task errored. A declaration cannot show "
                        "this: the task fires, fails, and the XML stays perfect"
                    ),
                )
            )
    missed = info.number_of_missed_runs
    if missed is not None and missed > MISSED_RUNS_WARN_AT:
        out.append(
            Finding(
                task=view.name,
                severity=WARN,
                code="missed_runs",
                detail=(
                    f"NumberOfMissedRuns is {missed}. A populated NextRunTime "
                    "with a climbing missed count is a third state again: the "
                    "scheduler intends to fire and is not managing to"
                ),
            )
        )
    last_run = _parse_utc(info.last_run_time_utc)
    if view.repetitions and measured_at is not None and last_run is not None:
        slowest = max(view.repeat_intervals_s)
        age_s = (measured_at - last_run).total_seconds()
        if age_s > slowest * STALE_LAST_RUN_INTERVALS:
            out.append(
                Finding(
                    task=view.name,
                    severity=WARN,
                    code="last_run_stale",
                    detail=(
                        f"LastRunTime is {age_s / 3600:.1f}h before the export, "
                        f"against a declared repetition of {slowest:.0f}s. "
                        "Whatever the declaration says the cadence is, this task "
                        "has not been running at it. The live desk sat in this "
                        "state for twelve days"
                    ),
                )
            )
    return out


def audit_task(
    spec: TaskSpec,
    view: TaskView,
    *,
    max_interval_s: float,
    measurable: bool = True,
) -> list[Finding]:
    """Every finding for one task. Order is stable so output diffs cleanly."""
    if view.unreadable:
        return [
            Finding(
                task=spec.name,
                severity=FAIL,
                code="task_unreadable",
                detail=(
                    "a definition for this task was dumped and will not parse "
                    f"({view.parse_error}). This is NOT the same reading as "
                    "the task being absent: supervision here is UNMEASURED, so "
                    "it is reported as a failure rather than as either answer. "
                    "Re-dump it with deploy/windows/Export-Tasks.ps1"
                ),
            )
        ]
    if not view.present:
        return [
            Finding(
                task=spec.name,
                severity=FAIL,
                code="task_missing",
                detail=(
                    f"no such scheduled task. This task {spec.purpose}. "
                    "Absence is the reading an operator cannot get from the "
                    "box by looking at it"
                ),
            )
        ]
    return (
        _check_trigger(spec, view, max_interval_s, measurable=measurable)
        + _check_action(spec, view)
        + _check_settings(spec, view)
        + _check_liveness(spec, view)
    )


def audit(
    views: dict[str, TaskView],
    *,
    max_interval_s: float,
    measurable: bool = True,
    specs: tuple[TaskSpec, ...] = DECLARED_TASKS,
) -> list[Finding]:
    out: list[Finding] = []
    for spec in specs:
        view = views.get(spec.name) or TaskView(name=spec.name, present=False)
        out.extend(
            audit_task(
                spec, view, max_interval_s=max_interval_s, measurable=measurable
            )
        )
    return out


def read_dump(path: Path) -> str:
    """Decode a task dump whichever way Windows wrote it.

    `schtasks /query /xml` emits UTF-16 with a BOM, and a `cmd.exe` redirect
    keeps it that way, while PowerShell `Out-File -Encoding utf8` re-encodes.
    Both shapes reach this function in practice, so the BOM is sniffed from the
    BYTES rather than assumed from the XML declaration. Guessing wrong here
    would not raise: it would produce text that will not parse, and
    `parse_task_xml` reports an unparseable dump as an ABSENT task, which would
    read as "the supervision task is missing" when the truth is "this audit
    cannot read the file". Those must not be confused, so the decode is
    explicit.
    """
    raw = path.read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16", errors="replace")
    # A UTF-16LE dump without a BOM still has a NUL after every ASCII byte.
    if b"\x00" in raw[:64]:
        return raw.decode("utf-16-le", errors="replace")
    return raw.decode("utf-8-sig", errors="replace")


def _load_info(directory: Path, name: str) -> TaskInfo | None:
    """The `<name>.info.json` sidecar beside a dump, or None if absent.

    None rather than an empty `TaskInfo` on purpose: absent and
    present-but-empty are different measurements, and `_check_liveness`
    reports the first as UNMEASURED. Audited through the same `read_dump`
    as the XML because PowerShell writes both and the BOM question is the
    same one.
    """
    candidate = directory / f"{name}.info.json"
    if not candidate.is_file():
        return None
    return parse_task_info(name, read_dump(candidate))


def load_views(source: str | Path, *, specs: tuple[TaskSpec, ...] = DECLARED_TASKS) -> dict[str, TaskView]:
    """Read one `<name>.xml` per declared task from a directory, or one file.

    A declared task with no file is NOT silently skipped: it becomes an absent
    `TaskView`, which audits as `task_missing`. A zero-finding run on an empty
    directory would be the single worst outcome this module could produce.
    """
    path = Path(source)
    views: dict[str, TaskView] = {}
    if path.is_dir():
        for spec in specs:
            candidate = path / f"{spec.name}.xml"
            if not candidate.is_file():
                continue
            view = parse_task_xml(spec.name, read_dump(candidate))
            view.info = _load_info(path, spec.name)
            views[spec.name] = view
        return views
    text = read_dump(path)
    name = path.stem
    view = parse_task_xml(name, text)
    view.info = _load_info(path.parent, name)
    views[name] = view
    return views


def liveness_note(views: dict[str, TaskView]) -> str:
    """One line on whether what-happens-next was measured, and for what.

    Stated in the header rather than left to the ABSENCE of a finding, because
    "no liveness finding" has two causes: these are templates and the question
    does not apply, or the box was measured and is fine. A reader cannot tell
    those apart from silence, and an instrument that is silent about not having
    measured is the subject of this entire module.
    """
    live = [v for v in views.values() if v.present and not v.is_template]
    if not live:
        return (
            "liveness: NOT APPLICABLE, every definition read is a shipped "
            f"template carrying {TEMPLATE_PLACEHOLDER}, so Task Scheduler holds "
            "no NextRunTime for any of them. This run judges the DECLARATION "
            "only; point --tasks at Export-Tasks.ps1 output to judge a box"
        )
    measured = [v for v in live if v.info is not None and v.info.present]
    return (
        f"liveness: measured for {len(measured)} of {len(live)} live task(s) "
        "from Get-ScheduledTaskInfo sidecars. NextRunTime is the one reading "
        "that separates a configured supervisor from a running one (#151)"
    )


def report(
    findings: list[Finding],
    *,
    max_interval_s: float,
    measurable: bool = True,
    specs: tuple[TaskSpec, ...] = DECLARED_TASKS,
    liveness: str = "",
) -> str:
    """The whole operator-facing output, failures first."""
    ceiling = (
        f"restart interval ceiling {max_interval_s:.0f}s, derived from this "
        "config via watchdog.stale_after_seconds"
    )
    if not measurable:
        ceiling += (
            " -- telegram unset, so the poll term is missing and the interval "
            "was NOT judged against this figure"
        )
    lines = [
        "supervision audit: the scheduled tasks that keep the desk alive",
        ceiling,
    ]
    if liveness:
        lines.append(liveness)
    fails = [f for f in findings if f.severity == FAIL]
    warns = [f for f in findings if f.severity == WARN]
    for finding in fails + warns:
        lines.append(finding.line())
    clean = {spec.name for spec in specs} - {f.task for f in fails}
    for name in sorted(clean):
        lines.append(f"ok {name}: supervised, and its arguments are auditable")
    if fails:
        lines.append(
            f"{len(fails)} failure(s): the desk is NOT supervised as declared. "
            "See deploy/windows/README.md"
        )
    else:
        lines.append("no failures: every declared task is supervision")
    return "\n".join(lines)


def exit_code(findings: list[Finding]) -> int:
    """0 when nothing FAILED. WARN never reds the gate; one meaning per code."""
    return 1 if any(f.severity == FAIL for f in findings) else 0


def interval_ceiling(cfg: Any) -> tuple[float, bool]:
    """The restart-interval ceiling, and whether it is a figure to judge by.

    Second element false means the config has no Telegram, so
    `watchdog.stale_after_seconds` is missing its long-poll term. `doctor` has
    carried the same caveat since the watchdog landed, for the same reason:
    `run` refuses to start without Telegram, so that number describes a desk
    that cannot exist. The audit then reports the interval instead of judging
    it.
    """
    enabled = bool(getattr(getattr(cfg, "telegram", None), "enabled", False))
    return float(watchdog.stale_after_seconds(cfg)), enabled
