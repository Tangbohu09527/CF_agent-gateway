#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$taskName = 'CF_Hermes_Gateway_Watchdog'
$taskPath = '\'
$watchdogScriptPath = Join-Path $PSScriptRoot 'HermesGatewayWatchdog.ps1'
if (-not (Test-Path -LiteralPath $watchdogScriptPath -PathType Leaf)) {
    throw "Required watchdog script was not found: $watchdogScriptPath"
}

. $watchdogScriptPath

function Get-HealthStatusValue {
    param(
        [Parameter()]
        [AllowNull()]
        [object]$Result
    )

    if ($Result -is [bool]) {
        return [bool]$Result
    }

    if ($null -eq $Result) {
        return $false
    }

    foreach ($propertyName in @('Healthy', 'IsHealthy', 'Ok', 'Success')) {
        $property = $Result.PSObject.Properties[$propertyName]
        if ($null -ne $property) {
            return [bool]$property.Value
        }
    }

    return $false
}

function Get-HealthReasonValue {
    param(
        [Parameter()]
        [AllowNull()]
        [object]$Result
    )

    if ($null -eq $Result -or $Result -is [bool]) {
        return $null
    }

    foreach ($propertyName in @('Reason', 'ReasonCode', 'Status')) {
        $property = $Result.PSObject.Properties[$propertyName]
        if ($null -ne $property -and $null -ne $property.Value) {
            return [string]$property.Value
        }
    }

    return $null
}

function Get-ListenerInformation {
    $listenerPid = $null

    try {
        $connection = Get-NetTCPConnection -LocalPort 8642 -State Listen -ErrorAction Stop |
            Select-Object -First 1
        if ($null -ne $connection) {
            $listenerPid = [int]$connection.OwningProcess
        }
    }
    catch {
        $netstatPath = Join-Path $env:SystemRoot 'System32\netstat.exe'
        if (Test-Path -LiteralPath $netstatPath -PathType Leaf) {
            $netstatLines = & $netstatPath -ano -p tcp 2>$null
            foreach ($line in $netstatLines) {
                if ($line -match '^\s*TCP\s+\S+:8642\s+\S+\s+LISTENING\s+(\d+)\s*$') {
                    $listenerPid = [int]$matches[1]
                    break
                }
            }
        }
    }

    return [pscustomobject]@{
        Listening = ($null -ne $listenerPid)
        ProcessId = $listenerPid
    }
}

function Get-VerifiedHermesGatewayProcessId {
    param(
        [AllowNull()][Nullable[int]]$ListenerProcessId
    )

    if ($null -eq $ListenerProcessId) {
        return $null
    }
    try {
        $process = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f [int]$ListenerProcessId) -ErrorAction Stop
        $name = [string]$process.Name
        $commandLine = [string]$process.CommandLine
        if (Test-HermesGatewayProcessIdentity -ProcessName $name -CommandLine $commandLine) {
            return [int]$ListenerProcessId
        }
    }
    catch {}

    return $null
}
function Get-LastRecoveryTime {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StatePath
    )

    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        return $null
    }

    try {
        $state = Get-Content -LiteralPath $StatePath -Raw -Encoding UTF8 | ConvertFrom-Json
        foreach ($propertyName in @('LastRecoveryTime', 'LastRecoveryUtc', 'LastRecoveryTimeUtc')) {
            $property = $state.PSObject.Properties[$propertyName]
            if ($null -ne $property -and $null -ne $property.Value -and
                -not [string]::IsNullOrWhiteSpace([string]$property.Value)) {
                return [string]$property.Value
            }
        }
    }
    catch {
        return $null
    }

    return $null
}

$paths = Get-HermesGatewayWatchdogPaths
$task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
$taskExists = $null -ne $task
$taskState = $null
if ($taskExists) {
    $taskState = [string]$task.State
}

$watchdogRunning = Test-HermesGatewayWatchdogInstanceRunning -ConfigPath $paths.ConfigPath
$paused = Test-Path -LiteralPath $paths.PauseFlagPath -PathType Leaf

$healthResult = $null
$healthCheckError = $null
$configurationStatus = 'Missing'
if (Test-Path -LiteralPath $paths.ConfigPath -PathType Leaf) {
    try {
        [void](Read-HermesGatewayWatchdogConfig -Path $paths.ConfigPath)
        $configurationStatus = 'Valid'
    }
    catch {
        $configurationStatus = 'Invalid'
    }

    try {
        $healthConfig = Read-HermesGatewayHealthConfig -Path $paths.ConfigPath
        $healthResult = Test-HermesGatewayHealth -Config $healthConfig
    }
    catch {
        $healthCheckError = 'HealthConfigurationInvalid'
    }
}
else {
    $healthCheckError = 'ConfigurationMissing'
}

$healthHealthy = Get-HealthStatusValue -Result $healthResult
$healthReason = Get-HealthReasonValue -Result $healthResult
if ($null -ne $healthCheckError) {
    $healthReason = $healthCheckError
}
elseif (-not $healthHealthy) {
    $healthReason = 'ProbeFailed'
}

$listener = Get-ListenerInformation
$verifiedGatewayPid = Get-VerifiedHermesGatewayProcessId -ListenerProcessId $listener.ProcessId
$lastRecoveryTime = Get-LastRecoveryTime -StatePath $paths.StatePath
$latestLogPath = $null
if (Test-Path -LiteralPath $paths.LogDirectory -PathType Container) {
    $latestLog = Get-ChildItem -LiteralPath $paths.LogDirectory -File -ErrorAction SilentlyContinue |
        Sort-Object -Property LastWriteTimeUtc -Descending |
        Select-Object -First 1
    if ($null -ne $latestLog) {
        $latestLogPath = $latestLog.FullName
    }
}

[pscustomobject]@{
    TaskExists            = $taskExists
    TaskState             = $taskState
    WatchdogProcessRunning = [bool]$watchdogRunning
    Paused                = [bool]$paused
    ConfigurationStatus   = $configurationStatus
    HermesHealthHealthy   = [bool]$healthHealthy
    HermesHealthReason    = $healthReason
    Port8642Listening     = [bool]$listener.Listening
    Port8642ListenerPid   = $listener.ProcessId
    GatewayPid            = $verifiedGatewayPid
    LastRecoveryTime      = $lastRecoveryTime
    LatestLogPath         = $latestLogPath
}
