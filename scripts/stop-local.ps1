<#
.SYNOPSIS
    停止本地服务。默认保留数据与实验结果。

.DESCRIPTION
    只停**本项目启动并登记过**的进程：按登记的 PID 加启动时刻双重核对，
    对不上就拒绝停并说明原因。不会按 "python"、"uvicorn" 之类的名字去模糊匹配——
    那样很可能杀掉操作者正在跑的评测（一轮要几十分钟）或别的无关程序。

    不动磁盘：评测结果、演示归档、基线、.env 一概不碰。要清演示会话用
    scripts/reset-demo.ps1。

.PARAMETER Force
    即使还有正在进行的演示会话也直接停。默认会先提示。

.EXAMPLE
    pwsh scripts/stop-local.ps1
#>

[CmdletBinding()]
param([switch]$Force)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "local-common.ps1")

$services = @("ui", "api")   # 先停界面再停接口，避免界面对着死掉的接口报错
$anyRecorded = $false
$refused = @()

# 有正在进行的演示就先说一声——停掉会丢掉会话里的对话和数据库改动（都在内存里）。
$apiRecord = Get-ServiceRecord "api"
if ($null -ne $apiRecord -and -not $Force) {
    $health = Test-Endpoint -Url "$($apiRecord.url)/health"
    if ($null -ne $health -and $health.active_sessions -gt 0) {
        Write-Host "当前有 $($health.active_sessions) 个演示会话正在进行。" -ForegroundColor Yellow
        Write-Host "停止会丢掉这些会话的对话与数据库改动（都只在内存里）。"
        $answer = Read-Host "仍要停止吗？[y/N]"
        if ($answer -notmatch '^[yY]') {
            Write-Host "已取消，未停止任何服务。"
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
        "stopped"         { Write-Host "已停止 $name（PID $($result.Pid)）" -ForegroundColor Green }
        "already-stopped" { Write-Host "$name 早已退出（PID $($result.Pid)），清掉登记" }
        "still-alive"     { Write-Host "$name（PID $($result.Pid)）没能停下，请手动处理" -ForegroundColor Red }
        "refused" {
            $refused += $name
            Write-Host "拒绝停止 $name：$($result.Detail)" -ForegroundColor Red
        }
        default           { Write-Host "$name：$($result.Status)" }
    }
}

if (-not $anyRecorded) {
    Write-Host "没有登记在案的本项目服务，无需停止。"
    Write-Host "（本脚本只停自己启动过的进程，不会去猜哪个 python 属于本项目。）"
    exit 0
}

if ($refused.Count -gt 0) {
    Write-Host ""
    Write-Host "以上服务的 PID 已被别的进程占用，本脚本不会去停它——" -ForegroundColor Yellow
    Write-Host "那有很大概率是你自己的其他程序。登记文件保留在 .local/run/ 供查证。"
    exit 1
}

Write-Host ""
Write-Host "已停止。数据与实验结果未改动。"
Write-Host "  评测结果 evaluation/results/    演示归档 evaluation/demo_runs/"
