# 移除本地触发任务（可选保留加密令牌）
$ErrorActionPreference = 'Stop'
Unregister-ScheduledTask -TaskName 'ETF-Dashboard-Dispatch' -Confirm:$false -ErrorAction SilentlyContinue
Write-Host '已移除计划任务 ETF-Dashboard-Dispatch'
if ($args -contains '-RemoveToken') {
    $tokenPath = Join-Path $env:LOCALAPPDATA 'etf-dashboard\dispatch-token.xml'
    Remove-Item -LiteralPath $tokenPath -Force -ErrorAction SilentlyContinue
    Write-Host '已删除加密令牌'
}
