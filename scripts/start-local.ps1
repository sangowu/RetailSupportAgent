<#
.SYNOPSIS
    Start the local stack: check prerequisites, start the services, wait for health,
    print the URLs.

.DESCRIPTION
    Starts two processes:
      api  the FastAPI service (default 127.0.0.1:8000)
      ui   the Gradio demo UI (default 127.0.0.1:7860)

    Both bind to 127.0.0.1 only. M0 uses a fixed simulated identity with no registration
    and no authentication; real authentication is M3. This must not be exposed publicly.

    Starting costs nothing: health checks and rendering call no model, and only sending
    a message is billed.

    If one service fails to start, any already-started service is stopped before
    exiting. Leaving half a stack running is worse than nothing: the UI opens, the first
    message fails, and the investigation starts in the wrong place.

.PARAMETER ApiPort
    Port for the API, default 8000.

.PARAMETER UiPort
    Port for the demo UI, default 7860.

.PARAMETER SkipUi
    Start only the API.

.PARAMETER TimeoutSec
    How long to wait for one service to become healthy, default 90 seconds. The first
    start imports τ²-bench and is slower.

.EXAMPLE
    pwsh scripts/start-local.ps1
.EXAMPLE
    pwsh scripts/start-local.ps1 -ApiPort 8080 -SkipUi
#>

[CmdletBinding()]
param(
    [int]$ApiPort = 8000,
    [int]$UiPort = 7860,
    [switch]$SkipUi,
    [int]$TimeoutSec = 90
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "local-common.ps1")

$repo = Get-RepoRoot
$started = @()   # services started so far, rolled back on failure

function Fail([string]$Message) {
    Write-Host ""
    Write-Host "startup failed: $Message" -ForegroundColor Red
    if ($started.Count -gt 0) {
        Write-Host "stopping the services started by this run..." -ForegroundColor Yellow
        foreach ($name in $started) {
            $r = Stop-OurProcess -Name $name
            Write-Host "  $name -> $($r.Status)"
        }
        Write-Host "cleanup done." -ForegroundColor Yellow
    } else {
        Write-Host "nothing was started, so there is nothing to clean up."
    }
    Write-Host "log directory: $(Get-LogDir)"
    exit 1
}

# -- preflight checks -----------------------------------------------------
# None of these print a key value; they only answer whether one is configured.

Write-Host "checking prerequisites..."

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "uv not found. See https://docs.astral.sh/uv/getting-started/installation/"
}

# Services are launched with the virtualenv interpreter directly (see Start-Service),
# so it must exist.
$script:VenvPython = Join-Path $repo ".venv/Scripts/python.exe"
if (-not (Test-Path $script:VenvPython)) {
    Fail "virtualenv not found ($script:VenvPython). Run: uv sync --group bench"
}

$envFile = Join-Path $repo ".env"
if (-not (Test-Path $envFile)) {
    Fail "no .env. Run `cp .env.example .env` and fill in the keys (.env is gitignored)."
}
# Only whether the line has a non-empty value; the value itself is never printed or logged.
$hasKey = Select-String -Path $envFile -Pattern '^\s*DASHSCOPE_API_KEY\s*=\s*\S' -Quiet
if (-not $hasKey) {
    Fail "DASHSCOPE_API_KEY is empty in .env. The model under test uses the DashScope endpoint."
}

# The domain environment loads orders from the pinned τ²-bench data. Without it the
# failure appears on the first message, when the UI is already open and it looks like a
# model problem, so it is checked up front.
$dataDir = Join-Path $repo "third_party/tau2-bench/data"
if (-not (Test-Path $dataDir)) {
    Fail "τ²-bench data is missing. Run: uv run python scripts/fetch_tau2.py"
}

# The domain environment imports tau2, which lives in the bench dependency group and is
# not installed by a plain `uv sync`.
& $script:VenvPython -c "import tau2" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Fail "the bench dependency group is not installed (the domain needs tau2). Run: uv sync --group bench"
}

foreach ($check in @(@{ Port = $ApiPort; What = "the API" }, @{ Port = $UiPort; What = "the demo UI" })) {
    if ($SkipUi -and $check.What -eq "the demo UI") { continue }
    if (-not (Test-PortFree -Port $check.Port)) {
        Fail "port $($check.Port) ($($check.What)) is in use. Run scripts/stop-local.ps1 or pick another port."
    }
}

Write-Host "prerequisites ok." -ForegroundColor Green
Initialize-LocalDirs

# -- start ----------------------------------------------------------------

function Start-Service {
    <#
        Start one background service and wait for it to become healthy. The window is
        hidden and stdout and stderr go to separate files, because Start-Process does
        not allow both to point at the same file.

        The virtualenv python is launched directly rather than through `uv run`. This
        was measured: `uv run` starts a uv process and the real service is its child, so
        the recorded PID is the wrapper. Killing the wrapper does not kill the child,
        which keeps holding the port while the record file is already deleted, leaving
        nothing able to manage it. Launching python directly makes the recorded PID the
        service itself, which is what makes the identity check meaningful.
    #>
    param(
        [Parameter(Mandatory)][string]$Name,
        [Parameter(Mandatory)][string[]]$ArgumentList,
        [Parameter(Mandatory)][int]$Port,
        [Parameter(Mandatory)][string]$HealthUrl,
        [Parameter(Mandatory)][string]$Url
    )
    $log = Get-LogPath $Name
    $errLog = "$log.err"
    Write-Host "starting $Name ..."
    $proc = Start-Process -FilePath $script:VenvPython -ArgumentList $ArgumentList `
        -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError $errLog

    Save-ServiceRecord -Name $Name -Process $proc -Port $Port -Url $Url
    $script:started += $Name

    $wait = Wait-ForEndpoint -Url $HealthUrl -Process $proc -TimeoutSec $TimeoutSec
    if (-not $wait.Ok) {
        Write-Host "  $Name did not become ready: $($wait.Reason)" -ForegroundColor Red
        Write-Host "  log: $log"
        Write-Host "  stderr: $errLog"
        Fail "$Name failed to start"
    }
    Write-Host "  $Name ready -> $Url" -ForegroundColor Green
}

$apiUrl = "http://127.0.0.1:$ApiPort"
Start-Service -Name "api" -Port $ApiPort -Url $apiUrl -HealthUrl "$apiUrl/health" `
    -ArgumentList @(
        "-m", "uvicorn", "retail_support_agent.api:create_app",
        "--factory", "--host", "127.0.0.1", "--port", "$ApiPort"
    )

if (-not $SkipUi) {
    $uiUrl = "http://127.0.0.1:$UiPort"
    Start-Service -Name "ui" -Port $UiPort -Url $uiUrl -HealthUrl $uiUrl `
        -ArgumentList @("-m", "retail_support_agent.ui")
}

# -- report ---------------------------------------------------------------

$health = Test-Endpoint -Url "$apiUrl/health"
Write-Host ""
Write-Host "started (local access only)" -ForegroundColor Green
Write-Host "  API        $apiUrl        health $apiUrl/health"
if (-not $SkipUi) { Write-Host "  demo UI    http://127.0.0.1:$UiPort" }
if ($null -ne $health) {
    Write-Host "  identity   $($health.demo_user_id) (τ²-bench simulated data, not a real user)"
}
Write-Host ""
Write-Host "demo flows (these make real model calls and are billed):"
Write-Host "  uv run python scripts/demo_flows.py            # lookup / cancel / return in one go"
Write-Host "  uv run python scripts/demo_flows.py --flow cancel"
Write-Host ""
Write-Host "reset demo data: pwsh scripts/reset-demo.ps1"
Write-Host "stop:            pwsh scripts/stop-local.ps1"
Write-Host "logs:            $(Get-LogDir)"
