# 宽基ETF看板：Windows 本机定时更新（可选，用于本地长期运行或内网演示）
# 用法（普通 PowerShell 即可，无需管理员）：
#   powershell -ExecutionPolicy Bypass -File deploy\windows-task.ps1 -Action install
#   powershell -ExecutionPolicy Bypass -File deploy\windows-task.ps1 -Action run
#   powershell -ExecutionPolicy Bypass -File deploy\windows-task.ps1 -Action status
#   powershell -ExecutionPolicy Bypass -File deploy\windows-task.ps1 -Action uninstall
# 说明：触发器使用本机时区，请确认系统时区为“中国标准时间”（北京时间）。
#       脚本自身仍按 Asia/Shanghai 判断交易日，周末和法定休市日会跳过。

param(
  [ValidateSet('install', 'run', 'status', 'uninstall')]
  [string]$Action = 'install',
  [string]$TaskName = '宽基ETF看板每日更新',
  [string]$Python = '',
  [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
)

$ErrorActionPreference = 'Stop'
if (-not $Python) {
  $Python = (Get-Command python -ErrorAction Stop).Source
}
if (-not (Test-Path -LiteralPath (Join-Path $ProjectRoot 'update_daily.py'))) {
  throw "未找到 update_daily.py：$ProjectRoot"
}

$argument = "`"$(Join-Path $ProjectRoot 'update_daily.py')`" --work `"$(Join-Path $ProjectRoot 'data\current\history.json')`" --published `"$(Join-Path $ProjectRoot 'public\api\history.json')`" --log-dir `"$(Join-Path $ProjectRoot 'logs')`" --backup-dir `"$(Join-Path $ProjectRoot 'data\backups')`" --direct"

switch ($Action) {
  'install' {
    $existing = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($existing) { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false }
    $taskAction = New-ScheduledTaskAction -Execute $Python -Argument $argument -WorkingDirectory $ProjectRoot
    $trigger = New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At '08:00'
    $settings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Hours 2) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
    Register-ScheduledTask -TaskName $TaskName -Action $taskAction -Trigger $trigger -Settings $settings -Description '每个工作日北京时间08:00更新宽基ETF看板数据（休市日自动跳过）' | Out-Null
    Write-Output "已注册计划任务：$TaskName（周一至周五 08:00，系统时区需为北京时间）"
  }
  'run' {
    Start-ScheduledTask -TaskName $TaskName
    Start-Sleep -Seconds 3
    Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime | Format-List
  }
  'status' {
    Get-ScheduledTask -TaskName $TaskName | Select-Object TaskName, State
    Get-ScheduledTaskInfo -TaskName $TaskName | Select-Object TaskName, LastRunTime, LastTaskResult, NextRunTime | Format-List
  }
  'uninstall' {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false
    Write-Output "已删除计划任务：$TaskName"
  }
}
