[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackendImage,
    [Parameter(Mandatory = $true)][string]$FrontendImage,
    [Parameter(Mandatory = $true)][string[]]$BaseImages,
    [Parameter(Mandatory = $true)][string]$OutputDirectory
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "PostgresTools.ps1")
$docker = Get-Command docker -ErrorAction Stop
$trivy = Get-Command trivy -ErrorAction Stop
$Images = @($BackendImage, $FrontendImage) + $BaseImages
foreach ($image in $Images) {
    if ($image -notmatch '^[a-z0-9][a-z0-9./:_-]+@sha256:[a-f0-9]{64}$') { throw "Every release image must be pinned by digest." }
}
$requiredFamilies = @("python", "uv", "postgres", "node", "nginx")
foreach ($family in $requiredFamilies) {
    if (-not ($BaseImages | Where-Object { $_ -match ("(?:^|/)" + $family + "(?:[:@/])") })) {
        throw "Release evidence is missing a required base image: $family"
    }
}
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$dirty = & git -C $root status --porcelain
if ($LASTEXITCODE -ne 0 -or $dirty) { throw "Release evidence requires a clean committed checkout." }
$revision = & git -C $root rev-parse HEAD
$directory = [IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $directory | Out-Null
$records = @()
$failed = $false
$index = 0
foreach ($image in $Images) {
    & $docker.Source image inspect $image --format '{{.Id}}' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "A pinned release image is not available locally." }
    $index += 1
    $sbom = Join-Path $directory ("image-" + $index + ".cdx.json")
    $vulnerabilities = Join-Path $directory ("image-" + $index + ".vulnerabilities.json")
    & $trivy.Source image --quiet --format cyclonedx --output $sbom $image
    if ($LASTEXITCODE -ne 0) { throw "SBOM generation failed." }
    & $trivy.Source image --quiet --scanners vuln --severity "HIGH,CRITICAL" --exit-code 1 --format json --output $vulnerabilities $image
    $passed = $LASTEXITCODE -eq 0
    if (-not $passed) { $failed = $true }
    if (-not (Test-Path -LiteralPath $vulnerabilities -PathType Leaf)) { throw "Vulnerability scan did not produce evidence." }
    $records += [ordered]@{
        image = $image
        sbom_sha256 = (Get-FileHash -LiteralPath $sbom -Algorithm SHA256).Hash.ToLowerInvariant()
        vulnerabilities_sha256 = (Get-FileHash -LiteralPath $vulnerabilities -Algorithm SHA256).Hash.ToLowerInvariant()
        high_critical_gate_passed = $passed
    }
}
$manifest = [ordered]@{
    schema_version = "1.0"
    git_revision = ($revision -join "").Trim()
    created_at_utc = [DateTime]::UtcNow.ToString("o")
    trivy_version = (& $trivy.Source --version) -join "`n"
    images = $records
    passed = -not $failed
}
$manifestPath = Join-Path $directory "release-evidence.json"
Set-Utf8File -Path $manifestPath -Content (($manifest | ConvertTo-Json -Depth 8) + "`n")
if ($failed) { throw "Release vulnerability gate failed; inspect the recorded reports." }
[pscustomobject]@{ Result = "passed"; ManifestPath = $manifestPath; ImageCount = $Images.Count }
