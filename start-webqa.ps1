$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new()
$ProjectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$LocalRuntime = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$FallbackRuntime = "D:\KimiData\kimi\Workspaces\测试开发\pagepatrol-venv\Scripts\python.exe"
if (Test-Path -LiteralPath $LocalRuntime -PathType Leaf) {
    $Runtime = $LocalRuntime
} elseif (Test-Path -LiteralPath $FallbackRuntime -PathType Leaf) {
    $Runtime = $FallbackRuntime
} else {
    $Runtime = (Get-Command python -ErrorAction Stop).Source
}
$Entry = Join-Path $ProjectRoot "webqa_cli.py"

Write-Host ""
Write-Host "WebQA 状态图网站测试" -ForegroundColor Cyan
Write-Host "输出将归档到：$ProjectRoot\webqa-output\runs" -ForegroundColor DarkGray
Write-Host ""

do {
    $TargetUrl = (Read-Host "请输入完整网址（例如 https://staging.evomap.work/zh）").Trim()
    $ParsedUri = $null
    $UrlOk = [System.Uri]::TryCreate($TargetUrl, [System.UriKind]::Absolute, [ref]$ParsedUri) -and $ParsedUri.Scheme -in @("http", "https")
    if (-not $UrlOk) { Write-Host "网址无效，请重新输入。" -ForegroundColor Yellow }
} until ($UrlOk)

do {
    $DepthText = (Read-Host "请输入允许进入的子页面层数（0-20，主页为第 0 层）").Trim()
    $Depth = 0
    $DepthOk = [int]::TryParse($DepthText, [ref]$Depth) -and $Depth -ge 0 -and $Depth -le 20
    if (-not $DepthOk) { Write-Host "层数必须是 0 到 20 的整数。" -ForegroundColor Yellow }
} until ($DepthOk)

$ProfileInput = (Read-Host "选择测试模式：S 冒烟测试；F 全量测试（默认 F）").Trim().ToUpperInvariant()
$Profile = if ($ProfileInput -eq "S") { "smoke" } else { "full" }

$Mode = (Read-Host "输入 N 新建实例；输入 R 接续最近实例（可提高层数，默认 N）").Trim().ToUpperInvariant()
$Arguments = @($Entry, "run", "--target", $TargetUrl, "--max-depth", "$Depth", "--profile", $Profile)
if ($Mode -ne "R") { $Arguments += "--new-run" }

Write-Host ""
Write-Host "测试开始。R 会沿用原运行 ID 和结果；提高层数时只测试新解锁页面。" -ForegroundColor Green
Write-Host "测试模式：$Profile" -ForegroundColor Green
Write-Host "按 Ctrl+C 可安全暂停，之后选择 R 可继续。" -ForegroundColor Green
Write-Host ""
& $Runtime @Arguments
$ExitCode = $LASTEXITCODE
Write-Host ""
if ($ExitCode -eq 0) {
    Write-Host "测试结束。请查看上方输出的单次运行报告目录。" -ForegroundColor Green
} elseif ($ExitCode -eq 130) {
    Write-Host "测试已暂停，断点已经保存。" -ForegroundColor Yellow
} else {
    Write-Host "测试进程返回代码 $ExitCode，请查看 live-status.md 和报告中的阻塞项。" -ForegroundColor Red
}
Read-Host "按 Enter 关闭窗口"
exit $ExitCode
