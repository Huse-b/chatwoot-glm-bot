<#
.SYNOPSIS
    注册 Chatwoot 轮询机器人为 Windows 计划任务，实现登录自启 + 失败自动重启的常驻运行。
.DESCRIPTION
    以当前用户身份运行（DPAPI 加密凭据用的是用户级密钥，必须同一 Windows 账户才能解密）。
    登录时触发；异常退出按 1 分钟间隔自动重启（最多 999 次）；单实例运行避免重复回复。
    无人值守前必须先用 --save-credentials 录入加密凭据，本脚本会预检，缺失则中止且不注册任何任务。
.PARAMETER Inbox
    网站收件箱 ID，默认 7。
.PARAMETER Start
    加上该开关则注册后立即启动任务。
.NOTES
    令牌绝不出现在本脚本或任务定义里；任务只调用 chatwoot_test_bot.py，凭据由 DPAPI 加密文件提供。
#>
[CmdletBinding()]
param(
    [int]$Inbox = 7,
    [string]$TaskName = 'ChatwootOdooPolling',
    [string]$ProjectDir = 'D:\chatwoot-odoo-bot',
    [string]$PythonExe = '',
    [switch]$Start
)

$ErrorActionPreference = 'Stop'
$scriptPath  = Join-Path $ProjectDir 'chatwoot_test_bot.py'
$credFile    = Join-Path $ProjectDir 'chatwoot_test_bot.credentials'
$currentUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name

# 1) 脚本存在性预检
if (-not (Test-Path $scriptPath)) { throw "找不到机器人脚本：$scriptPath" }

# 2) 定位 Python：无人值守优先 pythonw.exe（无控制台窗口），否则退回 python.exe
if (-not $PythonExe) {
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if (-not $cmd) { throw '未找到 python，请用 -PythonExe 指定完整路径。' }
    $PythonExe = $cmd.Source
}
$pythonw = Join-Path (Split-Path $PythonExe -Parent) 'pythonw.exe'
$runExe  = if (Test-Path $pythonw) { $pythonw } else { $PythonExe }

# 3) 凭据预检：DPAPI 加密文件或环境变量至少要有一个，否则任务会卡在交互输入
$hasEnv = ($env:CHATWOOT_USER_TOKEN -and $env:CHATWOOT_BOT_TOKEN)
if (-not (Test-Path $credFile) -and -not $hasEnv) {
    Write-Warning "未检测到加密凭据文件：$credFile，也没有设置 CHATWOOT_USER_TOKEN / CHATWOOT_BOT_TOKEN 环境变量。"
    Write-Warning "请先运行： python `"$scriptPath`" --save-credentials  录入并加密保存两个令牌，再重新安装。"
    throw '缺少无人值守凭据，已中止安装（未注册任何任务）。'
}

# 4) 组装动作 / 触发器 / 主体 / 设置
$action    = New-ScheduledTaskAction -Execute $runExe -Argument "`"$scriptPath`" --polling --inbox $Inbox" -WorkingDirectory $ProjectDir
$trigger   = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$principal = New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$settings  = New-ScheduledTaskSettingsSet -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -StartWhenAvailable -WakeToRun -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -MultipleInstances IgnoreNew

# 5) 注册（-Force 覆盖同名旧任务）
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Description "Chatwoot 轮询机器人常驻服务（收件箱 $Inbox）" -Force | Out-Null

Write-Host "已注册计划任务：$TaskName"
Write-Host "  运行账户 : $currentUser"
Write-Host "  执行程序 : $runExe"
Write-Host "  命令行   : `"$scriptPath`" --polling --inbox $Inbox"

if ($Start) {
    Start-ScheduledTask -TaskName $TaskName
    Write-Host '已触发启动，稍等片刻后可用 check_status.ps1 查看状态。'
} else {
    Write-Host "立即启动： Start-ScheduledTask -TaskName '$TaskName'"
}
Write-Host "查看状态： powershell -NoProfile -File `"$ProjectDir\check_status.ps1`""
