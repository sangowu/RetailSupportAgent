<#
.SYNOPSIS
    本地启停脚本的共用部分：进程登记、身份核对、健康等待。

.DESCRIPTION
    这里最要紧的一件事是**只停我们自己启的进程**。

    Windows 会复用 PID：我们启动时记下的 4321，等到停服时可能已经是别人的进程了。
    只按 PID 停，或者更糟——按 "python" 之类的名字模糊匹配去停，都可能杀掉操作者
    正在跑的评测甚至无关程序。所以每个服务登记的不只是 PID，还有**进程启动时刻**，
    停之前两者都要对上；对不上就拒绝停并说明原因，让人自己处理。

    这不是理论风险：一轮 τ²-bench 评测要跑几十分钟，正是最容易被误杀的东西。
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
        登记一个我们启动的服务。StartTime 是防 PID 复用的关键，
        必须存成不受本地化影响的往返格式（"o"），不能存 ToString() 的本地时间。
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
        按登记信息找回进程，并核对身份。

        返回：
          @{ Status = "running";  Process = <进程> }   PID 与启动时刻都对上
          @{ Status = "gone" }                          进程已不在（正常停过或崩了）
          @{ Status = "foreign"; Detail = "..." }       PID 被别人占了——**绝不能停**
    #>
    param([Parameter(Mandatory)]$Record)

    $proc = Get-Process -Id $Record.pid -ErrorAction SilentlyContinue
    if ($null -eq $proc) { return @{ Status = "gone" } }

    # PID 复用的唯一可靠判据：同一个 PID 必须还是同一时刻启动的那个进程。
    #
    # ConvertFrom-Json 会把 ISO-8601 字符串**自动转成 DateTime 对象**，所以这里
    # 拿到的通常已经是 DateTime，不是字符串。原先无条件按字符串解析能跑通，
    # 只是因为 PowerShell 又把它隐式 ToString() 回去了——那用的是**当地格式**，
    # 换一台区域设置不同的机器就可能解析失败，而失败的后果是守卫失效。
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
            Detail = "PID $($Record.pid) 现在的启动时刻是 $($actual.ToString('o'))，登记的是 $($recorded.ToString('o'))——这个 PID 已被别的进程占用"
        }
    }
    return @{ Status = "running"; Process = $proc }
}

function Test-Endpoint {
    <# 探一次地址。通了返回响应体，没通返回 $null。#>
    param([Parameter(Mandatory)][string]$Url, [int]$TimeoutSec = 3)
    try {
        return Invoke-RestMethod -Uri $Url -TimeoutSec $TimeoutSec -ErrorAction Stop
    } catch {
        return $null
    }
}

function Wait-ForEndpoint {
    <#
        等到地址可用为止。**同时盯着进程是否还活着**——进程已经退了还傻等到超时，
        只会让操作者多等几十秒才看到一个没有信息量的"超时"，而真正的原因在日志里。
    #>
    param(
        [Parameter(Mandatory)][string]$Url,
        [Parameter(Mandatory)][System.Diagnostics.Process]$Process,
        [int]$TimeoutSec = 60
    )
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline) {
        if ($Process.HasExited) {
            return @{ Ok = $false; Reason = "进程已退出（退出码 $($Process.ExitCode)）" }
        }
        if ($null -ne (Test-Endpoint -Url $Url -TimeoutSec 2)) {
            return @{ Ok = $true }
        }
        Start-Sleep -Milliseconds 500
    }
    return @{ Ok = $false; Reason = "等待 $TimeoutSec 秒仍未就绪" }
}

function Test-PortFree {
    param([Parameter(Mandatory)][int]$Port)
    $inUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    return ($null -eq $inUse)
}

function Stop-OurProcess {
    <#
        停一个我们启的进程。身份对不上就**拒绝停**并如实报告。

        先请它自己退（CloseMainWindow 对隐藏窗口的控制台进程通常无效，所以直接
        Stop-Process），再等它真的走。等待是必要的：立刻返回会让紧接着的重启撞上
        还没释放的端口。
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
            # 不删登记：留着让人能看到发生过什么。
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
