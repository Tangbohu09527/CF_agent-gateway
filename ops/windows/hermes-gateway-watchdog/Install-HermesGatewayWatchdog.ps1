#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [ValidateNotNullOrEmpty()]
    [string]$HealthUrl,

    [Parameter()]
    [switch]$DryRun
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$taskName = 'CF_Hermes_Gateway_Watchdog'
$taskPath = '\'
$sourceRoot = $PSScriptRoot
$watchdogScriptPath = Join-Path $sourceRoot 'HermesGatewayWatchdog.ps1'

if (-not [string]::Equals($env:OS, 'Windows_NT', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Hermes Gateway Watchdog installation is supported only on Windows.'
}
if ($PSVersionTable.PSVersion -lt [version]'5.1') {
    throw 'Windows PowerShell 5.1 or later is required.'
}
if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
    throw 'LOCALAPPDATA is not available for the current user.'
}
if ([string]::IsNullOrWhiteSpace($env:SystemRoot)) {
    throw 'SystemRoot is not available.'
}
if (-not (Test-Path -LiteralPath $watchdogScriptPath -PathType Leaf)) {
    throw "Required watchdog script was not found: $watchdogScriptPath"
}

. $watchdogScriptPath

$currentIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$currentPrincipal = New-Object System.Security.Principal.WindowsPrincipal($currentIdentity)
$isElevated = $currentPrincipal.IsInRole([System.Security.Principal.WindowsBuiltInRole]::Administrator)

$currentUser = $currentIdentity.Name
$currentSid = $currentIdentity.User.Value
$rootPath = Join-Path $env:LOCALAPPDATA 'CF\HermesGatewayWatchdog'
$paths = Get-HermesGatewayWatchdogPaths -RootPath $rootPath
$installedWatchdogPath = Join-Path $paths.RootPath 'HermesGatewayWatchdog.ps1'
$windowsPowerShellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $windowsPowerShellPath -PathType Leaf)) {
    throw 'Windows PowerShell executable was not found.'
}

$actionArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -ConfigPath "{1}"' -f $installedWatchdogPath, $paths.ConfigPath

$sourceFiles = @(
    'HermesGatewayWatchdog.ps1',
    'Install-HermesGatewayWatchdog.ps1',
    'Uninstall-HermesGatewayWatchdog.ps1',
    'Pause-HermesGatewayWatchdog.ps1',
    'Resume-HermesGatewayWatchdog.ps1',
    'Get-HermesGatewayWatchdogStatus.ps1',
    'Test-HermesGatewayWatchdog.ps1',
    'watchdog.config.example.json',
    'README.md'
)

function Write-Utf8FileWithoutBom {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$Content
    )

    [System.IO.File]::WriteAllText(
        $Path,
        $Content,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Test-WatchdogHealthUrl {
    param(
        [Parameter(Mandatory = $true)][string]$Value
    )

    $uri = $null
    if (-not [System.Uri]::TryCreate($Value, [System.UriKind]::Absolute, [ref]$uri)) {
        throw 'HealthUrl must be an absolute HTTP or HTTPS URL.'
    }
    if (($uri.Scheme -cne 'http') -and ($uri.Scheme -cne 'https')) {
        throw 'HealthUrl must use HTTP or HTTPS.'
    }
    if (-not [string]::IsNullOrEmpty($uri.UserInfo) -or
        -not [string]::IsNullOrEmpty($uri.Query) -or
        -not [string]::IsNullOrEmpty($uri.Fragment)) {
        throw 'HealthUrl must not contain credentials, a query string, or a fragment.'
    }
    if ($Value.IndexOf('<') -ge 0 -or $Value.IndexOf('>') -ge 0) {
        throw 'HealthUrl still contains an example placeholder.'
    }
}

function Get-WatchdogAccountSid {
    param(
        [Parameter(Mandatory = $true)][string]$Identity
    )

    try {
        if ($Identity -match '^S-\d-\d+(?:-\d+)+$') {
            return (New-Object System.Security.Principal.SecurityIdentifier($Identity)).Value
        }
        $account = New-Object System.Security.Principal.NTAccount($Identity)
        return $account.Translate([System.Security.Principal.SecurityIdentifier]).Value
    }
    catch {
        throw "Could not resolve scheduled-task identity '$Identity'."
    }
}

function Assert-WatchdogScheduledTaskOwned {
    param(
        [Parameter(Mandatory = $true)]$Task,
        [Parameter(Mandatory = $true)][string]$ExpectedUserSid,
        [Parameter(Mandatory = $true)][string]$ExpectedPowerShellPath,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments
    )

    $principalSid = Get-WatchdogAccountSid -Identity ([string]$Task.Principal.UserId)
    if (-not [string]::Equals($principalSid, $ExpectedUserSid, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Scheduled task '$taskName' belongs to a different user. Refusing to modify it."
    }
    if ([string]$Task.Principal.RunLevel -ne 'Limited' -or
        @('Interactive', 'InteractiveToken') -notcontains [string]$Task.Principal.LogonType) {
        throw "Scheduled task '$taskName' is not a limited current-user task. Refusing to modify it."
    }

    $actions = @($Task.Actions)
    if ($actions.Count -ne 1) {
        throw "Scheduled task '$taskName' has an unexpected action count. Refusing to modify it."
    }

    try {
        $actualExecute = [System.IO.Path]::GetFullPath(
            [Environment]::ExpandEnvironmentVariables([string]$actions[0].Execute)
        )
        $expectedExecute = [System.IO.Path]::GetFullPath($ExpectedPowerShellPath)
    }
    catch {
        throw "Scheduled task '$taskName' has an invalid executable path."
    }

    if (-not [string]::Equals(
        $actualExecute,
        $expectedExecute,
        [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Scheduled task '$taskName' has an unexpected executable. Refusing to modify it."
    }
    if (-not [string]::Equals(
        [string]$actions[0].Arguments,
        $ExpectedArguments,
        [System.StringComparison]::Ordinal)) {
        throw "Scheduled task '$taskName' has unexpected arguments. Refusing to modify it."
    }

    $workingDirectoryProperty = $actions[0].PSObject.Properties['WorkingDirectory']
    if ($null -ne $workingDirectoryProperty -and
        -not [string]::IsNullOrWhiteSpace([string]$workingDirectoryProperty.Value)) {
        throw "Scheduled task '$taskName' has an unexpected working directory. Refusing to modify it."
    }
}

function Get-OwnedWatchdogScheduledTask {
    $task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    if ($null -ne $task) {
        Assert-WatchdogScheduledTaskOwned -Task $task -ExpectedUserSid $currentSid -ExpectedPowerShellPath $windowsPowerShellPath -ExpectedArguments $actionArguments
    }
    return $task
}

function Enter-WatchdogLifecycleMutex {
    param(
        [ValidateRange(0, 300000)][int]$TimeoutMilliseconds = 30000
    )

    $name = (Get-HermesGatewayWatchdogMutexName -ConfigPath $paths.ConfigPath) + '_Lifecycle'
    $mutex = New-Object System.Threading.Mutex($false, $name)
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne($TimeoutMilliseconds)
        }
        catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }
        [pscustomobject]@{
            Mutex    = $mutex
            Acquired = $acquired
        }
    }
    catch {
        $mutex.Dispose()
        throw
    }
}

function Exit-WatchdogLifecycleMutex {
    param(
        [Parameter(Mandatory = $true)]$Lock
    )

    if ($Lock.Acquired -and $null -ne $Lock.Mutex) {
        try {
            $Lock.Mutex.ReleaseMutex()
        }
        catch {}
    }
    if ($null -ne $Lock.Mutex) {
        $Lock.Mutex.Dispose()
    }
}

function Set-WatchdogStopFlagSafely {
    param(
        [Parameter(Mandatory = $true)]$WatchdogPaths
    )

    if (-not (Test-Path -LiteralPath $WatchdogPaths.RootPath -PathType Container)) {
        throw 'The Watchdog installation directory does not exist.'
    }

    $startLock = Enter-HermesGatewayWatchdogStartMutex -ConfigPath $WatchdogPaths.ConfigPath -TimeoutMilliseconds 30000
    if (-not $startLock.Acquired) {
        $startLock.Mutex.Dispose()
        throw 'Could not acquire the Gateway start gate to request Watchdog shutdown.'
    }

    $temporaryPath = $WatchdogPaths.StopFlagPath + ('.{0}.tmp' -f $PID)
    try {
        if ((Test-Path -LiteralPath $WatchdogPaths.StopFlagPath) -and
            -not (Test-Path -LiteralPath $WatchdogPaths.StopFlagPath -PathType Leaf)) {
            throw 'The Watchdog stop flag path is not a file.'
        }
        Write-Utf8FileWithoutBom -Path $temporaryPath -Content ([DateTime]::UtcNow.ToString('o'))
        Move-Item -LiteralPath $temporaryPath -Destination $WatchdogPaths.StopFlagPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
        Exit-HermesGatewayWatchdogStartMutex -Lock $startLock
    }
}

function Clear-WatchdogStopFlagSafely {
    param(
        [Parameter(Mandatory = $true)]$WatchdogPaths
    )

    $startLock = Enter-HermesGatewayWatchdogStartMutex -ConfigPath $WatchdogPaths.ConfigPath -TimeoutMilliseconds 30000
    if (-not $startLock.Acquired) {
        $startLock.Mutex.Dispose()
        throw 'Could not acquire the Gateway start gate to clear the Watchdog stop request.'
    }

    try {
        if ((Test-Path -LiteralPath $WatchdogPaths.StopFlagPath) -and
            -not (Test-Path -LiteralPath $WatchdogPaths.StopFlagPath -PathType Leaf)) {
            throw 'The Watchdog stop flag path is not a file.'
        }
        if (Test-Path -LiteralPath $WatchdogPaths.StopFlagPath -PathType Leaf) {
            Remove-Item -LiteralPath $WatchdogPaths.StopFlagPath -Force
        }
    }
    finally {
        Exit-HermesGatewayWatchdogStartMutex -Lock $startLock
    }
}

function Wait-WatchdogInstanceStopped {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ((Test-HermesGatewayWatchdogInstanceRunning -ConfigPath $ConfigPath) -and
        [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 250
    }
    return (-not (Test-HermesGatewayWatchdogInstanceRunning -ConfigPath $ConfigPath))
}

function Wait-WatchdogInstanceStarted {
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-HermesGatewayWatchdogInstanceRunning -ConfigPath $ConfigPath) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }
    return (Test-HermesGatewayWatchdogInstanceRunning -ConfigPath $ConfigPath)
}

function Wait-WatchdogTaskNotRunning {
    param(
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
        if ($null -eq $task -or [string]$task.State -ne 'Running') {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    $task = Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue
    return ($null -eq $task -or [string]$task.State -ne 'Running')
}

function Get-WatchdogStateProcessIdentity {
    param(
        [Parameter(Mandatory = $true)][string]$StatePath
    )

    if (-not (Test-Path -LiteralPath $StatePath -PathType Leaf)) {
        return $null
    }

    try {
        $state = [System.IO.File]::ReadAllText($StatePath) | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $state -or $state -is [array]) {
            return $null
        }

        $schemaProperty = $state.PSObject.Properties['SchemaVersion']
        $pidProperty = $state.PSObject.Properties['ProcessId']
        $startProperty = $state.PSObject.Properties['ProcessStartUtc']
        $runningProperty = $state.PSObject.Properties['Running']
        if ($null -eq $schemaProperty -or [int]$schemaProperty.Value -ne 1 -or
            $null -eq $pidProperty -or $null -eq $startProperty -or
            $null -eq $runningProperty -or -not [bool]$runningProperty.Value) {
            return $null
        }

        $processId = [int]$pidProperty.Value
        if ($processId -le 0 -or $processId -eq $PID) {
            return $null
        }

        $processStart = [DateTimeOffset]::MinValue
        if (-not [DateTimeOffset]::TryParse(
            [string]$startProperty.Value,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::RoundtripKind,
            [ref]$processStart)) {
            return $null
        }

        [pscustomobject]@{
            ProcessId       = $processId
            ProcessStartUtc = $processStart.UtcDateTime
        }
    }
    catch {
        return $null
    }
}

function Convert-WatchdogProcessCreationUtc {
    param(
        [Parameter(Mandatory = $true)]$Value
    )

    if ($Value -is [DateTime]) {
        return ([DateTime]$Value).ToUniversalTime()
    }

    try {
        return [System.Management.ManagementDateTimeConverter]::ToDateTime([string]$Value).ToUniversalTime()
    }
    catch {
        $parsed = [DateTimeOffset]::MinValue
        if ([DateTimeOffset]::TryParse(
            [string]$Value,
            [Globalization.CultureInfo]::InvariantCulture,
            [Globalization.DateTimeStyles]::AssumeLocal,
            [ref]$parsed)) {
            return $parsed.UtcDateTime
        }
    }

    return $null
}

function Test-WatchdogCommandLineExact {
    param(
        [Parameter(Mandatory = $true)][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$ExpectedExecutablePath,
        [Parameter(Mandatory = $true)][string]$ExpectedArguments
    )

    foreach ($prefix in @(
        ('"{0}"' -f $ExpectedExecutablePath),
        $ExpectedExecutablePath
    )) {
        if ($CommandLine.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            if ($CommandLine.Length -le $prefix.Length -or
                -not [char]::IsWhiteSpace($CommandLine[$prefix.Length])) {
                continue
            }

            $actualArguments = $CommandLine.Substring($prefix.Length).TrimStart()
            return [string]::Equals(
                $actualArguments,
                $ExpectedArguments,
                [System.StringComparison]::Ordinal
            )
        }
    }

    return $false
}

function Test-WatchdogProcessRecordIdentity {
    param(
        [Parameter(Mandatory = $true)]$ProcessRecord,
        [Parameter(Mandatory = $true)]$StateIdentity
    )

    try {
        if ([int]$ProcessRecord.ProcessId -ne $StateIdentity.ProcessId -or
            -not [string]::Equals([string]$ProcessRecord.Name, 'powershell.exe', [System.StringComparison]::OrdinalIgnoreCase) -or
            [string]::IsNullOrWhiteSpace([string]$ProcessRecord.ExecutablePath) -or
            [string]::IsNullOrWhiteSpace([string]$ProcessRecord.CommandLine)) {
            return $false
        }

        $actualExecutable = [System.IO.Path]::GetFullPath([string]$ProcessRecord.ExecutablePath)
        $expectedExecutable = [System.IO.Path]::GetFullPath($windowsPowerShellPath)
        if (-not [string]::Equals(
            $actualExecutable,
            $expectedExecutable,
            [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }
        if (-not (Test-WatchdogCommandLineExact -CommandLine ([string]$ProcessRecord.CommandLine) -ExpectedExecutablePath $expectedExecutable -ExpectedArguments $actionArguments)) {
            return $false
        }

        $creationUtc = Convert-WatchdogProcessCreationUtc -Value $ProcessRecord.CreationDate
        if ($null -eq $creationUtc -or
            [Math]::Abs(($creationUtc - $StateIdentity.ProcessStartUtc).TotalSeconds) -gt 2.0) {
            return $false
        }

        $ownerResult = Invoke-CimMethod -InputObject $ProcessRecord -MethodName GetOwnerSid -ErrorAction Stop
        $returnValueProperty = $ownerResult.PSObject.Properties['ReturnValue']
        $sidProperty = $ownerResult.PSObject.Properties['Sid']
        if ($null -eq $returnValueProperty -or [uint32]$returnValueProperty.Value -ne 0 -or
            $null -eq $sidProperty -or
            -not [string]::Equals([string]$sidProperty.Value, $currentSid, [System.StringComparison]::OrdinalIgnoreCase)) {
            return $false
        }

        return $true
    }
    catch {
        return $false
    }
}

function Stop-WatchdogProcessSafely {
    param(
        [Parameter(Mandatory = $true)]$WatchdogPaths
    )

    if (-not (Test-HermesGatewayWatchdogInstanceRunning -ConfigPath $WatchdogPaths.ConfigPath)) {
        return $true
    }

    $stateIdentity = Get-WatchdogStateProcessIdentity -StatePath $WatchdogPaths.StatePath
    if ($null -eq $stateIdentity) {
        return $false
    }

    $processRecord = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f $stateIdentity.ProcessId) -ErrorAction SilentlyContinue
    if ($null -eq $processRecord -or
        -not (Test-WatchdogProcessRecordIdentity -ProcessRecord $processRecord -StateIdentity $stateIdentity)) {
        return $false
    }

    if (-not (Test-HermesGatewayWatchdogInstanceRunning -ConfigPath $WatchdogPaths.ConfigPath)) {
        return $true
    }

    $confirmedRecord = Get-CimInstance -ClassName Win32_Process -Filter ("ProcessId = {0}" -f $stateIdentity.ProcessId) -ErrorAction SilentlyContinue
    if ($null -eq $confirmedRecord -or
        -not (Test-WatchdogProcessRecordIdentity -ProcessRecord $confirmedRecord -StateIdentity $stateIdentity)) {
        return $false
    }

    try {
        Stop-Process -Id $stateIdentity.ProcessId -Force -ErrorAction Stop
    }
    catch {
        if (-not (Test-HermesGatewayWatchdogInstanceRunning -ConfigPath $WatchdogPaths.ConfigPath)) {
            return $true
        }
        throw
    }
    return $true
}

function Stop-WatchdogForLifecycle {
    param(
        [Parameter(Mandatory = $true)]$WatchdogPaths
    )

    $ownedTask = Get-OwnedWatchdogScheduledTask
    if ($null -ne $ownedTask) {
        Disable-ScheduledTask -TaskName $taskName -TaskPath $taskPath | Out-Null
    }

    Set-WatchdogStopFlagSafely -WatchdogPaths $WatchdogPaths

    if (-not (Wait-WatchdogInstanceStopped -ConfigPath $WatchdogPaths.ConfigPath -TimeoutSeconds 15)) {
        if (-not (Stop-WatchdogProcessSafely -WatchdogPaths $WatchdogPaths)) {
            throw 'The running Watchdog process could not be strictly verified and was not stopped.'
        }
        if (-not (Wait-WatchdogInstanceStopped -ConfigPath $WatchdogPaths.ConfigPath -TimeoutSeconds 10)) {
            throw 'The Watchdog did not release its single-instance mutex.'
        }
    }

    if (-not (Wait-WatchdogTaskNotRunning -TimeoutSeconds 15)) {
        throw 'The Watchdog scheduled task is still running and was left disabled.'
    }
}

function Copy-WatchdogFileAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Source,
        [Parameter(Mandatory = $true)][string]$Destination
    )

    $sourceFullPath = [System.IO.Path]::GetFullPath($Source)
    $destinationFullPath = [System.IO.Path]::GetFullPath($Destination)
    if ([string]::Equals($sourceFullPath, $destinationFullPath, [System.StringComparison]::OrdinalIgnoreCase)) {
        return
    }
    if ((Test-Path -LiteralPath $destinationFullPath) -and
        -not (Test-Path -LiteralPath $destinationFullPath -PathType Leaf)) {
        throw "Installation destination is not a file: $destinationFullPath"
    }

    $temporaryPath = $destinationFullPath + ('.{0}.install.tmp' -f $PID)
    try {
        Copy-Item -LiteralPath $sourceFullPath -Destination $temporaryPath -Force
        Move-Item -LiteralPath $temporaryPath -Destination $destinationFullPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}

function Write-WatchdogConfigAtomically {
    param(
        [Parameter(Mandatory = $true)][string]$Content
    )

    if ((Test-Path -LiteralPath $paths.ConfigPath) -and
        -not (Test-Path -LiteralPath $paths.ConfigPath -PathType Leaf)) {
        throw 'The Watchdog configuration path is not a file.'
    }

    $temporaryPath = $paths.ConfigPath + ('.{0}.install.tmp' -f $PID)
    try {
        Write-Utf8FileWithoutBom -Path $temporaryPath -Content $Content
        [void](Read-HermesGatewayWatchdogConfig -Path $temporaryPath)
        Move-Item -LiteralPath $temporaryPath -Destination $paths.ConfigPath -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }
}

foreach ($fileName in $sourceFiles) {
    $sourcePath = Join-Path $sourceRoot $fileName
    if (-not (Test-Path -LiteralPath $sourcePath -PathType Leaf)) {
        throw "Required installation file was not found: $sourcePath"
    }
}

$hermesCommand = Get-Command -Name 'hermes.exe' -CommandType Application -ErrorAction Stop | Select-Object -First 1
$hermesCommandPath = $hermesCommand.Path
if ([string]::IsNullOrWhiteSpace($hermesCommandPath)) {
    $hermesCommandPath = $hermesCommand.Source
}

$resolvedHermesPath = (Resolve-Path -LiteralPath $hermesCommandPath -ErrorAction Stop).ProviderPath
if (-not [System.IO.Path]::IsPathRooted($resolvedHermesPath) -or
    -not [string]::Equals([System.IO.Path]::GetFileName($resolvedHermesPath), 'hermes.exe', [System.StringComparison]::OrdinalIgnoreCase) -or
    -not (Test-Path -LiteralPath $resolvedHermesPath -PathType Leaf)) {
    throw 'The hermes command did not resolve to an absolute hermes.exe path.'
}

if (-not [string]::IsNullOrWhiteSpace($HealthUrl)) {
    Test-WatchdogHealthUrl -Value $HealthUrl
}

$existingConfig = $null
if (Test-Path -LiteralPath $paths.ConfigPath -PathType Leaf) {
    try {
        $existingRawConfig = [System.IO.File]::ReadAllText($paths.ConfigPath) | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'The existing Watchdog configuration is not valid JSON.'
    }
    if ($null -eq $existingRawConfig -or $existingRawConfig -is [array]) {
        throw 'The existing Watchdog configuration must be one JSON object.'
    }

    $hermesPathProperty = $existingRawConfig.PSObject.Properties['HermesExecutablePath']
    if ($null -eq $hermesPathProperty) {
        $existingRawConfig | Add-Member -NotePropertyName 'HermesExecutablePath' -NotePropertyValue $resolvedHermesPath
    }
    else {
        $hermesPathProperty.Value = $resolvedHermesPath
    }

    if (-not [string]::IsNullOrWhiteSpace($HealthUrl)) {
        $healthUrlProperty = $existingRawConfig.PSObject.Properties['HealthUrl']
        if ($null -eq $healthUrlProperty) {
            $existingRawConfig | Add-Member -NotePropertyName 'HealthUrl' -NotePropertyValue $HealthUrl
        }
        else {
            $healthUrlProperty.Value = $HealthUrl
        }
    }

    $existingConfig = ConvertTo-HermesGatewayWatchdogConfig -InputObject $existingRawConfig
}

$effectiveHealthUrl = $HealthUrl
if ([string]::IsNullOrWhiteSpace($effectiveHealthUrl) -and $null -ne $existingConfig) {
    $effectiveHealthUrl = [string]$existingConfig.HealthUrl
}
if ([string]::IsNullOrWhiteSpace($effectiveHealthUrl)) {
    throw 'HealthUrl is required for the first installation. Pass -HealthUrl with the AI host LAN health endpoint.'
}
Test-WatchdogHealthUrl -Value $effectiveHealthUrl

if ($null -eq $existingConfig) {
    $configValues = [ordered]@{
        SchemaVersion                    = 1
        HealthUrl                       = $effectiveHealthUrl
        HermesExecutablePath            = $resolvedHermesPath
        StartupGraceSeconds             = 45
        PollIntervalSeconds              = 5
        FailureSettleDelaySeconds        = 15
        HealthConnectTimeoutSeconds      = 3
        HealthTotalTimeoutSeconds        = 5
        GatewayStartTimeoutSeconds       = 30
        MaximumRestartBackoffSeconds     = 60
        RecoveryVerificationTimeoutSeconds = 45
        RequiredConsecutiveHealthyChecks = 3
        LogMaxBytes                      = 5242880
        LogRetentionCount                = 5
    }
}
else {
    $configValues = [ordered]@{
        SchemaVersion                    = $existingConfig.SchemaVersion
        HealthUrl                       = $effectiveHealthUrl
        HermesExecutablePath            = $resolvedHermesPath
        StartupGraceSeconds             = $existingConfig.StartupGraceSeconds
        PollIntervalSeconds              = $existingConfig.PollIntervalSeconds
        FailureSettleDelaySeconds        = $existingConfig.FailureSettleDelaySeconds
        HealthConnectTimeoutSeconds      = $existingConfig.HealthConnectTimeoutSeconds
        HealthTotalTimeoutSeconds        = $existingConfig.HealthTotalTimeoutSeconds
        GatewayStartTimeoutSeconds       = $existingConfig.GatewayStartTimeoutSeconds
        MaximumRestartBackoffSeconds     = $existingConfig.MaximumRestartBackoffSeconds
        RecoveryVerificationTimeoutSeconds = $existingConfig.RecoveryVerificationTimeoutSeconds
        RequiredConsecutiveHealthyChecks = $existingConfig.RequiredConsecutiveHealthyChecks
        LogMaxBytes                      = $existingConfig.LogMaxBytes
        LogRetentionCount                = $existingConfig.LogRetentionCount
    }
}
$configJson = ([pscustomobject]$configValues | ConvertTo-Json -Depth 4) + [Environment]::NewLine

[void](Get-OwnedWatchdogScheduledTask)

if ($DryRun) {
    Write-Host "[DryRun] Install files and configuration under $($paths.RootPath)"
    Write-Host "[DryRun] Register or replace current-user task $taskName in a disabled state"
    Write-Host '[DryRun] Enable and start the task after the Watchdog stop flag is cleared'
    Write-Host 'Installation preview completed. No files, processes, or scheduled tasks were changed.'
    return
}

$installTarget = "$($paths.RootPath) and scheduled task $taskName"
if (-not $PSCmdlet.ShouldProcess($installTarget, 'Install Hermes Gateway Watchdog for the current user')) {
    Write-Host 'Installation preview completed. No files, processes, or scheduled tasks were changed.'
    return
}

if ($isElevated) {
    throw 'Run the installer from a non-elevated PowerShell session.'
}

$lifecycleLock = Enter-WatchdogLifecycleMutex -TimeoutMilliseconds 30000
if (-not $lifecycleLock.Acquired) {
    $lifecycleLock.Mutex.Dispose()
    throw 'Another Watchdog install or uninstall operation is already running.'
}

try {
    [void](New-Item -ItemType Directory -Path $paths.RootPath -Force)
    Stop-WatchdogForLifecycle -WatchdogPaths $paths

    foreach ($fileName in $sourceFiles) {
        Copy-WatchdogFileAtomically -Source (Join-Path $sourceRoot $fileName) -Destination (Join-Path $paths.RootPath $fileName)
    }
    Write-WatchdogConfigAtomically -Content $configJson

    $taskAction = New-ScheduledTaskAction -Execute $windowsPowerShellPath -Argument $actionArguments
    $taskTrigger = New-ScheduledTaskTrigger -AtLogOn -User $currentUser
    $taskPrincipal = New-ScheduledTaskPrincipal -UserId $currentSid -LogonType Interactive -RunLevel Limited
    $taskSettingsParameters = @{
        AllowStartIfOnBatteries = $true
        DontStopIfGoingOnBatteries = $true
        ExecutionTimeLimit = [TimeSpan]::Zero
        Hidden = $true
        MultipleInstances = 'IgnoreNew'
        RestartCount = 3
        RestartInterval = (New-TimeSpan -Minutes 1)
        Disable = $true
    }
    $taskSettings = New-ScheduledTaskSettingsSet @taskSettingsParameters

    [void](Get-OwnedWatchdogScheduledTask)
    $registerParameters = @{
        TaskName = $taskName
        TaskPath = $taskPath
        Action = $taskAction
        Trigger = $taskTrigger
        Principal = $taskPrincipal
        Settings = $taskSettings
        Description = 'CF current-user Hermes Gateway health watchdog.'
        Force = $true
    }
    Register-ScheduledTask @registerParameters | Out-Null

    $registeredTask = Get-OwnedWatchdogScheduledTask
    if ($null -eq $registeredTask -or [bool]$registeredTask.Settings.Enabled) {
        throw 'The Watchdog task was not registered in a disabled state.'
    }

    Clear-WatchdogStopFlagSafely -WatchdogPaths $paths
    Enable-ScheduledTask -TaskName $taskName -TaskPath $taskPath | Out-Null
    [void](Get-OwnedWatchdogScheduledTask)
    Start-ScheduledTask -TaskName $taskName -TaskPath $taskPath

    if (-not (Wait-WatchdogInstanceStarted -ConfigPath $paths.ConfigPath -TimeoutSeconds 30)) {
        throw 'The Watchdog task did not acquire its single-instance mutex after startup.'
    }
}
catch {
    $installationError = $_
    try {
        Stop-WatchdogForLifecycle -WatchdogPaths $paths
    }
    catch {
        Write-Warning 'Installation failed and the Watchdog could not be fully quiesced; the task remains fail-closed where possible.'
    }
    throw $installationError
}
finally {
    Exit-WatchdogLifecycleMutex -Lock $lifecycleLock
}

Write-Host "Hermes Gateway Watchdog installed for the current user. Task: $taskName"
