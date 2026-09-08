<#
.SYNOPSIS
    Shared parts of the local start/stop scripts: process records, identity checks,
    health waiting.

.DESCRIPTION
    The important property here is that only processes we started are ever stopped.

    Windows reuses PIDs: the 4321 recorded at startup may belong to something else by
    the time the stop script runs. Stopping by PID alone, or worse by a name match such
    as "python", could kill an evaluation the operator is running, or an unrelated
    program. Each service therefore records its start time as well as its PID, and both
    must match before anything is stopped. On a mismatch the stop is refused with an
    explanation.

    This is not a theoretical risk: a τ²-bench run takes tens of minutes and is exactly
    the kind of thing that would be killed by accident.
#>

Set-StrictMode -Version Latest

$script:RepoRoot = Split-Path -Parent $PSScriptRoot
$script:RunDir = Join-Path $RepoRoot ".local/run"
$script:LogDir = Join-Path $RepoRoot ".local/logs"

function Get-RepoRoot { $script:RepoRoot }
function Get-LogDir { $script:LogDir }

function Initialize-LocalDirs {
    foreach ($dir in @($script:RunDir, $script:LogDir)) {
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    }
}

function Get-RecordPath([string]$Name) {
    Join-Path $script:RunDir "$Name.json"
}

function Get-LogPath([string]$Name) {
    Join-Path $script:LogDir "$Name.log"
}

function Save-ServiceRecord {
    <#
        Record a service we started. StartTime is what defeats PID reuse and must be
        stored in the round-trip format ("o"), never as a localized ToString().
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$Url
    )
    Initialize-LocalDirs
    $record = [ordered]@{
        name       = $Name
        pid        = $Process.Id
        start_time = $Process.StartTime.ToString("o")
        port       = $Port
        url        = $Url
        log        = Get-LogPath $Name
        recorded_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    $record | ConvertTo-Json | Set-Content -Path (Get-RecordPath $Name) -Encoding utf8
}

function Get-ServiceRecord([string]$Name) {
    $path = Get-RecordPath $Name
    if (-not (Test-Path $path)) { return $null }
    try { return Get-Content $path -Raw -Encoding utf8 | ConvertFrom-Json }
    catch { return $null }
}

function Remove-ServiceRecord([string]$Name) {
    $path = Get-RecordPath $Name
    if (Test-Path $path) { Remove-Item $path -Force }
}

function Resolve-OurProcess {
    <#
        Find the recorded process again and verify its identity.

        Returns:
          @{ Status = "running";  Process = <process> }  PID and start time both match
          @{ Status = "gone" }                           the process is no longer there
          @{ Status = "foreign"; Detail = "..." }        the PID belongs to something else
    #>
    param([Parameter(Mandatory)]$Record)

    $proc = Get-Process -Id $Record.pid -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return @{ Status = "gone" } }

    # The only reliable test against PID reuse: the same PID must still be the process
    # that started at the same moment.
    #
    # ConvertFrom-Json turns an ISO-8601 string into a DateTime object, so this value is
    # usually already a DateTime rather than a string. Parsing it unconditionally as a
    # string worked only because PowerShell implicitly called ToString() on it, using
    # the current culture's format; on a machine with different regional settings that
    # parse could fail, and a failed parse disables the guard.
    $raw = $Record.start_time
    if ($raw -is [datetime]) {
        $recorded = $raw
    } else {
        $recorded = [datetime]::Parse(
            [string]$raw, [cultureinfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind)
    }
    $actual = $proc.StartTime
    if ([math]::Abs(($actual - $recorded).TotalSeconds) -gt 2) {
        return @{
            Status = "foreign"
            Detail = "PID $($Record.pid) now started at $($actual.ToString('o')) but was recorded as $($recorded.ToString('o')): the PID has been taken by another process"
        }
    }
    return @{ Status = "running"; Process = $proc }
}

function Test-Endpoint {
    <# Probe a URL once. Returns the response body, or $null if it did not respond. #>
    param([Parameter(Mandatory)][string]$Url, [int]$TimeoutSec = 3)
    try {
        return Invoke-RestMethod -Uri $Url -TimeoutSec $TimeoutSec -ErrorAction Stop
    } catch {
        return $null
    }
}

function Wait-ForEndpoint {
    <#
        Wait until the URL responds, while also watching whether the process is still
        alive. Waiting out the full timeout after the process has exited only makes the
        operator wait tens of seconds for an uninformative "timeout" while the real
        reason is in the log.
    #>
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
        [int]$TimeoutSec = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            return @{ Ok = $false; Reason = "the process exited (exit code $($Process.ExitCode))" }
        }
        if ($null -ne (Test-Endpoint -Url $Url -TimeoutSec 2)) {
            return @{ Ok = $true }
        }
        Start-Sleep -Milliseconds 500
    }
    return @{ Ok = $false; Reason = "still not ready after $TimeoutSec seconds" }
}

function Test-PortFree {
    param([Parameter(Mandatory)][int]$Port)
    $inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -eq $inUse)
}

function Stop-OurProcess {
    <#
        Stop a process we started. A mismatched identity refuses the stop and reports why.

        Ask it to exit (CloseMainWindow usually does nothing for a hidden console
        process, so Stop-Process is used directly), then wait until it is really gone.
        The wait matters: returning immediately would make a following restart collide
        with a port that has not been released yet.
    #>
    param([Parameter(Mandatory)][string]$Name)

    $record = Get-ServiceRecord $Name
    if ($null -eq $record) { return @{ Status = "not-recorded" } }

    $found = Resolve-OurProcess -Record $record
    switch ($found.Status) {
        "gone" {
            Remove-ServiceRecord $Name
            return @{ Status = "already-stopped"; Pid = $record.pid }
        }
        "foreign" {
            # The record is kept so the operator can see what happened.
            return @{ Status = "refused"; Detail = $found.Detail; Pid = $record.pid }
        }
    }

    Stop-Process -Id $record.pid -Force -ErrorAction SilentlyContinue
    $deadline = (Get-Date).AddSeconds(10)
    while ((Get-Date) -lt $deadline -and (Get-Process -Id $record.pid -ErrorAction SilentlyContinue)) {
        Start-Sleep -Milliseconds 200
    }
    if (Get-Process -Id $record.pid -ErrorAction SilentlyContinue) {
        return @{ Status = "still-alive"; Pid = $record.pid }
    }
    Remove-ServiceRecord $Name
    return @{ Status = "stopped"; Pid = $record.pid }
}
