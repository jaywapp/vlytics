# 전체 프로젝트 후속 점검 — 2026-09-27

## 최신 조치 상태

최종 코드 후보 `7fa9a6d`의 [CI 36291809544](https://github.com/jaywapp/vlytics/actions/runs/36291809544)가 전체 성공했다. Backend 439 및 추가 production 내부 기동·동일 복구 DB 서비스 재개를 포함한 인수 검증을 통과했다. 기존 네 가지 변경 승인과 실제 운영 증거는 남아 있다.

R01~R06은 코드 `6280185`와 CI 36289935169에서 검증됐다. R07~R09는 코드 `3038ba4`의 [CI 36290510898](https://github.com/jaywapp/vlytics/actions/runs/36290510898) 전체 성공으로 검증됐다(backend 436, frontend 32, fixture browser 9, 실제 Nginx/API/DB browser 1, Windows 34). R04/R05의 stale fault·동일 시각 충돌·crash/lease 경계 인수 테스트도 추가해 로컬 dispatcher+pipeline 25개가 통과했다. 아래 발견은 원 감사 시점의 근거로 보존한다. 기존 네 가지 승인 대기와 실제 운영 검증은 [최신 조치 장부](audit-remediation.md)를 따른다.

## 판정과 기준

**코드·합성 통합 검증은 크게 보완됐지만, 전체 구현 완료 및 운영 활성화로 판정할 수 없다.** 특히 진행 중인 감시 기능에는 통합 전에 해결할 결함이 남아 있다. 이 보고서는 기존 감사의 해결 항목을 다시 미해결로 세지 않고, 현재 잔여 작업과 이번에 확인한 누락을 구분한다.

- 저장소: `jaywapp/vlytics`, 브랜치 `codex/project-audit-20260927`.
- 커밋 기준: `1b94a07d46d0f132c09c66a94198f9e78f4f4f3e`.
- [PR #12](https://github.com/jaywapp/vlytics/pull/12)는 OPEN/DRAFT이며 병합 전이다.
- 작업 트리에는 `health_dispatch.py`, `heartbeat_export.py`, 각각의 테스트와 `ci-compose-smoke.sh` 변경이 미커밋 상태로 존재한다. 아래 R01~R05는 이 작업 트리 기준이며, 이미 배포된 결함으로 표현하지 않는다.
- root와 기존 gpt-5.6-sol 검토 에이전트가 코드·실행 경로·기존 증거를 대조했다. 이번 점검 중 제품 코드 수정, 실제 알림 전송, 유료 호출, 배포는 하지 않았다.
- P1: 해당 기능 통합/운영 전 필수 수정. P2: 신뢰성·검증·유지보수 개선. 확인된 결함과 미검증 조건은 별도로 표시한다.

## 이번 점검에서 확인한 누락

| ID | 우선순위 | 확인 내용 | 영향 / 완료 기준 |
|---|---|---|---|
| R01 | P1 | CI worker에 heartbeat 출력 경로가 없다 | 신규 export 단계가 읽을 파일이 생성되지 않는다. CI용 설정에 경로를 전달하고 실제 컨테이너 export까지 통과해야 한다. |
| R02 | P2 | 재시작 검사가 이전 heartbeat도 허용한다 | 재시작 후 첫 poll 실패를 놓칠 수 있다. 재시작 경계 이후 완료 시각을 검증해야 한다. |
| R03 | P1 | 장애 복구 후 재발에도 알림 키를 재사용한다 | 수신처가 새 장애를 중복으로 억제할 수 있다. 장애 회차를 구분하고 같은 회차 재전송만 같은 키를 써야 한다. |
| R04 | P2 | 오래된 health 보고서가 최신 상태를 변경한다 | 늦게 도착한 정상 보고서가 최신 장애의 중복 억제 기록을 지운다. 평가 시각의 순서 검증과 복구 후에도 유지되는 상태가 필요하다. |
| R05 | P2 | 모든 알림 lease가 함수 시작 시각을 사용한다 | 앞선 요청이 느리면 뒤 요청의 예약이 전송 전에 만료될 수 있다. 예약 시점 기준 유효기간과 느린 전송/동시 실행 회귀가 필요하다. |

### R01 — CI worker heartbeat 설정 누락

근거: [CI script](../../infra/scripts/ci-compose-smoke.sh) 11~13, 202~223행; [개발 Compose](../../infra/compose.yaml) 74~78행; [Settings](../../backend/src/vlytics/config.py) 90행; [worker](../../backend/src/vlytics/worker.py) 118~121행.

CI script는 개발 Compose의 worker를 사용한다. 해당 서비스에는 `VLYTICS_WORKER_HEARTBEAT_PATH`가 없고 기본값은 `None`이다. worker는 경로가 있을 때만 기록한다. production Compose에 경로가 있다는 사실은 이 CI 경로를 해결하지 않는다. 신규 export는 기본 `/tmp/vlytics-worker-heartbeat.json`을 읽으므로 현재 구성에서는 실패한다.

이번 판정은 설정부터 기록/읽기까지의 정적 경로 확인이다. 로컬 Docker가 없어 신규 script를 실제 컨테이너로 실행한 결과라고 주장하지 않는다. 기존 성공 CI는 이 미커밋 export 변경을 포함하지 않는다.

### R02 — 재시작 이후 실제 처리 증거 부족

근거: [CI script](../../infra/scripts/ci-compose-smoke.sh) 202~223행.

`compose restart`는 같은 컨테이너의 writable layer를 유지한다. R01 수정 후 파일이 기록되더라도, 재시작 전에 생성된 180초 이내 heartbeat로 신규 검사가 통과할 수 있다. 단순 freshness에 더해 `completed_at`이 재시작 경계 이후인지 확인해야 한다. 컨테이너의 main process PID는 재시작 전후 모두 1일 수 있으므로 PID 변화만으로 판정하지 않는다.

### R03 — 장애 회차와 알림 멱등키 혼동

근거: [dispatcher](../../backend/src/vlytics/ops/health_dispatch.py) 153, 377~388, 430~441행; [기존 재발 테스트](../../backend/tests/test_health_dispatch.py) 186행.

현재 키는 목적지·check·status·evidence의 해시다. 복구 시 SQLite row를 삭제하지만 동일 evidence의 장애가 다시 발생하면 같은 키가 생성된다. `unknown`처럼 시각 evidence가 없는 장애 재발도 해당한다. 수신처가 멱등키를 보존하면 두 번째 장애를 새 알림으로 받아들이지 않을 수 있다.

MockTransport로 장애 → 복구 → 같은 장애 재발을 실행했다. 요청은 2회였지만 `same_key=true`였다. 기존 테스트는 요청 수만 검사해 이 문제를 놓친다. 로컬 상태에 장애 회차를 보존하고, 실패 재전송은 같은 키, 복구 후 새 회차는 다른 키임을 검증해야 한다.

### R04 — 평가 시각 순서 검증 누락

근거: [dispatcher](../../backend/src/vlytics/ops/health_dispatch.py) 137~139, 216, 430~441, 443~506행.

`evaluated_at`의 시간대/형식은 검사하지만 저장된 상태와의 선후 관계는 비교하지 않는다. MockTransport에서 12:10 장애 → 12:00 정상 → 같은 12:10 장애를 처리하면 두 번째 장애가 다시 발송됐다(`duplicate_sent=1`). 오래된 정상 보고서가 최신 상태를 지운 것이다.

목적지/check별 마지막 평가 시각을 트랜잭션 안에서 비교하고, 복구 후에도 순서 기준을 유지해야 한다. 오래된 장애, 오래된 복구, 같은 시각의 충돌, 동시 처리도 회귀에 포함한다.

### R05 — 예약 lease의 시간 기준 오류

근거: [dispatcher](../../backend/src/vlytics/ops/health_dispatch.py) 124~127, 155~162, 477행.

함수 진입 시각 `current`를 모든 순차 발송의 예약 기준으로 사용한다. 유효기간은 그 시각에 `2 × timeout + 30초`를 더한 값이다. 예를 들어 여러 요청이 각각 오래 걸리면 뒤쪽 예약을 생성할 때 이미 만료될 수 있어, 다른 프로세스가 같은 발송을 다시 예약할 수 있다.

이 항목은 코드 경로 확인이며 실제 수신처 중복 전달을 관측한 것은 아니다. 예약 직전 clock으로 계산하고, 긴 전송·lease 만료·재시작을 함께 검증해야 한다. HTTP 성공 직후 로컬 기록 실패처럼 원격 전송과 SQLite 사이의 원자성 한계도 운영 문서에 명시한다.

## API/UI 교차 검토에서 추가 확인한 사항

### R06 · P1 · 상태 projection이 없으면 무효 예측도 published로 간주

근거: [API SQL](../../backend/src/vlytics/api/repository.py) 512~513, 529행; [현재 예측 선택](../../backend/src/vlytics/api/service.py) 844행; [projection repository](../../backend/src/vlytics/engine/repositories/predictions.py) 384~385행.

SQL은 projection을 LEFT JOIN하고 없으면 `coalesce(ps.current_status, 'published')`로 반환한다. projection은 코드상 재구성 가능한 파생 저장소지만, immutable event의 최신 상태를 fallback으로 읽지 않는다. 같은 schedule revision의 예측이 voided/late_rejected/superseded였는데 projection row만 유실되거나 재구성 중이면 현재 비교표의 published 필터를 통과할 수 있다.

일반적인 정상 저장 경로에서 projection이 항상 누락된다는 뜻은 아니다. **projection 부재라는 장애/복구 조건의 정확성 결함**이다. immutable event에서 상태를 재구성하거나 부재 시 현재 예측에서 제외하도록 보완하고, 무효 event는 유지한 채 projection만 없는 실제 DB 회귀를 추가해야 한다. 이번에는 SQL→service 경로를 직접 확인했으며 DB 삭제 재현은 하지 않았다. 기존 A04의 정상 projection 경로 수정은 유효하다.

### R07 · P2 · 일정 화면의 pagination 미처리 — 조건부 확장 개선

근거: [일정 client](../../frontend/src/features/matches/api.ts) 63~70행; [일정 화면](../../frontend/src/features/matches/MatchBriefingPage.tsx) 413행.

client는 `limit=100`으로 조회하고 화면은 `data.items`만 소비한다. `next_cursor`를 순회하거나 더보기로 연결하지 않는다. 같은 날짜/필터에 101개 이상 결과가 있으면 이후 항목을 보여주지 않는다. 현재 KOVO 하루 일정에서 실제 101건이 발생했다는 증거는 없으므로 운영 차단이 아닌 조건부 개선이다. cursor 지원 또는 지원 범위/잘림 안내를 명시한다.

### R08 · P2 · History/Operations의 잘못된 성공 응답에 대한 방어 부족

근거: [History client](../../frontend/src/features/history/api.ts) 42행; [Operations client](../../frontend/src/features/operations/api.ts) 23행; [Operations 화면](../../frontend/src/features/operations/OperationsPage.tsx) 150행.

HTTP 200 JSON을 TypeScript 타입으로 cast할 뿐 런타임 구조는 검증하지 않는다. `data.items`나 `data.budgets`가 없는 응답이 오면 ready 상태로 들어간 뒤 렌더 중 예외가 발생할 수 있다. 해당 오류를 잡는 ErrorBoundary도 현재 src에서 확인되지 않았다.

정상 backend가 현재 잘못된 응답을 보낸다는 발견은 아니다. 버전 불일치·예상 밖 200 응답에 대한 복원력 개선이다. API 경계에서 필수 구조를 검증해 오류 패널로 전환하고 malformed-200 회귀를 추가한다.

### R09 · P3 · 공용 상태 패널의 제목 계층 개선

근거: [StatePanel](../../frontend/src/components/StatePanel.tsx) 14행; [Operations 화면](../../frontend/src/features/operations/OperationsPage.tsx) 150~171행.

상태 패널은 항상 h1을 렌더한다. 페이지 h1과 섹션 h2 아래의 비용/coverage/jobs 상태에도 같은 패널이 쓰여 제목 탐색에서 상태 메시지가 페이지 제목과 같은 계층으로 노출된다. 문맥별 heading level을 지원하고 렌더된 제목 계층을 확인하는 개선을 권장한다. 다수 h1 자체를 보안 문제나 검증된 접근성 표준 위반으로 단정하지 않는다.

## 기존 감사의 미완료 연결

| 항목 | 현재 상태 | 필요한 조치 |
|---|---|---|
| A03 production 네트워크 | internal network만 사용하는 구성의 host 접근/외부 호출 문제가 남아 있다 | 승인된 ingress/egress 설계 적용 후 production Compose 자체의 접근·격리·외부 호출 검증 |
| A08 운영 통계 모델 | `_home_probability()`는 최근/시즌 승률 평균이며 구현된 Elo를 사용하지 않는다 | 계획한 Elo의 cutoff 재생·모델 버전·분포 연결을 production runner에서 검증 |
| A12 Market 생성 | API eligibility는 수정됐지만 비교 생성/평가 운영 연결은 남아 있다 | 합성 available/stale/late 경로부터 동일 worker에서 검증. 실제 adapter 없는 `missing`은 UC-005 허용 예외 |
| B1 roster/stats Feature | 검증된 사실을 factory에서 hydrate하는 경로가 남아 있다 | source 의미 검증 후 typed mapping·cutoff·lineage를 연결. 미검증 사실을 임의 입력하면 안 됨 |

근거는 [조치 장부](audit-remediation.md), [production Compose](../../infra/compose.production.yaml), [통계/Feature runtime](../../backend/src/vlytics/ops/runtime.py), [평가 runtime](../../backend/src/vlytics/engine/evaluation/runtime.py)를 따른다. 통계 평균 계산은 현재 runtime 497~508행에서 재확인했다.

위 네 변경은 앞선 자동 승인 검토가 보안 경계·운영 계산식·데이터 입력 경로 변경에 대한 구체적인 사용자 승인이 필요하다는 이유로 거부한 상태다. 이번 점검에서는 재시도하거나 우회 적용하지 않았다.

## 운영과 성능에서 남은 검증

| 범위 | 이미 확보한 것 | 아직 없는 것 / 개선 방향 |
|---|---|---|
| 감시 | DB/파일 evidence collector와 health 평가, heartbeat export/dispatcher 작업 초안 | R01~R05 수정, host NTP evidence 생성, 예약 실행, 실제 수신 성공과 실패 감지 증거 |
| 백업/복구 | 새 cluster의 owner/ACL/SCRAM 복원 및 서비스 권한 검사 | 운영 외부 암호화 백업, 보존/RPO/RTO, PITR 목표 시각 복원, 주기 실행과 실패 알림 |
| Source/Provider | 합성 transport, worker/planner, deadline/과금 회귀 | OP-001/003/004의 실제 승인 범위·model/version·예산·소량 smoke. 미검증 팀/기간 매핑은 격리 유지 |
| 배포 | 개발 Compose 실제 browser/Nginx/API/DB, production config parser | production Compose 실제 기동, host NTP/접근 경로/운영 데이터, rollback 증거 |
| 이미지 | CI 후보 8개 이미지 SBOM·identity·HIGH/CRITICAL 0건 검증 | 게시된 release digest·대상 architecture에 대한 같은 검사와 배포 기록 |
| 성능 | 22시즌·5,500경기·예측/평가 각 16,500개, 실제 API warm 20회 측정 | 동시 사용자/쓰기, 서버 RSS·PostgreSQL 메모리, 실패 집중 부하, 전체 규모 Market/통계 비교 |

전체 기간 Performance는 p95 **1.74초**, Python peak **67.35MiB**, 응답 **2.81MB**다. 현재 로컬 회귀 budget은 충족하지만 운영 SLO는 아니다. 실제 사용 패턴을 확인한 뒤 기본 조회 기간 제한, SQL/사전 집계, provenance 응답 축약 중 계약에 맞는 방식을 선택할 수 있다. 최근 한 시즌 p95는 **108.53ms**다. [측정 조건과 한계](api-performance.md)를 따른다.

## 작업 과정의 개선점

1. **완료 판정은 실제 entrypoint 기준으로 한다.** 독립 모듈 테스트뿐 아니라 worker·설정·Compose·API·UI가 같은 계약으로 연결됐는지 확인해야 한다. R01은 구현된 두 모듈 사이의 설정 누락이다.
2. **시간/재시작/재발을 기본 검증 축으로 둔다.** 정상 순서의 단일 실행뿐 아니라 지연 보고서, 복구 후 재발, 느린 수신처와 동시 실행을 포함해야 한다. 신규 단위 테스트 27개가 통과해도 R03/R04는 별도 재현됐다.
3. **코드 완료·합성 통합·live 증거를 분리한다.** 기존 A01/A04~A07/A09~A11/A13~A16 및 B2/B4 등의 수정/회귀 결과는 유지한다. 실제 배포나 모델 품질까지 통과한 것으로 확대하지 않는다.
4. **최신 상태 표와 과거 기록을 분리한다.** 현재 장부에는 과거의 scan 미실행 문장과 이후 scan 성공, 여러 최종 결과가 누적돼 있다. 문서 첫머리의 기준 commit/CI/미완료 목록을 단일 최신 표로 유지하고 과거 결과는 실행 이력으로 구분하는 편이 좋다.
5. **미커밋 변경은 기존 초록색 CI로 인증하지 않는다.** 새 감시 기능은 별도 검증 후 커밋하고 그 정확한 commit의 CI를 확인해야 한다. 현재 PR 초안과 작업 트리 상태를 유지한다.

## 이번에 확인한 검증 결과

- [CI 36287847668](https://github.com/jaywapp/vlytics/actions/runs/36287847668): `1b94a07` 기준 backend/frontend/deployment-windows 세 job 모두 성공을 GitHub에서 재확인했다. 기존 기록은 backend 400, Windows 34, frontend 24, fixture E2E 9, 실제 browser 1 및 8개 image 검사 통과다.
- 신규 감시 테스트를 로컬에서 다시 실행: `test_heartbeat_export.py` + `test_health_dispatch.py` **27 passed**.
- 신규 두 모듈과 테스트 Ruff 통과, 두 모듈 mypy 통과, `git diff --check` 통과.
- 별도 MockTransport 재현: 장애 재발 시 동일 key, 오래된 정상 보고서에 의한 최신 상태 삭제 확인. 실제 HTTP 요청 없음.
- 전체 DB suite·브라우저·컨테이너를 이번 점검에서 다시 실행하지 않았다. 기존 CI 증거와 이번 국소 재현을 구분한다.

검토한 미커밋 dispatcher의 UTF-8/LF 정규화 SHA256: `c95221151566fe496f4d7195d38fc6bc8e9be1a4b838c848c504ef858f2099d4`.

## 권장 처리 순서

1. API projection 누락 처리 R06 및 미커밋 감시 기능 R01~R05 보완 → 실제 DB/국소 회귀 → Compose CI.
2. 위 네 운영 연결 변경의 구체적 승인 후 A03/A08/A12/B1 통합과 재검증.
3. 운영 host·source·Provider·백업/PITR·알림 값 확정 후 소량 실제 smoke와 release digest 증거 수집.
4. R07~R09를 사용 규모·응답 계약·접근성 개선으로 처리하고, 운영 부하 성능 및 rollback 검증 후 활성화 판정.

이 문서는 점검 결과이며 구현 완료 선언이나 운영 활성화 승인이 아니다.


## 후속 조치 기록

위 발견과 코드 hash는 점검 당시의 기록이다. 이후 R01~R06을 수정했다. R01/R02의 실제 Docker 증거는 새 CI에서 확인하며, R03~R05는 dispatcher·실제 health pipeline 22 tests, R06은 실제 DB API 29 tests로 검증했다. 깨끗한 PostgreSQL 전체 회귀 435 tests도 통과했다. export 실패 관측 시각 보완과 추가 회귀가 이어졌으며, 최종 CI 결과는 PR의 해당 commit check를 따른다.

R07~R09는 UI 담당 에이전트가 진행 중이다. 최종 코드·운영 잔여 상태는 [조치 장부](audit-remediation.md)를 따른다. 전체 목표 완료나 운영 활성화를 선언하지 않는다.


R01~R06의 [CI 36289935169](https://github.com/jaywapp/vlytics/actions/runs/36289935169)가 `6280185`에서 전체 성공했다. Backend 436 tests와 실제 재시작 후 heartbeat export를 포함한다. 기존 R01/R02의 컨테이너 검증 대기는 이 실행으로 해소됐다. R07~R09 UI는 이 commit의 범위 밖이다.
