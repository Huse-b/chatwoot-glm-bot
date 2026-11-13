<#
.SYNOPSIS
    停止并注销 Chatwoot 轮询机器人计划任务。不会删除凭据文件、日志或 sqlite 状态。
.DESCRIPTION
    只移除常驻任务本身；加密凭据、日志、sqlite 游标都保留，方便以后重新安装或排查。
    另外会提示是否还有你手动前台启动、未被任务管理的 chatwoot_test_bot.py 进程。
#>
[CmdletBinding()]
param(
    [string]$TaskName = 'ChatwootOdooPolling'
)
$ErrorActionPreference = 'Stop'

$task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
if (-not $task) {
    Write-Host "未找到计划任务：$TaskName（可能已注销）。"
} else {
    Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Host "已停止并注销计划任务：$TaskName。"
}

# 手动前台跑过的进程不受任务管理，这里只做提示，不强杀，避免误伤
$procs = Get-CimInstance Win32_Process -Filter "Name='python.exe' OR Name='pythonw.exe'" -ErrorAction SilentlyContinue |
    Where-Object { $_.CommandLine -like '*chatwoot_test_bot.py*' }
if ($procs) {
    Write-Warning '仍有 chatwoot_test_bot.py 进程在运行（可能是你手动前台启动的）：'
    $procs | ForEach-Object { Write-Warning ("  PID {0}" -f $_.ProcessId) }
    Write-Warning '如需彻底停服，请自行确认后手动结束这些进程。'
} else {
    Write-Host '没有残留的 chatwoot_test_bot.py 进程。'
}
