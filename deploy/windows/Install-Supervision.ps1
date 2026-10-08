# Register the two declared tasks from their XML, and record what was there
# before so the change can be backed out.
#
# THIS ONE MUTATES THE BOX. Read deploy/windows/README.md first, and the
# ordering section in particular: this script does NOT stop the desk and does
# NOT stop the MT4 terminal, because registering a task does not start it. If
# a desk is live and attached to a real account, registering straightedge-desk
# is safe (MultipleInstancesPolicy IgnoreNew plus journal.lock mean the next
# trigger is a no-op while the desk is up), but that is a claim to verify on a
# demo account before it is believed on a live one.
#
# It will not change anything unless -Apply is passed, because an
# infrastructure change to a real-money box should take a deliberate keystroke.
# The switch is -Apply and not -Confirm: -Confirm is a PowerShell common
# parameter name, and a script that declares its own meaning for a reserved
# name is a script that behaves differently the day someone adds
# SupportsShouldProcess to it.

[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $PythonExe,
    [Parameter(Mandatory = $true)] [string] $ConfigPath,
    [Parameter(Mandatory = $true)] [string] $WorkingDirectory,
    [string] $UserId = "$env:USERDOMAIN\$env:USERNAME",
    [string] $BackupDir = ".\tasks-before",
    [switch] $Apply
)

$ErrorActionPreference = "Stop"

if (-not $Apply) {
    Write-Host "DRY RUN. Nothing will be changed. Re-run with -Apply to register."
}

foreach ($path in @($PythonExe, $ConfigPath, $WorkingDirectory)) {
    if (-not (Test-Path $path)) {
        throw "no such path: $path. Fix the argument; a task pointing at a missing path registers fine and then fails silently on every trigger."
    }
}

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$tasks = @("straightedge-desk", "straightedge-watch")

# Record the previous definitions FIRST. A rollback is only a rollback if the
# previous state was captured before the change, not reconstructed after it.
New-Item -ItemType Directory -Force -Path $BackupDir | Out-Null
& (Join-Path $here "Export-Tasks.ps1") -OutDir $BackupDir -TaskName $tasks
Write-Host "previous definitions (if any) recorded in $BackupDir"

foreach ($name in $tasks) {
    $source = Join-Path $here "$name.xml"
    if (-not (Test-Path $source)) { throw "missing declaration: $source" }
    $xml = Get-Content -Raw -Path $source
    $xml = $xml.Replace("C:\REPLACE_ME\python.exe", $PythonExe)
    $xml = $xml.Replace("C:\REPLACE_ME\config.toml", $ConfigPath)
    $xml = $xml.Replace("C:\REPLACE_ME", $WorkingDirectory)
    $xml = $xml.Replace("<UserId>REPLACE_ME</UserId>", "<UserId>$UserId</UserId>")
    # The check runs against the XML with its COMMENTS STRIPPED, and the
    # comments are what made this necessary: they tell a reader to replace the
    # placeholders, so they name the token, so an unstripped check fires on the
    # instruction rather than on an unfilled value. The registered XML keeps its
    # comments; only this comparison does not see them.
    $values = [regex]::Replace($xml, "(?s)<!--.*?-->", "")
    if ($values -match "REPLACE_ME") {
        throw "$name still has an unfilled placeholder in a VALUE after substitution; refusing to register a half-filled definition"
    }
    if (-not $Apply) {
        Write-Host "would register $name from $source (substitutions all resolved)"
        continue
    }
    Register-ScheduledTask -TaskName $name -Xml $xml -Force | Out-Null
    Write-Host "registered $name"
}

if (-not $Apply) {
    Write-Host ""
    Write-Host "dry run complete. Nothing was changed."
    exit 0
}

Write-Host ""
Write-Host "verify, do not assume:"
Write-Host "  .\Export-Tasks.ps1 -OutDir .\tasks"
Write-Host "  $PythonExe -m straightedge --config $ConfigPath supervision --tasks .\tasks"
Write-Host ""
Write-Host "to back out: schtasks /create /xml <file from $BackupDir> /tn <name> /f"
Write-Host "or, if the task did not exist before: schtasks /delete /tn <name> /f"
