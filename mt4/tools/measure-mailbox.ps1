<#
.SYNOPSIS
    Measure the MT4 file-mailbox round trip, and count requests the Expert
    answered with silence.

.DESCRIPTION
    This is the instrument the claim-open and reply-delivery fixes were measured
    with. It watches the mailbox directory from OUTSIDE both processes, which is
    the only vantage that can see the failure it was built to find: the Expert
    consuming a request and producing no reply, logging nothing.

    It reports, with denominators:

        requests published      .req renamed into place by the desk
        claims taken/released   the Expert private claim appearing/disappearing
        res_tmp_created         the Expert reply staging file being created
        replies published       .res renamed into place by the Expert
        unanswered requests     a request with no reply before the next request,
                                and whether a reply staging file ever appeared

    That last column is the discriminator and it is the whole point. If the
    staging file WAS created, the Expert opened the reply file and the failure is
    in the publish (delete then rename). If it was NOT, the reply FileOpen itself
    failed. Those are different bugs and these counts tell them apart.

    WHAT IT CANNOT SEE, and you must say so whenever you quote it:
      * FileSystemWatcher drops events on internal buffer overflow, and this
        script does NOT register the Error event. So a missing reply could in
        principle be a missed notification rather than a real loss. Corroborate
        every gap against journal.jsonl, where a real loss appears as a
        loop_error or reconnect roughly one adapter budget later. Two observers
        or it did not happen.
      * Round-trip latency USED to be paired naively, each request to the next
        reply, so one unanswered request shifted every later pairing. The 2026-09
        run's median survived that only because all three of its losses fell in
        the last 18% of the window; replayed with one loss at 20% in, the old
        pairing reads 418ms against a true 206ms. "The median is trustworthy"
        was never a property of the algorithm. Fixed in #127: the pairing is now a single
        pass over the same events, holding the open request, and a request that
        got no reply is EXCLUDED rather than paired with somebody else's. That
        is sound because the mailbox is a strict singleton by contract (one
        .req at a time, the desk blocks on its reply), so a .res that lands
        between two .req renames belongs to the first of them and to nothing
        else. No file is read to achieve it; see the read-only note below, which
        is the reason request ids are NOT used.
      * A dropped watcher event therefore costs ONE excluded sample now instead
        of corrupting every sample after it. It still costs that sample, and the
        buffer-overflow caveat above still applies, so `paired` and `unanswered`
        are printed next to every percentile and must be quoted with them.
      * THE PAIRING FIX IS UNVERIFIED AGAINST A LIVE TERMINAL. It was written on
        a Mac with no Windows host and no MT4, from the event log format this
        script already produces. Treat the first live run as a test of the
        instrument as much as of the mailbox: `paired` plus `unanswered` should
        account for `requests_published`, give or take the final in-flight one.
      * It is read-only. It never opens, writes, renames or deletes a mailbox
        file, because doing so would perturb the claim-by-rename protocol it is
        supposed to be measuring.

.PARAMETER FilesDir
    The MT4 Common Files directory, i.e. mt4.files_dir from config.toml.

.PARAMETER Seconds
    How long to watch. A useful window has to cover several failures at the
    measured rate of about 0.14 percent: at roughly 87 requests a minute, 25
    minutes is about 2200 requests and expects about three.

.PARAMETER Base
    Mailbox basename, declared exactly once here on purpose.

.EXAMPLE
    Run it IN the session, never detached. Windows OpenSSH kills a detached child
    when the SSH session closes, which silently ends the measurement and leaves a
    short log that reads exactly like a clean window.

    powershell -NoProfile -ExecutionPolicy Bypass -File measure-mailbox.ps1 -FilesDir "C:\Users\cbgb\AppData\Roaming\MetaQuotes\Terminal\Common\Files" -Seconds 1500
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string] $FilesDir,
    [int] $Seconds = 1500,
    [string] $Base = 'mt4_risk_bot',
    [string] $LogPath = ''
)

$ErrorActionPreference = 'Stop'
if (-not (Test-Path $FilesDir)) { throw "no such directory: $FilesDir" }
if ($LogPath -eq '') { $LogPath = Join-Path ([System.IO.Path]::GetTempPath()) "measure-mailbox-$PID.log" }

$reqName = "$Base.req"
$resName = "$Base.res"
$resTmp  = "$Base.res.tmp"
$claimPrefix = "$Base.req.claim."

Add-Content -Path $LogPath -Value ("START|" + (Get-Date).ToUniversalTime().ToString('o') + "|seconds=$Seconds")
$w = New-Object System.IO.FileSystemWatcher
$w.Path = $FilesDir
$w.Filter = '*'
$w.IncludeSubdirectories = $false
$w.NotifyFilter = [System.IO.NotifyFilters]::FileName -bor [System.IO.NotifyFilters]::LastWrite -bor [System.IO.NotifyFilters]::Size
$act = {
    $ea = $Event.SourceEventArgs
    Add-Content -Path $Event.MessageData -Value ("{0}|{1}|{2}" -f (Get-Date).ToUniversalTime().ToString('o'), $ea.ChangeType, $ea.Name)
}
foreach ($n in @('Created', 'Changed', 'Deleted', 'Renamed')) {
    Register-ObjectEvent -InputObject $w -EventName $n -Action $act -MessageData $LogPath | Out-Null
}
$w.EnableRaisingEvents = $true
Write-Host "watching $FilesDir for $Seconds s, events to $LogPath"
$end = (Get-Date).AddSeconds($Seconds)
while ((Get-Date) -lt $end) { Start-Sleep -Milliseconds 200 }
$w.EnableRaisingEvents = $false
Add-Content -Path $LogPath -Value ("END|" + (Get-Date).ToUniversalTime().ToString('o'))

$lines = Get-Content $LogPath
$reqTimes = New-Object System.Collections.Generic.List[datetime]
$resTimes = New-Object System.Collections.Generic.List[datetime]
$claimTaken = 0; $claimReleased = 0; $resTmpCreated = 0
foreach ($l in $lines) {
    if ($l -notmatch '^\d{4}-') { continue }
    $p = $l.Split('|'); $t = [datetime]::Parse($p[0]).ToUniversalTime()
    if ($p[1] -eq 'Renamed' -and $p[2] -eq $reqName) { $reqTimes.Add($t) }
    if ($p[1] -eq 'Renamed' -and $p[2] -eq $resName) { $resTimes.Add($t) }
    if ($p[1] -eq 'Created' -and $p[2] -eq $resTmp) { $resTmpCreated++ }
    if ($p[2].StartsWith($claimPrefix)) {
        if ($p[1] -eq 'Renamed') { $claimTaken++ }
        if ($p[1] -eq 'Deleted') { $claimReleased++ }
    }
}
"requests_published = $($reqTimes.Count)"
"claims_taken       = $claimTaken"
"claims_released    = $claimReleased"
"res_tmp_created    = $resTmpCreated"
"replies_published  = $($resTimes.Count)"

$span = 0.0
if ($reqTimes.Count -gt 1) { $span = ($reqTimes[$reqTimes.Count - 1] - $reqTimes[0]).TotalSeconds }
if ($span -gt 0) { "request_rate_per_min = $([math]::Round($reqTimes.Count / $span * 60, 1))" }

# One pass, holding the open request. A .res while a request is open is THAT
# request reply. A second .req while one is still open means the first was never
# answered, and it is dropped rather than paired with the next reply: the naive
# forward walk this replaces shifted every later sample by one each time that
# happened, which is why only the median was ever quotable (#127).
$lat = New-Object System.Collections.Generic.List[double]
$dropped = 0
$openAt = $null
foreach ($l in $lines) {
    if ($l -notmatch '^\d{4}-') { continue }
    $p = $l.Split('|')
    if ($p[1] -ne 'Renamed') { continue }
    $t = [datetime]::Parse($p[0]).ToUniversalTime()
    if ($p[2] -eq $reqName) {
        if ($openAt -ne $null) { $dropped++ }
        $openAt = $t
        continue
    }
    if ($p[2] -eq $resName -and $openAt -ne $null) {
        $lat.Add(($t - $openAt).TotalMilliseconds)
        $openAt = $null
    }
}
if ($openAt -ne $null) { $dropped++ }   # the last request may still be in flight
function Pct($sorted, $q) { $sorted[[int][math]::Floor($q * ($sorted.Count - 1))] }
if ($lat.Count -gt 0) {
    $s = @($lat | Sort-Object)
    "round_trip_paired  = $($s.Count)"
    "round_trip_unpaired= $dropped  (excluded, NOT paired with a later reply)"
    "round_trip_p50_ms  = $([math]::Round((Pct $s 0.50), 1))"
    "round_trip_p90_ms  = $([math]::Round((Pct $s 0.90), 1))"
    "round_trip_p99_ms  = $([math]::Round((Pct $s 0.99), 1))"
    "round_trip_max_ms  = $([math]::Round(($s[$s.Count - 1]), 1))"
    "NOTE: quote these ONLY with round_trip_paired and round_trip_unpaired. A"
    "percentile without its denominator is an opinion. A dropped watcher event"
    "costs one excluded sample and cannot shift the others, but it is still a"
    "sample this instrument did not see."
} else {
    "round_trip_paired  = 0  (NOTHING was measured; this is not a clean window)"
}

"--- unanswered requests, corroborate each against journal.jsonl ---"
$state = 'idle'; $tReq = $null; $sawTmp = $false; $n = 0
foreach ($l in $lines) {
    if ($l -notmatch '^\d{4}-') { continue }
    $p = $l.Split('|')
    if ($p[1] -eq 'Renamed' -and $p[2] -eq $reqName) {
        if ($state -eq 'open') { $n++; "unanswered #$n at $tReq  res_tmp_created=$sawTmp" }
        $state = 'open'; $tReq = $p[0]; $sawTmp = $false; continue
    }
    if ($state -eq 'open' -and $p[1] -eq 'Created' -and $p[2] -eq $resTmp) { $sawTmp = $true }
    if ($state -eq 'open' -and $p[1] -eq 'Renamed' -and $p[2] -eq $resName) { $state = 'idle' }
}
"unanswered_total = $n"
"NOTE: a request whose reply lands in the same watcher batch as the following"
"request can appear here spuriously. Treat every gap as a CANDIDATE until"
"journal.jsonl shows a loop_error or reconnect about one adapter budget later."
