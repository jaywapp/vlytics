# 동기화 coverage 보고

작성일: 2026-09-20 · 범위: TASK-006

이 문서는 시즌·대회별 원천 일정과 V-Mirror 적재 결과를 대조하는 운영 기준이다. 현재 OP-001의 대량 수집 허용 범위와 요청 속도가 확정되지 않았으므로 실제 KOVO bulk collection은 비활성 상태다. 아래 실행 결과는 합성 source adapter로 검증했으며 실데이터 coverage를 주장하지 않는다.

## 실행 안전장치

- 시즌·대회 조합마다 `ops.sync_checkpoints`에 cursor, 단계, 완료 페이지·응답 수를 저장한다.
- 첫 페이지는 작은 validation 크기로 처리하고 성공한 뒤 batch 크기로 전환한다.
- 한 페이지의 모든 응답이 정상 적재된 뒤에만 cursor를 전진시킨다. 중단 시 마지막 완료 cursor부터 재개한다.
- HTTP 429의 `Retry-After`를 우선 적용하고, 없으면 제한된 지수 backoff를 사용한다. 5xx도 같은 제한 안에서 재시도한다.
- 각 요청은 rate limiter를 통과한다. 실제 어댑터는 OP-001의 permission policy, 양수 요청률·동시성, bulk 활성화가 모두 있어야 실행된다.
- `received_at`이 해당 요청 시작보다 이르면 수집을 거부한다. 과거 경기의 backfill도 관측 시각은 실제 수신 시각이며 경기 날짜로 소급하지 않는다.

## 현재 시즌 작업

`CurrentSeasonSyncPlanner`는 동일 시간 bucket에서 같은 job key를 만들어 중복 enqueue를 막는다.

| 작업 | 기본 주기 | 대상 |
|---|---:|---|
| `mirror.current_schedule` | 5분 | 현재 시즌·대회 일정 변경 |
| `mirror.final_result` | 5분 | 시작 시각이 지났고 final을 아직 관측하지 않은 경기 |
| `mirror.correction_recheck` | 6시간 | 첫 final 관측 뒤 7일 이내 경기 |

취소·연기 경기는 final polling 대상에서 제외한다. 결과 수정은 기존 revision의 UPDATE가 아니라 새 immutable result revision으로 저장한다.

## 대조 기준

예상 분모는 원천 schedule의 중복 제거된 source match code다. 적재 상태는 다음처럼 분류한다.

| 상태 | 의미 |
|---|---|
| `loaded` | 동일 source/그룹/시즌/대회/match code가 `mirror.matches`에 존재 |
| `missing` | 예상 목록에는 있으나 정상 적재가 없으며 구체적 실패 사유를 기록 |
| `not_supported` | 계약상 endpoint가 해당 경기 또는 데이터 종류를 제공하지 않으며 근거를 기록 |
| `unexpected` | V-Mirror에는 있으나 현재 원천 schedule 예상 목록에는 없는 경기 |

보고서는 예상·적재·누락·미지원·예상 외 합계를 항상 함께 출력한다. 누락과 미지원을 0이나 적재 성공으로 합치지 않는다. `reconcile_coverage` 결과의 생성 시각도 실제 실행 시각을 사용한다.

## 합성 검증 결과

합성 일정 3경기에서 적재 1, 계약 실패 누락 1, endpoint 미지원 1을 대조해 `3 = 1 + 1 + 1`을 확인한다. validation 페이지 완료 후 중단하고 같은 checkpoint store로 재실행했을 때 두 번째 cursor부터 이어지며 응답 코드는 한 번씩만 ingestion sink에 전달된다. 429 응답의 7초 `Retry-After`, 요청 간격 제한, 과거 `received_at` 거부도 자동 테스트로 검증한다.

## 실운영 전 남은 게이트

OP-001에 다음 근거와 값이 확정되어야 실제 source adapter를 추가·활성화할 수 있다.

1. KOVO 대량 자동수집 허용 범위와 재사용 범위의 근거
2. 승인된 분당 최대 요청 수와 최대 동시성
3. 소량 validation 실행의 응답 코드, 429/`Retry-After` 동작, 운영 연락·중지 절차
4. 승인 범위 안에서 시즌·대회별 실제 schedule 분모를 다시 읽은 reconciliation 결과

이 항목들이 설정에 반영되기 전에는 `CollectionGate`가 non-synthetic adapter를 `BulkCollectionBlocked`로 거부한다.

## 내구성·재시작 계약

TASK-005의 receipt-first 경계를 그대로 사용한다. HTTP 응답은 먼저 독립 트랜잭션으로 `mirror.raw_snapshots`에 커밋하고, 200 응답의 정규화 fact는 별도 원자 트랜잭션으로 커밋한다. 페이지의 모든 fact가 커밋된 뒤에만 `ops.sync_checkpoints`를 generation CAS로 전진시킨다. 중간 fact 실패 시 receipt와 quarantine 근거는 남고 checkpoint는 바뀌지 않아 재시작이 같은 cursor를 다시 처리한다. 같은 응답의 fact revision은 TASK-005 중복 방지 계약으로 중복 생성되지 않는다.

checkpoint update는 기대 generation이 현재 generation과 일치해야 하며 매 update마다 정확히 1 증가한다. DB trigger가 완료 checkpoint 재개방, `batch`에서 `validation`으로의 단계 회귀, page/response count 감소, 시각 회귀를 거부한다.

429와 5xx 응답도 재시도 전에 receipt-only 또는 quarantine receipt로 영속화한다. `Retry-After`는 줄이지 않는다. 승인된 최대 delay보다 길거나 job deadline에 닿는 값이면 sleep하지 않고 작업을 중단한다. timeout·connection 오류에는 bounded exponential backoff와 테스트에서 주입 가능한 deterministic jitter를 적용한다.

실 source의 limiter 값과 동시성 값은 OP-001 gate의 승인값 이하만 허용한다. 프로세스 간 동일 scope 동시성은 PostgreSQL advisory-lock slot으로 제한하며, 실제 adapter는 PostgreSQL lease 없이 실행할 수 없다.

`CurrentSeasonSyncPlanner`의 job은 `job_key` 충돌 시 기존 row를 그대로 반환한다. worker는 `ops.jobs`를 lease한 뒤 등록 handler를 실행하고 성공·retry_wait·failed·quarantined 상태로 전이한다. 반복 일정/final/정정 조회는 각 `ops.jobs` row 자체를 실행 checkpoint로 사용하므로 완료된 역사 backfill cursor에 막히지 않는다. 재시작하거나 같은 시간 bucket을 다시 계획해도 job row와 성공 실행은 중복되지 않는다.

coverage 대조는 원천 예상 목록에 없는 적재 경기(`loaded - expected`)를 `unexpected`로 별도 보고한다. 따라서 보고 합계는 예상·적재·누락·미지원과 함께 예상 외 적재 수를 포함한다.