# KOVO 원천 계약 조사

- 확인일: 2026-09-20 (Asia/Seoul)
- 범위: 공개 KOVO 홈페이지와 `user-api.kovo.co.kr`의 소량 읽기 전용 조회
- 상태: 합성 계약 구현 가능, 대량 Backfill 활성화는 차단

## 결론

시즌 목록, 일정·결과, 경기 상세의 현재 경로와 응답 외형은 확인했다. 완료된 정규리그 22시즌의 일정 건수 4,460도 재확인했다. 그러나 이 API는 KOVO가 공개한 개발자용 명세가 아니며 자동 수집 허용량, 429 정책, 원문 장기 보관과 재배포 범위가 명시돼 있지 않다. 따라서 이 문서는 관측 가능한 데이터 계약을 제공하지만 대량 수집 허가의 근거로 사용하지 않는다.

KOVO 이용약관 제23조는 홈페이지에서 얻은 정보를 사전 승낙 없이 영리 목적으로 복제·송신·출판·배포·방송하거나 제3자가 이용하게 하는 행위를 제한한다. 이 조항만으로 개인용 자동 수집, 비영리 원문 보관, 공개 재배포가 허용됐다고 해석하지 않는다. OP-001의 권한·요청량 부분은 미해결이다.

## 확인한 공식 경로

기본 호스트는 `https://user-api.kovo.co.kr`이다. 아래 경로는 2026-09-20에 인증 헤더 없이 `GET` 200과 JSON 응답을 반환했다. 무인증 응답은 자동 수집 허가를 의미하지 않는다.

| 목적 | 경로 | 관측한 응답 |
|---|---|---|
| 시즌·대회 목록 | `GET /stat/season-list?gcode=001` | `result`, 시즌 배열 `payload` |
| 일정·결과 | `GET /stat/game-schedule?gcode=001&seasonCode={seasonCode}&leagueCode={leagueCode}` | `payload.content`, `payload.page` |
| 경기 상세 | `GET /stat/game-schedule/{gnum}?gcode=001&seasonCode={seasonCode}&leagueCode={leagueCode}` | `payload.game`, `player`, `team`, `ranking` |
| 약관 목록 | `GET /terms/FOOTER`, `Accept-Language: ko` | 약관 ID·제목 메타데이터 |
| 약관 본문 | `GET /terms/content/1`, `Accept-Language: ko` | 이용약관 버전 2, `effectiveAt=2025-12-11T15:10:06` |

근거 링크: [시즌 목록](https://user-api.kovo.co.kr/stat/season-list?gcode=001), [정규리그 일정 예시](https://user-api.kovo.co.kr/stat/game-schedule?gcode=001&seasonCode=022&leagueCode=201), [KOVO 통합서비스 이용약관](https://kovo.co.kr/terms/1), [robots 경로](https://kovo.co.kr/robots.txt).

경기 상세 URL은 실제 `gnum`을 포함하므로 이 저장소에 실 URL과 원문을 남기지 않았다. 공개 fixture에도 실제 응답을 넣지 않았다.

## 이용 조건과 요청 정책

| 항목 | 2026-09-20 관측 | 적용 |
|---|---|---|
| 이용약관 | 약관 API가 버전 2와 2025-12-11 적용 시각을 반환했다. 제23조는 KOVO 작성물의 권리 귀속과 사전 승낙 없는 영리 목적 이용 제한을 둔다 | 개인용 분석 외의 공개·수익화·원문 재배포 전 별도 확인 필요 |
| `robots.txt` | `/robots.txt`가 HTTP 200 `text/html`로 SPA shell을 반환했다. `text/plain` robots 규칙을 확인할 수 없었다 | 허용 또는 금지의 근거로 해석하지 않음 |
| 공개 API 문서 | 공식 개발자 명세, 요청 속도, 동시성, 일일 한도, 429 응답 계약을 찾지 못했다 | production 요청 속도 미설정, bulk disabled |
| 성공 응답 헤더 | 표본 200 응답에 `Retry-After`, 명시적 rate-limit 헤더, `X-Robots-Tag`가 없었다 | 제한이 없다는 뜻으로 해석하지 않음 |
| 조사 요청 | `GET`만 사용했고 완료 시즌 일정 확인은 동시성 1, 요청 간 1초 간격으로 수행했다 | 조사 방식일 뿐 허용 요청량으로 확정하지 않음 |

실수집 활성화 전 KOVO에 자동 수집 목적, 개인 분석용 원문 보관, 요청 빈도·동시성, 정정 재조회, 공개 서비스 시 집계 결과와 원문 재배포 범위를 확인해야 한다. 확인 전에는 합성 adapter만 활성화한다.

수집기가 적용할 방어 동작은 다음과 같다. 이는 KOVO가 승인한 정책이 아니라 원천 보호를 위한 내부 계약이다.

1. `GET`만 사용하고 동시성은 1로 시작한다.
2. 429에서는 payload를 파싱하지 않고 `Retry-After`가 있으면 우선 적용한다.
3. `Retry-After`가 없으면 지수 backoff와 jitter를 적용하고 해당 batch를 중단할 수 있어야 한다.
4. 반복 429, 403 또는 약관·robots 변화가 있으면 자동 수집을 중단하고 운영 확인 상태로 전환한다.
5. OP-001 요청 속도가 명시적으로 정해질 때까지 1초 간격의 이번 조사값을 production 기본값으로 복사하지 않는다.

## 식별자 계약

| 대상 | 원천 필드 | 내부 계약 | 검증 상태 |
|---|---|---|---|
| 시즌 | `gcode`, `seasonCode` | 문자열 그대로 보존. `seasonCode`의 선행 0 유지 | 001~023 관측 |
| 대회 | `leagueCode` | 시즌과 함께 보존하고 내부 stage 매핑은 버전 관리 | 코드·이름 관측, 시즌별 존재 차이 있음 |
| 경기 | `gnum` | `(gcode, seasonCode, leagueCode, string(gnum))` | 시즌 022 정규리그 252건에서 `gnum` 중복 0. 전 대회 전역 유일성 미확인 |
| 부문 | `gender` | 관측 mapping `1=men`, `2=women`; raw 값도 보존 | 시즌 022에서 각 126건, 공식 enum 명세 없음 |
| 팀 | `hcode`, `acode`, 상세의 `tcode` | 시즌별 source team code. 이름으로 식별하지 않음 | 경기·상세 표본 관측. franchise 연속성 미확인 |
| 선수 | `pcode` | raw player code와 시즌·팀·경기 row key를 함께 보존 | 상세 표본 관측. 시즌 간 전역 유일성·재사용 미확인 |
| 경기장 | `place`, `city` | 원문 문자열 보존, 검증된 venue identity가 생길 때까지 자동 병합 금지 | 별도 venue code는 표본에서 확인하지 못함 |

시즌 목록에서 관측한 대회 코드는 `200` 시즌 합계, `201` 정규리그, `202` 플레이오프, `203` 챔피언결정전, `204` 준플레이오프, `801` 올스타전, `901` 시범경기다. 모든 시즌에 모든 코드가 존재하지 않는다. 정규리그를 기본 cohort로 두고 PO·챔프는 분리하며 기타 대회는 자동으로 정규리그에 합치지 않는다.

## 응답 필드 계약

여기서 `필수`는 KOVO의 장기 보장을 뜻하지 않는다. 현재 parser가 응답 종류와 source key를 안전하게 식별하기 위한 최소 요구다. `선택`은 표본에서 관측했지만 경기 상태에 따라 비거나 없을 수 있다. `미확인`은 필드가 있어도 의미를 확정하지 않은 상태다.

### 시즌 목록

| 분류 | 필드 | 처리 |
|---|---|---|
| 필수 | `result.status`, `result.message`, `payload[]` | envelope 검증 |
| 필수 | `gcode`, `seasonCode`, `seasonName`, `ryear`, `leagues[]` | source season 생성 |
| 필수 | `leagues[].leagueCode`, `leagueName` | source competition 후보 생성 |
| 선택 | `remark`, `num`, `rounds`, `cpart` | 원문 보존, 의미 확인 전 typed mapping 금지 |

### 일정·결과

| 분류 | 필드 | 처리 |
|---|---|---|
| 필수 | `payload.content[]`, `payload.page` | 페이지 건수와 content 수를 검증 |
| 필수 | `seasonCode`, `leagueCode`, `gnum`, `gender` | 복합 source key와 부문 |
| 필수 | `gdate`, `gstime`, `hcode`, `acode` | 일정 revision 최소 입력 |
| 선택 | `seasonName`, `leagueName`, `hname`, `aname`, `hsname`, `asname`, `place`, `city` | 표시값과 관측 사실로 보존 |
| 선택 | `getime`, `spectators`, `weather`, 심판·중계 관련 문자열 | 사후에 채워질 수 있으므로 null/빈 값과 0을 구별 |
| 선택 | `hspoint`, `aspoint`, `hs1point`~`hs5point`, `as1point`~`as5point`, `s1Ptime`~`s5Ptime`, `sptime` | 완료 상태와 함께 해석. 미진행 세트의 0/빈 값만으로 실제 0점 세트를 만들지 않음 |
| 미확인 | `result`, `gpart`, `score`, `rank`의 전체 vocabulary | raw 보존, enum을 추측하지 않음 |

### 경기 상세

| 분류 | 필드 | 처리 |
|---|---|---|
| 필수 | `payload.game`, `player[]`, `team[]`, `ranking` | 배열은 미래 경기에서 비어 있을 수 있음 |
| 필수 | `game`의 일정 최소 필드 | 일정 응답과 같은 source key인지 대조 |
| 필수 | 선수 row의 `season`, `gnum`, `tcode`, `pcode` | match-level player row 식별 |
| 필수 | 팀 row의 `season`, `gnum`, `tcode` | match-level team row 식별 |
| 선택 | `point`, `warning`, `err`, `terr` 및 숫자 약어 통계 | 필드·타입만 보존. 공식 metric 정의 확인 후 승격 |
| 미확인 | 선수·팀의 `att/ats/...`, `st/ss/...`, `bt/bs/...`, `dt/ds/...`, `sett/sets/...`, `rt/rs/...` 약어 의미·분모 | 이름만 보고 공격·서브·블로킹·디그·세트·리시브 수식으로 확정하지 않음 |
| 미확인 | `ynS1`~`ynS5` | 세트별 출전·기록으로 해석하지 않음 |
| 미확인 | `ranking` 동적 team-code 객체와 하위 배열 의미 | feature 입력으로 사용하지 않음 |

알려지지 않은 추가 필드는 원문에 보존하되 parser가 조용히 typed fact로 승격하지 않는다. 필수 필드 누락, 타입 변화, request key와 body key 불일치는 `source_contract_error`로 격리한다.

## 관측·정정·429 계약

각 요청은 `requested_at`, `received_at`, redacted URL/request key, HTTP status, 응답 bytes의 SHA-256, parser version을 별도 receipt로 남긴다. 현재 backfill 시각을 과거 경기 시각으로 소급하지 않는다.

| 조건 | 동작 |
|---|---|
| 같은 request key, 같은 body hash | 새 receipt는 보존하고 fact revision은 추가하지 않음 |
| 같은 request key, 다른 body hash | 원문 receipt와 새 fact revision을 append. 이전 revision을 수정·삭제하지 않음 |
| 필수 필드 누락·타입 변화 | 원문 receipt 보존, parser 결과 격리, coverage를 `missing` 또는 `unverified`로 기록 |
| 429 | 응답 원문이 있으면 private receipt로 보존, fact parse 금지, `Retry-After` 우선 재시도 |
| 403 | 자동 수집 중단, 권한 재확인 |
| 일시 5xx·네트워크 오류 | 제한된 재시도 후 누락 사유 기록 |

KOVO가 정정 시각, revision ID, ETag 또는 최종 확정 시점을 제공하는지는 확인하지 못했다. 정정 여부는 동일 key의 body hash 변화로만 탐지하는 보수적 계약을 사용한다.

## 계약 파일과 공개 fixture

- [계약 README](../../contracts/source/README.md)
- [시즌 목록 schema](../../contracts/source/kovo-season-list.v1.schema.json)
- [일정 schema](../../contracts/source/kovo-game-schedule.v1.schema.json)
- [경기 상세 schema](../../contracts/source/kovo-game-detail.v1.schema.json)
- [관측 시나리오 schema](../../contracts/source/kovo-source-observations.v1.schema.json)
- [합성 fixture](../../fixtures/synthetic/)

fixture의 팀·선수·경기·시각·점수·hash는 전부 가공 값이다. `gcode`, `leagueCode`, 필드명처럼 parser 계약에 필요한 식별자만 관측 구조를 반영한다.

## OP-001 상태

확인된 부분:

- 시즌·정규리그 일정·경기 상세의 현재 경로와 응답 외형
- 시즌 목록 001~023과 완료 시즌 001~022 정규리그 일정 4,460건
- 세트 점수 필드, match-level 팀·선수 통계 row의 표본 존재
- 이용약관 버전·적용 시각·제23조와 유효한 robots 지시 부재

미해결 부분:

- 자동 수집, 원문 보관, 파생 DB 생성, 비영리/영리 공개와 재배포의 명시적 허용 범위
- 허용 요청 속도·동시성·일일 한도와 실제 429/`Retry-After` 계약
- 전체 4,460경기 상세의 완전성, 정정 빈도·최종 확정 시점
- T-60 선수 명단 경로와 공개 시각
- 세트별 선수 기록, 랠리/문자중계의 형식·역사 보존 범위

이 항목이 해결될 때까지 production backfill은 활성화하지 않는다.

## OP-006 상태

확인된 부분은 시즌·대회 코드, 경기별 팀 코드·경기장 문자열, 그리고 과거 backfill 관측 시각을 현재로 기록해야 한다는 availability 경계다. 다음은 미해결이다.

- 인수·개명 구단의 검증된 `franchise_id` mapping과 Elo 이월/추가 회귀 근거
- 시즌별 경기 규칙·제도 변경의 정확한 적용 구간
- 경기장 명칭 변경·임시 홈구장의 identity mapping
- 선수 code의 시즌 간 안정성·재사용 여부
- 과거 당시 공개 시점을 증명할 수 있는 roster·정정 자료

검증 전에는 이름 유사성으로 구단·선수·경기장을 자동 병합하지 않는다. 과거 현재시점 backfill은 `historical_reconstruction`, 당시 관측 증거가 있는 자료만 `historical_point_in_time`, 앞으로 수집하는 자료는 `live_prospective`로 분리한다.
