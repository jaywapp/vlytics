[CmdletBinding()]
param(
    [string]$SourceConnectionEnvironmentVariable = "VLYTICS_BACKUP_DATABASE_URL",
    [string]$AdminConnectionEnvironmentVariable = "VLYTICS_MIGRATION_DATABASE_URL",
    [string]$OutputDirectory,
    [switch]$KeepBackup
)

. (Join-Path $PSScriptRoot "PostgresTools.ps1")

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $PSScriptRoot "..\..\artifacts\operations"
}

function Invoke-PostgresTool {
    param(
        [Parameter(Mandatory = $true)][string]$Tool,
        [Parameter(Mandatory = $true)][string]$DatabaseUrl,
        [Parameter(Mandatory = $true)][string[]]$Arguments,
        [Parameter(Mandatory = $true)][string]$FailureMessage
    )

    $previous = Push-PostgresEnvironment $DatabaseUrl
    try {
        $output = & $Tool @Arguments 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw ($FailureMessage + ": " + ($output -join " "))
        }
    }
    finally {
        Pop-PostgresEnvironment $previous
    }
}

function New-DatabaseUrl {
    param(
        [Parameter(Mandatory = $true)][string]$DatabaseUrl,
        [Parameter(Mandatory = $true)][string]$DatabaseName
    )

    $normalized = ConvertTo-PostgresToolUrl $DatabaseUrl
    $builder = New-Object System.UriBuilder($normalized)
    $builder.Path = "/" + $DatabaseName
    $builder.Query = ""
    $builder.Fragment = ""
    return $builder.Uri.AbsoluteUri
}

function Get-DomainTables {
    param([string]$Psql, [string]$DatabaseUrl)

    $query = @"
SELECT table_schema || '.' || table_name
FROM information_schema.tables
WHERE table_type = 'BASE TABLE'
  AND table_schema IN ('mirror', 'engine', 'market', 'ops')
ORDER BY table_schema, table_name;
"@
    $result = Invoke-PsqlScalar -Psql $Psql -DatabaseUrl $DatabaseUrl -Query $query
    if ([string]::IsNullOrWhiteSpace($result)) {
        return @()
    }
    return @($result -split "`n")
}

function Get-TableCountSignature {
    param([string]$Psql, [string]$DatabaseUrl, [string[]]$Tables)

    $signature = New-Object System.Collections.Generic.List[string]
    foreach ($table in $Tables) {
        $parts = $table.Split('.')
        if ($parts.Count -ne 2 -or $parts[0] -notmatch '^[a-z_]+$' -or $parts[1] -notmatch '^[a-z_]+$') {
            throw "Unsafe catalog table name encountered."
        }
        $query = 'SELECT count(*) FROM "' + $parts[0] + '"."' + $parts[1] + '";'
        $count = Invoke-PsqlScalar -Psql $Psql -DatabaseUrl $DatabaseUrl -Query $query
        $signature.Add($table + "=" + $count)
    }
    return @($signature)
}

$startedAt = [DateTime]::UtcNow
$sourceUrl = Get-RequiredEnvironmentValue $SourceConnectionEnvironmentVariable
$adminUrl = Get-RequiredEnvironmentValue $AdminConnectionEnvironmentVariable
$psql = Resolve-PostgresTool "psql"
$pgRestore = Resolve-PostgresTool "pg_restore"
$restoreDatabase = "vlytics_restore_" + [DateTime]::UtcNow.ToString("yyyyMMddHHmmss") + "_" + ([Guid]::NewGuid().ToString("N").Substring(0, 8))
$restoreCreated = $false
$backupResult = $null
$resolvedDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
New-Item -ItemType Directory -Force -Path $resolvedDirectory | Out-Null
$reportPath = Join-Path $resolvedDirectory ("restore-drill-" + [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssZ") + ".json")
$report = $null
$cleanupError = $null

try {
    $backupResult = & (Join-Path $PSScriptRoot "Backup-Database.ps1") -ConnectionEnvironmentVariable $SourceConnectionEnvironmentVariable -OutputDirectory $resolvedDirectory -Prefix "restore-source"
    if ($null -eq $backupResult -or -not (Test-Path -LiteralPath $backupResult.DumpPath -PathType Leaf)) {
        throw "Backup step did not return a valid dump artifact."
    }

    $createSql = "CREATE DATABASE $restoreDatabase TEMPLATE template0;"
    Invoke-PostgresTool -Tool $psql -DatabaseUrl $adminUrl -Arguments @("--no-password", "--no-psqlrc", "--set=ON_ERROR_STOP=1", "--command=$createSql") -FailureMessage "Could not create the isolated restore database"
    $restoreCreated = $true
    $restoreUrl = New-DatabaseUrl -DatabaseUrl $adminUrl -DatabaseName $restoreDatabase

    Invoke-PostgresTool -Tool $pgRestore -DatabaseUrl $restoreUrl -Arguments @("--dbname=$restoreDatabase", "--no-password", "--exit-on-error", "--single-transaction", "--no-owner", "--no-privileges", $backupResult.DumpPath) -FailureMessage "pg_restore failed"

    $sourceMigrations = Invoke-PsqlScalar -Psql $psql -DatabaseUrl $sourceUrl -Query "SELECT version || ':' || checksum FROM public.vlytics_schema_migrations ORDER BY version;"
    $restoredMigrations = Invoke-PsqlScalar -Psql $psql -DatabaseUrl $restoreUrl -Query "SELECT version || ':' || checksum FROM public.vlytics_schema_migrations ORDER BY version;"
    if ($sourceMigrations -ne $restoredMigrations) {
        throw "Restored migration versions or checksums differ from the source."
    }

    $sourceTables = @(Get-DomainTables -Psql $psql -DatabaseUrl $sourceUrl)
    $restoredTables = @(Get-DomainTables -Psql $psql -DatabaseUrl $restoreUrl)
    if (($sourceTables -join "`n") -ne ($restoredTables -join "`n")) {
        throw "Restored domain table set differs from the source."
    }

    $sourceCounts = @(Get-TableCountSignature -Psql $psql -DatabaseUrl $sourceUrl -Tables $sourceTables)
    $restoredCounts = @(Get-TableCountSignature -Psql $psql -DatabaseUrl $restoreUrl -Tables $restoredTables)
    if (($sourceCounts -join "`n") -ne ($restoredCounts -join "`n")) {
        throw "Restored domain table row counts differ from the source."
    }

    $invariantQuery = @"
SELECT json_build_object(
    'duplicate_prediction_identity', (
        SELECT count(*) FROM (
            SELECT snapshot_id, variant_id, stage
            FROM engine.predictions
            GROUP BY snapshot_id, variant_id, stage
            HAVING count(*) > 1
        ) AS duplicate_predictions
    ),
    'duplicate_job_attempt', (
        SELECT count(*) FROM (
            SELECT job_id, attempt_no
            FROM ops.job_attempts
            GROUP BY job_id, attempt_no
            HAVING count(*) > 1
        ) AS duplicate_attempts
    ),
    'duplicate_published_event', (
        SELECT count(*) FROM (
            SELECT prediction_id
            FROM engine.prediction_events
            WHERE event_type = 'published'
            GROUP BY prediction_id
            HAVING count(*) > 1
        ) AS duplicate_events
    ),
    'orphan_projection', (
        SELECT count(*)
        FROM engine.prediction_status_projection AS projection
        LEFT JOIN engine.predictions AS prediction ON prediction.id = projection.prediction_id
        WHERE prediction.id IS NULL
    )
)::text;
"@
    $invariantJson = Invoke-PsqlScalar -Psql $psql -DatabaseUrl $restoreUrl -Query $invariantQuery
    $invariants = $invariantJson | ConvertFrom-Json
    if ($invariants.duplicate_prediction_identity -ne 0 -or $invariants.duplicate_job_attempt -ne 0 -or $invariants.duplicate_published_event -ne 0 -or $invariants.orphan_projection -ne 0) {
        throw "Restored database failed duplicate or referential invariants."
    }

    $schemaCount = Invoke-PsqlScalar -Psql $psql -DatabaseUrl $restoreUrl -Query "SELECT count(*) FROM pg_namespace WHERE nspname IN ('mirror','engine','market','ops');"
    if ($schemaCount -ne "4") {
        throw "Restored database does not contain all four domain schemas."
    }

    $migrationCount = 0
    if (-not [string]::IsNullOrWhiteSpace($restoredMigrations)) {
        $migrationCount = @($restoredMigrations -split "`n").Count
    }
$report = [ordered]@{
        schema_version = "1.0"
        started_at_utc = $startedAt.ToString("o")
        completed_at_utc = $null
        postgres_version = Invoke-PsqlScalar -Psql $psql -DatabaseUrl $restoreUrl -Query "SELECT current_setting('server_version');"
        backup_sha256 = $backupResult.Sha256
        migration_count = $migrationCount
        domain_schema_count = [int]$schemaCount
        domain_table_count = $restoredTables.Count
        row_count_signatures_match = $true
        invariants = $invariants
        isolated_restore_database_removed = $false
        backup_removed = $false
        result = "passed"
    }
}
finally {
    if ($restoreCreated) {
        try {
            $adminToolUrl = ConvertTo-PostgresToolUrl $adminUrl
            $terminateSql = "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = '$restoreDatabase' AND pid <> pg_backend_pid();"
            Invoke-PostgresTool -Tool $psql -DatabaseUrl $adminUrl -Arguments @("--no-password", "--no-psqlrc", "--set=ON_ERROR_STOP=1", "--command=$terminateSql") -FailureMessage "Could not terminate isolated restore database sessions"
            $dropSql = "DROP DATABASE IF EXISTS $restoreDatabase;"
            Invoke-PostgresTool -Tool $psql -DatabaseUrl $adminUrl -Arguments @("--no-password", "--no-psqlrc", "--set=ON_ERROR_STOP=1", "--command=$dropSql") -FailureMessage "Could not remove the isolated restore database"
            if ($null -ne $report) {
                $report.isolated_restore_database_removed = $true
            }
        }
        catch {
            $cleanupError = $_
            Write-Warning "Restore database cleanup failed; remove the generated vlytics_restore_* database manually."
        }
    }
    if ($null -ne $backupResult -and -not $KeepBackup) {
        Remove-Item -LiteralPath $backupResult.DumpPath -Force -ErrorAction SilentlyContinue
        Remove-Item -LiteralPath $backupResult.ManifestPath -Force -ErrorAction SilentlyContinue
        if ($null -ne $report) {
            $report.backup_removed = $true
        }
    }
}

if ($null -ne $cleanupError) {
    throw $cleanupError
}
if ($null -eq $report) {
    throw "Restore drill ended without a report."
}
$report.completed_at_utc = [DateTime]::UtcNow.ToString("o")
Set-Utf8File -Path $reportPath -Content (($report | ConvertTo-Json -Depth 6) + "`n")
Write-Host "Restore drill passed: migration checksums, schema/table set, row counts, invariants, and cleanup match."
[pscustomobject]@{ ReportPath = $reportPath; Result = "passed"; TableCount = $report.domain_table_count; MigrationCount = $report.migration_count }