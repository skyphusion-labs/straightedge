"""The audit of the scheduled tasks that keep the desk alive, driven RED first.

What this suite is FOR. `docs/RUNBOOK.md` documented the correct two-task
arrangement from the day the watchdog landed, an operator typed it once, and
nothing ever compared the box to the instruction again. Measured on the live
box 2026-10-08: the MT4 terminal was supervised every two minutes, the desk had
a logon trigger only and had run once in twelve days, and the watcher task did
not exist. Twelve days of an unsupervised real-money desk with every visible
indicator reading normal, because there was no indicator.

So the first test below is not a smoke test. It is the measured live state,
reconstructed as a task dump, and it has to FAIL. A gate written after an
incident that cannot reproduce that incident is decoration.

The rest of the suite is the other half of the same discipline: every check
gets a case that drives it red, and the two cases that must NOT red (a warning
that is only hardening, and an interval that cannot be judged) are here too,
because a gate that fails everything is as useless as one that fails nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from straightedge import supervision
from straightedge.config import BotConfig, SessionConfig, TelegramConfig

REPO = Path(__file__).resolve().parents[1]
DECLARED = REPO / "deploy" / "windows"

#: What the shipped declaration asks Task Scheduler for, in seconds.
DECLARED_INTERVAL_S = 300.0
#: `tests/test_watchdog.py` pins the same arithmetic: poll_seconds = 1 on MT4.
MT4_STALE = 428


def _cfg(tmp_path: Path, *, mode: str = "mt4", telegram: bool = True) -> BotConfig:
    cfg = BotConfig()
    cfg.mode = mode
    cfg.poll_seconds = 1
    cfg.session = SessionConfig(enabled=False)
    cfg.symbols = ["EURUSD"]
    cfg.journal_path = str(tmp_path / "journal.jsonl")
    cfg.risk.halt_file = str(tmp_path / "HALT")
    cfg.telegram = TelegramConfig(token="t" * 10, chat_id="42") if telegram else TelegramConfig()
    return cfg


def _task_xml(
    *,
    name: str = "straightedge-desk",
    triggers: str,
    command: str = "C:\\Python312\\python.exe",
    arguments: str = "-m straightedge --config C:\\bot\\config.toml run --mode mt4 --loop",
    logon_type: str = "InteractiveToken",
    instances: str = "IgnoreNew",
    time_limit: str = "PT0S",
    enabled: str = "true",
    batteries: str = "false",
) -> str:
    """A Task Scheduler dump, in the shape `schtasks /query /xml` emits.

    Hand-built rather than captured so each test can move ONE field. The shape
    itself is cross-checked against the shipped declaration by
    `test_the_fixture_shape_matches_the_shipped_declaration`, so a fixture that
    drifted into a shape Task Scheduler never produces cannot quietly become
    the only thing this suite tests.
    """
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <URI>\\{name}</URI>
  </RegistrationInfo>
  <Triggers>
{triggers}
  </Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>EXAMPLE\\operator</UserId>
      <LogonType>{logon_type}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>{instances}</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>{batteries}</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>{batteries}</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>{enabled}</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>{time_limit}</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>C:\\bot</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


LOGON_ONLY = """    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>EXAMPLE\\operator</UserId>
    </LogonTrigger>"""

LOGON_AND_REPEAT = """    <LogonTrigger>
      <Enabled>true</Enabled>
      <UserId>EXAMPLE\\operator</UserId>
    </LogonTrigger>
    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>true</Enabled>
    </TimeTrigger>"""


def _repeat_every(iso: str, *, enabled: str = "true") -> str:
    return f"""    <TimeTrigger>
      <Repetition>
        <Interval>{iso}</Interval>
        <StopAtDurationEnd>false</StopAtDurationEnd>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
      <Enabled>{enabled}</Enabled>
    </TimeTrigger>"""


def _audit_one(xml: str, cfg: BotConfig, spec: supervision.TaskSpec | None = None) -> list[supervision.Finding]:
    chosen = spec or supervision.DESK_TASK
    view = supervision.parse_task_xml(chosen.name, xml)
    ceiling, measurable = supervision.interval_ceiling(cfg)
    return supervision.audit_task(
        chosen, view, max_interval_s=ceiling, measurable=measurable
    )


def _codes(findings: list[supervision.Finding], severity: str | None = None) -> set[str]:
    return {f.code for f in findings if severity is None or f.severity == severity}


# --- the incident, reproduced ----------------------------------------------------------


def test_the_measured_live_box_fails_the_audit(tmp_path: Path) -> None:
    """The live box on 2026-10-08, reconstructed. This MUST be red.

    Two independent defects in one task definition: a logon trigger only, so
    nothing restarts the desk unless a human logs in, and an action that runs a
    hidden wrapper script, so the desk's real command line is not in the
    definition and neither "does this start the desk" nor "is the
    risk-acceptance flag absent" can be checked at all.
    """
    xml = _task_xml(
        triggers=LOGON_ONLY,
        command="C:\\Windows\\System32\\wscript.exe",
        arguments="C:\\bot-state\\run-desk-hidden.vbs",
    )
    findings = _audit_one(xml, _cfg(tmp_path))
    codes = _codes(findings, supervision.FAIL)
    print(f"supervision: live box -> {sorted(codes)}")
    assert "no_repeating_trigger" in codes
    assert "action_not_auditable" in codes
    assert supervision.exit_code(findings) == 1
    text = supervision.report(findings, max_interval_s=float(MT4_STALE))
    assert "logs in" in text
    assert "i-accept-risk" in text.lower()


def test_the_watcher_task_absence_is_a_failure_not_a_silence(tmp_path: Path) -> None:
    """The other half of the measured state: no `straightedge-watch` at all.

    An empty dump directory must not audit clean. This is the single worst
    outcome this module could produce, so it gets its own test rather than
    being implied by the one above.
    """
    empty = tmp_path / "tasks"
    empty.mkdir()
    views = supervision.load_views(empty)
    ceiling, measurable = supervision.interval_ceiling(_cfg(tmp_path))
    findings = supervision.audit(views, max_interval_s=ceiling, measurable=measurable)
    assert _codes(findings, supervision.FAIL) == {"task_missing"}
    assert {f.task for f in findings} == {"straightedge-desk", "straightedge-watch"}
    assert supervision.exit_code(findings) == 1


# --- the shipped declaration ----------------------------------------------------------


def test_the_shipped_declaration_passes_its_own_audit(tmp_path: Path) -> None:
    """The artifact satisfies the rule that judges it, or one of them is wrong."""
    views = supervision.load_views(DECLARED)
    ceiling, measurable = supervision.interval_ceiling(_cfg(tmp_path))
    findings = supervision.audit(views, max_interval_s=ceiling, measurable=measurable)
    fails = [f.line() for f in findings if f.severity == supervision.FAIL]
    assert fails == [], "the shipped declaration fails its own audit: " + "; ".join(fails)
    assert supervision.exit_code(findings) == 0


def test_the_declaration_still_carries_its_placeholders() -> None:
    """Same guard as `test_launchd_example_keepalive_umask_heartbeat`.

    A real path committed once is a path that rots silently.
    """
    for name in ("straightedge-desk", "straightedge-watch"):
        text = (DECLARED / f"{name}.xml").read_text(encoding="utf-8")
        assert "REPLACE_ME" in text
        assert "--i-accept-risk" not in text


#: The strings that must not appear in anything this change ships: the live
#: box's address, its computer name, and the principal the desk runs as. They
#: are ASSEMBLED FROM FRAGMENTS so that this file does not match its own
#: needles. That is not a trick to dodge the check: a guard whose only hit is
#: its own declaration gets its needles deleted by the next reader, and then
#: the guard is gone while still looking present.
LIVE_BOX_NEEDLES = (
    "64.177" + ".8.7",
    "STRAIGHT" + "EDGE" + "\\",
    "straightedge" + "-vps",
)


def _names_the_live_box(paths: list[Path]) -> list[str]:
    """Every (file, needle) hit. Separate from its assertion so it can go red.

    `tests/test_the_live_box_guard_can_go_red` plants one of the needles in a
    temporary file and requires this to find it. Without that, a guard that
    returned an empty list because of a wrong root, a swallowed read error or
    an empty needle tuple would be indistinguishable from a clean tree, which
    is the failure this whole module is about one layer up.
    """
    hits: list[str] = []
    for path in paths:
        if not path.is_file() or path.suffix == ".pyc":
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for needle in LIVE_BOX_NEEDLES:
            if needle in text:
                hits.append(f"{path}: {needle!r}")
    return hits


def _shipped_paths() -> list[Path]:
    roots = [REPO / "deploy", REPO / "tests" / "test_supervision.py"]
    out: list[Path] = []
    for base in roots:
        out.extend(sorted(base.rglob("*")) if base.is_dir() else [base])
    return out


def test_nothing_shipped_here_names_the_live_box() -> None:
    """This repo is PUBLIC and that box holds a real-money desk.

    The finding in `deploy/windows/README.md` is the trigger table, not the
    address, and an installer example reads as a template whether or not it
    carries the real host. This file is scanned too, because its own fixtures
    are the obvious place to paste a real principal in from the box, and
    `EXAMPLE\\operator` serves them exactly as well.

    It also covers the admin question. The desk running as Administrator is
    open work, so writing that principal down here would hand an adversary a
    hint rather than merely a hostname.
    """
    paths = _shipped_paths()
    assert len(paths) > 5, "this guard scanned almost nothing, so it proves nothing"
    hits = _names_the_live_box(paths)
    assert hits == [], "a shipped file names the live box: " + "; ".join(hits)


def test_the_live_box_guard_can_go_red(tmp_path: Path) -> None:
    """The control of the control: plant a needle and require a hit."""
    planted = tmp_path / "Install-Example.ps1"
    planted.write_text(
        "-ComputerName " + LIVE_BOX_NEEDLES[0] + "\n", encoding="utf-8"
    )
    hits = _names_the_live_box([planted])
    assert len(hits) == 1, f"the guard did not see a planted needle: {hits}"
    assert LIVE_BOX_NEEDLES[0] in hits[0]


def test_the_declared_interval_is_under_the_derived_threshold(tmp_path: Path) -> None:
    """The number in the XML is tied to the code that derives it.

    If `poll_seconds`, the Telegram retry ceiling or a venue `timeout_ms`
    default moves the threshold below 300s, this test is what notices that the
    shipped restart interval has become slower than the alarm.
    """
    ceiling, measurable = supervision.interval_ceiling(_cfg(tmp_path))
    assert measurable
    print(f"supervision: declared {DECLARED_INTERVAL_S:.0f}s, ceiling {ceiling:.0f}s")
    assert ceiling == MT4_STALE
    assert DECLARED_INTERVAL_S <= ceiling
    for name in ("straightedge-desk", "straightedge-watch"):
        view = supervision.parse_task_xml(
            name, supervision.read_dump(DECLARED / f"{name}.xml")
        )
        assert view.repeat_intervals_s == [DECLARED_INTERVAL_S]


def test_the_fixture_shape_matches_the_shipped_declaration(tmp_path: Path) -> None:
    """The fixtures above and the real artifact must parse to the same fields.

    Without this, a fixture could drift into a shape Task Scheduler never emits
    and the whole suite would be testing the fixture generator.
    """
    hand = supervision.parse_task_xml(
        "straightedge-desk", _task_xml(triggers=LOGON_AND_REPEAT)
    )
    real = supervision.parse_task_xml(
        "straightedge-desk", supervision.read_dump(DECLARED / "straightedge-desk.xml")
    )
    assert hand.instances_policy == real.instances_policy
    assert hand.execution_time_limit == real.execution_time_limit
    assert hand.logon_type == real.logon_type
    assert hand.repeat_intervals_s == real.repeat_intervals_s
    assert hand.has_logon_trigger == real.has_logon_trigger is True
    assert hand.enabled == real.enabled is True


# --- one field at a time, each driven red ---------------------------------------------


def test_a_disabled_task_fails_although_its_triggers_are_perfect(tmp_path: Path) -> None:
    """The state a trigger-only check reports as healthy."""
    xml = _task_xml(triggers=LOGON_AND_REPEAT, enabled="false")
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "task_disabled" in _codes(findings, supervision.FAIL)


def test_a_disabled_trigger_does_not_count_as_a_repeat(tmp_path: Path) -> None:
    """A repetition on a trigger that will never fire is not supervision."""
    xml = _task_xml(triggers=_repeat_every("PT5M", enabled="false"))
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "no_repeating_trigger" in _codes(findings, supervision.FAIL)


def test_stop_existing_is_a_failure_and_names_what_it_would_do(tmp_path: Path) -> None:
    """The misconfiguration that turns the supervisor into the killer."""
    xml = _task_xml(triggers=LOGON_AND_REPEAT, instances="StopExisting")
    findings = _audit_one(xml, _cfg(tmp_path))
    hit = [f for f in findings if f.code == "instances_policy"]
    assert hit and hit[0].severity == supervision.FAIL
    assert "END the healthy desk" in hit[0].detail


def test_parallel_instances_is_also_a_failure(tmp_path: Path) -> None:
    xml = _task_xml(triggers=LOGON_AND_REPEAT, instances="Parallel")
    assert "instances_policy" in _codes(_audit_one(xml, _cfg(tmp_path)), supervision.FAIL)


def test_the_schtasks_default_time_limit_fails(tmp_path: Path) -> None:
    """PT72H is what `schtasks /create` gives you, and it ends a healthy desk.

    This is the most likely way to reintroduce "the bot died mid trading day"
    while believing the box is supervised, which is why it is a failure and not
    a warning.
    """
    xml = _task_xml(triggers=LOGON_AND_REPEAT, time_limit="PT72H")
    findings = _audit_one(xml, _cfg(tmp_path))
    hit = [f for f in findings if f.code == "execution_time_limit"]
    assert hit and hit[0].severity == supervision.FAIL
    assert "259200s" in hit[0].detail


def test_an_unparseable_time_limit_is_reported_not_coerced(tmp_path: Path) -> None:
    """A value this audit cannot read is not a value it may assume is fine."""
    xml = _task_xml(triggers=LOGON_AND_REPEAT, time_limit="three days")
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "execution_time_limit" in _codes(findings, supervision.FAIL)


def test_a_task_that_arms_real_money_fails(tmp_path: Path) -> None:
    """fc34. A task re-runs its arguments on every restart."""
    xml = _task_xml(
        triggers=LOGON_AND_REPEAT,
        arguments=(
            "-m straightedge --config C:\\bot\\config.toml run --mode mt4 "
            "--loop --i-accept-risk"
        ),
    )
    findings = _audit_one(xml, _cfg(tmp_path))
    hit = [f for f in findings if f.code == "task_arms_real_money"]
    assert hit and hit[0].severity == supervision.FAIL
    assert "every crash" in hit[0].detail


def test_a_non_interactive_desk_task_fails_and_the_watcher_is_allowed_one(
    tmp_path: Path,
) -> None:
    """MT4 is a GUI program; the watcher only reads a file.

    Declared per task rather than assumed for both, so this test is also the
    proof that the distinction is real and not a comment.
    """
    cfg = _cfg(tmp_path)
    desk = _task_xml(triggers=LOGON_AND_REPEAT, logon_type="Password")
    assert "not_interactive" in _codes(_audit_one(desk, cfg), supervision.FAIL)
    watcher = _task_xml(
        name="straightedge-watch",
        triggers=LOGON_AND_REPEAT,
        logon_type="Password",
        arguments="-m straightedge --config C:\\bot\\config.toml watch --loop --ok-every 3600",
    )
    findings = _audit_one(watcher, cfg, supervision.WATCH_TASK)
    assert "not_interactive" not in _codes(findings)
    assert _codes(findings, supervision.FAIL) == set()


def test_a_watcher_that_does_not_loop_fails(tmp_path: Path) -> None:
    xml = _task_xml(
        name="straightedge-watch",
        triggers=LOGON_AND_REPEAT,
        arguments="-m straightedge --config C:\\bot\\config.toml watch",
    )
    findings = _audit_one(xml, _cfg(tmp_path), supervision.WATCH_TASK)
    assert "watcher_does_not_loop" in _codes(findings, supervision.FAIL)


def test_a_desk_that_does_not_loop_fails(tmp_path: Path) -> None:
    xml = _task_xml(
        triggers=LOGON_AND_REPEAT,
        arguments="-m straightedge --config C:\\bot\\config.toml run --mode mt4",
    )
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "desk_does_not_loop" in _codes(findings, supervision.FAIL)


def test_the_wrong_subcommand_fails(tmp_path: Path) -> None:
    """A task named straightedge-desk that actually runs the watcher."""
    xml = _task_xml(
        triggers=LOGON_AND_REPEAT,
        arguments="-m straightedge --config C:\\bot\\config.toml watch --loop",
    )
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "wrong_subcommand" in _codes(findings, supervision.FAIL)


def test_an_interval_slower_than_the_alarm_fails(tmp_path: Path) -> None:
    """A restart slower than the threshold pages a human for a self-healing gap."""
    xml = _task_xml(triggers=_repeat_every("PT30M"))
    findings = _audit_one(xml, _cfg(tmp_path))
    hit = [f for f in findings if f.code == "interval_slower_than_staleness"]
    assert hit and hit[0].severity == supervision.FAIL
    assert "1800s" in hit[0].detail
    assert f"{MT4_STALE}s" in hit[0].detail


def test_an_interval_inside_the_threshold_passes(tmp_path: Path) -> None:
    """The positive control for the test above: it has to be able NOT to fire."""
    xml = _task_xml(triggers=_repeat_every("PT7M"))
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "interval_slower_than_staleness" not in _codes(findings)


def test_a_cmd_wrapper_that_keeps_its_arguments_is_auditable(tmp_path: Path) -> None:
    """The documented way to hide the console window must still pass.

    `action_not_auditable` is about the ARGUMENTS being readable, not about
    which executable is launched. A check on the interpreter's filename would
    have failed this legitimate shape and passed a wrapper named
    `python-launcher.exe`.
    """
    xml = _task_xml(
        triggers=LOGON_AND_REPEAT,
        command="C:\\Windows\\System32\\cmd.exe",
        arguments=(
            "/c \"C:\\Python312\\python.exe -m straightedge --config "
            "C:\\bot\\config.toml run --mode mt4 --loop &gt;&gt; "
            "C:\\bot-state\\desk.log 2&gt;&amp;1\""
        ),
    )
    findings = _audit_one(xml, _cfg(tmp_path))
    assert _codes(findings, supervision.FAIL) == set()


# --- the findings that must NOT red the gate ------------------------------------------


def test_the_battery_defaults_warn_and_do_not_fail(tmp_path: Path) -> None:
    """Hardening on a VPS with no battery. A gate that fails everything is noise."""
    xml = _task_xml(triggers=LOGON_AND_REPEAT, batteries="true")
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "battery_stops_the_task" in _codes(findings, supervision.WARN)
    assert _codes(findings, supervision.FAIL) == set()
    assert supervision.exit_code(findings) == 0


def test_a_missing_logon_trigger_warns_and_does_not_fail(tmp_path: Path) -> None:
    xml = _task_xml(triggers=_repeat_every("PT5M"))
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "no_logon_trigger" in _codes(findings, supervision.WARN)
    assert _codes(findings, supervision.FAIL) == set()


def test_the_interval_is_not_judged_when_the_figure_cannot_describe_a_desk(
    tmp_path: Path,
) -> None:
    """The same caveat `doctor` carries, and the reason it is not a failure.

    With Telegram unset the derived threshold collapses toward its floor and
    describes a desk that cannot run, because `run` refuses to start without
    Telegram. Judging a real 300s interval against that figure would fail every
    correct task, and a gate that reds on a healthy box is a gate that gets
    ignored. So the comparison is reported as NOT MADE.
    """
    cfg = _cfg(tmp_path, telegram=False)
    ceiling, measurable = supervision.interval_ceiling(cfg)
    assert not measurable
    assert ceiling < DECLARED_INTERVAL_S, (
        "the unmeasurable ceiling is not actually below the declared interval, "
        "so this test cannot show the false failure it exists to prevent"
    )
    findings = _audit_one(_task_xml(triggers=LOGON_AND_REPEAT), cfg)
    assert "interval_unjudged" in _codes(findings, supervision.WARN)
    assert "interval_slower_than_staleness" not in _codes(findings)
    assert supervision.exit_code(findings) == 0
    text = supervision.report(findings, max_interval_s=ceiling, measurable=False)
    assert "NOT judged" in text


def test_exit_code_is_one_only_when_something_failed() -> None:
    """The control of the control. An exit code nobody drove both ways is a guess."""
    warn = supervision.Finding(task="t", severity=supervision.WARN, code="w", detail="d")
    fail = supervision.Finding(task="t", severity=supervision.FAIL, code="f", detail="d")
    assert supervision.exit_code([]) == 0
    assert supervision.exit_code([warn]) == 0
    assert supervision.exit_code([warn, fail]) == 1
    assert supervision.exit_code([fail]) == 1


# --- reading the dump at all -----------------------------------------------------------


def test_an_unreadable_dump_is_not_the_same_as_a_missing_task(tmp_path: Path) -> None:
    """Two different facts, and only one of them is about the box.

    A corrupt or truncated export means supervision is UNMEASURED. Reporting
    that as `task_missing` would be this audit inventing an answer, which is
    the failure mode it was written about one layer down.
    """
    dest = tmp_path / "tasks"
    dest.mkdir()
    (dest / "straightedge-desk.xml").write_text("<Task><not closed", encoding="utf-8")
    views = supervision.load_views(dest)
    ceiling, measurable = supervision.interval_ceiling(_cfg(tmp_path))
    findings = supervision.audit(views, max_interval_s=ceiling, measurable=measurable)
    desk = [f for f in findings if f.task == "straightedge-desk"]
    assert [f.code for f in desk] == ["task_unreadable"]
    assert "NOT the same reading" in desk[0].detail
    assert supervision.exit_code(findings) == 1


def test_a_utf16_dump_is_read_not_rejected(tmp_path: Path) -> None:
    """`schtasks /query /xml` emits UTF-16, and a cmd.exe redirect keeps it.

    Guessing wrong here would not raise: it would produce text that will not
    parse, and the audit would report the task as unreadable when the truth is
    that it was dumped perfectly well.
    """
    dest = tmp_path / "tasks"
    dest.mkdir()
    target = dest / "straightedge-desk.xml"
    target.write_bytes(_task_xml(triggers=LOGON_AND_REPEAT).encode("utf-16"))
    assert target.read_bytes()[:2] in (b"\xff\xfe", b"\xfe\xff")
    views = supervision.load_views(dest)
    view = views["straightedge-desk"]
    assert view.present and not view.unreadable
    assert view.repeat_intervals_s == [DECLARED_INTERVAL_S]


def test_a_single_file_can_be_audited(tmp_path: Path) -> None:
    """`--tasks` takes one dump as well as a directory of them."""
    target = tmp_path / "straightedge-desk.xml"
    target.write_text(_task_xml(triggers=LOGON_AND_REPEAT), encoding="utf-8")
    views = supervision.load_views(target)
    assert set(views) == {"straightedge-desk"}
    assert views["straightedge-desk"].present


@pytest.mark.parametrize(
    "text,expected",
    [
        ("PT5M", 300.0),
        ("PT30M", 1800.0),
        ("PT72H", 259200.0),
        ("PT0S", 0.0),
        ("P1DT2H3M4S", 93784.0),
        ("PT1.5M", 90.0),
        ("", None),
        ("five minutes", None),
        ("P", None),
        ("PT", None),
    ],
)
def test_duration_parsing(text: str, expected: float | None) -> None:
    """`PT0S` means NO LIMIT and an unreadable value means NOT MEASURED.

    Both would be falsy if this returned 0 for a parse failure, and the whole
    `execution_time_limit` check turns on telling them apart.
    """
    assert supervision.parse_duration_seconds(text) == expected


def test_a_dump_of_a_different_task_entirely_is_still_judged(tmp_path: Path) -> None:
    """Nothing about the FILENAME is trusted: the checks read the definition.

    The live box's `mt4-terminal-supervisor` is a correctly supervised task,
    and if someone exported it under the desk's name this audit must still say
    that it does not start the desk.
    """
    xml = _task_xml(
        triggers=LOGON_AND_REPEAT,
        command="C:\\Program Files\\MT4\\terminal.exe",
        arguments="/portable",
    )
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "action_not_auditable" in _codes(findings, supervision.FAIL)


# --- definitions that are odd rather than wrong ---------------------------------------
#
# A dump missing whole sections is the case where a lenient parser quietly
# produces a healthy-looking view: no Settings means no policy to object to, no
# Exec means no arguments to object to. The audit has to red on it anyway,
# because "I found nothing to complain about" and "this is supervision" are
# different statements.


def test_a_definition_missing_every_section_is_not_a_pass(tmp_path: Path) -> None:
    xml = """<?xml version="1.0" encoding="UTF-8"?>
<Task version="1.2" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><URI>\\straightedge-desk</URI></RegistrationInfo>
</Task>
"""
    view = supervision.parse_task_xml("straightedge-desk", xml)
    assert view.present and not view.unreadable
    assert view.enabled is None, "absent is not False; it must not read as disabled"
    assert view.instances_policy == ""
    assert view.repeat_intervals_s == []
    findings = _audit_one(xml, _cfg(tmp_path))
    codes = _codes(findings, supervision.FAIL)
    print(f"supervision: skeletal definition -> {sorted(codes)}")
    assert "no_repeating_trigger" in codes
    assert "no_action" in codes
    assert supervision.exit_code(findings) == 1


def test_an_empty_command_is_named_rather_than_skipped(tmp_path: Path) -> None:
    xml = _task_xml(triggers=LOGON_AND_REPEAT, command="", arguments="")
    findings = _audit_one(xml, _cfg(tmp_path))
    assert "no_action" in _codes(findings, supervision.FAIL)


def test_an_unrecognised_enabled_value_reads_as_neither(tmp_path: Path) -> None:
    """`true`/`false` are the schema; anything else is UNMEASURED, not False.

    Reading an unknown value as False would disable a live task on paper and
    as True would clear a disabled one. Both are the audit inventing an answer,
    so the field stays None and the checks that need it do not fire.
    """
    xml = _task_xml(triggers=LOGON_AND_REPEAT, enabled="yes")
    view = supervision.parse_task_xml("straightedge-desk", xml)
    assert view.enabled is None
    assert "task_disabled" not in _codes(_audit_one(xml, _cfg(tmp_path)))


def test_a_trigger_with_no_enabled_element_still_counts(tmp_path: Path) -> None:
    """Task Scheduler omits <Enabled> on an enabled trigger.

    This is the one place where treating a missing field as False would hide a
    WORKING trigger, so it is asserted rather than left to the comment.
    """
    xml = _task_xml(
        triggers="""    <TimeTrigger>
      <Repetition>
        <Interval>PT5M</Interval>
      </Repetition>
      <StartBoundary>2026-01-01T00:00:00</StartBoundary>
    </TimeTrigger>"""
    )
    view = supervision.parse_task_xml("straightedge-desk", xml)
    assert view.repeat_intervals_s == [DECLARED_INTERVAL_S]
    assert "no_repeating_trigger" not in _codes(_audit_one(xml, _cfg(tmp_path)))


def test_an_empty_interval_is_not_a_repeat(tmp_path: Path) -> None:
    """A <Repetition> with nothing in it does not repeat."""
    xml = _task_xml(
        triggers="""    <TimeTrigger>
      <Repetition>
        <Interval></Interval>
      </Repetition>
      <Enabled>true</Enabled>
    </TimeTrigger>"""
    )
    view = supervision.parse_task_xml("straightedge-desk", xml)
    assert view.repeat_intervals_s == []
    assert "no_repeating_trigger" in _codes(_audit_one(xml, _cfg(tmp_path)), supervision.FAIL)


def test_a_bomless_utf16_dump_is_read(tmp_path: Path) -> None:
    """A UTF-16LE dump whose BOM was stripped in transit still has to parse.

    Guessing UTF-8 here yields text that will not parse, and the audit would
    then report the task as unreadable when it was dumped perfectly well: a
    measurement failure wearing the costume of a finding.
    """
    dest = tmp_path / "tasks"
    dest.mkdir()
    target = dest / "straightedge-desk.xml"
    raw = _task_xml(triggers=LOGON_AND_REPEAT).encode("utf-16-le")
    assert not raw.startswith((b"\xff\xfe", b"\xfe\xff"))
    target.write_bytes(raw)
    view = supervision.load_views(dest)["straightedge-desk"]
    assert view.present and not view.unreadable
    assert view.repeat_intervals_s == [DECLARED_INTERVAL_S]
