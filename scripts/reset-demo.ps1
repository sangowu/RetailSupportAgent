<#
.SYNOPSIS
    Reset the demo data. Clears demo sessions only; no baseline, result, or file on disk
    is deleted.

.DESCRIPTION
    M0's demo data lives entirely in memory: each session holds its own copy of the
    τ²-bench database, so resetting means dropping sessions, and the next message starts
    from fresh initial data.

    This script deletes no files. Evaluation results, demo archives, baselines and .env
    are untouched, and it offers no way to remove them, so one mistake cannot destroy an
    experiment that cannot be reproduced.

    If the service is not running it says so rather than reporting a successful reset,
    which would leave the impression everything was cleared until old sessions reappear
    on the next start.

.PARAMETER Session
    Reset one session. Without it, every session is reset.

.EXAMPLE
    pwsh scripts/reset-demo.ps1
.EXAMPLE
    pwsh scripts/reset-demo.ps1 -Session demo-1
#>

[CmdletBinding()]
param([string]$Session)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "local-common.ps1")

$record = Get-ServiceRecord "api"
if ($null -eq $record) {
    Write-Host "the API is not running (no recorded instance); nothing to reset." -ForegroundColor Yellow
    Write-Host "Demo data exists only in a running process, so with the service stopped it is already in its initial state."
    exit 0
}

$health = Test-Endpoint -Url "$($record.url)/health"
if ($null -eq $health) {
    Write-Host "the API is recorded (PID $($record.pid)) but $($record.url)/health does not respond." -ForegroundColor Red
    Write-Host "The service may have crashed. Run scripts/stop-local.ps1 to clean up, then start again."
    exit 1
}

if ($Session) {
    # Reset is a POST; Test-Endpoint only does a GET probe and cannot be used here.
    $result = Invoke-RestMethod -Uri "$($record.url)/session/$Session/reset" -Method Post -TimeoutSec 10
    Write-Host "reset session $($result.session_id)." -ForegroundColor Green
} else {
    $before = $health.active_sessions
    $result = Invoke-RestMethod -Uri "$($record.url)/sessions/reset" -Method Post -TimeoutSec 10
    if ($result.cleared -eq 0) {
        Write-Host "there were no active sessions; the demo data is already in its initial state." -ForegroundColor Green
    } else {
        Write-Host "reset $($result.cleared) demo sessions (there were $before)." -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "nothing on disk was changed:"
Write-Host "  results evaluation/results/    demo archives evaluation/demo_runs/    .env"
