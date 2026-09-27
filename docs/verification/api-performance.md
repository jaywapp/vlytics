# API 조회 성능 검증

기록일: 2026-09-27

## 검증 범위

운영 API가 요청마다 전체 DB를 Python으로 읽던 경로를 endpoint별 PostgreSQL 조회로 분리한 뒤, 일정만이 아니라 History, Performance, Operations, Coverage와 기존 전체 `load()`를 같은 합성 DB에서 측정했다. Repository materialization과 FastAPI service 집계·Pydantic 검증·JSON 직렬화를 포함한 TestClient 응답 시간을 분리했다.

- 일정: UTC 시간 구간·division·competition·team을 SQL에서 제한한다.
- 상세: 대상 경기의 예측과 coverage만 조회한다.
- History: SQL 필터와 `(generated_at, id)` 내림차순 keyset, `limit + 1` lookahead를 사용한다.
- Performance: evaluator·cohort·경기 시각을 먼저 제한하고, 성공 prediction은 failure 집계 입력에서 제외한다.
- Operations: 상태·작업 유형 필터와 `(due_at, id)` 오름차순 keyset을 사용하고 예산은 SQL에서 집계한다.
- Coverage: source/scope/data-kind별 최신 행과 응답 필터를 적용한다.

## 재현 방법

[benchmark_api_reads.py](../../infra/scripts/benchmark_api_reads.py)는 migration만 적용된 **빈 disposable clone**을 요구한다. `mirror.matches`, `engine.predictions`, `engine.evaluations`, `ops.jobs`, `mirror.source_coverage`, `ops.provider_budget_reservations` 중 하나라도 행이 있으면 seed를 거부한다. 실제 source·Market·유료 Provider 호출은 하지 않는다.

DB URL 값은 파일이나 CLI 인자에 넣지 않고 환경변수로만 전달한다. 변수 이름은 바꿀 수 있지만 값이나 credential은 출력하지 않는다.

```powershell
cd backend
$env:VLYTICS_BENCHMARK_MIGRATOR_DATABASE_URL = '<disposable-clone migrator URL>'
$env:VLYTICS_BENCHMARK_READ_API_DATABASE_URL = '<read-api URL>'
.venv\Scripts\python.exe ..\infra\scripts\benchmark_api_reads.py `
  --output ..\docs\verification\api-performance-results.json `
  --warm-samples 20
```

스크립트는 bulk SQL로 fixture를 한 번 생성하고 관련 표를 `ANALYZE`한 뒤 측정한다. 각 endpoint의 기대 row 수, pagination 여부, HTTP 200, Performance sample·evaluation revision·calibration 표본 합을 검사하므로 0행 fast path나 잘못된 join도 성공으로 기록하지 않는다.

## Fixture와 측정 조건

- 22시즌 × 250경기 = 경기 5,500개
- Provider 3개 × 경기 5,500개 = prediction 16,500개, evaluation 16,500개
- jobs 5,500개, coverage 5,500개, budget reservation 5,500개
- PostgreSQL `vlytics_read_api_login`으로 실제 로그인하여 읽기 역할 권한으로 조회
- 각 경로 첫 새 client connection 1회와 warm 20회
- p95는 정렬한 20개 표본의 nearest-rank `ceil(0.95 × n)`
- latency 반복에서는 `tracemalloc`을 끄고, Python peak memory는 별도 1회 pass로 측정
- client peak는 Python allocation만 포함한다. PostgreSQL 메모리, psycopg native buffer, process RSS는 포함하지 않는다.
- cold client는 SQLAlchemy pool을 비운 뒤 첫 호출이다. PostgreSQL shared buffer나 OS cache를 비우지 않았으므로 서버 cold start를 뜻하지 않는다.

환경은 Windows 11, CPython 3.12.10, PostgreSQL 17.11, SQLAlchemy 2.0.43, psycopg 3.2.10이다. 원본 수치와 UTF-8/LF 정규화한 실행 코드 SHA256은 [api-performance-results.json](api-performance-results.json)에 있다. JSON에는 DB URL, host, token, password가 없다.

## Repository 결과

| 경로 | SQL 수 | 반환 cardinality | warm p50 | warm p95 | Python peak |
|---|---:|---|---:|---:|---:|
| Schedule | 3 | match 1, prediction 3 | 25.84 ms | 29.32 ms | 0.04 MiB |
| Match detail | 4 | match 1, prediction 3, coverage 1 | 27.85 ms | 32.58 ms | 0.04 MiB |
| History first page | 4 | match/prediction/evaluation 각 25, `has_more=true` | 88.18 ms | 96.96 ms | 0.25 MiB |
| Performance 전체 22시즌 | 3 | evaluation 16,500, 성공 prediction 0 | 571.62 ms | 619.28 ms | 64.20 MiB |
| Performance 최근 1시즌 | 3 | evaluation 750, 성공 prediction 0 | 60.84 ms | 67.68 ms | 2.98 MiB |
| Operations first page | 3 | job 25, budget 집계 6, `has_more=true` | 13.23 ms | 15.00 ms | 0.04 MiB |
| Coverage | 2 | coverage 5,500 | 36.60 ms | 39.43 ms | 4.95 MiB |
| 기존 전체 `load()` | 7 | match 5,500, prediction/evaluation 각 16,500, job/coverage 각 5,500 | 1,029.36 ms | 1,126.93 ms | 126.46 MiB |

## 실제 API 결과

| 경로 | 응답 검증 | warm p50 | warm p95 | Python peak |
|---|---|---:|---:|---:|
| Schedule | HTTP 200, item 1 | 28.75 ms | 33.33 ms | 0.08 MiB |
| History first page | HTTP 200, item 25, next cursor 있음 | 95.84 ms | 105.28 ms | 0.35 MiB |
| History second page | HTTP 200, item 25, next cursor 있음 | 84.00 ms | 95.10 ms | 0.35 MiB |
| Performance 전체 22시즌 | HTTP 200, 12 groups, sample/revision/calibration 각 16,500 | 1,491.45 ms | 1,736.89 ms | 67.35 MiB |
| Performance 최근 1시즌 | HTTP 200, 6 groups, sample/revision/calibration 각 750 | 95.40 ms | 108.53 ms | 3.29 MiB |
| Operations | HTTP 200, item 25, next cursor 있음 | 14.14 ms | 14.79 ms | 0.10 MiB |
| Coverage | HTTP 200, 집계 item 1 | 47.48 ms | 52.66 ms | 5.81 MiB |

History 두 페이지는 각각 25개이며 keyset 경계와 `has_more`를 실제 응답에서 확인한다. 전체 Performance 응답은 2.81MB였고 evaluation revision ID 16,500개를 그대로 직렬화한다. 최근 한 시즌 응답은 750개 표본만 집계한다.

## 판정과 한계

일정·상세·History·Operations·Coverage는 22시즌 fixture에서 전체 `load()`보다 반환 cardinality와 Python memory가 작다. 기존 일정 조회 warm p95 100ms 목표를 유지하고 충족했다.

B3 근거로 일정 측정만 제시한 이전 문서는 충분하지 않았다. 이번 측정은 원 감사가 요구한 endpoint별 cardinality, latency, client peak memory와 기존 전체 `load()` 비교를 채운다. 합성 로컬 회귀 budget은 일정 warm p95 100ms 미만, 나머지 목록 endpoint warm p95 150ms 미만, 최근 한 시즌 Performance p95 250ms·peak 16MiB 미만, 전체 22시즌 Performance p95 2초·peak 128MiB 미만으로 기록한다. 이는 이번 baseline에서 회귀를 탐지하기 위한 budget이며 production SLO는 아니다. 현재 결과는 모두 이 budget 안이다.

전체 22시즌 Performance는 SQL에서 성공 prediction 16,500개를 제거한 뒤에도 evaluation 16,500개를 Python으로 materialize하고, service가 group·calibration을 계산하며 revision ID를 모두 직렬화한다. warm p95 1.74초, peak 67.35MiB, 응답 2.81MB인 비용 경계는 유지된다. 사용자가 느끼는 응답이나 동시 부하에서 이 비용이 허용되지 않으면 허용 기간·비동기 사전 집계·응답 provenance 축약 중 제품 계약에 맞는 방식을 선택하고 별도 SLO로 다시 측정해야 한다.

이 결과는 단일 client 로컬 측정이며 production ingress/network, 동시 쓰기, PostgreSQL server memory, 다중 사용자 부하를 입증하지 않는다. 최종 실행은 빈 clone의 seed·ANALYZE부터 CLI로 수행했으며 prediction은 cutoff+5초 시작·+10초 완료로 생성했다. 저장된 evaluation cohort를 조회하는 성능 검증이며 실제 예측 생성이나 timing eligibility 재평가를 검증하지 않는다. 세 Provider가 성공한 fixture이고 실패 집중 부하나 통계 기준선·Market의 전체 규모 비교는 포함하지 않는다.
