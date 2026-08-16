#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$watchdogScriptPath = Join-Path $PSScriptRoot 'HermesGatewayWatchdog.ps1'
if (-not (Test-Path -LiteralPath $watchdogScriptPath -PathType Leaf)) {
    throw "Required watchdog script was not found: $watchdogScriptPath"
}

. $watchdogScriptPath

$paths = Get-HermesGatewayWatchdogPaths
if (-not (Test-Path -LiteralPath $paths.RootPath -PathType Container)) {
    throw 'Hermes Gateway Watchdog is not installed for the current user.'
}

$logMaxBytes = 5242880
$logRetentionCount = 5
if (Test-Path -LiteralPath $paths.ConfigPath -PathType Leaf) {
    try {
        $config = Read-HermesGatewayWatchdogConfig -Path $paths.ConfigPath
        $logMaxBytes = $config.LogMaxBytes
        $logRetentionCount = $config.LogRetentionCount
    }
    catch {
        # Pause remains available when configuration or the saved executable path is broken.
    }
}
$pauseChanged = $false
if ((Test-Path -LiteralPath $paths.PauseFlagPath) -and
    -not (Test-Path -LiteralPath $paths.PauseFlagPath -PathType Leaf)) {
    throw 'The Watchdog pause control path is not a file. Pause was not changed.'
}

if (-not (Test-Path -LiteralPath $paths.PauseFlagPath -PathType Leaf)) {
    $startLock = Enter-HermesGatewayWatchdogStartMutex -ConfigPath $paths.ConfigPath -TimeoutMilliseconds 30000
    if (-not $startLock.Acquired) {
        $startLock.Mutex.Dispose()
        throw 'An in-flight Gateway start could not be synchronized. Pause was not changed.'
    }

    try {
        $pauseChanged = Set-HermesGatewayWatchdogControlFlag -Path $paths.PauseFlagPath
    }
    finally {
        Exit-HermesGatewayWatchdogStartMutex -Lock $startLock
    }
}
if ($pauseChanged) {
    Write-HermesGatewayWatchdogLog `
        -LogPath $paths.LogPath `
        -EventName 'pause_requested' `
        -Level 'Info' `
        -Message 'Watchdog pause requested.' `
        -MaxBytes $logMaxBytes `
        -RetentionCount $logRetentionCount
    Write-Host 'Hermes Gateway Watchdog is paused. Gateway recovery starts are disabled.'
}
else {
    Write-Host 'Hermes Gateway Watchdog is already paused.'
}
