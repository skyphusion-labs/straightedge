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
# THE CLAIM THAT USED TO SIT HERE WAS FALSE, and straightedge#274 is what makes
# that worth writing down rather than quietly deleting. It said the behaviour
# was "switched off explicitly rather than hoped about". It is not:
# `$PSNativeCommandUseErrorActionPreference` exists only on PowerShell 7.3 and
# later, the box has 5.1 and no `pwsh`, so on the only shell that runs this the
# guard is INERT. In Deploy-Desk.ps1 the failure that same inert guard was
# written to stop is the one that aborted a live deploy.
#
# NO BEHAVIOUR CHANGE HERE, and the reason is measured rather than assumed: the
# stderr case the comment described is UNREACHABLE. `schtasks /query` writes to
# stderr only for a task that does not exist, and the loop below has already
# `continue`d on `Get-ScheduledTask` returning $null before it ever calls
# schtasks. The one invocation that does run is against a task that exists, it
# writes no stderr, and its exit code is checked. The assignment is kept because
# on 7.3+ it is real, and it is now labelled as what it is rather than as a
# mitigation this script depends on.
if (Test-Path Variable:PSNativeCommandUseErrorActionPreference) {
    $PSNativeCommandUseErrorActionPreference = $false
}

New-Item -ItemType Directory -Force -Path $OutDir | Out-Null

# A DateTime from Task Scheduler as an explicit UTC ISO-8601 string, or $null.
#
# The offset is resolved HERE, on the box, where the box's own offset is known.
# Writing a bare local timestamp and letting the audit label it UTC is
# straightedge#172's defect with a different clock in it, and a reader cannot
# recover an offset that was never written. An Unspecified Kind is treated as
# LOCAL, because that is what Task Scheduler hands out, and it is stated rather
# than assumed silently.
function ConvertTo-UtcIso {
    param($Value)
    if ($null -eq $Value) { return $null }
    try { $dt = [datetime]$Value } catch { return $null }
    # Task Scheduler reports a sentinel in the distant past for "never ran".
    # That is not a timestamp and must not become one: a 1899 LastRunTime
    # compared against a 300s cadence would report a 1.1 million hour staleness
    # and bury the real finding, which is that it has never run at all.
    if ($dt.Year -lt 2000) { return $null }
    if ($dt.Kind -eq [System.DateTimeKind]::Unspecified) {
        $dt = [datetime]::SpecifyKind($dt, [System.DateTimeKind]::Local)
    }
    return $dt.ToUniversalTime().ToString("o")
}

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

    # THE LIVENESS HALF (#151). `schtasks /query /xml` carries none of this, and
    # the audit cannot infer it: a repetition that will never fire is
    # indistinguishable from supervision in the declaration alone. Measured, on
    # the merged audit against the shape the live box actually carried: zero
    # findings and exit 0, while that desk had not restarted in twelve days.
    # `Get-ScheduledTaskInfo` is what Task Scheduler will actually DO next.
    #
    # Still read-only. Get-ScheduledTaskInfo queries; it changes nothing.
    $infoDest = Join-Path $OutDir "$name.info.json"
    $info = Get-ScheduledTaskInfo -TaskName $name -ErrorAction SilentlyContinue
    if ($null -eq $info) {
        # No sidecar is written, deliberately. The audit reports
        # liveness_unmeasured for a real task with no sidecar, and inventing an
        # empty one here would turn "could not measure" into "measured nothing",
        # which is the collapse this whole audit exists to prevent.
        if (Test-Path $infoDest) { Remove-Item $infoDest -Force }
        Write-Warning "no Get-ScheduledTaskInfo for $name (the audit will report liveness_unmeasured)"
    } else {
        # MultipleInstances comes from the task, not the info, and it is carried
        # here so the audit can read the benign-refusal tuple without
        # re-deriving it from the XML half.
        [ordered]@{
            task_name              = $name
            measured_utc           = (Get-Date).ToUniversalTime().ToString("o")
            state                  = [string] $existing.State
            multiple_instances     = [string] $existing.Settings.MultipleInstances
            last_task_result       = $info.LastTaskResult
            number_of_missed_runs  = $info.NumberOfMissedRuns
            next_run_time_utc      = (ConvertTo-UtcIso $info.NextRunTime)
            last_run_time_utc      = (ConvertTo-UtcIso $info.LastRunTime)
        } | ConvertTo-Json -Depth 4 | Out-File -FilePath $infoDest -Encoding utf8
        Write-Host "dumped $name liveness -> $infoDest (NextRunTime $($info.NextRunTime))"
    }
}

foreach ($name in $missing) {
    Write-Warning "no such scheduled task: $name (the audit will report task_missing)"
}

Write-Host ""
Write-Host "every task above should have BOTH a .xml and a .info.json."
Write-Host "a missing .info.json makes the audit report liveness_unmeasured,"
Write-Host "which is a FAILURE and not a pass (straightedge#151)."
Write-Host ""
Write-Host "now run the audit, with the DESK's own config:"
Write-Host "  python -m straightedge --config C:\path\to\config.toml supervision --tasks $OutDir"
