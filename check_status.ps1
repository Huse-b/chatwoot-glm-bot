<#
.SYNOPSIS
    查看 Chatwoot 轮询机器人常驻服务状态：计划任务、运行进程、最近日志、累计回复数。
.DESCRIPTION
    只读检查，不改动任何配置。用于确认常驻服务是否存活、是否还在正常轮询与回复。
    “上次结果”为 0 表示上次运行正常退出；非 0 通常意味着脚本因致命错误退出（如令牌失效），
    此时任务会按设置自动重启，可结合最近日志判断原因。
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'ChatwootOdooPolling',
    [string]$ProjectDir = 'D:\chatwoot-odoo-bot',
    [int]$LogTail = 15
)
$ErrorActionPreference = 'SilentlyContinue'

Write-Host '==== 计划任务 ===='
$task = Get-ScheduledTask -TaskName $TaskName
if ($task) {
    $info = Get-ScheduledTaskInfo -TaskName $TaskName
    Write-Host ("状态: {0} | 上次运行: {1} | 上次结果: {2} | 下次运行: {3}" -f $task.State, $info.LastRunTime, $info.LastTaskResult, $info.NextRunTime)
} else {
    Write-Host "未注册计划任务：$TaskName"
}

Write-Host ''
Write-Host '==== 运行进程 ===='
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" |
    Where-Object { $_.CommandLine -like '*chatwoot_test_bot.py*' }
if ($procs) {
    $procs | ForEach-Object { Write-Host ("PID {0} | {1}" -f $_.ProcessId, $_.CommandLine) }
} else {
    Write-Host '没有正在运行的 chatwoot_test_bot.py 进程。'
}

Write-Host ''
Write-Host "==== 最近日志（chatwoot_test_bot.log 末尾 $LogTail 行）===="
$log = Join-Path $ProjectDir 'chatwoot_test_bot.log'
if (Test-Path $log) { Get-Content $log -Tail $LogTail -Encoding UTF8 } else { Write-Host "尚无日志文件：$log" }

Write-Host ''
Write-Host '==== 累计回复数（sqlite attempts）===='
$py = (Get-Command python).Source
if ($py) { & $py (Join-Path $ProjectDir 'check_attempts.py') } else { Write-Host '未找到 python。' }
