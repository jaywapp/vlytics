# KOVO source contracts

이 디렉터리는 2026-09-20에 소량 조회로 관측한 KOVO 공개 응답의 경계를 정의한다. KOVO가 공개한 정식 API 명세가 아니며, 필드 의미·요청 허용량·장기 호환성을 보장하지 않는다.

## 계약 목록

| 파일 | 대상 |
|---|---|
| `kovo-season-list.v1.schema.json` | `GET /stat/season-list` 응답 |
| `kovo-game-schedule.v1.schema.json` | `GET /stat/game-schedule` 응답 |
| `kovo-game-detail.v1.schema.json` | `GET /stat/game-schedule/{gnum}` 응답 |
| `kovo-source-observations.v1.schema.json` | 내부 수집 receipt·정정·429 합성 시나리오 |

원천 계약은 앞으로 필드가 추가되는 경우를 막지 않도록 `additionalProperties: true`를 사용한다. 필수 필드는 현재 parser가 source key와 관측 종류를 구분하는 데 필요한 최소값이다. 필드가 없거나 타입이 바뀌면 값을 추측하거나 `0`으로 채우지 않고 계약 오류로 격리한다.

`gnum`은 원천 JSON에서 정수로 관측됐지만 내부 source key를 만들 때 문자열로 변환한다. 경기 key는 `(gcode, seasonCode, leagueCode, gnum)`이다. 이 복합 key의 모든 대회·시즌에 대한 전역 유일성은 아직 검증하지 않았으므로 원천 request key도 함께 보존한다.

관측 receipt의 request key는 endpoint별로 다르다. `season_list`는 `gcode`, `game_schedule`은 `gcode + seasonCode + leagueCode`, `game_detail`은 여기에 `gnum`까지 필수다. HTTP 2xx의 `payload`는 해당 endpoint schema 전체와 일치해야 하며, 429 등 비성공 응답은 원천 오류 객체 또는 `null`일 수 있다.

`player`와 `team`의 통계 약어는 필드 존재와 숫자 타입만 관측했다. 공식 의미를 확인하기 전에는 typed metric으로 승격하지 않는다. 특히 `ynS1`~`ynS5`를 세트별 출전 또는 세트별 선수 기록으로 해석하지 않는다.

공개 fixture는 모두 `fixtures/synthetic/`의 가공 데이터다. 실제 KOVO 응답, 팀명, 선수명, 원문 해시를 포함하지 않는다.
