# Windows supervision: the two tasks that keep the desk alive

The desk owns halt, daily-loss, drawdown, stop management and every refusal
gate, and it is the INITIATOR in this architecture (`docs/TRANSPORT.md`): the
Expert is a transport client, so the gates live in the bot's process and on the
bot's clock. **A dead desk is therefore not a paused desk.** Open positions keep
running with no stop management, the circuit cannot trip because nothing is
evaluating it, working orders on the book can still fill with nothing watching,
and the only human-visible signal is the ABSENCE of the nightly recap.

That is why there are two tasks and not one, and why their definitions are in
this directory instead of in a runbook paragraph.

## What went wrong, measured

Measured on the live Windows box on 2026-10-08, by `Get-ScheduledTask` /
`Get-ScheduledTaskInfo`. The box's hostname and address are deliberately not
written down here: this repo is public, that box holds a real-money desk, and
the finding is the trigger table, not the address.

| task | triggers | RestartCount | NextRunTime | LastRunTime |
| --- | --- | --- | --- | --- |
| `mt4-terminal-supervisor` | Logon + Time | 0 | about every 2 min | that day |
| `straightedge-desk` | **Logon only** | 0 | **none** | 2026-09-26 01:56 |
| `straightedge-watch` | **did not exist** | | | |

The MT4 terminal, which holds no risk state, was supervised every two minutes.
The desk, which holds all of it, had run once in twelve days. The watchdog
reader shipped in PR#80 and nothing ran it.

`docs/RUNBOOK.md` had documented the correct arrangement the whole time. **That
is the actual finding: the procedure was prose, an operator typed it once, and
nothing ever compared the result to the instruction again.** A runbook is not a
control. The control is `python -m straightedge supervision`, which reads the
live definitions and goes red.

## The files

| file | what it is | does it change the box |
| --- | --- | --- |
| `straightedge-desk.xml` | declared definition of the restart task | no |
| `straightedge-watch.xml` | declared definition of the watcher task | no |
| `Export-Tasks.ps1` | dumps the LIVE definitions AND their liveness sidecars for the audit | no, read-only |
| `Install-Supervision.ps1` | registers both from XML, after recording what was there | **yes** |
| `Deploy-Desk.ps1` | the git deploy executor, see [`docs/DEPLOY.md`](../../docs/DEPLOY.md) | **yes, with `-Apply`** |
| `assert-config-loads.py` | validates a config through the LOADER before a restart | no, read-only |

`python -m straightedge supervision --tasks <dir>` is the audit. With no
`--tasks` it audits the DECLARED definitions in this directory, which is what
CI does; point it at `Export-Tasks.ps1` output to audit the live box.

## The declaration is not the behaviour (#151)

`Export-Tasks.ps1` writes TWO files per task: `<task>.xml`, which is what the
task is DECLARED to be, and `<task>.info.json`, which is what Task Scheduler
says it will actually DO next. Both are read-only queries.

The second one exists because the first one is not enough, and that was
measured rather than reasoned. On 2026-09-26 the desk's definition carried a
`PT5M` repetition on its `LogonTrigger`; every other field was correct, and the
audit returned **zero findings and exit 0** while that desk had not restarted
in twelve days. A repetition that will never fire and a working cadence are the
same XML.

So the audit now has two instruments that fail independently:

| reading | source | catches |
| --- | --- | --- |
| trigger TYPE under the repetition | the XML alone | the shape that actually happened: a cadence hung only on an event trigger |
| `NextRunTime` | `<task>.info.json` | the shapes XML cannot settle: a future `StartBoundary`, an expired `EndBoundary`, an elapsed `Duration`, a task the live system has disabled |

**A dump of a real task with no `.info.json` is a FAILURE** (`liveness_unmeasured`),
not a pass, by the same rule a corrupt dump already follows. If you audit an
export taken before this change, re-run `Export-Tasks.ps1`. The templates in
this directory are exempt, because they are registered with nothing and carry
`REPLACE_ME`; the audit says which question it answered on its `liveness:` line.

### Why a non-zero `LastTaskResult` is not a failure

The desk's task reports `LastTaskResult = 0x800710E0` on a healthy box. The
5-minute trigger fires, finds the desk already running, and
`MultipleInstances=IgnoreNew` refuses the duplicate launch. **For a task whose
job is "start it if it is not running", a refused launch IS the healthy steady
state**, and a zero would mean it had just started a fresh instance.

`mt4-terminal-supervisor` reports `0` for the opposite reason: its action is a
short probe that exits cleanly, so it is never the already-running case. Same
family of task, opposite healthy value. So the audit judges the TUPLE
(`State`, `LastTaskResult`, `NextRunTime`, `MultipleInstances`) and never one
field: `Running` plus `0x800710E0` plus a populated `NextRunTime` is healthy,
and any other non-zero result is a real failure.

## Install

```bat
cd deploy\windows
powershell -ExecutionPolicy Bypass -File .\Install-Supervision.ps1 ^
  -PythonExe "C:\Program Files\Python312\python.exe" ^
  -ConfigPath "C:\bot\config.toml" ^
  -WorkingDirectory "C:\bot"
```

That is a DRY RUN; it changes nothing and says so. Add `-Apply` to register.
Then verify, do not assume:

```bat
powershell -ExecutionPolicy Bypass -File .\Export-Tasks.ps1 -OutDir .\tasks
"C:\Program Files\Python312\python.exe" -m straightedge --config C:\bot\config.toml supervision --tasks .\tasks
```

Exit 0 means every declared task is supervision. Non-zero names each failure.

### Ordering, and what a deploy stops

Registering a task does not start it, so installing these two does not stop the
desk and does not stop the MT4 terminal. The desk holds an exclusive
`journal.lock` and the terminal is a separate supervised process; neither is
touched here. **Stopping and starting the desk safely is the git-deploy
question, and it is a separate issue, not this one.**

While the desk is up, the repeating trigger is a no-op for two independent
reasons, and both are wanted:

1. `MultipleInstancesPolicy` is `IgnoreNew`, so Task Scheduler will not start a
   second instance of a task that is already running.
2. `journal.lock` (`src/straightedge/journal.py`) makes a second desk exit 2
   with `already running` before it touches MT4 or Telegram.

**Reason 1 only holds if the process the task launches stays alive for the
desk's lifetime.** A wrapper that launches python and returns immediately makes
Task Scheduler mark the task complete, and then the instance policy protects
nothing and `journal.lock` is the only barrier. That is survivable, but it is
not what the runbook claims, so do not introduce such a wrapper without
changing the claim.

## Why each setting is the way it is

**`MultipleInstancesPolicy` `IgnoreNew`.** The trigger fires every five minutes
forever, including while the desk is healthy, and this is what makes that a
no-op. `StopExisting` would make this task END the healthy desk on every
interval: the thing installed to keep the desk alive would be the thing killing
it. `Parallel` would leave `journal.lock` as the only barrier.

**`ExecutionTimeLimit` `PT0S`, meaning indefinite.** `schtasks /create` without
`/xml` defaults this to `PT72H`, so a hand-created task ends a perfectly
healthy desk three days in, mid-session, and nothing an operator looks at
explains it. This is the single most likely way to reintroduce
"the bot just died mid trading day" while believing it is supervised.

**`LogonType` `InteractiveToken`.** MT4 is a GUI program and only a task in the
logged-in session can reach it. A task set to run whether the user is logged on
or not restarts a desk that cannot see the terminal, which comes back and
refuses every order: a worse failure than staying down, because it looks alive.

**The interval is derived, never chosen.** It must be at or under the desk's own
staleness threshold, which `python -m straightedge doctor` prints on every run
and which `watchdog.stale_after_seconds` computes from `poll_seconds`, the
Telegram retry ceiling and the venue `timeout_ms`. `PT5M` (300s) is under the
428s the shipped example config yields. A restart slower than the threshold
means the gap is ALARMED before it is CLOSED, so the operator is paged for an
outage the box was about to fix, and `#68` is the standing reminder of what an
instrument-blind constant costs. The audit fails a task whose interval exceeds
the threshold derived from the config it was given.

**No `--i-accept-risk`, ever (fc34).** A scheduled task re-runs its arguments on
every restart, so that flag would re-arm a real-money desk after every crash
with nobody there. Arming is per process by design: the desk comes back ALIVE
and DISARMED, and a human sends `/live on I-ACCEPT-RISK` in the locked chat. The
audit fails a task that carries the flag.

**The command line is in the task definition, not in a wrapper.** The audit
fails a task whose action does not name the `straightedge` module anywhere,
under the code `action_not_auditable`, and that is a failure rather than a
warning on purpose: when the real arguments are inside a script the audit never
sees, two invariants cannot be checked AT ALL, namely that the task starts the
desk and that the risk-acceptance flag is absent from it. An invariant that
cannot be read has not been met.

The cost is a visible console window in the interactive session, because a
console program cannot be both windowless and output-capturing on Windows
without a wrapper. If the window has to go, wrap with `cmd.exe` and keep the
arguments in the definition:

```
Command:   C:\Windows\System32\cmd.exe
Arguments: /c "C:\path\python.exe -m straightedge --config C:\bot\config.toml run --mode mt4 --loop >> C:\bot-state\desk.log 2>&1"
```

That stays auditable (every argument is still in the XML), keeps stderr, and
`cmd /c` waits for the child so the instance policy stays load-bearing.

**`--ok-every 3600` on the watcher is an operator CHOICE, not a measurement.**
It sends one healthy message an hour, which is what makes the watcher's own
death observable: nothing on the box can see the watcher die, so silence has to
mean something. Setting it to 0 gives that up.

## What this does and does not fix

It fixes: a desk that exited comes back inside a bounded, stated interval; a
restart reaches Telegram even when nothing else about the desk changed (the
heartbeat carries `run_id`, see `src/straightedge/watchdog.py`); a crash loop is
counted and named rather than smoothed over; and the arrangement is now
auditable from outside by a command that can go red.

It does not fix a desk whose process is alive and whose ticks have stopped. The
watchdog reports that as `STALE` and deliberately refuses to kill anything:
proving alive-but-stalled apart from dead needs `journal.lock`, and a watcher
that can hold that lock for even a moment can make a restarting desk exit
`already running`, which is a watchdog that can kill the desk. So a stalled desk
is ALARMED, not restarted, and a human decides. That boundary is PR#80's and
this change does not move it.

## One piece of Windows trivia, recorded so nobody re-derives it

The files declare `encoding="UTF-8"` and their bytes are UTF-8, which is what
`schtasks /create /xml <file>` needs. `Register-ScheduledTask -Xml` is a
different route: it takes an in-memory string, which is UTF-16, and the Task
Scheduler parser reads the declaration, disagrees with the bytes it was handed,
and fails with

```
The task XML is malformed.  (1,40)::ERROR: unable to switch the encoding
```

Declaring UTF-16 would fix that route and break the file route. So
`Install-Supervision.ps1` strips the declaration before it registers, which is
valid XML either way. Caught by the `supervision-xml` CI job on its first run,
which is the whole argument for that job existing: nothing on a Mac could have
found it, and the alternative place to find it was the live box.

## Status of these definitions

The XML parses, and `python -m straightedge supervision --tasks deploy/windows`
passes it (CI runs that on every push). **Whether Task Scheduler ACCEPTS it has
to be proven by registering it**; the CI job `supervision-xml` does exactly that
on a `windows-latest` runner, registers both declarations, exports them back and
audits the export. Until that job has run green, treat the schema as
unvalidated and say so rather than assuming it.

## Prove it works before leaving it

On the DEMO account, once, per `docs/RUNBOOK.md`:

1. Install both tasks. Confirm `supervision` exits 0 against the live export.
2. Wait for the first `watch` message in the chat.
3. End the desk process in Task Manager.
4. Wait one interval.
5. Read the chat. You get `RESTARTED`, with a restart count, and the state.
6. On a real account you also get `ALIVE NOT TRADING (live_not_accepted)`:
   that is the desk saying it came back disarmed. Send
   `/live on I-ACCEPT-RISK`.

A watchdog you have never seen fire is not a watchdog.
