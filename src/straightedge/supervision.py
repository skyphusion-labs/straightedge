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
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
    #: Seconds, for each enabled trigger that repeats. Empty means nothing in
    #: this task ever fires twice.
    repeat_intervals_s: list[float] = field(default_factory=list)
    has_logon_trigger: bool = False
    battery_flags: tuple[bool, bool] = (False, False)
    #: Why the dump would not parse, quoted into the report. A reader needs the
    #: parser's own words to tell a truncated file from a wrong one.
    parse_error: str = ""


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
    view = TaskView(name=name, present=True)
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
                view.repeat_intervals_s.append(interval)
    return view


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
            views[spec.name] = parse_task_xml(spec.name, read_dump(candidate))
        return views
    text = read_dump(path)
    name = path.stem
    views[name] = parse_task_xml(name, text)
    return views


def report(
    findings: list[Finding],
    *,
    max_interval_s: float,
    measurable: bool = True,
    specs: tuple[TaskSpec, ...] = DECLARED_TASKS,
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
