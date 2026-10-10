# The controls for deploy/windows. Run under BOTH shells, because which shell
# ran it IS the finding (straightedge#274).
#
# WHY THIS FILE EXISTS, STATED PLAINLY
#
# #274: `Deploy-Desk.ps1` aborted a live deploy on a `git fetch` that SUCCEEDED,
# in the window between "supervision disabled, desk killed" and "supervision
# re-enabled", and left a real-money box with no desk and no supervision. Two
# separate things had to be true for that to happen:
#
#   1. `Invoke-Git` treated stderr as failure. On Windows PowerShell 5.1 a
#      native command writing to stderr raises a NativeCommandError, and
#      `$ErrorActionPreference = "Stop"` makes it TERMINATING. git narrates to
#      stderr on every fetch that advances a ref.
#   2. The mitigation in the file, `$PSNativeCommandUseErrorActionPreference`,
#      exists only on PowerShell 7.3 and later. The box has 5.1 and no `pwsh`.
#
# And a third thing, which is why CI did not catch it: every step in ci.yml that
# ran this script used `shell: pwsh`, PowerShell 7, which does not have the 5.1
# behaviour at all. The gate was structurally incapable of producing the red. So
# this file is run under `shell: powershell` (Windows PowerShell 5.1, the shell
# the box actually has) AND under `shell: pwsh`, and the 5.1 leg carries the
# control that reproduces the defect.
#
# THE PAIR IS THE POINT. A fix that swallowed stderr AND swallowed a real
# failure would pass "stderr does not abort" on its own. Every expected-throw
# case here has an expected-no-throw twin and the reverse, and the harness
# itself is driven red first, because an assertion helper that cannot fail makes
# every case below decoration.

[CmdletBinding()]
param(
    # The repo root, for the Deploy-Desk.ps1 dry runs. A real git checkout.
    [Parameter(Mandatory = $true)] [string] $Repo,
    # A python that exists. Preflight refuses without one.
    [Parameter(Mandatory = $true)] [string] $PythonExe,
    # Scratch. Everything this script writes goes under here and nowhere else.
    [Parameter(Mandatory = $true)] [string] $WorkRoot
)

$ErrorActionPreference = "Stop"

. (Join-Path $PSScriptRoot "DeskDeployLib.ps1")

$deploy = Join-Path $PSScriptRoot "Deploy-Desk.ps1"
$gitExe = (Get-Command git).Source

# ------------------------------------------------------------------ harness

$script:cases = 0
$script:failures = @()
$EXPECTED_CASES = 20

function Case {
    param([string] $Name, [scriptblock] $Body)
    $script:cases += 1
    try {
        & $Body
        Write-Host ("  ok    " + $Name)
    } catch {
        $script:failures += ($Name + " :: " + $_.Exception.Message)
        Write-Host ("  FAIL  " + $Name + " :: " + $_.Exception.Message)
    }
}

function Assert-Throws {
    param([scriptblock] $Body, [string] $Because)
    $threw = $false
    $message = ""
    try { & $Body | Out-Null } catch { $threw = $true; $message = $_.Exception.Message }
    if (-not $threw) { throw ("expected a throw and got none; " + $Because) }
    return $message
}

function Assert-NoThrow {
    param([scriptblock] $Body, [string] $Because)
    try { return (& $Body) } catch { throw ("expected no throw; " + $Because + "; got: " + $_.Exception.Message) }
}

function Assert-Equal {
    param($Actual, $Expected, [string] $What)
    if ($Actual -ne $Expected) { throw ($What + ": expected [" + $Expected + "] and got [" + $Actual + "]") }
}

function Assert-Match {
    param([string] $Text, [string] $Pattern, [string] $What)
    if ($Text -notmatch $Pattern) { throw ($What + ": [" + $Text + "] does not match /" + $Pattern + "/") }
}

# The pre-fix `Invoke-Git`, copied rather than described, so the control below
# reproduces the shipped defect and not an impression of it. No local
# $ErrorActionPreference, no $PSNativeCommandUseErrorActionPreference: exactly
# what ran on the box.
function Invoke-GitPreFix {
    param([string] $Git, [string] $Repo, [string[]] $GitArgs)
    $out = & $Git -C $Repo @GitArgs 2>&1
    if ($LASTEXITCODE -ne 0) { throw ("git exited " + $LASTEXITCODE + " : " + $out) }
    return ($out | Out-String).Trim()
}

# ------------------------------------------------- what shell is this, really

$isDesktop = ($PSVersionTable.PSEdition -ne "Core")
$guardExists = [bool](Test-Path Variable:PSNativeCommandUseErrorActionPreference)

Write-Host "================================================================"
Write-Host ("PSVersion            : " + $PSVersionTable.PSVersion)
Write-Host ("PSEdition            : " + $PSVersionTable.PSEdition)
Write-Host ("guard variable exists: " + $guardExists + "   (the #274 mitigation; FALSE on the live box)")
Write-Host ("git                  : " + $gitExe)
Write-Host ("repo                 : " + $Repo)
Write-Host "================================================================"

# ------------------------------------------------------ a throwaway git repo
#
# Local, so no case depends on a network, a remote or a credential. `git
# checkout -b` writes "Switched to a new branch" to STDERR and exits 0, which is
# the same shape as the `From https://...` that fetch writes, and it is
# deterministic.

if (-not (Test-Path $WorkRoot)) { New-Item -ItemType Directory -Force -Path $WorkRoot | Out-Null }
$scratch = Join-Path $WorkRoot "se274"
New-Item -ItemType Directory -Force -Path $scratch | Out-Null
$sandbox = Join-Path $scratch "sandbox-repo"
New-Item -ItemType Directory -Force -Path $sandbox | Out-Null

& $gitExe -C $sandbox init --quiet | Out-Null
Set-Content -LiteralPath (Join-Path $sandbox "a.txt") -Value "a"
& $gitExe -C $sandbox add a.txt | Out-Null
& $gitExe -C $sandbox -c user.email=ci@example.invalid -c user.name=ci commit --quiet -m init | Out-Null

$script:branchSeq = 0
function New-BranchName {
    $script:branchSeq += 1
    return ("se274-case-" + $script:branchSeq)
}

Write-Host "--- harness controls (an assertion helper that cannot fail makes every case below decoration)"

Case "the harness can fail: Assert-Throws reds when the body does not throw" {
    $caught = $false
    try { Assert-Throws -Body { 1 + 1 } -Because "deliberate control" | Out-Null } catch { $caught = $true }
    if (-not $caught) { throw "Assert-Throws accepted a body that did not throw, so every expected-throw case here is decorative" }
}

Case "the harness can fail: Assert-Equal reds on a mismatch" {
    $caught = $false
    try { Assert-Equal -Actual 1 -Expected 2 -What "deliberate control" } catch { $caught = $true }
    if (-not $caught) { throw "Assert-Equal accepted 1 -eq 2, so every equality case here is decorative" }
}

Write-Host "--- the instrument: does the command under test actually write to stderr"

Case "the git command used below really writes to stderr and really exits 0" {
    # THE DENOMINATOR. If this command wrote nothing to stderr, every case after
    # it would pass for the wrong reason: an absent check reads like a passed
    # one. Streams are separated with Start-Process rather than a PowerShell
    # redirect, so the measurement does not depend on the behaviour under test.
    $branch = New-BranchName
    $outFile = Join-Path $scratch "probe.out"
    $errFile = Join-Path $scratch "probe.err"
    $p = Start-Process -FilePath $gitExe -NoNewWindow -Wait -PassThru `
        -ArgumentList @("-C", $sandbox, "checkout", "-b", $branch) `
        -RedirectStandardOutput $outFile -RedirectStandardError $errFile
    $stderrText = (Get-Content -Raw -LiteralPath $errFile)
    Assert-Equal -Actual $p.ExitCode -Expected 0 -What "git checkout -b exit code"
    if ([string]::IsNullOrWhiteSpace($stderrText)) {
        throw "git checkout -b wrote NOTHING to stderr on this git, so the #274 shape is not reproduced here and the cases below are vacuous"
    }
    Assert-Match -Text $stderrText -Pattern "Switched to a new branch" -What "the stderr git wrote"
    Write-Host ("        stderr was: " + $stderrText.Trim())
}

Write-Host "--- the positive control: the shipped defect, reproduced"

Case "the pre-fix Invoke-Git aborts on that successful command (Windows PowerShell 5.1)" {
    $branch = New-BranchName
    $body = { Invoke-GitPreFix -Git $gitExe -Repo $sandbox -GitArgs @("checkout", "-b", $branch) }
    if ($isDesktop) {
        # MANDATORY on 5.1. If this does not throw, this runner cannot observe
        # #274 and nothing else in this file is evidence about it.
        $message = Assert-Throws -Body $body -Because "on Windows PowerShell 5.1 native stderr under ErrorActionPreference Stop is a terminating NativeCommandError, which is what aborted the live deploy"
        Write-Host ("        reproduced, it threw: " + $message.Trim())
    } else {
        # NOT asserted on PowerShell 7, and the reason is the other half of
        # #274: PowerShell 7 does not raise NativeCommandError on native stderr,
        # so a pwsh-only CI job could never have produced this red. Recorded
        # rather than asserted, because asserting either outcome here would be
        # asserting a claim about the wrong shell.
        $threw = $false
        try { & $body | Out-Null } catch { $threw = $true }
        Write-Host ("        PowerShell Core, so not asserted. threw=" + $threw + " (this is exactly why the pwsh-only gate could not see #274)")
    }
}

Write-Host "--- the fix: stderr is not failure, and failure is still failure"

Case "Invoke-Git does NOT abort on a command that writes stderr and succeeds" {
    $branch = New-BranchName
    $text = Assert-NoThrow -Body { Invoke-Git -Git $gitExe -Repo $sandbox -GitArgs @("checkout", "-b", $branch) } `
        -Because "the exit code is the authority on whether git failed and it was 0"
    Assert-Match -Text $text -Pattern "Switched to a new branch" -What "what Invoke-Git returned"
}

Case "Invoke-Git DOES abort on a git that genuinely fails, naming the code and the reason" {
    $message = Assert-Throws -Body { Invoke-Git -Git $gitExe -Repo $sandbox -GitArgs @("rev-parse", "--verify", "se274-no-such-ref^{commit}") } `
        -Because "a fix that swallows stderr must not also swallow a real failure; without this case the case above is satisfied by swallowing everything"
    Assert-Match -Text $message -Pattern "exited 128" -What "the refusal must name the exit code"
    Assert-Match -Text $message -Pattern "fatal|Needed a single revision" -What "the refusal must carry what git said"
}

Case "Invoke-Git does not leak ErrorActionPreference on the success path" {
    $branch = New-BranchName
    Invoke-Git -Git $gitExe -Repo $sandbox -GitArgs @("checkout", "-b", $branch) | Out-Null
    Assert-Equal -Actual $ErrorActionPreference -Expected "Stop" -What "the caller's ErrorActionPreference after a successful Invoke-Git"
}

Case "Invoke-Git does not leak ErrorActionPreference on the failure path" {
    Assert-Throws -Body { Invoke-Git -Git $gitExe -Repo $sandbox -GitArgs @("rev-parse", "--verify", "se274-still-no-such-ref^{commit}") } `
        -Because "setting up the leak check" | Out-Null
    Assert-Equal -Actual $ErrorActionPreference -Expected "Stop" -What "the caller's ErrorActionPreference after a failing Invoke-Git"
}

Case "Invoke-Git refuses when git never ran, rather than inheriting the previous 0" {
    # Ordered deliberately: a SUCCESSFUL call runs first so $LASTEXITCODE is 0
    # going in. Without the clear inside Invoke-Git this case would pass as a
    # success with empty output, which is the stale-exit-code shape of the same
    # family as #274 itself.
    $branch = New-BranchName
    Invoke-Git -Git $gitExe -Repo $sandbox -GitArgs @("checkout", "-b", $branch) | Out-Null
    Assert-Equal -Actual $LASTEXITCODE -Expected 0 -What "the exit code going into the no-such-git case"
    $absent = Join-Path $scratch "no-such-git.exe"
    Assert-Throws -Body { Invoke-Git -Git $absent -Repo $sandbox -GitArgs @("rev-parse", "HEAD") } `
        -Because "a git that could not be launched must not read as success" | Out-Null
}

Write-Host "--- the stated requirement, driven from both sides"

Case "Assert-PowerShellSupported REFUSES PowerShell 4.0" {
    Assert-Throws -Body { Assert-PowerShellSupported -Version ([version]"4.0") } `
        -Because "the cmdlets this script uses are PowerShell 5 surface" | Out-Null
}

Case "Assert-PowerShellSupported ACCEPTS 5.1.20348.5622, the version measured on the live box" {
    Assert-NoThrow -Body { Assert-PowerShellSupported -Version ([version]"5.1.20348.5622") } `
        -Because "the box runs 5.1 and the deploy must run there" | Out-Null
}

Write-Host "--- the abort-window decision (straightedge#274 requirement 3)"

Case "resume is safe when the tree never moved, which is the #274 case exactly" {
    Assert-Equal -Actual (Test-ResumeSupervisionIsSafe -TreeMoved $false -Validated $false) -Expected $true `
        -What "tree untouched, so re-enabling supervision returns the box to what it already was"
}

Case "resume is safe when the tree moved AND validation accepted it" {
    Assert-Equal -Actual (Test-ResumeSupervisionIsSafe -TreeMoved $true -Validated $true) -Expected $true `
        -What "the loader and doctor both accepted the tree"
}

Case "resume is NOT safe when the tree moved and validation refused it" {
    Assert-Equal -Actual (Test-ResumeSupervisionIsSafe -TreeMoved $true -Validated $false) -Expected $false `
        -What "re-enabling here would crash-loop a desk on a tree doctor already rejected"
}

Case "the in-progress marker round trips and is written WITHOUT a BOM" {
    $markerDir = Join-Path $scratch "markers"
    $path = Get-DeployMarkerPath -RecordDir $markerDir
    Write-DeployMarker -Path $path -Marker ([ordered]@{ phase = "tree-moved"; tree_moved = $true })
    $bytes = [System.IO.File]::ReadAllBytes($path)
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        throw "the marker was written with a UTF-8 BOM; docs/DEPLOY.md records a BOM costing a live outage"
    }
    $back = Read-DeployMarker -Path $path
    Assert-Equal -Actual $back.phase -Expected "tree-moved" -What "the phase read back out of the marker"
    Remove-DeployMarker -Path $path
    Assert-Equal -Actual (Test-Path $path) -Expected $false -What "the marker after Remove-DeployMarker"
    Assert-Equal -Actual (Read-DeployMarker -Path $path) -Expected $null -What "reading an absent marker"
}

Write-Host "--- Deploy-Desk.ps1 itself, on this shell"

function New-DeployState {
    param([string] $Name)
    $root = Join-Path $scratch $Name
    New-Item -ItemType Directory -Force -Path $root | Out-Null
    Copy-Item (Join-Path $Repo "config.example.toml") (Join-Path $root "config.toml") -Force
    return $root
}

Case "the dry run STOPS at the judgement gate and exits 0 without -BookIsFlat" {
    $state = New-DeployState "state-gate"
    $rec = Join-Path $state "deploys"
    Assert-NoThrow -Body {
        & $deploy -Ref HEAD -Repo $Repo -StateDir $state -ConfigPath (Join-Path $state "config.toml") `
            -PythonExe $PythonExe -RecordDir $rec
    } -Because "stopping a live autonomous desk is not this script's decision, so the gate is an exit 0 and not an error" | Out-Null
    Assert-Equal -Actual $LASTEXITCODE -Expected 0 -What "the exit code at the judgement gate"
}

Case "the full dry run traverses every step and changes NOTHING" {
    $state = New-DeployState "state-dry"
    $rec = Join-Path $state "deploys"
    Assert-NoThrow -Body {
        & $deploy -Ref HEAD -Repo $Repo -StateDir $state -ConfigPath (Join-Path $state "config.toml") `
            -PythonExe $PythonExe -RecordDir $rec -BookIsFlat
    } -Because "a dry run must reach the end" | Out-Null
    if (Test-Path (Join-Path $state "journal.deployed.json")) { throw "a DRY RUN wrote the deploy stamp; -Apply is not gating writes" }
    if (Test-Path $rec) { throw "a DRY RUN created the rollback record directory" }
    if (Test-Path (Get-DeployMarkerPath -RecordDir $rec)) { throw "a DRY RUN wrote the in-progress marker" }
}

Case "the script REFUSES a state dir inside the checkout" {
    # The negative control for the most destructive mistake this procedure could
    # make: a hard checkout with the live journal inside the tree would delete
    # the audit log of every trade the desk has ever made.
    $inside = Join-Path $Repo "se274-state-inside"
    New-Item -ItemType Directory -Force -Path $inside | Out-Null
    Copy-Item (Join-Path $Repo "config.example.toml") (Join-Path $inside "config.toml") -Force
    try {
        $message = Assert-Throws -Body {
            & $deploy -Ref HEAD -Repo $Repo -StateDir $inside -ConfigPath (Join-Path $inside "config.toml") `
                -PythonExe $PythonExe -RecordDir (Join-Path $inside "deploys")
        } -Because "a hard checkout would destroy the live journal"
        Assert-Match -Text $message -Pattern "REFUSING" -What "the refusal message"
    } finally {
        Remove-Item -Recurse -Force $inside
    }
}

Case "an -Apply run REFUSES to step over an in-progress marker" {
    # -Apply IS set, and that is the point: the marker refusal is the only thing
    # between this call and the destructive steps. It is safe anyway, because
    # -BookIsFlat is absent, so if the refusal ever stopped firing this case
    # fails at the judgement gate instead of changing the runner.
    $state = New-DeployState "state-marker-refuse"
    $rec = Join-Path $state "deploys"
    Write-DeployMarker -Path (Get-DeployMarkerPath -RecordDir $rec) -Marker ([ordered]@{
        at = "2026-10-10T04:02:47Z"
        phase = "desk-stopped"
        pid = 1636
        ps_version = "5.1.20348.5622"
        before_commit = "91ec522e535e832d71a82ebbeb17755c90905832"
        tree_moved = $false
        validated = $false
        rollback_record = "C:\bot-state\deploys\rollback-20261010T040247Z.json"
    })
    $message = Assert-Throws -Body {
        & $deploy -Ref HEAD -Repo $Repo -StateDir $state -ConfigPath (Join-Path $state "config.toml") `
            -PythonExe $PythonExe -RecordDir $rec -Apply
    } -Because "a previous deploy that never finished means the box may be running a tree nobody chose, with supervision off"
    Assert-Match -Text $message -Pattern "REFUSING" -What "the refusal message"
    Assert-Match -Text $message -Pattern "PreviousDeployResolved" -What "the refusal must name the way forward"
}

Case "an -Apply run PROCEEDS over the marker once the operator asserts -PreviousDeployResolved" {
    # The other half. A refusal that cannot be cleared is a lockout, and the
    # standing guardrail here is that a filter able to ban its own ingress path
    # does not belong in that path.
    $state = New-DeployState "state-marker-resolved"
    $rec = Join-Path $state "deploys"
    Write-DeployMarker -Path (Get-DeployMarkerPath -RecordDir $rec) -Marker ([ordered]@{
        at = "2026-10-10T04:02:47Z"
        phase = "desk-stopped"
        tree_moved = $false
        validated = $false
    })
    Assert-NoThrow -Body {
        & $deploy -Ref HEAD -Repo $Repo -StateDir $state -ConfigPath (Join-Path $state "config.toml") `
            -PythonExe $PythonExe -RecordDir $rec -Apply -PreviousDeployResolved
    } -Because "the operator has asserted the half-landed deploy was dealt with" | Out-Null
    Assert-Equal -Actual $LASTEXITCODE -Expected 0 -What "the exit code at the judgement gate, having been let past the marker"
}

# ------------------------------------------------------------------ verdict

Write-Host "================================================================"
Write-Host ("cases run: " + $script:cases + " of " + $EXPECTED_CASES + " expected   failures: " + $script:failures.Count)
if ($script:cases -ne $EXPECTED_CASES) {
    Write-Host "::error::case count does not match; a case was skipped or added without updating EXPECTED_CASES, and a case that did not run reports nothing rather than passing"
    exit 2
}
if ($script:failures.Count -gt 0) {
    foreach ($f in $script:failures) { Write-Host ("::error::" + $f) }
    exit 1
}
Write-Host ("all " + $script:cases + " cases passed on PowerShell " + $PSVersionTable.PSVersion + " (" + $PSVersionTable.PSEdition + ")")
exit 0
