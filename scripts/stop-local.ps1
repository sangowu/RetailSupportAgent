<#
.SYNOPSIS
    Stop the local services. Data and experiment results are left alone.

.DESCRIPTION
    Only processes this project started and recorded are stopped, verified by both the
    recorded PID and its start time. A mismatch refuses the stop and says why. No name
    matching on "python" or "uvicorn": that could kill an evaluation the operator is
    running, which takes tens of minutes, or an unrelated program.

    Nothing on disk is touched: evaluation results, demo archives, baselines and .env
    are all untouched. To clear demo sessions, use scripts/reset-demo.ps1.

.PARAMETER Force
    Stop even while demo sessions are in progress. Without it, you are asked first.

.EXAMPLE
    pwsh scripts/stop-local.ps1
#>

[CmdletBinding()]
param([switch]$Force)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "local-common.ps1")

$services = @("ui", "api")   # UI first, so it does not error against a dead API
$anyRecorded = $false
$refused = @()

# Warn about sessions in progress: stopping loses their conversation and database
# changes, which live only in memory.
$apiRecord = Get-ServiceRecord "api"
if ($null -ne $apiRecord -and -not $Force) {
    $health = Test-Endpoint -Url "$($apiRecord.url)/health"
    if ($null -ne $health -and $health.active_sessions -gt 0) {
        Write-Host "$($health.active_sessions) demo sessions are in progress." -ForegroundColor Yellow
        Write-Host "Stopping loses their conversation and database changes, which are in memory only."
        $answer = Read-Host "Stop anyway? [y/N]"
        if ($answer -notmatch '^[yY]') {
            Write-Host "cancelled; nothing was stopped."
            exit 0
        }
    }
}

foreach ($name in $services) {
    $record = Get-ServiceRecord $name
    if ($null -eq $record) { continue }
    $anyRecorded = $true

    $result = Stop-OurProcess -Name $name
    switch ($result.Status) {
        "stopped"         { Write-Host "stopped $name (PID $($result.Pid))" -ForegroundColor Green }
        "already-stopped" { Write-Host "$name had already exited (PID $($result.Pid)); record removed" }
        "still-alive"     { Write-Host "$name (PID $($result.Pid)) did not stop; handle it manually" -ForegroundColor Red }
        "refused" {
            $refused += $name
            Write-Host "refused to stop ${name}: $($result.Detail)" -ForegroundColor Red
        }
        default           { Write-Host "${name}: $($result.Status)" }
    }
}

if (-not $anyRecorded) {
    Write-Host "no recorded services for this project; nothing to stop."
    Write-Host "(This script stops only what it started and never guesses which python is ours.)"
    exit 0
}

if ($refused.Count -gt 0) {
    Write-Host ""
    Write-Host "the PIDs above now belong to other processes, so they were not stopped." -ForegroundColor Yellow
    Write-Host "They are most likely your own programs. The records remain in .local/run/ for inspection."
    exit 1
}

Write-Host ""
Write-Host "stopped. Data and experiment results are unchanged."
Write-Host "  results evaluation/results/    demo archives evaluation/demo_runs/"
