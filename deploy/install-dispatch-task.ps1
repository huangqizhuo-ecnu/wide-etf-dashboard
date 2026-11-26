# 注册每交易日 07:45 / 08:30 / 09:00 的 GitHub 更新触发任务
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$tokenPath = Join-Path $env:LOCALAPPDATA 'etf-dashboard\dispatch-token.xml'

if (-not (Test-Path -LiteralPath $tokenPath)) {
    Write-Host '未找到加密令牌。请粘贴 GitHub 令牌（输入不回显）：'
    $secure = Read-Host -AsSecureString
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $tokenPath) | Out-Null
    $secure | Export-Clixml -LiteralPath $tokenPath
    Write-Host "令牌已用 Windows DPAPI 加密保存：$tokenPath"
}

$script = Join-Path $PSScriptRoot 'dispatch-update.ps1'
$action = New-ScheduledTaskAction -Execute 'powershell.exe' -Argument ('-NoProfile -ExecutionPolicy Bypass -File "{0}"' -f $script)
$times = @('07:45', '08:30', '09:00')
$triggers = foreach ($time in $times) {
    New-ScheduledTaskTrigger -Weekly -DaysOfWeek Monday, Tuesday, Wednesday, Thursday, Friday -At $time
}
$settings = New-ScheduledTaskSettingsSet `
    -StartWhenAvailable `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -WakeToRun `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 10)

Register-ScheduledTask `
    -TaskName 'ETF-Dashboard-Dispatch' `
    -Trigger $triggers `
    -Action $action `
    -Settings $settings `
    -Description '每个交易日 07:45/08:30/09:00 触发 GitHub 宽基 ETF 数据更新' `
    -Force | Out-Null

Write-Host '计划任务已注册：ETF-Dashboard-Dispatch'
Get-ScheduledTask -TaskName 'ETF-Dashboard-Dispatch' | Select-Object TaskName, State
Get-ScheduledTaskInfo -TaskName 'ETF-Dashboard-Dispatch' | Select-Object NextRunTime, LastRunTime, LastTaskResult

Write-Host '立即测试一次...'
Start-ScheduledTask -TaskName 'ETF-Dashboard-Dispatch'
Start-Sleep -Seconds 8
Get-ScheduledTaskInfo -TaskName 'ETF-Dashboard-Dispatch' | Select-Object LastRunTime, LastTaskResult
