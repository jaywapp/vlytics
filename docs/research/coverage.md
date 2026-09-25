# KOVO 원천 coverage

- 확인일: 2026-09-20 (Asia/Seoul)
- 대상: V리그 `gcode=001`
- 방법: 공식 시즌 목록 1회, 시즌별 정규리그 일정 001~022를 동시성 1·요청 간 1초로 조회, 완료 경기 상세 1건과 미래 경기 상세 1건 표본 확인
- 제한: 상세 4,460건을 내려받지 않았으며 실제 raw payload를 저장하지 않음

## coverage 상태 정의

| 상태 | 의미 |
|---|---|
| `available` | 지정 범위에서 경로와 필드가 직접 관측됨 |
| `missing` | 있어야 할 범위에서 응답이 비거나 필드가 없음 |
| `not_supported` | 확인한 endpoint가 해당 단위를 제공하지 않음 |
| `unverified` | 표본 또는 의미가 부족해 지원 여부를 확정할 수 없음 |

표본 한 건에서 관측한 필드를 전체 시즌 `available`로 확대하지 않는다. 아래 범위 열에 검증 단위를 함께 적는다.

## 시즌·경기 범위

시즌 목록은 23개 시즌 코드 `001`~`023`을 반환했다. `001`~`022` 정규리그 일정 합계는 4,460건으로 2026-09-19 제공 문서의 수치와 일치했다. `023`은 2026-27 시즌이며 조사 시점 일정 252건을 반환했다. 023은 아직 완료 시즌 coverage가 아니다.

| 시즌 코드 | 표시 연도 | 정규리그 일정 건수 |
|---|---:|---:|
| 001 | 2005 | 100 |
| 002 | 2005-2006 | 175 |
| 003 | 2006-2007 | 150 |
| 004 | 2007-2008 | 175 |
| 005 | 2008-2009 | 175 |
| 006 | 2009-2010 | 196 |
| 007 | 2010-2011 | 165 |
| 008 | 2011-2012 | 216 |
| 009 | 2012-2013 | 180 |
| 010 | 2013-2014 | 195 |
| 011 | 2014-2015 | 216 |
| 012 | 2015-2016 | 216 |
| 013 | 2016-2017 | 216 |
| 014 | 2017-2018 | 216 |
| 015 | 2018-2019 | 216 |
| 016 | 2019-2020 | 192 |
| 017 | 2020-2021 | 216 |
| 018 | 2021-2022 | 237 |
| 019 | 2022-2023 | 252 |
| 020 | 2023-2024 | 252 |
| 021 | 2024-2025 | 252 |
| 022 | 2025-2026 | 252 |
| **합계** | **22시즌** | **4,460** |

건수는 원천 `payload.page.totalElements`의 합이다. 일정 row 존재를 뜻하며 모든 경기의 상세·팀·선수 통계가 완전하다는 뜻은 아니다. 취소·중단·몰수·정정 분류도 전수 검증하지 않았다.

시즌 022 정규리그 252개 row에서는 `gnum` 고유값도 252개였고 중복 그룹은 없었다. `gender=1`과 `gender=2`가 각각 126건이었다. 이 결과는 `(seasonCode=022, leagueCode=201)` 안의 표본 검증이며 다른 대회까지 `gnum` 단독 유일성을 보장하지 않는다.

## 데이터 종류별 coverage

| 데이터 종류 | 상태 | 확인 범위 | 근거·주의 |
|---|---|---|---|
| 시즌 목록 | `available` | 001~023 metadata | `seasonCode`, 이름, 연도, 대회 배열 관측 |
| 정규리그 일정 | `available` | 001~022 총 4,460건, 023 252건 | 건수와 최소 key 확인 |
| PO·챔프 일정 | `unverified` | 코드 202·203 존재 | 시즌별 전체 건수·완전성 미집계 |
| 준PO | `unverified` | 시즌 023에 코드 204 존재 | 과거 적용 구간·일정 미검증 |
| 올스타·시범 | `unverified` | 과거 시즌 목록에 801·901 존재 | 첫 MVP 정규 cohort 밖, 상세 미검증 |
| 남녀부 구분 | `available` | 시즌 022 정규 126+126 | `gender` raw 값과 team context로 mapping, 공식 enum 문서 없음 |
| 경기 source key | `available` | 시즌 022 정규 | `gnum` 중복 0; 복합 key 사용 |
| 일정 시각 | `available` | 일정 endpoint | `gdate`, `gstime`; 시간대 명시 필드 없음, KOVO 현지 표시를 바로 UTC로 단정하지 않음 |
| 경기장 | `available` | 일정·상세 표본 | `place`, `city` 문자열. venue code 미확인 |
| 경기 총·세트 점수 | `available` | 완료 경기 상세 1건 표본 | 1~5세트 양 팀 점수와 총점 필드 관측. 전체 완전성 미검증 |
| 세트 소요시간 | `available` | 완료 경기 상세 1건 표본 | `s1Ptime`~`s5Ptime`, `sptime`; 단위 문자열 parsing 미확정 |
| 팀 경기 통계 | `available` | 완료 경기 상세 1건, 2행 | 숫자 약어 field 존재. 공식 의미·분모는 미확인 |
| 선수 경기 통계 | `available` | 완료 경기 상세 1건, 23행 | `pcode`와 숫자 약어 field 존재. 전체 경기 완전성 미확인 |
| 세트별 선수 기록 | `unverified` | 상세 endpoint 표본 | 별도 row가 없고 `ynS1`~`ynS5` 의미 미확인 |
| 랠리·문자중계 | `unverified` | 조사 경로 밖 | endpoint·형식·역사 보존 범위 미확인 |
| 선수 명단/라인업 | `unverified` | 미래 시즌 023 경기 상세 1건 | 선수·팀 배열 0행. T-60 공개 경로·시각을 판단할 수 없음 |
| 결과 정정 이력 | `unverified` | 명시 revision endpoint 없음 | 동일 key 재조회 hash 변화로만 탐지 예정 |
| 429·rate limit | `unverified` | 의도적 부하 시험 안 함 | 공개 요청 정책을 찾지 못했고 429를 유발하지 않음 |
| 이용·재배포 권한 | `unverified` | 이용약관 제23조 확인 | 영리 목적 제한은 확인, 자동 수집·개인 보관·비영리 공개의 명시 허용은 확인 못함 |

`available`은 접근 가능한 사실을 뜻하며 이용 허가를 뜻하지 않는다.

## 필드별 최소 coverage

| entity | 식별 필드 | 사실 필드 | 미확인/보류 |
|---|---|---|---|
| season | `gcode`, `seasonCode` | 이름, 표시 연도, 대회 목록 | season 시작·종료 UTC |
| competition | `seasonCode`, `leagueCode` | 원천 이름 | 모든 시즌의 stage mapping, 특례 |
| match | 복합 key `(gcode, seasonCode, leagueCode, gnum)` | 날짜·시각, 홈/원정 code, 장소 | 실제 시작시각과 예정시각 구분, status vocabulary |
| result | match key | 세트 승수 표현, 세트별 점수, 총점, 소요시간 | finality·정정 시각, 몰수/중단 규칙 |
| team match stats | match key + `tcode` | match-level raw 숫자 field | 공식 metric 이름·분모·단위 |
| player match stats | match key + `tcode` + `pcode` | match-level raw 숫자 field | pcode 장기 유일성, 공식 metric 이름·분모 |
| roster/lineup | 없음 | 미래 표본에서 row 없음 | 원천 key·공개 시각·revision |
| player set stats | 없음 | 확인하지 못함 | 전부 보류 |
| rally | 없음 | 확인하지 못함 | 전부 보류 |

## historical availability

현재 조회한 과거 응답에는 과거 당시의 관측 시각을 증명하는 revision 정보가 없다. 따라서 지금 수집하는 001~022 자료의 `observed_at`은 2026년 수집 시각이며 경기일로 소급하지 않는다.

| policy | 편입 조건 | 현재 적용 가능성 |
|---|---|---|
| `historical_reconstruction` | 현재 backfill한 경기 사실을 당시 이용 가능했다고 가정하는 한계를 명시 | 001~022 기본값 |
| `historical_point_in_time` | 당시 timestamp·원문 hash·revision을 증명하는 자료 존재 | 현재 확인 자료 없음 |
| `live_prospective` | 실제 경기 전부터 수집 receipt와 관측 시각 보존 | 023부터 구축 가능 |

세 policy의 성능을 한 cohort로 합치지 않는다. 경기 후 확인한 선수 row를 T-60 명단으로 소급하지 않는다.

## OP-001 blocker

다음 항목이 해결되기 전까지 실제 대량 Backfill을 활성화하지 않는다.

- KOVO의 자동 수집·보관·재배포 허용 범위
- production 요청 속도·동시성·정정 조회 주기
- 전체 상세 응답의 completeness audit와 예외 분류
- T-60 명단, 세트별 선수 기록, 랠리 자료의 실제 가용성

경로와 일정 건수 확인만으로 OP-001 전체가 해제되지 않는다.

## OP-006 blocker

다음 mapping은 조사 결과에서 만들지 않았다.

- 구단 인수·개명에 대한 franchise 연속성
- 시즌별 규칙·제도 변경 구간
- 장소 문자열의 동일 경기장/임시 홈구장 mapping
- 선수 code의 시즌 간 안정성

검증된 근거와 mapping version이 생길 때까지 source code를 그대로 분리 저장하고, 이름이 비슷하다는 이유로 병합하지 않는다. 이 범위가 해결되지 않은 팀·시즌은 학습·평가 편입 전에 격리한다.
