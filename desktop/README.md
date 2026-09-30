# Vlytics Windows 설정 앱

Vlytics 설정 앱은 개인용 Windows 호스트에서 로컬 배포 설정과 비밀을 관리하고, 검증된 Docker Compose 구성을 실행한다. MSI는 현재 사용자 전용으로 `%LocalAppData%\Programs\Vlytics`에 설치되며 관리자 권한을 요구하지 않는다. 실행 파일은 `app`, 런타임은 그 형제 디렉터리인 `runtime`에 둔다.

## 설치 및 제거

`Vlytics-Settings-<version>-win-x64.msi`를 실행하면 앱과 자체 포함 .NET 10 Windows Desktop 런타임, 그리고 검증에 필요한 Vlytics 런타임 파일이 설치된다. 시작 메뉴의 **Vlytics Settings**에서 앱을 실행할 수 있다.

설정, 암호화한 비밀, 생성한 Python 환경, uv 캐시, 적용한 배포 세대는 `%LocalAppData%\Vlytics`에 저장된다. 이 디렉터리는 설치 디렉터리 밖에 있으며 MSI 제거와 업그레이드에서 보존된다. 앱 제거 후 데이터를 지우려면 사용자가 해당 디렉터리를 별도로 삭제해야 한다.

설치 프로그램은 자동 시작, Docker Desktop·WSL 설치, 방화벽 변경, 서비스 등록, 재부팅 또는 컨테이너 실행을 수행하지 않는다. 낮은 버전 MSI로의 다운그레이드는 거부한다.

## 실행 전 준비 사항

다음 도구를 PATH에서 사용할 수 있어야 한다.

- Windows 11과 WSL 2 백엔드를 사용하는 Docker Desktop. Docker Desktop은 Linux 컨테이너 모드여야 한다.
- Docker Compose 2.33.1 이상.
- Python 3.12 또는 3.13과 uv 0.12.5. 앱은 `uv --project <설치 경로>\runtime\backend --frozen`을 사용하며 가상 환경은 `%LocalAppData%\Vlytics\python-runtime`에 만든다.

앱에서 아래 값을 모두 확정한 뒤 **적용**, **검증**, **시작** 순서로 진행한다.

- 일곱 이미지 키 `VLYTICS_BACKEND_IMAGE`, `VLYTICS_FRONTEND_IMAGE`, `VLYTICS_NODE_BUILD_IMAGE`, `VLYTICS_NGINX_RUNTIME_IMAGE`, `VLYTICS_POSTGRES_IMAGE`, `VLYTICS_PYTHON_BUILD_IMAGE`, `VLYTICS_UV_BUILD_IMAGE`. 모든 값은 `repository/image@sha256:<64자리 소문자 hex>` 형식의 변경 불가능한 digest여야 한다.
- 선택한 Provider의 실제 `ModelId`, smoke 검증에서 반환된 정확한 `PinnedModelVersion`, 그리고 모델 검증 완료 상태. `__REQUIRED_AFTER_MODEL_SMOKE__`와 `__UNRESOLVED_OP_003__`는 실행 가능한 값이 아니다.
- 검토한 입력·출력 토큰 가격, KRW/USD 환율, 호출·토큰·일/월 예산 제한.
- 현재 설정과 일치하며 24시간 이내에 생성된 dry-run 증거. 증거의 `config_sha256`, `completed_at`, `source_sync_verified`, `freeze_verified`, `providers_verified`가 현재 구성과 검증 결과를 나타내야 한다. 포함된 `live-dry-run-evidence.example.json`은 형식 예시일 뿐 실행 증거가 아니다.
- 선택한 Provider API 키. 데이터베이스 역할 암호와 운영자 토큰은 앱이 생성한다.

비밀은 Windows DPAPI의 현재 사용자 범위로 암호화한다. 암호화 파일을 다른 Windows 사용자나 다른 컴퓨터로 복사해도 복호화할 수 없으므로, 이동 후에는 원래 값을 새 사용자 계정에서 다시 입력해야 한다. 비밀은 설치 폴더나 생성되는 `runtime.env`에 기록하지 않는다.

브라우저에서 사용할 운영자 비밀번호는 본인이 기억할 값을 앱에서 지정한다. 자동 생성한 운영자 토큰은 화면에 표시하지 않는다. 기존 DB를 사용하는 경우 고급 DB 항목에 기존 역할별 비밀번호를 입력한다. 빈 비밀번호 칸은 기존 값을 유지한다. SSH 항목은 접속 안내용이며 앱의 서비스 제어는 설치된 Windows 호스트에서 실행된다.

`시작`은 위 조건을 모두 검증한 다음에만 Compose 스택을 시작한다. 설치 프로그램 빌드와 앱 자체 점검은 Docker 서비스 배포가 성공했다는 증거가 아니다.

## 로컬 빌드

.NET SDK 10이 설치된 Windows PowerShell 5.1에서 저장소 루트를 기준으로 실행한다.

```powershell
.\desktop\build-installer.ps1 -Version 1.0.0
```

스크립트는 앱을 `win-x64` 자체 포함 모드로 publish하고, 명시된 런타임 허용 목록만 패키징한 뒤 다음 파일을 `artifacts\windows-installer`에 만든다.

- `Vlytics-Settings-1.0.0-win-x64.msi`
- `Vlytics-Settings-1.0.0-win-x64.zip`
- `SHA256SUMS.txt`

ZIP은 압축을 푼 디렉터리에서 `app\Vlytics.Settings.exe`를 실행하는 휴대용 레이아웃이다. MSI와 ZIP 모두 `app`과 `runtime`이 같은 상위 디렉터리 아래 있어야 한다.

버전은 `major.minor.build` 세 자리 숫자로 지정한다. MSI 제한에 따라 major/minor는 255 이하, build는 65535 이하여야 한다. 정식 배포에서는 이전 배포보다 높은 버전을 지정해야 한다.

패키징은 NuGet의 `WixToolset.Sdk` 5.0.2를 프로젝트 단위로 복원하므로 WiX를 전역 설치하지 않는다. WiX 5.0.2는 MS-RL이며 WiX 6부터 도입된 Open Source Maintenance Fee가 적용되지 않는다. 자세한 고지는 [Vlytics.Installer/THIRD-PARTY-NOTICES.md](Vlytics.Installer/THIRD-PARTY-NOTICES.md)에 있다.

## 패키지 내용 경계

설치 패키지는 publish된 앱 외에 다음 런타임 파일만 포함한다.

- `backend/src`, `backend/migrations`, `backend/pyproject.toml`, `backend/uv.lock`
- `config/variants.home.toml`, `config/source.toml`, `contracts`
- `infra/compose.home.yaml`, `infra/.env.home.example`, `infra/operational.home.example.toml`, `infra/live-dry-run-evidence.example.json`, `infra/nginx.operator-ingress.conf`, `infra/postgres-init`
- `infra/scripts/*.ps1`, `infra/scripts/compile_desktop_settings.py`

실제 `.env`, API 키, 토큰, 사용자 설정, 사용자 데이터, Python 캐시, 테스트 파일과 빌드 산출물은 포함하지 않는다.

## GitHub 자동 릴리즈

`Windows Installer` GitHub Actions가 **모든 브랜치의 모든 푸시**마다 실행된다. 문서만 바뀐 푸시도 포함한다. 태그 푸시와 PR 검증 자체는 릴리즈를 추가 생성하지 않는다.

1. workflow 실행 번호로 `1.0.<실행 번호>` 버전을 부여한다. PR 검증도 실행 번호를 사용하므로 버전 사이에 빈 번호가 있을 수 있다.
2. 설정 생성·기존 preflight 계약, Windows 보안 저장소 테스트, 네이티브 화면 자체 검증을 통과한 뒤 MSI와 ZIP을 빌드한다.
3. 푸시한 정확한 커밋에 `v1.0.<실행 번호>` 태그를 만들고 MSI·ZIP·`SHA256SUMS.txt`를 [GitHub Releases](https://github.com/jaywapp/vlytics/releases)에 등록한다.
4. `main`은 정식 릴리즈이고 다른 브랜치는 시험 릴리즈다. 정식 릴리즈 중 현재 `main` 커밋에 해당하는 빌드만 Latest로 지정해 늦게 끝난 이전 푸시가 Latest를 되돌리지 않게 한다.

실패한 빌드는 릴리즈를 게시하지 않는다. 동일 실행을 재실행하면 같은 버전을 복구하며 별도의 새 버전을 만들지 않는다. 필요한 권한은 릴리즈 job의 `contents: write`이고 내장 `GITHUB_TOKEN`을 사용하므로 PAT를 따로 입력할 필요가 없다. MSI build 번호가 65535에 도달하기 전에 버전 계열을 올려야 한다.

현재 설치 파일에는 코드 서명을 하지 않는다. 코드 서명이 필요하면 별도의 서명 인증서와 릴리즈 비밀 설정이 필요하다.
