function Get-DesktopRoleDatabaseUrl {
    param(
        [Parameter(Mandatory = $true)][ValidateSet('MIGRATOR','COLLECTOR','ENGINE','MARKET_INGEST','READ_API')][string]$Role,
        [Parameter(Mandatory = $true)][string]$Password
    )
    $userName = 'vlytics_' + $Role.ToLowerInvariant()
    if ($Role -ne 'MIGRATOR') { $userName += '_login' }
    return 'postgresql+psycopg://' + $userName + ':' + [Uri]::EscapeDataString($Password) + '@postgres:5432/vlytics'
}
