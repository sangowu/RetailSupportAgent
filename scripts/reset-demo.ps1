<#
.SYNOPSIS
    重置演示数据。只清演示会话，不删基线、评测结果或任何磁盘上的东西。

.DESCRIPTION
    M0 的演示数据全在内存里：每个会话各持一份 τ²-bench 数据库副本，
    所以"重置"就是丢掉会话——下次发消息会拿到一份全新的初始数据。

    **本脚本不删除任何文件。** 评测结果、演示归档、基线、.env 都不碰；
    要清它们请自己动手，本脚本不提供这个能力，免得一次误操作毁掉不可重现的实验。

    服务没起时会明说，而不是假装重置成功——那会让人以为清干净了，
    等下次起服务时又看到旧会话。

.PARAMETER Session
    只重置指定会话。省略则重置全部会话。

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
    Write-Host "业务接口没有在运行（没有登记在案的实例），无需重置。" -ForegroundColor Yellow
    Write-Host "演示数据只存在于运行中的进程内存里，服务停了就已经是初始状态。"
    exit 0
}

$health = Test-Endpoint -Url "$($record.url)/health"
if ($null -eq $health) {
    Write-Host "登记了业务接口（PID $($record.pid)），但 $($record.url)/health 探不通。" -ForegroundColor Red
    Write-Host "服务可能已经崩了。先跑 scripts/stop-local.ps1 清理，再重新启动。"
    exit 1
}

if ($Session) {
    # 重置是 POST；Test-Endpoint 只做 GET 探活，这里不能用它
    $result = Invoke-RestMethod -Uri "$($record.url)/session/$Session/reset" -Method Post -TimeoutSec 10
    Write-Host "已重置会话 $($result.session_id)。" -ForegroundColor Green
} else {
    $before = $health.active_sessions
    $result = Invoke-RestMethod -Uri "$($record.url)/sessions/reset" -Method Post -TimeoutSec 10
    if ($result.cleared -eq 0) {
        Write-Host "本来就没有活动会话，演示数据已是初始状态。" -ForegroundColor Green
    } else {
        Write-Host "已重置 $($result.cleared) 个演示会话（原有 $before 个）。" -ForegroundColor Green
    }
}

Write-Host ""
Write-Host "未改动任何磁盘数据："
Write-Host "  评测结果 evaluation/results/    演示归档 evaluation/demo_runs/    .env"
