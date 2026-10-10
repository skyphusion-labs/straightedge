# Deploy the desk from git, in order, reversibly, and say what landed.
#
# Read docs/DEPLOY.md first. It is the decision record; this is the executor.
#
# WHAT THIS IS AND IS NOT. It is a checklist executor with mandatory stops, not
# an automated deploy. The mechanical, skippable, easy-to-get-wrong steps are
# automated here precisely because they are the ones that get skipped under
# pressure: recording the rollback point BEFORE anything changes, disabling the
# repeating triggers so supervision does not restart the desk onto a
# half-updated tree, validating the config through the LOADER, writing the
# stamp, re-enabling the triggers and PROVING they are enabled. The judgement
# steps are not automated and the script refuses to pass them without an
# explicit flag, because this box holds a real-money autonomous desk and a
# deploy that half-lands is worse here than one that is slow.
#
# Two gates, both deliberate:
#   -Apply       nothing is changed without it. The default run is a dry run.
#   -BookIsFlat  the operator's assertion that the book is flat or deliberately
#                held and that this is a market-closed window. The script can
#                REPORT the book; it cannot decide that stopping the desk now is
#                acceptable, and pretending otherwise would be the script taking
#                a decision it has no standing to take.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $Ref,
    [string] $Repo = "C:\bot",
    [string] $StateDir = "C:\bot-state",
    [string] $ConfigPath = "C:\bot\config.toml",
    [string] $PythonExe = "C:\Program Files\Python312\python.exe",
    [string] $RecordDir = "C:\bot-state\deploys",
    [string[]] $Tasks = @("straightedge-desk", "straightedge-watch"),
    [switch] $BookIsFlat,
    [switch] $Apply,
    [string] $Rollback,
    # The operator's assertion that a previous deploy which did not finish has
    # been dealt with. straightedge#274: the script now leaves a marker on disk
    # for the whole window in which it is changing the box, and refuses to start
    # another -Apply run over one. It cannot know that the half-landed state was
    # resolved; only a human who looked can say so.
    [switch] $PreviousDeployResolved
)

$ErrorActionPreference = "Stop"

# Say, Die, Invoke-Git, Write-Utf8NoBom, Assert-PowerShellSupported and the
# in-progress marker helpers live in DeskDeployLib.ps1, dot-sourced here.
#
# straightedge#274 is why they are not in this file. The function that aborted a
# live deploy mid-window was unreachable by any test, because the only way to
# reach it was to run a deploy. The guard that was supposed to prevent that
# abort, `$PSNativeCommandUseErrorActionPreference = $false`, exists only on
# PowerShell 7.3 and later and the box has 5.1, so it was present in the file
# and dead on the only shell the box has. It is GONE rather than gated: the fix
# is inside Invoke-Git, it needs no version test, and it is now watched going
# red in CI under Windows PowerShell 5.1, which is the shell the box runs.
$lib = Join-Path $PSScriptRoot "DeskDeployLib.ps1"
if (-not (Test-Path $lib)) {
    Write-Host "::error::no library at $lib; this script cannot run without it"
    throw "no library at $lib"
}
. $lib

Assert-PowerShellSupported -Version $PSVersionTable.PSVersion
Say "PowerShell edition : $($PSVersionTable.PSEdition)"

$git = "C:\Program Files\Git\cmd\git.exe"

# The library's Invoke-Git takes the git path and the repo explicitly, so that a
# test can drive it. This closes over the two this script uses.
function Invoke-RepoGit {
    param([string[]] $GitArgs)
    return (Invoke-Git -Git $git -Repo $Repo -GitArgs $GitArgs)
}

function Assert-TasksState {
    param([bool] $WantEnabled)
    # PROVE it, do not assume it. A deploy that leaves supervision disabled
    # returns the box to exactly the state straightedge#133 existed to fix, and
    # it does so silently, which is the whole failure mode.
    foreach ($name in $Tasks) {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($null -eq $t) {
            if ($WantEnabled) { Die "$name does not exist, so supervision is NOT in place after this deploy. Install it: deploy\windows\Install-Supervision.ps1" }
            Say "$name does not exist (nothing to disable)"
            continue
        }
        $isOn = [bool]$t.Settings.Enabled
        if ($isOn -ne $WantEnabled) {
            Die "$name Enabled=$isOn but this step requires Enabled=$WantEnabled"
        }
        Say "$name Enabled=$isOn as required"
    }
}

# ---------------------------------------------------------------- 0. preflight

if (-not (Test-Path $git)) { Die "no git at $git" }
if (-not (Test-Path $PythonExe)) { Die "no python at $PythonExe" }
if (-not (Test-Path $Repo)) { Die "no checkout at $Repo" }
if (-not (Test-Path $ConfigPath)) { Die "no config at $ConfigPath" }
if (-not (Test-Path $StateDir)) { Die "no state dir at $StateDir" }

# REQUIREMENT, not an observation. The state directory holds journal.jsonl, the
# inflight ledger, the equity snapshot and the heartbeat: the audit log of a
# real-money desk. It must be a SIBLING of the checkout and never a child,
# because this procedure does a hard checkout inside the checkout. If it were
# inside, a `git checkout` or a stray `git clean` would delete the audit log of
# every trade this desk has ever made, and no step below would notice.
$repoFull = (Resolve-Path $Repo).Path.TrimEnd('\') + '\'
$stateFull = (Resolve-Path $StateDir).Path.TrimEnd('\') + '\'
if ($stateFull.StartsWith($repoFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    Die "REFUSING: the state dir $StateDir is INSIDE the checkout $Repo. A hard checkout would destroy the live journal. Move it out before deploying; see docs/DEPLOY.md."
}
Say "state dir is a sibling of the checkout, not a child"

# ----------------------------- 0b. a previous deploy that never finished
#
# straightedge#274. The marker is written for the whole window in which this
# script is changing the box, and removed once step 8 has PROVED supervision is
# back on. Finding one means a previous run died inside that window, so the box
# may be running a tree nobody chose, with supervision off.
$markerPath = Get-DeployMarkerPath -RecordDir $RecordDir
$prior = Read-DeployMarker -Path $markerPath
if ($null -ne $prior) {
    Write-Host "::warning::a previous deploy recorded itself IN PROGRESS and never cleared the record:"
    Say "  marker        : $markerPath"
    Say "  written at    : $($prior.at)"
    Say "  phase reached : $($prior.phase)"
    Say "  shell / pid   : PowerShell $($prior.ps_version) / pid $($prior.pid)"
    Say "  before commit : $($prior.before_commit)"
    Say "  tree moved    : $($prior.tree_moved)"
    Say "  validated     : $($prior.validated)"
    Say "  rollback rec  : $($prior.rollback_record)"
    # A ROLLBACK IS LET THROUGH ON PURPOSE. Refusing the recovery tool over the
    # very state it recovers from is a lockout, and the standing guardrail here
    # is that a filter which can ban its own ingress path does not belong in
    # that path. A dry run is let through too, because it changes nothing and
    # refusing it would remove the operator's way to inspect.
    if ($Apply -and -not $Rollback -and -not $PreviousDeployResolved) {
        Die "REFUSING: the deploy above did not finish. Resolve it first (roll back with -Rollback $($prior.rollback_record), or read the tree, doctor and both tasks by hand), then re-run with -PreviousDeployResolved. See docs/DEPLOY.md."
    }
    if ($Apply) {
        $why = if ($Rollback) { "-Rollback was given" } else { "-PreviousDeployResolved was given" }
        Say "proceeding over the marker because $why"
    }
}

if (-not $Apply) { Say "DRY RUN. Nothing will be changed. Re-run with -Apply." }

# ---------------------------------------------------------------- rollback mode

if ($Rollback) {
    if (-not (Test-Path $Rollback)) { Die "no rollback record at $Rollback" }
    $rec = Get-Content -Raw -Path $Rollback | ConvertFrom-Json
    Say "rollback record: ref=$($rec.ref) commit=$($rec.commit) taken=$($rec.at)"
    if (-not $rec.commit) { Die "the rollback record carries no commit; it cannot be used" }
    $Ref = $rec.commit
    Say "rolling back to $Ref"
}

# ------------------------------------------- 1. record the rollback point FIRST

$before = Invoke-RepoGit @("rev-parse", "HEAD")
# A rollback record whose commit is not a commit is not a rollback record.
# Invoke-Git merges stdout and stderr, so a git that decided to narrate would
# otherwise be written in here as the previous state (straightedge#274).
if ($before -notmatch '^[0-9a-f]{40}$') { Die "rev-parse HEAD returned something that is not a 40-character oid, so the rollback point cannot be trusted: $before" }
$status = Invoke-RepoGit @("status", "--porcelain")
$expert = Join-Path $Repo "mt4\Experts\Mt4RiskBot.mq4"
$expertHash = if (Test-Path $expert) { (Get-FileHash -Algorithm SHA256 $expert).Hash.ToLower() } else { "" }

Say "current HEAD      : $before"
Say "worktree dirty    : $(if ($status) { 'YES' } else { 'no' })"
Say "Expert sha256     : $(if ($expertHash) { $expertHash } else { '(absent)' })"
if ($status) {
    Write-Host "::warning::the worktree has local modifications. A checkout may refuse or may discard them. Inspect before continuing:"
    Write-Host $status
}

# A rollback is only a rollback if the previous state was captured BEFORE the
# change rather than reconstructed after it. The Expert's hash is recorded
# alongside because this estate has already split a deployment across two
# artifacts and lost track of which was where (straightedge#142); a rollback
# that restored one and not the other would recreate that skew.
$record = [ordered]@{
    at = (Get-Date).ToUniversalTime().ToString("o")
    commit = $before
    ref = "HEAD-before-deploy"
    requested = $Ref
    expert_sha256 = $expertHash
    worktree_dirty = [bool]$status
}
$recordPath = Join-Path $RecordDir ("rollback-" + (Get-Date).ToUniversalTime().ToString("yyyyMMddTHHmmssZ") + ".json")
if ($Apply) {
    New-Item -ItemType Directory -Force -Path $RecordDir | Out-Null
    Write-Utf8NoBom -Path $recordPath -Text ($record | ConvertTo-Json)
    Say "rollback point recorded: $recordPath"
} else {
    Say "would record the rollback point at $recordPath"
}

# ------------------------------------------------------- 2. report preconditions

$inflight = Join-Path $StateDir "journal.inflight.json"
if (Test-Path $inflight) {
    $open = (Get-Content -Raw -Path $inflight | ConvertFrom-Json).open
    $count = if ($null -eq $open) { 0 } else { @($open.PSObject.Properties).Count }
    Say "open inflight entries: $count"
    if ($count -gt 0) {
        # An open entry is the desk saying it does not know whether an order
        # moved money. Deploying across one destroys the only process that
        # could still resolve it, and nothing downstream reconciles it.
        Die "REFUSING: $count open inflight entr(ies). Resolve them before deploying; see docs/RUNBOOK.md `Unresolved sends`."
    }
} else {
    Say "no inflight ledger yet (no send has ever been attempted)"
}

if (-not $BookIsFlat) {
    Say ""
    Say "STOPPING HERE, and this is not an error."
    Say "Pass -BookIsFlat once you have confirmed, by looking:"
    Say "  * the market is closed, or this window is one you accept"
    Say "  * the book is flat or deliberately held, with no working orders"
    Say "  * /status in the locked chat reads what you expect"
    Say "This script can report the ledger. It cannot decide that stopping a"
    Say "live autonomous desk right now is acceptable."
    exit 0
}

# ------------------------------------------------ THE ABORT WINDOW OPENS HERE
#
# straightedge#274. Every step below changes the box: supervision goes off, the
# desk is killed, the tree moves. A live deploy aborted inside this window on a
# `git fetch` that had SUCCEEDED and left a real-money box with no desk and no
# supervision, and the only thing that noticed was a person reading the
# transcript. Two mechanisms close that, because they fail differently:
#
#   * the `finally` below runs whenever this script THROWS. It announces the
#     half-landed state and re-enables supervision when that is safe.
#   * the marker on disk survives the script being KILLED, the window being
#     closed, or the box rebooting, where no `finally` runs at all. The next
#     -Apply run refuses to step over it.
#
# THE `finally` DOES NOT RE-ENABLE UNCONDITIONALLY, and that is a decision
# rather than an omission. Re-enabling is right when the tree never moved (the
# #274 case exactly: the box goes back to what it already was) and when the tree
# moved AND the loader plus doctor accepted it. It is WRONG when the tree moved
# and validation refused it, because supervision would then crash-loop a desk on
# a tree this script has already rejected. In that one case the box is left down
# deliberately, loudly, with the rollback command printed, and a human decides.

$treeMoved = $false
$validated = $false
$supervisionDisabled = $false
$completed = $false
$phase = "before any change"
$after = "(not reached)"

function Set-Phase {
    param([string] $Name)
    $script:phase = $Name
    if (-not $Apply) { return }
    $marker = [ordered]@{
        at = (Get-Date).ToUniversalTime().ToString("o")
        phase = $Name
        pid = $PID
        ps_version = $PSVersionTable.PSVersion.ToString()
        repo = $Repo
        requested_ref = $Ref
        before_commit = $before
        tree_moved = [bool]$script:treeMoved
        validated = [bool]$script:validated
        rollback_record = $recordPath
    }
    Write-DeployMarker -Path $markerPath -Marker $marker
}

try {

# --------------------------------------------- 3. DISABLE THE TRIGGERS, not just
#                                                  the process
#
# This step exists because of straightedge#146 and it is the one that is easy to
# get wrong. Supervision's repeating trigger fires on its own schedule
# regardless of what is being done to the working tree, so a checkout with the
# task still enabled can start a desk on a HALF-UPDATED tree. Stopping the
# process is not enough: the trigger has to be off.
#
# And the current box makes it worse. `run-desk-hidden.vbs` launches
# `cmd /c run-desk.cmd` with the wait flag FALSE, so wscript returns
# immediately, Task Scheduler marks the task complete, and
# MultipleInstancesPolicy=IgnoreNew protects nothing: the only barrier against a
# second desk is journal.lock. Measured 2026-10-08.
#
# straightedge-watch goes down too, or it pages the locked chat about an outage
# being caused on purpose.

if ($Apply) {
    foreach ($name in $Tasks) {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($null -ne $t) { Disable-ScheduledTask -TaskName $name | Out-Null; Say "disabled $name" }
        else { Say "$name not present, nothing to disable" }
    }
    Assert-TasksState -WantEnabled $false
    $supervisionDisabled = $true
    Set-Phase "supervision-disabled"
    Say "in-progress marker written: $markerPath"
} else {
    Say "would disable: $($Tasks -join ', ')"
    Say "would write an in-progress marker at $markerPath and clear it at step 8"
}

# ------------------------------------------------------------ 4. stop the desk

$lock = Join-Path $StateDir "journal.lock"
if ($Apply) {
    $procs = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
               Where-Object { $_.CommandLine -like "*straightedge*run*" })
    Say "desk processes found: $($procs.Count)"
    foreach ($p in $procs) {
        Say "stopping pid $($p.ProcessId)"
        Stop-Process -Id $p.ProcessId -Force
    }
    Start-Sleep -Seconds 3
    $still = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
               Where-Object { $_.CommandLine -like "*straightedge*run*" })
    if ($still.Count -gt 0) { Die "a desk process is still running after a stop; refusing to change the tree underneath it" }
    Say "no desk process remains"
    Set-Phase "desk-stopped"
} else {
    Say "would stop any python process whose command line matches straightedge run"
}

# ------------------------------------------------------------- 5. move the tree

if ($Apply) {
    # THE #274 LINE. `git fetch` writes "From https://github.com/..." to stderr
    # on every fetch that advances a ref, and on Windows PowerShell 5.1 that was
    # a terminating NativeCommandError here, right after the desk was killed and
    # before supervision came back.
    Invoke-RepoGit @("fetch", "--tags", "--prune", "origin") | Out-Null
    $target = Invoke-RepoGit @("rev-parse", "--verify", "$Ref^{commit}")
    if ($target -notmatch '^[0-9a-f]{40}$') { Die "rev-parse --verify of $Ref returned something that is not a 40-character oid: $target" }
    Say "target commit     : $target"
    # DETACHED at an exact commit on purpose. A branch name is not a statement
    # about what is running, because the branch moves; that is how twelve days
    # of staleness went unnoticed. `git clean` is NEVER run here: it is the one
    # command that could remove an untracked config.toml, and nothing in this
    # procedure needs it.
    Invoke-RepoGit @("checkout", "--detach", $target) | Out-Null
    $after = Invoke-RepoGit @("rev-parse", "HEAD")
    if ($after -ne $target) { Die "checkout did not land on $target (HEAD is $after)" }
    Say "HEAD is now        : $after"
    $treeMoved = $true
    Set-Phase "tree-moved"
} else {
    Say "would fetch and checkout --detach $Ref (git clean is never run)"
    $after = "(dry run)"
}

# ----------------------------------------------- 6. reinstall, then VALIDATE the
#                                                    config through the loader

if ($Apply) {
    & $PythonExe -m pip install -e $Repo --quiet
    if ($LASTEXITCODE -ne 0) { Die "pip install -e failed; the package is the working tree, so the desk would run the old code or none" }
    Say "package reinstalled from the working tree"

    & $PythonExe (Join-Path $Repo "deploy\windows\assert-config-loads.py") $ConfigPath
    if ($LASTEXITCODE -ne 0) { Die "the config does NOT load. Fix it before re-enabling supervision, or the desk will crash-loop. See docs/DEPLOY.md on the BOM trap." }
    Say "config loads, and the derived figures are above"

    & $PythonExe -m straightedge --config $ConfigPath doctor
    if ($LASTEXITCODE -ne 0) { Die "doctor exited non-zero; it is the documented pre-run gate and it has refused this tree" }
    Say "doctor exited 0"
    $validated = $true
    Set-Phase "validated"
} else {
    Say "would reinstall, then run assert-config-loads.py and doctor"
}

# ------------------------------------------------------------- 7. stamp it

if ($Apply) {
    $stamp = [ordered]@{
        ref = $Ref
        commit = $after
        at = (Get-Date).ToUniversalTime().ToString("o")
    }
    # Beside the journal, same idiom as every other sidecar, so one
    # journal_path setting locates it. Written WITHOUT a BOM.
    $stampPath = Join-Path $StateDir "journal.deployed.json"
    Write-Utf8NoBom -Path $stampPath -Text ($stamp | ConvertTo-Json)
    Say "stamped: $stampPath"
    Set-Phase "stamped"
} else {
    Say "would write journal.deployed.json with the ref and the full oid"
}

# -------------------------------------- 8. RE-ENABLE supervision, and prove it

if ($Apply) {
    foreach ($name in $Tasks) {
        $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
        if ($null -ne $t) { Enable-ScheduledTask -TaskName $name | Out-Null; Say "enabled $name" }
    }
    Assert-TasksState -WantEnabled $true
} else {
    Say "would re-enable and then PROVE enabled: $($Tasks -join ', ')"
}

# ----------------------------------------------- THE ABORT WINDOW CLOSES HERE

$phase = "complete"
$completed = $true
if ($Apply) {
    Remove-DeployMarker -Path $markerPath
    Say "in-progress marker cleared: the box is supervised again"
}

} finally {
    if ($Apply -and -not $completed) {
        # SELF-ANNOUNCE. Before straightedge#274 a half-landed deploy produced
        # nothing but a stack trace in a transcript somebody had to read.
        Write-Host "::error::DEPLOY DID NOT COMPLETE, and the box is in a half-landed state."
        $treeLine = if ($treeMoved) { "YES, HEAD is now $after" } else { "no, HEAD is still $before" }
        $validLine = if ($validated) { "yes" } else { "NO" }
        Say "phase reached      : $phase"
        Say "tree moved         : $treeLine"
        Say "config validated   : $validLine"
        Say "rollback record    : $recordPath"
        Say "in-progress marker : $markerPath (LEFT IN PLACE; the next -Apply run refuses to step over it)"

        if ($supervisionDisabled) {
            $resumeIsSafe = Test-ResumeSupervisionIsSafe -TreeMoved $treeMoved -Validated $validated
            if ($resumeIsSafe) {
                # A throw in here would REPLACE the exception that brought us
                # here, so the re-enable gets its own try and reports instead.
                try {
                    foreach ($name in $Tasks) {
                        $t = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
                        if ($null -ne $t) { Enable-ScheduledTask -TaskName $name | Out-Null; Say "re-enabled $name on the way out" }
                    }
                    Assert-TasksState -WantEnabled $true
                    Say "supervision is BACK ON. The tree is one a desk may run on, so this is the state the box was already in."
                } catch {
                    Write-Host "::error::THE RE-ENABLE ON THE WAY OUT ALSO FAILED: $($_.Exception.Message)"
                    Write-Host "::error::SUPERVISION IS OFF AND THE DESK IS DOWN. Re-enable by hand: Enable-ScheduledTask -TaskName <name> for each of $($Tasks -join ', ')"
                }
            } else {
                Write-Host "::error::SUPERVISION IS LEFT DISABLED AND THE DESK IS DOWN, DELIBERATELY. The tree moved and the loader or doctor refused it, so re-enabling supervision would crash-loop a desk on a tree this script has already rejected. A human decides this one."
            }
        } else {
            Say "supervision was never disabled, so there is nothing to restore"
        }
        Write-Host "::error::to roll back: Deploy-Desk.ps1 -Rollback $recordPath -BookIsFlat -Apply"
    }
}

# -------------------------------------------------------------- 9. verify

Say ""
Say "VERIFY, do not assume. Run these and read them:"
Say "  powershell -File $Repo\deploy\windows\Export-Tasks.ps1 -OutDir $StateDir\tasks"
Say "  `"$PythonExe`" -m straightedge --config $ConfigPath supervision --tasks $StateDir\tasks"
Say "  type $StateDir\journal.heartbeat     # deployed= must name what you just deployed"
Say "  /status in the locked chat            # same answer, from outside the box"
Say ""
Say "THE DESK IS BACK DISARMED, by design (fc34). A real-money desk trades"
Say "again only when a human sends /live on I-ACCEPT-RISK in the locked chat."
Say "If auto was armed before this deploy, it is NOT armed now."
if ($Apply) {
    Say ""
    Say "to roll back: Deploy-Desk.ps1 -Rollback $recordPath -BookIsFlat -Apply"
}
