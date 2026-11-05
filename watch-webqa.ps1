$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$Runtime = "D:\KimiData\kimi\Workspaces\测试开发\pagepatrol-venv\Scripts\python.exe"
$Entry = Join-Path $ProjectRoot "webqa_cli.py"
$RunId = (Read-Host "请输入运行 ID；直接回车查看最近实例").Trim()
$Arguments = @($Entry, "status", "--watch")
if ($RunId) { $Arguments += @("--run-id", $RunId) }
Write-Host "开始跟踪。按 Ctrl+C 停止查看，不会停止测试进程。" -ForegroundColor Cyan
& $Runtime @Arguments
