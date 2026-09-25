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

$databaseUrl = Get-RequiredEnvironmentValue $ConnectionEnvironmentVariable
$pgDump = Resolve-PostgresTool "pg_dump"
$psql = Resolve-PostgresTool "psql"
$resolvedDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $resolvedDirectory | Out-Null
$timestamp = [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ")
$dumpPath = Join-Path $resolvedDirectory ($Prefix + "-" + $timestamp + ".dump")
$manifestPath = $dumpPath + ".manifest.json"
$previousPostgresEnvironment = $null
$completed = $false

try {
    $previousPostgresEnvironment = Push-PostgresEnvironment $databaseUrl
    & $pgDump --format=custom --compress=9 --no-owner --no-privileges --file=$dumpPath
    if ($LASTEXITCODE -ne 0) { throw "pg_dump failed without exposing its connection string." }
    if (-not (Test-Path -LiteralPath $dumpPath -PathType Leaf)) { throw "pg_dump did not create the expected backup file." }

    $serverVersion = Invoke-PsqlScalar -Psql $psql -DatabaseUrl $databaseUrl -Query "SELECT current_setting('server_version');"
    $databaseName = Invoke-PsqlScalar -Psql $psql -DatabaseUrl $databaseUrl -Query "SELECT current_database();"
    $hash = (Get-FileHash -LiteralPath $dumpPath -Algorithm SHA256).Hash.ToLowerInvariant()
    $manifest = [ordered]@{
        schema_version = "1.0"
        created_at_utc = [DateTime]::UtcNow.ToString("o")
        database_name = $databaseName
        postgres_version = $serverVersion
        format = "custom"
        sha256 = $hash
        byte_length = (Get-Item -LiteralPath $dumpPath).Length
    }
    Set-Utf8File -Path $manifestPath -Content (($manifest | ConvertTo-Json -Depth 4) + "`n")
    $completed = $true
    Write-Host "Backup created and hashed. Connection details were not written to the artifact."
    [pscustomobject]@{ DumpPath = $dumpPath; ManifestPath = $manifestPath; Sha256 = $hash }
}
finally {
    if ($null -ne $previousPostgresEnvironment) { Pop-PostgresEnvironment $previousPostgresEnvironment }
    if (-not $completed) {
        Remove-Item -LiteralPath $dumpPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $manifestPath -Force -ErrorAction SilentlyContinue
    }
}