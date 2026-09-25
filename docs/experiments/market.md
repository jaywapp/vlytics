# Market 후행 비교 계약

TASK-016의 Market 구현은 독립 예측이 끝난 뒤 고정된 시장 snapshot을 비교한다. 실제 외부
schema와 연결 정보는 OP-005가 해결될 때까지 알 수 없으므로 런타임 기본값은
`MissingMarketAdapter`다. 이 상태에서도 통계·AI 예측과 일반 결과 평가는 계속되며, 합성
fixture만으로 시장 계약과 계산을 검증한다. 합성 값은 실제 시장 성능 근거로 사용하지 않는다.

## 시점과 식별자

외부 `(source, source_event_id)`는 adapter가 내부 `match_id`에 명시적으로 매핑해야 한다.
매핑이 없거나 다른 경기로 연결되면 snapshot을 거부한다. snapshot과 각 line은
`quoted_at <= observed_at <= received_at`을 만족해야 하며 세 시각과 source가 일치해야 한다.
예측 cutoff 기준으로 세 시각이 모두 cutoff 이하인 snapshot만 선택한다. 과거 quote라도
`received_at`이 cutoff 뒤면 소급 사용하지 않는다. 해당 과거 quote만 존재하면 `late`, 선택한
quote가 `max_age`를 넘으면 `stale`, quote 자체가 없으면 `missing`이다. DB-backed adapter도
최신 as-of 행을 먼저 고른 뒤 동일한 상태 계약을 반환한다.

snapshot은 canonical JSON SHA-256과 함께 append-only로 저장한다. DB는
`source_event_mappings`의 `(source, source_event_id, match_id)` 외래 키와 시각 순서 CHECK를
직접 적용한다. 각 line은 `market_snapshot_lines`에 정규화하며 settlement의
`(market_snapshot_id, line_id)` 외래 키가 실제 snapshot membership을 보장한다. 결과 정산은
`market_snapshot_id + line_id + result_revision_id + evaluator_version`마다 새 행으로 저장한다.
결과 정정은 과거 정산을 수정하지 않고 새 result revision 정산을 추가한다.

## 예측 분포 소유권

`PredictionMarketInput`은 직접 생성할 수 없다. 저장소 resolver가 돌려준 검증된
`JointScoreDistribution`과 기대한 producer variant, input snapshot, match, cutoff를 factory에서
대조한 뒤에만 생성한다. winner, 세트스코어, 점수 총점, 점수 득실차는 모두 그 하나의
공동분포에서 다시 주변화한다. 호출자가 별도 확률 map을 공급하거나 일부 target만 바꾸는
경로는 없다.

## 라인 의미와 지원 범위

handicap line은 선택한 쪽의 값에 더하는 signed decimal이다. 따라서 홈 세트 `-1.5`는
`home_sets - away_sets - 1.5`가 양수일 때 cover다. 원정 반대편은 동일 계약에서 `+1.5`를
사용하며 두 line의 부호 합은 0이어야 한다. total은 `over`와 `under`, moneyline은 `home`과
`away` 두 선택을 같은 source·시각·정산 규칙으로 저장한다.

초기 지원 범위는 `full_match`의 정수·반점 line이다. moneyline, 세트 handicap, 점수
handicap, 세트 total, 점수 total을 지원한다. 세트 확률은 6개 세트스코어 분포에서, 점수
확률은 공동 점수 분포의 득실차·총점 주변분포에서만 계산한다. 필요한 분포가 없으면
`unsupported`이며 추정 분포를 만들지 않는다.

quarter line과 `set_n` 부분 경기 계약은 문서 구조를 보존할 수 있지만 evaluator는
`unsupported`로 반환하고 확률이나 정산을 계산하지 않는다. `final`과 `corrected` 결과는
정확한 revision으로 승·패·push를 정산한다. `void` 결과는 모든 line을 void 처리한다.
몰수·중단·재개 특례는 OP-005의 실제 공급원 정산 규칙이 확정되기 전까지 지원하지 않는다.

## 합성 검증

`fixtures/synthetic/market/full-match-v1.json`은 승패, 홈 세트 `-1.5`, 홈 점수 `-5`,
점수 O/U `180`, 세트 O/U `4`의 양쪽 line을 포함한다.
`unsupported-v1.json`은 quarter line과 부분 경기 계약이 임의 처리되지 않는지 확인한다.
단위 테스트는 late receipt, stale, 경기 identity, 결과 revision, win/loss/push/void를 함께
검증한다.