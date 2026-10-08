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
    [string] $Rollback
)

$ErrorActionPreference = "Stop"
if (Test-Path Variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

$git = "C:\Program Files\Git\cmd\git.exe"
function Say($m) { Write-Host "[deploy] $m" }
function Die($m) { Write-Host "::error::$m"; throw $m }

function Invoke-Git {
    param([string[]] $GitArgs)
    $out = & $git -C $Repo @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) { Die "git $($GitArgs -join ' ') exited $LASTEXITCODE : $out" }
    return ($out | Out-String).Trim()
}

function Write-Utf8NoBom {
    param([string] $Path, [string] $Text)
    # NEVER Set-Content -Encoding UTF8. On Windows PowerShell 5.1 that writes a
    # BOM, and a BOM in config.toml stopped the desk's loader dead on
    # 2026-10-08; see docs/DEPLOY.md. The desk now tolerates one and complains,
    # but nothing written BY this script should ever need that tolerance.
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding $false))
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

$before = Invoke-Git @("rev-parse", "HEAD")
$status = Invoke-Git @("status", "--porcelain")
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
} else {
    Say "would disable: $($Tasks -join ', ')"
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
} else {
    Say "would stop any python process whose command line matches straightedge run"
}

# ------------------------------------------------------------- 5. move the tree

if ($Apply) {
    Invoke-Git @("fetch", "--tags", "--prune", "origin") | Out-Null
    $target = Invoke-Git @("rev-parse", "--verify", "$Ref^{commit}")
    Say "target commit     : $target"
    # DETACHED at an exact commit on purpose. A branch name is not a statement
    # about what is running, because the branch moves; that is how twelve days
    # of staleness went unnoticed. `git clean` is NEVER run here: it is the one
    # command that could remove an untracked config.toml, and nothing in this
    # procedure needs it.
    Invoke-Git @("checkout", "--detach", $target) | Out-Null
    $after = Invoke-Git @("rev-parse", "HEAD")
    if ($after -ne $target) { Die "checkout did not land on $target (HEAD is $after)" }
    Say "HEAD is now        : $after"
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
