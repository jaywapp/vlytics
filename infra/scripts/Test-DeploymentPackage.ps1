[CmdletBinding()]
param(
    [string]$ComposeFile,
    [string]$EnvironmentExample,
    [string]$OperationalConfig,
    [ValidateSet("standard", "personal_home")][string]$Profile = "standard"
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "ProviderEgress.ps1")

if ($Profile -eq "personal_home") {
    if ([string]::IsNullOrWhiteSpace($ComposeFile)) { $ComposeFile = Join-Path $PSScriptRoot "..\compose.home.yaml" }
    if ([string]::IsNullOrWhiteSpace($EnvironmentExample)) { $EnvironmentExample = Join-Path $PSScriptRoot "..\.env.home.example" }
    if ([string]::IsNullOrWhiteSpace($OperationalConfig)) { $OperationalConfig = Join-Path $PSScriptRoot "..\operational.home.example.toml" }
}
else {
    if ([string]::IsNullOrWhiteSpace($ComposeFile)) { $ComposeFile = Join-Path $PSScriptRoot "..\compose.production.yaml" }
    if ([string]::IsNullOrWhiteSpace($EnvironmentExample)) { $EnvironmentExample = Join-Path $PSScriptRoot "..\.env.example" }
    if ([string]::IsNullOrWhiteSpace($OperationalConfig)) { $OperationalConfig = Join-Path $PSScriptRoot "..\operational.production.example.toml" }
}
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$frontendDockerfile = Join-Path $repositoryRoot "frontend\Dockerfile"
$frontendProxyConfig = Join-Path $repositoryRoot "frontend\nginx.production.conf"
$frontendDockerIgnore = Join-Path $repositoryRoot "frontend\.dockerignore"
$operatorIngressConfig = Join-Path $PSScriptRoot "..\nginx.operator-ingress.conf"

function Assert-Contains {
    param([string]$Content, [string]$Pattern, [string]$Message)
    if ($Content -notmatch $Pattern) { throw $Message }
}

function Get-ServiceBlock {
    param([string]$Content, [string]$Service, [string]$NextSectionPattern)
    $match = [regex]::Match($Content, "(?ms)^  $Service`:\r?\n(?<block>.*?)(?=^  [A-Za-z0-9_]+`:\r?`n|^volumes`:\r?`n)")
    if (-not $match.Success) { throw "Compose service block is missing: $Service" }
    return $match.Groups["block"].Value
}

$requiredFiles = @(
    $ComposeFile, $EnvironmentExample, $OperationalConfig,
    $frontendDockerfile, $frontendProxyConfig, $frontendDockerIgnore, $operatorIngressConfig,
    (Join-Path $PSScriptRoot "Backup-Database.ps1"),
    (Join-Path $PSScriptRoot "Invoke-RestoreDrill.ps1"),
    (Join-Path $PSScriptRoot "Invoke-Preflight.ps1"),
    (Join-Path $PSScriptRoot "ProviderEgress.ps1"),
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
$workerBlock = Get-ServiceBlock $compose "worker" "frontend"
$frontendBlock = Get-ServiceBlock $compose "frontend" "operator_ingress"
$ingressBlock = Get-ServiceBlock $compose "operator_ingress" "__never__"
if ($postgresBlock -match '(?m)^\s+ports:' -or $apiBlock -match '(?m)^\s+ports:' -or
    $workerBlock -match '(?m)^\s+ports:' -or $frontendBlock -match '(?m)^\s+ports:') {
    throw "Only operator ingress may publish a production host port."
}
Assert-Contains $frontendBlock 'image:\s+\$\{VLYTICS_FRONTEND_IMAGE:' "Frontend image contract is missing."
Assert-Contains $frontendBlock 'networks:\s*\r?\n\s+- private\s*\r?\n' "Frontend must remain on the private network."
Assert-Contains $frontendBlock 'condition:\s+service_healthy' "Frontend must wait for API health."
Assert-Contains $frontendBlock '/healthz' "Frontend healthcheck is missing."
Assert-Contains $ingressBlock 'nginx\.operator-ingress\.conf:/etc/nginx/conf\.d/default\.conf:ro' "Operator ingress config mount is missing."
Assert-Contains $ingressBlock 'networks:\s*\r?\n\s+- private\s*\r?\n\s+- operator_access' "Operator ingress must bridge private and access networks."
if (($compose | Select-String -Pattern '(?m)^\s+- operator_access\s*$' -AllMatches).Matches.Count -ne 1) {
    throw "Only operator ingress may join the access network."
}
Assert-Contains $ingressBlock '127\.0\.0\.1:\$\{WEB_PORT:-8080\}:8080' "Operator ingress is not bound to loopback."
Assert-Contains $ingressBlock 'frontend:\s*\r?\n\s+condition:\s+service_healthy' "Operator ingress must wait for frontend health."
Assert-Contains $compose 'operator_access:\s*\r?\n\s+driver:\s+bridge' "Operator access network is missing."
if (($compose | Select-String -Pattern 'image: \$\{VLYTICS_BACKEND_IMAGE:' -AllMatches).Matches.Count -ne 1) {
    throw "Backend services must inherit exactly one shared immutable image reference."
}

$dockerfile = Get-Content -LiteralPath $frontendDockerfile -Raw -Encoding UTF8
Assert-Contains $dockerfile '(?m)^ARG NODE_IMAGE\r?$' "Frontend Node build image must be an explicit build argument."
Assert-Contains $dockerfile '(?m)^ARG NGINX_IMAGE\r?$' "Frontend Nginx runtime image must be an explicit build argument."
Assert-Contains $dockerfile 'FROM \$\{NODE_IMAGE\} AS build' "Frontend build stage is not pinned through NODE_IMAGE."
Assert-Contains $dockerfile 'FROM \$\{NGINX_IMAGE\} AS runtime' "Frontend runtime stage is not pinned through NGINX_IMAGE."
Assert-Contains $dockerfile '(?m)^RUN npm ci\r?$' "Frontend build must use npm ci."
Assert-Contains $dockerfile '(?m)^USER 101:101\r?$' "Frontend runtime must be non-root."
if ($dockerfile -match '(?i)(SECRET|TOKEN|API_KEY|\.env)') { throw "Frontend Dockerfile must not copy or define secret material." }

$proxy = Get-Content -LiteralPath $frontendProxyConfig -Raw -Encoding UTF8
$ingressProxy = Get-Content -LiteralPath $operatorIngressConfig -Raw -Encoding UTF8
Assert-Contains $ingressProxy 'proxy_pass http://frontend:8080;' "Operator ingress must target internal frontend."
Assert-Contains $proxy 'location /api/' "Same-origin API proxy is missing."
Assert-Contains $proxy 'proxy_pass http://api:8000;' "API proxy does not target the internal API service."
Assert-Contains $proxy 'try_files \$uri \$uri/ /index\.html;' "SPA history fallback is missing."
Assert-Contains $proxy 'listen 8080;' "Unprivileged frontend listen port is missing."
$dockerIgnore = Get-Content -LiteralPath $frontendDockerIgnore -Raw -Encoding UTF8
Assert-Contains $dockerIgnore '(?m)^\.env\.\*\r?$' "Frontend Docker context must exclude environment files."

$environmentText = Get-Content -LiteralPath $EnvironmentExample -Raw -Encoding UTF8
if ($environmentText -match '(?i)(sk-[a-z0-9]{12,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|postgres(?:ql)?://[^_\r\n]*:[^_\r\n]*@)') {
    throw "Environment example appears to contain a credential."
}
$requiredPlaceholderKeys = @(
    "VLYTICS_BACKEND_IMAGE", "VLYTICS_FRONTEND_IMAGE", "VLYTICS_NODE_BUILD_IMAGE", "VLYTICS_NGINX_RUNTIME_IMAGE",
    "VLYTICS_PYTHON_BUILD_IMAGE", "VLYTICS_UV_BUILD_IMAGE", "VLYTICS_POSTGRES_IMAGE",
    "MIGRATOR_DATABASE_PASSWORD", "COLLECTOR_DATABASE_PASSWORD", "ENGINE_DATABASE_PASSWORD",
    "MARKET_INGEST_DATABASE_PASSWORD", "READ_API_DATABASE_PASSWORD", "MIGRATOR_DATABASE_URL",
    "ENGINE_DATABASE_URL", "READ_API_DATABASE_URL", "VLYTICS_OPERATOR_AUTH_SECRET",
    "VLYTICS_READONLY_AUTH_SECRET", "VLYTICS_OPENAI_API_KEY", "VLYTICS_ANTHROPIC_API_KEY",
    "VLYTICS_GOOGLE_API_KEY"
)
foreach ($key in $requiredPlaceholderKeys) {
    if ($Profile -eq "personal_home" -and $key -in @("VLYTICS_OPENAI_API_KEY", "VLYTICS_ANTHROPIC_API_KEY", "VLYTICS_GOOGLE_API_KEY")) { continue }
    Assert-Contains $environmentText ("(?m)^" + [regex]::Escape($key) + "=__REQUIRED__\r?$") "Placeholder is missing for $key."
}

$python = Get-Command python -ErrorAction SilentlyContinue
if ($null -ne $python) {
    $syntaxCheck = @"
import json, pathlib, sys, tomllib
config = tomllib.loads(pathlib.Path(sys.argv[1]).read_text(encoding='utf-8-sig'))
json.loads(pathlib.Path(sys.argv[2]).read_text(encoding='utf-8-sig'))
profile = config.get('deployment', {}).get('profile', 'standard')
if profile != sys.argv[3]:
    raise ValueError('Package profile must match operational deployment profile')
if profile == 'personal_home':
    enabled = {name for name in ('openai', 'anthropic', 'google') if config['ai'][name]['enabled']}
    if not enabled:
        raise ValueError('Home package requires an enabled Provider')
    env = dict(line.split('=', 1) for line in pathlib.Path(sys.argv[4]).read_text(encoding='utf-8-sig').splitlines() if line and not line.startswith('#'))
    for name in enabled:
        if env.get(config['ai'][name]['api_key_env']) != '__REQUIRED__':
            raise ValueError('Enabled Provider example requires a key placeholder')
    if config['backup']['enabled'] or config['alerting']['enabled']:
        raise ValueError('Home package template requires backup and alerting disabled')
else:
    enabled = {name for name in ('openai', 'anthropic', 'google') if config['ai'][name]['enabled']}
print(json.dumps(sorted(enabled)))
"@
    $schemaPath = Join-Path $repositoryRoot "contracts\config.schema.json"
    $activeOutput = & $python.Source -c $syntaxCheck ([System.IO.Path]::GetFullPath($OperationalConfig)) $schemaPath $Profile ([System.IO.Path]::GetFullPath($EnvironmentExample))
    if ($LASTEXITCODE -ne 0) { throw "Operational TOML or JSON schema syntax validation failed." }
    [string[]]$enabledProviders = ConvertFrom-Json -InputObject ($activeOutput -join "")
}
else {
    if ($Profile -eq "personal_home") { throw "Python is required to validate home Provider selection." }
    Write-Warning "Python was not found; TOML/JSON syntax validation was skipped."
}

if ($Profile -eq "personal_home") {
    Assert-Contains $workerBlock '<<:\s+\*backend-service' "Home worker must inherit the private backend network."
    if ($workerBlock -match '(?m)^    networks:') { throw "Home worker must not override the private network." }
    foreach ($provider in @("openai", "anthropic", "google")) {
        $serviceName = $provider + "_egress"
        $networkName = $provider + "_access"
        $keyPrefix = if ($provider -eq "google") { "GOOGLE" } else { $provider.ToUpperInvariant() }
        if ($provider -notin $enabledProviders) {
            if ($compose -match ("(?m)^  " + $serviceName + ":") -or
                $workerBlock -match ("VLYTICS_" + $keyPrefix + "_(API_KEY|PROXY_URL):")) {
                throw "Disabled home Provider must not have a relay, worker key or proxy."
            }
            continue
        }
        $relayBlock = Get-ServiceBlock $compose $serviceName "__never__"
        Assert-Contains $relayBlock '<<:\s+\*backend-service' "Home relay must inherit backend hardening."
        Assert-Contains $relayBlock 'command:\s*\["python", "-m", "vlytics.ops.provider_egress"\]' "Home relay module is invalid."
        Assert-Contains $relayBlock ('(?m)VLYTICS_EGRESS_PROVIDER:\s+' + $provider + '\r?$') "Home relay Provider is invalid."
        Assert-Contains $relayBlock 'VLYTICS_EGRESS_BIND:\s+0\.0\.0\.0:8081' "Home relay bind is invalid."
        Assert-Contains $relayBlock ('networks:\s*\r?\n      private: \{\}\s*\r?\n      ' + $networkName + ':\s*\r?\n        gw_priority: 1\s*\r?\n') "Home relay network isolation or default gateway is invalid (Compose 2.33.1+ required)."
        Assert-Contains $workerBlock ('(?m)VLYTICS_' + $keyPrefix + '_PROXY_URL:\s+http://' + $serviceName + ':8081\r?$') "Home worker proxy is invalid."
        Assert-Contains $workerBlock ('VLYTICS_' + $keyPrefix + '_API_KEY:') "Home worker Provider key is missing."
        Assert-Contains $compose ($networkName + ':\s*\r?\n\s+driver:\s+bridge') "Home Provider access network is missing."
        if ($relayBlock -match '(?m)^    (ports|volumes):' -or
            $relayBlock -match 'VLYTICS_.*(API_KEY|AUTH_SECRET|DATABASE_URL)' -or
            ($compose | Select-String -Pattern ('(?m)^      ' + $networkName + ':\s*$') -AllMatches).Matches.Count -ne 1) {
            throw "Home relay must not receive secrets or expose its network or host port."
        }
    }
}

$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($null -ne $docker) {
    & $docker.Source compose --env-file $EnvironmentExample --file $ComposeFile config --quiet
    if ($LASTEXITCODE -ne 0) { throw "docker compose config validation failed." }
    if ($Profile -eq "personal_home") {
        $renderedJson = & $docker.Source compose --env-file $EnvironmentExample --file $ComposeFile config --format json
        if ($LASTEXITCODE -ne 0) { throw "docker compose JSON validation failed." }
        $rendered = ($renderedJson -join [Environment]::NewLine) | ConvertFrom-Json
        Assert-HomeProviderEgress $rendered $enabledProviders -AllowExampleKeys
    }
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
