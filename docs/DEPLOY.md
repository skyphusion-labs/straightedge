# Deploying the desk from git

Conrad's requirement, in his words: "I wanted the deployment method of this bot
on the box to be via git."

The box is already a git checkout, so this is not a conversion. This document is
the decision record for the missing part: a controlled, repeatable, reversible
UPDATE procedure, and an answer to "what is running" that does not require
logging into the box. `deploy/windows/Deploy-Desk.ps1` is the executor.

## Scope, and three things deliberately outside it

**In scope:** what a deploy stops and starts and in what order, what exactly
gets deployed, how a bad one is backed out, how the running version becomes
observable, and how the live journal is protected.

**Not the decision to deploy.** That is `straightedge#143`, it is assigned to
Conrad, it carries the risk register, and this document does not re-argue it.
Its recommendation is the constraint this procedure is built around: deploy
after `#133` gives a supervisor to catch a bad start, in a market-closed window.

**Not the Expert.** Ruled by the architecture owner of `#73` on 2026-10-08:
**a desk deploy does not touch `mt4/Experts/Mt4RiskBot.mq4`**, explicitly and
not by omission. Three reasons for the record: the deployed Expert is AHEAD of
the deployed desk, so leaving it alone CLOSES the `#142` skew rather than
opening one; it is the only artifact a customer installs and `docs/MT4.md` is
its ICD, so changing it has a different blast radius from updating a Python
tree; and `mt4-compile` has its own supply-chain gate on the installer pin, so
the binary could not be rebuilt on demand anyway.

**Not a version handshake.** `#142` states the boundary: "Not an argument for a
version handshake. That is a design question nobody has asked for, the cost of
getting it wrong is a desk that refuses to start." What this adds is
OBSERVABILITY. Nothing refuses to start over a version, nothing compares the
desk to the Expert and gates on the answer.

## What gets deployed

**An immutable ref, and this repo's convention for that is a tag.**

`main` moves. "The box is on main" is not a statement about what is running, it
is a statement about a moving reference, and that is exactly how twelve days of
staleness went unnoticed. A branch is worse, because it can be force-pushed. A
tag is one tree, forever, and it is the thing a handover document can name.

Measured 2026-10-08, which is why this needed saying rather than assuming:
tagging had lapsed after `v1.1.2`, which was **135 commits** behind `main` and
contained **none** of `#84`, `#94`, `#102`, `#105`, `#107` or `#109`. Deploying
the newest tag would have been a downgrade. `v1.6.0` exists for this reason.

**The exact 40-character OID is the precise answer, and it is what the stamp
records.** `v1.6.0` collapses 640 changelog lines and 18 sections, several
independently load-bearing: `#37`'s money-safety fixes on the send path,
`#73`'s transport, `#38`'s watchdog, `#133`'s supervision. "The box is on
1.6.0" cannot distinguish "has the send fence" from "has supervision", because
both are inside it. So the tag is for a human conversation and the OID is for
precision, and the procedure records both.

The interim is a real option and not a fallback: when no tag names the tree you
want, deploy a recorded full OID of `main`. Never the branch name.

## The live journal must not be clobbered

**REQUIREMENT, not an observation: the state directory is a SIBLING of the
checkout and never a child.**

Measured on the box: `C:\bot-state` beside `C:\bot`. That directory holds
`journal.jsonl`, the inflight ledger, the equity snapshot, the Telegram offset
and the heartbeat, which together are the audit log of a real-money desk.

The consequence if it were ever inside the checkout: this procedure performs a
hard detached checkout inside the tree, so the audit log of every trade the
desk has ever made would be deleted, and no other step would notice.
`Deploy-Desk.ps1` refuses to run in that configuration, and CI watches that
refusal fire on every push, because a refusal nobody has watched is not a
refusal.

**`git clean` is a command this procedure never runs.** The sibling layout
protects the state directory; nothing protects an untracked file inside the
worktree, and `config.toml` is untracked. `git clean -xd` would delete the
operator's configuration and the procedure would report success.

## The BOM trap, which cost a live outage by two minutes

Measured live on 2026-10-08, on the real-money box, during the hand deploy that
preceded this document:

An edit made with Windows PowerShell 5.1's `Set-Content -Encoding UTF8` wrote a
UTF-8 BOM. The file looked correct in an editor and `Get-Content` returned
exactly the expected text, because a BOM is invisible to both. `tomllib` refused
it with `Invalid statement (at line 1, column 1)`, naming neither the cause nor
the remedy, and **the desk would not have started at its next restart.** It was
caught by validating through the loader before restarting, and reverted from a
backup.

Three things follow, and all three are implemented:

1. **Validate through the LOADER, never by reading the file back.** Reading it
   back is the check that cannot see this defect.
   `deploy/windows/assert-config-loads.py` is that check: it loads the config
   the way the desk does, prints the figures the desk will believe, and exits
   non-zero when the desk could not read it. It has no side effects, so it is
   safe against a live autonomous desk.
2. **Anything this repo writes writes without a BOM.** `Deploy-Desk.ps1` uses
   `[System.IO.File]::WriteAllText` with `UTF8Encoding($false)` and never
   `Set-Content -Encoding UTF8`.
3. **The loader tolerates a BOM and says so.** `config.py` decodes
   `utf-8-sig` and warns on stderr naming the remedy. A pure encoding artifact
   must not be able to refuse a config whose meaning is unchanged, on a box
   that trades money.

## What a deploy stops and starts, and why the first step is not the obvious one

`deploy/windows/Deploy-Desk.ps1` executes this. The numbering matches the
script.

**0. Preflight.** git, python, the checkout, the config and the state directory
all exist, and the state directory is not inside the checkout.

**1. Record the rollback point, before anything changes.** The full 40-character
OID of the current HEAD, the Expert's sha256, and whether the worktree was
dirty, written to `C:\bot-state\deploys\rollback-<utc>.json`. A rollback is only
a rollback if the previous state was captured BEFORE the change rather than
reconstructed after it. The Expert's hash is recorded alongside because this
estate has already split a deployment across two artifacts and lost track of
which was where (`#142`); a rollback that restored one and not the other would
recreate that skew.

**2. Report the preconditions and then STOP.** The script refuses to continue
without `-BookIsFlat`. It checks and refuses outright on an open inflight
entry, because an open entry is the desk saying it does not know whether an
order moved money, and deploying across one destroys the only process that
could still resolve it. Everything else at this gate is a human's call: the
script can report the ledger, it cannot decide that stopping a live autonomous
desk right now is acceptable.

**3. DISABLE THE TRIGGERS. Not just the process.** This is the step that is
easy to get wrong, and it only exists because of `#133`.

> Supervision's repeating trigger fires on its own schedule regardless of what
> is being done to the working tree, so a `git checkout` with the task still
> enabled can start a desk on a HALF-UPDATED tree. Stopping the process is not
> enough.

And the current box makes it worse, measured 2026-10-08:
`C:\bot-state\run-desk-hidden.vbs` is one line,
`CreateObject("Wscript.Shell").Run "cmd /c ...", 0, False`. The wait flag is
**False**, so wscript returns immediately, Task Scheduler marks the task
complete, and `MultipleInstancesPolicy=IgnoreNew` protects nothing: the only
barrier against a second desk is `journal.lock`.

`straightedge-watch` goes down with it, or it pages the locked chat about an
outage being caused on purpose.

**4. Stop the desk** and confirm no matching process remains. The script
refuses to change the tree underneath a surviving desk.

**5. Move the tree,** detached at an exact commit, after a `fetch --tags`. HEAD
names a commit and not a branch. `git clean` is not run.

**6. Reinstall, then validate.** `pip install -e .`, because the package IS the
working tree. Then `assert-config-loads.py` must exit 0, then `doctor` must
exit 0. `doctor` is already the documented pre-run gate and carries the
per-symbol history preflight, which is what catches a symbol that cannot
produce a signal.

**7. Stamp it.** `journal.deployed.json` beside the journal, carrying the ref,
the full OID and the time, written without a BOM.

**8. Re-enable supervision, and PROVE it is enabled.** Not "enable and assume":
the script reads each task back and fails if `Settings.Enabled` is not true.
**A deploy that leaves supervision disabled returns the box to exactly the
state `#133` existed to fix, and it does so silently.** That is the worst
outcome this procedure could produce, so it is the one thing it asserts twice.

**9. Verify from outside.** Export the live tasks and run the supervision audit;
read `deployed=` in the heartbeat; read `/status` in the chat. Then re-arm.

**The desk comes back DISARMED, by design (fc34).** If `auto` was armed before
the deploy it is NOT armed after. A human sends `/live on I-ACCEPT-RISK`, and
arms auto, from the locked chat. Never from a task argument.

### Rollback

`Deploy-Desk.ps1 -Rollback <record> -BookIsFlat -Apply`. Same ordered steps,
with the recorded OID as the target. It is a rollback only because step 1
happened.

## How the running version becomes observable

Before this: `__version__` was printed by `doctor` and by nothing else, and
`Engine.status_text` carried halt state, mode, server, equity, balance, peak,
positions and risk, and no version at all. Nobody could tell from the chat that
the box was 25 commits behind for twelve days.

Now the deploy writes `journal.deployed.json` and the desk publishes it in two
places, because they have different readers:

- **`/status`** answers a person in the locked chat.
- **`journal.heartbeat`**, as `deployed=`, answers a process on the box, which
  is what lets a deploy verify itself without going through Telegram.

`src/straightedge/deployed.py` reads it. The desk never derives this itself:
shelling out to `git` would make git a runtime dependency of a real-money desk
for a diagnostic, and reading `.git/HEAD` would couple the desk to a layout
this document does not promise.

**`unstamped` is an answer, not a blank.** A desk with no stamp says
`deployed=unstamped` and never falls back to `__version__`, because a
hand-maintained version that spans 18 changelog sections would be a confident
wrong answer in place of an honest absent one. A corrupt stamp, a stamp that is
valid JSON but not an object, and a stamp with no commit all read as
`unstamped` too: a ref that names no tree must not look like an answer.

## Installing supervision on a desk that is already running

This is the case the box is actually in: live, unattended, autonomous on gold,
with `#146` merged and NOT installed. The install must not be the outage.

**Registering a scheduled task does not start it and does not touch the running
process.** That is the property that makes this safe at all. Two risks remain
and the order below is chosen to bound them.

**The risk that matters: a restart silently disarms an armed desk.** Arming is
per process by design, so if anything restarts that desk it comes back ALIVE
and NOT TRADING, and the only immediate signal is the desk's own `start` line in
the chat. An autonomous desk that has quietly stopped trading looks identical to
a quiet market. So the install must not restart the desk, and the procedure
verifies that by comparing the process id before and after.

**Install the observer before the thing being observed:**

1. Export the current task definitions first (`Export-Tasks.ps1`). That is the
   rollback record for the task layer.
2. Record the desk's process id.
3. Register **`straightedge-watch` first.** It is read-only with respect to the
   desk: it never takes `journal.lock` and never calls `getUpdates`, so it
   cannot restart the desk and cannot steal its commands. It gives an
   independent observer before anything else changes.
4. Read the chat. `watch` always announces its first observation, so a message
   should arrive, and on the armed box it should read `ALIVE ARMED`. **That
   message is the install being verified**, not noise.
5. Register `straightedge-desk`. `Register-ScheduledTask -Force` replaces the
   wrapper-based definition.
6. **Confirm the desk's process id is UNCHANGED.** If it moved, the desk
   restarted and is now disarmed; re-arm from the chat before walking away.
7. Run the supervision audit against a fresh export. Expect exit 0.

**The restart drill costs a re-arm, so it is the operator's call when to run
it.** `docs/RUNBOOK.md` says to prove the watchdog once by ending the desk in
Task Manager. On this box that is a demo account, so the drill is appropriate,
but it will disarm auto and a human has to re-arm. Do it deliberately, not as
part of the install.

## What is NOT verified

**`Deploy-Desk.ps1` has never performed a real deploy.** CI exercises its dry
run against a real git checkout on `windows-latest`, proves the dry run changes
nothing, and watches it refuse a state directory inside the checkout. None of
that exercises the task disable and enable, the checkout, the reinstall or the
stamp write, because those need a desk to stop. **The first real run should be
watched by a person, on the demo account, in a market-closed window**, and the
rollback record from step 1 is what makes that acceptable.

The BOM trap, the sibling-directory requirement and the trigger-disable step are
each written down because somebody hit them, not because they were predicted.
