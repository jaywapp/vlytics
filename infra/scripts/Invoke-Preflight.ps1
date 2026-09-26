[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$EnvironmentFile,
    [Parameter(Mandatory = $true)][string]$OperationalConfig,
    [Parameter(Mandatory = $true)][string]$DryRunEvidence,
    [string]$ComposeFile
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "DeploymentPaths.ps1")

if ([string]::IsNullOrWhiteSpace($ComposeFile)) { $ComposeFile = Join-Path $PSScriptRoot "..\compose.production.yaml" }

function Read-EnvironmentFile {
    param([string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "Environment file does not exist."
    }
    $values = @{}
    foreach ($line in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $trimmed = $line.Trim()
        if (-not $trimmed -or $trimmed.StartsWith("#")) {
            continue
        }
        $separator = $trimmed.IndexOf("=")
        if ($separator -lt 1) {
            throw "Environment file contains an invalid assignment."
        }
        $name = $trimmed.Substring(0, $separator).Trim()
        $value = $trimmed.Substring($separator + 1).Trim()
        if ($values.ContainsKey($name)) {
            throw "Environment file contains a duplicate key: $name"
        }
        $values[$name] = $value
    }
    return $values
}

function Assert-ClockSynchronized {
    if ($env:OS -eq "Windows_NT") {
        $service = Get-Service -Name W32Time -ErrorAction SilentlyContinue
        if ($null -eq $service -or $service.Status -ne "Running") {
            throw "Windows Time service is not running."
        }
        $clockStatus = & w32tm /query /status 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "Windows time synchronization status could not be read."
        }
        $joined = $clockStatus -join "`n"
        if ($joined -match '(?i)(local cmos clock|free-running system clock)') {
            throw "Host clock is using an unsynchronized local source."
        }
        return
    }

    $timedatectl = Get-Command timedatectl -ErrorAction SilentlyContinue
    if ($null -eq $timedatectl) {
        throw "No supported clock synchronization status command was found."
    }
    $synchronized = & $timedatectl.Source show --property=NTPSynchronized --value 2>&1
    if ($LASTEXITCODE -ne 0 -or ($synchronized -join "").Trim() -ne "yes") {
        throw "Host clock is not NTP synchronized."
    }
}

$EnvironmentFile = Resolve-DeploymentFile $EnvironmentFile (Get-Location).ProviderPath
$ComposeFile = Resolve-DeploymentFile $ComposeFile (Get-Location).ProviderPath
$composeDirectory = Split-Path -Parent $ComposeFile
$values = Read-EnvironmentFile $EnvironmentFile
$required = @(
    "VLYTICS_BACKEND_IMAGE", "VLYTICS_FRONTEND_IMAGE", "VLYTICS_NODE_BUILD_IMAGE", "VLYTICS_NGINX_RUNTIME_IMAGE", "VLYTICS_POSTGRES_IMAGE", "VLYTICS_PYTHON_BUILD_IMAGE", "VLYTICS_UV_BUILD_IMAGE", "MIGRATOR_DATABASE_PASSWORD", "COLLECTOR_DATABASE_PASSWORD",
    "ENGINE_DATABASE_PASSWORD", "MARKET_INGEST_DATABASE_PASSWORD", "READ_API_DATABASE_PASSWORD",
    "MIGRATOR_DATABASE_URL", "COLLECTOR_DATABASE_URL", "ENGINE_DATABASE_URL",
    "MARKET_INGEST_DATABASE_URL", "READ_API_DATABASE_URL", "VLYTICS_OPERATOR_AUTH_SECRET",
    "VLYTICS_READONLY_AUTH_SECRET", "VLYTICS_BACKUP_CREDENTIAL", "VLYTICS_ALERT_DESTINATION",
    "VLYTICS_OPENAI_API_KEY", "VLYTICS_ANTHROPIC_API_KEY", "VLYTICS_GOOGLE_API_KEY",
    "VLYTICS_LIVE_DRY_RUN_EVIDENCE_FILE", "VLYTICS_OPERATIONAL_CONFIG_FILE",
    "VLYTICS_PROVIDER_REGISTRY_FILE", "VLYTICS_SOURCE_REGISTRY_FILE"
)
foreach ($name in $required) {
    if (-not $values.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($values[$name])) {
        throw "Required deployment value is missing: $name"
    }
    if ($values[$name] -match '^__(REQUIRED|REQUIRED_BY_OP_[0-9]+)__$') {
        throw "Deployment value still contains a placeholder: $name"
    }
}

foreach ($imageKey in @("VLYTICS_BACKEND_IMAGE", "VLYTICS_FRONTEND_IMAGE", "VLYTICS_NODE_BUILD_IMAGE", "VLYTICS_NGINX_RUNTIME_IMAGE", "VLYTICS_POSTGRES_IMAGE", "VLYTICS_PYTHON_BUILD_IMAGE", "VLYTICS_UV_BUILD_IMAGE")) {
    if ($values[$imageKey] -notmatch '@sha256:[a-f0-9]{64}$') {
        throw "$imageKey must be pinned by sha256 digest."
    }
}
$OperationalConfig = Assert-DeploymentFileBinding -ProvidedPath $OperationalConfig -ConfiguredPath $values["VLYTICS_OPERATIONAL_CONFIG_FILE"] -ComposeDirectory $composeDirectory -Name "OperationalConfig"
$DryRunEvidence = Assert-DeploymentFileBinding -ProvidedPath $DryRunEvidence -ConfiguredPath $values["VLYTICS_LIVE_DRY_RUN_EVIDENCE_FILE"] -ComposeDirectory $composeDirectory -Name "DryRunEvidence"
$values["VLYTICS_OPERATIONAL_CONFIG_FILE"] = $OperationalConfig
$values["VLYTICS_LIVE_DRY_RUN_EVIDENCE_FILE"] = $DryRunEvidence
$registryBindings = @(
    @{ Key = "VLYTICS_PROVIDER_REGISTRY_FILE"; Target = "/run/vlytics/variants.toml" },
    @{ Key = "VLYTICS_SOURCE_REGISTRY_FILE"; Target = "/run/vlytics/source.toml" }
)
$registryHashes = @{}
foreach ($binding in $registryBindings) {
    $values[$binding.Key] = Resolve-DeploymentFile $values[$binding.Key] $composeDirectory
    $registryHashes[$binding.Key] = (Get-FileHash -LiteralPath $values[$binding.Key] -Algorithm SHA256).Hash
}
$configSha256 = (Get-FileHash -LiteralPath $OperationalConfig -Algorithm SHA256).Hash.ToLowerInvariant()
$evidenceSha256 = (Get-FileHash -LiteralPath $DryRunEvidence -Algorithm SHA256).Hash.ToLowerInvariant()
$configText = Get-Content -LiteralPath $OperationalConfig -Raw -Encoding UTF8
if ($configText -match '__(REQUIRED|REQUIRED_BY_OP_[0-9]+)__|=\s*"unconfigured"') {
    throw "Operational config still contains an activation placeholder."
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($null -eq $docker) {
    throw "Docker CLI is required for activation preflight. Package-only validation can use Test-DeploymentPackage.ps1."
}

$originalEnvironment = @{}
try {
    foreach ($entry in $values.GetEnumerator()) {
        $originalEnvironment[$entry.Key] = [Environment]::GetEnvironmentVariable($entry.Key)
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value)
    }
    if (-not $originalEnvironment.ContainsKey("VLYTICS_DATABASE_URL")) {
        $originalEnvironment["VLYTICS_DATABASE_URL"] = [Environment]::GetEnvironmentVariable("VLYTICS_DATABASE_URL")
    }
    [Environment]::SetEnvironmentVariable("VLYTICS_DATABASE_URL", $values["ENGINE_DATABASE_URL"])

    $repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
    $schemaPath = Join-Path $repositoryRoot "contracts\config.schema.json"
    $pythonProgram = @"
import os
import sys
from pathlib import Path
from vlytics.config import Settings, load_operational_config
from vlytics.ops.scheduler import load_live_dry_run_evidence
from vlytics.engine.providers import load_variant_registry, build_live_provider_plan
from vlytics.mirror.source_registry import load_kovo_parser
from vlytics.mirror.kovo import scopes_from_operational_config

config_path = Path(sys.argv[1])
schema_path = Path(sys.argv[2])
evidence_path = Path(sys.argv[3])
settings = Settings(
    database_url=os.environ["ENGINE_DATABASE_URL"],
    operational_config_path=config_path,
    operational_schema_path=schema_path,
    live_dry_run_evidence_path=evidence_path,
    provider_registry_path=Path(os.environ["VLYTICS_PROVIDER_REGISTRY_FILE"]),
    source_registry_path=Path(os.environ["VLYTICS_SOURCE_REGISTRY_FILE"]),
)
config = load_operational_config(settings, environ=os.environ)
load_live_dry_run_evidence(config, settings.live_dry_run_evidence_path)
if config.live_operations_enabled:
    build_live_provider_plan(config, load_variant_registry(settings.provider_registry_path), environ=os.environ)
if config.values["source"]["bulk_collection_enabled"]:
    load_kovo_parser(settings.source_registry_path, scopes_from_operational_config(config))
"@
    Push-Location (Join-Path $repositoryRoot "backend")
    try {
        & uv run --frozen python -c $pythonProgram ([System.IO.Path]::GetFullPath($OperationalConfig)) $schemaPath ([System.IO.Path]::GetFullPath($DryRunEvidence))
        if ($LASTEXITCODE -ne 0) {
            throw "Operational config or exact OP-004 dry-run evidence validation failed."
        }
    }
    finally {
        Pop-Location
    }

    foreach ($imageKey in @("VLYTICS_BACKEND_IMAGE", "VLYTICS_FRONTEND_IMAGE")) {
        & $docker.Source image inspect $values[$imageKey] --format '{{.Id}}' | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "The pinned deployment image is not available to this host: $imageKey"
        }
    }
    $renderedJson = & $docker.Source compose --env-file $EnvironmentFile --file $ComposeFile config --format json
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose config validation failed."
    }
    $rendered = ($renderedJson -join [Environment]::NewLine) | ConvertFrom-Json
    foreach ($serviceName in @("api", "worker", "migrate")) {
        $service = $rendered.services.PSObject.Properties[$serviceName].Value
        foreach ($binding in @(
            @{ Target = "/run/vlytics/operational.toml"; Source = $OperationalConfig },
            @{ Target = "/run/vlytics/live-dry-run-evidence.json"; Source = $DryRunEvidence }
        )) {
            $mounts = @($service.volumes | Where-Object { $_.target -eq $binding.Target })
            if ($mounts.Count -ne 1 -or $mounts[0].type -ne "bind" -or -not $mounts[0].read_only) {
                throw "Validated deployment file must be mounted read-only by $serviceName."
            }
            Assert-DeploymentFileBinding -ProvidedPath $binding.Source -ConfiguredPath $mounts[0].source -ComposeDirectory $composeDirectory -Name $serviceName | Out-Null
        }
    }
    foreach ($binding in $registryBindings) {
        $mounts = @($rendered.services.worker.volumes | Where-Object { $_.target -eq $binding.Target })
        if ($mounts.Count -ne 1 -or -not $mounts[0].read_only) { throw "Worker registry must be mounted read-only." }
        Assert-DeploymentFileBinding -ProvidedPath $values[$binding.Key] -ConfiguredPath $mounts[0].source -ComposeDirectory $composeDirectory -Name $binding.Key | Out-Null
        if ((Get-FileHash -LiteralPath $values[$binding.Key] -Algorithm SHA256).Hash -ne $registryHashes[$binding.Key]) { throw "Registry changed during preflight." }
    }
    $rendered = $null
    $renderedJson = $null
    if ((Get-FileHash -LiteralPath $OperationalConfig -Algorithm SHA256).Hash.ToLowerInvariant() -ne $configSha256 -or
        (Get-FileHash -LiteralPath $DryRunEvidence -Algorithm SHA256).Hash.ToLowerInvariant() -ne $evidenceSha256) {
        throw "Deployment files changed during preflight."
    }

    Assert-ClockSynchronized

    [pscustomobject]@{
        Result = "passed"
        PlaceholderGate = "passed"
        OperationalConfig = "passed"
        OperationalConfigSha256 = $configSha256
        DryRunEvidenceSha256 = $evidenceSha256
        ExactComposeMounts = "passed"
        ExactDryRunEvidence = "passed"
        BackendAndFrontendImages = "available"
        ComposeConfig = "passed"
        ClockSynchronization = "passed"
    }
}
finally {
    foreach ($entry in $originalEnvironment.GetEnumerator()) {
        [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value)
    }
}