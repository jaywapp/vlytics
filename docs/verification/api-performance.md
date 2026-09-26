# API 조회 성능 검증

기록일: 2026-09-27

## 조회 범위

운영 API는 요청마다 전체 DB를 Python으로 읽는 대신 endpoint별 PostgreSQL 조회 범위를 사용한다.

- 일정: UTC 시간 구간·division·competition·team을 SQL에서 제한한다.
- 상세: 대상 경기의 예측과 coverage를 조회한다.
- History: SQL 필터와 `(generated_at, id)` 내림차순 keyset, `limit + 1` lookahead를 사용한다.
- 성능: cohort·evaluator·경기 시각을 먼저 제한한다. 비교에 필요한 통계·Market 기준선을 보존한 뒤 표시할 provider/model/result-finality를 적용한다.
- Operations: 상태·작업 유형 필터와 `(due_at, id)` 오름차순 keyset을 사용하며 예산을 SQL에서 집계한다.
- Coverage: source/scope/data-kind별 최신 행과 응답 필터를 적용한다.

service는 cursor revision과 필터 fingerprint를 확인한다. InMemory repository는 결정적 계약 테스트용 전체 snapshot을 유지한다.

## 22시즌 규모 측정

migration된 격리 PostgreSQL에 22시즌 × 250경기 = 합성 경기 5,500개와 경기별 schedule revision 1개를 삽입했다. 삽입·조회는 한 transaction에서 수행하고 rollback했다. DDL·migration·실제 source·Provider 호출은 수행하지 않았다.

환경은 Windows 11, CPython 3.12.10, PostgreSQL 17.11, SQLAlchemy 2.0.43, psycopg 3.2.10이다. DB와 client가 로컬에 있으며 네트워크 지연을 주입하지 않았다. 대상 날짜에는 36경기가 있고 기존 테스트 경기 28개를 포함한 전체 비교 대상은 5,528행이다.

| 측정 | 결과 |
|---|---:|
| 날짜로 제한한 경기 수 | 36 |
| 전체 경기 수 | 5,528 |
| 반환 행 감소율 | 99.35% |
| 제한 조회 warm 10회 중앙값 | 9.23 ms |
| 제한 조회 warm 10회 최댓값 | 39.52 ms |
| 전체 경기 materialization 3회 중앙값 | 43.89 ms |
| 합성 데이터 삽입 시간 | 2,232.4 ms |

이 환경의 목표는 repository 경계를 넘는 일정 행 500개 미만, 22시즌 규모 warm 조회 p95 100 ms 미만이다. 10회 최댓값을 보수적인 p95 대용치로 삼은 이번 측정에서는 목표를 만족했다.

## 검증과 한계

endpoint SQL을 실제 migration schema에서 실행하고, rollback 전용 Operations fixture로 중복·누락 없는 keyset 두 페이지를 확인했다. API 계약·cursor 불일치·오래된 revision·lifecycle·Market 출처·coverage·serializer도 회귀 검사한다.

이는 단일 client 로컬 측정이며 운영 부하 테스트가 아니다. 경기 수만 22시즌 규모이며 같은 기간의 prediction/evaluation/Market 데이터나 동시 쓰기는 포함하지 않았다. 최댓값은 10회 표본의 최대치로 정식 분포 p95가 아니다. 제한 조회 시간은 revision fingerprint를 포함하지만 전체 비교는 경기 materialization만 측정해 과거 전체 `load()` 비용보다 작다. production ingress/network 성능을 입증하지 않는다. 후속 API 정확성 보완 후 전체 장기 데이터 부하 측정은 별도로 필요하다.
