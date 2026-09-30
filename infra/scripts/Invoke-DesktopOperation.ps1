[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][ValidateSet('Validate','Apply','Start','Stop','Status','Update')][string]$Action,
    [Parameter(Mandatory = $true)][string]$SettingsFile,
    [Parameter(Mandatory = $true)][string]$DataDirectory
)
Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
. (Join-Path $PSScriptRoot 'DesktopEnvironment.ps1')
$stage = 'settings'
$operationLock = $null
function Invoke-DockerChecked {
    param([string[]]$Arguments)
    & docker @Arguments 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw 'Docker operation failed.' }
}
try {
    # Load the OS module explicitly when launched from a parent PowerShell 7 environment.
    Import-Module (Join-Path $PSHOME 'Modules\Microsoft.PowerShell.Utility\Microsoft.PowerShell.Utility.psd1') -ErrorAction Stop
    $settings = Get-Content -LiteralPath $SettingsFile -Raw -Encoding UTF8 | ConvertFrom-Json
    $root = [IO.Path]::GetFullPath([string]$settings.RuntimeRoot)
    $DataDirectory = [IO.Path]::GetFullPath($DataDirectory)
    $operationLock = [IO.File]::Open((Join-Path $DataDirectory 'operation.lock'), 'OpenOrCreate', 'ReadWrite', 'None')
    $stage = 'docker'
    if ($null -eq (Get-Command docker -ErrorAction SilentlyContinue) -and $Action -ne 'Apply') { throw 'Docker missing.' }
    if ($Action -in @('Stop','Status')) {
        $ids = @(& docker ps -q --filter 'label=com.docker.compose.project=vlytics' 2>$null)
        if ($LASTEXITCODE -ne 0) { throw 'Docker unavailable.' }
        if ($Action -eq 'Stop' -and $ids.Count -gt 0) { Invoke-DockerChecked -Arguments (@('stop') + $ids) }
        $message = if ($Action -eq 'Stop') { '서비스를 중지했습니다. 데이터는 보존됩니다.' } else { "실행 중인 Vlytics 컨테이너: $($ids.Count)개" }
    }
    else {
        $stage = 'python'
        if ($null -eq (Get-Command uv -ErrorAction SilentlyContinue)) { throw 'uv missing.' }
        $env:UV_PROJECT_ENVIRONMENT = Join-Path $DataDirectory 'python-runtime'
        $env:UV_CACHE_DIR = Join-Path $DataDirectory 'uv-cache'
        $generationRoot = Join-Path $DataDirectory 'deployments'
        [IO.Directory]::CreateDirectory($generationRoot) | Out-Null
        $currentFile = Join-Path $DataDirectory 'applied-deployment.txt'
        if ($Action -in @('Apply','Validate')) {
            $generation = Join-Path $generationRoot ([Guid]::NewGuid().ToString('N'))
            $stage = 'compile'
            & uv run --project (Join-Path $root 'backend') --frozen python (Join-Path $PSScriptRoot 'compile_desktop_settings.py') $SettingsFile $generation 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw 'Compilation failed.' }
            if ($Action -eq 'Apply') {
                $temporaryPointer = "$currentFile.new"
                [IO.File]::WriteAllText($temporaryPointer, $generation)
                if (Test-Path -LiteralPath $currentFile) { [IO.File]::Replace($temporaryPointer, $currentFile, $null) }
                else { [IO.File]::Move($temporaryPointer, $currentFile) }
                $message = '운영 설정 파일을 준비했습니다. 시작 전 전체 검증을 수행합니다.'
            }
        }
        else {
            $stage = 'apply'
            $generation = [IO.File]::ReadAllText($currentFile).Trim()
            $expectedParent = [IO.Path]::GetFullPath($generationRoot).TrimEnd('\') + '\'
            if (-not [IO.Path]::GetFullPath($generation).StartsWith($expectedParent, [StringComparison]::OrdinalIgnoreCase)) { throw 'Invalid deployment path.' }
            $manifest = Get-Content -LiteralPath (Join-Path $generation 'manifest.json') -Raw | ConvertFrom-Json
            $sha = (Get-FileHash -LiteralPath $SettingsFile -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($manifest.settingsSha256 -ne $sha) { throw 'Settings changed after apply.' }
        }
        if ($Action -ne 'Apply') {
            $stage = 'secrets'
            foreach ($role in @('MIGRATOR','COLLECTOR','ENGINE','MARKET_INGEST','READ_API')) {
                $password = [Environment]::GetEnvironmentVariable("${role}_DATABASE_PASSWORD", 'Process')
                if ([string]::IsNullOrWhiteSpace($password)) { throw 'Database secret missing.' }
                $url = Get-DesktopRoleDatabaseUrl -Role $role -Password $password
                [Environment]::SetEnvironmentVariable("${role}_DATABASE_URL", $url, 'Process')
            }
            $compose = Join-Path $generation 'compose.yaml'
            $environment = Join-Path $generation 'runtime.env'
            $stage = 'model'
            if (-not $settings.ModelVerified -or [string]::IsNullOrWhiteSpace([string]$settings.PinnedModelVersion)) { throw 'Model smoke required.' }
            $stage = 'pricing'
            if ($settings.PricingToBudgetRate -le 0 -or $settings.InputPrice -le 0 -or $settings.OutputPrice -le 0) { throw 'Reviewed pricing required.' }
            $stage = 'key'
            $providerKey = 'VLYTICS_' + ([string]$settings.Provider).ToUpperInvariant() + '_API_KEY'
            if ([string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable($providerKey, 'Process'))) { throw 'Provider key required.' }
            $stage = 'evidence'
            if (-not (Test-Path -LiteralPath (Join-Path $generation 'evidence.json') -PathType Leaf)) { throw 'Evidence required.' }
            $composeArgs = @('compose','--project-name','vlytics','--env-file',$environment,'--file',$compose)
            if ($Action -eq 'Update') {
                $stage = 'pull'
                Invoke-DockerChecked -Arguments ($composeArgs + @('pull'))
            }
            $stage = 'preflight'
            & (Join-Path $PSScriptRoot 'Invoke-Preflight.ps1') -EnvironmentFile $environment -OperationalConfig (Join-Path $generation 'operational.toml') -DryRunEvidence (Join-Path $generation 'evidence.json') -ComposeFile $compose -Profile personal_home -UseProcessSecrets *> $null
            if ($Action -in @('Start','Update')) {
                $stage = 'start'
                Invoke-DockerChecked -Arguments ($composeArgs + @('stop','worker','api'))
                Invoke-DockerChecked -Arguments ($composeArgs + @('up','-d','--wait','postgres'))
                Invoke-DockerChecked -Arguments ($composeArgs + @('run','--rm','migrate'))
                Invoke-DockerChecked -Arguments ($composeArgs + @('up','-d','--wait','--remove-orphans'))
                $message = '검증을 통과하고 서비스를 시작했습니다.'
            }
            else { $message = '전체 운영 검증을 통과했습니다. 유료 API 호출은 수행하지 않았습니다.' }
        }
    }
    @{ success = $true; message = $message } | ConvertTo-Json -Compress
}
catch {
    $messages = @{
        settings = '설정 파일을 읽지 못했거나 다른 작업이 진행 중입니다.'
        docker = 'Docker Desktop의 Linux 컨테이너 엔진과 Compose를 확인하세요.'
        python = 'uv와 Python 3.12~3.13 실행 환경을 준비하세요.'
        compile = '설정 파일 생성에 실패했습니다. 이미지 digest, 경로, 예산을 확인하세요.'
        apply = '저장한 설정을 먼저 적용하세요. 저장 이후에는 다시 적용해야 합니다.'
        secrets = 'Windows 보안 저장소의 데이터베이스 키를 확인하세요.'
        model = '실제 모델 smoke 검증 후 확인된 모델 버전과 검증 완료 여부를 설정하세요.'
        pricing = '확인한 입력·출력 토큰 가격과 KRW/USD 환율을 입력하세요.'
        key = '선택한 Provider의 API 키를 보안 저장소에 저장하세요.'
        evidence = '현재 설정으로 검증한 dry-run 증거 파일을 선택하고 다시 적용하세요.'
        pull = '고정한 이미지 다운로드에 실패했습니다. 이미지 주소와 Docker 접속을 확인하세요.'
        preflight = '운영 검증이 실패했습니다. 모델 검증·가격/환율·키·동일 설정의 24시간 내 dry-run·이미지·시각 동기화를 확인하세요.'
        start = '서비스 시작에 실패했습니다. 컨테이너 상태를 확인하세요. 데이터는 보존됩니다.'
    }
    @{ success = $false; message = $messages[$stage] } | ConvertTo-Json -Compress
    exit 1
}
finally {
    if ($null -ne $operationLock) { $operationLock.Dispose() }
}
