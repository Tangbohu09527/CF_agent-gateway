param(
    [string]$Runtime,
    [ValidateSet('demo', 'read_only')][string]$Mode = 'demo',
    [int]$Port = 18770,
    [int]$PeerPort = 18771,
    [switch]$BrowserTest
)
$ErrorActionPreference = 'Stop'
$candidateRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (-not $Runtime) { $Runtime = Join-Path (Split-Path $candidateRoot -Parent) 'runtime/console' }
$Runtime = [IO.Path]::GetFullPath($Runtime)
$python = Join-Path $candidateRoot '.venv/Scripts/python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Prepared candidate .venv is required; see docs/wechat-management-console.md.' }
[IO.Directory]::CreateDirectory($Runtime) | Out-Null
$recordPath = Join-Path $Runtime 'owned-processes.json'
if (Test-Path -LiteralPath $recordPath) {
    $previous = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
    $existing = Get-Process -Id $previous.supervisor -ErrorAction SilentlyContinue
    if ($existing) {
        $stoppedPath = Join-Path $Runtime 'stopped.json'
        if (Test-Path -LiteralPath $stoppedPath) {
            $stopped = Get-Content -LiteralPath $stoppedPath -Raw | ConvertFrom-Json
            if ($stopped.supervisor -eq $previous.supervisor -and $stopped.children_stopped) {
                Wait-Process -Id $previous.supervisor -Timeout 8 -ErrorAction SilentlyContinue
                $existing = Get-Process -Id $previous.supervisor -ErrorAction SilentlyContinue
            }
        }
        if ($existing) { throw 'This runtime has an existing process. Use stop.ps1 and inspect its process record first.' }
    }
}
if (-not (Test-Path -LiteralPath (Join-Path $Runtime 'admin.json')) -and -not $BrowserTest) {
    & $python -m cf_agent_gateway.management_console.runtime init-admin --runtime $Runtime
    if ($LASTEXITCODE -ne 0) { throw 'Administrator initialization did not complete.' }
}
if ($Mode -eq 'read_only' -and -not (Test-Path -LiteralPath (Join-Path $Runtime 'sources.json'))) {
    throw 'read_only mode requires explicit operator sources.json and environment secret references.'
}
$arguments = @('-m', 'cf_agent_gateway.management_console.runtime', 'serve', '--runtime', ('"' + $Runtime + '"'), '--mode', $Mode, '--host', '127.0.0.1', '--port', $Port, '--peer-port', $PeerPort)
if ($BrowserTest) { $arguments += '--browser-test' }
$candidateProcess = Start-Process -FilePath $python -ArgumentList $arguments -WorkingDirectory $candidateRoot -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $Runtime 'stdout.log') -RedirectStandardError (Join-Path $Runtime 'stderr.log')
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    if ($candidateProcess.HasExited) { throw ('Candidate exited; inspect ' + (Join-Path $Runtime 'stderr.log')) }
    try {
        $response = Invoke-WebRequest -UseBasicParsing -Uri ('http://127.0.0.1:' + $Port + '/console') -TimeoutSec 2
        if ($response.StatusCode -eq 200) {
            Write-Host ('Candidate management: http://127.0.0.1:' + $Port + '/console')
            Write-Host ('Mode: ' + $Mode + '; candidate configuration only; production is unchanged.')
            Write-Host ('Stop: scripts/wechat-console/stop.ps1 -Runtime "' + $Runtime + '"')
            exit 0
        }
    } catch { Start-Sleep -Milliseconds 300 }
}
throw 'Candidate did not become ready. Inspect only this runtime logs and process record.'
