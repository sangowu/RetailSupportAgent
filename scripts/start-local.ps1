<#
.SYNOPSIS
    本地一键启动：检查依赖与配置，起服务，等健康，打印网址。

.DESCRIPTION
    起两个进程：
      api  FastAPI 业务接口（默认 127.0.0.1:8000）
      ui   Gradio 演示界面（默认 127.0.0.1:7860）

    只绑定 127.0.0.1。M0 用固定模拟身份，没有注册也没有鉴权——真正的认证是 M3
    的独立主题，这里不做也不假装做，所以**绝不能把它暴露到公网**。

    起服务本身不花钱：健康检查与界面渲染都不调模型，只有真的发消息才会计费。

    起不来时会把已经起来的那个一并停掉再退出——留下半套服务比什么都没起更糟：
    界面能开、一发消息就报错，排查方向全错。

.PARAMETER ApiPort
    业务接口端口，默认 8000。

.PARAMETER UiPort
    演示界面端口，默认 7860。

.PARAMETER SkipUi
    只起业务接口，不起界面。

.PARAMETER TimeoutSec
    单个服务等待健康的上限，默认 90 秒。首次启动要导入 τ²-bench，会慢一些。

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
$started = @()   # 已经起来的服务名，失败时按此回滚

function Fail([string]$Message) {
    Write-Host ""
    Write-Host "启动失败：$Message" -ForegroundColor Red
    if ($started.Count -gt 0) {
        Write-Host "正在清理本次已启动的服务……" -ForegroundColor Yellow
        foreach ($name in $started) {
            $r = Stop-OurProcess -Name $name
            Write-Host "  $name -> $($r.Status)"
        }
        Write-Host "清理完成。" -ForegroundColor Yellow
    } else {
        Write-Host "本次没有启动任何服务，无需清理。"
    }
    Write-Host "日志目录：$(Get-LogDir)"
    exit 1
}

# ── 起飞前检查 ────────────────────────────────────────────────
# 这些全部**不打印任何密钥值**，只回答"配没配"。

Write-Host "检查运行前提……"

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Fail "找不到 uv。安装见 https://docs.astral.sh/uv/getting-started/installation/"
}

# 服务直接用虚拟环境的解释器启动（理由见 Start-Service），所以它必须存在。
$script:VenvPython = Join-Path $repo ".venv/Scripts/python.exe"
if (-not (Test-Path $script:VenvPython)) {
    Fail "找不到虚拟环境（$script:VenvPython）。先运行：uv sync --group bench"
}

$envFile = Join-Path $repo ".env"
if (-not (Test-Path $envFile)) {
    Fail "缺少 .env。先 `cp .env.example .env` 并填入密钥（.env 已被 gitignore）。"
}
# 只判断"这一行有没有非空值"，绝不把值本身打出来或写进日志。
$hasKey = Select-String -Path $envFile -Pattern '^\s*DASHSCOPE_API_KEY\s*=\s*\S' -Quiet
if (-not $hasKey) {
    Fail ".env 里 DASHSCOPE_API_KEY 为空。被测模型走 DashScope 端点，没有它连不上。"
}

# 领域环境要从固定 commit 的 τ²-bench 数据里加载订单，缺了会在**第一次发消息时**
# 才炸——那时界面已经开着，看起来像模型的问题。所以提前查。
$dataDir = Join-Path $repo "third_party/tau2-bench/data"
if (-not (Test-Path $dataDir)) {
    Fail "缺少 τ²-bench 数据。先运行：uv run python scripts/fetch_tau2.py"
}

# 领域环境 import tau2，它在 bench 依赖组里，默认 `uv sync` 不装。
& $script:VenvPython -c "import tau2" 2>$null | Out-Null
if ($LASTEXITCODE -ne 0) {
    Fail "未安装 bench 依赖组（领域环境需要 tau2）。先运行：uv sync --group bench"
}

foreach ($check in @(@{ Port = $ApiPort; What = "业务接口" }, @{ Port = $UiPort; What = "演示界面" })) {
    if ($SkipUi -and $check.What -eq "演示界面") { continue }
    if (-not (Test-PortFree -Port $check.Port)) {
        Fail "端口 $($check.Port)（$($check.What)）已被占用。先跑 scripts/stop-local.ps1，或换端口重试。"
    }
}

Write-Host "前提检查通过。" -ForegroundColor Green
Initialize-LocalDirs

# ── 启动 ──────────────────────────────────────────────────────

function Start-Service {
    <#
        起一个后台服务并等它健康。窗口隐藏，stdout/stderr 分别落盘
        （Start-Process 不允许两者指向同一个文件）。

        **直接用虚拟环境里的 python，不走 `uv run`。** 实测踩过的坑：
        `uv run` 会先起一个 uv 进程，真正的服务是它的子进程；我们登记到的 PID
        是那个包装进程，停服时杀掉它**并不会**杀掉子进程——端口还占着、服务还活着，
        而登记文件已经删了，从此没人管得了它。直接起 python 让"登记的 PID"
        就是"服务本身"，身份核对才有意义。
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
    Write-Host "启动 $Name ……"
    $proc = Start-Process -FilePath $script:VenvPython -ArgumentList $ArgumentList `
        -WorkingDirectory $repo -WindowStyle Hidden -PassThru `
        -RedirectStandardOutput $log -RedirectStandardError $errLog

    Save-ServiceRecord -Name $Name -Process $proc -Port $Port -Url $Url
    $script:started += $Name

    $wait = Wait-ForEndpoint -Url $HealthUrl -Process $proc -TimeoutSec $TimeoutSec
    if (-not $wait.Ok) {
        Write-Host "  $Name 未能就绪：$($wait.Reason)" -ForegroundColor Red
        Write-Host "  日志：$log"
        Write-Host "  错误输出：$errLog"
        Fail "$Name 启动失败"
    }
    Write-Host "  $Name 就绪 -> $Url" -ForegroundColor Green
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

# ── 汇报 ──────────────────────────────────────────────────────

$health = Test-Endpoint -Url "$apiUrl/health"
Write-Host ""
Write-Host "已启动（仅本机可访问）" -ForegroundColor Green
Write-Host "  业务接口   $apiUrl        健康检查 $apiUrl/health"
if (-not $SkipUi) { Write-Host "  演示界面   http://127.0.0.1:$UiPort" }
if ($null -ne $health) {
    Write-Host "  演示身份   $($health.demo_user_id)（τ²-bench 模拟数据，不是真实用户）"
}
Write-Host ""
Write-Host "三类业务演示（会发起真实模型调用并计费）："
Write-Host "  uv run python scripts/demo_flows.py            # 查询 / 取消 / 退货 一次跑完"
Write-Host "  uv run python scripts/demo_flows.py --flow cancel"
Write-Host ""
Write-Host "重置演示数据：pwsh scripts/reset-demo.ps1"
Write-Host "停止：        pwsh scripts/stop-local.ps1"
Write-Host "日志：        $(Get-LogDir)"
