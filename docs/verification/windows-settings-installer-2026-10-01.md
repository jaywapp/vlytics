# Windows 설정 앱·설치 파일 검증

2026-10-01. 사용자 요청에 따라 정식 Windows 설정 앱과 MSI를 구현하고 모든 브랜치 푸시의 GitHub 자동 릴리즈를 연결했다.

## 구현·확인 사항

- WPF .NET 10 솔루션, Core·App·Tests와 별도 WiX 5.0.2 MSI 프로젝트.
- OpenAI·Claude·Gemini 선택, 모델·가격·환율·예산·호출·토큰 한도와 고정 이미지 설정.
- API·인증·역할별 DB 비밀번호는 DPAPI 현재 사용자 암호화와 현재 사용자 전용 ACL로 보호한다. JSON·명령 인수·`runtime.env`·설치 패키지에는 실제 키를 넣지 않는다.
- Apply·Stop·Status는 vault를 복호화하지 않는다. Validate·Start·Update는 선택한 Provider와 DB·인증 비밀만 전달한다. 임의 프로세스 출력은 화면에 반환하지 않는다.
- 저장·운영 파일 준비·검증·시작·중지·상태·고정 이미지 업데이트를 구분한다. 미저장 변경을 차단하고, 앱 종료 시 자식 프로세스를 정리한 뒤 종료한다.
- 기존 preflight에 프로세스 비밀 주입을 추가했다. 모델·registry hash·같은 설정의 dry-run·시각·외부 접속 제한을 유지한다. 시작 전 검증 실패는 Docker 변경을 차단한다.
- 실제 DB 역할 `vlytics_migrator`·`vlytics_*_login`과 `postgresql+psycopg` URL을 사용한다. 검증 통과 후 worker·API를 중지하고 마이그레이션·시작을 수행한다. DB 볼륨은 삭제하지 않는다.
- MSI는 사용자별 앱·운영 템플릿과 .NET 런타임을 설치한다. OS 서비스·자동 시작·Docker/WSL 설치·방화벽 변경·컨테이너 실행 custom action은 없다.
- 푸시마다 `1.0.<workflow 실행 번호>` MSI·ZIP·SHA256을 생성한다. PR은 검증만 수행하고, 기본 브랜치 외의 푸시는 시험 릴리즈로 게시한다. 재실행은 같은 버전을 복구한다.

## 실행한 검증

| 검증 | 결과 |
|---|---|
| backend 전체 pytest | 522 통과, DB 연동 조건 56 건너뜀 |
| 추가·기존 운영 계약 pytest | 25 통과: 3 Provider의 실제 worker 계약, DB 계정/driver, preflight 전 Docker 변경 차단 포함 |
| Core DPAPI·설정·프로세스 테스트 | 23 통과 |
| Release 솔루션 빌드 | 경고 0, 오류 0 |
| 네이티브 자체 점검 | 종료 코드 0: 4탭 레이아웃, 저장, Provider 변경 시 검증 초기화 |
| WPF 자체 렌더링 | 키 없는 격리 화면 PNG 확인 |
| MSI·ZIP 생성 | 실제 파일 생성, SHA256 manifest 포함 |
| WiX ICE | 경고 0, 오류 0. 고정 per-user 범위에 부적합한 ICE91만 지정 제외, ICE38/ICE64 및 나머지 검증 유지 |
| Python Ruff·포맷, PS 5.1 구문 | 통과 |

전체 backend 실행 후 Windows 시작 보호 테스트 2개를 추가했고 별도 운영 계약 25개 실행에서 확인했다. Linux backend CI에서는 Windows 전용 시작 테스트 2개만 제외하며 Windows 설치 workflow에서는 실행한다.

## 실제 운영과 구분

실제 API 키·유료 호출·모델 smoke·host 배포·실제 설치/제거는 수행하지 않았다. 로컬에 Docker가 없어 실제 컨테이너 시작은 미검증이며 합성 프로세스로 순서와 차단 동작을 확인했다. Windows Computer Use는 sandbox helper 초기화 실패로 사용하지 못했고, 앱 자체 렌더링·저장/변경 검증으로 확인했다.

Docker Desktop·uv/Python·고정 이미지·확인한 모델/가격/환율·동일 설정의 최신 dry-run과 키를 준비해야 서비스를 시작할 수 있다. 소스 수집은 기존 미해결 권리 검증 상태를 유지한다. 설치 파일은 현재 코드 서명하지 않는다.

GitHub Actions의 최초 실행·Release 게시 결과는 원격 실행에서 추가 확인한다. 사용법과 자동 릴리즈 정책은 [Windows 안내](../../desktop/README.md)를 따른다.
