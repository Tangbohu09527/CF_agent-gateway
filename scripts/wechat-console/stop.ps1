param([string]$Runtime)
$ErrorActionPreference = 'Stop'
$candidateRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
if (-not $Runtime) { $Runtime = Join-Path (Split-Path $candidateRoot -Parent) 'runtime/console' }
$recordPath = Join-Path $Runtime 'owned-processes.json'
if (-not (Test-Path -LiteralPath $recordPath)) { throw 'No process record for this candidate runtime.' }
$record = Get-Content -LiteralPath $recordPath -Raw | ConvertFrom-Json
[IO.File]::WriteAllText((Join-Path $Runtime 'stop-requested'), 'stop')
for ($attempt = 0; $attempt -lt 60; $attempt++) {
    $stoppedPath = Join-Path $Runtime 'stopped.json'
    if (Test-Path -LiteralPath $stoppedPath) {
        $stopped = Get-Content -LiteralPath $stoppedPath -Raw | ConvertFrom-Json
        if ($stopped.supervisor -eq $record.supervisor -and $stopped.children_stopped) {
            Write-Host 'Candidate and synthetic peer stopped; data and dependencies retained.'
            exit 0
        }
    }
    Start-Sleep -Milliseconds 300
}
throw 'No matching shutdown acknowledgement. No unrelated process was killed.'
