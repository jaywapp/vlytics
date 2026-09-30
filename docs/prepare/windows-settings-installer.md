# Windows 설정 프로그램과 설치 프로그램

2026-09-30 사용자 요청: 설정 프로그램을 먼저 구현하고 설치 프로그램까지 완성한다.

## 범위와 완료 기준

정식 WPF .NET 10 프로젝트를 사용한다. OpenAI·Claude·Gemini 중 하나를 활성화하고 모델, 예산, 호출 한도, 웹 포트, 운영 이미지와 검증 파일을 설정한다. API 키와 인증·DB 비밀번호는 Windows 현재 사용자 DPAPI로 암호화한다. 설정 JSON, 명령 인수, 로그와 설치 파일에 실제 키를 넣지 않는다.

저장, 운영 파일 적용, 전체 검증, 시작·중지·상태·고정 이미지 업데이트를 구분한다. 검증과 시작에는 기존 `Invoke-Preflight.ps1`의 모델·registry·동일 설정 dry-run·시각·네트워크 검증을 그대로 적용한다. 앱은 유료 API 호출이나 소스 권리 승인을 대신하지 않는다. SSH 항목은 원격 접속 안내 정보이며 서비스 제어는 앱이 설치된 Windows 호스트에서 수행한다.

MSI는 사용자별로 앱과 운영 템플릿을 설치하고 시작 메뉴와 앱 제거 목록에 등록한다. 앱에는 .NET 런타임을 포함한다. Docker/WSL·uv·Python·고정 운영 이미지는 별도 실행 전제이며 무단 설치하지 않는다. 업그레이드와 앱 제거는 사용자 설정·키·Postgres 볼륨을 삭제하지 않는다.

## 작업 배정

| 작업 | 산출물 | 모델 | 의존 |
|---|---|---|---|
| 설정 저장·DPAPI·프로세스 실행·테스트 | `desktop/Vlytics.Settings.Core`, `desktop/Vlytics.Settings.Tests` | gpt-5.6-sol / High | 공개 인터페이스 |
| Windows 설정·운영 화면 | `desktop/Vlytics.Settings.App` | gpt-5.6-sol / High | Core 인터페이스 |
| MSI·빌드·CI·사용 설명 | `desktop/Vlytics.Installer`, `desktop/build-installer.ps1` | gpt-5.6-sol / High | 앱 publish 결과 |
| 기존 운영 검증 연결·통합 검증 | `infra/scripts/Invoke-DesktopOperation.ps1`, compiler, 솔루션 | 리더 | 각 작업 결과 |

저장소 계획의 Sol 모델 통일 결정을 유지한다. 사용자가 이미 승인한 설정 앱과 설치 앱 범위 안에서 구현한다. 실제 키 입력·유료 모델 smoke·실서비스 배포는 이번 설치 파일 제작과 구분한다.
