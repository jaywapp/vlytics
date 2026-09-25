[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$EnvironmentFile,
    [Parameter(Mandatory = $true)][string]$OperationalConfig,
    [Parameter(Mandatory = $true)][string]$DryRunEvidence,
    [string]$ComposeFile
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

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

$values = Read-EnvironmentFile $EnvironmentFile
$required = @(
    "VLYTICS_BACKEND_IMAGE", "VLYTICS_FRONTEND_IMAGE", "VLYTICS_NODE_BUILD_IMAGE", "VLYTICS_NGINX_RUNTIME_IMAGE", "MIGRATOR_DATABASE_PASSWORD", "COLLECTOR_DATABASE_PASSWORD",
    "ENGINE_DATABASE_PASSWORD", "MARKET_INGEST_DATABASE_PASSWORD", "READ_API_DATABASE_PASSWORD",
    "MIGRATOR_DATABASE_URL", "COLLECTOR_DATABASE_URL", "ENGINE_DATABASE_URL",
    "MARKET_INGEST_DATABASE_URL", "READ_API_DATABASE_URL", "VLYTICS_OPERATOR_AUTH_SECRET",
    "VLYTICS_READONLY_AUTH_SECRET", "VLYTICS_BACKUP_CREDENTIAL", "VLYTICS_ALERT_DESTINATION",
    "VLYTICS_OPENAI_API_KEY", "VLYTICS_ANTHROPIC_API_KEY", "VLYTICS_GOOGLE_API_KEY",
    "VLYTICS_LIVE_DRY_RUN_EVIDENCE_FILE"
)
foreach ($name in $required) {
    if (-not $values.ContainsKey($name) -or [string]::IsNullOrWhiteSpace($values[$name])) {
        throw "Required deployment value is missing: $name"
    }
    if ($values[$name] -match '^__(REQUIRED|REQUIRED_BY_OP_[0-9]+)__$') {
        throw "Deployment value still contains a placeholder: $name"
    }
}

foreach ($imageKey in @("VLYTICS_BACKEND_IMAGE", "VLYTICS_FRONTEND_IMAGE", "VLYTICS_NODE_BUILD_IMAGE", "VLYTICS_NGINX_RUNTIME_IMAGE")) {
    if ($values[$imageKey] -notmatch '@sha256:[a-f0-9]{64}$') {
        throw "$imageKey must be pinned by sha256 digest."
    }
}
if (-not (Test-Path -LiteralPath $OperationalConfig -PathType Leaf)) {
    throw "Operational config does not exist."
}
if (-not (Test-Path -LiteralPath $DryRunEvidence -PathType Leaf)) {
    throw "OP-004 live dry-run evidence does not exist."
}
$resolvedEvidence = [System.IO.Path]::GetFullPath($DryRunEvidence)
$configuredEvidence = [System.IO.Path]::GetFullPath($values["VLYTICS_LIVE_DRY_RUN_EVIDENCE_FILE"])
if ($resolvedEvidence -ne $configuredEvidence) {
    throw "DryRunEvidence must match VLYTICS_LIVE_DRY_RUN_EVIDENCE_FILE."
}
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

config_path = Path(sys.argv[1])
schema_path = Path(sys.argv[2])
evidence_path = Path(sys.argv[3])
settings = Settings(
    database_url=os.environ["ENGINE_DATABASE_URL"],
    operational_config_path=config_path,
    operational_schema_path=schema_path,
    live_dry_run_evidence_path=evidence_path,
)
config = load_operational_config(settings, environ=os.environ, component="worker")
load_live_dry_run_evidence(config, settings.live_dry_run_evidence_path)
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
    & $docker.Source compose --env-file $EnvironmentFile --file $ComposeFile config --quiet
    if ($LASTEXITCODE -ne 0) {
        throw "docker compose config validation failed."
    }

    Assert-ClockSynchronized

    [pscustomobject]@{
        Result = "passed"
        PlaceholderGate = "passed"
        OperationalConfig = "passed"
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