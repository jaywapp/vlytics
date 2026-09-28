# 전체 프로젝트 재점검 — 2026-09-28

## 기준과 결론

- 대상: `vlytics`, `codex/project-audit-20260927`, HEAD `5bc34999fe7453f227aba10649c53134f042ae0a`.
- PR: [#12](https://github.com/jaywapp/vlytics/pull/12), Open/Draft.
- 동일 HEAD의 [CI 36402649306](https://github.com/jaywapp/vlytics/actions/runs/36402649306)는 backend/frontend/deployment-windows 모두 성공했다.
- 판정: **정상 경로 검증은 통과했지만, 장애 처리·동시성·운영 상태 판정에서 P1 2건과 P2 6건이 남아 있다.** 코드 보완과 실제 운영 검증이 모두 필요하다.
- 이번 작업은 재감사다. 제품 코드·배포 설정은 수정하지 않았으며 이 보고서만 추가했다.

## 점검 범위와 검증

Sol 검토 에이전트 3개가 예측/Feature/Market/평가, API/화면, 배포/보안/복구를 읽기 전용으로 검토했다. 메인 에이전트는 워커/스케줄러/수집을 점검하고 주요 발견의 원문과 재현 결과를 교차 확인했다.

| 검증 | 결과 | 한계 |
|---|---|---|
| 로컬 backend pytest | 401 passed, 53 skipped, 4 warnings | PostgreSQL 검사는 로컬 DB 부재로 건너뜀 |
| 로컬 Ruff 및 mypy | 통과, mypy 83 source files | 정적 검사 |
| 로컬 frontend | 32 tests, typecheck, lint 통과 | 이번에는 브라우저를 새로 실행하지 않음 |
| 동일 HEAD CI | 전체 성공, PostgreSQL 포함 backend 454 tests, Compose/복구/browser/image gate 성공 | 합성 데이터/환경이며 실제 source·유료 Provider·운영 host 증거가 아님 |
| 장애 경로 재현 | 실제 모듈과 test double을 이용한 짧은 Python/ASGI/TS 재현 | 아래 각 항목에서 정적 근거와 실행 재현을 구분 |

## 우선 처리할 코드 결함

### R10 · P1 · 수집의 정상적인 실패가 워커 전체를 종료

**위치:** `backend/src/vlytics/mirror/kovo.py:264`, `:296`, `:302`; `backend/src/vlytics/mirror/backfill.py:527`; `backend/src/vlytics/ops/scheduler.py:814`; `backend/src/vlytics/worker.py:138`.

`KovoSyncJobHandler`는 전역 source lock을 얻지 못한 `ScopeLeaseUnavailable`, 429/5xx 재시도 소진의 `SourceRequestFailed`를 그대로 전파한다. production dispatcher는 `RetryableJobError`와 `TerminalJobError`만 처리한다. 기본 동시 작업 수는 4지만 source lock은 전역 1개이므로, 같은 poll에 수집 작업이 둘 이상 있으면 외부 장애 없이도 이 경로가 발생할 수 있다.

**영향:** 작업이 retry_wait/failed로 기록되지 않고 running lease로 남으며 `future.result()`와 `Worker.run()`까지 예외가 전파된다. 재시작 후 같은 작업을 반복 회수할 수 있고 예측 작업 처리도 영향을 받는다.

**재현:** 실제 `PredictionJobDispatcher._execute()`에 두 source 예외를 각각 던지는 handler를 주입했다. 두 경우 모두 원래 예외가 전파됐고 작업 상태를 기록하는 DB transaction 호출은 0회였다.

**권장 수정:** handler 경계에서 source lock 경합/일시 통신 오류를 재시도 예외로, 잘못된 scope/payload/영구 계약 오류를 격리 예외로 분류한다. 실제 PostgreSQL에서 같은 poll의 수집 2개와 예측 1개, 429 소진을 함께 검증한다.

### R11 · P1 · 격리된 불량 예측이 같은 경기의 정상 평가까지 중단

**위치:** `backend/src/vlytics/engine/evaluation/runtime.py:147`, `:239`, `:329`, `:423`; `backend/src/vlytics/engine/market/runtime.py:126`, `:308`.

평가 planner는 Market 비교가 있는 개별 예측을 기준으로 작업을 만든다. 그러나 reader는 같은 경기의 모든 published 예측을 다시 읽는다. 다른 예측이 hash 불일치로 Market 작업에서 격리됐다면, reader의 missing 비교 생성 경로가 그 예측을 다시 읽고 `MarketContractError`를 발생시킨다. Evaluation handler는 이를 작업 격리 예외로 변환하지 않는다.

**영향:** 불량 예측 하나 때문에 정상 예측을 포함한 경기 평가 transaction 전체가 rollback된다. 오류가 dispatcher로 전파돼 워커도 중단될 수 있다. Market 단계의 격리만으로는 장애가 분리되지 않는다.

**근거:** planner의 Market EXISTS는 trigger 예측에 적용되지만 reader SELECT에는 같은 조건/trigger 제한이 없다. reader는 모든 work item을 생성한 뒤 batch를 평가하며 handler는 payload 파싱 ValueError만 처리한다. 여러 모듈을 잇는 정적 경로를 두 검토자가 확인했다. 이 조합의 실제 PostgreSQL 장애 재현은 이번 점검에서 실행하지 않았다.

**권장 수정:** trigger_prediction_id 단위 또는 검증된 Market 비교가 있는 예측만 평가하도록 작업/reader 계약을 일치시킨다. 예측별 실패 격리와 정상 예측 평가 보존을 검증한다. handler 예외 변환만 추가하면 경기 전체 평가가 빠지는 문제는 남는다.

### R12 · P2 · 긴 수집 작업과 고정 2분 lease가 충돌

**위치:** `backend/src/vlytics/ops/scheduler.py:761`, `:853`; `backend/src/vlytics/mirror/backfill.py:515`; `backend/src/vlytics/worker.py:519`.

production dispatcher는 2분 lease를 사용하며 갱신 경로가 없다. KOVO recurring sync는 기본 page size 2로 모든 페이지를 한 작업에서 처리하고 속도 제한·네트워크·재시도 대기를 포함한다. 예시 결과 poll interval은 300초이므로 작업 deadline이 lease보다 늦을 수 있다.

**영향:** 정상 수집이 120초를 넘으면 DB finish의 `lease_until > completed_at` 조건에 실패한다. dispatcher는 이를 RuntimeError로 전파한다. 다른 worker가 있으면 같은 작업의 재회수도 가능하다. `sync_once`는 cursor를 매번 처음부터 시작하므로 긴 수집의 재실행 비용이 반복된다.

**재현/근거:** 실제 dispatcher에 121초 후 완료 시각과 finish 실패를 주입하면 `job lease was lost before completion`이 발생한다. DB 거부 조건과 갱신 부재는 코드로 확인했다. 실제 121초 네트워크 수집을 수행한 재현은 아니다.

**권장 수정:** 소유권을 검증하는 lease 갱신 또는 페이지별 bounded 작업·영속 cursor를 도입한다. 단순히 lease를 임의로 늘리기보다 작업 상한과 복구 단위를 일치시킨다. 긴 수집이 T-60 예약을 막지 않는지도 함께 확인한다.

### R13 · P2 · 속도 제한 대기 후 deadline이 지난 요청을 전송

**위치:** `backend/src/vlytics/mirror/backfill.py:536`, `:622`.

페이지 진입에서는 deadline을 확인하지만 `_fetch_with_retry()`는 rate limiter 대기 후 시각을 읽고 바로 요청한다. 재시도 반복에서도 요청 직전 deadline 재검사가 없다.

**영향:** 요청 속도 제한이나 재시도 대기가 남은 시간을 넘기면 작업 deadline 이후에도 외부 요청을 시작한다. T-60 이전 동기화 창을 넘긴 불필요한 요청과 작업 지연이 발생한다. 이 사실만으로 Feature cutoff 오염이 일어난다고 단정하지 않는다.

**재현:** 실제 `_fetch_with_retry()`에 deadline +1초, limiter 대기 +10초를 주입했다. adapter가 deadline보다 9초 늦게 호출됐다.

**권장 수정:** limiter/재시도 대기 직후 각 요청 전에 deadline을 확인하고, 요청 timeout도 남은 시간으로 제한한다.

### R14 · P2 · 동기 DB 조회가 API 이벤트 루프를 차단

**위치:** `backend/src/vlytics/api/app.py:148`, `:161`; `backend/src/vlytics/api/repository.py:165`; `backend/src/vlytics/api/main.py:11`.

DB route들이 `async def` 안에서 동기 `ReadService`와 SQLAlchemy connection을 직접 실행한다. 현재 API 엔트리는 단일 uvicorn worker다.

**영향:** 느린 Performance 조회나 DB 대기 하나가 같은 프로세스의 다른 요청·health 처리를 지연시킨다. SQL 최적화만으로는 이 동시성 문제를 해결하지 못한다.

**재현:** 현재 ASGI 앱에 `load_query()`가 300ms 대기하는 합성 repository를 주입했다. 같은 loop에서 50ms에 실행하도록 예약한 callback은 약 301ms에 실행됐다. 응답은 200이었다. 소스의 동기 호출 경로를 메인 에이전트가 확인했다.

**권장 수정:** 동기 DB stack을 유지한다면 DB route를 동기 함수로 만들어 framework threadpool을 사용하거나 명시적으로 offload한다. async DB stack으로 전환하는 방법도 있다. slow-read와 health 동시 요청 회귀를 추가한다.

### R15 · P2 · 동일 역할 토큰 설정을 거부하지 않음

**위치:** `backend/src/vlytics/api/security.py:41`; `backend/src/vlytics/config.py:263`; `infra/scripts/Invoke-Preflight.ps1:72`.

operator/readonly secret이 같은 값으로 설정돼도 preflight/API 시작 시 거부하지 않는다. 인증은 operator와 먼저 비교한다.

**영향:** 두 역할에 같은 값을 복사한 배포 오설정에서는 readonly로 전달된 토큰도 operator 권한을 얻어 retry POST를 호출할 수 있다. 서로 다른 정상 토큰 설정에서의 권한 우회는 발견하지 않았다.

**재현:** 동일한 합성 SecretStr 두 개로 만든 실제 Authenticator에 해당 토큰을 전달하면 `operator`가 반환됐다.

**권장 수정:** API 시작과 배포 preflight에서 역할 토큰의 상호 불일치를 강제하고 동일값 설정 실패 회귀를 추가한다. 실제 값은 출력하지 않는다.

### R16 · P2 · 백업 원본을 잃어도 manifest만으로 정상 판정

**위치:** `backend/src/vlytics/ops/health.py:133`, `:488`.

백업 감시는 manifest의 날짜·hash 문자열·기록된 크기만 검사한다. 연관 dump의 존재·실제 크기·hash 또는 원격 object 저장소 증거는 확인하지 않는다.

**영향:** manifest가 최신이어도 dump가 삭제/손상되면 `backup_freshness=ok`가 유지된다. 현재 감시가 백업 복구 가능성까지 보증하는 것으로 사용되면 장애를 놓친다.

**재현:** 실제 dump 없이 `sha256='0'*64`, `byte_length=1`인 최신 합성 manifest가 실제 평가기에서 `ok`로 판정됐다.

**권장 수정:** local dump 존재/크기/hash를 확인하는 collector나 원격 object HEAD/checksum 증거를 추가한다. freshness와 artifact integrity를 별도 상태로 표시하고 접근 실패도 성공으로 처리하지 않는다.

### R17 · P2 · Windows 시각 점검이 명시적 비동기 상태를 통과

**위치:** `infra/scripts/Invoke-Preflight.ps1:41`.

Windows 분기는 W32Time 실행 여부, `w32tm` 종료 코드, 영어 source 이름 두 개만 확인한다. Leap Indicator, 마지막 성공 시각, offset을 확인하지 않는다.

**영향:** 서비스와 명령이 정상 실행돼도 실제로 동기화되지 않은 호스트가 ClockSynchronization=passed를 받을 수 있다. T-60와 cutoff 판단의 운영 증거로 부족하다.

**재현/근거:** `Leap Indicator: 3(not synchronized)`와 일반 원격 Source를 포함한 합성 출력은 현재 거부 정규식에 걸리지 않는다. 실제 운영 호스트 시계를 변경하거나 preflight 전체를 실행하지 않았다.

**권장 수정:** OS 언어에 의존하지 않는 상태·마지막 동기화·offset 검증을 사용하고, 비동기/오래된 동기화/과도 offset fixture를 추가한다.

## 추가 개선과 문서 정합성

- **P3 · Matches/Performance 응답 검증:** `frontend/src/features/matches/api.ts:40`은 metadata/data 키 존재만, `performance/api.ts:25`는 최상위 필드와 items 배열만 확인한다. 실제 TS client에 잘못된 200 payload를 주입하면 성공으로 수용하고, 화면 소비식 `data.items.some()` 또는 `row.cohort[...]`에서 TypeError가 난다. 현재 backend response model이 정상 생성하는 응답의 결함은 아니며, 버전 불일치/잘못된 응답에 대한 복원력 공백이다. History/Operations 수준의 소비 필드 검증과 오류 패널 회귀를 권장한다.
- **복구 ACL 검증 강화:** `backend/src/vlytics/storage/recovery.py:61`의 허용 목록은 알려진 역할과 DB 권한 이름 조합을 폭넓게 허용한다. 원본 ACL 보존이라는 현재 복구 계약을 존중하되, 원본의 권한 과잉도 복제될 수 있으므로 복구 결과에 역할별 최소권한 경고와 CREATE SCHEMA 거부 검사 추가를 권장한다. 현재 계약상 확정 결함으로 집계하지 않았다.
- **문서 정리:** `audit-remediation.md`의 최신 요약은 성공 CI를 반영했지만 B1 표에 PostgreSQL CI 대기 문구가 남아 있다. 과거 승인 거부 문구가 별도 현재형 단락에도 남아 있어 최신 결정과 혼동할 수 있다. 이력과 현재 판정을 명확히 구분할 필요가 있다.
- **기존 경고:** 테스트의 Starlette/httpx 및 HTTP_422 상수 deprecation 4건은 이번 기능 실패 원인이 아니다.

## 여전히 필요한 운영 증거

다음은 위 코드 결함과 별개다. 현재 확인되지 않았다는 사실을 구현 실패나 성공으로 바꿔 표시하지 않는다.

- 실제 운영 host와 SSH 접근, NTP/offset.
- worker의 승인된 외부 통신 목적지와 egress 경로. 현재 기본 차단은 사용자 결정에 따른 상태다.
- 외부 백업/PITR, 정기 복구 검사, 실제 알림 수신.
- OP-001~004의 source/Provider/시간·비용 정책 및 실제 소량 실행 증거.
- 검증된 KOVO roster/statistics 의미·공개 시점, Feature coverage evidence lineage 확장.
- 게시 이미지 digest 및 대상 architecture의 release 증거.
- 실제 Market adapter는 정책 버전/출처/유효기간 확정 전 차단된다. Market missing 운영은 UC-005 허용 예외이므로 필수 실제 연동으로 오인하지 않는다.

## 권장 처리 순서

1. R10/R11: 수집과 평가에서 실패한 작업·예측을 격리하고 정상 작업 지속을 실제 DB로 검증.
2. R12/R13: 수집 페이지 작업 단위·lease·deadline을 일치시켜 장시간/저속/재시도 환경 검증.
3. R14/R15: API 동시성·역할 설정 검증 보완.
4. R16/R17: 백업 및 clock 정상 판정 근거 보완.
5. 방어적 UI/ACL·문서 정리 후 전체 CI. 실제 운영 증거는 지정된 host에서 별도 확인.


## 후속 코드 조치

위 발견과 재현은 `5bc3499` 시점의 기록이다. 이후 같은 작업 브랜치에서 다음 변경을 적용했다. 최신 테스트/CI 결과는 별도 검증 기록으로 갱신하며, 실제 운영 증거가 없다는 사실은 코드 수정으로 해제하지 않는다.

| ID | 후속 변경 |
| --- | --- |
| R10 | KOVO handler에서 lock 경합·일시 source 실패를 retry 대기로, payload·영구 계약 오류를 quarantine으로 분류한다. |
| R11 | 자동 평가를 trigger prediction으로 제한하고, 수동 배치는 현재 Market 비교가 존재하는 published 예측만 읽는다. 부적격 입력은 해당 평가 job을 quarantine한다. |
| R12 | 실행 중인 job의 소유권을 확인해 lease를 갱신하고, 긴 source 작업 완료/만료 경계를 검증한다. |
| R13 | rate limiter·재시도 대기 후 요청 직전에 deadline을 다시 검사하고 요청 timeout을 남은 시간 이하로 제한한다. |
| R14 | 동기 DB 작업을 사용하는 API route를 framework threadpool에서 실행하도록 전환한다. |
| R15 | API 시작·preflight·인증 객체에서 동일 operator/readonly 토큰을 거부한다. |
| R16 | 백업 신선도와 `backup_integrity`를 분리한다. 최신 manifest에 대응하는 dump의 존재·크기·SHA-256을 확인하고 알림 전달 계약에 연결한다. |
| R17 | Windows preflight가 leap indicator·최근 동기화·실측 offset을 확인하고 증거 불충분 시 실패한다. |
| P3 UI | Matches/Performance 200 응답의 화면 소비 필드를 검증해 잘못된 payload를 오류 상태로 처리한다. |
| 복구 ACL | 원본 ACL의 비마이그레이션 `CREATE` grant를 경고로 기록하고 서비스 로그인 네 개의 `CREATE SCHEMA` 거부를 복구 드릴에서 검증한다. |
| 문서 | B1 PostgreSQL CI 성공과 이후 사용자 승인 기록을 현재형 과거 문구와 구분했다. |

로컬 통합 검증: backend 전체 pytest 통과(PostgreSQL 관련 항목은 DB URL 부재로 skip), Ruff 전체·format·mypy 83 source file 통과. frontend lint·production build·unit 34개·fixture Playwright 9개 통과. 백업/알림/복구 경계와 신규 평가 PostgreSQL 테스트의 로컬 targeted 회귀는 통과했으며 PostgreSQL 테스트는 CI에서 판정한다. Windows preflight와 복구 드릴 스크립트는 PowerShell parser를 통과했다. 이 결과는 실제 운영 host 검증이 아니다.

운영 활성화 판정은 여전히 **NO-GO**다. 실제 host/SSH/NTP, 승인된 source egress·요청량, Provider 모델·키·예산, 외부 backup/PITR·복구 주기, 알림 수신, roster/statistics 의미·공개 시점, 게시 이미지 digest/대상 아키텍처 증거가 필요하다. 이 항목은 합성 CI나 문서 수정만으로 완료할 수 없다.
