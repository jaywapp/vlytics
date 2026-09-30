# 홈 운영 결정 반영 검증 — 2026-09-30

대상 브랜치: `codex/home-server-operations` · 기준 `main`: `3db2830`. 아래 검증은 커밋 전 로컬 실행 기준이며, 이후 커밋·push·병합과 CI 상태는 GitHub PR 기록에서 확인한다. 실제 키·API 호출·운영 서버 배포는 수행하지 않았다.

## 반영한 범위

- 별도 `personal_home` 프로필과 홈 Compose/env/운영 TOML/Provider catalogue를 추가했다. 기존 공용 production 템플릿은 유지한다.
- 전용 개인 장비와 사설망 접근 조건에서 호스팅 비용 0, backup/PITR·외부 알림 비활성을 허용한다. standard 프로필의 기존 백업·알림 조건은 유지한다.
- 활성 Provider만 키·모델·예산·실행 binding·dry-run 증거에 포함한다. 현재 홈 패키지는 OpenAI 하나를 선택하며, 향후 Anthropic·Gemini의 전환·병행도 같은 계약으로 구성할 수 있다.
- OpenAI 월 예산 KRW 10,000, 일 예산 KRW 1,000과 호출/token 상한을 홈 예제로 구성했다. USD 가격을 검토한 명시적 환산계수로 KRW 예약·정산에 적용한다.
- 월 원장 통화 혼합을 차단하고 비용을 DB `numeric(20,8)`에 맞춰 올림해 예약·정산·재시도 비교를 일치시켰다.
- 최종 Provider TOML 해시를 `activation.provider_registry_sha256`에 연결해 가격·전역 cap 변경 후 기존 dry-run 증거를 재사용하지 못하게 했다.
- 실제 모델 버전 확인 전 placeholder와 `op003_resolved=false`를 유지한다. 별칭 응답을 고정 버전으로 임의 인정하지 않는다.
- 사용자의 후속 직접 승인에 따라 OpenAI worker proxy·Compose 연결을 적용했다. Provider별 제한 CONNECT relay는 해당 Provider의 고정 API host:443만 허용한다. worker는 내부망에만 남고 relay만 전용 외부망에 가입하며, 준비된 relay의 healthcheck 뒤에 시작한다.
- Claude·Gemini 추가에 필요한 relay selector와 고정 내부 proxy 경로를 준비했다. 현재 Compose에는 해당 relay·키·외부망이 없고, 활성 Provider 설정과 배선이 어긋나면 시작 전 검사가 거부한다. 비활성 Provider 흔적, 임의 proxy, worker 외부망, relay의 호스트 포트·파일 mount·credential 주입도 차단한다.
- CI에 홈 배포 패키지 검사를 추가했다. 실제 CI 실행 결과는 push 이후 별도 확인 대상이다.
- relay 전용 외부망에 `gw_priority: 1`을 지정하고 시작 전 검사에서도 요구한다. 홈 host의 Docker Compose 최소 버전은 2.33.1로 기록했다. [Docker 공식 기준](https://docs.docker.com/reference/compose-file/services/#gw_priority)을 확인했다.
- 사용자 `내 결정` 원문을 보존하고 운영 문서·계획·마지막 설정 체크리스트를 동기화했다.
- 병합 전 CI에서 발견한 production 합성 설정의 해시 불일치를 수정했다. 합성 TOML에 `deployment.profile = "standard"`를 명시하고, 실제 CI 증거 생성 코드를 실행한 뒤 worker 설정·dry-run 검증 함수로 일치 여부를 확인하는 회귀 검사를 추가했다.

## 실행한 검증

| 검사 | 결과 |
| --- | --- |
| 전체 backend pytest | 510 passed, 56 skipped |
| 최종 gateway·health dependency·relay mount·StrictMode 보강 뒤 배포 회귀 | 11 passed |
| CI 합성 설정·증거 해시 보강 뒤 배포 회귀 | 12 passed |
| Provider transport·relay·배포 격리 대상 검사 | 75 passed, 위 전체 검사에 포함 |
| Ruff lint / format | 통과 |
| mypy | 84개 소스 파일 통과 |
| standard/home 배포 패키지 정적 검사 | Windows PowerShell 5.1에서 모두 통과 |
| 홈 YAML 구조 검사 | gateway 우선순위·worker 내부망·logging options 확인 |
| LF/CRLF 및 standard/home 패키지 회귀 | 전체 pytest에 포함, 통과 |
| 사용자 결정 원문·문서 상대 링크 | 보존·유효 확인 |
| git diff --check | 통과 |

이 장비에는 Docker CLI가 없어 실제 Compose 해석·이미지 기동은 검사하지 못했다. PostgreSQL fixture 등 환경을 요구하는 56개 테스트는 skip됐다. 프런트엔드 코드는 수정하지 않았으며 이전 CI 기록을 새 커밋의 CI 성공으로 표시하지 않았다. 실제 KOVO·OpenAI 통신, 운영 서버 접속, 유료 요청은 수행하지 않았다.

## 남은 게이트

1. 실제 키는 사용자가 마지막에 비밀 저장소 또는 제한된 untracked 파일에 넣는다. 홈 프로필에는 OpenAI key, 서로 다른 operator/readonly 인증값, 역할별 DB 자격 증명이 필요하다.
2. 실제 홈 서버의 Docker/Compose·시계·SSH·디스크·네트워크 격리와 이미지 증거를 확인한다. 저장소 배선 구현 완료는 실서버 기동 성공을 뜻하지 않는다.
3. OpenAI 계정 접근·응답 모델 버전·가격·환산계수와 최종 registry 해시를 확정하고 동일 설정의 fresh dry-run 증거를 생성한다. 검증 가능한 모델 버전이 없으면 strict cohort AI는 계속 보류한다.
4. Anthropic·Gemini 활성화 시 선택과 예산 배분을 기록하고 해당 Provider의 key·model/version·가격·설정·배선 및 fresh evidence를 함께 갱신한다. 현재 OpenAI 연결 승인은 [구체 구현 기록](../operations/openai-egress-proposal.md)에 반영했으며 승인 차단은 해소됐다.
5. KOVO 이용 근거·허용 요청량·필드 의미를 확인하기 전 실제 수집은 비활성이다. 이 승인은 사용자 GO나 OpenAI 연결 승인으로 대체되지 않는다.

내부 원장 cap은 실제 청구 총액 보장이 아니다. 실제 usage가 최대 예약을 넘으면 이미 발생한 비용을 보존해 정산하고 이후 호출을 차단하는 기존 정책을 유지한다. 환율·세금·다른 앱의 API 사용·가격 변경은 운영 계정에서도 확인한다.
