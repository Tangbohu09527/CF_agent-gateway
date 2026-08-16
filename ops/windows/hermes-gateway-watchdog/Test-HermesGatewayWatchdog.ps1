#requires -Version 5.1

[CmdletBinding()]
param()

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

$watchdogScript = Join-Path $PSScriptRoot 'HermesGatewayWatchdog.ps1'
$installScript = Join-Path $PSScriptRoot 'Install-HermesGatewayWatchdog.ps1'
$uninstallScript = Join-Path $PSScriptRoot 'Uninstall-HermesGatewayWatchdog.ps1'
$pauseScript = Join-Path $PSScriptRoot 'Pause-HermesGatewayWatchdog.ps1'

if (-not (Test-Path -LiteralPath $watchdogScript -PathType Leaf)) {
    throw "Watchdog script was not found: $watchdogScript"
}

. $watchdogScript

$script:PassedCount = 0
$script:FailedCount = 0
$script:Failures = New-Object System.Collections.ArrayList

function Assert-WatchdogTestTrue {
    param(
        [Parameter(Mandatory = $true)][bool]$Condition,
        [Parameter(Mandatory = $true)][string]$Message
    )

    if (-not $Condition) {
        throw $Message
    }
}

function Assert-WatchdogTestEqual {
    param(
        [AllowNull()]$Expected,
        [AllowNull()]$Actual,
        [Parameter(Mandatory = $true)][string]$Message
    )

    if (-not [object]::Equals($Expected, $Actual)) {
        throw ("{0} Expected: <{1}>. Actual: <{2}>." -f $Message, $Expected, $Actual)
    }
}

function Assert-WatchdogTestThrows {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$Action,
        [Parameter(Mandatory = $true)][string]$Message
    )

    $threw = $false
    try {
        & $Action
    }
    catch {
        $threw = $true
    }

    if (-not $threw) {
        throw $Message
    }
}

function Invoke-WatchdogTestCase {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][scriptblock]$Test
    )

    try {
        & $Test
        $script:PassedCount++
        Write-Host "[PASS] $Name"
    }
    catch {
        $script:FailedCount++
        $detail = "[FAIL] $Name`: $($_.Exception.Message)"
        [void]$script:Failures.Add($detail)
        Write-Host $detail -ForegroundColor Red
    }
}

function New-WatchdogTestQueue {
    param(
        [object[]]$Values = @()
    )

    $queue = New-Object System.Collections.Queue
    foreach ($value in $Values) {
        $queue.Enqueue($value)
    }
    Write-Output -NoEnumerate $queue
}

function New-WatchdogTestConfig {
    [pscustomobject]@{
        SchemaVersion                       = 1
        HealthUrl                          = 'http://192.0.2.10:8642/health'
        HermesExecutablePath               = Join-Path ([System.IO.Path]::GetTempPath()) 'hermes.exe'
        StartupGraceSeconds                = 45
        PollIntervalSeconds                 = 2
        FailureSettleDelaySeconds           = 3
        HealthConnectTimeoutSeconds         = 3
        HealthTotalTimeoutSeconds           = 5
        GatewayStartTimeoutSeconds          = 30
        MaximumRestartBackoffSeconds        = 5
        RecoveryVerificationTimeoutSeconds = 6
        RequiredConsecutiveHealthyChecks    = 3
        LogMaxBytes                         = 4096
        LogRetentionCount                   = 2
    }
}

function New-WatchdogCycleHarness {
    param(
        [object[]]$HealthResults = @($true),
        [object[]]$PauseResults = @($false),
        [object[]]$StartExitCodes = @(0)
    )

    $context = @{
        HealthQueue  = New-WatchdogTestQueue -Values $HealthResults
        PauseQueue   = New-WatchdogTestQueue -Values $PauseResults
        StartQueue   = New-WatchdogTestQueue -Values $StartExitCodes
        LastHealth   = $false
        LastPaused   = $false
        LastExitCode = 0
        HealthCalls  = 0
        PauseCalls   = 0
        StartCalls   = 0
        Sleeps       = New-Object System.Collections.ArrayList
        Events       = New-Object System.Collections.ArrayList
        NowUtc       = [datetime]'2026-08-16T00:00:00Z'
    }

    $healthCheck = {
        $context.HealthCalls = [int]$context.HealthCalls + 1
        if ($context.HealthQueue.Count -gt 0) {
            $context.LastHealth = [bool]$context.HealthQueue.Dequeue()
        }
        [bool]$context.LastHealth
    }.GetNewClosure()

    $isPaused = {
        $context.PauseCalls = [int]$context.PauseCalls + 1
        if ($context.PauseQueue.Count -gt 0) {
            $context.LastPaused = [bool]$context.PauseQueue.Dequeue()
        }
        [bool]$context.LastPaused
    }.GetNewClosure()

    $startGateway = {
        $context.StartCalls = [int]$context.StartCalls + 1
        if ($context.StartQueue.Count -gt 0) {
            $context.LastExitCode = [int]$context.StartQueue.Dequeue()
        }
        [int]$context.LastExitCode
    }.GetNewClosure()

    $sleep = {
        param($seconds)
        [void]$context.Sleeps.Add([int]$seconds)
        $context.NowUtc = ([datetime]$context.NowUtc).AddSeconds([int]$seconds)
    }.GetNewClosure()

    $getUtcNow = {
        [datetime]$context.NowUtc
    }.GetNewClosure()

    $writeEvent = {
        param($eventName, $level, $message, $data)
        $dataCopy = @{}
        if ($null -ne $data) {
            foreach ($key in @($data.Keys)) {
                $dataCopy[$key] = $data[$key]
            }
        }
        [void]$context.Events.Add([pscustomobject]@{
            EventName = [string]$eventName
            Level     = [string]$level
            Message   = [string]$message
            Data      = $dataCopy
        })
    }.GetNewClosure()

    [pscustomobject]@{
        Context      = $context
        State        = New-HermesGatewayWatchdogState
        HealthCheck  = $healthCheck
        IsPaused     = $isPaused
        StartGateway = $startGateway
        Sleep        = $sleep
        GetUtcNow    = $getUtcNow
        WriteEvent   = $writeEvent
    }
}

function Invoke-WatchdogTestCycle {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)]$Harness
    )

    Invoke-HermesGatewayWatchdogCycle -Config $Config -State $Harness.State `
        -IsPaused $Harness.IsPaused -HealthCheck $Harness.HealthCheck `
        -StartGateway $Harness.StartGateway -Sleep $Harness.Sleep `
        -GetUtcNow $Harness.GetUtcNow -WriteEvent $Harness.WriteEvent
}

function Get-WatchdogTestEventCount {
    param(
        [Parameter(Mandatory = $true)]$Harness,
        [Parameter(Mandatory = $true)][string]$EventName
    )

    @($Harness.Context.Events | Where-Object { $_.EventName -eq $EventName }).Count
}

function Set-WatchdogTestStartBehavior {
    param(
        [Parameter(Mandatory = $true)]$Harness,
        [Parameter(Mandatory = $true)][scriptblock]$Behavior
    )

    $context = $Harness.Context
    $behaviorCopy = $Behavior
    $Harness.StartGateway = {
        $context.StartCalls = [int]$context.StartCalls + 1
        & $behaviorCopy
    }.GetNewClosure()
}


function Start-WatchdogTestHttpServer {
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Body,
        [ValidateRange(0, 30)][int]$ResponseDelaySeconds = 0
    )

    $job = Start-Job -ScriptBlock {
        param($responseBody, $delaySeconds)
        $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Loopback, 0)
        $client = $null
        try {
            $listener.Start()
            Write-Output ('READY:{0}' -f $listener.LocalEndpoint.Port)
            $client = $listener.AcceptTcpClient()
            if ($delaySeconds -gt 0) {
                Start-Sleep -Seconds $delaySeconds
            }
            else {
                $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes([string]$responseBody)
                $newLine = [string][char]13 + [char]10
                $headers = 'HTTP/1.1 200 OK' + $newLine +
                    'Content-Type: application/json' + $newLine +
                    ('Content-Length: {0}' -f $bodyBytes.Length) + $newLine +
                    'Connection: close' + $newLine + $newLine
                $headerBytes = [System.Text.Encoding]::ASCII.GetBytes($headers)
                $stream = $client.GetStream()
                $stream.Write($headerBytes, 0, $headerBytes.Length)
                $stream.Write($bodyBytes, 0, $bodyBytes.Length)
                $stream.Flush()
            }
        }
        finally {
            if ($null -ne $client) { $client.Dispose() }
            $listener.Stop()
        }
    } -ArgumentList $Body, $ResponseDelaySeconds

    $deadline = [datetime]::UtcNow.AddSeconds(15)
    $port = $null
    while ([datetime]::UtcNow -lt $deadline -and $null -eq $port) {
        $output = @(Receive-Job -Job $job -Keep -ErrorAction SilentlyContinue)
        foreach ($item in $output) {
            if ([string]$item -match '^READY:(\d+)$') {
                $port = [int]$matches[1]
                break
            }
        }
        if ($null -eq $port -and $job.State -in @('Completed', 'Failed', 'Stopped')) {
            break
        }
        if ($null -eq $port) {
            Start-Sleep -Milliseconds 100
        }
    }
    if ($null -eq $port) {
        Stop-Job -Job $job -ErrorAction SilentlyContinue
        Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
        throw 'The loopback health test server did not become ready.'
    }

    [pscustomobject]@{
        Job  = $job
        Port = $port
    }
}

function Stop-WatchdogTestHttpServer {
    param(
        [Parameter(Mandatory = $true)]$Server
    )

    if ($Server.Job.State -notin @('Completed', 'Failed', 'Stopped')) {
        Stop-Job -Job $Server.Job -ErrorAction SilentlyContinue
    }
    Receive-Job -Job $Server.Job -ErrorAction SilentlyContinue | Out-Null
    Remove-Job -Job $Server.Job -Force -ErrorAction SilentlyContinue
}

function Write-WatchdogTestUtf8File {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Content
    )

    [System.IO.File]::WriteAllText(
        $Path,
        $Content,
        (New-Object System.Text.UTF8Encoding($false))
    )
}

function Get-WatchdogTestDirectoryFingerprint {
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path)) {
        return '<absent>'
    }

    $root = [System.IO.Path]::GetFullPath($Path).TrimEnd('\', '/')
    $entries = foreach ($item in @(Get-ChildItem -LiteralPath $root -Recurse -Force | Sort-Object FullName)) {
        $relative = $item.FullName.Substring($root.Length).TrimStart('\', '/')
        if ($item.PSIsContainer) {
            "D|$relative|$($item.LastWriteTimeUtc.Ticks)"
        }
        else {
            $hash = (Get-FileHash -LiteralPath $item.FullName -Algorithm SHA256).Hash
            "F|$relative|$($item.Length)|$($item.LastWriteTimeUtc.Ticks)|$hash"
        }
    }
    (@($entries) -join "`n")
}

function Get-WatchdogTestTaskFingerprint {
    $taskName = 'CF_Hermes_Gateway_Watchdog'
    try {
        [string](Export-ScheduledTask -TaskName $taskName -TaskPath '\' -ErrorAction Stop)
    }
    catch {
        '<absent>'
    }
}

$temporaryRoot = Join-Path ([System.IO.Path]::GetTempPath()) `
    ("CF-HermesGatewayWatchdog-Test-{0}" -f [guid]::NewGuid().ToString('N'))
$originalLocalAppData = $env:LOCALAPPDATA
$originalPath = $env:PATH

try {
    [void](New-Item -ItemType Directory -Path $temporaryRoot -Force)
    $config = New-WatchdogTestConfig

    Invoke-WatchdogTestCase -Name '1. always healthy does not start Gateway' -Test {
        $harness = New-WatchdogCycleHarness -HealthResults @($true)
        $result = Invoke-WatchdogTestCycle -Config $config -Harness $harness

        Assert-WatchdogTestEqual -Expected 'Healthy' -Actual $result.Outcome `
            -Message 'A healthy cycle returned the wrong outcome.'
        Assert-WatchdogTestEqual -Expected 0 -Actual $harness.Context.StartCalls `
            -Message 'A healthy cycle attempted to start Gateway.'
        Assert-WatchdogTestEqual -Expected 0 -Actual $harness.Context.Sleeps.Count `
            -Message 'A healthy cycle entered recovery delays.'
    }

    Invoke-WatchdogTestCase -Name '2. transient failure settles without starting Gateway' -Test {
        $harness = New-WatchdogCycleHarness -HealthResults @($false, $true)
        $result = Invoke-WatchdogTestCycle -Config $config -Harness $harness

        Assert-WatchdogTestEqual -Expected 'SettledHealthy' -Actual $result.Outcome `
            -Message 'Transient recovery returned the wrong outcome.'
        Assert-WatchdogTestEqual -Expected 0 -Actual $harness.Context.StartCalls `
            -Message 'Transient failure attempted to start Gateway.'
        Assert-WatchdogTestEqual -Expected 1 -Actual $harness.Context.Sleeps.Count `
            -Message 'Transient failure did not use exactly one settle delay.'
        Assert-WatchdogTestEqual -Expected $config.FailureSettleDelaySeconds `
            -Actual $harness.Context.Sleeps[0] -Message 'The settle delay was incorrect.'
    }

    Invoke-WatchdogTestCase -Name '3. persistent failure starts Gateway once after settle' -Test {
        $harness = New-WatchdogCycleHarness -HealthResults @($false)
        $result = Invoke-WatchdogTestCycle -Config $config -Harness $harness

        Assert-WatchdogTestEqual -Expected 'RecoveryFailed' -Actual $result.Outcome `
            -Message 'Persistent failure returned the wrong outcome.'
        Assert-WatchdogTestEqual -Expected 1 -Actual $harness.Context.StartCalls `
            -Message 'Persistent failure did not make exactly one start attempt.'
        Assert-WatchdogTestEqual -Expected $config.FailureSettleDelaySeconds `
            -Actual $harness.Context.Sleeps[0] -Message 'Gateway started before the settle delay.'
        Assert-WatchdogTestEqual -Expected 1 `
            -Actual (Get-WatchdogTestEventCount -Harness $harness -EventName 'gateway_start_attempt') `
            -Message 'The start attempt event count was incorrect.'
    }

    Invoke-WatchdogTestCase -Name '4. start followed by three healthy checks records recovery' -Test {
        $harness = New-WatchdogCycleHarness `
            -HealthResults @($false, $false, $true, $true, $true)
        $result = Invoke-WatchdogTestCycle -Config $config -Harness $harness

        Assert-WatchdogTestEqual -Expected 'Recovered' -Actual $result.Outcome `
            -Message 'Successful recovery returned the wrong outcome.'
        Assert-WatchdogTestEqual -Expected 1 -Actual $harness.Context.StartCalls `
            -Message 'Successful recovery made the wrong number of start attempts.'
        Assert-WatchdogTestEqual -Expected 1 `
            -Actual (Get-WatchdogTestEventCount -Harness $harness -EventName 'recovery_succeeded') `
            -Message 'Recovery success was not recorded exactly once.'
        $recoveryEvent = @($harness.Context.Events | Where-Object {
            $_.EventName -eq 'recovery_succeeded'
        })[0]
        Assert-WatchdogTestEqual -Expected 3 `
            -Actual ([int]$recoveryEvent.Data.consecutive_successes) `
            -Message 'Recovery was recorded without three consecutive successes.'
        Assert-WatchdogTestTrue -Condition ($null -ne $harness.State.LastRecoveryUtc) `
            -Message 'The last recovery time was not saved in state.'
    }

    Invoke-WatchdogTestCase -Name '5. failed recovery enters bounded exponential backoff' -Test {
        $harness = New-WatchdogCycleHarness -HealthResults @($false)
        $first = Invoke-WatchdogTestCycle -Config $config -Harness $harness
        $startCallsAfterFirst = $harness.Context.StartCalls
        $duringBackoff = Invoke-WatchdogTestCycle -Config $config -Harness $harness

        Assert-WatchdogTestEqual -Expected 'RecoveryFailed' -Actual $first.Outcome `
            -Message 'The first failed recovery returned the wrong outcome.'
        Assert-WatchdogTestEqual -Expected $config.PollIntervalSeconds `
            -Actual $first.BackoffSeconds -Message 'The initial backoff was incorrect.'
        Assert-WatchdogTestEqual -Expected 'Backoff' -Actual $duringBackoff.Outcome `
            -Message 'A cycle inside the backoff window did not stay in backoff.'
        Assert-WatchdogTestEqual -Expected $startCallsAfterFirst `
            -Actual $harness.Context.StartCalls -Message 'Gateway was started during backoff.'

        $harness.Context.NowUtc = ([datetime]$harness.Context.NowUtc).AddSeconds($first.BackoffSeconds)
        $second = Invoke-WatchdogTestCycle -Config $config -Harness $harness
        Assert-WatchdogTestEqual -Expected 2 -Actual $harness.Context.StartCalls `
            -Message 'Gateway was not retried after backoff elapsed.'
        Assert-WatchdogTestEqual -Expected 4 -Actual $second.BackoffSeconds `
            -Message 'The second exponential backoff was incorrect.'

        $harness.Context.NowUtc = ([datetime]$harness.Context.NowUtc).AddSeconds($second.BackoffSeconds)
        $third = Invoke-WatchdogTestCycle -Config $config -Harness $harness
        Assert-WatchdogTestEqual -Expected $config.MaximumRestartBackoffSeconds `
            -Actual $third.BackoffSeconds -Message 'Backoff did not stop at the configured maximum.'
    }

    Invoke-WatchdogTestCase -Name '6. paused watchdog never starts Gateway' -Test {
        $harness = New-WatchdogCycleHarness `
            -HealthResults @($false) -PauseResults @($true)
        $result = Invoke-WatchdogTestCycle -Config $config -Harness $harness

        Assert-WatchdogTestEqual -Expected 'Paused' -Actual $result.Outcome `
            -Message 'Paused cycle returned the wrong outcome.'
        Assert-WatchdogTestEqual -Expected 0 -Actual $harness.Context.StartCalls `
            -Message 'Paused cycle attempted to start Gateway.'
        Assert-WatchdogTestEqual -Expected 0 -Actual $harness.Context.Sleeps.Count `
            -Message 'Paused cycle entered a recovery delay.'
    }

    Invoke-WatchdogTestCase -Name '7. concurrent instances allow only one mutex owner' -Test {
        $mutexConfigPath = Join-Path $temporaryRoot 'concurrent\watchdog.config.json'
        $parentLock = Enter-HermesGatewayWatchdogMutex -ConfigPath $mutexConfigPath
        Assert-WatchdogTestTrue -Condition ([bool]$parentLock.Acquired) `
            -Message 'The first instance could not acquire its mutex.'

        $job = $null
        try {
            $job = Start-Job -ScriptBlock {
                param($scriptPath, $childConfigPath)
                $ErrorActionPreference = 'Stop'
                . $scriptPath
                $childLock = Enter-HermesGatewayWatchdogMutex -ConfigPath $childConfigPath
                try {
                    [bool]$childLock.Acquired
                }
                finally {
                    Exit-HermesGatewayWatchdogMutex -Lock $childLock
                }
            } -ArgumentList $watchdogScript, $mutexConfigPath

            $completedJob = Wait-Job -Job $job -Timeout 20
            Assert-WatchdogTestTrue -Condition ($null -ne $completedJob) `
                -Message 'The competing mutex process timed out.'
            $childOutput = @(Receive-Job -Job $job -ErrorAction Stop)
            Assert-WatchdogTestEqual -Expected 1 -Actual $childOutput.Count `
                -Message 'The competing process returned unexpected output.'
            Assert-WatchdogTestEqual -Expected $false -Actual ([bool]$childOutput[0]) `
                -Message 'Two instances acquired the same mutex.'
        }
        finally {
            if ($null -ne $job) {
                Remove-Job -Job $job -Force -ErrorAction SilentlyContinue
            }
            Exit-HermesGatewayWatchdogMutex -Lock $parentLock
        }

        $replacementLock = Enter-HermesGatewayWatchdogMutex -ConfigPath $mutexConfigPath
        try {
            Assert-WatchdogTestTrue -Condition ([bool]$replacementLock.Acquired) `
                -Message 'The mutex was not released for a later instance.'
        }
        finally {
            Exit-HermesGatewayWatchdogMutex -Lock $replacementLock
        }
    }

    Invoke-WatchdogTestCase -Name '7b. pause flag is linearized with the start gate' -Test {
        Assert-WatchdogTestTrue -Condition (Test-Path -LiteralPath $pauseScript -PathType Leaf) `
            -Message 'Pause script is missing.'

        $raceLocalAppData = Join-Path $temporaryRoot 'pause-race-localappdata'
        $raceRoot = Join-Path $raceLocalAppData 'CF\HermesGatewayWatchdog'
        $raceConfigPath = Join-Path $raceRoot 'watchdog.config.json'
        $racePausePath = Join-Path $raceRoot 'watchdog.pause'
        $jobReadyPath = Join-Path $temporaryRoot 'pause-race-job.ready'
        [void](New-Item -ItemType Directory -Path $raceRoot -Force)
        Write-WatchdogTestUtf8File -Path $raceConfigPath -Content '{invalid-json'

        $parentStartLock = Enter-HermesGatewayWatchdogStartMutex -ConfigPath $raceConfigPath -TimeoutMilliseconds 0
        Assert-WatchdogTestTrue -Condition ([bool]$parentStartLock.Acquired) `
            -Message 'The parent test could not acquire the Gateway start gate.'

        $pauseJob = $null
        try {
            $pauseJob = Start-Job -ScriptBlock {
                param($scriptPath, $localAppData, $readyPath)
                $ErrorActionPreference = 'Stop'
                $env:LOCALAPPDATA = $localAppData
                [System.IO.File]::WriteAllText($readyPath, 'ready')
                & $scriptPath
            } -ArgumentList $pauseScript, $raceLocalAppData, $jobReadyPath

            $readyDeadline = [datetime]::UtcNow.AddSeconds(15)
            while (-not (Test-Path -LiteralPath $jobReadyPath -PathType Leaf) -and
                [datetime]::UtcNow -lt $readyDeadline) {
                Start-Sleep -Milliseconds 100
            }
            Assert-WatchdogTestTrue -Condition (Test-Path -LiteralPath $jobReadyPath -PathType Leaf) `
                -Message 'The competing Pause process did not start.'
            Start-Sleep -Milliseconds 500
            Assert-WatchdogTestTrue -Condition (-not (Test-Path -LiteralPath $racePausePath)) `
                -Message 'Pause flag became visible before the start gate was acquired.'

            Exit-HermesGatewayWatchdogStartMutex -Lock $parentStartLock
            $parentStartLock = $null
            $completedPause = Wait-Job -Job $pauseJob -Timeout 20
            Assert-WatchdogTestTrue -Condition ($null -ne $completedPause) `
                -Message 'Pause did not complete after the start gate was released.'
            Receive-Job -Job $pauseJob -ErrorAction Stop | Out-Null
            Assert-WatchdogTestTrue -Condition (Test-Path -LiteralPath $racePausePath -PathType Leaf) `
                -Message 'Pause flag was not written after gate acquisition.'
        }
        finally {
            if ($null -ne $parentStartLock) {
                Exit-HermesGatewayWatchdogStartMutex -Lock $parentStartLock
            }
            if ($null -ne $pauseJob) {
                if ($pauseJob.State -notin @('Completed', 'Failed', 'Stopped')) {
                    Stop-Job -Job $pauseJob -ErrorAction SilentlyContinue
                }
                Remove-Job -Job $pauseJob -Force -ErrorAction SilentlyContinue
            }
        }
    }

    Invoke-WatchdogTestCase -Name '8. missing or invalid configuration fails safely' -Test {
        $configRoot = Join-Path $temporaryRoot 'config-validation'
        [void](New-Item -ItemType Directory -Path $configRoot -Force)
        $missingPath = Join-Path $configRoot 'missing.json'
        $invalidJsonPath = Join-Path $configRoot 'invalid.json'
        Write-WatchdogTestUtf8File -Path $invalidJsonPath -Content '{not-json'

        $fakeHermes = Join-Path $configRoot 'hermes.exe'
        [System.IO.File]::WriteAllBytes($fakeHermes, [byte[]]@(77, 90))
        $invalidValuesPath = Join-Path $configRoot 'invalid-values.json'
        $invalidValues = [ordered]@{
            SchemaVersion                    = 1
            HealthUrl                       = 'http://192.0.2.10:8642/health'
            HermesExecutablePath            = $fakeHermes
            StartupGraceSeconds             = 45
            PollIntervalSeconds              = 0
            FailureSettleDelaySeconds        = 15
            HealthConnectTimeoutSeconds      = 3
            HealthTotalTimeoutSeconds        = 5
            GatewayStartTimeoutSeconds       = 30
            MaximumRestartBackoffSeconds     = 60
            RecoveryVerificationTimeoutSeconds = 45
            RequiredConsecutiveHealthyChecks = 3
            LogMaxBytes                      = 4096
            LogRetentionCount                = 2
        }
        Write-WatchdogTestUtf8File -Path $invalidValuesPath `
            -Content ($invalidValues | ConvertTo-Json -Depth 4)

        $statePath = Join-Path $configRoot 'watchdog.state.json'
        Write-HermesGatewayWatchdogStateFile -Path $statePath -LastRecoveryUtc $null -Running $true
        $initialState = [System.IO.File]::ReadAllText($statePath) | ConvertFrom-Json
        Assert-WatchdogTestEqual -Expected $null -Actual $initialState.LastRecoveryUtc -Message 'Initial state did not preserve an empty recovery time safely.'

        Write-WatchdogTestUtf8File -Path $statePath -Content ''
        Assert-WatchdogTestEqual -Expected $null -Actual (Read-HermesGatewayWatchdogStateFile -Path $statePath) `
            -Message 'Empty state was not ignored safely.'
        Write-WatchdogTestUtf8File -Path $statePath -Content '{}'
        Assert-WatchdogTestEqual -Expected $null -Actual (Read-HermesGatewayWatchdogStateFile -Path $statePath) `
            -Message 'Old state without schema fields was not ignored safely.'
        Write-WatchdogTestUtf8File -Path $statePath -Content '{not-json'
        Assert-WatchdogTestEqual -Expected $null -Actual (Read-HermesGatewayWatchdogStateFile -Path $statePath) `
            -Message 'Malformed state was not ignored safely.'

        Assert-WatchdogTestThrows -Action {
            Read-HermesGatewayWatchdogConfig -Path $missingPath | Out-Null
        } -Message 'Missing configuration did not fail.'
        Assert-WatchdogTestThrows -Action {
            Read-HermesGatewayWatchdogConfig -Path $invalidJsonPath | Out-Null
        } -Message 'Malformed JSON did not fail.'
        Assert-WatchdogTestThrows -Action {
            Read-HermesGatewayWatchdogConfig -Path $invalidValuesPath | Out-Null
        } -Message 'Illegal configuration values did not fail.'
    }

    Invoke-WatchdogTestCase -Name '8b. health checks and process identity are strict' -Test {
        Assert-WatchdogTestTrue -Condition (Test-HermesGatewayHealthPayload -StatusCode 200 -Body '{"status":"ok"}') `
            -Message 'A valid health payload was rejected.'
        Assert-WatchdogTestTrue -Condition (-not (Test-HermesGatewayHealthPayload -StatusCode 503 -Body '{"status":"ok"}')) `
            -Message 'A non-success HTTP status was accepted.'
        Assert-WatchdogTestTrue -Condition (-not (Test-HermesGatewayHealthPayload -StatusCode 200 -Body '{bad-json')) `
            -Message 'Malformed health JSON was accepted.'
        Assert-WatchdogTestTrue -Condition (-not (Test-HermesGatewayHealthPayload -StatusCode 200 -Body '{"status":"OK"}')) `
            -Message 'A non-ok health status was accepted.'

        $hermesCommandLine = '"{0}" gateway start' -f $config.HermesExecutablePath
        Assert-WatchdogTestTrue -Condition (Test-HermesGatewayProcessIdentity -ProcessName 'hermes.exe' -CommandLine $hermesCommandLine) `
            -Message 'Hermes executable Gateway identity was not recognized.'
        Assert-WatchdogTestTrue -Condition (Test-HermesGatewayProcessIdentity -ProcessName 'python.exe' -CommandLine 'python.exe -m hermes_cli.main gateway run') `
            -Message 'Hermes CLI Python runtime identity was not recognized.'
        Assert-WatchdogTestTrue -Condition (-not (Test-HermesGatewayProcessIdentity -ProcessName 'python.exe' -CommandLine 'python.exe -m http.server 8642')) `
            -Message 'An unrelated listener process was identified as Hermes Gateway.'
        Assert-WatchdogTestTrue -Condition (Test-HermesGatewayStartCommandIdentity `
            -ProcessName 'hermes.exe' -ExecutablePath $config.HermesExecutablePath `
            -CommandLine $hermesCommandLine -HermesExecutablePath $config.HermesExecutablePath) `
            -Message 'The configured Hermes start command was not recognized.'
        Assert-WatchdogTestTrue -Condition (-not (Test-HermesGatewayStartCommandIdentity `
            -ProcessName 'python.exe' -ExecutablePath (Join-Path $temporaryRoot 'other\python.exe') `
            -CommandLine 'python.exe -m hermes_cli.main gateway start' `
            -HermesExecutablePath $config.HermesExecutablePath)) `
            -Message 'An unrelated Python Hermes installation suppressed the configured start.'

        $healthyServer = $null
        $stalledServer = $null
        try {
            $healthyServer = Start-WatchdogTestHttpServer -Body '{"status":"ok"}'
            $healthyConfig = [pscustomobject]@{
                HealthUri                   = [uri]("http://127.0.0.1:{0}/health" -f $healthyServer.Port)
                HealthConnectTimeoutSeconds = 1
                HealthTotalTimeoutSeconds   = 2
            }
            Assert-WatchdogTestTrue -Condition (Test-HermesGatewayHealth -Config $healthyConfig) `
                -Message 'The WinHTTP health probe rejected a valid loopback response.'
            Stop-WatchdogTestHttpServer -Server $healthyServer
            $healthyServer = $null

            $stalledServer = Start-WatchdogTestHttpServer -Body '' -ResponseDelaySeconds 4
            $stalledConfig = [pscustomobject]@{
                HealthUri                   = [uri]("http://127.0.0.1:{0}/health" -f $stalledServer.Port)
                HealthConnectTimeoutSeconds = 1
                HealthTotalTimeoutSeconds   = 1
            }
            $stopwatch = [System.Diagnostics.Stopwatch]::StartNew()
            $stalledHealthy = Test-HermesGatewayHealth -Config $stalledConfig
            $stopwatch.Stop()
            Assert-WatchdogTestTrue -Condition (-not $stalledHealthy) `
                -Message 'A stalled health response was accepted.'
            Assert-WatchdogTestTrue -Condition ($stopwatch.Elapsed.TotalSeconds -lt 3.5) `
                -Message 'The health probe did not enforce its total timeout.'
        }
        finally {
            if ($null -ne $healthyServer) { Stop-WatchdogTestHttpServer -Server $healthyServer }
            if ($null -ne $stalledServer) { Stop-WatchdogTestHttpServer -Server $stalledServer }
        }
    }

    Invoke-WatchdogTestCase -Name '8c. structured start results produce mutually exclusive events' -Test {
        $startedInProgress = New-WatchdogCycleHarness -HealthResults @($false)
        Set-WatchdogTestStartBehavior -Harness $startedInProgress -Behavior {
            [pscustomobject]@{ StartedNew = $true; Completed = $false; ExitCode = $null; Reason = 'command_timeout' }
        }
        [void](Invoke-WatchdogTestCycle -Config $config -Harness $startedInProgress)
        Assert-WatchdogTestEqual -Expected 1 -Actual (Get-WatchdogTestEventCount -Harness $startedInProgress -EventName 'gateway_start_attempt') `
            -Message 'A new in-progress command did not record one attempt.'
        Assert-WatchdogTestEqual -Expected 1 -Actual (Get-WatchdogTestEventCount -Harness $startedInProgress -EventName 'gateway_start_in_progress') `
            -Message 'A new in-progress command did not record its state.'
        Assert-WatchdogTestEqual -Expected 0 -Actual (Get-WatchdogTestEventCount -Harness $startedInProgress -EventName 'gateway_start_exit') `
            -Message 'An in-progress command recorded a fake exit.'

        $priorInProgress = New-WatchdogCycleHarness -HealthResults @($false)
        Set-WatchdogTestStartBehavior -Harness $priorInProgress -Behavior {
            [pscustomobject]@{ StartedNew = $false; Completed = $false; ExitCode = $null; Reason = 'command_still_running' }
        }
        [void](Invoke-WatchdogTestCycle -Config $config -Harness $priorInProgress)
        Assert-WatchdogTestEqual -Expected 0 -Actual (Get-WatchdogTestEventCount -Harness $priorInProgress -EventName 'gateway_start_attempt') `
            -Message 'A prior in-progress command recorded a duplicate attempt.'
        Assert-WatchdogTestEqual -Expected 1 -Actual (Get-WatchdogTestEventCount -Harness $priorInProgress -EventName 'gateway_start_in_progress') `
            -Message 'A prior in-progress command was not reported.'

        $priorCompleted = New-WatchdogCycleHarness -HealthResults @($false)
        Set-WatchdogTestStartBehavior -Harness $priorCompleted -Behavior {
            [pscustomobject]@{ StartedNew = $false; Completed = $true; ExitCode = 7; Reason = 'prior_command_exited' }
        }
        [void](Invoke-WatchdogTestCycle -Config $config -Harness $priorCompleted)
        Assert-WatchdogTestEqual -Expected 1 -Actual (Get-WatchdogTestEventCount -Harness $priorCompleted -EventName 'gateway_start_exit') `
            -Message 'A completed prior command did not record its exit.'
        Assert-WatchdogTestEqual -Expected 0 -Actual (Get-WatchdogTestEventCount -Harness $priorCompleted -EventName 'gateway_start_in_progress') `
            -Message 'A completed prior command was still reported in progress.'

        $cancelled = New-WatchdogCycleHarness -HealthResults @($false)
        Set-WatchdogTestStartBehavior -Harness $cancelled -Behavior {
            throw (New-Object System.OperationCanceledException('paused'))
        }
        $cancelledResult = Invoke-WatchdogTestCycle -Config $config -Harness $cancelled
        Assert-WatchdogTestEqual -Expected 'Paused' -Actual $cancelledResult.Outcome `
            -Message 'A canceled start was not treated as paused.'
        foreach ($eventName in @('gateway_start_attempt', 'gateway_start_exit', 'gateway_start_in_progress', 'gateway_start_failed')) {
            Assert-WatchdogTestEqual -Expected 0 -Actual (Get-WatchdogTestEventCount -Harness $cancelled -EventName $eventName) `
                -Message "A canceled start recorded a false $eventName event."
        }

        $launchFailed = New-WatchdogCycleHarness -HealthResults @($false)
        Set-WatchdogTestStartBehavior -Harness $launchFailed -Behavior {
            throw 'simulated launch failure'
        }
        [void](Invoke-WatchdogTestCycle -Config $config -Harness $launchFailed)
        Assert-WatchdogTestEqual -Expected 1 -Actual (Get-WatchdogTestEventCount -Harness $launchFailed -EventName 'gateway_start_failed') `
            -Message 'A launch exception did not record a start failure.'
        Assert-WatchdogTestEqual -Expected 0 -Actual (Get-WatchdogTestEventCount -Harness $launchFailed -EventName 'gateway_start_exit') `
            -Message 'A launch exception recorded a fake exit.'

        $preLogged = New-WatchdogCycleHarness -HealthResults @($false)
        Set-WatchdogTestStartBehavior -Harness $preLogged -Behavior {
            [pscustomobject]@{
                StartedNew = $true
                Completed = $false
                ExitCode = $null
                Reason = 'command_timeout'
                AttemptLogged = $true
            }
        }
        [void](Invoke-WatchdogTestCycle -Config $config -Harness $preLogged)
        Assert-WatchdogTestEqual -Expected 0 -Actual (Get-WatchdogTestEventCount -Harness $preLogged -EventName 'gateway_start_attempt') `
            -Message 'An immediately logged start attempt was recorded twice.'

        $fakeProcess = [pscustomobject]@{
            HasExited = $true
            ExitCode  = 7
            Disposed  = $false
        }
        $fakeProcess | Add-Member -MemberType ScriptMethod -Name Dispose -Value {
            $this.Disposed = $true
        }
        $script:HermesGatewayStartProcess = $fakeProcess
        $pendingCompletion = Get-HermesGatewayStartProcessCompletion
        Assert-WatchdogTestEqual -Expected 7 -Actual $pendingCompletion.ExitCode `
            -Message 'A pending start exit code was not collected.'
        Assert-WatchdogTestEqual -Expected $null -Actual $script:HermesGatewayStartProcess `
            -Message 'A completed pending start handle was not cleared.'
        Assert-WatchdogTestTrue -Condition ([bool]$fakeProcess.Disposed) `
            -Message 'A completed pending start handle was not disposed.'
    }

    Invoke-WatchdogTestCase -Name '9. logs redact sensitive fields and values' -Test {
        $logPath = Join-Path $temporaryRoot 'logs\watchdog.log'
        $secretToken = 'token-value-4a117e'
        $secretApiKey = 'api-key-value-91c025'
        $wechatText = 'wechat-message-body-6c8b9f'
        $sessionText = 'session-content-5fc130'
        $databaseUrl = ('postgres' + 'ql:' + '//dbuser:dbpass@' + 'example.invalid/prod')
        $message = "Token=$secretToken; API Key=$secretApiKey; WeChat=$wechatText; session content=$sessionText"

        Write-HermesGatewayWatchdogLog -LogPath $logPath `
            -EventName 'sensitive_input_test' -Level 'error' -Message $message `
            -Data @{ reason = $databaseUrl } -MaxBytes 4096 -RetentionCount 2

        $logText = [System.IO.File]::ReadAllText($logPath)
        foreach ($secret in @($secretToken, $secretApiKey, $wechatText, $sessionText, $databaseUrl)) {
            Assert-WatchdogTestTrue -Condition ($logText.IndexOf($secret, [StringComparison]::OrdinalIgnoreCase) -lt 0) `
                -Message "Sensitive log value was not redacted: $secret"
        }
        Assert-WatchdogTestTrue -Condition ($logText -match '\[REDACTED\]') `
            -Message 'The log did not contain a redaction marker.'
    }

    Invoke-WatchdogTestCase -Name '10. install and uninstall DryRun do not modify system state' -Test {
        Assert-WatchdogTestTrue -Condition (Test-Path -LiteralPath $installScript -PathType Leaf) `
            -Message 'Installer script is missing.'
        Assert-WatchdogTestTrue -Condition (Test-Path -LiteralPath $uninstallScript -PathType Leaf) `
            -Message 'Uninstaller script is missing.'

        $dryRunLocalAppData = Join-Path $temporaryRoot 'dry-run-localappdata'
        $fakeBin = Join-Path $temporaryRoot 'fake-bin'
        [void](New-Item -ItemType Directory -Path $dryRunLocalAppData -Force)
        [void](New-Item -ItemType Directory -Path $fakeBin -Force)
        $fakeHermes = Join-Path $fakeBin 'hermes.exe'
        [System.IO.File]::WriteAllBytes($fakeHermes, [byte[]]@(77, 90))

        $env:LOCALAPPDATA = $dryRunLocalAppData
        $env:PATH = "$fakeBin;$originalPath"
        $installRoot = Join-Path $dryRunLocalAppData 'CF\HermesGatewayWatchdog'
        [void](New-Item -ItemType Directory -Path $installRoot -Force)
        $staleConfig = New-WatchdogTestConfig
        $staleConfig.HermesExecutablePath = Join-Path $temporaryRoot 'removed-hermes\hermes.exe'
        Write-WatchdogTestUtf8File -Path (Join-Path $installRoot 'watchdog.config.json') `
            -Content (($staleConfig | ConvertTo-Json -Depth 4) + [Environment]::NewLine)

        function Get-ScheduledTask { return $null }

        $taskBeforeInstall = Get-WatchdogTestTaskFingerprint
        $directoryBeforeInstall = Get-WatchdogTestDirectoryFingerprint -Path $installRoot

        & $installScript -HealthUrl 'http://192.0.2.10:8642/health' -DryRun | Out-Null

        $taskAfterInstall = Get-WatchdogTestTaskFingerprint
        $directoryAfterInstall = Get-WatchdogTestDirectoryFingerprint -Path $installRoot
        Assert-WatchdogTestEqual -Expected $taskBeforeInstall -Actual $taskAfterInstall `
            -Message 'Installer DryRun changed the scheduled task definition.'
        Assert-WatchdogTestEqual -Expected $directoryBeforeInstall -Actual $directoryAfterInstall `
            -Message 'Installer DryRun changed the installation directory.'

        [void](New-Item -ItemType Directory -Path $installRoot -Force)
        $markerPath = Join-Path $installRoot 'preserve.marker'
        Write-WatchdogTestUtf8File -Path $markerPath -Content 'preserve this file'
        $taskBeforeUninstall = Get-WatchdogTestTaskFingerprint
        $directoryBeforeUninstall = Get-WatchdogTestDirectoryFingerprint -Path $installRoot

        & $uninstallScript -DryRun | Out-Null

        $taskAfterUninstall = Get-WatchdogTestTaskFingerprint
        $directoryAfterUninstall = Get-WatchdogTestDirectoryFingerprint -Path $installRoot
        Assert-WatchdogTestEqual -Expected $taskBeforeUninstall -Actual $taskAfterUninstall `
            -Message 'Uninstaller DryRun changed the scheduled task definition.'
        Assert-WatchdogTestEqual -Expected $directoryBeforeUninstall -Actual $directoryAfterUninstall `
            -Message 'Uninstaller DryRun changed local files.'
    }
}
finally {
    $env:LOCALAPPDATA = $originalLocalAppData
    $env:PATH = $originalPath
    if (Test-Path -LiteralPath $temporaryRoot) {
        Remove-Item -LiteralPath $temporaryRoot -Recurse -Force -ErrorAction SilentlyContinue
    }
}

Write-Host ("Watchdog tests: {0} passed, {1} failed." -f $script:PassedCount, $script:FailedCount)
if ($script:FailedCount -gt 0) {
    foreach ($failure in $script:Failures) {
        Write-Host $failure -ForegroundColor Red
    }
    exit 1
}

exit 0
