[CmdletBinding()]
param()

Set-StrictMode -Version 2.0

function Resolve-DeploymentFile {
    param(
        [Parameter(Mandatory = $true)][string]$Path,
        [Parameter(Mandatory = $true)][string]$BaseDirectory
    )
    $candidate = $Path
    if (-not [System.IO.Path]::IsPathRooted($candidate)) {
        $candidate = Join-Path $BaseDirectory $candidate
    }
    if (-not (Test-Path -LiteralPath $candidate -PathType Leaf)) {
        throw "Deployment file does not exist: $candidate"
    }
    return (Get-Item -LiteralPath $candidate).FullName
}

function Assert-DeploymentFileBinding {
    param(
        [Parameter(Mandatory = $true)][string]$ProvidedPath,
        [Parameter(Mandatory = $true)][string]$ConfiguredPath,
        [Parameter(Mandatory = $true)][string]$ComposeDirectory,
        [Parameter(Mandatory = $true)][string]$Name
    )
    $provided = Resolve-DeploymentFile $ProvidedPath (Get-Location).ProviderPath
    $configured = Resolve-DeploymentFile $ConfiguredPath $ComposeDirectory
    $comparison = [System.StringComparison]::Ordinal
    if ($env:OS -eq "Windows_NT") { $comparison = [System.StringComparison]::OrdinalIgnoreCase }
    if (-not [string]::Equals($provided, $configured, $comparison)) {
        throw "$Name must match the file mounted by Compose."
    }
    return $provided
}
