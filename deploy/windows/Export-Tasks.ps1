# Dump the LIVE scheduled task definitions so the audit can read them.
#
# READ-ONLY. It queries Task Scheduler and writes files. It does not register,
# start, stop, enable, disable or edit a task, and it does not touch the desk,
# the MT4 terminal, journal.lock or Telegram. Safe to run on a live box during
# a trading session, which is the point: an audit an operator is afraid to run
# during trading hours is one that only ever runs after the outage.
#
# Then:
#   python -m straightedge --config C:\path\to\config.toml supervision --tasks .\tasks
#
# Pass the DESK's own config. The restart-interval ceiling is derived from it
# (see src/straightedge/supervision.py), so auditing with a different config
# measures a different desk.

[CmdletBinding()]
param(
    [string] $OutDir = ".\tasks",
    # The names the audit declares. A default rather than a hardcoded loop so a
    # box using different names can still be dumped; the audit expects these.
    [string[]] $TaskName = @("straightedge-desk", "straightedge-watch")
)

$ErrorActionPreference = "Stop"
# PowerShell 7.4 turns native-command stderr into a terminating error under
# ErrorActionPreference Stop. `schtasks /query` on a task that does not exist
# writes to stderr, and that case is a FINDING here, not an error, so the
# behaviour is switched off explicitly rather than hoped about.
if (Test-Path Variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

$missing = @()
foreach ($name in $TaskName) {
    $dest = Join-Path $OutDir "$name.xml"
    $existing = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $existing) {
        # A task that does not exist is the finding, not an error. Leaving NO
        # file behind is what makes the audit report task_missing rather than
        # reporting nothing at all, so a stale file from a previous run is
        # removed on purpose.
        $missing += $name
        if (Test-Path $dest) { Remove-Item $dest -Force }
        continue
    }
    # schtasks writes UTF-16. Out-File re-encodes to UTF-8; the audit sniffs
    # the BOM either way (supervision.read_dump), so a plain cmd.exe redirect
    # of the same command also works.
    $xml = & schtasks.exe /query /tn $name /xml
    if ($LASTEXITCODE -ne 0) {
        throw "schtasks /query /tn $name /xml exited $LASTEXITCODE although the task exists; the audit must not be given a partial dump"
    }
    $xml | Out-File -FilePath $dest -Encoding utf8
    Write-Host "dumped $name -> $dest"
}

foreach ($name in $missing) {
    Write-Warning "no such scheduled task: $name (the audit will report task_missing)"
}

Write-Host ""
Write-Host "now run the audit, with the DESK's own config:"
Write-Host "  python -m straightedge --config C:\path\to\config.toml supervision --tasks $OutDir"
