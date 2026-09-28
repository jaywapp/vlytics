[CmdletBinding()]
param(
    [string]$ConnectionEnvironmentVariable = "VLYTICS_BACKUP_DATABASE_URL",
    [string]$OutputDirectory,
    [string]$Prefix = "vlytics"
)

. (Join-Path $PSScriptRoot "PostgresTools.ps1")
if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $PSScriptRoot "..\..\artifacts\operations"
}
Get-RequiredEnvironmentValue $ConnectionEnvironmentVariable | Out-Null
$pgDump = Resolve-PostgresTool "pg_dump"
$resolvedDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $resolvedDirectory | Out-Null
$timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$dumpPath = Join-Path $resolvedDirectory ($Prefix + "-" + $timestamp + "-" + [Guid]::NewGuid().ToString("N").Substring(0, 8) + ".dump")
Push-Location (Join-Path $PSScriptRoot "..\..\backend")
try {
    $result = & uv run --frozen python -m vlytics.storage.recovery backup --connection-env $ConnectionEnvironmentVariable --output $dumpPath --pg-dump $pgDump
    if ($LASTEXITCODE -ne 0) { throw "Consistent database backup failed." }
    $result -join "`n" | ConvertFrom-Json
}
finally { Pop-Location }
