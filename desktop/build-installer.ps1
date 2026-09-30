[CmdletBinding()]
param(
    [Parameter()]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$Version = '1.0.0',

    [Parameter()]
    [ValidateSet('Debug', 'Release')]
    [string]$Configuration = 'Release'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$ArgumentList
    )

    & $FilePath @ArgumentList
    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath"
    }
}

function Assert-PathUnderRoot {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$Root
    )

    $resolvedPath = [System.IO.Path]::GetFullPath($Path).TrimEnd('\')
    $resolvedRoot = [System.IO.Path]::GetFullPath($Root).TrimEnd('\')
    if (-not $resolvedPath.StartsWith($resolvedRoot + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to modify a path outside the repository: $resolvedPath"
    }
}

function Reset-Directory {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Path,

        [Parameter(Mandatory = $true)]
        [string]$RepositoryRoot
    )

    Assert-PathUnderRoot -Path $Path -Root $RepositoryRoot
    if (Test-Path -LiteralPath $Path) {
        Remove-Item -LiteralPath $Path -Recurse -Force
    }
    New-Item -ItemType Directory -Path $Path -Force | Out-Null
}

function Copy-FileChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,

        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Leaf)) {
        throw "Required payload file is missing: $Source"
    }

    $destinationDirectory = Split-Path -Parent $Destination
    New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
    Copy-Item -LiteralPath $Source -Destination $Destination -Force
}

function Copy-DirectoryChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Source,

        [Parameter(Mandatory = $true)]
        [string]$Destination
    )

    if (-not (Test-Path -LiteralPath $Source -PathType Container)) {
        throw "Required payload directory is missing: $Source"
    }

    $sourceRoot = [System.IO.Path]::GetFullPath($Source).TrimEnd('\')
    $forbiddenDirectoryNames = @('__pycache__', '.mypy_cache', '.pytest_cache', '.ruff_cache', '.venv', 'bin', 'obj', 'artifacts', 'tests')
    $forbiddenExtensions = @('.pyc', '.pyo', '.pdb')

    Get-ChildItem -LiteralPath $sourceRoot -File -Recurse | ForEach-Object {
        if (($_.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0) {
            throw "Reparse points are not allowed in the installer payload: $($_.FullName)"
        }

        $relativePath = $_.FullName.Substring($sourceRoot.Length).TrimStart('\')
        $segments = $relativePath -split '[\\/]'
        $blockedSegment = $segments | Where-Object { $forbiddenDirectoryNames -contains $_ } | Select-Object -First 1
        if ($null -ne $blockedSegment) {
            return
        }
        if ($forbiddenExtensions -contains $_.Extension.ToLowerInvariant()) {
            return
        }
        if ($_.Name -eq '.env' -or $_.Name -match '^\.env\.(?!.*\.example$)') {
            throw "Secret-bearing environment files are not allowed in the installer payload: $($_.FullName)"
        }

        $destinationPath = Join-Path $Destination $relativePath
        $destinationDirectory = Split-Path -Parent $destinationPath
        New-Item -ItemType Directory -Path $destinationDirectory -Force | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $destinationPath -Force
    }
}

function Get-StableHash {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $algorithm = [System.Security.Cryptography.SHA256]::Create()
    try {
        $bytes = [System.Text.Encoding]::UTF8.GetBytes($Value.ToLowerInvariant())
        $hash = $algorithm.ComputeHash($bytes)
        return ([System.BitConverter]::ToString($hash)).Replace('-', '').ToLowerInvariant()
    }
    finally {
        $algorithm.Dispose()
    }
}

function Get-StableGuid {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    $hash = Get-StableHash -Value $Value
    return ('{0}-{1}-{2}-{3}-{4}' -f $hash.Substring(0, 8), $hash.Substring(8, 4), $hash.Substring(12, 4), $hash.Substring(16, 4), $hash.Substring(20, 12)).ToUpperInvariant()
}

function ConvertTo-XmlAttribute {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Value
    )

    return [System.Security.SecurityElement]::Escape($Value)
}

function Write-GeneratedPayload {
    param(
        [Parameter(Mandatory = $true)]
        [string]$PayloadRoot,

        [Parameter(Mandatory = $true)]
        [string]$OutputPath
    )

    $directoryIds = @{
        'app' = 'APPFOLDER'
        'runtime' = 'RUNTIMEFOLDER'
    }
    $directoryChildren = @{}
    $components = New-Object System.Collections.Generic.List[string]
    $payloadRootFull = [System.IO.Path]::GetFullPath($PayloadRoot).TrimEnd('\')
    $payloadFiles = @(Get-ChildItem -LiteralPath $payloadRootFull -File -Recurse | Sort-Object FullName)
    if ($payloadFiles.Count -eq 0) {
        throw 'The installer payload is empty.'
    }

    foreach ($file in $payloadFiles) {
        $relativePath = $file.FullName.Substring($payloadRootFull.Length).TrimStart('\')
        $segments = $relativePath -split '\\'
        $rootName = $segments[0]
        if (-not $directoryIds.ContainsKey($rootName)) {
            throw "Unexpected top-level payload directory: $rootName"
        }

        $currentRelativeDirectory = $rootName
        $currentDirectoryId = $directoryIds[$rootName]
        for ($index = 1; $index -lt ($segments.Length - 1); $index++) {
            $childName = $segments[$index]
            $childRelativeDirectory = $currentRelativeDirectory + '\' + $childName
            if (-not $directoryIds.ContainsKey($childRelativeDirectory)) {
                $childHash = Get-StableHash -Value ('directory:' + $childRelativeDirectory)
                $directoryIds[$childRelativeDirectory] = 'dir_' + $childHash.Substring(0, 32)
            }

            $childDirectoryId = $directoryIds[$childRelativeDirectory]
            if (-not $directoryChildren.ContainsKey($currentDirectoryId)) {
                $directoryChildren[$currentDirectoryId] = @{}
            }
            $directoryChildren[$currentDirectoryId][$childDirectoryId] = $childName
            $currentRelativeDirectory = $childRelativeDirectory
            $currentDirectoryId = $childDirectoryId
        }

        $fileHash = Get-StableHash -Value ('file:' + $relativePath)
        $componentId = 'cmp_' + $fileHash.Substring(0, 32)
        $fileId = 'fil_' + $fileHash.Substring(0, 32)
        $componentGuid = Get-StableGuid -Value ('component:' + $relativePath)
        $source = ConvertTo-XmlAttribute -Value $file.FullName
        $name = ConvertTo-XmlAttribute -Value $file.Name
        $registryName = $fileHash.Substring(0, 32)

        $components.Add(@"
      <Component Id="$componentId" Directory="$currentDirectoryId" Guid="$componentGuid" Bitness="always64">
        <File Id="$fileId" Source="$source" Name="$name" />
        <RegistryValue Root="HKCU" Key="Software\Vlytics\Installer\Components" Name="$registryName" Type="integer" Value="1" KeyPath="yes" />
      </Component>
"@)
    }

    $cleanupDirectoryIds = @('LocalProgramsFolder', 'INSTALLFOLDER') + @($directoryIds.Values)
    foreach ($directoryId in @($cleanupDirectoryIds | Sort-Object -Unique)) {
        $cleanupHash = Get-StableHash -Value ('cleanup:' + $directoryId)
        $componentId = 'cmp_cleanup_' + $cleanupHash.Substring(0, 24)
        $removeFolderId = 'rmf_' + $cleanupHash.Substring(0, 32)
        $componentGuid = Get-StableGuid -Value ('cleanup-component:' + $directoryId)
        $registryName = 'cleanup_' + $cleanupHash.Substring(0, 24)
        $components.Add(@"
      <Component Id="$componentId" Directory="$directoryId" Guid="$componentGuid" Bitness="always64">
        <RemoveFolder Id="$removeFolderId" Directory="$directoryId" On="uninstall" />
        <RegistryValue Root="HKCU" Key="Software\Vlytics\Installer\Components" Name="$registryName" Type="integer" Value="1" KeyPath="yes" />
      </Component>
"@)
    }

    $lines = New-Object System.Collections.Generic.List[string]
    $lines.Add('<?xml version="1.0" encoding="utf-8"?>')
    $lines.Add('<Wix xmlns="http://wixtoolset.org/schemas/v4/wxs">')
    $lines.Add('  <Fragment>')
    foreach ($parentDirectoryId in @($directoryChildren.Keys | Sort-Object)) {
        $lines.Add(('    <DirectoryRef Id="{0}">' -f $parentDirectoryId))
        foreach ($childDirectoryId in @($directoryChildren[$parentDirectoryId].Keys | Sort-Object)) {
            $childName = ConvertTo-XmlAttribute -Value $directoryChildren[$parentDirectoryId][$childDirectoryId]
            $lines.Add(('      <Directory Id="{0}" Name="{1}" />' -f $childDirectoryId, $childName))
        }
        $lines.Add('    </DirectoryRef>')
    }
    $lines.Add('  </Fragment>')
    $lines.Add('  <Fragment>')
    $lines.Add('    <ComponentGroup Id="PayloadComponents">')
    foreach ($component in $components) {
        $lines.Add($component.TrimEnd())
    }
    $lines.Add('    </ComponentGroup>')
    $lines.Add('  </Fragment>')
    $lines.Add('</Wix>')
    [System.IO.File]::WriteAllLines($OutputPath, $lines, (New-Object System.Text.UTF8Encoding($false)))
}

$scriptRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$repositoryRoot = [System.IO.Path]::GetFullPath((Join-Path $scriptRoot '..'))
$appProject = Join-Path $scriptRoot 'Vlytics.Settings.App\Vlytics.Settings.App.csproj'
$installerProject = Join-Path $scriptRoot 'Vlytics.Installer\Vlytics.Installer.wixproj'
$artifactRoot = Join-Path $repositoryRoot 'artifacts\windows-installer'
$workRoot = Join-Path $repositoryRoot 'artifacts\.windows-installer-work'
$publishRoot = Join-Path $workRoot 'publish'
$payloadRoot = Join-Path $workRoot 'payload'
$appPayloadRoot = Join-Path $payloadRoot 'app'
$runtimePayloadRoot = Join-Path $payloadRoot 'runtime'
$msiBuildRoot = Join-Path $workRoot 'msi'
$portableRoot = Join-Path $workRoot 'portable'
$generatedPayloadPath = Join-Path $workRoot 'GeneratedPayload.wxs'
$nugetPackages = Join-Path $repositoryRoot '.packages'
$dotnetCliHome = Join-Path $workRoot '.dotnet-home'

$versionParts = $Version.Split('.')
if ([int]$versionParts[0] -gt 255 -or [int]$versionParts[1] -gt 255 -or [int]$versionParts[2] -gt 65535) {
    throw 'MSI versions require major/minor <= 255 and build <= 65535.'
}
if (-not (Get-Command dotnet -ErrorAction SilentlyContinue)) {
    throw '.NET SDK 10 is required to build the Windows installer.'
}
$dotnetVersion = (& dotnet --version).Trim()
if ($LASTEXITCODE -ne 0 -or $dotnetVersion -notmatch '^10\.') {
    throw ".NET SDK 10 is required; active SDK is $dotnetVersion."
}
if (-not (Test-Path -LiteralPath $appProject -PathType Leaf)) {
    throw "Settings application project is missing: $appProject"
}

Reset-Directory -Path $artifactRoot -RepositoryRoot $repositoryRoot
Reset-Directory -Path $workRoot -RepositoryRoot $repositoryRoot
New-Item -ItemType Directory -Path $publishRoot, $appPayloadRoot, $runtimePayloadRoot, $msiBuildRoot, $portableRoot, $nugetPackages, $dotnetCliHome -Force | Out-Null

$previousDotnetCliHome = $env:DOTNET_CLI_HOME
$previousNugetPackages = $env:NUGET_PACKAGES
$previousTelemetry = $env:DOTNET_CLI_TELEMETRY_OPTOUT
$previousCertificateGeneration = $env:DOTNET_GENERATE_ASPNET_CERTIFICATE
try {
    $env:DOTNET_CLI_HOME = $dotnetCliHome
    $env:NUGET_PACKAGES = $nugetPackages
    $env:DOTNET_CLI_TELEMETRY_OPTOUT = '1'
    $env:DOTNET_GENERATE_ASPNET_CERTIFICATE = 'false'

    Invoke-NativeCommand -FilePath 'dotnet' -ArgumentList @(
        'publish', $appProject,
        '--configuration', $Configuration,
        '--runtime', 'win-x64',
        '--self-contained', 'true',
        '--output', $publishRoot,
        ('-p:Version=' + $Version),
        '-p:DebugSymbols=false',
        '-p:DebugType=None'
    )

    $publishedExecutable = Join-Path $publishRoot 'Vlytics.Settings.exe'
    if (-not (Test-Path -LiteralPath $publishedExecutable -PathType Leaf)) {
        throw "Published application is missing Vlytics.Settings.exe: $publishRoot"
    }
    $selfCheck = Start-Process -FilePath $publishedExecutable -ArgumentList '--self-check' -WindowStyle Hidden -Wait -PassThru
    if ($selfCheck.ExitCode -ne 0) { throw 'Published application self-check failed.' }
    Copy-DirectoryChecked -Source $publishRoot -Destination $appPayloadRoot
    Copy-FileChecked -Source (Join-Path $scriptRoot 'Vlytics.Installer\THIRD-PARTY-NOTICES.md') -Destination (Join-Path $appPayloadRoot 'THIRD-PARTY-NOTICES.md')

    $directoryPayloads = @(
        @{ Source = 'backend\src'; Destination = 'backend\src' },
        @{ Source = 'backend\migrations'; Destination = 'backend\migrations' },
        @{ Source = 'contracts'; Destination = 'contracts' },
        @{ Source = 'infra\postgres-init'; Destination = 'infra\postgres-init' }
    )
    foreach ($payload in $directoryPayloads) {
        Copy-DirectoryChecked -Source (Join-Path $repositoryRoot $payload.Source) -Destination (Join-Path $runtimePayloadRoot $payload.Destination)
    }

    $filePayloads = @(
        'backend\pyproject.toml',
        'backend\uv.lock',
        'config\variants.home.toml',
        'config\source.toml',
        'infra\compose.home.yaml',
        'infra\.env.home.example',
        'infra\operational.home.example.toml',
        'infra\live-dry-run-evidence.example.json',
        'infra\nginx.operator-ingress.conf',
        'infra\scripts\compile_desktop_settings.py'
    )
    foreach ($relativePath in $filePayloads) {
        Copy-FileChecked -Source (Join-Path $repositoryRoot $relativePath) -Destination (Join-Path $runtimePayloadRoot $relativePath)
    }

    Get-ChildItem -LiteralPath (Join-Path $repositoryRoot 'infra\scripts') -Filter '*.ps1' -File | Sort-Object Name | ForEach-Object {
        $destination = Join-Path $runtimePayloadRoot ('infra\scripts\' + $_.Name)
        Copy-FileChecked -Source $_.FullName -Destination $destination
    }

    $forbiddenPayload = @(Get-ChildItem -LiteralPath $payloadRoot -File -Recurse | Where-Object {
        $relativePayloadPath = $_.FullName.Substring($payloadRoot.Length).TrimStart('\')
        $_.Name -eq '.env' -or
        $_.Extension -in @('.pyc', '.pyo', '.pdb') -or
        $relativePayloadPath -match '(^|[\\/])(tests|__pycache__|\.mypy_cache|\.pytest_cache|\.ruff_cache|\.venv|bin|obj|artifacts)([\\/]|$)'
    })
    if ($forbiddenPayload.Count -gt 0) {
        throw "Forbidden files reached the installer payload: $($forbiddenPayload[0].FullName)"
    }

    Write-GeneratedPayload -PayloadRoot $payloadRoot -OutputPath $generatedPayloadPath
    Invoke-NativeCommand -FilePath 'dotnet' -ArgumentList @(
        'restore', $installerProject,
        '--packages', $nugetPackages,
        ('-p:ProductVersion=' + $Version),
        ('-p:GeneratedPayloadPath=' + $generatedPayloadPath)
    )
    Invoke-NativeCommand -FilePath 'dotnet' -ArgumentList @(
        'build', $installerProject,
        '--configuration', $Configuration,
        '--no-restore',
        '--output', $msiBuildRoot,
        ('-p:ProductVersion=' + $Version),
        ('-p:GeneratedPayloadPath=' + $generatedPayloadPath)
    )
}
finally {
    $env:DOTNET_CLI_HOME = $previousDotnetCliHome
    $env:NUGET_PACKAGES = $previousNugetPackages
    $env:DOTNET_CLI_TELEMETRY_OPTOUT = $previousTelemetry
    $env:DOTNET_GENERATE_ASPNET_CERTIFICATE = $previousCertificateGeneration
}

$baseName = "Vlytics-Settings-$Version-win-x64"
$builtMsi = Join-Path $msiBuildRoot ($baseName + '.msi')
if (-not (Test-Path -LiteralPath $builtMsi -PathType Leaf)) {
    throw "MSI output was not produced: $builtMsi"
}

$finalMsi = Join-Path $artifactRoot ($baseName + '.msi')
$finalZip = Join-Path $artifactRoot ($baseName + '.zip')
Copy-Item -LiteralPath $builtMsi -Destination $finalMsi -Force
Copy-DirectoryChecked -Source $appPayloadRoot -Destination (Join-Path $portableRoot 'app')
Copy-DirectoryChecked -Source $runtimePayloadRoot -Destination (Join-Path $portableRoot 'runtime')
Compress-Archive -Path (Join-Path $portableRoot '*') -DestinationPath $finalZip -CompressionLevel Optimal -Force

$manifestPath = Join-Path $artifactRoot 'SHA256SUMS.txt'
$manifestLines = @($finalMsi, $finalZip) | ForEach-Object {
    $hash = Get-FileHash -LiteralPath $_ -Algorithm SHA256
    '{0}  {1}' -f $hash.Hash.ToLowerInvariant(), (Split-Path -Leaf $_)
}
[System.IO.File]::WriteAllLines($manifestPath, $manifestLines, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Windows installer artifacts are available in $artifactRoot"
