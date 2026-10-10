# Deploy-Desk.ps1's helpers, in their own file so that a test can reach them.
#
# WHY THIS FILE EXISTS. straightedge#274: `Invoke-Git` aborted a live deploy on a
# `git fetch` that SUCCEEDED, in the window between "supervision disabled, desk
# killed" and "supervision re-enabled", and left a real-money box with no desk
# and no supervision. The function could not be tested, because the only way to
# reach it was to run a deploy: dot-sourcing Deploy-Desk.ps1 executes it. So the
# helpers live here, Deploy-Desk.ps1 dot-sources this, and
# Test-DeployScripts.ps1 dot-sources it too and drives each one from both sides.
#
# This file defines functions and nothing else. Dot-sourcing it must change
# nothing, or the test and the deploy stop being the same code path.

function Say {
    param([string] $Message)
    Write-Host "[deploy] $Message"
}

function Die {
    param([string] $Message)
    Write-Host "::error::$Message"
    throw $Message
}

function Assert-PowerShellSupported {
    param([Parameter(Mandatory = $true)] [version] $Version)
    # A STATED REQUIREMENT, which is what straightedge#274 item 4 asked for in
    # place of a mitigation that silently does not apply.
    #
    # It deliberately does NOT gate on a PowerShell version that `Invoke-Git`
    # needs, because after #274 `Invoke-Git` needs none: it judges git by exit
    # code and sets `$ErrorActionPreference` locally, and that variable exists on
    # every PowerShell. Keying a refusal on 7.3 would be carrying the inert
    # guard AND a gate over it, which is a worse version of the same defect.
    #
    # What this gates is the rest of the script. Get-ScheduledTask,
    # Disable-ScheduledTask, Get-CimInstance and Get-FileHash are PowerShell 5
    # surface (Windows 8 / Server 2012 and later), and the live box measured
    # 5.1.20348.5622 with no `pwsh` installed.
    #
    # The version arrives as a PARAMETER rather than being read from
    # $PSVersionTable inside, so this guard can be driven from both sides by a
    # test on any host. A guard nobody has watched refuse is not a guard.
    if ($Version.Major -lt 5) {
        Die "REFUSING: PowerShell $Version is too old for this script. It needs 5.0 or later for Get-ScheduledTask, Get-CimInstance and Get-FileHash; the live box is 5.1 (straightedge#274)."
    }
    Say "PowerShell version : $Version (5.0 or later required)"
}

function Invoke-Git {
    param(
        [Parameter(Mandatory = $true)] [string] $Git,
        [Parameter(Mandatory = $true)] [string] $Repo,
        [Parameter(Mandatory = $true)] [string[]] $GitArgs
    )
    # THE EXIT CODE IS THE AUTHORITY ON WHETHER GIT FAILED. STDERR IS NOT.
    #
    # straightedge#274, measured on the live box rather than predicted: on
    # Windows PowerShell 5.1 a native command that writes to stderr raises a
    # NativeCommandError, and the caller's `$ErrorActionPreference = "Stop"`
    # makes that TERMINATING. git writes ordinary progress to stderr, for
    # instance "From https://github.com/..." on every fetch that advances a ref,
    # so a fetch that SUCCEEDED threw, with supervision already off and the desk
    # already killed.
    #
    # The failure distribution was the worst available one: a fetch with nothing
    # to report writes nothing, so the script passed every rehearsal against an
    # already-current tree and failed on every deploy that moved the tree.
    #
    # `$ErrorActionPreference` is DYNAMICALLY scoped, so assigning it here
    # applies for the duration of this function and does not leak back to the
    # caller, which keeps "Stop" over every other line of the deploy. A test
    # asserts that it did not leak, on both the success and the failure path.
    $ErrorActionPreference = "Continue"
    # NOT LOAD-BEARING, and that is the whole point of writing it this way. On
    # PowerShell 7.3 and later this stops a non-zero exit code ALSO raising an
    # ErrorRecord, so the message below is the only one an operator reads. On
    # 5.1 the name does not exist, and the assignment is a function-local
    # variable nobody reads. The #274 defect was carrying this as the ONLY
    # mitigation, version-gated, on a box that does not have it. The line above
    # is what makes the behaviour correct, on every PowerShell, with no version
    # test anywhere in the fix.
    $PSNativeCommandUseErrorActionPreference = $false
    # Cleared so that "git never ran" cannot inherit a previous command's 0 and
    # read as success. An absent check reads like a passed one; a stale exit code
    # is the same shape. A test drives this with a $Git path that does not exist.
    $global:LASTEXITCODE = $null
    $out = & $Git -C $Repo @GitArgs 2>&1
    $code = $LASTEXITCODE
    # stdout and stderr are deliberately MERGED, which is what this function has
    # always returned and what PowerShell 7 already did here. The two call sites
    # that parse the result both ask for a commit oid and both cross-check the
    # answer against a 40-hex pattern, so a stderr line leaking into a parsed
    # value fails loudly at the call site rather than being written into a
    # rollback record.
    $text = ($out | Out-String).Trim()
    if ($null -eq $code) {
        Die "git $($GitArgs -join ' ') did not run, so there is no exit code to judge it by : $text"
    }
    if ($code -ne 0) {
        Die "git $($GitArgs -join ' ') exited $code : $text"
    }
    return $text
}

function Write-Utf8NoBom {
    param([string] $Path, [string] $Text)
    # NEVER Set-Content -Encoding UTF8. On Windows PowerShell 5.1 that writes a
    # BOM, and a BOM in config.toml stopped the desk's loader dead on
    # 2026-10-08; see docs/DEPLOY.md. The desk now tolerates one and complains,
    # but nothing written BY this script should ever need that tolerance.
    [System.IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding $false))
}

# ------------------------------------------------- the in-progress marker (#274)
#
# A deploy that dies between "supervision disabled" and "supervision re-enabled"
# leaves a real-money box with no desk and no supervision. Deploy-Desk.ps1 has a
# `finally` for the case where it THROWS, but no `finally` runs when the process
# is killed, the window is closed or the box reboots. This marker survives all
# three, and the next -Apply run refuses to step over it.

function Get-DeployMarkerPath {
    param([Parameter(Mandatory = $true)] [string] $RecordDir)
    return (Join-Path $RecordDir "deploy-in-progress.json")
}

function Write-DeployMarker {
    param(
        [Parameter(Mandatory = $true)] [string] $Path,
        [Parameter(Mandatory = $true)] $Marker
    )
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
    Write-Utf8NoBom -Path $Path -Text ($Marker | ConvertTo-Json)
}

function Read-DeployMarker {
    param([Parameter(Mandatory = $true)] [string] $Path)
    if (-not (Test-Path $Path)) { return $null }
    return (Get-Content -Raw -Path $Path | ConvertFrom-Json)
}

function Remove-DeployMarker {
    param([Parameter(Mandatory = $true)] [string] $Path)
    if (Test-Path $Path) { Remove-Item -Force -Path $Path }
}

function Test-ResumeSupervisionIsSafe {
    param(
        [Parameter(Mandatory = $true)] [bool] $TreeMoved,
        [Parameter(Mandatory = $true)] [bool] $Validated
    )
    # THE ONE JUDGEMENT INSIDE Deploy-Desk.ps1's abort handler, pulled out here
    # so it can be driven from every side. straightedge#274 asked for a
    # `finally` that re-enables the tasks it disabled; an UNCONDITIONAL one
    # would be wrong, and the wrong case is the dangerous one.
    #
    #   tree never moved        -> SAFE. This is the #274 case exactly: the
    #                              fetch threw before anything changed, so
    #                              re-enabling returns the box to the state it
    #                              was already in.
    #   tree moved, validated   -> SAFE. The loader and doctor both accepted it,
    #                              which is the same bar step 8 re-enables on.
    #   tree moved, NOT validated -> NOT SAFE. Supervision would crash-loop a
    #                              desk on a tree this script has already
    #                              rejected. The box stays down, loudly, and a
    #                              human decides.
    #
    # Returning a bool rather than acting means the caller still owns the
    # announcement, and this stays a pure function a test can enumerate.
    return ((-not $TreeMoved) -or $Validated)
}
