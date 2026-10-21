param([string]$Runtime, [string]$HermesSource, [string]$HermesPython)
$ErrorActionPreference = 'Stop'
$candidateRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$taskRoot = Split-Path $candidateRoot -Parent
if (-not $Runtime) { $Runtime = Join-Path $taskRoot 'runtime/demo' }
if (-not $HermesSource) { $HermesSource = Join-Path $taskRoot 'deps/hermes-agent' }
if (-not $HermesPython) {
    $factsRoot = Join-Path $taskRoot 'runtime/hermes-home/installs'
    $factsFiles = @(Get-ChildItem -LiteralPath $factsRoot -Filter facts.json -Depth 1 -File)
    if ($factsFiles.Count -ne 1) { throw 'Specify -HermesPython using the verified isolated official environment.' }
    $facts = Get-Content -LiteralPath $factsFiles[0].FullName -Raw | ConvertFrom-Json
    $HermesPython = Join-Path $facts.packages.venv.environment 'Scripts/python.exe'
}
$gatewayPython = Join-Path $candidateRoot '.venv/Scripts/python.exe'
foreach ($required in @($gatewayPython, $HermesPython, $HermesSource)) {
    if (-not (Test-Path -LiteralPath $required)) { throw "Prepared dependency missing: $required" }
}
$actualRevision = & git -C $HermesSource rev-parse HEAD
if ($LASTEXITCODE -ne 0 -or $actualRevision -ne '427d605d4985c2d49a795493ef6c5d66413057d1') { throw 'Official Hermes revision mismatch.' }
Push-Location -LiteralPath $candidateRoot
try {
    Write-Host '测试管理页面：http://127.0.0.1:18760/manage'
    Write-Host "测试登录：$Runtime/demo-secrets.json 中的 admin 值（不写入前端或Git）。"
    Write-Host '停止：另一终端运行 scripts/profile-b/stop.ps1；也可在本前台窗口按 Ctrl+C。'
    & $gatewayPython -m cf_agent_gateway.profile_b.demo --runtime $Runtime --hermes-source $HermesSource --hermes-python $HermesPython
    if ($LASTEXITCODE -ne 0) { throw 'Demo exited abnormally; inspect runtime logs.' }
} finally { Pop-Location }
