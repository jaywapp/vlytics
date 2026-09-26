# 전체 프로젝트 감사 — 2026-09-27

## 판정과 범위

현재 저장소는 합성 단위·계약 테스트와 개발용 통합 검증을 갖추고 있으나, **외부 운영값만 입력하면 실운영 가능한 상태는 아니다.** 실제 entrypoint 연결, 예측 선택, 재시도·과금, 복구에서 코드 보완이 필요한 항목을 확인했다. 기존 plan의 “TASK-001~020 구현 완료”는 모듈 구현과 운영 통합 완료를 분리해 다시 판정해야 한다.

- 기준: main 69585df34adee4b8b6f4b69ee054d9eb1224cff4. 직전 작업 b2cf190과 제품 코드 차이 없음.
- 방식: root의 배포·운영 검토와 gpt-5.6-sol 서브에이전트 3개의 수집·실행 / 모델·평가 / API·UI 검토. 핵심 발견은 root가 코드 경로와 합성 재현으로 교차 확인.
- P1: 실운영 또는 정확한 결과 제공 전에 고칠 항목. P2: 조건부 기능·운영 신뢰성·검증 개선.
- 제품 코드 수정, 운영 설정 변경, 실제 KOVO·유료 Provider 호출은 이번 감사에 포함하지 않았다.
- 전체 보안 인증이나 실제 모델 예측 품질 검증을 의미하지 않는다.

## 확인된 검사 결과

| 검사 | 결과와 한계 |
|---|---|
| 최신 main CI | [실행 36274126006](https://github.com/jaywapp/vlytics/actions/runs/36274126006) 성공. Backend 297 passed, frontend 20 passed, Playwright 9 passed, 개발용 Compose smoke 성공 |
| 이번 로컬 backend | 260 passed, 37 skipped, upstream deprecation warning 1건. DB 환경변수 미설정에 따른 37개 skip이며 DB 통합을 새로 통과했다고 주장하지 않음 |
| 이번 frontend | lint·typecheck 통과, 3 files / 20 tests 통과 |
| 활성 설정 + Compose env 집합 | 합성 활성 config를 실제 서비스별 env key 집합으로 검사하면 API·worker 모두 secret 누락으로 실패 |
| API 변환 합성 재현 | voided 행이 succeeded로 노출됨. 입력 행 순서만 바꾸면 대표 feature_snapshot_id 변경. cutoff 이후 Market quote도 available |
| 배포 패키지 검사 | Windows PowerShell에서 Test-DeploymentPackage.ps1 실패: CRLF Dockerfile의 정상 ARG NODE_IMAGE를 인식하지 못함 |
| production 전체 스택 | 이번에 재기동하지 않음. [PR #9](https://github.com/jaywapp/vlytics/pull/9)의 기존 실제 runtime smoke 실패와 초안 상태 확인 |

통과한 테스트는 불변성·역할 경계·예산 원장·합성 계산·화면 상태의 중요한 근거다. 아래 발견은 이 테스트가 다루지 않는 운영 연결과 상태 조합에서 발생한다.

## 실운영 전 필수 보완

### A01 · P1 · production 서비스의 secret 요구와 주입값 불일치

**근거:** [Compose](../../infra/compose.production.yaml) 84–118행, [설정 검증](../../backend/src/vlytics/config.py) 254–276행.

공통 설정 검증은 component와 무관하게 operator, backup, alert, 세 Provider secret을 요구한다. API 컨테이너에는 DB와 인증 secret만 전달되고, worker에는 DB와 세 Provider key만 전달된다.

합성 활성 설정으로 재현한 결과:
- API: backup credential, alert destination, 세 Provider key 누락으로 시작 거부.
- worker: operator secret, backup credential, alert destination 누락으로 시작 거부.

**개선/인수:** 배포 전체의 정책 검증과 각 프로세스가 실제 사용하는 secret 검증을 분리한다. 불필요한 Provider key를 조회 API에 배포하는 식으로 해결하지 않는다. 승인된 값 대신 합성 secret을 넣은 production 구성으로 API와 worker 모두 기동하는 CI가 필요하다.

### A02 · P1 · 실제 KOVO 수집 경로가 worker에 연결되지 않음

**근거:** [worker](../../backend/src/vlytics/worker.py) 218–248행, [disabled source handlers](../../backend/src/vlytics/ops/runtime.py) 103–118행, [coverage 문서](../operations/coverage-report.md) 47–54행.

source.bulk_collection_enabled를 켜면 “no approved KOVO transport exists”로 즉시 종료한다. 끈 상태에서는 pre-cutoff/current schedule/final/correction 작업이 모두 disabled handler로 연결된다. 구현된 sync planner와 handler는 production entrypoint에서 사용되지 않는다.

**영향:** OP-001이 결정되어도 설정 변경만으로 현재 시즌 수집이나 결과 정정이 시작되지 않는다. 이는 정책 승인 대기와 별개의 구현 잔여 작업이다.

**개선/인수:** 승인 범위 안의 transport → receipt-first ingestion → sync handler → 주기 planner → worker 연결을 완성한다. 합성 HTTP transport를 같은 entrypoint에 주입해 일정 생성부터 결과 정정까지 검증하고, 실제 요청은 OP-001 조건이 정해진 뒤 실행한다.

### A03 · P1 · production 네트워크로 host 접근과 외부 호출이 막힘 — 기존 발견

**근거:** [Compose](../../infra/compose.production.yaml) 13–14, 143–167행, [PR #9 실패 실행](https://github.com/jaywapp/vlytics/actions/runs/36272248195).

모든 서비스가 internal private network에만 속한다. 기존 CI에서 컨테이너 내부 health는 정상이었지만 host의 loopback frontend 접근이 실패했다. worker에도 외부 source/Provider 연결 경로가 없다.

**개선/인수:** DB/API private 경계를 유지하면서 frontend 접근 경로와 worker 외부 호출 경로를 설계해 적용한다. production Compose 자체를 대상으로 host health·same-origin 401/403/200·내부 격리·허용된 outbound를 검사한다.

**현재 상태:** 제안한 ingress/egress 추가는 자동 승인 검토가 보안 경계 확장에 대한 사용자 명시 승인이 필요하다는 이유로 거부했다. 이번 감사에서는 변경을 재시도하지 않았다.

### A04 · P1 · 무효화된 예측과 다른 Snapshot을 현재 비교표에 표시

**근거:** [API SQL](../../backend/src/vlytics/api/repository.py) 218–250행, [상세 서비스](../../backend/src/vlytics/api/service.py) 149–170, 594–608행, [화면의 첫 Provider 선택](../../frontend/src/features/matches/MatchBriefingPage.tsx) 75–104행.

SQL은 lifecycle이 voided/superseded여도 provider_status를 succeeded로 만든다. 상세 조회는 현재 schedule revision·공유 snapshot 기준으로 대표 예측을 선택하지 않으며, 같은 Provider가 여러 개면 UI는 첫 행을 고른다.

**재현:** voided old와 published new를 입력하면 둘 다 succeeded가 된다. 입력 순서를 old/new에서 new/old로 바꾸면 대표 feature_snapshot_id도 old-feature에서 new-feature로 바뀐다.

**개선/인수:** 현재 경기 비교는 현재 schedule revision + 공유 snapshot/cutoff + 유효 lifecycle을 기준으로 서버에서 결정적으로 선택한다. outcome에 해당 snapshot과 lifecycle을 명시하고 이전 예측은 History에서 구분한다. 연기·취소·재편성·복수 variant를 포함하는 API→UI 테스트가 필요하다.

### A05 · P1 · timeout 재시도 시각과 게시 자격이 충돌

**근거:** [예측 실행](../../backend/src/vlytics/engine/orchestrator.py) 233–250, 318–334, 590–600행, [dispatcher](../../backend/src/vlytics/ops/scheduler.py) 816–860행.

초기 허용은 cutoff+30초, 호출 timeout은 60초, 첫 재시도 지연은 5초다. 첫 호출이 timeout이면 약 cutoff+65초에 재호출하지만 각 시도의 시작을 다시 초기 30초 기준으로 검사한다. 따라서 재시도가 성공해도 late_rejected로 버리고, handler가 실패로 전달하지 않아 job은 succeeded로 종료될 수 있다.

**개선/인수:** 최초 시작 자격과 후속 시도의 완료 deadline을 분리해 영속적으로 추적한다. 자격이 없는 재호출은 호출 전에 막고, 늦은 폐기를 성공 job으로 기록하지 않는다. fake clock으로 timeout→재시도 성공·deadline 초과·재시작을 묶어 검증한다.

### A06 · P1 · 경기별 Provider 호출 상한이 적용되지 않음

**근거:** [운영 Provider 계획](../../backend/src/vlytics/engine/providers/operational.py) 21–29, 126–148행, [worker](../../backend/src/vlytics/worker.py) 232–240, 341–349행, [예산 원장](../../backend/src/vlytics/engine/providers/budget.py) 99–126행.

max_calls_per_match는 파싱만 된다. 실행 계층은 일/월 금액·호출 수만 넘기고 원장도 일/월만 집계한다. 재시도마다 attempt_no 기반의 새 reservation을 만든다.

**영향:** 경기별 상한 1, 최대 시도 3이면 timeout/provider error 경로에서 경기별 설정을 넘어 호출될 수 있다. 일/월 상한은 별도로 작동한다.

**개선/인수:** 경기·Provider 범위의 예약/정산 호출 수를 DB 트랜잭션에서 원자 검사한다. 재편성과 variant가 같은 상한을 공유하는지 계약을 명시하고 동시 재시도·timeout 포함 회귀 테스트를 추가한다.

### A07 · P1 · 정상 결과를 final로 확정하는 단계 누락

**근거:** [parser](../../backend/src/vlytics/mirror/parser.py) 588–597행, [fact writer](../../backend/src/vlytics/mirror/repositories/facts.py) 592–615행, [통계 입력 필터](../../backend/src/vlytics/ops/runtime.py) 566, 594행.

최초 완결 결과는 항상 provisional이다. 같은 raw이면 revision을 추가하지 않고, raw hash가 달라지면 결과 내용과 무관하게 corrected로 만든다. 설정의 provisional_stability_seconds를 이용한 final 승격 경로가 없다.

**영향:** 정상 결과는 provisional에 남고, final/corrected만 받는 5세트 학습·규칙 조회에서 제외될 수 있다. 점수 외 필드 변경도 결과 정정으로 취급될 수 있다.

**개선/인수:** source 상태와 안정화 시간에 근거한 append-only 확정 revision을 생성한다. final 이후 의미상 점수 변경만 corrected로 구분한다. 동일 payload 재조회·점수 외 변경·진짜 점수 정정·프로세스 재시작을 검증한다.

### A08 · P1 · 구현된 Elo와 운영 통계 기준선이 연결되지 않음

**근거:** [계획 TASK-008](../prepare/plan.md) 194행 부근, [runtime](../../backend/src/vlytics/ops/runtime.py) 328–331, 496–509행.

운영 statistical-joint-v1의 승률은 최근/시즌 승률을 평균한 식으로 계산한다. 구현된 EloPredictor의 남녀 구분, 홈 어드밴티지, 시즌 회귀·구단 이월이 worker의 통계 예측 경로에 사용되지 않는다. 세트·공동점수 분포 역시 이 다른 승률에서 파생된다.

**개선/인수:** cutoff 이전 사실만으로 versioned Elo 상태를 재생하거나 저장하고, 그 출력을 세트 모델에 연결한다. 계획된 기준선을 대체하려면 설계·모델 라벨·비교 결과를 명시적으로 변경해야 한다. 실험 함수뿐 아니라 production runner를 대상으로 같은 입력의 기대 승률을 검증한다.

### A09 · P1 · 복구 drill 성공이 서비스 복구 가능성을 보장하지 않음

**근거:** [restore drill](../../infra/scripts/Invoke-RestoreDrill.ps1) 4, 102–107행, [backup](../../infra/scripts/Backup-Database.ps1) 27행, [migrations](../../backend/src/vlytics/storage/migrations.py) 129–160행, [CI restore](../../infra/scripts/ci-compose-smoke.sh) 138–164행.

두 문제가 있다.
1. 기본 restore 관리자 URL은 migrator를 가리키지만 최초 migration 후 migrator는 NOCREATEDB이다. 문서의 기본 호출로는 임시 복구 DB CREATE DATABASE가 실패한다.
2. dump와 restore는 ACL을 제외하고, restore는 소유권도 제외한다. 복구 검사는 관리자 계정으로 행 수·schema만 확인한다. 실제 API/engine 계정의 접근 권한은 검증하지 않는다. 복원된 migration ledger 때문에 migrations를 다시 실행해도 기존 GRANT는 재적용되지 않는다.

**개선/인수:** 별도 복구 관리자 역할과 owner/role/GRANT 복원 전략을 정한다. 새 cluster에 복원한 뒤 실제 네 서비스 로그인으로 SELECT·허용 INSERT·금지 UPDATE/DELETE·worker 1회 실행을 확인한다. 행 수가 맞다는 이유만으로 복구 완료로 판정하지 않는다.

### A10 · P1 · 반복 동기화에서 후속 페이지를 버림

**근거:** [sync_once](../../backend/src/vlytics/mirror/backfill.py) 479–507행, [sync handler](../../backend/src/vlytics/ops/sync.py) 402–418행.

sync_once는 매번 cursor=None으로 한 페이지만 가져오고 next_cursor를 무시한다. handler도 다음 cursor를 durable하게 저장하지 않는다. 페이지형 adapter에서는 다음 주기에도 첫 페이지만 다시 처리한다.

**개선/인수:** deadline 안에서 모든 페이지를 순회하거나 job/checkpoint에 cursor를 저장해 재개한다. 최소 2페이지·중간 실패·재시작 사례에서 전체 coverage를 확인한다. 실제 KOVO 응답의 pagination 여부·단위는 adapter 계약에서 검증한다.

## 추가 정확성·검증 개선

### A11 · P2 · production 예시와 preflight가 실제 배포 설정을 충분히 검증하지 못함

- [운영 예시](../../infra/operational.production.example.toml) 9, 14, 15행의 single-private-host, loopback-or-private-tunnel, bearer-secret은 schema enum에 없다. placeholder와 예산만 채워도 계속 거부된다.
- [preflight](../../infra/scripts/Invoke-Preflight.ps1) 91–103, 146, 161행은 인자로 전달한 OperationalConfig를 검사하지만 [Compose](../../infra/compose.production.yaml) 30행이 마운트하는 VLYTICS_OPERATIONAL_CONFIG_FILE과 일치하는지 확인하지 않는다. evidence는 경로 비교를 하지만 config는 하지 않는다.

**개선/인수:** 유효 enum과 명확한 미설정 필드를 사용하고, 검증한 파일의 절대 경로/hash와 실제 Compose 마운트가 같아야 통과시키도록 한다. config A를 검증하면서 config B를 배포하려는 경우 거부하는 테스트를 추가한다.

### A12 · P2 · Market 표시·생성 경로는 adapter 연결 전에 보완 필요

[API SQL](../../backend/src/vlytics/api/repository.py) 189–207행은 match별 최신 received_at의 Market을 가져오며 [응답 변환](../../backend/src/vlytics/api/service.py) 558–568행은 ID만 있으면 available로 표시한다. prediction cutoff의 as-of·stale·late 판정과 무관하다. 합성 입력에서 cutoff 13:30, quote 14:30도 available로 재현됐다.

또한 [평가 runtime](../../backend/src/vlytics/engine/evaluation/runtime.py) 209–234행은 기존 market_evaluations를 읽지만 운영 worker에는 prediction별 Market 비교를 생성하는 호출 경로가 없다. snapshot을 적재하는 것만으로 비교 자료가 만들어지지 않는다.

**개선/인수:** prediction의 고정 Market snapshot/eligibility를 조회에 사용하고, 예측 이후 별도 멱등 job에서 비교를 생성한다. MissingMarketAdapter와 합성 available/stale/late 사례를 같은 생산 경로로 실행한다. **현재 Market missing은 UC-005가 허용한 상태이므로 실제 adapter 부재 자체를 MVP 차단으로 보지 않는다.**

### A13 · P2 · 재실패한 작업을 같은 화면에서 다시 재시도할 수 없음

**근거:** [OperationsPage](../../frontend/src/features/operations/OperationsPage.tsx) 119–130, 166행, [retry 함수](../../backend/migrations/0014_operator_retry.sql) 89–103행.

첫 retry 요청이 성공하면 job별 done 상태와 idempotency key를 계속 보존한다. 이후 worker가 다시 실패해도 버튼이 비활성이다. 상태만 풀어도 같은 key는 이전 요청 replay가 된다.

**개선/인수:** 불확실한 동일 요청을 재전송하는 동안만 key를 유지한다. 요청 수락 후 새로운 attempt가 실패하면 새 논리 요청과 key를 생성한다. accepted→재실패→두 번째 retry를 테스트한다.

### A14 · P2 · 최신 coverage와 History 상태의 실제 데이터 계약 불일치

- [coverage SQL](../../backend/src/vlytics/api/repository.py) 289–294행은 모든 이력을 정렬 없이 읽고 [상세](../../backend/src/vlytics/api/service.py) 158–162행이 순회 마지막 값으로 덮어쓴다. match/data_kind별 최신 observed_at과 동률 기준으로 선택해야 한다.
- [HistoryPage](../../frontend/src/features/history/HistoryPage.tsx) 211–216행은 succeeded만 정상 톤으로 표시하지만 실제 SQL의 성공 lifecycle은 published다. [브라우저 fixture](../../frontend/tests/e2e/fixtures.ts)는 succeeded를 넣어 차이를 감춘다.

**개선/인수:** missing→available 정정과 동률 coverage를 검증하고, lifecycle과 Provider 실행 상태를 분리한 계약으로 UI 문구·톤을 맞춘다.

### A15 · P2 · Windows checkout에서 배포 패키지 정적 검사가 실패

**근거:** [Test-DeploymentPackage](../../infra/scripts/Test-DeploymentPackage.ps1) 74–79행.

줄 끝 regex가 LF만 가정한다. 실제 CRLF Dockerfile에서 정상 ARG NODE_IMAGE를 찾지 못하며 이번 PowerShell 실행에서도 실패했다.

**개선/인수:** 입력 개행 정규화 또는 CRLF 허용 패턴을 사용한다. LF/CRLF 양쪽 fixture와 Windows 실행을 CI에 포함하고, TOML/JSON 문법만이 아니라 예상한 비활성화 사유와 schema 적합성도 검사한다.

## 조건부 준비도·확장 개선

1. **검증된 roster/통계를 Feature에 전달할 경로:** [DatabaseFeatureSnapshotFactory](../../backend/src/vlytics/ops/runtime.py) 135–145, 191–294행은 schedule/result/set만 읽고 roster 및 team/player stats를 builder에 넘기지 않는다. 현재 source 계약에서 약어·분모·T-60 명단 공개 시점이 미검증이므로 missing 자체는 올바르다. 다만 해당 사실이 검증된 뒤에도 factory 수정이 필요하다. source 계약 승인 → typed mapping → cutoff 조회 → feature lineage의 별도 완료 기준을 둔다.
2. **실제 API를 사용하는 browser E2E:** 현재 Playwright는 route interception으로 응답을 만든다. 합성 DB를 사용해 production frontend→Nginx→FastAPI→PostgreSQL로 일정 선택·비교·History·retry·평가까지 실행하는 최소 1개 흐름이 필요하다. A04/A14 같은 상태 불일치를 잡는 것이 목적이다.
3. **조회 성능:** [PostgresReadRepository.load](../../backend/src/vlytics/api/repository.py) 110–143행은 요청마다 전체 경기·예측·평가·작업·coverage·예산을 메모리에 읽는다. 장기 데이터에서는 endpoint별 WHERE/keyset pagination/SQL 집계와 인덱스를 적용한다. 먼저 22시즌 규모 합성 데이터로 지연·메모리를 측정해 목표값을 정한다.
4. **복구 세부:** RestoreDrill.New-DatabaseUrl은 query를 지워 sslmode/인증서 옵션을 잃는다(44행). 원본 row count를 dump 이후 별도 조회하므로 쓰기 중인 DB와 비교하면 정상 dump도 불일치할 수 있다(121–123행). TLS 옵션을 보존하고 일관된 snapshot이나 명시적 쓰기 정지 경계를 사용한다.
5. **운영 자동화:** alert/backup 설정값 존재와 실제 예약 실행·전송 성공을 구분한다. 시계·job heartbeat·backup age·Provider 비용 알림의 실행 주체와 시험 증거를 명시한다. worker의 kill -0 1 healthcheck만으로 처리 정지를 탐지할 수는 없다.
6. **재현 가능한 릴리스:** backend Python/uv 및 PostgreSQL image에도 digest 정책을 일관되게 적용하고 SBOM/취약점 검사를 릴리스 증거로 남긴다. frontend .vite 캐시 ignore는 초안 PR #9에 묶여 있어 현재 작업 트리에 계속 나타난다.
7. **완료 장부 갱신:** plan과 release checklist를 모듈 구현 / production 연결 / 합성 통합 / live 검증으로 나눈다. 특히 TASK-006·008·012·014·017·018은 이번 발견을 반영해 재검증 항목을 열어야 한다.

## 권장 작업 순서와 완료 기준

| 순서 | 묶음 | 완료 기준 |
|---|---|---|
| 1 | A01·A03·A11·A15: 운영 부팅·설정 | 합성 활성 설정의 production Compose 실제 기동, 서비스별 secret 최소화, 경로/hash 일치, host/API 검증 |
| 2 | A04·A13·A14: 현재 예측·UI 계약 | 무효 예측 제외, 동일 Snapshot의 결정적 비교, 실제 backend 응답 기반 E2E, 재실패 retry |
| 3 | A05·A06: 재시도·과금 | timeout/restart/concurrency에서 deadline과 경기별·일별·월별 한도 모두 준수 |
| 4 | A02·A07·A10: 수집·확정 | 같은 production handler로 다중 페이지·정정·안정화·재시작 합성 통합 통과 |
| 5 | A08 및 검증된 Feature: 통계 모델 | worker의 Elo 출력이 검증된 기준선·모델 version과 일치 |
| 6 | A09: 복구 | 새 cluster 복원 후 실제 서비스 역할과 API/worker 재개 성공 |
| 7 | A12와 운영 외부 게이트 | Market missing 허용 유지. OP-001~004·호스트·NTP·backup/PITR·alert·실제 Provider smoke를 증거로 종료 |

네트워크 승인이나 실제 유료 리소스 없이도 A01/A04~A11/A13~A15의 코드·합성 테스트 보완은 진행할 수 있다. 외부 값 대기 때문에 이러한 구현 누락을 완료로 처리하면 안 된다.
