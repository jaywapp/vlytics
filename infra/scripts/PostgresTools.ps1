Set-StrictMode -Version 2.0
$ErrorActionPreference = "Stop"

function Resolve-PostgresTool {
    param([Parameter(Mandatory = $true)][string]$Name)
    if ($env:VLYTICS_POSTGRES_BIN) {
        $candidate = Join-Path $env:VLYTICS_POSTGRES_BIN ($Name + ".exe")
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
        $candidate = Join-Path $env:VLYTICS_POSTGRES_BIN $Name
        if (Test-Path -LiteralPath $candidate -PathType Leaf) { return $candidate }
    }
    $command = Get-Command $Name -ErrorAction SilentlyContinue
    if ($null -eq $command) { throw "PostgreSQL tool '$Name' was not found. Set VLYTICS_POSTGRES_BIN or add it to PATH." }
    return $command.Source
}

function Get-RequiredEnvironmentValue {
    param([Parameter(Mandatory = $true)][string]$Name)
    $value = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($value)) { throw "Required environment variable '$Name' is missing." }
    if ($value -match '^__(REQUIRED|REQUIRED_BY_OP_[0-9]+)__$') { throw "Required environment variable '$Name' still contains a placeholder." }
    return $value
}

function ConvertTo-PostgresToolUrl {
    param([Parameter(Mandatory = $true)][string]$DatabaseUrl)
    return $DatabaseUrl -replace '^postgresql\+psycopg://', 'postgresql://'
}

function Push-PostgresEnvironment {
    param([Parameter(Mandatory = $true)][string]$DatabaseUrl)

    $uri = New-Object System.Uri((ConvertTo-PostgresToolUrl $DatabaseUrl))
    if ($uri.Scheme -ne "postgresql" -and $uri.Scheme -ne "postgres") { throw "Database URL must use the PostgreSQL scheme." }
    $userInfo = @($uri.UserInfo -split ':', 2)
    if ($userInfo.Count -lt 1 -or [string]::IsNullOrWhiteSpace($userInfo[0])) { throw "Database URL must include a role name." }
    $databaseName = [System.Uri]::UnescapeDataString($uri.AbsolutePath.TrimStart('/'))
    if ([string]::IsNullOrWhiteSpace($databaseName)) { throw "Database URL must include a database name." }

    $queryEnvironment = @{
        "sslmode" = "PGSSLMODE"
        "sslrootcert" = "PGSSLROOTCERT"
        "sslcert" = "PGSSLCERT"
        "sslkey" = "PGSSLKEY"
        "sslcrl" = "PGSSLCRL"
        "channel_binding" = "PGCHANNELBINDING"
    }
    $queryValues = @{}
    foreach ($pair in @($uri.Query.TrimStart('?') -split '&')) {
        if ([string]::IsNullOrWhiteSpace($pair)) { continue }
        $parts = @($pair -split '=', 2)
        $key = [System.Uri]::UnescapeDataString($parts[0]).ToLowerInvariant()
        if ($queryEnvironment.ContainsKey($key) -and $parts.Count -eq 2) {
            $queryValues[$queryEnvironment[$key]] = [System.Uri]::UnescapeDataString($parts[1])
        }
    }

    $names = @("PGHOST", "PGPORT", "PGUSER", "PGPASSWORD", "PGDATABASE", "PGCONNECT_TIMEOUT", "PGSSLMODE", "PGSSLROOTCERT", "PGSSLCERT", "PGSSLKEY", "PGSSLCRL", "PGCHANNELBINDING")
    $previous = @{}
    foreach ($name in $names) { $previous[$name] = [Environment]::GetEnvironmentVariable($name) }
    $env:PGHOST = $uri.Host
    $env:PGPORT = $(if ($uri.IsDefaultPort) { "5432" } else { $uri.Port.ToString() })
    $env:PGUSER = [System.Uri]::UnescapeDataString($userInfo[0])
    $env:PGPASSWORD = $(if ($userInfo.Count -eq 2) { [System.Uri]::UnescapeDataString($userInfo[1]) } else { $null })
    $env:PGDATABASE = $databaseName
    $env:PGCONNECT_TIMEOUT = "10"
    foreach ($name in $queryEnvironment.Values) {
        $queryValue = $null
        if ($queryValues.ContainsKey($name)) { $queryValue = $queryValues[$name] }
        [Environment]::SetEnvironmentVariable($name, $queryValue)
    }
    return $previous
}

function Pop-PostgresEnvironment {
    param([Parameter(Mandatory = $true)][hashtable]$Previous)
    foreach ($entry in $Previous.GetEnumerator()) { [Environment]::SetEnvironmentVariable($entry.Key, $entry.Value) }
}

function Invoke-PsqlScalar {
    param(
        [Parameter(Mandatory = $true)][string]$Psql,
        [Parameter(Mandatory = $true)][string]$DatabaseUrl,
        [Parameter(Mandatory = $true)][string]$Query
    )

    $previous = Push-PostgresEnvironment $DatabaseUrl
    try {
        $output = & $Psql --no-password --no-psqlrc --set=ON_ERROR_STOP=1 --tuples-only --no-align --command=$Query 2>&1
        if ($LASTEXITCODE -ne 0) { throw "psql query failed without exposing its connection string: $($output -join ' ')" }
        return (($output | ForEach-Object { $_.ToString() }) -join "`n").Trim()
    }
    finally { Pop-PostgresEnvironment $previous }
}

function Set-Utf8File {
    param([Parameter(Mandatory = $true)][string]$Path, [Parameter(Mandatory = $true)][string]$Content)
    $utf8WithoutBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($Path, $Content, $utf8WithoutBom)
}