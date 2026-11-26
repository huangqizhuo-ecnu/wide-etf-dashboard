# 触发 GitHub Actions 更新（供 Windows 计划任务调用）
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$logDir = Join-Path $projectRoot 'logs'
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$log = Join-Path $logDir 'dispatch.log'

function Write-Log([string]$Message) {
    "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') $Message" | Add-Content -LiteralPath $log -Encoding UTF8
}

try {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $tokenPath = Join-Path $env:LOCALAPPDATA 'etf-dashboard\dispatch-token.xml'
    if (-not (Test-Path -LiteralPath $tokenPath)) {
        throw "令牌文件不存在：$tokenPath"
    }
    $secure = Import-Clixml -LiteralPath $tokenPath
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
    try {
        $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    }
    finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    }
    $headers = @{
        Authorization = "Bearer $token"
        Accept        = 'application/vnd.github+json'
        'User-Agent'  = 'etf-dashboard-dispatch'
    }
    $uri = 'https://api.github.com/repos/huangqizhuo-ecnu/wide-etf-dashboard/actions/workflows/update-data.yml/dispatches'
    Invoke-RestMethod -Method Post -Uri $uri -Headers $headers -Body '{"ref":"main"}' -ContentType 'application/json' -UseBasicParsing | Out-Null
    Write-Log 'OK 已触发 update-data 工作流'
    exit 0
}
catch {
    Write-Log ("ERROR " + $_.Exception.Message)
    exit 1
}
