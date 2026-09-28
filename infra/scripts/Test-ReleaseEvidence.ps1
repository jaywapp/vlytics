[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$BackendImage,
    [Parameter(Mandatory = $true)][string]$FrontendImage,
    [Parameter(Mandatory = $true)][string[]]$BaseImages,
    [Parameter(Mandatory = $true)][string]$OutputDirectory,
    [string]$GoBuilderImage
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"
. (Join-Path $PSScriptRoot "PostgresTools.ps1")
$images = [ordered]@{ backend = $BackendImage; frontend = $FrontendImage }
$requiredFamilies = @("python", "uv", "postgres", "node", "nginx")
if ($BaseImages.Count -ne $requiredFamilies.Count) { throw "Exactly five base images are required." }
foreach ($family in $requiredFamilies) {
    $familyImages = @($BaseImages | Where-Object { $_ -match ("(?:^|/)" + $family + "(?:[:@])") })
    if ($familyImages.Count -ne 1) { throw "Exactly one base image is required for each family." }
    $images[$family] = $familyImages[0]
}
if ($GoBuilderImage) { $images["golang"] = $GoBuilderImage }
foreach ($image in $images.Values) {
    if ($image -notmatch '^[a-z0-9][a-z0-9./:_-]+@sha256:[a-f0-9]{64}$') {
        throw "Every release image must be pinned by digest."
    }
}
$root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot "..\.."))
$revision = & git -C $root rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw "The release checkout could not be identified." }
$python = Join-Path $root "backend\.venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    $python = (Get-Command python -ErrorAction Stop).Source
}
$directory = [IO.Path]::GetFullPath($OutputDirectory)
$inventoryPath = [IO.Path]::GetTempFileName()
try {
    $inventory = [ordered]@{ git_revision = ($revision -join "").Trim(); images = $images }
    Set-Utf8File -Path $inventoryPath -Content (($inventory | ConvertTo-Json -Depth 5) + "`n")
    Push-Location $root
    try {
        & $python (Join-Path $PSScriptRoot "collect_image_evidence.py") --input $inventoryPath --output $directory --scope published-release
        if ($LASTEXITCODE -ne 0) { throw "Release evidence gate failed; inspect the sanitized manifest." }
    } finally {
        Pop-Location
    }
} finally {
    Remove-Item -LiteralPath $inventoryPath -Force -ErrorAction SilentlyContinue
}
[pscustomobject]@{
    Result = "passed"
    ManifestPath = Join-Path $directory "manifest.json"
    ImageCount = $images.Count
}
