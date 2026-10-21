param([string]$Runtime)
$ErrorActionPreference = 'Stop'
if (-not $Runtime) { $Runtime = Join-Path ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../../..'))) 'runtime/demo' }
$recordPath = Join-Path $Runtime 'owned-processes.json'
if (-not (Test-Path -LiteralPath $recordPath)) { throw 'No process record for this isolated runtime.' }
$record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
# Signal only this runtime's foreground supervisor; never enumerate or kill other processes.
[IO.File]::WriteAllText((Join-Path $Runtime 'stop-requested'), 'stop')
for ($attempt = 0; $attempt -lt 50; $attempt++) {
    $stoppedPath = Join-Path $Runtime 'stopped.json'
    if (Test-Path -LiteralPath $stoppedPath) {
        $stopped = Get-Content -LiteralPath $stoppedPath -Raw | ConvertFrom-Json
        if ($stopped.supervisor -eq $record.supervisor -and $stopped.children_stopped) { Write-Host '本轮测试服务已停止；源码、测试数据和隔离依赖已保留。'; exit 0 }
    }
    Start-Sleep -Milliseconds 400
}
throw 'Supervisor did not acknowledge; inspect owned-processes.json and logs. No unrelated processes were touched.'
