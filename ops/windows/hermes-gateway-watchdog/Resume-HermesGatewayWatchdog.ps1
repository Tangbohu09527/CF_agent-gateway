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

$config = Read-HermesGatewayWatchdogConfig -Path $paths.ConfigPath
$resumeChanged = $false

if (Test-Path -LiteralPath $paths.PauseFlagPath) {
    Remove-Item -LiteralPath $paths.PauseFlagPath -Force
    $resumeChanged = $true
}

if ($resumeChanged) {
    Write-HermesGatewayWatchdogLog `
        -LogPath $paths.LogPath `
        -EventName 'resume_requested' `
        -Level 'Info' `
        -Message 'Watchdog resume requested.' `
        -MaxBytes $config.LogMaxBytes `
        -RetentionCount $config.LogRetentionCount
    Write-Host 'Hermes Gateway Watchdog is resumed. Recovery starts are enabled.'
}
else {
    Write-Host 'Hermes Gateway Watchdog is already active.'
}
