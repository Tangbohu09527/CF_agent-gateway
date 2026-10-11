[CmdletBinding()]
param(
    [ValidateSet('init-request','bind-tls','plan','export-public','csr-plan','prepare-csr','stop','apply','start','quiesce','rollback','start-legacy')]
    [string]$Phase = 'plan',
    [string]$Request,
    [string]$DeploymentId,
    [string]$HermesHome = (Join-Path $env:LOCALAPPDATA 'hermes'),
    [string]$HermesOrigin,
    [string]$GatewayCaFile,
    [string]$HermesCaFile,
    [string]$TlsCertFile,
    [string]$TlsKeyFile,
    [string]$Python,
    [switch]$AuthorizeMaintenance,
    [string]$ExpectedPlanSha256,
    [string]$PeerReceipt
)
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
if (-not $Python) {
    $factsPath = $null
    if ($Request) {
        $requestData = Get-Content -LiteralPath $Request -Raw | ConvertFrom-Json
        $factsPath = $requestData.facts
        $HermesHome = $requestData.hermes_home
    }
    if (-not $factsPath) {
        $factsPath = Join-Path $HermesHome 'installs/902a4fe5d10abbac/facts.json'
    }
    $facts = Get-Content -LiteralPath $factsPath -Raw | ConvertFrom-Json
    $Python = Join-Path $facts.packages.venv.environment 'Scripts/python.exe'
}
$arguments = @('-m','cf_agent_gateway.hermes.return_bridge.site_windows',$Phase)
if ($Request) { $arguments += @('--request',(Resolve-Path -LiteralPath $Request).Path) }
if ($DeploymentId) { $arguments += @('--deployment-id',$DeploymentId) }
if ($Phase -eq 'init-request') { $arguments += @('--hermes-home',$HermesHome) }
if ($HermesOrigin) { $arguments += @('--hermes-origin',$HermesOrigin) }
if ($GatewayCaFile) { $arguments += @('--gateway-ca-file',(Resolve-Path -LiteralPath $GatewayCaFile).Path) }
if ($HermesCaFile) { $arguments += @('--hermes-ca-file',(Resolve-Path -LiteralPath $HermesCaFile).Path) }
if ($TlsCertFile) { $arguments += @('--tls-cert-file',(Resolve-Path -LiteralPath $TlsCertFile).Path) }
if ($TlsKeyFile) { $arguments += @('--tls-key-file',(Resolve-Path -LiteralPath $TlsKeyFile).Path) }
if ($AuthorizeMaintenance) { $arguments += '--authorize-maintenance' }
if ($ExpectedPlanSha256) { $arguments += @('--expected-plan-sha256',$ExpectedPlanSha256) }
if ($PeerReceipt) { $arguments += @('--peer-receipt',(Resolve-Path -LiteralPath $PeerReceipt).Path) }
$priorPath = $env:PYTHONPATH
try {
    $env:PYTHONPATH = (Join-Path $root 'src')
    & $Python @arguments
    exit $LASTEXITCODE
} finally {
    $env:PYTHONPATH = $priorPath
}
