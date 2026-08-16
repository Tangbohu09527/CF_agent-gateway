#requires -Version 5.1

[CmdletBinding(SupportsShouldProcess = $true, ConfirmImpact = 'Medium')]
param(
    [Parameter()]
    [switch]$RemoveLogs,

    [Parameter()]
    [switch]$DryRun
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$taskName = 'CF_Hermes_Gateway_Watchdog'
$taskPath = '\'
$watchdogScriptPath = Join-Path $PSScriptRoot 'HermesGatewayWatchdog.ps1'

if (-not [string]::Equals($env:OS, 'Windows_NT', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw 'Hermes Gateway Watchdog uninstallation is supported only on Windows.'
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

$currentSid = $currentIdentity.User.Value
$rootPath = Join-Path $env:LOCALAPPDATA 'CF\HermesGatewayWatchdog'
$paths = Get-HermesGatewayWatchdogPaths -RootPath $rootPath
$installedWatchdogPath = Join-Path $paths.RootPath 'HermesGatewayWatchdog.ps1'
$windowsPowerShellPath = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $windowsPowerShellPath -PathType Leaf)) {
    throw 'Windows PowerShell executable was not found.'
}
$actionArguments = '-NoLogo -NoProfile -NonInteractive -ExecutionPolicy Bypass -WindowStyle Hidden -File "{0}" -ConfigPath "{1}"' -f $installedWatchdogPath, $paths.ConfigPath

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
        [Parameter(Mandatory = $true)]$Task
    )

    $principalSid = Get-WatchdogAccountSid -Identity ([string]$Task.Principal.UserId)
    if (-not [string]::Equals($principalSid, $currentSid, [System.StringComparison]::OrdinalIgnoreCase)) {
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
        $expectedExecute = [System.IO.Path]::GetFullPath($windowsPowerShellPath)
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
        $actionArguments,
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
        Assert-WatchdogScheduledTaskOwned -Task $task
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

function Wait-WatchdogTaskRemoved {
    param(
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds
    )

    $deadline = [DateTime]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        if ($null -eq (Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    } while ([DateTime]::UtcNow -lt $deadline)

    return ($null -eq (Get-ScheduledTask -TaskName $taskName -TaskPath $taskPath -ErrorAction SilentlyContinue))
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
        [Parameter(Mandatory = $true)][string]$CommandLine
    )

    foreach ($prefix in @(
        ('"{0}"' -f $windowsPowerShellPath),
        $windowsPowerShellPath
    )) {
        if ($CommandLine.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            if ($CommandLine.Length -le $prefix.Length -or
                -not [char]::IsWhiteSpace($CommandLine[$prefix.Length])) {
                continue
            }

            $actualArguments = $CommandLine.Substring($prefix.Length).TrimStart()
            return [string]::Equals(
                $actualArguments,
                $actionArguments,
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
        if (-not (Test-WatchdogCommandLineExact -CommandLine ([string]$ProcessRecord.CommandLine))) {
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

    $rootExists = Test-Path -LiteralPath $WatchdogPaths.RootPath -PathType Container
    if ($rootExists) {
        Set-WatchdogStopFlagSafely -WatchdogPaths $WatchdogPaths
    }

    if (-not (Wait-WatchdogInstanceStopped -ConfigPath $WatchdogPaths.ConfigPath -TimeoutSeconds 15)) {
        if (-not $rootExists -or
            -not (Stop-WatchdogProcessSafely -WatchdogPaths $WatchdogPaths)) {
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

function Remove-WatchdogFileWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [ValidateRange(1, 10)][int]$Attempts = 4
    )

    $lastError = $null
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        if (-not (Test-Path -LiteralPath $Path)) {
            return
        }
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Refusing to remove a non-file Watchdog path: $Path"
        }

        try {
            Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
            if (-not (Test-Path -LiteralPath $Path)) {
                return
            }
        }
        catch {
            $lastError = $_
        }

        if ($attempt -lt $Attempts) {
            Start-Sleep -Milliseconds 250
        }
    }

    if ($null -ne $lastError) {
        throw $lastError
    }
    throw "Could not remove Watchdog file: $Path"
}

function Remove-WatchdogDirectoryWithRetry {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [switch]$Recurse,
        [ValidateRange(1, 10)][int]$Attempts = 4
    )

    $lastError = $null
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        if (-not (Test-Path -LiteralPath $Path)) {
            return
        }
        if (-not (Test-Path -LiteralPath $Path -PathType Container)) {
            throw "Refusing to remove a non-directory Watchdog path: $Path"
        }

        $item = Get-Item -LiteralPath $Path -Force
        if (($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Refusing to recursively remove a reparse-point Watchdog directory: $Path"
        }

        try {
            if ($Recurse) {
                Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
            }
            else {
                Remove-Item -LiteralPath $Path -Force -ErrorAction Stop
            }
            if (-not (Test-Path -LiteralPath $Path)) {
                return
            }
        }
        catch {
            $lastError = $_
        }

        if ($attempt -lt $Attempts) {
            Start-Sleep -Milliseconds 250
        }
    }

    if ($null -ne $lastError) {
        throw $lastError
    }
    throw "Could not remove Watchdog directory: $Path"
}

$config = $null
if (Test-Path -LiteralPath $paths.ConfigPath -PathType Leaf) {
    try {
        $config = Read-HermesGatewayWatchdogConfig -Path $paths.ConfigPath
    }
    catch {
        Write-Warning 'The Watchdog configuration is invalid. Uninstallation will continue without configuration-derived log settings.'
    }
}

[void](Get-OwnedWatchdogScheduledTask)

if ($DryRun) {
    Write-Host "[DryRun] Disable and remove current-user task $taskName after the Watchdog exits"
    Write-Host "[DryRun] Remove managed Watchdog files under $($paths.RootPath)"
    if ($RemoveLogs) {
        Write-Host "[DryRun] Remove Watchdog logs under $($paths.LogDirectory)"
    }
    else {
        Write-Host "[DryRun] Preserve Watchdog logs under $($paths.LogDirectory)"
    }
    Write-Host 'Uninstall preview completed. No files, processes, logs, or scheduled tasks were changed.'
    return
}

$uninstallTarget = "$($paths.RootPath) and scheduled task $taskName"
if (-not $PSCmdlet.ShouldProcess($uninstallTarget, 'Uninstall Hermes Gateway Watchdog for the current user')) {
    Write-Host 'Uninstall preview completed. No files, processes, logs, or scheduled tasks were changed.'
    return
}

if ($isElevated) {
    throw 'Run the uninstaller from a non-elevated PowerShell session.'
}

$lifecycleLock = Enter-WatchdogLifecycleMutex -TimeoutMilliseconds 30000
if (-not $lifecycleLock.Acquired) {
    $lifecycleLock.Mutex.Dispose()
    throw 'Another Watchdog install or uninstall operation is already running.'
}

try {
    if ($null -ne $config) {
        try {
            Write-HermesGatewayWatchdogLog -LogPath $paths.LogPath -EventName 'uninstall_requested' -Level 'Info' -Message 'Watchdog uninstall requested.' -MaxBytes $config.LogMaxBytes -RetentionCount $config.LogRetentionCount
        }
        catch {
            Write-Warning 'The uninstall event could not be written to the Watchdog log.'
        }
    }

    Stop-WatchdogForLifecycle -WatchdogPaths $paths

    $ownedTask = Get-OwnedWatchdogScheduledTask
    if ($null -ne $ownedTask) {
        Unregister-ScheduledTask -TaskName $taskName -TaskPath $taskPath -Confirm:$false
        if (-not (Wait-WatchdogTaskRemoved -TimeoutSeconds 5)) {
            throw "Scheduled task '$taskName' could not be confirmed as removed."
        }
    }

    $managedFiles = @(
        'Install-HermesGatewayWatchdog.ps1',
        'Pause-HermesGatewayWatchdog.ps1',
        'Resume-HermesGatewayWatchdog.ps1',
        'Get-HermesGatewayWatchdogStatus.ps1',
        'Test-HermesGatewayWatchdog.ps1',
        'watchdog.config.example.json',
        'README.md'
    )
    foreach ($fileName in $managedFiles) {
        Remove-WatchdogFileWithRetry -Path (Join-Path $paths.RootPath $fileName)
    }

    foreach ($dataPath in @(
        $paths.ConfigPath,
        $paths.PauseFlagPath,
        $paths.StopFlagPath,
        $paths.StatePath
    )) {
        Remove-WatchdogFileWithRetry -Path $dataPath
    }

    if ($RemoveLogs) {
        Remove-WatchdogDirectoryWithRetry -Path $paths.LogDirectory -Recurse
    }

    Remove-WatchdogFileWithRetry -Path (Join-Path $paths.RootPath 'HermesGatewayWatchdog.ps1')
    Remove-WatchdogFileWithRetry -Path (Join-Path $paths.RootPath 'Uninstall-HermesGatewayWatchdog.ps1')

    if (Test-Path -LiteralPath $paths.RootPath -PathType Container) {
        $remainingItem = Get-ChildItem -LiteralPath $paths.RootPath -Force | Select-Object -First 1
        if ($null -eq $remainingItem) {
            Remove-WatchdogDirectoryWithRetry -Path $paths.RootPath
        }
    }
}
finally {
    Exit-WatchdogLifecycleMutex -Lock $lifecycleLock
}

if ($RemoveLogs) {
    Write-Host 'Hermes Gateway Watchdog uninstalled. Watchdog logs were removed.'
}
else {
    Write-Host "Hermes Gateway Watchdog uninstalled. Logs were preserved at $($paths.LogDirectory)."
}
