@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

rem 本地看板 + Cloudflare 快速隧道，用于无代理的临时公网预览。
rem 需要 tools\cloudflared.exe；未下载时先执行 README 中的下载命令。
if not exist "tools\cloudflared.exe" (
  echo 缺少 tools\cloudflared.exe，请先按 README 下载 cloudflared。
  pause
  exit /b 1
)

start "ETF 本地服务" /min python scripts\serve.py --port 8080
timeout /t 2 >nul

echo 正在建立隧道，稍后会出现 https://<随机名>.trycloudflare.com 地址。
echo 关闭本窗口或本机休眠后地址失效。
tools\cloudflared.exe tunnel --url http://127.0.0.1:8080 --no-autoupdate --protocol http2
