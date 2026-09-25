[CmdletBinding()]
param(
    [string]$ComposeFile,
    [string]$EnvironmentExample,
    [string]$OperationalConfig
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

if ([string]::IsNullOrWhiteSpace($ComposeFile)) { $ComposeFile = Join-Path $PSScriptRoot "..\compose.production.yaml" }
if ([string]::IsNullOrWhiteSpace($EnvironmentExample)) { $EnvironmentExample = Join-Path $PSScriptRoot "..\.env.example" }
if ([string]::IsNullOrWhiteSpace($OperationalConfig)) { $OperationalConfig = Join-Path $PSScriptRoot "..\operational.production.example.toml" }
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$frontendDockerfile = Join-Path $repositoryRoot "frontend\Dockerfile"
$frontendProxyConfig = Join-Path $repositoryRoot "frontend\nginx.production.conf"
$frontendDockerIgnore = Join-Path $repositoryRoot "frontend\.dockerignore"

function Assert-Contains {
    param([string]$Content, [string]$Pattern, [string]$Message)
    if ($Content -notmatch $Pattern) { throw $Message }
}

function Get-ServiceBlock {
    param([string]$Content, [string]$Service, [string]$NextSectionPattern)
    $match = [regex]::Match($Content, "(?ms)^  $Service`:\r?\n(?<block>.*?)(?=^  $NextSectionPattern`:\r?`n|^volumes`:\r?`n)")
    if (-not $match.Success) { throw "Compose service block is missing: $Service" }
    return $match.Groups["block"].Value
}

$requiredFiles = @(
    $ComposeFile, $EnvironmentExample, $OperationalConfig,
    $frontendDockerfile, $frontendProxyConfig, $frontendDockerIgnore,
    (Join-Path $PSScriptRoot "Backup-Database.ps1"),
    (Join-Path $PSScriptRoot "Invoke-RestoreDrill.ps1"),
    (Join-Path $PSScriptRoot "Invoke-Preflight.ps1"),
    (Join-Path $PSScriptRoot "..\postgres-init\001-create-migrator.sql")
)
foreach ($file in $requiredFiles) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Deployment package file is missing: $file" }
}

$compose = Get-Content -LiteralPath $ComposeFile -Raw -Encoding UTF8
Assert-Contains $compose 'x-backend-service:\s*&backend-service' "Shared backend image anchor is missing."
Assert-Contains $compose 'command:\s*\["vlytics-api"\]' "API command is not explicit."
Assert-Contains $compose 'command:\s*\["vlytics-worker"\]' "Worker command is not explicit."
Assert-Contains $compose 'networks:\s*\r?\n\s+private:\s*\r?\n\s+internal:\s+true' "Private internal network is missing."
Assert-Contains $compose 'read_only:\s+true' "Read-only filesystem hardening is missing."
Assert-Contains $compose 'no-new-privileges:true' "no-new-privileges hardening is missing."
Assert-Contains $compose 'cap_drop:\s*\r?\n\s+- ALL' "Capability drop hardening is missing."
Assert-Contains $compose 'condition:\s+service_completed_successfully' "Migration completion dependency is missing."
Assert-Contains $compose 'POSTGRES_USER:\s+vlytics_bootstrap_admin' "Bootstrap administrator must be distinct from the migrator."
Assert-Contains $compose 'postgres-init:/docker-entrypoint-initdb.d:ro' "Migrator bootstrap init mount is missing."
$bootstrapSql = Get-Content -LiteralPath (Join-Path $PSScriptRoot "..\postgres-init\001-create-migrator.sql") -Raw -Encoding UTF8
Assert-Contains $bootstrapSql 'CREATE ROLE vlytics_migrator LOGIN SUPERUSER' "One-time migrator bootstrap is missing."
Assert-Contains $bootstrapSql 'ALTER ROLE vlytics_bootstrap_admin NOLOGIN' "Bootstrap administrator must be locked after initialization."
if ($compose -match '(?m)^\s+-\s+"?0\.0\.0\.0:') { throw "A service port is exposed on all host interfaces." }

$postgresBlock = Get-ServiceBlock $compose "postgres" "migrate"
$apiBlock = Get-ServiceBlock $compose "api" "worker"
$frontendBlock = Get-ServiceBlock $compose "frontend" "__never__"
if ($postgresBlock -match '(?m)^\s+ports:' -or $apiBlock -match '(?m)^\s+ports:') {
    throw "PostgreSQL and API must not publish host ports in production."
}
Assert-Contains $frontendBlock 'image:\s+\$\{VLYTICS_FRONTEND_IMAGE:' "Frontend image contract is missing."
Assert-Contains $frontendBlock '127\.0\.0\.1:\$\{WEB_PORT:-8080\}:8080' "Frontend is not bound to loopback."
Assert-Contains $frontendBlock 'condition:\s+service_healthy' "Frontend must wait for API health."
Assert-Contains $frontendBlock '/healthz' "Frontend healthcheck is missing."
if (($compose | Select-String -Pattern 'image: \$\{VLYTICS_BACKEND_IMAGE:' -AllMatches).Matches.Count -ne 1) {
    throw "Backend services must inherit exactly one shared immutable image reference."
}

$dockerfile = Get-Content -LiteralPath $frontendDockerfile -Raw -Encoding UTF8
Assert-Contains $dockerfile '(?m)^ARG NODE_IMAGE$' "Frontend Node build image must be an explicit build argument."
Assert-Contains $dockerfile '(?m)^ARG NGINX_IMAGE$' "Frontend Nginx runtime image must be an explicit build argument."
Assert-Contains $dockerfile 'FROM \$\{NODE_IMAGE\} AS build' "Frontend build stage is not pinned through NODE_IMAGE."
Assert-Contains $dockerfile 'FROM \$\{NGINX_IMAGE\} AS runtime' "Frontend runtime stage is not pinned through NGINX_IMAGE."
Assert-Contains $dockerfile '(?m)^RUN npm ci$' "Frontend build must use npm ci."
Assert-Contains $dockerfile '(?m)^USER 101:101$' "Frontend runtime must be non-root."
if ($dockerfile -match '(?i)(SECRET|TOKEN|API_KEY|\.env)') { throw "Frontend Dockerfile must not copy or define secret material." }

$proxy = Get-Content -LiteralPath $frontendProxyConfig -Raw -Encoding UTF8
Assert-Contains $proxy 'location /api/' "Same-origin API proxy is missing."
Assert-Contains $proxy 'proxy_pass http://api:8000;' "API proxy does not target the internal API service."
Assert-Contains $proxy 'try_files \$uri \$uri/ /index\.html;' "SPA history fallback is missing."
Assert-Contains $proxy 'listen 8080;' "Unprivileged frontend listen port is missing."
$dockerIgnore = Get-Content -LiteralPath $frontendDockerIgnore -Raw -Encoding UTF8
Assert-Contains $dockerIgnore '(?m)^\.env\.\*$' "Frontend Docker context must exclude environment files."

$environmentText = Get-Content -LiteralPath $EnvironmentExample -Raw -Encoding UTF8
if ($environmentText -match '(?i)(sk-[a-z0-9]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|postgres(?:ql)?://[^_\r\n]*:[^_\r\n]*@)') {
    throw "Environment example appears to contain a credential."
}
$requiredPlaceholderKeys = @(
    "VLYTICS_BACKEND_IMAGE", "VLYTICS_FRONTEND_IMAGE", "VLYTICS_NODE_BUILD_IMAGE", "VLYTICS_NGINX_RUNTIME_IMAGE",
    "MIGRATOR_DATABASE_PASSWORD", "COLLECTOR_DATABASE_PASSWORD", "ENGINE_DATABASE_PASSWORD",
    "MARKET_INGEST_DATABASE_PASSWORD", "READ_API_DATABASE_PASSWORD", "MIGRATOR_DATABASE_URL",
    "ENGINE_DATABASE_URL", "READ_API_DATABASE_URL", "VLYTICS_OPERATOR_AUTH_SECRET",
    "VLYTICS_READONLY_AUTH_SECRET", "VLYTICS_OPENAI_API_KEY", "VLYTICS_ANTHROPIC_API_KEY",
    "VLYTICS_GOOGLE_API_KEY"
)
foreach ($key in $requiredPlaceholderKeys) {
    Assert-Contains $environmentText ("(?m)^" + [regex]::Escape($key) + "=__REQUIRED__\r?$") "Placeholder is missing for $key."
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $python) {
    $syntaxCheck = "import json,pathlib,sys,tomllib; tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8-sig')); json.loads(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8-sig'))"
    $schemaPath = Join-Path $repositoryRoot "contracts\config.schema.json"
    & $python.Source -c $syntaxCheck ([System.IO.Path]::GetFullPath($OperationalConfig)) $schemaPath
    if ($LASTEXITCODE -ne 0) { throw "Operational TOML or JSON schema syntax validation failed." }
}
else { Write-Warning "Python was not found; TOML/JSON syntax validation was skipped." }

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($null -ne $docker) {
    & $docker.Source compose --env-file $EnvironmentExample --file $ComposeFile config --quiet
    if ($LASTEXITCODE -ne 0) { throw "docker compose config validation failed." }
    $composeValidation = "passed"
}
else { $composeValidation = "skipped-no-docker-cli" }

[pscustomobject]@{
    Result = "passed"
    StaticComposeValidation = "passed"
    FrontendBuildContract = "passed"
    SameOriginProxyValidation = "passed"
    DockerComposeValidation = $composeValidation
    SecretExampleValidation = "passed"
    ConfigSyntaxValidation = $(if ($null -ne $python) { "passed" } else { "skipped-no-python" })
}