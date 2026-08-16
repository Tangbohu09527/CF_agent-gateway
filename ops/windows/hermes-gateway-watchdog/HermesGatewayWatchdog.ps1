#requires -Version 5.1

[CmdletBinding()]
param(
    [string]$ConfigPath,
    [switch]$RunOnce
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
$script:HermesGatewayStartProcess = $null
try {
    $script:HermesGatewayWatchdogProcessStartUtc =
        (Get-Process -Id $PID -ErrorAction Stop).StartTime.ToUniversalTime()
}
catch {
    $script:HermesGatewayWatchdogProcessStartUtc = [datetime]::UtcNow
}
$script:HermesGatewayWatchdogInstanceId = [guid]::NewGuid().ToString('N')

function Get-HermesGatewayWatchdogPaths {
    [CmdletBinding()]
    param(
        [string]$RootPath
    )

    if ([string]::IsNullOrWhiteSpace($RootPath)) {
        if ([string]::IsNullOrWhiteSpace($env:LOCALAPPDATA)) {
            throw 'LOCALAPPDATA is not available for the current user.'
        }

        $RootPath = Join-Path (Join-Path $env:LOCALAPPDATA 'CF') 'HermesGatewayWatchdog'
    }

    $root = [System.IO.Path]::GetFullPath($RootPath)
    $logDirectory = Join-Path $root 'Logs'

    [pscustomobject]@{
        RootPath      = $root
        ConfigPath    = Join-Path $root 'watchdog.config.json'
        PauseFlagPath = Join-Path $root 'watchdog.pause'
        StopFlagPath  = Join-Path $root 'watchdog.stop'
        StatePath     = Join-Path $root 'watchdog.state.json'
        LogDirectory  = $logDirectory
        LogPath       = Join-Path $logDirectory 'watchdog.log'
    }
}

function Get-HermesGatewayWatchdogDefaultConfig {
    [CmdletBinding()]
    param()

    [ordered]@{
        SchemaVersion                    = 1
        HealthUrl                       = $null
        HermesExecutablePath            = $null
        StartupGraceSeconds              = 45
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

function ConvertTo-HermesGatewayWatchdogInteger {
    param(
        [Parameter(Mandatory = $true)]$Value,
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][int]$Minimum,
        [Parameter(Mandatory = $true)][int]$Maximum
    )

    if ($Value -is [string] -or $Value -is [bool] -or $null -eq $Value) {
        throw "Configuration property '$Name' must be an integer."
    }

    try {
        $number = [double]$Value
    }
    catch {
        throw "Configuration property '$Name' must be an integer."
    }

    if ([double]::IsNaN($number) -or [double]::IsInfinity($number) -or
        $number -ne [math]::Truncate($number) -or $number -lt $Minimum -or
        $number -gt $Maximum) {
        throw "Configuration property '$Name' is outside its allowed range."
    }

    [int]$number
}

function ConvertTo-HermesGatewayHealthConfig {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$InputObject
    )

    $healthUrlProperty = $InputObject.PSObject.Properties['HealthUrl']
    if ($null -eq $healthUrlProperty -or [string]::IsNullOrWhiteSpace([string]$healthUrlProperty.Value)) {
        throw "Configuration property 'HealthUrl' is required."
    }

    $healthUri = $null
    $healthUrl = [string]$healthUrlProperty.Value
    if (-not [uri]::TryCreate($healthUrl, [UriKind]::Absolute, [ref]$healthUri) -or
        @('http', 'https') -notcontains $healthUri.Scheme.ToLowerInvariant() -or
        [string]::IsNullOrWhiteSpace($healthUri.Host) -or
        -not [string]::IsNullOrEmpty($healthUri.UserInfo) -or
        -not [string]::IsNullOrEmpty($healthUri.Query) -or
        -not [string]::IsNullOrEmpty($healthUri.Fragment) -or
        $healthUrl.IndexOfAny([char[]]'<>') -ge 0) {
        throw "Configuration property 'HealthUrl' must be an absolute HTTP(S) URL without credentials, query, fragment, or placeholders."
    }

    $connectProperty = $InputObject.PSObject.Properties['HealthConnectTimeoutSeconds']
    $connectValue = if ($null -eq $connectProperty) { 3 } else { $connectProperty.Value }
    $totalProperty = $InputObject.PSObject.Properties['HealthTotalTimeoutSeconds']
    $totalValue = if ($null -eq $totalProperty) { 5 } else { $totalProperty.Value }
    $connectTimeout = ConvertTo-HermesGatewayWatchdogInteger -Value $connectValue -Name 'HealthConnectTimeoutSeconds' -Minimum 1 -Maximum 60
    $totalTimeout = ConvertTo-HermesGatewayWatchdogInteger -Value $totalValue -Name 'HealthTotalTimeoutSeconds' -Minimum 1 -Maximum 300
    if ($connectTimeout -gt $totalTimeout) {
        throw "Configuration property 'HealthConnectTimeoutSeconds' cannot exceed HealthTotalTimeoutSeconds."
    }

    [pscustomobject]@{
        HealthUrl                  = $healthUri.AbsoluteUri
        HealthUri                  = $healthUri
        HealthConnectTimeoutSeconds = $connectTimeout
        HealthTotalTimeoutSeconds  = $totalTimeout
    }
}

function Read-HermesGatewayHealthConfig {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Watchdog configuration file is missing.'
    }
    try {
        $parsed = [System.IO.File]::ReadAllText([System.IO.Path]::GetFullPath($Path)) |
            ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'Watchdog configuration file is not valid JSON.'
    }
    if ($null -eq $parsed -or $parsed -is [array]) {
        throw 'Watchdog configuration must be one JSON object.'
    }

    ConvertTo-HermesGatewayHealthConfig -InputObject $parsed
}
function ConvertTo-HermesGatewayWatchdogConfig {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$InputObject
    )

    $defaults = Get-HermesGatewayWatchdogDefaultConfig
    $allowedNames = @($defaults.Keys)
    foreach ($property in @($InputObject.PSObject.Properties)) {
        if ($allowedNames -notcontains $property.Name) {
            throw "Unknown Watchdog configuration property '$($property.Name)'."
        }
    }

    $values = [ordered]@{}
    foreach ($name in $allowedNames) {
        $property = $InputObject.PSObject.Properties[$name]
        if ($null -ne $property) {
            $values[$name] = $property.Value
        }
        else {
            $values[$name] = $defaults[$name]
        }
    }

    $schemaVersion = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.SchemaVersion -Name 'SchemaVersion' -Minimum 1 -Maximum 1

    if ([string]::IsNullOrWhiteSpace([string]$values.HealthUrl)) {
        throw "Configuration property 'HealthUrl' is required."
    }

    $healthUri = $null
    if (-not [uri]::TryCreate([string]$values.HealthUrl, [UriKind]::Absolute, [ref]$healthUri) -or
        @('http', 'https') -notcontains $healthUri.Scheme.ToLowerInvariant() -or
        [string]::IsNullOrWhiteSpace($healthUri.Host) -or
        -not [string]::IsNullOrEmpty($healthUri.UserInfo) -or
        -not [string]::IsNullOrEmpty($healthUri.Query) -or
        -not [string]::IsNullOrEmpty($healthUri.Fragment) -or
        ([string]$values.HealthUrl).IndexOfAny([char[]]'<>') -ge 0) {
        throw "Configuration property 'HealthUrl' must be an absolute HTTP(S) URL without credentials, query, fragment, or placeholders."
    }

    if ([string]::IsNullOrWhiteSpace([string]$values.HermesExecutablePath) -or
        -not [System.IO.Path]::IsPathRooted([string]$values.HermesExecutablePath)) {
        throw "Configuration property 'HermesExecutablePath' must be an absolute path."
    }

    $hermesPath = [System.IO.Path]::GetFullPath([string]$values.HermesExecutablePath)
    if ([System.IO.Path]::GetFileName($hermesPath) -ine 'hermes.exe' -or
        -not (Test-Path -LiteralPath $hermesPath -PathType Leaf)) {
        throw "Configuration property 'HermesExecutablePath' must identify an existing hermes.exe file."
    }

    $startupGrace = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.StartupGraceSeconds -Name 'StartupGraceSeconds' -Minimum 0 -Maximum 3600
    $pollInterval = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.PollIntervalSeconds -Name 'PollIntervalSeconds' -Minimum 1 -Maximum 300
    $settleDelay = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.FailureSettleDelaySeconds -Name 'FailureSettleDelaySeconds' -Minimum 0 -Maximum 300
    $connectTimeout = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.HealthConnectTimeoutSeconds -Name 'HealthConnectTimeoutSeconds' -Minimum 1 -Maximum 60
    $totalTimeout = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.HealthTotalTimeoutSeconds -Name 'HealthTotalTimeoutSeconds' -Minimum 1 -Maximum 300
    $gatewayStartTimeout = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.GatewayStartTimeoutSeconds -Name 'GatewayStartTimeoutSeconds' -Minimum 1 -Maximum 300
    if ($connectTimeout -gt $totalTimeout) {
        throw "Configuration property 'HealthConnectTimeoutSeconds' cannot exceed HealthTotalTimeoutSeconds."
    }

    $maximumBackoff = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.MaximumRestartBackoffSeconds -Name 'MaximumRestartBackoffSeconds' -Minimum 1 -Maximum 3600
    $verificationTimeout = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.RecoveryVerificationTimeoutSeconds -Name 'RecoveryVerificationTimeoutSeconds' -Minimum 3 -Maximum 3600
    $requiredHealthy = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.RequiredConsecutiveHealthyChecks -Name 'RequiredConsecutiveHealthyChecks' -Minimum 3 -Maximum 3
    $logMaxBytes = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.LogMaxBytes -Name 'LogMaxBytes' -Minimum 256 -Maximum 104857600
    $logRetention = ConvertTo-HermesGatewayWatchdogInteger `
        -Value $values.LogRetentionCount -Name 'LogRetentionCount' -Minimum 1 -Maximum 20

    if ($verificationTimeout -lt ($pollInterval * $requiredHealthy)) {
        throw "Configuration property 'RecoveryVerificationTimeoutSeconds' must allow three health checks."
    }

    [pscustomobject]@{
        SchemaVersion                    = $schemaVersion
        HealthUrl                       = $healthUri.AbsoluteUri
        HealthUri                       = $healthUri
        HermesExecutablePath            = $hermesPath
        StartupGraceSeconds              = $startupGrace
        PollIntervalSeconds              = $pollInterval
        FailureSettleDelaySeconds        = $settleDelay
        HealthConnectTimeoutSeconds      = $connectTimeout
        HealthTotalTimeoutSeconds        = $totalTimeout
        GatewayStartTimeoutSeconds       = $gatewayStartTimeout
        MaximumRestartBackoffSeconds     = $maximumBackoff
        RecoveryVerificationTimeoutSeconds = $verificationTimeout
        RequiredConsecutiveHealthyChecks = $requiredHealthy
        LogMaxBytes                      = $logMaxBytes
        LogRetentionCount                = $logRetention
    }
}

function Read-HermesGatewayWatchdogConfig {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw 'Watchdog configuration file is missing.'
    }

    try {
        $raw = [System.IO.File]::ReadAllText([System.IO.Path]::GetFullPath($Path))
        $parsed = $raw | ConvertFrom-Json -ErrorAction Stop
    }
    catch {
        throw 'Watchdog configuration file is not valid JSON.'
    }

    if ($null -eq $parsed -or $parsed -is [array]) {
        throw 'Watchdog configuration must be one JSON object.'
    }

    ConvertTo-HermesGatewayWatchdogConfig -InputObject $parsed
}

function Protect-HermesGatewayWatchdogLogText {
    param(
        [AllowNull()][string]$Text
    )

    if ($null -eq $Text) {
        return ''
    }

    $singleLine = ($Text -replace '[\r\n]+', ' ').Trim()
    if ($singleLine.Length -gt 512) {
        $singleLine = $singleLine.Substring(0, 512)
    }

    if ($singleLine -match '(?i)(token|api[ _-]?key|authorization|password|secret|wechat|session content|database[ _-]?(url|connection|string)|postgres(ql)?://|mysql://|mongodb(\+srv)?://|redis://)') {
        return '[REDACTED]'
    }

    [regex]::Replace($singleLine, '(?i)\b[a-z][a-z0-9+.-]*://\S+', '[URL REDACTED]')
}

function Write-HermesGatewayWatchdogLog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$LogPath,
        [Parameter(Mandatory = $true)][string]$EventName,
        [ValidateSet('debug', 'info', 'warning', 'error')][string]$Level = 'info',
        [Parameter(Mandatory = $true)][string]$Message,
        [hashtable]$Data = @{},
        [int]$MaxBytes = 5242880,
        [int]$RetentionCount = 5
    )

    $normalizedEvent = $EventName.ToLowerInvariant()
    $safeEvent = if ($normalizedEvent -match '^[a-z0-9_.-]{1,64}$') { $normalizedEvent } else { 'invalid_event' }
    $record = [ordered]@{
        timestamp = [datetime]::UtcNow.ToString('o')
        level     = $Level
        event     = $safeEvent
        message   = Protect-HermesGatewayWatchdogLogText -Text $Message
    }

    $allowedData = @(
        'health', 'paused', 'exit_code', 'backoff_seconds',
        'consecutive_successes', 'process_id', 'running', 'reason'
    )
    foreach ($key in @($Data.Keys)) {
        if ($allowedData -contains [string]$key) {
            $value = $Data[$key]
            if ($value -is [string]) {
                $record[[string]$key] = Protect-HermesGatewayWatchdogLogText -Text $value
            }
            elseif ($value -is [bool] -or $value -is [byte] -or $value -is [int16] -or
                $value -is [int32] -or $value -is [int64] -or $value -is [decimal] -or
                $value -is [double]) {
                $record[[string]$key] = $value
            }
        }
    }

    $line = $record | ConvertTo-Json -Compress
    $utf8 = New-Object System.Text.UTF8Encoding($false)
    $logMutex = $null
    $acquired = $false

    try {
        $logMutex = New-Object System.Threading.Mutex($false, 'Local\CF_Hermes_Gateway_Watchdog_Log')
        try {
            $acquired = $logMutex.WaitOne(5000)
        }
        catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }
        if (-not $acquired) {
            return
        }

        $directory = Split-Path -Parent ([System.IO.Path]::GetFullPath($LogPath))
        if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
            New-Item -ItemType Directory -Path $directory -Force | Out-Null
        }

        $lineBytes = $utf8.GetByteCount($line + [Environment]::NewLine)
        if ((Test-Path -LiteralPath $LogPath -PathType Leaf) -and
            ((Get-Item -LiteralPath $LogPath).Length + $lineBytes) -gt $MaxBytes) {
            for ($index = $RetentionCount; $index -ge 1; $index--) {
                $current = if ($index -eq 1) { $LogPath } else { "$LogPath.$($index - 1)" }
                $next = "$LogPath.$index"
                if (Test-Path -LiteralPath $current -PathType Leaf) {
                    if ($index -eq $RetentionCount -and (Test-Path -LiteralPath $next -PathType Leaf)) {
                        Remove-Item -LiteralPath $next -Force
                    }
                    Move-Item -LiteralPath $current -Destination $next -Force
                }
            }
        }

        [System.IO.File]::AppendAllText($LogPath, $line + [Environment]::NewLine, $utf8)
    }
    finally {
        if ($acquired -and $null -ne $logMutex) {
            try { $logMutex.ReleaseMutex() } catch {}
        }
        if ($null -ne $logMutex) {
            $logMutex.Dispose()
        }
    }
}

function Get-HermesGatewayWatchdogMutexName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath
    )

    $canonicalPath = [System.IO.Path]::GetFullPath($ConfigPath).TrimEnd('\', '/').ToUpperInvariant()
    try {
        $userIdentity = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    }
    catch {
        $userIdentity = [Environment]::UserName
    }

    $bytes = (New-Object System.Text.UTF8Encoding($false)).GetBytes("$userIdentity|$canonicalPath")
    $sha256 = [System.Security.Cryptography.SHA256]::Create()
    try {
        $hash = [BitConverter]::ToString($sha256.ComputeHash($bytes)).Replace('-', '').Substring(0, 24)
    }
    finally {
        $sha256.Dispose()
    }

    "Global\CF_Hermes_Gateway_Watchdog_$hash"
}

function Enter-HermesGatewayWatchdogMutex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath
    )

    $name = Get-HermesGatewayWatchdogMutexName -ConfigPath $ConfigPath
    $mutex = New-Object System.Threading.Mutex($false, $name)
    $acquired = $false
    try {
        try {
            $acquired = $mutex.WaitOne(0)
        }
        catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }

        [pscustomobject]@{
            Name     = $name
            Mutex    = $mutex
            Acquired = $acquired
        }
    }
    catch {
        $mutex.Dispose()
        throw
    }
}

function Exit-HermesGatewayWatchdogMutex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Lock
    )

    if ($Lock.Acquired -and $null -ne $Lock.Mutex) {
        try { $Lock.Mutex.ReleaseMutex() } catch {}
    }
    if ($null -ne $Lock.Mutex) {
        $Lock.Mutex.Dispose()
    }
}

function Get-HermesGatewayWatchdogStartMutexName {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath
    )

    (Get-HermesGatewayWatchdogMutexName -ConfigPath $ConfigPath) + '_Start'
}

function Enter-HermesGatewayWatchdogStartMutex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [ValidateRange(0, 300000)][int]$TimeoutMilliseconds = 30000
    )

    $name = Get-HermesGatewayWatchdogStartMutexName -ConfigPath $ConfigPath
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
            Name     = $name
            Mutex    = $mutex
            Acquired = $acquired
        }
    }
    catch {
        $mutex.Dispose()
        throw
    }
}

function Exit-HermesGatewayWatchdogStartMutex {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Lock
    )

    Exit-HermesGatewayWatchdogMutex -Lock $Lock
}

function Set-HermesGatewayWatchdogControlFlag {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (Test-Path -LiteralPath $Path) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
            throw "Control flag path is not a file: $Path"
        }
        return $false
    }

    $directory = Split-Path -Parent ([System.IO.Path]::GetFullPath($Path))
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        throw "Control flag directory does not exist: $directory"
    }

    $temporaryPath = '{0}.{1}.{2}.tmp' -f $Path, $PID, [guid]::NewGuid().ToString('N')
    try {
        [System.IO.File]::WriteAllText(
            $temporaryPath,
            [datetime]::UtcNow.ToString('o'),
            (New-Object System.Text.UTF8Encoding($false))
        )
        Move-Item -LiteralPath $temporaryPath -Destination $Path -Force
    }
    finally {
        if (Test-Path -LiteralPath $temporaryPath) {
            Remove-Item -LiteralPath $temporaryPath -Force -ErrorAction SilentlyContinue
        }
    }

    return $true
}

function Test-HermesGatewayProcessIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ProcessName,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$CommandLine
    )

    if ([string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }

    $isHermesExecutable =
        $ProcessName -ieq 'hermes.exe' -and
        $CommandLine -match '(?i)(?:^|\s)gateway\s+(?:run|start)(?:\s|$)'
    $isHermesCliRuntime =
        @('python.exe', 'pythonw.exe', 'hermes.exe') -contains $ProcessName -and
        $CommandLine -match '(?i)(?:^|\s)-m\s+hermes_cli\.main\s+gateway\s+run(?:\s|$)'

    return ($isHermesExecutable -or $isHermesCliRuntime)
}

function Test-HermesGatewayStartCommandIdentity {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ProcessName,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$ExecutablePath,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$CommandLine,
        [Parameter(Mandatory = $true)][string]$HermesExecutablePath
    )

    if ($ProcessName -ine 'hermes.exe' -or
        [string]::IsNullOrWhiteSpace($ExecutablePath) -or
        [string]::IsNullOrWhiteSpace($CommandLine)) {
        return $false
    }

    try {
        $sameExecutable = [string]::Equals(
            [System.IO.Path]::GetFullPath($ExecutablePath),
            [System.IO.Path]::GetFullPath($HermesExecutablePath),
            [System.StringComparison]::OrdinalIgnoreCase
        )
    }
    catch {
        return $false
    }

    return ($sameExecutable -and
        $CommandLine -match '(?i)(?:^|\s)gateway\s+start(?:\s|$)')
}

function Test-HermesGatewayStartCommandRunning {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$HermesExecutablePath
    )

    $processes = Get-CimInstance -ClassName Win32_Process -ErrorAction Stop
    foreach ($process in @($processes)) {
        if (Test-HermesGatewayStartCommandIdentity `
            -ProcessName ([string]$process.Name) `
            -ExecutablePath ([string]$process.ExecutablePath) `
            -CommandLine ([string]$process.CommandLine) `
            -HermesExecutablePath $HermesExecutablePath) {
            return $true
        }
    }

    return $false
}

function Test-HermesGatewayWatchdogInstanceRunning {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ConfigPath
    )

    $name = Get-HermesGatewayWatchdogMutexName -ConfigPath $ConfigPath
    $mutex = $null
    $acquired = $false
    try {
        try {
            $mutex = [System.Threading.Mutex]::OpenExisting($name)
        }
        catch [System.Threading.WaitHandleCannotBeOpenedException] {
            return $false
        }

        try {
            $acquired = $mutex.WaitOne(0)
        }
        catch [System.Threading.AbandonedMutexException] {
            $acquired = $true
        }

        if ($acquired) {
            $mutex.ReleaseMutex()
            return $false
        }

        return $true
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $mutex) {
            $mutex.Dispose()
        }
    }
}

function Test-HermesGatewayHealthPayload {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$StatusCode,
        [Parameter(Mandatory = $true)][AllowEmptyString()][string]$Body
    )

    if ($StatusCode -lt 200 -or $StatusCode -ge 300 -or $Body.Length -gt 65536) {
        return $false
    }
    try {
        $payload = $Body | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $payload -or $payload -is [array]) {
            return $false
        }
        $statusProperty = $payload.PSObject.Properties['status']
        return ($null -ne $statusProperty -and [string]$statusProperty.Value -ceq 'ok')
    }
    catch {
        return $false
    }
}
function Test-HermesGatewayHealth {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Config
    )

    $request = $null
    try {
        $connectMilliseconds = [int]$Config.HealthConnectTimeoutSeconds * 1000
        $totalMilliseconds = [int]$Config.HealthTotalTimeoutSeconds * 1000
        $request = New-Object -ComObject 'WinHttp.WinHttpRequest.5.1'
        $request.SetTimeouts(
            $connectMilliseconds,
            $connectMilliseconds,
            $totalMilliseconds,
            $totalMilliseconds
        )
        $request.SetProxy(1)
        $request.SetAutoLogonPolicy(2)
        $request.Option(6) = $false
        $request.Open('GET', [string]$Config.HealthUri.AbsoluteUri, $true)
        $request.Send()

        if (-not $request.WaitForResponse([int]$Config.HealthTotalTimeoutSeconds)) {
            try { $request.Abort() } catch {}
            return $false
        }

        return Test-HermesGatewayHealthPayload -StatusCode ([int]$request.Status) -Body ([string]$request.ResponseText)
    }
    catch {
        return $false
    }
    finally {
        if ($null -ne $request) {
            try { [void][System.Runtime.InteropServices.Marshal]::FinalReleaseComObject($request) } catch {}
        }
    }
}

function Get-HermesGatewayStartProcessCompletion {
    [CmdletBinding()]
    param()

    if ($null -eq $script:HermesGatewayStartProcess) {
        return $null
    }

    $completion = $null
    try {
        if (-not $script:HermesGatewayStartProcess.HasExited) {
            return $null
        }
        $completion = [pscustomobject]@{
            ExitCode = [int]$script:HermesGatewayStartProcess.ExitCode
            Reason   = 'command_exited'
        }
    }
    catch {
        $completion = [pscustomobject]@{
            ExitCode = -1
            Reason   = 'command_state_failed'
        }
    }
    finally {
        if ($null -ne $completion) {
            try { $script:HermesGatewayStartProcess.Dispose() } catch {}
            $script:HermesGatewayStartProcess = $null
        }
    }

    return $completion
}

function Start-HermesGatewayProcess {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$HermesExecutablePath,
        [Parameter(Mandatory = $true)][string]$ConfigPath,
        [Parameter(Mandatory = $true)][string]$PauseFlagPath,
        [Parameter(Mandatory = $true)][string]$StopFlagPath,
        [Parameter(Mandatory = $true)][int]$CommandTimeoutSeconds,
        [scriptblock]$OnStarted
    )

    if ($null -ne $script:HermesGatewayStartProcess) {
        $priorCompletion = Get-HermesGatewayStartProcessCompletion
        if ($null -eq $priorCompletion) {
            return [pscustomobject]@{
                StartedNew    = $false
                Completed     = $false
                ExitCode      = $null
                Reason        = 'command_still_running'
                AttemptLogged = $false
            }
        }
        return [pscustomobject]@{
            StartedNew    = $false
            Completed     = $true
            ExitCode      = $priorCompletion.ExitCode
            Reason        = 'prior_command_exited'
            AttemptLogged = $false
        }
    }

    $startLock = Enter-HermesGatewayWatchdogStartMutex -ConfigPath $ConfigPath -TimeoutMilliseconds 30000
    if (-not $startLock.Acquired) {
        $startLock.Mutex.Dispose()
        throw 'The Gateway start gate could not be acquired.'
    }

    $process = $null
    try {
        if ((Test-Path -LiteralPath $PauseFlagPath -PathType Leaf) -or
            (Test-Path -LiteralPath $StopFlagPath -PathType Leaf)) {
            throw (New-Object System.OperationCanceledException('Watchdog recovery is paused or stopping.'))
        }
        if (Test-HermesGatewayStartCommandRunning -HermesExecutablePath $HermesExecutablePath) {
            return [pscustomobject]@{
                StartedNew = $false
                Completed  = $false
                ExitCode   = $null
                Reason     = 'existing_command_running'
            }
        }
        $process = Start-Process -FilePath $HermesExecutablePath -ArgumentList @('gateway', 'start') -WindowStyle Hidden -PassThru
    }
    finally {
        Exit-HermesGatewayWatchdogStartMutex -Lock $startLock
    }

    $attemptLogged = $false
    if ($null -ne $OnStarted) {
        try {
            & $OnStarted | Out-Null
            $attemptLogged = $true
        }
        catch {}
    }

    if ($process.WaitForExit($CommandTimeoutSeconds * 1000)) {
        try {
            return [pscustomobject]@{
                StartedNew    = $true
                Completed     = $true
                ExitCode      = [int]$process.ExitCode
                Reason        = 'command_exited'
                AttemptLogged = $attemptLogged
            }
        }
        finally {
            $process.Dispose()
        }
    }

    $script:HermesGatewayStartProcess = $process
    [pscustomobject]@{
        StartedNew    = $true
        Completed     = $false
        ExitCode      = $null
        Reason        = 'command_timeout'
        AttemptLogged = $attemptLogged
    }
}
function New-HermesGatewayWatchdogState {
    [CmdletBinding()]
    param()

    @{
        LastHealth          = $null
        LastPaused          = $null
        IncidentActive      = $false
        RestartFailureCount = 0
        BackoffUntilUtc     = $null
        LastRecoveryUtc     = $null
    }
}

function Invoke-HermesGatewayWatchdogEvent {
    param(
        [Parameter(Mandatory = $true)][scriptblock]$WriteEvent,
        [Parameter(Mandatory = $true)][string]$EventName,
        [Parameter(Mandatory = $true)][string]$Level,
        [Parameter(Mandatory = $true)][string]$Message,
        [hashtable]$Data = @{}
    )

    & $WriteEvent $EventName $Level $Message $Data | Out-Null
}

function Get-HermesGatewayDependencyHealth {
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][scriptblock]$HealthCheck,
        [Parameter(Mandatory = $true)][scriptblock]$WriteEvent
    )

    $healthy = $false
    try {
        $healthy = [bool](& $HealthCheck)
    }
    catch {
        $healthy = $false
    }

    if ($null -eq $State.LastHealth -or [bool]$State.LastHealth -ne $healthy) {
        $State.LastHealth = $healthy
        Invoke-HermesGatewayWatchdogEvent -WriteEvent $WriteEvent `
            -EventName 'health_state_changed' -Level $(if ($healthy) { 'info' } else { 'warning' }) `
            -Message $(if ($healthy) { 'Hermes Gateway health is OK.' } else { 'Hermes Gateway health is not OK.' }) `
            -Data @{ health = $healthy }
    }

    $healthy
}

function Get-HermesGatewayDependencyPaused {
    param(
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][scriptblock]$IsPaused,
        [Parameter(Mandatory = $true)][scriptblock]$WriteEvent
    )

    $paused = [bool](& $IsPaused)
    if ($null -eq $State.LastPaused -or [bool]$State.LastPaused -ne $paused) {
        $State.LastPaused = $paused
        Invoke-HermesGatewayWatchdogEvent -WriteEvent $WriteEvent `
            -EventName 'pause_state_changed' -Level 'info' `
            -Message $(if ($paused) { 'Watchdog recovery is paused.' } else { 'Watchdog recovery is active.' }) `
            -Data @{ paused = $paused }
    }

    $paused
}

function Reset-HermesGatewayWatchdogIncident {
    param(
        [Parameter(Mandatory = $true)][hashtable]$State
    )

    $State.IncidentActive = $false
    $State.RestartFailureCount = 0
    $State.BackoffUntilUtc = $null
}

function Set-HermesGatewayWatchdogBackoff {
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][datetime]$NowUtc
    )

    $State.RestartFailureCount = [int]$State.RestartFailureCount + 1
    $exponent = [math]::Min([int]$State.RestartFailureCount - 1, 20)
    $unbounded = [double]$Config.PollIntervalSeconds * [math]::Pow(2, $exponent)
    $seconds = [int][math]::Min([double]$Config.MaximumRestartBackoffSeconds, [math]::Ceiling($unbounded))
    $State.BackoffUntilUtc = $NowUtc.AddSeconds($seconds)
    $seconds
}

function Invoke-HermesGatewayWatchdogCycle {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)]$Config,
        [Parameter(Mandatory = $true)][hashtable]$State,
        [Parameter(Mandatory = $true)][scriptblock]$IsPaused,
        [Parameter(Mandatory = $true)][scriptblock]$HealthCheck,
        [Parameter(Mandatory = $true)][scriptblock]$StartGateway,
        [Parameter(Mandatory = $true)][scriptblock]$Sleep,
        [Parameter(Mandatory = $true)][scriptblock]$GetUtcNow,
        [Parameter(Mandatory = $true)][scriptblock]$WriteEvent
    )

    $healthy = Get-HermesGatewayDependencyHealth -State $State -HealthCheck $HealthCheck -WriteEvent $WriteEvent
    $paused = Get-HermesGatewayDependencyPaused -State $State -IsPaused $IsPaused -WriteEvent $WriteEvent

    if ($healthy) {
        Reset-HermesGatewayWatchdogIncident -State $State
        return [pscustomobject]@{ Outcome = 'Healthy'; StartedGateway = $false; ExitCode = $null; BackoffSeconds = 0 }
    }

    if ($paused) {
        Reset-HermesGatewayWatchdogIncident -State $State
        return [pscustomobject]@{ Outcome = 'Paused'; StartedGateway = $false; ExitCode = $null; BackoffSeconds = 0 }
    }

    $now = ([datetime](& $GetUtcNow)).ToUniversalTime()
    if ($null -ne $State.BackoffUntilUtc -and $now -lt ([datetime]$State.BackoffUntilUtc)) {
        return [pscustomobject]@{ Outcome = 'Backoff'; StartedGateway = $false; ExitCode = $null; BackoffSeconds = 0 }
    }

    if (-not [bool]$State.IncidentActive) {
        $State.IncidentActive = $true
        Invoke-HermesGatewayWatchdogEvent -WriteEvent $WriteEvent `
            -EventName 'failure_settle_started' -Level 'warning' `
            -Message 'Initial health failure entered the settle delay.'
        if ($Config.FailureSettleDelaySeconds -gt 0) {
            & $Sleep ([int]$Config.FailureSettleDelaySeconds) | Out-Null
        }

        $healthy = Get-HermesGatewayDependencyHealth -State $State -HealthCheck $HealthCheck -WriteEvent $WriteEvent
        $paused = Get-HermesGatewayDependencyPaused -State $State -IsPaused $IsPaused -WriteEvent $WriteEvent
        if ($healthy) {
            Reset-HermesGatewayWatchdogIncident -State $State
            return [pscustomobject]@{ Outcome = 'SettledHealthy'; StartedGateway = $false; ExitCode = $null; BackoffSeconds = 0 }
        }
        if ($paused) {
            Reset-HermesGatewayWatchdogIncident -State $State
            return [pscustomobject]@{ Outcome = 'Paused'; StartedGateway = $false; ExitCode = $null; BackoffSeconds = 0 }
        }
    }

    # Close the maintenance race as tightly as possible before creating the process.
    if (Get-HermesGatewayDependencyPaused -State $State -IsPaused $IsPaused -WriteEvent $WriteEvent) {
        Reset-HermesGatewayWatchdogIncident -State $State
        return [pscustomobject]@{ Outcome = 'Paused'; StartedGateway = $false; ExitCode = $null; BackoffSeconds = 0 }
    }

    $startWasNew = $true
    $startCompleted = $true
    $exitCode = -1
    $startReason = 'command_exited'
    $attemptLogged = $false
    try {
        $rawStartResult = & $StartGateway
        $completedProperty = $rawStartResult.PSObject.Properties['Completed']
        if ($null -ne $completedProperty) {
            $startWasNew = [bool]$rawStartResult.PSObject.Properties['StartedNew'].Value
            $startCompleted = [bool]$completedProperty.Value
            $exitProperty = $rawStartResult.PSObject.Properties['ExitCode']
            $exitCode = if ($null -ne $exitProperty -and $null -ne $exitProperty.Value) { [int]$exitProperty.Value } else { $null }
            $reasonProperty = $rawStartResult.PSObject.Properties['Reason']
            if ($null -ne $reasonProperty) {
                $startReason = [string]$reasonProperty.Value
            }
            $attemptProperty = $rawStartResult.PSObject.Properties['AttemptLogged']
            if ($null -ne $attemptProperty) {
                $attemptLogged = [bool]$attemptProperty.Value
            }
        }
        else {
            $exitCode = [int]$rawStartResult
        }
    }
    catch [System.OperationCanceledException] {
        [void](Get-HermesGatewayDependencyPaused -State $State -IsPaused $IsPaused -WriteEvent $WriteEvent)
        Reset-HermesGatewayWatchdogIncident -State $State
        return [pscustomobject]@{ Outcome = 'Paused'; StartedGateway = $false; ExitCode = $null; BackoffSeconds = 0 }
    }
    catch {
        $startWasNew = $false
        $startCompleted = $false
        $exitCode = $null
        $startReason = 'launch_failed'
    }

    if ($startWasNew -and -not $attemptLogged) {
        Invoke-HermesGatewayWatchdogEvent -WriteEvent $WriteEvent -EventName 'gateway_start_attempt' -Level 'warning' -Message 'Started a Hermes Gateway recovery command.'
    }
    if ($startCompleted) {
        Invoke-HermesGatewayWatchdogEvent -WriteEvent $WriteEvent -EventName 'gateway_start_exit' -Level $(if ($exitCode -eq 0) { 'info' } else { 'warning' }) -Message 'Hermes Gateway start command exited.' -Data @{ exit_code = $exitCode }
    }
    elseif ($startReason -eq 'launch_failed') {
        Invoke-HermesGatewayWatchdogEvent -WriteEvent $WriteEvent -EventName 'gateway_start_failed' -Level 'error' -Message 'Hermes Gateway start command could not be launched.' -Data @{ reason = $startReason }
    }
    else {
        Invoke-HermesGatewayWatchdogEvent -WriteEvent $WriteEvent -EventName 'gateway_start_in_progress' -Level 'warning' -Message 'Hermes Gateway start command is still in progress.' -Data @{ reason = $startReason }
    }
    $consecutiveHealthy = 0
    $maximumChecks = [int][math]::Floor(
        [double]$Config.RecoveryVerificationTimeoutSeconds / [double]$Config.PollIntervalSeconds
    )
    $maximumChecks = [math]::Max($maximumChecks, [int]$Config.RequiredConsecutiveHealthyChecks)

    for ($check = 1; $check -le $maximumChecks; $check++) {
        & $Sleep ([int]$Config.PollIntervalSeconds) | Out-Null
        $healthy = Get-HermesGatewayDependencyHealth -State $State -HealthCheck $HealthCheck -WriteEvent $WriteEvent
        $paused = Get-HermesGatewayDependencyPaused -State $State -IsPaused $IsPaused -WriteEvent $WriteEvent

        if ($healthy) {
            $consecutiveHealthy++
        }
        else {
            $consecutiveHealthy = 0
        }

        if ($consecutiveHealthy -ge [int]$Config.RequiredConsecutiveHealthyChecks) {
            $State.LastRecoveryUtc = ([datetime](& $GetUtcNow)).ToUniversalTime()
            Reset-HermesGatewayWatchdogIncident -State $State
            Invoke-HermesGatewayWatchdogEvent -WriteEvent $WriteEvent `
                -EventName 'recovery_succeeded' -Level 'info' `
                -Message 'Hermes Gateway recovery succeeded.' `
                -Data @{ consecutive_successes = $consecutiveHealthy }
            return [pscustomobject]@{ Outcome = 'Recovered'; StartedGateway = $startWasNew; ExitCode = $exitCode; BackoffSeconds = 0 }
        }

        if ($paused) {
            Reset-HermesGatewayWatchdogIncident -State $State
            return [pscustomobject]@{ Outcome = 'PausedAfterStart'; StartedGateway = $startWasNew; ExitCode = $exitCode; BackoffSeconds = 0 }
        }
    }

    $now = ([datetime](& $GetUtcNow)).ToUniversalTime()
    $backoffSeconds = Set-HermesGatewayWatchdogBackoff -Config $Config -State $State -NowUtc $now
    Invoke-HermesGatewayWatchdogEvent -WriteEvent $WriteEvent `
        -EventName 'recovery_failed' -Level 'error' `
        -Message 'Hermes Gateway recovery did not become healthy.' `
        -Data $(if ($startCompleted) { @{ exit_code = $exitCode; backoff_seconds = $backoffSeconds } } else { @{ backoff_seconds = $backoffSeconds; reason = $startReason } })

    [pscustomobject]@{
        Outcome        = 'RecoveryFailed'
        StartedGateway = $startWasNew
        ExitCode       = $exitCode
        BackoffSeconds = $backoffSeconds
    }
}

function Read-HermesGatewayWatchdogStateFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path
    )

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        return $null
    }
    try {
        $state = [System.IO.File]::ReadAllText($Path) | ConvertFrom-Json -ErrorAction Stop
        if ($null -eq $state -or $state -is [array]) {
            return $null
        }
        $schemaProperty = $state.PSObject.Properties['SchemaVersion']
        if ($null -eq $schemaProperty -or [int]$schemaProperty.Value -ne 1) {
            return $null
        }
        return $state
    }
    catch {
        return $null
    }
}

function Write-HermesGatewayWatchdogStateFile {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [AllowNull()][Nullable[datetime]]$LastRecoveryUtc,
        [Parameter(Mandatory = $true)][bool]$Running
    )

    $record = [ordered]@{
        SchemaVersion   = 1
        ProcessId       = $PID
        ProcessStartUtc = $script:HermesGatewayWatchdogProcessStartUtc.ToString('o')
        InstanceId      = $script:HermesGatewayWatchdogInstanceId
        Running         = $Running
        UpdatedUtc      = [datetime]::UtcNow.ToString('o')
        LastRecoveryUtc = if ($null -ne $LastRecoveryUtc) { ([datetime]$LastRecoveryUtc).ToUniversalTime().ToString('o') } else { $null }
    }
    $json = $record | ConvertTo-Json
    $directory = Split-Path -Parent ([System.IO.Path]::GetFullPath($Path))
    if (-not (Test-Path -LiteralPath $directory -PathType Container)) {
        New-Item -ItemType Directory -Path $directory -Force | Out-Null
    }
    $temporary = "$Path.$PID.tmp"
    [System.IO.File]::WriteAllText($temporary, $json, (New-Object System.Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $temporary -Destination $Path -Force
}

function Wait-HermesGatewayWatchdogDelay {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][int]$Seconds,
        [Parameter(Mandatory = $true)][string]$StopFlagPath
    )

    $remainingMilliseconds = $Seconds * 1000
    while ($remainingMilliseconds -gt 0) {
        if (Test-Path -LiteralPath $StopFlagPath -PathType Leaf) {
            return $false
        }
        $slice = [math]::Min(1000, $remainingMilliseconds)
        Start-Sleep -Milliseconds $slice
        $remainingMilliseconds -= $slice
    }
    -not (Test-Path -LiteralPath $StopFlagPath -PathType Leaf)
}

function Invoke-HermesGatewayWatchdog {
    [CmdletBinding()]
    param(
        [Parameter(Mandatory = $true)][string]$ConfigurationPath,
        [switch]$OneCycle
    )

    $fullConfigPath = [System.IO.Path]::GetFullPath($ConfigurationPath)
    $paths = Get-HermesGatewayWatchdogPaths -RootPath (Split-Path -Parent $fullConfigPath)
    $config = Read-HermesGatewayWatchdogConfig -Path $fullConfigPath
    $lock = Enter-HermesGatewayWatchdogMutex -ConfigPath $fullConfigPath
    if (-not $lock.Acquired) {
        Write-HermesGatewayWatchdogLog -LogPath $paths.LogPath `
            -EventName 'duplicate_instance_ignored' -Level 'info' `
            -Message 'Another Watchdog instance already owns this configuration.' `
            -MaxBytes $config.LogMaxBytes -RetentionCount $config.LogRetentionCount
        $lock.Mutex.Dispose()
        return
    }

    $state = New-HermesGatewayWatchdogState
    $previousState = Read-HermesGatewayWatchdogStateFile -Path $paths.StatePath
    if ($null -ne $previousState) {
        $recoveryProperty = $previousState.PSObject.Properties['LastRecoveryUtc']
        if ($null -ne $recoveryProperty -and $null -ne $recoveryProperty.Value) {
            try { $state.LastRecoveryUtc = ([datetime]$recoveryProperty.Value).ToUniversalTime() } catch {}
        }
    }

    $writeEvent = {
        param($eventName, $level, $message, $data)
        Write-HermesGatewayWatchdogLog -LogPath $paths.LogPath -EventName $eventName `
            -Level $level -Message $message -Data $data `
            -MaxBytes $config.LogMaxBytes -RetentionCount $config.LogRetentionCount
        if ($eventName -eq 'recovery_succeeded') {
            Write-HermesGatewayWatchdogStateFile -Path $paths.StatePath `
                -LastRecoveryUtc $state.LastRecoveryUtc -Running $true
        }
    }

    $writePendingStartCompletion = {
        $completion = Get-HermesGatewayStartProcessCompletion
        if ($null -ne $completion) {
            Invoke-HermesGatewayWatchdogEvent -WriteEvent $writeEvent `
                -EventName 'gateway_start_exit' `
                -Level $(if ($completion.ExitCode -eq 0) { 'info' } else { 'warning' }) `
                -Message 'Hermes Gateway start command exited.' `
                -Data @{ exit_code = $completion.ExitCode }
        }
    }

    try {
        Write-HermesGatewayWatchdogLog -LogPath $paths.LogPath `
            -EventName 'watchdog_started' -Level 'info' -Message 'Hermes Gateway Watchdog started.' `
            -Data @{ process_id = $PID; running = $true } `
            -MaxBytes $config.LogMaxBytes -RetentionCount $config.LogRetentionCount
        Write-HermesGatewayWatchdogStateFile -Path $paths.StatePath `
            -LastRecoveryUtc $state.LastRecoveryUtc -Running $true

        if (-not (Wait-HermesGatewayWatchdogDelay `
            -Seconds $config.StartupGraceSeconds -StopFlagPath $paths.StopFlagPath)) {
            return
        }

        while ($true) {
            & $writePendingStartCompletion | Out-Null
            if (Test-Path -LiteralPath $paths.StopFlagPath -PathType Leaf) {
                break
            }

            $sleepDependency = {
                param($seconds)
                if (-not (Wait-HermesGatewayWatchdogDelay -Seconds $seconds -StopFlagPath $paths.StopFlagPath)) {
                    throw (New-Object System.OperationCanceledException('Watchdog stop requested.'))
                }
                & $writePendingStartCompletion | Out-Null
            }
            try {
                Invoke-HermesGatewayWatchdogCycle -Config $config -State $state `
                    -IsPaused { Test-Path -LiteralPath $paths.PauseFlagPath -PathType Leaf } `
                    -HealthCheck { Test-HermesGatewayHealth -Config $config } `
                    -StartGateway {
                        if (Test-Path -LiteralPath $paths.PauseFlagPath -PathType Leaf) {
                            throw (New-Object System.OperationCanceledException('Watchdog recovery paused.'))
                        }
                        Start-HermesGatewayProcess -HermesExecutablePath $config.HermesExecutablePath `
                            -ConfigPath $fullConfigPath -PauseFlagPath $paths.PauseFlagPath `
                            -StopFlagPath $paths.StopFlagPath `
                            -CommandTimeoutSeconds $config.GatewayStartTimeoutSeconds `
                            -OnStarted {
                                Invoke-HermesGatewayWatchdogEvent -WriteEvent $writeEvent `
                                    -EventName 'gateway_start_attempt' -Level 'warning' `
                                    -Message 'Started a Hermes Gateway recovery command.'
                            }
                    } `
                    -Sleep $sleepDependency -GetUtcNow { [datetime]::UtcNow } -WriteEvent $writeEvent | Out-Null
            }
            catch [System.OperationCanceledException] {
                if (Test-Path -LiteralPath $paths.StopFlagPath -PathType Leaf) {
                    break
                }
            }

            if ($OneCycle) {
                break
            }
            if (-not (Wait-HermesGatewayWatchdogDelay `
                -Seconds $config.PollIntervalSeconds -StopFlagPath $paths.StopFlagPath)) {
                break
            }
        }
    }
    finally {
        try { & $writePendingStartCompletion | Out-Null } catch {}
        Write-HermesGatewayWatchdogLog -LogPath $paths.LogPath `
            -EventName 'watchdog_stopped' -Level 'info' -Message 'Hermes Gateway Watchdog stopped.' `
            -Data @{ process_id = $PID; running = $false } `
            -MaxBytes $config.LogMaxBytes -RetentionCount $config.LogRetentionCount
        try {
            Write-HermesGatewayWatchdogStateFile -Path $paths.StatePath `
                -LastRecoveryUtc $state.LastRecoveryUtc -Running $false
        }
        catch {}
        if ($null -ne $script:HermesGatewayStartProcess) {
            try { $script:HermesGatewayStartProcess.Dispose() } catch {}
            $script:HermesGatewayStartProcess = $null
        }
        Exit-HermesGatewayWatchdogMutex -Lock $lock
    }
}

if ($MyInvocation.InvocationName -ne '.') {
    try {
        if ([string]::IsNullOrWhiteSpace($ConfigPath)) {
            $ConfigPath = (Get-HermesGatewayWatchdogPaths).ConfigPath
        }
        Invoke-HermesGatewayWatchdog -ConfigurationPath $ConfigPath -OneCycle:$RunOnce
        exit 0
    }
    catch {
        try {
            $failurePaths = Get-HermesGatewayWatchdogPaths
            Write-HermesGatewayWatchdogLog -LogPath $failurePaths.LogPath `
                -EventName 'watchdog_start_failed' -Level 'error' `
                -Message 'Watchdog configuration or startup validation failed.' -Data @{ reason = 'safe_failure' }
        }
        catch {}
        Write-Error 'Hermes Gateway Watchdog could not start safely. Review the local Watchdog log.'
        exit 1
    }
}
